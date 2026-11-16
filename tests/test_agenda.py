"""Google Agenda avec une fausse API : aucune requête réelle. python -m unittest tests.test_agenda

Référence : jeudi 5 novembre 2026, 14 h 30. Calendriers : principal (Dan), Étudiant, Jarvis test, un calendrier
partagé en lecture seule et les jours fériés.
"""

import copy
import datetime as dt
import io
import json
import unittest
import urllib.error
from unittest import mock

from jarvis.agent import Agent, _private
from jarvis.tools import Toolbox
from jarvis.tools import agenda as agenda_module
from jarvis.tools import dates
from jarvis.tools import windows as win
from jarvis.tools.agenda import Agenda, Settings
from jarvis.tools.google_auth import NotConnected
from jarvis.tools.google_calendar import ApiError, CalendarApi, CalendarError

NOW = dt.datetime(2026, 11, 5, 14, 30, tzinfo=dates.TZ)
MAIN, STUDENT, TEST, CLUB = "dan@gmail.com", "etudiant-id", "test-id", "club-id"
HOLIDAYS = "fr.french#holiday@group.v.calendar.google.com"
CALENDARS = [
    {"id": MAIN, "summary": "Dan", "accessRole": "owner", "primary": True},
    {"id": STUDENT, "summary": "Étudiant", "accessRole": "writer"},
    {"id": TEST, "summary": "Jarvis test", "accessRole": "owner"},
    {"id": CLUB, "summary": "Club de foot", "accessRole": "reader"},
    {"id": HOLIDAYS, "summary": "Jours fériés en France", "accessRole": "reader"},
]


def event(event_id, title, day, start, end, **extra):
    """Événement brut, comme l'API le renvoie."""
    day = dt.date(2026, 11, day)
    return {"id": event_id, "summary": title, "etag": '"{}"'.format(event_id), "iCalUID": event_id + "@google.com",
            "start": {"dateTime": dates.at(day, dt.time(*start)).isoformat()},
            "end": {"dateTime": dates.at(day, dt.time(*end)).isoformat()}, **extra}


def all_day(event_id, title, day):
    return {"id": event_id, "summary": title, "etag": '"x"', "start": {"date": "2026-11-{:02d}".format(day)},
            "end": {"date": "2026-11-{:02d}".format(day + 1)}}


class FakeApi:
    """Agenda en mémoire : lit, crée, modifie et supprime comme Google, et garde les écritures."""

    def __init__(self, events=None):
        self.store = {MAIN: [], STUDENT: [], TEST: [], CLUB: [], HOLIDAYS: [], **copy.deepcopy(events or {})}
        self.writes = []
        self.auth = mock.Mock()

    def calendars(self):
        return CALENDARS

    def events_many(self, ids, start, end):
        found = {}
        for cid in ids:
            items = []
            for raw in self.store.get(cid, []):
                e = agenda_module._event(agenda_module.Calendar(cid, "", "owner"), raw)
                if e and e.start < end and e.end > start:
                    items.append(raw)
            found[cid] = items
        return found

    def insert(self, cid, body, send_updates="none"):
        self.writes.append(("insert", cid, body, send_updates))
        self.store[cid].append({**body, "etag": '"new"'})
        return body

    def patch(self, cid, event_id, body, etag="", send_updates="none"):
        self.writes.append(("patch", cid, event_id, body, etag, send_updates))
        raw = next(r for r in self.store[cid] if r["id"] == event_id)
        raw.update(body)
        return raw

    def delete(self, cid, event_id, etag="", send_updates="none"):
        self.writes.append(("delete", cid, event_id, etag, send_updates))
        self.store[cid] = [r for r in self.store[cid] if r["id"] != event_id]

    def move(self, cid, event_id, destination, send_updates="none"):
        self.writes.append(("move", cid, event_id, destination, send_updates))
        raw = next(r for r in self.store[cid] if r["id"] == event_id)
        self.store[cid].remove(raw)
        self.store[destination].append(raw)
        return raw


def make(events=None, heard="", **settings):
    api = FakeApi(events)
    return Agenda(api, Settings("dan@gmail.com", **settings), lambda: heard, lambda: NOW), api


def ask_then_run(agenda, action, **args):
    """(question posée, résultat) : comme l'agent, prepare puis exécution."""
    ready = agenda.prepare(action, **args)
    if not ready.ok:
        return None, ready
    return ready.message, agenda.run(action, **args)


class ReadTest(unittest.TestCase):
    EVENTS = {
        MAIN: [event("sport", "Séance de sport", 6, (18, 0), (19, 0))],
        STUDENT: [event("maths", "Cours de maths", 6, (9, 0), (12, 0)),
                  event("libre", "Pause", 6, (12, 0), (13, 0), transparency="transparent")],
        CLUB: [event("match", "Match", 7, (10, 0), (12, 0))],
        HOLIDAYS: [all_day("ferie", "Armistice", 11)],
    }

    def test_day(self):
        agenda, _ = make(self.EVENTS)
        self.assertEqual(agenda.run("list", when="demain").message,
                         "Vendredi 6 novembre, tu as 3 événements : Cours de maths de 9 h à 12 h (Étudiant), Pause "
                         "de 12 h à 13 h (Étudiant) et Séance de sport de 18 h à 19 h.")

    def test_empty_day_and_named_calendar(self):
        agenda, _ = make(self.EVENTS)
        self.assertEqual(agenda.run("list", when="lundi").message, "Lundi 9 novembre, tu n'as rien de prévu.")
        self.assertIn("dans le calendrier Étudiant : Cours de maths",
                      agenda.run("list", when="demain", calendar="mon calendrier étudiant").message)

    def test_week_is_grouped_by_day(self):
        agenda, _ = make(self.EVENTS)
        text = agenda.run("list", when="cette semaine").message
        self.assertTrue(text.startswith("Du jeudi 5 au dimanche 8 novembre : 4 événements."), text)
        self.assertIn("\nSamedi 7 : Match de 10 h à 12 h (Club de foot).", text)

    def test_free_slots_take_every_calendar_into_account(self):
        agenda, _ = make(self.EVENTS)
        # « Pause » est marquée disponible ; les jours fériés ne comptent pas
        self.assertEqual(agenda.run("free", when="demain").message,
                         "Vendredi 6 novembre, tu es libre de 8 h à 9 h, de 12 h à 18 h et de 19 h à 20 h.")
        self.assertEqual(agenda.run("free", when="mercredi 11 novembre").message,
                         "Mercredi 11 novembre, tu es libre tout le temps entre 8 h et 20 h.")
        self.assertEqual(agenda.run("free", when="aujourd'hui").message,  # il est 14 h 30
                         "Jeudi 5 novembre, tu es libre de 14 h 30 à 20 h.")
        week = agenda.run("free", when="cette semaine").message
        self.assertIn("Samedi 7 : de 8 h à 10 h et de 12 h à 20 h.", week)

    def test_unreadable_calendar_is_reported_not_hidden(self):  # essai réel : « rien de prévu » alors que faux
        agenda, api = make(self.EVENTS)
        read = api.events_many
        api.events_many = lambda ids, start, end: {**read(ids, start, end),
                                                   STUDENT: CalendarError("Google Agenda est injoignable.")}
        text = agenda.run("list", when="demain").message
        self.assertIn("Séance de sport", text)
        self.assertTrue(text.endswith("\nAttention : je n'ai pas pu lire Étudiant, Google ne répond pas."), text)
        self.assertIn("je n'ai pas pu lire Étudiant", agenda.run("free", when="demain").message)
        _, result = ask_then_run(agenda, "add", title="Sport", when="lundi 18 h")
        self.assertIn("je n'ai pas pu vérifier les conflits dans Étudiant", result.message)

    def test_a_failed_read_is_retried_once(self):
        answers = [urllib.error.URLError("timeout"), {"items": [{"id": "x"}]}]

        def opener(request, timeout=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(json.dumps(answer).encode())

        api = CalendarApi(mock.Mock(access_token=lambda: "jeton"), opener)
        self.assertEqual(api.calendars(), [{"id": "x"}])
        answers[:] = [urllib.error.URLError("timeout")]
        with self.assertRaises(CalendarError):  # une écriture n'est jamais refaite seule
            api.request("DELETE", "/calendars/x/events/y")

    def test_calendars(self):
        agenda, _ = make()
        self.assertEqual(agenda.run("calendars").message,
                         "Tu as 5 calendriers. Je peux écrire dans : ton agenda principal, Étudiant et Jarvis test. "
                         "En lecture seule : Club de foot et Jours fériés en France.")

    def test_not_connected(self):
        agenda, api = make()
        api.events_many = mock.Mock(side_effect=NotConnected)
        self.assertIn("python -m jarvis.tools.agenda login", agenda.run("list", when="demain").message)

    def test_reads_never_ask(self):
        agenda, _ = make()
        for action in ("list", "free", "calendars"):
            self.assertEqual(agenda.guard(action).verdict.value, "allow")


class AddTest(unittest.TestCase):
    def test_simple_event_is_created_without_question(self):
        agenda, api = make()
        question, result = ask_then_run(agenda, "add", title="Séance de sport", when="vendredi à 18 h",
                                        duration="une heure")
        self.assertEqual(question, "")
        self.assertEqual(result.message, "J'ai ajouté « Séance de sport » vendredi 6 novembre de 18 h à 19 h dans "
                                         "ton agenda principal.")
        _, cid, body, updates = api.writes[0]
        self.assertEqual((cid, body["summary"], updates), (MAIN, "Séance de sport", "none"))
        self.assertEqual(body["start"], {"dateTime": "2026-11-06T18:00:00+01:00", "timeZone": "Europe/Paris"})
        self.assertEqual(body["end"]["dateTime"], "2026-11-06T19:00:00+01:00")
        self.assertEqual(len(body["id"]), 32)  # choisi par Jarvis : une création refaite ne fait pas de doublon

    def test_calendar_is_chosen_by_name(self):
        agenda, api = make()
        ask_then_run(agenda, "add", title="TD", when="lundi 9 h", calendar="mon calendrier étudiant")
        ask_then_run(agenda, "add", title="Essai", when="lundi 11 h", calendar="jarvis test")
        self.assertEqual([w[1] for w in api.writes], [STUDENT, TEST])

    def test_calendar_forgotten_by_the_model_is_taken_from_the_request(self):
        agenda, api = make(heard="ajoute le TD lundi à 9 h dans mon calendrier étudiant")
        _, result = ask_then_run(agenda, "add", title="TD", when="lundi 9 h")
        self.assertIn("dans le calendrier Étudiant", result.message)
        self.assertEqual(api.writes[0][1], STUDENT)

    def test_invented_duration_is_ignored(self):  # essai réel : duration="jarvis test", puis « 1 minute » inventé
        said = "ajoute un test jarvis demain à 10 h dans mon calendrier jarvis test"
        for invented in ("jarvis test", "1 minute"):
            agenda, api = make(heard=said)
            _, result = ask_then_run(agenda, "add", title="test jarvis", when="demain à 10 h", duration=invented)
            self.assertEqual(result.message, "J'ai ajouté « test jarvis » vendredi 6 novembre de 10 h à 11 h dans le "
                                             "calendrier Jarvis test.", invented)

    def test_duration_said_by_the_user_is_kept(self):
        agenda, _ = make(heard="ajoute du sport vendredi à 18 h pendant deux heures")
        _, result = ask_then_run(agenda, "add", title="Sport", when="vendredi 18 h", duration="2 h")
        self.assertIn("de 18 h à 20 h", result.message)

    def test_calendar_of_the_original_request_after_a_correction(self):  # essai réel : parti dans le principal
        busy = {TEST: [event("t", "test jarvis", 6, (10, 0), (11, 0))]}
        agenda, api = make(busy, heard="ajoute un essai jarvis demain à 10 h 30 dans mon calendrier jarvis test\n"
                                       "ajoute-le quand même")
        _, result = ask_then_run(agenda, "add", title="essai jarvis", when="demain à 10 h 30")
        self.assertIn("dans le calendrier Jarvis test", result.message)
        self.assertEqual(api.writes[0][1], TEST)

    def test_empty_calendar_name_filters_nothing(self):  # le modèle met « mon calendrier »
        agenda, _ = make({STUDENT: [event("maths", "Cours de maths", 6, (9, 0), (12, 0))]})
        self.assertIn("Cours de maths", agenda.run("list", when="demain", calendar="mon calendrier").message)

    def test_read_only_or_unknown_calendar(self):
        agenda, api = make()
        _, result = ask_then_run(agenda, "add", title="Match", when="samedi 10 h", calendar="club de foot")
        self.assertIn("lecture seule", result.message)
        _, result = ask_then_run(agenda, "add", title="Match", when="samedi 10 h", calendar="boulot")
        self.assertIn("Je ne trouve pas le calendrier « boulot »", result.message)
        self.assertEqual(api.writes, [])

    def test_conflict_in_another_calendar_proposes_a_free_slot(self):
        agenda, api = make({STUDENT: [event("maths", "Cours de maths", 6, (17, 30), (18, 30))]})
        question, result = ask_then_run(agenda, "add", title="Séance de sport", when="vendredi 18 h")
        self.assertTrue(question.startswith("Vendredi 6 novembre de 18 h à 19 h, tu as déjà « Cours de maths » de "
                                            "17 h 30 à 18 h 30 (Étudiant). Je le mets plutôt de 18 h 30 à 19 h 30 ?"),
                        question)
        self.assertIn("ajoute-le quand même", question)
        self.assertIn("de 18 h 30 à 19 h 30", result.message)  # « oui » : le créneau proposé
        self.assertEqual(api.writes[0][2]["start"]["dateTime"], "2026-11-06T18:30:00+01:00")

    def test_overlap_kept_on_request(self):
        agenda, api = make({STUDENT: [event("maths", "Cours de maths", 6, (17, 30), (18, 30))]})
        question, result = ask_then_run(agenda, "add", title="Sport", when="vendredi 18 h", overlap=True)
        self.assertEqual(question, "")
        self.assertIn("Attention, ça chevauche « Cours de maths »", result.message)

    def test_add_it_anyway(self):  # essai réel : le modèle relance sans overlap après « ajoute-le quand même »
        busy = {STUDENT: [event("maths", "Cours de maths", 6, (17, 30), (18, 30))]}
        agenda, _ = make(busy, heard="ajoute du sport vendredi à 18 h\najoute-le quand même")
        question, result = ask_then_run(agenda, "add", title="Sport", when="vendredi 18 h")
        self.assertEqual(question, "")
        self.assertIn("de 18 h à 19 h", result.message)
        self.assertIn("Attention, ça chevauche « Cours de maths »", result.message)

    def test_free_or_declined_events_are_not_conflicts(self):
        agenda, _ = make({MAIN: [event("dispo", "Télétravail", 6, (8, 0), (20, 0), transparency="transparent"),
                                 event("non", "Apéro", 6, (18, 0), (20, 0),
                                       attendees=[{"email": "dan@gmail.com", "self": True,
                                                   "responseStatus": "declined"}])],
                          HOLIDAYS: [all_day("f", "Fête", 6)]})
        question, _ = ask_then_run(agenda, "add", title="Sport", when="vendredi 18 h")
        self.assertEqual(question, "")

    def test_whole_day_and_missing_hour(self):
        agenda, api = make()
        _, result = ask_then_run(agenda, "add", title="Anniversaire de Paul", when="toute la journée samedi")
        self.assertEqual(result.message, "J'ai ajouté « Anniversaire de Paul » samedi 7 novembre, toute la journée "
                                         "dans ton agenda principal.")
        self.assertEqual(api.writes[0][2]["start"], {"date": "2026-11-07"})
        _, result = ask_then_run(agenda, "add", title="Réunion", when="lundi")
        self.assertFalse(result.ok)
        self.assertIn("À quelle heure", result.message)

    def test_past_date_is_refused(self):
        agenda, api = make()
        _, result = ask_then_run(agenda, "add", title="Réunion", when="hier à 10 h")
        self.assertIn("est déjà passé", result.message)
        self.assertEqual(api.writes, [])


class InvitationTest(unittest.TestCase):
    def test_invitations_always_ask_and_are_sent(self):
        agenda, api = make(heard="ajoute une réunion vendredi à 15 h avec alex@gmail.com et thomas@gmail.com")
        question, result = ask_then_run(agenda, "add", title="Réunion", when="vendredi 15 h",
                                        attendees="alex@gmail.com, thomas@gmail.com")
        first, second = question.split("\n")
        self.assertEqual(first, "Je crée « Réunion » vendredi 6 novembre de 15 h à 16 h dans ton agenda principal et "
                                "j'invite alex arobase gmail point com et thomas arobase gmail point com ?")
        self.assertEqual(second, "Invités : alex@gmail.com, thomas@gmail.com")
        _, _, body, updates = api.writes[0]
        self.assertEqual((body["attendees"], updates),
                         ([{"email": "alex@gmail.com"}, {"email": "thomas@gmail.com"}], "all"))
        self.assertIn("Invitation envoyée à 2 personnes", result.message)

    def test_dictated_address(self):
        agenda, api = make(heard="invite alex arobase gmail point com")
        question, _ = ask_then_run(agenda, "add", title="Réunion", when="vendredi 15 h", attendees="alex@gmail.com")
        self.assertIn("Invités : alex@gmail.com", question)

    def test_address_never_said_is_refused(self):
        agenda, api = make(heard="ajoute une réunion vendredi à 15 h avec alex@gmail.com")
        _, result = ask_then_run(agenda, "add", title="Réunion", when="vendredi 15 h",
                                 attendees="alex@gmail.com, pirate@evil.com")
        self.assertFalse(result.ok)
        self.assertIn("pirate@evil.com", result.message)
        self.assertEqual(api.writes, [])

    def test_invalid_address(self):
        agenda, _ = make(heard="avec alex@gmail")
        _, result = ask_then_run(agenda, "add", title="Réunion", when="vendredi 15 h", attendees="alex@gmail")
        self.assertIn("Adresse e-mail invalide", result.message)

    def test_name_without_address_creates_without_invitation(self):
        agenda, api = make(heard="ajoute une réunion vendredi à 15 h avec Alex")
        question, result = ask_then_run(agenda, "add", title="Réunion avec Alex", when="vendredi 15 h",
                                        attendees="Alex")
        self.assertEqual(question, "")
        self.assertIn("aucune invitation envoyée", result.message)
        self.assertNotIn("attendees", api.writes[0][2])

    def test_invite_to_an_existing_event(self):
        agenda, api = make({MAIN: [event("r", "Réunion projet", 6, (15, 0), (16, 0),
                                         attendees=[{"email": "alex@gmail.com", "responseStatus": "accepted"}])]},
                           heard="invite thomas@gmail.com à ma réunion de vendredi")
        question, result = ask_then_run(agenda, "invite", event="ma réunion", when="vendredi",
                                        attendees="thomas@gmail.com")
        self.assertTrue(question.startswith("J'invite thomas arobase gmail point com à « Réunion projet » "
                                            "(vendredi 6 novembre de 15 h à 16 h) ?"), question)
        _, cid, event_id, body, etag, updates = api.writes[0]
        self.assertEqual(body["attendees"], [{"email": "alex@gmail.com", "responseStatus": "accepted"},
                                             {"email": "thomas@gmail.com"}])
        self.assertEqual((etag, updates), ('"r"', "all"))

    def test_emails_are_not_logged(self):
        self.assertEqual(_private("invite alex@gmail.com et thomas arobase gmail point com"),
                         "invite <e-mail> et <e-mail>")


class ChangeTest(unittest.TestCase):
    EVENTS = {MAIN: [event("dent", "Dentiste", 5, (16, 0), (17, 0)),
                     event("r1", "Réunion stage", 9, (10, 0), (11, 0)),
                     event("r2", "Réunion projet", 9, (14, 0), (15, 0),
                           attendees=[{"email": "alex@gmail.com"}, {"email": "dan@gmail.com", "self": True}]),
                     event("yoga-1", "Yoga", 10, (19, 0), (20, 0), recurringEventId="yoga")],
              STUDENT: [event("maths", "Cours de maths", 6, (15, 0), (17, 0))],
              CLUB: [event("match", "Match", 7, (10, 0), (12, 0))]}

    def test_delete_always_asks_and_names_the_event(self):
        agenda, api = make(self.EVENTS)
        question, result = ask_then_run(agenda, "delete", event="la réunion stage", when="lundi")
        self.assertEqual(question, "Je supprime « Réunion stage », lundi 9 novembre de 10 h à 11 h, dans ton agenda "
                                   "principal ?")
        self.assertEqual(api.writes, [("delete", MAIN, "r1", '"r1"', "none")])
        self.assertEqual(result.message, "J'ai supprimé « Réunion stage », lundi 9 novembre de 10 h à 11 h.")

    def test_delete_with_participants_warns_them(self):
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "delete", event="réunion projet", when="lundi")
        self.assertIn("Un participant prévenu.", question)
        self.assertEqual(api.writes[0][-1], "all")

    def test_ambiguous_event_is_not_touched(self):
        agenda, api = make(self.EVENTS)
        _, result = ask_then_run(agenda, "delete", event="ma réunion", when="lundi")
        self.assertEqual(result.message, "Plusieurs événements correspondent : « Réunion stage » lundi 9 novembre à "
                                         "10 h et « Réunion projet » lundi 9 novembre à 14 h. Lequel ?")
        _, result = ask_then_run(agenda, "delete", event="ma réunion", when="lundi 14 h")  # l'heure départage
        self.assertIn("« Réunion projet »", result.message)

    def test_unknown_or_similar_name_is_not_taken(self):
        agenda, api = make({TEST: [event("c", "Test Jarvis C", 8, (9, 0), (10, 0))]})
        _, result = ask_then_run(agenda, "delete", event="test jarvis a", when="dimanche")
        self.assertTrue(result.message.startswith("Je ne trouve pas « test jarvis a » le dimanche 8 novembre."))
        self.assertEqual(api.writes, [])

    def test_generic_name_with_a_single_event(self):  # « mon rendez-vous de jeudi » : le seul ce jour-là
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "delete", event="mon rendez-vous", when="jeudi")
        self.assertIn("« Dentiste »", question)

    def test_read_only_event(self):
        agenda, api = make(self.EVENTS)
        _, result = ask_then_run(agenda, "delete", event="match", when="samedi")
        self.assertIn("lecture seule", result.message)

    def test_move_keeps_the_duration_and_asks(self):
        agenda, api = make(self.EVENTS)
        question, result = ask_then_run(agenda, "move", event="mon rendez-vous", when="jeudi", to="vendredi à 15 h")
        self.assertTrue(question.startswith("Attention, tu as déjà « Cours de maths » de 15 h à 17 h (Étudiant) à ce "
                                            "moment. Je déplace « Dentiste » (jeudi 5 novembre de 16 h à 17 h) : "
                                            "nouveau créneau vendredi 6 novembre de 15 h à 16 h ?"), question)
        _, cid, event_id, body, etag, updates = api.writes[0]
        self.assertEqual((cid, event_id, etag, updates), (MAIN, "dent", '"dent"', "none"))
        self.assertEqual(body["start"]["dateTime"], "2026-11-06T15:00:00+01:00")
        self.assertEqual(body["end"]["dateTime"], "2026-11-06T16:00:00+01:00")
        self.assertEqual(result.message, "J'ai déplacé « Dentiste » : vendredi 6 novembre de 15 h à 16 h.")

    def test_move_hour_only_or_day_only(self):
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "move", event="dentiste", when="jeudi", to="18 h")
        self.assertIn("nouveau créneau jeudi 5 novembre de 18 h à 19 h", question)
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "move", event="réunion stage", when="lundi", to="mardi")
        self.assertIn("nouveau créneau mardi 10 novembre de 10 h à 11 h", question)

    def test_move_to_another_calendar(self):
        agenda, api = make(self.EVENTS)
        question, result = ask_then_run(agenda, "move", event="dentiste", when="jeudi", calendar="étudiant")
        self.assertEqual(question, "Je déplace « Dentiste » (jeudi 5 novembre de 16 h à 17 h) vers le calendrier "
                                   "Étudiant ?")
        self.assertEqual(api.writes, [("move", MAIN, "dent", STUDENT, "none")])
        self.assertEqual(result.message, "J'ai déplacé « Dentiste » dans le calendrier Étudiant.")

    def test_recurring_event_changes_only_this_occurrence(self):
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "delete", event="yoga", when="mardi")
        self.assertIn("Seulement cette occurrence, pas toute la série.", question)
        self.assertEqual(api.writes[0][2], "yoga-1")
        agenda, api = make(self.EVENTS)
        _, result = ask_then_run(agenda, "move", event="yoga", when="mardi", calendar="étudiant")
        self.assertIn("une seule occurrence", result.message)

    def test_simple_rename_is_direct_shared_change_asks(self):
        agenda, api = make(self.EVENTS)
        question, result = ask_then_run(agenda, "update", event="dentiste", when="jeudi", title="Orthodontiste")
        self.assertEqual(question, "")
        self.assertEqual(api.writes[0][3], {"summary": "Orthodontiste"})
        question, _ = ask_then_run(agenda, "update", event="réunion projet", when="lundi", location="Salle B")
        self.assertTrue(question.startswith("Je modifie « Réunion projet »"), question)
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "update", event="dentiste", when="jeudi", duration="2 h")  # horaire
        self.assertIn("de 16 h à 18 h", question)

    def test_date_left_in_the_name(self):  # le modèle écrit event="réunion stage de lundi", sans when
        agenda, _ = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "delete", event="réunion stage de lundi")
        self.assertIn("« Réunion stage », lundi 9 novembre", question)

    def test_new_time_in_update_is_a_move(self):  # « décale le dentiste à 18 h » pris pour update
        agenda, api = make(self.EVENTS)
        question, _ = ask_then_run(agenda, "update", event="dentiste", when="jeudi", to="18 h")
        self.assertTrue(question.startswith("Je déplace « Dentiste »"), question)
        self.assertEqual(api.writes[0][0], "patch")

    def test_same_event_elsewhere_is_moved_not_copied(self):  # « mets-le plutôt dans mon calendrier perso »
        agenda, api = make({STUDENT: [event("piano", "Cours de piano", 11, (17, 0), (18, 0))]})
        question, result = ask_then_run(agenda, "add", title="Cours de piano", when="mercredi 17 h", calendar="perso")
        self.assertEqual(question, "« Cours de piano » existe déjà, mercredi 11 novembre de 17 h à 18 h, dans le "
                                   "calendrier Étudiant. Je le déplace dans ton agenda principal ?")
        self.assertEqual(api.writes, [("move", STUDENT, "piano", MAIN, "none")])
        _, result = ask_then_run(agenda, "add", title="Cours de piano", when="mercredi 17 h", calendar="perso")
        self.assertIn("existe déjà", result.message)  # pas de doublon dans le même calendrier
        self.assertEqual(len(api.writes), 1)

    def test_changed_meanwhile(self):
        agenda, api = make(self.EVENTS)
        api.delete = mock.Mock(side_effect=ApiError(412, "conditionNotMet", "L'événement a changé entre-temps."))
        _, result = ask_then_run(agenda, "delete", event="dentiste", when="jeudi")
        self.assertEqual((result.ok, result.message), (False, "L'événement a changé entre-temps."))

    def test_writes_go_through_prepare(self):
        agenda, _ = make()
        for action in ("add", "update", "move", "delete", "invite"):
            check = agenda.guard(action)
            self.assertEqual((check.verdict.value, check.reason), ("confirm", ""))


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None):
        reply = self.replies.pop(0)
        return {"content": reply, "tool_calls": []} if isinstance(reply, str) else {"content": "", "tool_calls": reply}


def call(**args):
    return [{"function": {"name": "agenda", "arguments": args}}]


class AgentFlowTest(unittest.TestCase):
    """L'agenda dans le vrai agent : création directe, question en cas de conflit ou d'invitation."""

    def setUp(self):
        patcher = mock.patch.object(win, "current_window", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent(self, events, *replies):
        box = Toolbox()
        agent = Agent(FakeLLM(*replies), box)
        agenda, api = make(events)
        agenda._heard = agent.heard
        agenda_module.register(box, agenda)
        return agent, api

    def test_simple_add_is_done_without_question(self):
        agent, api = self.agent({}, call(action="add", title="Sport", when="vendredi 18 h"), "OK")
        reply = agent.handle("ajoute une séance de sport vendredi à 18 h")
        self.assertFalse(reply.question)
        self.assertEqual(reply.text, "J'ai ajouté « Sport » vendredi 6 novembre de 18 h à 19 h dans ton agenda "
                                     "principal.")
        self.assertEqual(len(api.writes), 1)

    def test_conflict_question_then_yes(self):
        busy = {MAIN: [event("m", "Cours", 6, (18, 0), (19, 0))]}
        agent, api = self.agent(busy, call(action="add", title="Sport", when="vendredi 18 h"), "OK")
        reply = agent.handle("ajoute une séance de sport vendredi à 18 h")
        self.assertTrue(reply.question)
        self.assertIn("Je le mets plutôt de 19 h à 20 h ?", reply.text)
        self.assertEqual(api.writes, [])
        self.assertIn("de 19 h à 20 h", agent.handle("oui").text)
        self.assertEqual(len(api.writes), 1)

    def test_conflict_question_then_no(self):
        busy = {MAIN: [event("m", "Cours", 6, (18, 0), (19, 0))]}
        agent, api = self.agent(busy, call(action="add", title="Sport", when="vendredi 18 h"))
        agent.handle("ajoute une séance de sport vendredi à 18 h")
        self.assertEqual(agent.handle("non").text, "D'accord, j'annule.")
        self.assertEqual(api.writes, [])

    def test_invitation_uses_what_the_user_said(self):
        agent, api = self.agent({}, call(action="add", title="Réunion", when="vendredi 15 h",
                                         attendees="alex@gmail.com"), "OK")
        reply = agent.handle("ajoute une réunion vendredi à 15 h avec alex@gmail.com")
        self.assertTrue(reply.question)
        agent.handle("oui")
        self.assertEqual(api.writes[0][3], "all")

    def test_invented_address_is_refused(self):
        agent, api = self.agent({}, call(action="add", title="Réunion", when="vendredi 15 h",
                                         attendees="alex@gmail.com"), "Je n'ai pas son adresse.")
        reply = agent.handle("ajoute une réunion vendredi à 15 h avec Alex")
        self.assertFalse(reply.question)
        self.assertEqual(api.writes, [])

    def test_delete_asks_first(self):
        agent, api = self.agent({MAIN: [event("r", "Réunion", 9, (10, 0), (11, 0))]},
                                call(action="delete", event="ma réunion", when="lundi"), "OK")
        reply = agent.handle("supprime ma réunion de lundi")
        self.assertEqual(reply.text, "Je supprime « Réunion », lundi 9 novembre de 10 h à 11 h, dans ton agenda "
                                     "principal ?")
        self.assertEqual(api.writes, [])
        agent.handle("oui")
        self.assertEqual(api.writes[0][0], "delete")


if __name__ == "__main__":
    unittest.main()
