"""Minuteurs et rappels : « mets un minuteur de 10 minutes », « rappelle-moi à 18 h d'appeler maman ».

Ils vivent tant que Jarvis tourne (pas de sauvegarde) : à l'heure dite, Jarvis le dit à voix haute.
"""

from __future__ import annotations

import datetime
import re
import threading
from dataclasses import dataclass
from typing import Callable

from jarvis.tools import Param, Result, Tool, Toolbox, norm

_MAX_DELAY = datetime.timedelta(hours=24)


@dataclass
class _Timer:
    due: datetime.datetime
    label: str
    thread: threading.Timer


def _parse_clock(text: str, now: datetime.datetime) -> datetime.datetime | None:
    """« 18:30 », « 18h30 », « 18 h », « 7 h 05 » -> prochaine occurrence de cette heure."""
    found = re.fullmatch(r"(\d{1,2})\s*(?:h|:|heures?)?\s*(\d{2})?", norm(text).strip())
    if not found or int(found[1]) > 23 or int(found[2] or 0) > 59:
        return None
    due = now.replace(hour=int(found[1]), minute=int(found[2] or 0), second=0, microsecond=0)
    return due if due > now else due + datetime.timedelta(days=1)


def _duration(delta: datetime.timedelta) -> str:
    seconds = round(delta.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [(hours, "heure"), (minutes, "minute"), (seconds, "seconde")]
    return " ".join("{} {}{}".format(n, unit, "s" if n > 1 else "") for n, unit in parts if n) or "0 seconde"


class Timers:
    def __init__(self, notify: Callable[[str], None]) -> None:
        self._notify = notify   # dit le message à voix haute (appelé depuis un autre thread)
        self._timers: list[_Timer] = []
        self._lock = threading.Lock()

    def set(self, minutes: float = 0, at: str = "", label: str = "") -> Result:
        now = datetime.datetime.now()
        if at:
            due = _parse_clock(at, now)
            if due is None:
                return Result(False, "Heure invalide : « {} ». Donne-la comme 18:30.".format(at))
        elif minutes > 0:
            due = now + datetime.timedelta(minutes=minutes)
        else:
            return Result(False, "Il me faut une durée (minutes) ou une heure (at).")
        if due - now > _MAX_DELAY:
            return Result(False, "Je ne règle pas de rappel à plus de 24 heures.")
        label = label.strip()
        entry = _Timer(due, label, threading.Timer((due - now).total_seconds(), lambda: self._ring(entry)))
        entry.thread.daemon = True
        with self._lock:
            self._timers.append(entry)
        entry.thread.start()
        when = "à {} h {:02d}".format(due.hour, due.minute) if at else "dans " + _duration(due - now)
        return Result(True, "C'est réglé : je te {} {}.".format(
            "rappelle « {} »".format(label) if label else "préviens", when))

    def _ring(self, entry: _Timer) -> None:
        with self._lock:
            if entry not in self._timers:  # annulé entre-temps
                return
            self._timers.remove(entry)
        self._notify("Rappel : {}.".format(entry.label) if entry.label else "Le minuteur est terminé.")

    def list(self) -> Result:
        with self._lock:
            timers = sorted(self._timers, key=lambda t: t.due)
        if not timers:
            return Result(True, "Aucun minuteur ni rappel en cours.")
        now = datetime.datetime.now()
        return Result(True, "En cours :\n" + "\n".join(
            "- {} : à {} h {:02d} (dans {})".format(t.label or "minuteur", t.due.hour, t.due.minute,
                                                     _duration(t.due - now)) for t in timers))

    def cancel(self, label: str = "") -> Result:
        with self._lock:
            targets = [t for t in self._timers if not label or norm(label) in norm(t.label or "minuteur")]
            for t in targets:
                t.thread.cancel()
                self._timers.remove(t)
        if not targets:
            return Result(False, "Aucun minuteur ou rappel à annuler{}.".format(" pour « {} »".format(label) if label else ""))
        return Result(True, "J'ai annulé {} minuteur{}.".format(len(targets), "s" if len(targets) > 1 else ""))


def register(box: Toolbox, timers: Timers) -> None:
    box.add(Tool("set_timer",
                 "Règle un minuteur ou un rappel dit à voix haute le moment venu (« minuteur de 10 minutes », "
                 "« rappelle-moi dans 20 minutes de sortir le linge », « rappelle-moi à 18 h d'appeler maman »).",
                 timers.set, {
                     "minutes": Param("durée en minutes (30 secondes = 0.5, 1 h 30 = 90)", type="number",
                                      required=False),
                     "at": Param("heure précise au format HH:MM, à la place de minutes", required=False),
                     "label": Param("ce qu'il faut rappeler, vide pour un simple minuteur", required=False),
                 }))
    box.add(Tool("list_timers", "Liste les minuteurs et rappels en cours.", timers.list, reads=True))
    box.add(Tool("cancel_timer", "Annule un minuteur ou un rappel (tous si aucun nom).", timers.cancel, {
        "label": Param("rappel à annuler, vide pour tout annuler", required=False),
    }))
