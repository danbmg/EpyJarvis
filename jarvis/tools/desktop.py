"""Bureau Windows : son, touches média, capture d'écran, presse-papiers.

Le son et les médias passent par les touches multimédia virtuelles : elles agissent
sur le lecteur actif (Spotify, YouTube, VLC...) sans dépendre d'une application.
Quand Spotify joue, pause et changement de morceau passent par son API (voir Media).
"""

from __future__ import annotations

import ctypes
import datetime
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from jarvis.tools import Param, Result, Tool, Toolbox

log = logging.getLogger("jarvis.tools.desktop")

_KEYUP = 0x0002
_VOLUME_STEP = 5  # une touche = 2 %, donc « monte le son » = +10 %
_SCREENSHOT_DIR = Path.home() / "Pictures" / "Jarvis"

VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "space": 0x20, "backspace": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "delete": 0x2E,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "next": 0xB0, "previous": 0xB1, "play_pause": 0xB3,
    **{"f{}".format(i): 0x6F + i for i in range(1, 13)},
}
KEY_ALIASES = {"control": "ctrl", "entrée": "enter", "entree": "enter", "return": "enter",
               "echap": "esc", "escape": "esc", "espace": "space", "suppr": "delete",
               "del": "delete", "windows": "win", "haut": "up", "bas": "down",
               "gauche": "left", "droite": "right", "pgup": "pageup", "pgdn": "pagedown"}


def key_codes(combo: str) -> list[int]:
    """« ctrl+shift+t » -> codes de touches virtuelles. Lève ValueError si une touche est inconnue."""
    codes = []
    for part in combo.lower().replace(" ", "").split("+"):
        part = KEY_ALIASES.get(part, part)
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ValueError("touche inconnue : {}".format(part))
    return codes


def press(*keys: int, times: int = 1) -> None:
    """Appuie sur une combinaison de touches (maintenues dans l'ordre, relâchées à l'envers)."""
    user32 = ctypes.windll.user32
    for _ in range(times):
        for vk in keys:
            user32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(keys):
            user32.keybd_event(vk, 0, _KEYUP, 0)


_VOLUME_WORDS = {"max": "100", "maximum": "100", "fond": "100", "a fond": "100", "à fond": "100",
                 "min": "0", "zero": "0", "zéro": "0", "coupe": "mute", "couper": "mute", "muet": "mute"}


def set_volume(level: str) -> Result:
    level = _VOLUME_WORDS.get(level.lower().rstrip(" %"), level.lower().rstrip(" %"))
    if level == "up":
        press(VK["volume_up"], times=_VOLUME_STEP)
        return Result(True, "Volume augmenté.")
    if level == "down":
        press(VK["volume_down"], times=_VOLUME_STEP)
        return Result(True, "Volume baissé.")
    if level in ("mute", "unmute"):
        press(VK["volume_mute"])
        return Result(True, "Son coupé ou rétabli.")
    if level.isdigit():
        value = min(100, int(level))
        press(VK["volume_down"], times=50)          # remet à 0
        press(VK["volume_up"], times=round(value / 2))
        return Result(True, "Volume réglé à {} %.".format(value))
    return Result(False, "Niveau de volume invalide : {}.".format(level))


class Media:
    """Pause, lecture, suivant, précédent.

    Si Spotify joue, même sur un autre appareil (que les touches multimédia de ce PC n'atteignent pas),
    sa Web API agit et la pause ne se transforme jamais en lecture. Sinon, les touches multimédia
    agissent sur le lecteur de ce PC (YouTube, VLC...). « Reprends » relance ce qui a été mis en pause.
    """

    def __init__(self, spotify: Any = None) -> None:
        self.spotify = spotify    # SpotifyClient, ou None si Spotify n'est pas configuré
        self._paused = ""         # « spotify » ou « keys » : ce que Jarvis a mis en pause en dernier

    def _spotify_state(self) -> dict | None:
        """État de lecture Spotify ({} si rien), ou None si l'API est indisponible."""
        if self.spotify is None:
            return None
        try:
            return self.spotify.api("GET", "/me/player")
        except Exception as exc:  # pas connecté, réseau...
            log.info("État Spotify indisponible : %s", exc)
            return None

    def run(self, command: str) -> Result:
        if command not in ("play_pause", "pause", "play", "next", "previous"):
            return Result(False, "Commande média inconnue : {}.".format(command))
        state = self._spotify_state()
        playing = bool(state and state.get("is_playing"))
        paused_spotify = bool(state and state.get("item") and not playing)
        if command == "play_pause":
            command = "pause" if playing else "play"
        try:
            if command == "pause" and playing:
                self.spotify.pause()
                self._paused = "spotify"
                return Result(True, "Spotify est en pause.")
            if command == "play" and paused_spotify and self._paused != "keys" and self.spotify.play().ok:
                self._paused = ""
                return Result(True, "Je relance Spotify.")
            if command in ("next", "previous") and (playing or paused_spotify and self._paused == "spotify"):
                self.spotify.api("POST", "/me/player/" + command)
                return Result(True, "Morceau suivant." if command == "next" else "Morceau précédent.")
        except Exception as exc:
            log.info("Commande Spotify échouée (%s) : touches multimédia", exc)
        press(VK["play_pause" if command in ("pause", "play") else command])
        self._paused = "keys" if command == "pause" else ""
        return Result(True, {"pause": "C'est en pause.", "play": "Lecture reprise.", "next": "Morceau suivant.",
                             "previous": "Morceau précédent."}[command])


def wait(seconds: int) -> Result:
    seconds = max(1, min(seconds, 30))
    time.sleep(seconds)
    return Result(True, "J'ai attendu {} secondes.".format(seconds))


def show_desktop() -> Result:
    press(VK["win"], ord("D"))
    return Result(True, "J'affiche le bureau.")


def screenshot() -> Result:
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _SCREENSHOT_DIR / datetime.datetime.now().strftime("capture_%Y%m%d_%H%M%S.png")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
         "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
         "$img = New-Object System.Drawing.Bitmap($b.Width, $b.Height); "
         "$g = [System.Drawing.Graphics]::FromImage($img); "
         "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
         "$img.Save('{}'); $g.Dispose(); $img.Dispose()".format(path)],
        capture_output=True, timeout=15, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return Result(True, "Capture enregistrée dans {}.".format(path))


def clipboard_read() -> Result:
    import uiautomation as auto
    text = auto.GetClipboardText() or ""
    if not text.strip():
        return Result(True, "Le presse-papiers est vide.")
    return Result(True, "Contenu du presse-papiers : " + text[:3000])


def clipboard_write(text: str) -> Result:
    import uiautomation as auto
    auto.SetClipboardText(text)
    return Result(True, "Copié dans le presse-papiers.")


def register(box: Toolbox, spotify: Any = None) -> None:
    box.add(Tool("volume", "Règle le son de l'ordinateur.", set_volume, {
        "level": Param("'up' (monter, plus fort), 'down' (baisser, moins fort), 'mute' (« coupe le son », "
                       "« remets le son »), ou un nombre de 0 à 100 (« à fond » = 100)"),
    }))
    box.add(Tool("media", "Pause, reprise, morceau suivant ou précédent de la musique ou vidéo en cours, "
                          "Spotify compris (« mets pause », « reprends », « chanson suivante »). Rien d'autre : "
                          "ni « j'aime », ni pouce (youtube_control). Ne donne aucune information : pour savoir "
                          "ce qui passe sur Spotify, spotify_now_playing.", Media(spotify).run, {
        "command": Param("action", enum=["pause", "play", "next", "previous", "play_pause"], aliases={
            "stop": "pause", "lecture": "play", "reprendre": "play", "resume": "play", "reprise": "play",
            "playpause": "play_pause", "play pause": "play_pause", "suivant": "next", "suivante": "next",
            "skip": "next", "precedent": "previous", "precedente": "previous", "prev": "previous",
            "back": "previous"}),
    }))
    box.add(Tool("wait", "Attend quelques secondes (30 au plus) entre deux actions, quand l'utilisateur le "
                         "demande (« attends 5 secondes et... »).", wait, {
        "seconds": Param("nombre de secondes", type="integer"),
    }, reads=True, acts=True))
    box.add(Tool("show_desktop", "Réduit toutes les fenêtres et affiche le bureau.", show_desktop))
    box.add(Tool("screenshot", "Fait une capture d'écran enregistrée dans Images\\Jarvis.", screenshot))
    box.add(Tool("clipboard_read", "Lit le texte du presse-papiers.", clipboard_read,
                 reads=True, untrusted=True))
    box.add(Tool("clipboard_write", "Copie un texte dans le presse-papiers.", clipboard_write, {
        "text": Param("texte à copier"),
    }, private=True))
