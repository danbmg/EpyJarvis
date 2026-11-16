"""Google Agenda par son API officielle (Calendar v3) : planning, disponibilités, création, invitations,
modifications, sur tous les calendriers du compte.

Connexion une seule fois (le navigateur s'ouvre) :
    python -m jarvis.tools.agenda login [--client fichier.json] | status | logout

Le code décide des confirmations, pas le modèle :
- lire (planning, créneaux libres, calendriers) : directement ;
- créer un événement simple : directement ; la réponse dit la date complète, l'heure et le calendrier ;
- créer sur un créneau déjà pris : une question signale le conflit et propose le premier créneau libre
  (« oui » : créé à ce créneau ; « ajoute-le quand même » : le modèle relance avec overlap) ;
- inviter, déplacer, supprimer, modifier un événement partagé, récurrent ou sa durée : une question qui
  nomme l'événement retrouvé.
Le garde-fou envoie toutes les écritures à `prepare`, qui calcule tout (dates, conflits, événement visé) et
renvoie la question, ou une question vide quand il n'y a rien à confirmer : l'agent agit alors directement.
L'action préparée est gardée et exécutée telle quelle après le « oui ».

Les adresses e-mail des invités doivent avoir été dites ou écrites par l'utilisateur (jamais inventées par
le modèle, ni lues dans une invitation) ; elles ne sont ni retenues ni écrites dans le journal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import logging
import re
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jarvis import secret_store
from jarvis.tools import ALLOW, Check, Param, Result, Tool, Toolbox, confirm, norm
from jarvis.tools import dates
from jarvis.tools.dates import TZ, DateError, at, parse_duration
from jarvis.tools.google_auth import AuthError, GoogleAuth, NotConnected
from jarvis.tools.google_calendar import TIMEZONE, CalendarApi, CalendarError

log = logging.getLogger("jarvis.tools.agenda")

SCOPES = ("https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.calendarlist.readonly")
READS = ("list", "free", "calendars")
WRITES = ("add", "update", "move", "delete", "invite")
_CALENDARS_TTL = 300   # la liste des calendriers est gardée 5 minutes
_PLAN_TTL = 600        # une action préparée attend la réponse de l'utilisateur au plus 10 minutes
_SEARCH_DAYS = 30      # sans date, l'événement visé est cherché dans les 30 prochains jours
_MAX_LISTED = 25
_PAST_MARGIN = dt.timedelta(minutes=10)
_NOT_CONNECTED = "Google Agenda n'est pas connecté. Lance une fois : python -m jarvis.tools.agenda login"
_AUTO_IDS = ("#holiday@", "#contacts@", "#weeknum@")  # jours fériés, anniversaires, numéros de semaine
# Mots ignorés pour retrouver un événement, comparés AVANT de retirer les accents : « à » est ignoré, « A » non.
_FILLERS_SAID = {"mon", "ma", "mes", "le", "la", "les", "l", "un", "une", "de", "du", "des", "d", "avec", "à", "au",
                 "aux", "pour", "en", "ce", "cet", "cette", "son", "sa", "ton", "ta", "et", "sur", "prévu", "prévue",
                 "prevu", "prevue"}
# Mots de date laissés dans le nom de l'événement par le modèle (« réunion de lundi ») : pas dans le titre.
_DATE_WORDS = set(dates.DAYS) | {norm(m) for m in dates.MONTH_NAMES} | {
    "demain", "aujourd", "hui", "apres-demain", "hier", "prochain", "prochaine", "dernier", "derniere", "matin",
    "soir", "midi", "apres-midi", "semaine", "week-end", "weekend", "h", "heure", "heures", "minuit", "1er"}
_GENERIC = {"rendez-vous", "rendez", "vous", "rdv", "reunion", "evenement", "event", "truc", "chose", "creneau",
            "activite", "seance", "cours", "rencontre", "rendezvous"}
_CALENDAR_WORDS = {"calendrier", "agenda", "planning", "mon", "ma", "le", "la", "du", "de", "dans", "l", "d"}
_PRIMARY_WORDS = {"principal", "perso", "personnel", "par defaut", "", "google", "moi", "mien"}
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
_SPOKEN_EMAIL = re.compile(r"([a-z0-9][a-z0-9._-]*(?: (?:point|tiret bas|tiret|underscore) [a-z0-9]+)*) "
                           r"(?:arobase|arrobase|at) ([a-z0-9-]+(?: point [a-z0-9-]+)+)")


@dataclass
class Settings:
    account: str
    default_calendar: str = ""          # vide : le calendrier principal
    aliases: dict = field(default_factory=dict)   # nom dit -> nom ou identifiant de calendrier
    default_duration: int = 60          # minutes
    day_start: dt.time = dt.time(8)     # plage des créneaux libres et des propositions
    day_end: dt.time = dt.time(20)
    min_free: int = 30                  # minutes : un créneau plus court n'est pas proposé
    ignore: list = field(default_factory=list)    # calendriers qui ne comptent pas dans les conflits


@dataclass
class Calendar:
    id: str
    name: str
    access: str          # owner, writer, reader, freeBusyReader
    primary: bool = False

    @property
    def writable(self) -> bool:
        return self.access in ("owner", "writer")

    @property
    def label(self) -> str:
        return "ton agenda principal" if self.primary else "le calendrier {}".format(self.name)

    @property
    def short(self) -> str:
        return "principal" if self.primary else self.name


@dataclass
class Event:
    calendar: Calendar
    id: str
    title: str
    start: dt.datetime
    end: dt.datetime
    all_day: bool
    etag: str = ""
    guests: list = field(default_factory=list)   # e-mails des autres participants
    organizer: str = ""      # vide : c'est l'utilisateur qui l'organise
    recurring: bool = False  # occurrence d'une série
    transparent: bool = False  # marqué « Disponible »
    declined: bool = False     # invitation refusée
    kind: str = "default"
    uid: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        """Compte dans les conflits et les disponibilités."""
        return not (self.all_day or self.transparent or self.declined
                    or self.kind in ("workingLocation", "birthday"))

    def overlaps(self, start: dt.datetime, end: dt.datetime) -> bool:
        return self.start < end and self.end > start


@dataclass
class _Plan:
    question: str                    # vide : rien à confirmer, l'action est faite directement
    execute: Callable[[], Result]


def _event(calendar: Calendar, raw: dict) -> Event | None:
    if raw.get("status") == "cancelled" or not raw.get("id"):
        return None
    start, end = raw.get("start") or {}, raw.get("end") or {}
    try:
        if start.get("date"):
            first = dt.date.fromisoformat(start["date"])
            last = dt.date.fromisoformat(end.get("date") or start["date"])
            begin, finish, all_day = at(first, dt.time(0)), at(max(last, first + dt.timedelta(days=1)), dt.time(0)), True
        elif start.get("dateTime"):
            begin = dt.datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
            finish = dt.datetime.fromisoformat(end.get("dateTime") or start["dateTime"]).astimezone(TZ)
            all_day = False
        else:
            return None
    except ValueError:
        return None
    attendees = raw.get("attendees") or []
    me = next((a for a in attendees if a.get("self")), None)
    organizer = raw.get("organizer") or {}
    return Event(calendar, raw["id"], (raw.get("summary") or "").strip() or "Sans titre", begin, finish, all_day,
                 raw.get("etag", ""),
                 [a["email"] for a in attendees if a.get("email") and not a.get("self") and not a.get("resource")],
                 "" if organizer.get("self", True) else (organizer.get("displayName") or organizer.get("email", "")),
                 bool(raw.get("recurringEventId")), raw.get("transparency") == "transparent",
                 bool(me and me.get("responseStatus") == "declined"), raw.get("eventType", "default"),
                 raw.get("iCalUID") or raw["id"], raw)


class Agenda:
    def __init__(self, api: CalendarApi, settings: Settings, heard: Callable[[], str] = lambda: "",
                 clock: Callable[[], dt.datetime] = dates.now) -> None:
        self.api = api
        self.settings = settings
        self._heard = heard     # ce que l'utilisateur a dit récemment : les adresses e-mail doivent en venir
        self._now = clock
        self._calendars: tuple[float, list[Calendar]] = (0.0, [])
        self._plans: dict[str, tuple[float, _Plan]] = {}
        self._unread: list[Calendar] = []  # calendriers illisibles à la dernière lecture

    # ---- Entrées de l'outil ----

    def guard(self, action: str, **args) -> Check:
        """Lectures : directes. Écritures : toujours préparées (prepare décide s'il faut une question)."""
        return ALLOW if action in READS else confirm("")

    def prepare(self, action: str, **args) -> Result:
        try:
            plan = self._plan(action, args)
        except (AuthError, CalendarError, DateError, secret_store.SecretError) as exc:
            return _failure(exc)
        self._plans[_key(action, args)] = (time.monotonic(), plan)
        return Result(True, plan.question)

    def run(self, action: str, **args) -> Result:
        try:
            if action == "list":
                return self.list_events(**args)
            if action == "free":
                return self.free(**args)
            if action == "calendars":
                return self.list_calendars()
            entry = self._plans.pop(_key(action, args), None)
            fresh = entry and time.monotonic() - entry[0] < _PLAN_TTL
            return (entry[1] if fresh else self._plan(action, args)).execute()
        except (AuthError, CalendarError, DateError, secret_store.SecretError) as exc:
            return _failure(exc)

    def _plan(self, action: str, args: dict) -> _Plan:
        planner = {"add": self._plan_add, "update": self._plan_update, "move": self._plan_move,
                   "delete": self._plan_delete, "invite": self._plan_invite}.get(action)
        if planner is None:
            raise CalendarError("Action d'agenda inconnue : {}.".format(action))
        return planner(**args)

    # ---- Calendriers ----

    def calendars(self, fresh: bool = False) -> list[Calendar]:
        loaded, found = self._calendars
        if found and not fresh and time.monotonic() - loaded < _CALENDARS_TTL:
            return found
        found = [Calendar(i["id"], (i.get("summaryOverride") or i.get("summary") or i["id"]).strip(),
                          i.get("accessRole", "reader"), bool(i.get("primary")))
                 for i in self.api.calendars() if i.get("id") and not i.get("deleted")]
        found.sort(key=lambda c: (not c.primary, not c.writable, norm(c.name)))
        self._calendars = (time.monotonic(), found)
        return found

    def _named_in_request(self) -> str:
        """Calendrier nommé dans la dernière phrase de l'utilisateur (« dans mon calendrier étudiant »), quand le
        modèle l'a oublié : l'événement ne part pas en silence dans le calendrier par défaut."""
        said = (self._heard() or "").strip().splitlines()
        for line in reversed(said[-3:]):  # la correction (« ajoute-le quand même »), puis la demande d'origine
            text = norm(line).replace("'", " ")
            for m in re.finditer(r"\b(?:calendrier|agenda)\s+((?:[\w-]+\s*){1,3})", text):
                words = [w for w in m.group(1).split() if w not in _CALENDAR_WORDS]
                for count in range(len(words), 0, -1):
                    try:
                        found = self._match_calendar(" ".join(words[:count]), self.calendars())
                    except CalendarError:
                        found = None
                    if found:
                        return found.id
        return ""

    def primary(self) -> Calendar | None:
        return next((c for c in self.calendars(fresh=True) if c.primary), None)

    def _readable(self) -> list[Calendar]:
        return [c for c in self.calendars() if c.access != "freeBusyReader"]

    def _counted(self) -> list[Calendar]:
        """Calendriers qui comptent dans les conflits et les disponibilités : tous, sauf jours fériés,
        anniversaires et ceux exclus dans la config."""
        ignored = {norm(str(i)) for i in self.settings.ignore}
        return [c for c in self._readable() if not any(tag in c.id for tag in _AUTO_IDS)
                and norm(c.name) not in ignored and norm(c.id) not in ignored]

    def _calendar(self, name: str = "", writable: bool = False) -> Calendar:
        """Le calendrier nommé (alias de la config, nom approchant), sinon celui par défaut."""
        found_all = self.calendars()
        if not found_all:
            raise CalendarError("Aucun calendrier sur ce compte Google.")
        wanted = (name or "").strip() or str(self.settings.default_calendar or "")
        found = self._match_calendar(wanted, found_all) if wanted else next((c for c in found_all if c.primary), None)
        if found is None:
            raise CalendarError("Je ne trouve pas le calendrier « {} ». Tes calendriers : {}.".format(
                wanted, _join([c.short for c in found_all])))
        if writable and not found.writable:
            raise CalendarError("Le calendrier {} est en lecture seule : je ne peux pas y écrire. Calendriers "
                                "modifiables : {}.".format(found.name, _join([c.short for c in found_all if c.writable])))
        return found

    def _match_calendar(self, wanted: str, found_all: list[Calendar]) -> Calendar | None:
        aliases = {norm(k): str(v) for k, v in (self.settings.aliases or {}).items()}
        key = _strip(wanted, _CALENDAR_WORDS)
        if key in aliases:
            wanted, key = aliases[key], _strip(aliases[key], _CALENDAR_WORDS)
        exact = next((c for c in found_all if c.id == wanted or norm(c.id) == norm(wanted)), None)
        if exact:
            return exact
        scored = []
        for c in found_all:
            name = _strip(c.name, _CALENDAR_WORDS)
            if not key or not name:
                continue
            score = (3 if name == key else 2.5 if re.search(r"\b{}\b".format(re.escape(key)), name)
                     else 2 if re.search(r"\b{}\b".format(re.escape(name)), key)
                     else 1.5 if difflib.SequenceMatcher(None, name, key).ratio() >= 0.75 else 0)
            if score:
                scored.append((score, c))
        if scored:
            best = max(s for s, _ in scored)
            matches = [c for s, c in scored if s == best]
            if len(matches) > 1:
                raise CalendarError("Plusieurs calendriers correspondent : {}. Lequel ?".format(
                    _join([c.name for c in matches])))
            return matches[0]
        if key in _PRIMARY_WORDS or wanted.strip().lower() in ("primary", self.settings.account.lower()):
            return next((c for c in found_all if c.primary), None)
        return None

    # ---- Lecture ----

    def _read(self, start: dt.datetime, end: dt.datetime, calendars: list[Calendar]) -> list[Event]:
        results = self.api.events_many([c.id for c in calendars], start, end)
        events, seen, failed = [], set(), []
        self._unread = []  # calendriers illisibles à cette lecture : la réponse le dit, au lieu de « rien de prévu »
        for calendar in calendars:  # principal d'abord : une invitation vue dans deux calendriers y reste
            got = results.get(calendar.id, [])
            if isinstance(got, Exception):
                failed.append(got)
                self._unread.append(calendar)
                continue
            for raw in got:
                event = _event(calendar, raw)
                if event and (event.uid, event.start) not in seen:
                    seen.add((event.uid, event.start))
                    events.append(event)
        if failed and len(failed) == len(calendars):
            raise failed[0]
        if failed:
            log.warning("%d calendrier(s) illisible(s) : %s", len(failed), failed[0])
        events.sort(key=lambda e: (e.start, not e.all_day, norm(e.title)))
        return events

    def list_events(self, when: str = "", calendar: str = "", **_) -> Result:
        now = self._now()
        w = dates.parse(when or "aujourd'hui", now)
        start, end = w.bounds()
        if w.start and not w.period and not w.part:  # « demain à partir de 14 h »
            start = at(w.first, w.start)
        chosen = self._calendar(calendar) if _named(calendar) else None
        events = [e for e in self._read(start, end, [chosen] if chosen else self._readable())
                  if not e.declined and e.kind != "workingLocation"]
        return Result(True, self._agenda_text(w, events, start, chosen, now.date()) + self._unread_note())

    def _unread_note(self, verb: str = "lire") -> str:
        if not self._unread:
            return ""
        names = _join([c.short if not c.primary else "ton agenda principal" for c in self._unread])
        return "\nAttention : je n'ai pas pu {} {}, Google ne répond pas.".format(verb, names)

    def _agenda_text(self, w: dates.When, events: list[Event], start: dt.datetime, chosen: Calendar | None,
                     today: dt.date) -> str:
        where = " dans {}".format(chosen.label) if chosen else ""
        label = _period(w, today)
        shown, rest = events[:_MAX_LISTED], len(events) - _MAX_LISTED
        if not w.period:
            if not events:
                return "{}, tu n'as rien de prévu{}.".format(_cap(label), where)
            return "{}, tu as {} événement{}{} : {}.".format(
                _cap(label), len(events), "s" if len(events) > 1 else "", where,
                _join([self._short(e, w.first, chosen is None) for e in shown] + _more(rest)))
        if not events:
            return "{} : rien de prévu{}.".format(_cap(label), where)
        lines = ["{} : {} événement{}{}.".format(_cap(label), len(events), "s" if len(events) > 1 else "", where)]
        by_day: dict[dt.date, list[Event]] = {}
        for e in shown:
            by_day.setdefault(max(e.start, start).date(), []).append(e)
        for day, items in by_day.items():
            lines.append("{} {} : {}.".format(_cap(dates.DAYS[day.weekday()]), "1er" if day.day == 1 else day.day,
                                              _join([self._short(e, day, chosen is None) for e in items])))
        if rest > 0:
            lines.append("Et {} autre{}.".format(rest, "s" if rest > 1 else ""))
        return "\n".join(lines)

    def _short(self, e: Event, day: dt.date, show_calendar: bool) -> str:
        if e.all_day:
            text = "{} (toute la journée)".format(e.title)
        elif e.start.date() < day:
            text = "{} jusqu'à {}".format(e.title, dates.time_name(e.end.time()))
        elif e.end.date() > e.start.date() and e.end.time() != dt.time(0):
            text = "{} à partir de {}".format(e.title, dates.time_name(e.start.time()))
        else:
            text = "{} de {} à {}".format(e.title, dates.time_name(e.start.time()), dates.time_name(e.end.time()))
        return text + (" ({})".format(e.calendar.name) if show_calendar and not e.calendar.primary else "")

    def free(self, when: str = "", calendar: str = "", **_) -> Result:
        text = self._free_text(when, calendar)
        return Result(True, text + self._unread_note())

    def _free_text(self, when: str, calendar: str) -> str:
        now = self._now()
        today = now.date()
        w = dates.parse(when or "aujourd'hui", now)
        low, high = w.part if w.part and not w.start else (self.settings.day_start, self.settings.day_end)
        days = [w.first + dt.timedelta(days=i) for i in range(min((w.last - w.first).days + 1, 31))]
        chosen = self._calendar(calendar) if _named(calendar) else None
        busy = [e for e in self._read(at(days[0], dt.time(0)), at(days[-1] + dt.timedelta(days=1), dt.time(0)),
                                      [chosen] if chosen else self._counted()) if e.busy]
        rows = []
        for day in days:
            begin, finish = at(day, low), at(day, high)
            if day == today:
                begin = max(begin, _ceil_quarter(now))
            if begin >= finish:
                continue
            gaps = [(a, b) for a, b in _gaps(begin, finish, [(e.start, e.end) for e in busy if e.overlaps(begin, finish)])
                    if b - a >= dt.timedelta(minutes=self.settings.min_free)]
            rows.append((day, gaps, (begin, finish)))
        window = "entre {} et {}".format(dates.time_name(low), dates.time_name(high))
        if not rows:
            return "Cette période est déjà passée."
        if len(rows) == 1:
            day, gaps, (begin, finish) = rows[0]
            head = _cap(dates.day_name(day, today))
            if not gaps:
                return "{}, tu n'as aucun créneau libre {}.".format(head, window)
            if gaps == [(at(day, low), finish)]:  # plage entière (pas déjà entamée aujourd'hui)
                return "{}, tu es libre tout le temps {}.".format(head, window)
            return "{}, tu es libre {}.".format(head, _gap_text(gaps))
        lines = ["Tes créneaux libres {} :".format(window)]
        for day, gaps, (begin, finish) in rows:
            head = "{} {}".format(_cap(dates.DAYS[day.weekday()]), "1er" if day.day == 1 else day.day)
            text = ("rien de libre" if not gaps else "libre tout le temps" if gaps == [(at(day, low), finish)]
                    else _gap_text(gaps))
            lines.append("{} : {}.".format(head, text))
        return "\n".join(lines)

    def list_calendars(self, **_) -> Result:
        found = self.calendars(fresh=True)
        writable = [c.short if not c.primary else "ton agenda principal" for c in found if c.writable]
        others = [c.name for c in found if not c.writable]
        text = "Tu as {} calendrier{}. Je peux écrire dans : {}.".format(len(found), "s" if len(found) > 1 else "",
                                                                       _join(writable) or "aucun")
        if others:
            text += " En lecture seule : {}.".format(_join(others))
        return Result(True, text)

    # ---- Conflits et propositions ----

    def _busy(self, start: dt.datetime, end: dt.datetime, exclude: Event | None = None) -> list[Event]:
        return [e for e in self._read(start, end, self._counted()) if e.busy and e.overlaps(start, end)
                and not (exclude and (e.id == exclude.id or (e.uid == exclude.uid and e.start == exclude.start)))]

    def _propose(self, start: dt.datetime, length: dt.timedelta, exclude: Event | None = None) -> dt.datetime | None:
        """Premier créneau libre de même durée : le même jour après l'heure demandée (juste après les
        événements en conflit, sinon par quarts d'heure), puis les jours suivants à la même heure ou dans la
        plage de la journée, jusqu'à 7 jours."""
        now = self._now()
        day = start.date()
        busy = [(e.start, e.end) for e in self._busy(at(day, dt.time(0)), at(day + dt.timedelta(days=8), dt.time(0)),
                                                       exclude)]

        def free(moment: dt.datetime) -> bool:
            finish = moment + length
            return moment >= now and not any(b0 < finish and b1 > moment for b0, b1 in busy)

        limit = max(at(day, self.settings.day_end), start + length) - length
        same_day = sorted({b1 for _, b1 in busy if start < b1 <= limit} | set(_quarters(start, limit)))
        for moment in same_day:
            if moment > start and free(moment):
                return moment
        for offset in range(1, 8):
            other = day + dt.timedelta(days=offset)
            for moment in [at(other, start.time())] + _quarters(at(other, self.settings.day_start),
                                                                  at(other, self.settings.day_end) - length):
                if free(moment):
                    return moment
        return None

    def _clash_text(self, clash: list[Event], today: dt.date) -> str:
        items = ["« {} » de {} à {}{}".format(e.title, dates.time_name(e.start.time()), dates.time_name(e.end.time()),
                                              "" if e.calendar.primary else " ({})".format(e.calendar.name))
                 for e in clash[:2]]
        return _join(items + _more(len(clash) - 2))

    # ---- Création ----

    def _slot(self, w: dates.When, duration: str) -> tuple[dt.datetime, dt.datetime, bool]:
        if w.all_day or "journee" in norm(duration or ""):
            return at(w.first, dt.time(0)), at(w.last + dt.timedelta(days=1), dt.time(0)), True
        if w.start is None:
            raise CalendarError("À quelle heure ça commence ? Dis aussi « toute la journée » si c'est sur la journée.")
        start = at(w.first, w.start)
        if w.end:
            end = at(w.first, w.end)
        elif duration and norm(duration).startswith("jusqu"):
            until = dates.parse(duration, start)
            end = at(w.first, until.end or until.start or w.start)
        else:
            end = start + (self._duration(duration) or w.duration
                           or dt.timedelta(minutes=self.settings.default_duration))
        if end <= start:
            end += dt.timedelta(days=1)
        return start, end, False

    def _duration(self, text: str) -> dt.timedelta | None:
        """Durée donnée par le modèle, gardée seulement si l'utilisateur l'a dite : sinon la durée par défaut. Le
        modèle en invente (« 1 minute »), ou y glisse autre chose (le nom du calendrier)."""
        if not text:
            return None
        try:
            length = parse_duration(text)
        except DateError:
            return None
        heard = (self._heard() or "").strip()
        if not heard:  # rien d'entendu (benchmark, essais) : on fait confiance
            return length
        recent = " ".join(heard.splitlines()[-3:])  # la demande, et une éventuelle correction
        return length if length in dates.durations_in(recent) else None

    def _check_future(self, start: dt.datetime, all_day: bool) -> None:
        now = self._now()
        if (all_day and start.date() < now.date()) or (not all_day and start < now - _PAST_MARGIN):
            raise CalendarError("{} est déjà passé : redis la date.".format(_cap(dates.day_name(start.date(), now.date())
                                + ("" if all_day else " à " + dates.time_name(start.time())))))

    def _plan_add(self, title: str = "", when: str = "", duration: str = "", calendar: str = "",
                  attendees: str = "", location: str = "", overlap: bool = False, **_) -> _Plan:
        title = (title or "").strip(" .«»\"")
        if not title:
            raise CalendarError("Quel est le nom de l'événement ?")
        if not when:
            raise CalendarError("Pour quand ? Donne le jour et l'heure de « {} ».".format(title))
        now = self._now()
        today = now.date()
        target = self._calendar(calendar if _named(calendar) else self._named_in_request(), writable=True)
        start, end, all_day = self._slot(dates.parse(when, now), duration)
        self._check_future(start, all_day)
        emails = self._emails(attendees) if _has_email(attendees) else []
        without_address = bool((attendees or "").strip()) and not emails
        clash = [] if all_day else self._busy(start, end)
        unchecked = "" if all_day else self._unread_note("vérifier les conflits dans").strip()
        overlap = overlap or _insists(self._heard())  # « ajoute-le quand même » : sans compter sur le modèle
        event_id = secrets.token_hex(16)  # choisi ici : une création refaite ne fait pas de doublon

        def create(begin: dt.datetime, finish: dt.datetime) -> Result:
            body = {"id": event_id, "summary": title, **_times(begin, finish, all_day)}
            if location:
                body["location"] = location
            if emails:
                body["attendees"] = [{"email": e} for e in emails]
            self.api.insert(target.id, body, "all" if emails else "none")
            text = "J'ai ajouté « {} » {} dans {}.".format(title, dates.span(begin, finish, all_day, today), target.label)
            if emails:
                text += " Invitation envoyée à {}.".format(_people(len(emails)))
            elif without_address:
                text += " Je n'avais pas d'adresse e-mail : aucune invitation envoyée."
            overlapping = [e for e in clash if e.overlaps(begin, finish)]
            if overlapping:
                text += " Attention, ça chevauche {}.".format(_join(["« {} »".format(e.title) for e in overlapping]))
            if unchecked:
                text += " " + unchecked
            return Result(True, text)

        same = [e for e in clash if norm(e.title) == norm(title) and e.start == start]
        if same and not overlap:  # déjà dans l'agenda : pas de doublon (« mets-le plutôt dans mon calendrier X »)
            existing = same[0]
            span = dates.span(existing.start, existing.end, existing.all_day, today)
            if existing.calendar.id == target.id or not existing.calendar.writable or existing.recurring:
                raise CalendarError("« {} » existe déjà, {}, dans {}.".format(existing.title, span, existing.calendar.label))
            return self._transfer(existing, target, today)
        invite = " et j'invite {}".format(_say_emails(emails)) if emails else ""
        if clash and not overlap:
            proposal = self._propose(start, end - start)
            head = "{}, tu as déjà {}.".format(_cap(dates.span(start, end, False, today)), self._clash_text(clash, today))
            if proposal:
                finish = proposal + (end - start)
                when_text = (("de {} à {}".format(dates.time_name(proposal.time()), dates.time_name(finish.time())))
                             if proposal.date() == start.date() else dates.span(proposal, finish, False, today))
                question = "{} Je le mets plutôt {}{} ?".format(head, when_text, invite)
                execute = lambda: create(proposal, finish)  # noqa: E731
            else:
                question = "{} Je l'ajoute quand même{} ?".format(head, invite)
                execute = lambda: create(start, end)  # noqa: E731
            question += "\nSinon, dis une autre heure, ou « ajoute-le quand même »."
        elif emails:
            question = "Je crée « {} » {} dans {}{} ?".format(title, dates.span(start, end, all_day, today),
                                                              target.label, invite)
            execute = lambda: create(start, end)  # noqa: E731
        else:
            question, execute = "", lambda: create(start, end)  # noqa: E731
        if emails:
            question += "\nInvités : {}".format(", ".join(emails))
        return _Plan(question, execute)

    # ---- Retrouver un événement ----

    def _find(self, event: str = "", when: str = "", calendar: str = "") -> Event:
        now = self._now()
        today = now.date()
        moment = None
        if when:
            w = dates.parse(when, now)
            start, end = w.bounds()
            if w.start and not w.period:
                moment = at(w.first, w.start)
            where = " " + (("le " + dates.day_name(w.first, today)) if not w.period else _period(w, today))
        else:
            start, end = now - dt.timedelta(hours=12), now + dt.timedelta(days=_SEARCH_DAYS)
            where = " dans les {} prochains jours".format(_SEARCH_DAYS)
        chosen = self._calendar(calendar) if _named(calendar) else None
        if not when and event:  # « ma réunion de lundi » : la date est restée dans le nom
            try:
                guess = dates.parse(event, now)
            except DateError:
                guess = None
            if guess and guess.has_day:
                return self._find(event, event, calendar)
        events = [e for e in self._read(start, end, [chosen] if chosen else self._readable())
                  if e.kind not in ("birthday", "workingLocation") and not e.declined]
        if moment:  # « la réunion de lundi 14 h »
            events = [e for e in events if e.start == moment or (not e.all_day and e.start <= moment < e.end)] or events
        if not events:
            raise CalendarError("Tu n'as aucun événement{}.".format(where))
        scored = [(_score(e.title, event), e) for e in events]
        best = max(s for s, _ in scored)
        if best < 1.8 and not (_generic(event) and best >= 1):
            listing = " Il y a : {}.".format(_join([self._when_short(e, today) for e in events])) if len(events) <= 6 else ""
            raise CalendarError("Je ne trouve pas « {} »{}.{}".format(event, where, listing))
        candidates = [e for s, e in scored if s == best]
        if len(candidates) > 1:
            raise CalendarError("Plusieurs événements correspondent : {}. Lequel ?".format(
                _join([self._when_short(e, today) for e in candidates[:5]] + _more(len(candidates) - 5))))
        return candidates[0]

    def _when_short(self, e: Event, today: dt.date) -> str:
        if e.all_day:
            return "« {} » {} (toute la journée)".format(e.title, dates.day_name(e.start.date(), today))
        return "« {} » {} à {}".format(e.title, dates.day_name(e.start.date(), today), dates.time_name(e.start.time()))

    @staticmethod
    def _writable(e: Event) -> None:
        if not e.calendar.writable:
            raise CalendarError("« {} » est dans {}, en lecture seule : je ne peux pas le modifier.".format(
                e.title, e.calendar.label))

    @staticmethod
    def _notes(e: Event, updates: str) -> str:
        notes = []
        if updates == "all":
            notes.append("{} prévenu{}.".format(_cap(_people(len(e.guests), "participant")),
                                                 "s" if len(e.guests) > 1 else ""))
        if e.organizer:
            notes.append("C'est une invitation de {} : seul ton agenda change.".format(e.organizer))
        if e.recurring:
            notes.append("Seulement cette occurrence, pas toute la série.")
        return (" " + " ".join(notes)) if notes else ""

    # ---- Modifications ----

    def _transfer(self, e: Event, target: Calendar, today: dt.date) -> _Plan:
        """L'événement existe déjà dans un autre calendrier : le déplacer plutôt que créer un doublon."""
        updates = "all" if e.guests and not e.organizer else "none"
        question = "« {} » existe déjà, {}, dans {}. Je le déplace dans {} ?".format(
            e.title, dates.span(e.start, e.end, e.all_day, today), e.calendar.label, target.label) + self._notes(e, updates)

        def execute() -> Result:
            self.api.move(e.calendar.id, e.id, target.id, updates)
            return Result(True, "J'ai déplacé « {} » dans {}.".format(e.title, target.label))

        return _Plan(question, execute)

    def _plan_move(self, event: str = "", when: str = "", to: str = "", duration: str = "", calendar: str = "",
                   **_) -> _Plan:
        if not event and not when:
            raise CalendarError("Quel événement dois-je déplacer ?")
        now = self._now()
        today = now.date()
        e = self._find(event, when)  # calendar = destination, pas un filtre
        self._writable(e)
        target = self._calendar(calendar, writable=True) if calendar else None
        if target and target.id == e.calendar.id:
            target = None
        if not to and not duration and not target:
            raise CalendarError("Où dois-je déplacer « {} » ? Donne le nouveau jour ou la nouvelle heure.".format(e.title))
        start, end, all_day = e.start, e.end, e.all_day
        if to:
            w = dates.parse(to, now)
            day = w.first if w.has_day else e.start.date()
            if w.all_day:
                start, all_day = at(day, dt.time(0)), True
                end = start + (e.end - e.start if e.all_day else dt.timedelta(days=1))
            elif w.start:
                start, all_day = at(day, w.start), False
                length = e.end - e.start if not e.all_day else dt.timedelta(minutes=self.settings.default_duration)
                end = at(day, w.end) if w.end else start + (self._duration(duration) or w.duration or length)
            else:  # seulement un jour : même heure
                start = at(day, e.start.time())
                end = start + ((not e.all_day and self._duration(duration)) or e.end - e.start)
            if end <= start:
                end += dt.timedelta(days=1)
        elif duration and not e.all_day and self._duration(duration):
            end = start + self._duration(duration)
        moved = (start, end, all_day) != (e.start, e.end, e.all_day)
        if moved:
            self._check_future(start, all_day)
        if target and e.recurring:
            raise CalendarError("Je ne peux pas changer de calendrier une seule occurrence d'un événement récurrent.")
        clash = self._busy(start, end, exclude=e) if moved and not all_day else []
        updates = "all" if e.guests and not e.organizer else "none"
        new_span = dates.span(start, end, all_day, today)
        warning = "Attention, tu as déjà {} à ce moment. ".format(self._clash_text(clash, today)) if clash else ""
        if moved:
            question = "{}Je déplace « {} » ({}) : nouveau créneau {}{} ?".format(
                warning, e.title, dates.span(e.start, e.end, e.all_day, today), new_span,
                ", dans {}".format(target.label) if target else "")
        else:
            question = "Je déplace « {} » ({}) vers {} ?".format(e.title, dates.span(e.start, e.end, e.all_day, today),
                                                                  target.label)
        question += self._notes(e, updates)

        def execute() -> Result:
            if moved:
                self.api.patch(e.calendar.id, e.id, _times(start, end, all_day, switch=all_day != e.all_day),
                               e.etag, updates)
            if target:
                self.api.move(e.calendar.id, e.id, target.id, updates)
            if moved:
                return Result(True, "J'ai déplacé « {} » : {}{}.".format(
                    e.title, new_span, ", dans {}".format(target.label) if target else ""))
            return Result(True, "J'ai déplacé « {} » dans {}.".format(e.title, target.label))

        return _Plan(question, execute)

    def _plan_delete(self, event: str = "", when: str = "", calendar: str = "", **_) -> _Plan:
        if not event and not when:
            raise CalendarError("Quel événement dois-je supprimer ?")
        today = self._now().date()
        e = self._find(event, when, calendar)
        self._writable(e)
        updates = "all" if e.guests and not e.organizer else "none"
        span = dates.span(e.start, e.end, e.all_day, today)
        question = "Je supprime « {} », {}, dans {} ?".format(e.title, span, e.calendar.label) + self._notes(e, updates)

        def execute() -> Result:
            self.api.delete(e.calendar.id, e.id, e.etag, updates)
            return Result(True, "J'ai supprimé « {} », {}.".format(e.title, span))

        return _Plan(question, execute)

    def _plan_update(self, event: str = "", when: str = "", title: str = "", location: str = "",
                     duration: str = "", attendees: str = "", calendar: str = "", to: str = "", **_) -> _Plan:
        if attendees:
            return self._plan_invite(event=event or title, when=when, attendees=attendees, calendar=calendar)
        if to:  # un nouvel horaire est un déplacement (« décale le cours à 16 h »), avec sa confirmation
            return self._plan_move(event=event or title, when=when, to=to, duration=duration)
        if not event and not when:
            raise CalendarError("Quel événement dois-je modifier ?")
        today = self._now().date()
        e = self._find(event, when, calendar)
        self._writable(e)
        changes, said = {}, []
        new_title = (title or "").strip(" .«»\"")
        if new_title and norm(new_title) != norm(e.title):
            changes["summary"] = new_title
            said.append("nouveau nom « {} »".format(new_title))
        if location:
            changes["location"] = location
            said.append("lieu {}".format(location))
        length = None if e.all_day else self._duration(duration)
        timed = bool(length)
        if timed:
            end = e.start + length
            changes.update(_times(e.start, end, False))
            said.append("de {} à {}".format(dates.time_name(e.start.time()), dates.time_name(end.time())))
        if not changes:
            raise CalendarError("Que dois-je changer dans « {} » ? Le nom, le lieu ou la durée.".format(e.title))
        updates = "all" if e.guests and not e.organizer else "none"
        span = dates.span(e.start, e.end, e.all_day, today)
        sensitive = timed or bool(e.guests) or e.recurring or bool(e.organizer)
        question = ("Je modifie « {} » ({}) : {} ?".format(e.title, span, _join(said)) + self._notes(e, updates)
                    if sensitive else "")

        def execute() -> Result:
            self.api.patch(e.calendar.id, e.id, changes, e.etag, updates)
            return Result(True, "J'ai modifié « {} » ({}) : {}.".format(e.title, span, _join(said)))

        return _Plan(question, execute)

    def _plan_invite(self, event: str = "", when: str = "", attendees: str = "", calendar: str = "", **_) -> _Plan:
        if not _has_email(attendees):
            raise CalendarError("Donne-moi l'adresse e-mail de chaque invité : je n'en invente aucune.")
        emails = self._emails(attendees)
        if not event and not when:
            raise CalendarError("À quel événement dois-je les inviter ?")
        today = self._now().date()
        e = self._find(event, when, calendar)
        self._writable(e)
        if e.organizer:
            raise CalendarError("« {} » est organisé par {} : seul l'organisateur peut inviter.".format(e.title, e.organizer))
        known = {g.lower() for g in e.guests}
        new = [m for m in emails if m not in known]
        if not new:
            raise CalendarError("{} déjà invité{} à « {} ».".format(_join(emails), "s" if len(emails) > 1 else "", e.title))
        keep = ("email", "displayName", "optional", "responseStatus", "comment", "additionalGuests")
        current = [{k: v for k, v in a.items() if k in keep} for a in e.raw.get("attendees") or [] if a.get("email")]
        span = dates.span(e.start, e.end, e.all_day, today)
        question = ("J'invite {} à « {} » ({}) ?".format(_say_emails(new), e.title, span)
                    + (" Seulement cette occurrence, pas toute la série." if e.recurring else "")
                    + "\nInvités : {}".format(", ".join(new)))

        def execute() -> Result:
            self.api.patch(e.calendar.id, e.id, {"attendees": current + [{"email": m} for m in new]}, e.etag, "all")
            return Result(True, "Invitation envoyée à {} pour « {} », {}.".format(_people(len(new)), e.title, span))

        return _Plan(question, execute)

    # ---- Adresses e-mail ----

    def _emails(self, attendees: str) -> list[str]:
        """Adresses valides, dites ou écrites par l'utilisateur (pas inventées, ni lues dans une invitation)."""
        text = _dictated(attendees or "")
        tokens = [t.strip(".;:()<>\"'«»") for t in re.split(r"[\s,;]+", text)]
        emails, invalid = [], []
        for token in tokens:
            if "@" in token:
                (emails if _EMAIL.fullmatch(token) else invalid).append(token)
        if invalid:
            raise CalendarError("Adresse e-mail invalide : « {} ». Écris-la, ou épelle-la.".format(invalid[0]))
        heard = re.sub(r"\s+", "", _dictated(self._heard()))
        unheard = [m for m in emails if m not in heard]
        if unheard:
            raise CalendarError("Je n'invite que les adresses que tu m'as données toi-même ; je n'ai pas entendu {}."
                                .format(_join(unheard)))
        return list(dict.fromkeys(emails))


# ---- Petites fonctions ----

def _failure(error: Exception) -> Result:
    if isinstance(error, NotConnected):
        return Result(False, _NOT_CONNECTED)
    if isinstance(error, secret_store.SecretError):
        return Result(False, "Le jeton Google Agenda est illisible : relance la connexion (login).")
    return Result(False, str(error))


def _key(action: str, args: dict) -> str:
    return json.dumps({"action": action, **{k: str(v) for k, v in args.items()}}, ensure_ascii=False, sort_keys=True)


def _times(start: dt.datetime, end: dt.datetime, all_day: bool, switch: bool = False) -> dict:
    """Début et fin pour Google ; switch : l'événement passe de « toute la journée » à une heure, ou l'inverse."""
    if all_day:
        first, last = {"date": start.date().isoformat()}, {"date": end.date().isoformat()}
        if switch:
            for part in (first, last):
                part.update(dateTime=None, timeZone=None)
    else:
        first = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        last = {"dateTime": end.isoformat(), "timeZone": TIMEZONE}
        if switch:
            first["date"] = last["date"] = None
    return {"start": first, "end": last}


def _dictated(text: str) -> str:
    """Adresse dictée -> écrite : « alex arobase gmail point com » -> « alex@gmail.com »."""
    text = norm(text).replace("’", "'")

    def build(m: re.Match) -> str:
        local = m[1]
        for spoken, written in ((" tiret bas ", "_"), (" underscore ", "_"), (" tiret ", "-"), (" point ", ".")):
            local = local.replace(spoken, written)
        return local + "@" + m[2].replace(" point ", ".")

    text = _SPOKEN_EMAIL.sub(build, text)
    text = re.sub(r"\s*@\s*", "@", text)
    return re.sub(r"(@[a-z0-9.-]+?)\s*\.\s+([a-z]{2,})\b", r"\1.\2", text)  # « gmail. com »


def _has_email(text: str) -> bool:
    return "@" in _dictated(text or "")


def _say_emails(emails: list[str]) -> str:
    """Pour la voix : « alex arobase gmail point com » (une ou deux adresses), sinon leur nombre."""
    if len(emails) > 2:
        return "{} personnes".format(len(emails))
    return _join([m.replace("@", " arobase ").replace(".", " point ") for m in emails])


def _people(count: int, word: str = "personne") -> str:
    return "{} {}{}".format("une" if count == 1 and word == "personne" else "un" if count == 1 else count, word,
                            "s" if count > 1 else "")


def _words(text: str) -> list[str]:
    """Mots qui désignent l'événement, sans les articles ni « à ». Les accents comptent ici : « à » est ignoré,
    mais « A » (« Test A ») reste un mot du nom."""
    found = []
    for word in re.findall(r"[\w-]+", (text or "").lower().replace("’", "'")):
        if word not in _FILLERS_SAID and norm(word) not in _DATE_WORDS and not word.isdigit():
            found.append(norm(word))
    return found


def _score(title: str, wanted: str) -> float:
    """Ressemblance d'un titre avec l'événement dit : 3 identique, 2,5 contenu, 2,2 tous les mots précis
    présents (fautes de transcription tolérées sur les mots de 4 lettres et plus), 2 un mot générique présent
    (« ma réunion ») ; en dessous de 1,8, pas trouvé. Un mot dit absent du titre suffit à l'écarter."""
    words = _words(wanted)
    found = " ".join(re.findall(r"[\w-]+", norm(title)))
    if not words:
        return 1.0
    key = " ".join(words)
    if key == found:
        return 3.0
    if re.search(r"(?<![\w-]){}(?![\w-])".format(re.escape(key)), found):
        return 2.5
    title_words = found.split()

    def has(word: str) -> bool:
        return any(w == word or (len(word) >= 4 and difflib.SequenceMatcher(None, w, word).ratio() >= 0.8)
                   for w in title_words)

    precise = [w for w in words if w not in _GENERIC]
    if precise:
        return 2.2 if all(has(w) for w in precise) else sum(has(w) for w in precise) / len(precise)
    return 2.0 if any(has(w) for w in words) else 1.0


def _generic(wanted: str) -> bool:
    return all(w in _GENERIC for w in _words(wanted))


def _strip(text: str, words: set) -> str:
    return " ".join(w for w in norm(text).replace("'", " ").split() if w not in words)


_INSIST = re.compile(r"\b(?:quand meme|tant pis|pas grave|ca ne fait rien|peu importe|malgre (?:le|ce) conflit"
                     r"|garde (?:l'horaire|l'heure|cette heure|ce creneau)|meme si c'est pris)\b")


def _insists(heard: str) -> bool:
    """La dernière phrase de l'utilisateur garde l'horaire malgré le conflit (« ajoute-le quand même »)."""
    lines = (heard or "").strip().splitlines()
    return bool(lines) and bool(_INSIST.search(norm(lines[-1]).replace("’", "'")))


def _named(calendar: str) -> bool:
    """Un vrai nom de calendrier, pas seulement « mon calendrier » ou « mon agenda »."""
    return bool(_strip(calendar or "", _CALENDAR_WORDS))


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " et " + items[-1] if items else ""


def _more(count: int) -> list[str]:
    return ["{} autre{}".format(count, "s" if count > 1 else "")] if count > 0 else []


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _period(w: dates.When, today: dt.date) -> str:
    if not w.period:
        return dates.day_name(w.first, today)
    if w.first.month == w.last.month and w.first.year == w.last.year:
        return "du {} {} au {}".format(dates.DAYS[w.first.weekday()], "1er" if w.first.day == 1 else w.first.day,
                                       dates.day_name(w.last, today))
    return "du {} au {}".format(dates.day_name(w.first, today), dates.day_name(w.last, today))


def _ceil_quarter(moment: dt.datetime) -> dt.datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment + dt.timedelta(minutes=(-moment.minute) % 15)


def _quarters(start: dt.datetime, last: dt.datetime) -> list[dt.datetime]:
    moments, moment = [], _ceil_quarter(start)
    while moment <= last:
        moments.append(moment)
        moment += dt.timedelta(minutes=15)
    return moments


def _gaps(start: dt.datetime, end: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]):
    gaps, cursor = [], start
    for begin, finish in sorted(busy):
        if begin > cursor:
            gaps.append((cursor, min(begin, end)))
        cursor = max(cursor, finish)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(a, b) for a, b in gaps if b > a]


def _gap_text(gaps: list[tuple[dt.datetime, dt.datetime]]) -> str:
    return _join(["de {} à {}".format(dates.time_name(a.time()), dates.time_name(b.time())) for a, b in gaps])


# ---- Outil ----

DESCRIPTION = (
    "Google Agenda de l'utilisateur, tous ses calendriers (planning, rendez-vous, réunions, cours, "
    "disponibilités, invitations). list : ce qui est prévu sur une période ; free : créneaux libres ; "
    "calendars : liste des calendriers ; add : crée un événement, avec ses invités s'il y en a ; update : change "
    "le nom, le lieu ou la durée d'un événement existant ; move : change le jour, l'heure ou le calendrier d'un "
    "événement existant (décaler, déplacer, le mettre plutôt dans un autre calendrier) ; delete : le supprime "
    "ou l'annule ; invite : ajoute des invités à un événement qui existe déjà. Donne les dates et les heures "
    "telles que l'utilisateur les dit, sans les calculer. Le code demande confirmation quand il le faut.")


def register(box: Toolbox, agenda: Agenda) -> None:
    box.add(Tool("agenda", DESCRIPTION, agenda.run, {
        "action": Param("list, free, calendars, add, update, move, delete ou invite",
                        enum=["list", "free", "calendars", "add", "update", "move", "delete", "invite"], aliases={
                            "lire": "list", "voir": "list", "planning": "list", "lister": "list",
                            "libre": "free", "disponibilites": "free", "dispo": "free", "calendriers": "calendars",
                            "ajouter": "add", "creer": "add", "create": "add", "modifier": "update",
                            "renommer": "update", "edit": "update", "deplacer": "move", "decaler": "move",
                            "reschedule": "move", "supprimer": "delete", "annuler": "delete", "remove": "delete",
                            "inviter": "invite"}),
        "title": Param("add : intitulé de l'événement à créer, tel que dit ; update : son nouveau nom", required=False),
        "event": Param("update, move, delete, invite : le nom de l'événement existant visé, tel que dit, sans sa "
                       "date (la date va dans when)", required=False),
        "when": Param("date, heure ou période telles que dites, sans les calculer. add : quand il a lieu ; list, "
                      "free : la période ; update, move, delete, invite : quand a lieu l'événement visé, avant tout "
                      "changement", required=False),
        "to": Param("move : le nouveau jour et/ou la nouvelle heure, seulement ce que l'utilisateur dit (une "
                    "heure seule si seule l'heure change), sans calculer de date", required=False),
        "duration": Param("durée telle que dite, seulement si l'utilisateur en donne une ; sinon vide", required=False),
        "calendar": Param("seulement le nom propre du calendrier que l'utilisateur nomme ; vide s'il n'en nomme pas. "
                          "add : où créer ; move : vers lequel déplacer ; list, free : lequel lire", required=False),
        "attendees": Param("adresses e-mail des invités, exactement comme l'utilisateur les a données, séparées par "
                           "des virgules ; jamais inventées", required=False),
        "location": Param("lieu, seulement s'il est dit", required=False),
        "overlap": Param("true seulement si l'utilisateur veut garder l'horaire malgré un conflit", type="boolean",
                         required=False),
    }, untrusted=True, private=True, guard=agenda.guard, prepare=agenda.prepare))


def _clock(value: str, default: dt.time) -> dt.time:
    try:
        hour, _, minute = str(value).partition(":")
        return dt.time(int(hour), int(minute or 0))
    except ValueError:
        return default


def from_config(cfg, heard: Callable[[], str] = lambda: "") -> Agenda | None:
    account = cfg.get("google_calendar.account")
    if not account:
        return None
    folder = secret_store.store_dir()
    auth = GoogleAuth(account, SCOPES, folder / "google_client.bin", folder / "google_token.bin")
    settings = Settings(
        account=account,
        default_calendar=str(cfg.get("google_calendar.default_calendar", "") or ""),
        aliases=dict(cfg.get("google_calendar.aliases", {}) or {}),
        default_duration=int(cfg.get("google_calendar.default_duration", 60)),
        day_start=_clock(cfg.get("google_calendar.day_start", "08:00"), dt.time(8)),
        day_end=_clock(cfg.get("google_calendar.day_end", "20:00"), dt.time(20)),
        min_free=int(cfg.get("google_calendar.min_free", 30)),
        ignore=list(cfg.get("google_calendar.ignore_for_conflicts", []) or []))
    return Agenda(CalendarApi(auth), settings, heard)


def _warn_expiry(auth: GoogleAuth) -> None:
    expires = auth.refresh_expires_at()
    if expires:
        print("ATTENTION : Google limite cette connexion (jusqu'au {}). L'application est encore en mode « Test » : "
              "dans Google Cloud, « Audience » > « Publier l'application » (en production), puis reconnecte-toi "
              "pour ne plus avoir à le refaire.".format(expires.replace("T", " ")))
    else:
        print("Connexion sans date d'expiration : le jeton se renouvelle seul.")


def _check_account(agenda: Agenda) -> str:
    """Adresse du calendrier principal : c'est le compte connecté."""
    primary = agenda.primary()
    return primary.id if primary else "?"


def main() -> None:
    from jarvis.config import Config

    parser = argparse.ArgumentParser(description="Connexion de Jarvis à Google Agenda")
    parser.add_argument("action", choices=["login", "status", "logout"])
    parser.add_argument("--client", help="fichier JSON du client OAuth « Application de bureau » à importer")
    opts = parser.parse_args()
    cfg = Config()
    agenda = from_config(cfg)
    if agenda is None:
        raise SystemExit("google_calendar.account manque dans config.yaml")
    auth, account = agenda.api.auth, agenda.settings.account
    try:
        if opts.action == "login":
            client = cfg.path("google_calendar.client_file", "secret_google_calendar.json")
            if opts.client:
                client = Path(opts.client)
            if opts.client or not auth.has_client():
                if not client.exists():
                    raise SystemExit("Fichier d'identifiants introuvable : {}".format(client))
                auth.import_client(client)
                print("Identifiants OAuth importés, chiffrés par DPAPI :", auth.client_store)
            print("Le navigateur s'ouvre : choisis le compte {} et accepte l'accès ({} s au plus).".format(account, 180))
            granted = auth.login(lambda url: webbrowser.open(url) or print("Ouvre cette adresse :", url))
            connected = _check_account(agenda)
            if connected.lower() != account.lower():
                auth.logout()
                raise SystemExit("Mauvais compte ({}) : il faut {}. Accès révoqué, relance la connexion.".format(
                    connected, account))
            print("Connecté à {}.".format(account))
            print("Permissions accordées :", ", ".join(s.rsplit("/", 1)[-1] for s in granted))
            _warn_expiry(auth)
            print(agenda.list_calendars().message)
            if client.exists():
                print("Tu peux supprimer {} : Jarvis en garde une copie chiffrée.".format(client))
        elif opts.action == "status":
            if not auth.connected():
                print("Non connecté.", _NOT_CONNECTED)
                return
            print("Compte connecté :", _check_account(agenda), "(attendu : {})".format(account))
            print("Permissions accordées :", ", ".join(s.rsplit("/", 1)[-1] for s in auth.granted()))
            for c in agenda.calendars(fresh=True):
                print("  - {}{} : {}".format(c.name, " (principal)" if c.primary else "", c.access))
            print("Jeton chiffré (DPAPI) :", auth.token_store)
            _warn_expiry(auth)
        else:
            auth.logout()
            print("Déconnecté : accès révoqué chez Google et jeton effacé.")
    except NotConnected:
        raise SystemExit(_NOT_CONNECTED)
    except (AuthError, CalendarError, secret_store.SecretError) as exc:
        raise SystemExit("Erreur : {}".format(exc))


if __name__ == "__main__":
    main()
