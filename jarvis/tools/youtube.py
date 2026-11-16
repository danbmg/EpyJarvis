"""Contrôle de la vidéo YouTube affichée : pouce, plein écran, avancer, reculer, sous-titres.

Les raccourcis clavier de YouTube (f, l, j, c) sont envoyés à l'onglet affiché ; le pouce n'a pas de
raccourci, il est cliqué par UI Automation (bouton « J'aime cette vidéo… »).
"""

from __future__ import annotations

import re
import time

from jarvis.tools import Param, Result, Tool, Toolbox, norm
from jarvis.tools import windows as win
from jarvis.tools.desktop import press
from jarvis.tools.ui import _safe_walk

# « J'aime cette vidéo avec 1 234 autres personnes » ; pas « Je n'aime pas », ni le pouce d'un commentaire.
_LIKE = re.compile(r"^(?:j'aime cette video|like this video|j'aime$|like$)")
_MAX_NODES = 8000


def _youtube_window() -> win.Window | None:
    """Fenêtre de navigateur dont l'onglet affiché est YouTube (le titre suit l'onglet affiché)."""
    return next((w for w in win.list_windows()
                 if w.process in win.BROWSER_PROCESSES and "youtube" in w.title.lower()), None)


def _like(window: win.Window) -> Result:
    import uiautomation as auto

    root = auto.ControlFromHandle(window.hwnd)
    for attempt in range(2):  # Chromium n'expose sa page qu'après un premier accès
        count = 0
        for count, (control, _) in enumerate(_safe_walk(root)):
            if count > _MAX_NODES:
                break
            try:
                if control.ControlTypeName != "ButtonControl" or not _LIKE.match(
                        norm(control.Name or "").replace("’", "'")):
                    continue
                toggle = control.GetPattern(auto.PatternId.TogglePattern)
                if toggle and toggle.ToggleState == auto.ToggleState.On:
                    return Result(True, "Tu avais déjà mis un pouce à cette vidéo.")
                invoke = control.GetPattern(auto.PatternId.InvokePattern)
                invoke.Invoke() if invoke else control.Click(simulateMove=False)
                return Result(True, "J'ai mis un pouce à la vidéo.")
            except Exception:
                continue
        if count > 50:
            break
        time.sleep(1.0)
    return Result(False, "Je ne trouve pas le bouton « J'aime » de la vidéo (il faut être connecté à YouTube "
                         "et que la vidéo soit ouverte).")


def youtube_control(action: str, seconds: int = 10) -> Result:
    window = _youtube_window()
    if window is None:
        return Result(False, "Aucune vidéo YouTube n'est affichée : l'onglet YouTube doit être celui affiché "
                             "dans le navigateur.")
    if action == "like":
        return _like(window)
    win.focus(window.hwnd)
    time.sleep(0.3)
    if action in ("forward", "back"):
        presses = max(1, min(round(seconds / 10), 30))  # l et j : 10 secondes par appui
        press(ord("L" if action == "forward" else "J"), times=presses)
        return Result(True, "{} la vidéo de {} secondes.".format(
            "J'avance" if action == "forward" else "Je recule", presses * 10))
    key, message = {"fullscreen": ("F", "Plein écran activé ou quitté."),
                    "subtitles": ("C", "Sous-titres activés ou coupés."),
                    "theater": ("T", "Mode cinéma activé ou quitté.")}[action]
    press(ord(key))
    return Result(True, message)


def register(box: Toolbox) -> None:
    box.add(Tool("youtube_control",
                 "Agit sur la vidéo YouTube affichée : pouce / « j'aime » (like), plein écran, avancer ou "
                 "reculer, sous-titres, mode cinéma. Pour pause et vidéo suivante : media.", youtube_control, {
                     "action": Param("action", enum=["like", "fullscreen", "forward", "back", "subtitles", "theater"],
                                     aliases={"pouce": "like", "j'aime": "like", "plein ecran": "fullscreen",
                                              "avancer": "forward", "reculer": "back", "rewind": "back",
                                              "sous-titres": "subtitles", "cinema": "theater"}),
                     "seconds": Param("pour avancer ou reculer : nombre de secondes (10 par défaut)",
                                      type="integer", required=False),
                 }))
