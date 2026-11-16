"""Dates et heures dites en français -> dates précises, fuseau Europe/Paris.

Le modèle passe les dates telles que l'utilisateur les dit (« vendredi 18 h », « demain matin », « la semaine
prochaine ») : un modèle de 8B se trompe vite en calculant « jeudi prochain ». Le calcul se fait ici, et les
réponses de Jarvis disent toujours la date complète, pour qu'une erreur s'entende tout de suite.

Règles retenues (le français est ambigu) :
- « jeudi » : le prochain jeudi, aujourd'hui compris (sauf si l'heure dite est déjà passée : jeudi suivant) ;
- « jeudi prochain » : le prochain jeudi après aujourd'hui ; « jeudi de la semaine prochaine » : celui de la
  semaine suivante (lundi à dimanche) ; « jeudi en huit » : une semaine après « jeudi » ;
- une heure sans jour (« à 15 h ») : sa prochaine occurrence ;
- « le 12 » : ce mois-ci, ou le mois prochain si le 12 est passé ; « le 3 janvier » : l'année prochaine s'il est
  passé depuis plus d'un mois.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from jarvis.tools import norm

TZ = ZoneInfo("Europe/Paris")
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MONTHS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
           "novembre", "decembre"]
MONTH_NAMES = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
               "novembre", "décembre"]
_MONTH = "|".join(_MONTHS)
_PARTS = {  # moment de la journée sans heure précise
    "matin": (dt.time(6), dt.time(12)), "matinee": (dt.time(6), dt.time(12)),
    "apres-midi": (dt.time(12), dt.time(18)), "aprem": (dt.time(12), dt.time(18)),
    "soir": (dt.time(18), dt.time(23, 59)), "soiree": (dt.time(18), dt.time(23, 59)),
    "nuit": (dt.time(20), dt.time(23, 59)),
}
_NUMBERS = {
    "vingt et une": 21, "vingt et un": 21, "vingt deux": 22, "vingt trois": 23, "vingt quatre": 24,
    "vingt cinq": 25, "trente cinq": 35, "quarante cinq": 45, "cinquante cinq": 55, "quatre vingt dix": 90,
    "dix sept": 17, "dix huit": 18, "dix neuf": 19, "zero": 0, "une": 1, "un": 1, "deux": 2, "trois": 3,
    "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
    "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "vingt": 20, "trente": 30, "quarante": 40,
    "cinquante": 50, "soixante": 60,
}
_NUMBER_WORDS = re.compile(r"\b(?:{})\b".format("|".join(sorted(_NUMBERS, key=len, reverse=True))))


class DateError(ValueError):
    pass


@dataclass
class When:
    first: dt.date                      # premier jour
    last: dt.date                       # dernier jour (inclus)
    start: dt.time | None = None        # heure de début dite
    end: dt.time | None = None          # heure de fin dite (« de 14 h à 16 h »)
    all_day: bool = False               # « toute la journée »
    part: tuple[dt.time, dt.time] | None = None  # « demain matin » : plage, sans heure précise
    has_day: bool = True                # un jour a été dit (sinon : la prochaine occurrence de l'heure)
    duration: dt.timedelta | None = None  # « pendant une heure », glissé dans la date

    @property
    def period(self) -> bool:
        return self.last > self.first

    def bounds(self) -> tuple[dt.datetime, dt.datetime]:
        """Plage à lire : les jours entiers, ou le moment de la journée dit."""
        if self.part and not self.start:
            return at(self.first, self.part[0]), at(self.last, self.part[1])
        return at(self.first, dt.time(0)), at(self.last + dt.timedelta(days=1), dt.time(0))


def at(day: dt.date, time: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, time, TZ)


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def _prepare(text: str) -> str:
    """Minuscules, sans accents ; nombres écrits en lettres -> chiffres (« dix-huit heures » -> « 18 heures »)."""
    text = norm(text).replace("’", "'")
    text = re.sub(r"\b(dix|vingt|trente|quarante|cinquante|quatre)-(?=[a-z])", r"\1 ", text)  # dix-huit, quatre-vingt
    text = re.sub(r"\b(quatre vingt)-", r"\1 ", text)
    text = re.sub(r"\bvingt-et-un", "vingt et un", text)
    return _NUMBER_WORDS.sub(lambda m: str(_NUMBERS[m.group(0)]), text)


# ---- Heures ----

_TIME = re.compile(
    r"(?<![\d/])(?P<h>\d{1,2})\s*(?:heures?|h(?![a-z])|:(?=\d{2}))\s*(?P<m>\d{2})?(?!\d)"
    r"(?:\s*(?P<q>et demie?|et quart|moins le quart|moins (?P<less>\d{1,2})))?"
    r"(?:\s*(?P<p>du matin|du soir|de l'apres-midi|de l'apres midi|de l'aprem|de la nuit))?"
    r"|(?<!apres-)(?<!apres )\b(?P<noon>midi|minuit)(?:\s*(?P<q2>et demie?|et quart|moins le quart))?")


def _time_of(m: re.Match) -> dt.time:
    if m["noon"]:
        hour, minute, extra = (12 if m["noon"] == "midi" else 0), 0, m["q2"]
    else:
        hour, minute, extra = int(m["h"]), int(m["m"] or 0), m["q"]
    total = hour * 60 + minute
    if extra and extra.startswith("et demi"):
        total += 30
    elif extra == "et quart":
        total += 15
    elif extra == "moins le quart":
        total -= 15
    elif extra:
        total -= int(m["less"])
    if not m["noon"] and m["p"] and m["p"] != "du matin" and hour < 12:
        total += 12 * 60
    if hour > 24 or minute > 59 or not 0 <= total <= 24 * 60:
        raise DateError("Heure invalide : « {} ».".format(m.group(0).strip()))
    total %= 24 * 60
    return dt.time(total // 60, total % 60)


# ---- Durées ----

_DURATION = (r"\d+(?:[.,]\d+)?\s*(?:heures?|h(?![a-z]))(?:\s*\d{1,2})?(?:\s*(?:min|minutes))?(?:\s*et (?:demie?|quart))?"
             r"|\d+\s*(?:minutes?|min|mn)\b|(?:\d+ )?demi[- ]heure|\d* ?quarts? d'heure|\d+ jours?")


def durations_in(text: str) -> list[dt.timedelta]:
    """Durées présentes dans une phrase (« pendant une heure et demie » -> 1 h 30)."""
    found = []
    for m in re.finditer(_DURATION, _prepare(text)):
        try:
            found.append(parse_duration(m.group(0)))
        except DateError:
            continue
    return found


def parse_duration(text: str) -> dt.timedelta:
    """« 1 h », « une heure », « 1 h 30 », « 90 minutes », « une demi-heure », « 2 jours », « 1,5 h »."""
    s = _prepare(text)
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*", s)
    if m:  # nombre seul : des heures s'il est petit (« 2 »), sinon des minutes (« 90 »)
        value = float(m[1].replace(",", "."))
        minutes = value * 60 if value <= 12 else value
    else:
        minutes = 0.0
        hours = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:heures?|h(?![a-z]))(?:\s*(\d{1,2})(?!\s*(?:jours?)))?", s)
        if hours:
            minutes += float(hours[1].replace(",", ".")) * 60 + int(hours[2] or 0)
            s = s[:hours.start()] + " " + s[hours.end():]
        if re.search(r"\bet demie?\b", s) and hours:
            minutes += 30
        elif re.search(r"\bet quart\b", s) and hours:
            minutes += 15
        mins = re.search(r"(\d+)\s*(?:minutes?|min|mn)\b", s)
        if mins:
            minutes += int(mins[1])
        if re.search(r"demi[- ]heure", s):
            minutes += 30
        quarters = re.search(r"(\d+)? ?quarts? d'heure", s)
        if quarters:
            minutes += 15 * int(quarters[1] or 1)
        days = re.search(r"(\d+) jours?", s)
        if days:
            minutes += int(days[1]) * 24 * 60
    if minutes <= 0:
        raise DateError("Je ne comprends pas la durée « {} ».".format(text))
    if minutes > 14 * 24 * 60:
        raise DateError("Durée trop longue : « {} ».".format(text))
    return dt.timedelta(minutes=round(minutes))


# ---- Jours ----

def _next_weekday(today: dt.date, weekday: int, strictly_after: bool = False) -> dt.date:
    ahead = (weekday - today.weekday()) % 7
    if strictly_after and ahead == 0:
        ahead = 7
    return today + dt.timedelta(days=ahead)


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _date(day: int, month: int, year: int | None, today: dt.date) -> dt.date:
    try:
        found = dt.date(year or today.year, month, day)
        if year is None and found < today - dt.timedelta(days=31):
            found = dt.date(today.year + 1, month, day)
    except ValueError:
        raise DateError("Date invalide : {} {}.".format(day, MONTH_NAMES[month - 1])) from None
    return found


def _year(text: str | None) -> int | None:
    if not text:
        return None
    return int(text) + (2000 if len(text) == 2 else 0)


def parse(text: str, current: dt.datetime | None = None) -> When:
    """Date, heure ou période dites -> When. DateError si rien n'est compris."""
    current = (current or now()).astimezone(TZ)
    today = current.date()
    s = " {} ".format(_prepare(text))
    w = When(today, today, has_day=False)
    found = False

    def cut(m: re.Match) -> None:
        nonlocal s
        s = s[:m.start()] + " " * (m.end() - m.start()) + s[m.end():]

    def day(first: dt.date, last: dt.date | None = None) -> None:
        nonlocal found
        if not w.has_day:
            w.first, w.last, w.has_day = first, last or first, True
        found = True

    # Date ISO (« 2026-11-06 », « 2026-11-06T18:00 »), parfois donnée par le modèle
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{1,2}):(\d{2}))?", s):
        try:
            day(dt.date(int(m[1]), int(m[2]), int(m[3])))
            if m[4]:
                w.start = dt.time(int(m[4]), int(m[5]))
        except ValueError:
            raise DateError("Date invalide : « {} ».".format(m.group(0))) from None
        cut(m)

    # Durée glissée dans la date (« vendredi 18 h pendant une heure »)
    m = re.search(r"\b(?:pendant|durant|pour|duree de|duree)\s+(?P<d>{})".format(_DURATION), s)
    if m:
        w.duration = parse_duration(m["d"])
        cut(m)

    # « toute la journée »
    m = re.search(r"\b(?:toute la journee|toute la jour|la journee entiere|journee entiere|journee complete)\b", s)
    if m:
        w.all_day, found = True, True
        cut(m)

    # Dans N minutes / heures (instant précis) ; dans N jours / semaines / mois
    m = re.search(r"\bdans (\d+) ?(minutes?|min|mn|heures?|h(?![a-z]))(?: (?:et )?(\d+|demie?))?\b", s)
    if m:
        minutes = int(m[1]) * (60 if m[2].startswith("h") else 1)
        if m[3]:
            minutes += 30 if m[3].startswith("demi") else int(m[3])
        moment = current + dt.timedelta(minutes=minutes)
        day(moment.date())
        w.start = moment.time().replace(second=0, microsecond=0)
        cut(m)
    m = re.search(r"\bdans (\d+) (jours?|semaines?|mois)\b", s)
    if m:
        count = int(m[1])
        if m[2].startswith("jour"):
            day(today + dt.timedelta(days=count))
        elif m[2].startswith("semaine"):
            day(today + dt.timedelta(weeks=count))
        else:
            month = today.month - 1 + count
            day(_date(min(today.day, 28), month % 12 + 1, today.year + month // 12, today))
        cut(m)

    # Heures (« 18 h », « de 14 h à 16 h », « jusqu'à 19 h », « midi et demi », « 6 h du soir »)
    times = list(_TIME.finditer(s))
    if times:
        first_time = _time_of(times[0])
        before = s[:times[0].start()].rstrip()
        if len(times) >= 2 and re.search(r"(?:\ba\b|\bau\b|\bet\b|jusqu'a|jusqu'au|-)",
                                         s[times[0].end():times[1].start()]):
            w.start, w.end = first_time, _time_of(times[1])
        elif before.endswith(("jusqu'a", "jusqu'au")):
            w.end = first_time
        elif w.start is None:
            w.start = first_time
        for t in times[:2]:
            cut(t)
        found = True

    # Du 12 au 15 novembre
    m = re.search(r"\bdu (\d{{1,2}}|1er) ?(?:er )?(?:({0}) )?au (\d{{1,2}}) ({0})(?: (\d{{4}}))?\b".format(_MONTH), s)
    if m:
        last = _date(int(m[3]), _MONTHS.index(m[4]) + 1, _year(m[5]), today)
        first_month = _MONTHS.index(m[2]) + 1 if m[2] else last.month
        first = _date(1 if m[1] == "1er" else int(m[1]), first_month, last.year, today)
        if first > last:
            first = first.replace(year=first.year - 1)
        day(first, last)
        cut(m)

    # Le 12 novembre (2026), le 1er décembre, le 12/11, le 12
    m = re.search(r"\b(\d{{1,2}}|1er|premier) ?(?:er )?({0})(?: (\d{{4}}))?\b".format(_MONTH), s)
    if m:
        number = 1 if m[1] in ("1er", "premier") else int(m[1])
        day(_date(number, _MONTHS.index(m[2]) + 1, _year(m[3]), today))
        cut(m)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b", s)
    if m:
        if not 1 <= int(m[2]) <= 12:
            raise DateError("Date invalide : « {} ».".format(m.group(0)))
        day(_date(int(m[1]), int(m[2]), _year(m[3]), today))
        cut(m)
    m = re.search(r"\b(?:le|du|au|ce) (\d{1,2}|1er|premier)\b", s)
    if m:
        number = 1 if m[1] in ("1er", "premier") else int(m[1])
        month, year = today.month, today.year
        if number < today.day:
            month, year = (1, year + 1) if month == 12 else (month + 1, year)
        try:
            day(dt.date(year, month, number))
        except ValueError:
            raise DateError("Il n'y a pas de {} ce mois-là.".format(number)) from None
        cut(m)

    # Mots du jour
    for pattern, offset in ((r"\bavant[- ]hier\b", -2), (r"\bapres[- ]demain\b", 2), (r"\bhier\b", -1),
                            (r"\bdemain\b", 1), (r"\baujourd'hui\b|\bce jour\b", 0)):
        m = re.search(pattern, s)
        if m:
            day(today + dt.timedelta(days=offset))
            cut(m)

    # Jours de la semaine
    m = re.search(r"\b(?:(?:ce|le) )?({})(?: (prochain|d'apres|en huit|en 8|dernier|qui vient|de la semaine prochaine"
                  r"|de la semaine derniere|de cette semaine))?\b".format("|".join(DAYS)), s)
    if m:
        weekday, mod = DAYS.index(m[1]), m[2]
        if mod in ("prochain", "d'apres"):
            target = _next_weekday(today, weekday, strictly_after=True)
        elif mod in ("en huit", "en 8"):  # « jeudi en huit » dit un jeudi : jeudi prochain
            target = _next_weekday(today, weekday) + dt.timedelta(days=7)
        elif mod == "de la semaine prochaine":
            target = _monday(today) + dt.timedelta(days=7 + weekday)
        elif mod == "de cette semaine":
            target = _monday(today) + dt.timedelta(days=weekday)
        elif mod == "de la semaine derniere":
            target = _monday(today) - dt.timedelta(days=7 - weekday)
        elif mod == "dernier":
            target = today - dt.timedelta(days=(today.weekday() - weekday) % 7 or 7)
        else:  # « jeudi » : aujourd'hui compris, sauf si l'heure dite est passée
            target = _next_weekday(today, weekday)
            if target == today and w.start and at(today, w.start) < current:
                target += dt.timedelta(days=7)
        day(target)
        cut(m)

    # Périodes
    sunday = _monday(today) + dt.timedelta(days=6)
    periods = (
        (r"\b(?:la |cette )?semaine prochaine\b", lambda: (_monday(today) + dt.timedelta(days=7),
                                                            _monday(today) + dt.timedelta(days=13))),
        (r"\b(?:la )?semaine derniere\b", lambda: (_monday(today) - dt.timedelta(days=7),
                                                   _monday(today) - dt.timedelta(days=1))),
        (r"\bcette semaine\b|\bla semaine\b|\bsemaine\b", lambda: (today, sunday)),
        (r"\b(?:le )?week-?end prochain\b", lambda: _weekend(today, after=True)),
        (r"\b(?:ce |le )?week-?end\b", lambda: _weekend(today)),
        (r"\b(?:le )?mois prochain\b", lambda: _month(today, 1)),
        (r"\bce mois(?:-ci| ci)?\b|\ble mois\b", lambda: (today, _month(today, 0)[1])),
        (r"\b(?:les|dans les) (\d+) prochains jours\b", None),
    )
    for pattern, bounds in periods:
        m = re.search(pattern, s)
        if m:
            if bounds is None:
                day(today, today + dt.timedelta(days=max(int(m[1]), 1) - 1))
            else:
                day(*bounds())
            cut(m)
            break

    # Moments de la journée (« ce soir », « demain matin »)
    m = re.search(r"\b(?:ce |cet |cette )?(matinee|matin|apres-midi|apres midi|aprem|soiree|soir|nuit)\b", s)
    if m:
        name = "apres-midi" if m[1] == "apres midi" else m[1]
        w.part = _PARTS[name]
        if re.match(r"\s*(?:ce|cet|cette) ", m.group(0)):
            day(today)
        if w.start and name in ("apres-midi", "aprem", "soir", "soiree", "nuit") and w.start.hour < 12:
            w.start = w.start.replace(hour=w.start.hour + 12)  # « demain soir à 8 h » : 20 h
            if w.end and w.end.hour < 12:
                w.end = w.end.replace(hour=w.end.hour + 12)
        cut(m)
        found = True

    if not found:
        raise DateError("Je ne comprends pas la date « {} ».".format(text.strip()))
    if not w.has_day and w.start and not w.all_day:  # une heure seule : sa prochaine occurrence
        w.has_day = False
        if at(today, w.start) < current - dt.timedelta(minutes=1):
            w.first = w.last = today + dt.timedelta(days=1)
    return w


def _weekend(today: dt.date, after: bool = False) -> tuple[dt.date, dt.date]:
    if today.weekday() >= 5 and not after:
        return today, _monday(today) + dt.timedelta(days=6)
    saturday = _next_weekday(today, 5, strictly_after=today.weekday() >= 5)
    return saturday, saturday + dt.timedelta(days=1)


def _month(today: dt.date, offset: int) -> tuple[dt.date, dt.date]:
    month = today.month - 1 + offset
    first = dt.date(today.year + month // 12, month % 12 + 1, 1)
    following = dt.date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first, following - dt.timedelta(days=1)


# ---- Phrases ----

def day_name(day: dt.date, today: dt.date | None = None) -> str:
    """« vendredi 6 novembre » (« 1er », année si ce n'est pas l'année en cours)."""
    today = today or now().date()
    text = "{} {} {}".format(DAYS[day.weekday()], "1er" if day.day == 1 else day.day, MONTH_NAMES[day.month - 1])
    return text + (" {}".format(day.year) if day.year != today.year else "")


def time_name(time: dt.time) -> str:
    """« 18 h », « 18 h 30 », « minuit »."""
    if time.hour == 0 and time.minute == 0:
        return "minuit"
    return "{} h".format(time.hour) + (" {:02d}".format(time.minute) if time.minute else "")


def span(start: dt.datetime, end: dt.datetime, all_day: bool = False, today: dt.date | None = None) -> str:
    """« vendredi 6 novembre de 18 h à 19 h », « samedi 7 novembre, toute la journée »."""
    if all_day:
        last = (end - dt.timedelta(days=1)).date()
        if last <= start.date():
            return "{}, toute la journée".format(day_name(start.date(), today))
        return "du {} au {}".format(day_name(start.date(), today), day_name(last, today))
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    if start.date() == end.date() or (end - start <= dt.timedelta(days=1) and end.time() == dt.time(0)):
        return "{} de {} à {}".format(day_name(start.date(), today), time_name(start.time()), time_name(end.time()))
    return "du {} à {} au {} à {}".format(day_name(start.date(), today), time_name(start.time()),
                                          day_name(end.date(), today), time_name(end.time()))
