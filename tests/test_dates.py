"""Dates dites en français, fuseau Europe/Paris : python -m unittest tests.test_dates

Référence : jeudi 5 novembre 2026, 14 h 30.
"""

import datetime as dt
import unittest

from jarvis.tools import dates
from jarvis.tools.dates import DateError, parse, parse_duration

NOW = dt.datetime(2026, 11, 5, 14, 30, tzinfo=dates.TZ)
D = dt.date
T = dt.time


def when(text):
    w = parse(text, NOW)
    return w.first, w.last, w.start, w.end


class DayTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(when("aujourd'hui")[0], D(2026, 11, 5))
        self.assertEqual(when("demain")[0], D(2026, 11, 6))
        self.assertEqual(when("après-demain")[0], D(2026, 11, 7))
        self.assertEqual(when("hier")[0], D(2026, 11, 4))

    def test_weekdays(self):
        self.assertEqual(when("vendredi")[0], D(2026, 11, 6))
        self.assertEqual(when("lundi")[0], D(2026, 11, 9))
        self.assertEqual(when("jeudi")[0], D(2026, 11, 5))                   # aujourd'hui compris
        self.assertEqual(when("jeudi à 10 h")[0], D(2026, 11, 12))           # 10 h est passé : jeudi suivant
        self.assertEqual(when("jeudi à 18 h")[0], D(2026, 11, 5))
        self.assertEqual(when("jeudi prochain")[0], D(2026, 11, 12))
        self.assertEqual(when("mardi prochain")[0], D(2026, 11, 10))
        self.assertEqual(when("mardi de la semaine prochaine")[0], D(2026, 11, 10))
        self.assertEqual(when("lundi de la semaine prochaine")[0], D(2026, 11, 9))
        self.assertEqual(when("jeudi en huit")[0], D(2026, 11, 12))
        self.assertEqual(when("mardi en huit")[0], D(2026, 11, 17))
        self.assertEqual(when("lundi dernier")[0], D(2026, 11, 2))

    def test_explicit_dates(self):
        self.assertEqual(when("le 12 novembre")[0], D(2026, 11, 12))
        self.assertEqual(when("le 1er décembre")[0], D(2026, 12, 1))
        self.assertEqual(when("le 3 janvier")[0], D(2027, 1, 3))             # passé depuis longtemps : l'an prochain
        self.assertEqual(when("le 2 novembre")[0], D(2026, 11, 2))           # passé de peu : cette année
        self.assertEqual(when("12/11")[0], D(2026, 11, 12))
        self.assertEqual(when("le 12")[0], D(2026, 11, 12))
        self.assertEqual(when("le 2")[0], D(2026, 12, 2))                    # le 2 est passé : le mois prochain
        self.assertEqual(when("lundi 9 novembre")[0], D(2026, 11, 9))
        self.assertEqual(when("2026-11-06T18:00")[:3], (D(2026, 11, 6), D(2026, 11, 6), T(18)))

    def test_periods(self):
        self.assertEqual(when("cette semaine")[:2], (D(2026, 11, 5), D(2026, 11, 8)))
        self.assertEqual(when("la semaine prochaine")[:2], (D(2026, 11, 9), D(2026, 11, 15)))
        self.assertEqual(when("ce week-end")[:2], (D(2026, 11, 7), D(2026, 11, 8)))
        self.assertEqual(when("du 12 au 15 novembre")[:2], (D(2026, 11, 12), D(2026, 11, 15)))
        self.assertEqual(when("les 3 prochains jours")[:2], (D(2026, 11, 5), D(2026, 11, 7)))
        self.assertEqual(when("dans 3 jours")[0], D(2026, 11, 8))

    def test_invalid(self):
        for text in ("n'importe quand", "le 31 février", "25 h", ""):
            with self.assertRaises(DateError, msg=text):
                parse(text, NOW)


class TimeTest(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(when("vendredi à 18 h")[2], T(18))
        self.assertEqual(when("vendredi 18h30")[2], T(18, 30))
        self.assertEqual(when("vendredi à 18:45")[2], T(18, 45))
        self.assertEqual(when("dix-huit heures trente")[2], T(18, 30))
        self.assertEqual(when("demain midi et demi")[2], T(12, 30))
        self.assertEqual(when("demain 18 h moins le quart")[2], T(17, 45))
        self.assertEqual(when("demain 6 h du soir")[2], T(18))
        self.assertEqual(when("demain soir à 8 h")[2], T(20))
        self.assertEqual(when("demain à 9 h du matin")[2], T(9))

    def test_ranges(self):
        self.assertEqual(when("vendredi de 14 h à 15 h 30")[2:], (T(14), T(15, 30)))
        self.assertEqual(when("vendredi entre 9 h et 11 h")[2:], (T(9), T(11)))
        self.assertEqual(when("jusqu'à 19 h")[3], T(19))

    def test_hour_alone_is_its_next_occurrence(self):
        self.assertEqual(when("à 15 h")[0], D(2026, 11, 5))
        self.assertEqual(when("à 10 h")[0], D(2026, 11, 6))
        self.assertFalse(parse("à 15 h", NOW).has_day)

    def test_relative(self):
        w = parse("dans 2 heures", NOW)
        self.assertEqual((w.first, w.start), (D(2026, 11, 5), T(16, 30)))

    def test_parts_of_the_day(self):
        w = parse("demain matin", NOW)
        self.assertEqual((w.first, w.start, w.part), (D(2026, 11, 6), None, (T(6), T(12))))
        self.assertEqual(parse("ce soir", NOW).bounds()[0], dt.datetime(2026, 11, 5, 18, tzinfo=dates.TZ))
        self.assertTrue(parse("toute la journée samedi", NOW).all_day)

    def test_duration_inside_the_date(self):
        self.assertEqual(parse("vendredi 18 h pendant une heure", NOW).duration, dt.timedelta(hours=1))

    def test_summer_time_is_handled(self):  # changement d'heure le 29 mars 2026
        self.assertEqual(dates.at(D(2026, 3, 30), T(9)).utcoffset(), dt.timedelta(hours=2))
        self.assertEqual(dates.at(D(2026, 3, 28), T(9)).utcoffset(), dt.timedelta(hours=1))


class DurationTest(unittest.TestCase):
    def test_durations(self):
        cases = {"1 h": 60, "une heure": 60, "1 h 30": 90, "1h30": 90, "90 minutes": 90, "une demi-heure": 30,
                 "deux heures": 120, "1 heure et demie": 90, "trois quarts d'heure": 45, "1,5 h": 90, "45 min": 45,
                 "2": 120, "90": 90}
        for text, minutes in cases.items():
            self.assertEqual(parse_duration(text), dt.timedelta(minutes=minutes), text)

    def test_invalid_durations(self):
        for text in ("longtemps", "0 minute", "30 jours"):
            with self.assertRaises(DateError, msg=text):
                parse_duration(text)


class SpeechTest(unittest.TestCase):
    def test_full_date_is_said(self):
        start = dates.at(D(2026, 11, 6), T(18))
        self.assertEqual(dates.span(start, start + dt.timedelta(hours=1), today=NOW.date()),
                         "vendredi 6 novembre de 18 h à 19 h")
        self.assertEqual(dates.span(dates.at(D(2026, 12, 1), T(0)), dates.at(D(2026, 12, 2), T(0)), all_day=True,
                                    today=NOW.date()), "mardi 1er décembre, toute la journée")
        self.assertEqual(dates.day_name(D(2027, 1, 4), NOW.date()), "lundi 4 janvier 2027")
        self.assertEqual(dates.time_name(T(9, 5)), "9 h 05")


if __name__ == "__main__":
    unittest.main()
