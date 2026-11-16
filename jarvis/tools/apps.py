"""Applications : ouvrir n'importe quelle appli installée, fermer, passer d'une fenêtre à l'autre.

Les applis sont découvertes automatiquement (menu Démarrer). config.yaml ajoute des alias
et des modes de lancement particuliers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

import psutil

from jarvis.tools import ALLOW, Check, Param, Result, Tool, Toolbox, best_match, confirm, forbid, norm
from jarvis.tools import steam
from jarvis.tools import windows as win
from jarvis.tools.desktop import VK, press

log = logging.getLogger("jarvis.tools.apps")

# Raccourcis du menu Démarrer à ne jamais lancer : ils désinstallent ou modifient le système.
_EXCLUDED_START_APPS = re.compile(r"uninstall|d[ée]sinstall|remove|supprim|reset|r[ée]initialis", re.I)
# Sites demandés comme des applications (« ouvre YouTube ») : ouverts dans le navigateur s'ils ne sont
# pas installés.
_WEBSITES = {
    "youtube": ("YouTube", "https://www.youtube.com"), "twitch": ("Twitch", "https://www.twitch.tv"),
    "netflix": ("Netflix", "https://www.netflix.com"), "gmail": ("Gmail", "https://mail.google.com"),
    "google": ("Google", "https://www.google.com"), "google maps": ("Google Maps", "https://maps.google.com"),
    "facebook": ("Facebook", "https://www.facebook.com"), "instagram": ("Instagram", "https://www.instagram.com"),
    "twitter": ("X", "https://x.com"), "reddit": ("Reddit", "https://www.reddit.com"),
    "amazon": ("Amazon", "https://www.amazon.fr"), "leboncoin": ("Leboncoin", "https://www.leboncoin.fr"),
    "wikipedia": ("Wikipédia", "https://fr.wikipedia.org"), "chatgpt": ("ChatGPT", "https://chatgpt.com"),
    "github": ("GitHub", "https://github.com"), "linkedin": ("LinkedIn", "https://www.linkedin.com"),
    "prime video": ("Prime Video", "https://www.primevideo.com"),
    "disney plus": ("Disney+", "https://www.disneyplus.com"),
}


class AppCatalog:
    WINDOW_WAIT = 5.0  # secondes au plus pour voir apparaître la fenêtre d'une application lancée

    def __init__(self, config_apps: list[dict[str, Any]]) -> None:
        self._config = {a["name"].lower(): a for a in config_apps}
        self._names: dict[str, tuple[str, Any]] = {}
        for app in config_apps:
            for alias in [app["name"], app.get("label", ""), *app.get("aliases", [])]:
                if alias:
                    self._names[norm(alias)] = ("config", app)

    def load_start_menu(self) -> int:
        """Ajoute toutes les applis du menu Démarrer (~1 s). Renvoie leur nombre."""
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        apps = json.loads(out.stdout.decode("utf-8") or "[]")
        count = 0
        for app in apps if isinstance(apps, list) else [apps]:
            if not app.get("Name") or _EXCLUDED_START_APPS.search(app["Name"]):
                continue
            self._names.setdefault(norm(app["Name"]), ("start", app))
            count += 1
        log.info("%d applications trouvées dans le menu Démarrer.", count)
        return count

    def find(self, spoken: str) -> tuple[str, Any] | None:
        return best_match(spoken, self._names)

    def open(self, name: str) -> Result:
        found = self.find(name)
        if found is None or found[0] == "start":  # « ouvre Palworld » : un jeu Steam passe par Steam
            game = steam._find_installed(name, steam.installed_games())
            if game:
                return steam.steam_launch(game["name"])
        if found is None:
            spoken = norm(name.replace("+", " plus"))  # nom exact seulement : « regedit » n'est pas Reddit
            site = next((_WEBSITES[key] for key in sorted(_WEBSITES, key=len, reverse=True)  # « google maps »
                         if re.search(r"\b{}\b".format(key), spoken)), None)                 # avant « google »
            address = name.strip().lower()
            if not site and re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", address) \
                    and not address.endswith((".exe", ".msc", ".lnk", ".bat", ".cmd", ".ps1")):  # « lemonde.fr »
                site = (address, "https://" + address)
            if site:
                webbrowser.open(site[1])
                return Result(True, "J'ouvre {} dans le navigateur.".format(site[0]))
            return Result(False, "Je ne trouve pas l'application « {} » sur ce PC. Si c'est un site web, "
                                 "open_url l'ouvre dans le navigateur.".format(name))
        before = {w.hwnd for w in win.list_windows()}
        already_open = win.find_window(name)
        result = self._launch(*found)
        if result.ok:
            self._bring_to_front(before, already_open)
        return result

    def _bring_to_front(self, before: set[int], already_open: win.Window | None) -> None:
        """Attend la fenêtre de l'application lancée et la met au premier plan : l'étape suivante
        (ui_read, ui_type...) lit ainsi la bonne fenêtre."""
        deadline = time.monotonic() + (0.5 if already_open else self.WINDOW_WAIT)
        while time.monotonic() < deadline:
            time.sleep(0.25)
            new = [w for w in win.list_windows() if w.hwnd not in before]
            if new:
                win.focus(new[0].hwnd)
                return
        if already_open:
            win.focus(already_open.hwnd)

    def _launch(self, kind: str, app: Any) -> Result:
        if kind == "start":
            os.startfile("shell:AppsFolder\\" + app["AppID"])
            return Result(True, "J'ouvre {}.".format(app["Name"]))

        label = app.get("label", app["name"])
        target = os.path.expandvars(app.get("path", ""))
        if target.startswith("shell:") or target.lower().endswith((".lnk", ".msc")):  # .msc : console mmc
            os.startfile(target)
            return Result(True, "J'ouvre {}.".format(label))
        exe = target if target and Path(target).exists() else shutil.which(app.get("command", app["name"]))
        if not exe:
            return Result(False, "{} est introuvable. Vérifiez son chemin dans config.yaml.".format(label))
        subprocess.Popen([exe, *app.get("args", [])], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        return Result(True, "J'ouvre {}.".format(label))


def _target_windows(name: str) -> list[win.Window]:
    """Toutes les fenêtres de l'appli visée (par nom, ou celle au premier plan si nom vide)."""
    target = win.find_window(name) if name else win.current_window()
    if target is None or target.process == win.UWP_HOST:  # cadre partagé par plusieurs applis du Store
        return [target] if target else []
    return [w for w in win.list_windows() if w.process == target.process] or [target]


def close_app(name: str = "", force: bool = False) -> Result:
    targets = _target_windows(name)
    if not targets:
        return Result(False, "Je ne trouve pas de fenêtre « {} » ouverte.".format(name) if name
                      else "Aucune fenêtre à fermer.")
    process, label = targets[0].process, targets[0].app
    if force:
        killed = 0
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() == process:
                proc.kill()
                killed += 1
        return Result(True, "J'ai forcé la fermeture de {} ({} processus).".format(label, killed))

    for w in targets:
        win.close(w.hwnd)
    time.sleep(1.5)
    remaining = [w for w in win.list_windows() if w.process == process]
    if remaining:
        return Result(True, "J'ai demandé à {} de se fermer, mais une fenêtre est encore ouverte : "
                            "« {} ». Elle attend peut-être une réponse.".format(label, remaining[0].title))
    return Result(True, "J'ai fermé {}.".format(label))


def _guard_close(name: str = "", force: bool = False) -> Check:
    targets = _target_windows(name)
    if not targets or not force:
        return ALLOW
    if targets[0].process in win.SYSTEM_PROCESSES:
        return forbid("je ne force jamais la fermeture d'un processus système")
    return confirm("Je force la fermeture de {} ? Ce qui n'est pas enregistré sera perdu.".format(
        targets[0].app))


def close_tab() -> Result:
    """Ferme l'onglet affiché du navigateur le plus en avant (Ctrl+W)."""
    browser = next((w for w in win.list_windows() if w.process in win.BROWSER_PROCESSES), None)
    if browser is None:
        return Result(False, "Aucun navigateur n'est ouvert.")
    win.focus(browser.hwnd)
    press(VK["ctrl"], ord("W"))
    return Result(True, "J'ai fermé l'onglet ouvert dans {}.".format(browser.app))


def list_open_windows() -> Result:
    windows = win.list_windows()
    current = win.current_window()
    lines = ["- {} : « {} »{}".format(w.app, w.title, " (premier plan)" if current and w.hwnd == current.hwnd else "")
             for w in windows]
    return Result(True, "Fenêtres ouvertes :\n" + "\n".join(lines) if lines else "Aucune fenêtre ouverte.")


def focus_window(name: str) -> Result:
    target = win.find_window(name)
    if target is None:
        return Result(False, "Je ne trouve pas de fenêtre « {} ».".format(name))
    win.focus(target.hwnd)
    return Result(True, "Je passe sur {}.".format(target.app))


def register(box: Toolbox, catalog: AppCatalog) -> None:
    box.add(Tool("open_app", "Ouvre ou lance n'importe quelle application installée sur le PC.", catalog.open, {
        "name": Param("nom de l'application, tel que l'utilisateur le dit (ex. « discord », « paint »)"),
    }))
    box.add(Tool("close_app",
                 "Ferme une application entière (comme la croix de la fenêtre). Sans nom : ferme "
                 "l'application au premier plan (« ferme ça », « ferme cette application »). Pour un "
                 "onglet du navigateur : close_tab.", close_app, {
                     "name": Param("nom de l'application à fermer, vide pour celle au premier plan",
                                   required=False),
                     "force": Param("true uniquement si l'utilisateur demande de forcer la fermeture",
                                    type="boolean", required=False),
                 }, guard=_guard_close))
    box.add(Tool("close_tab", "Ferme l'onglet affiché du navigateur (« ferme cet onglet », « ferme la vidéo "
                              "YouTube »), sans fermer le navigateur.", close_tab))
    box.add(Tool("list_windows", "Liste les fenêtres ouvertes et celle au premier plan.", list_open_windows,
                 reads=True, untrusted=True))
    box.add(Tool("focus_window", "Met au premier plan la fenêtre d'une application déjà ouverte.",
                 focus_window, {"name": Param("nom de l'application ou titre de la fenêtre")}))
