"""Fenêtres ouvertes : liste, premier plan, catégories de processus sensibles."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

import psutil

from jarvis.tools import best_match, norm

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

_SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"}

# Fenêtres où Jarvis peut lire mais jamais cliquer ni écrire : consoles (une commande tapée
# s'exécuterait), réglages système, registre, gestionnaire des tâches, élévation UAC, recherche Windows.
SENSITIVE_PROCESSES = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "openconsole.exe", "conhost.exe",
    "systemsettings.exe", "control.exe", "mmc.exe", "regedit.exe", "taskmgr.exe", "rundll32.exe",
    "consent.exe", "credentialuibroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "msiexec.exe", "securityhealthsystray.exe", "sechealthui.exe",
}

# Applications de messagerie : valider un texte (Entrée) envoie un message -> confirmation.
MESSAGING_PROCESSES = {
    "whatsapp.exe", "whatsapp.root.exe", "discord.exe", "telegram.exe", "signal.exe", "slack.exe",
    "teams.exe", "ms-teams.exe", "outlook.exe", "olk.exe", "thunderbird.exe", "messenger.exe",
}

# Navigateurs : « ferme cet onglet » ferme l'onglet affiché (Ctrl+W), pas tout le navigateur.
BROWSER_PROCESSES = {
    "brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe", "arc.exe",
}

# Processus qu'on ne force jamais à se fermer.
SYSTEM_PROCESSES = {
    "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "services.exe",
    "svchost.exe", "smss.exe", "wininit.exe", "system", "fontdrvhost.exe", "sihost.exe",
    "applicationframehost.exe",  # cadre de toutes les applis du Store
}


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    process: str  # nom de l'exécutable en minuscules, ex. « spotify.exe »

    @property
    def app(self) -> str:
        return self.process.removesuffix(".exe")


def list_windows() -> list[Window]:
    """Fenêtres visibles de l'utilisateur, de la plus en avant à la plus en arrière (hors Jarvis)."""
    own_pid = os.getpid()
    windows: list[Window] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # 4 = GW_OWNER
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:                          # WS_EX_TOOLWINDOW
            return True
        cloaked = ctypes.c_int(0)  # fenêtres d'applis du Store suspendues, invisibles en réalité
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        length = user32.GetWindowTextLengthW(hwnd)
        if cloaked.value or not length:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in _SHELL_CLASSES:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = _pid(hwnd)
        if pid == own_pid:
            return True
        process = _process_name(pid)
        if process == UWP_HOST:  # appli du Store : le vrai processus possède une fenêtre enfant
            pid = _uwp_app_pid(hwnd, pid)
            process = _process_name(pid)
        windows.append(Window(hwnd, title.value, pid, process))
        return True

    user32.EnumWindows(_ENUM_PROC(callback), 0)
    return windows


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
UWP_HOST = "applicationframehost.exe"


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except psutil.Error:
        return ""


def _uwp_app_pid(hwnd: int, host_pid: int) -> int:
    """Processus de l'appli du Store affichée dans un cadre ApplicationFrameHost (sinon celui du cadre)."""
    found = []

    def callback(child, _):
        pid = _pid(child)
        if pid != host_pid:
            found.append(pid)
            return False
        return True

    user32.EnumChildWindows(hwnd, _ENUM_PROC(callback), 0)
    return found[0] if found else host_pid


def current_window() -> Window | None:
    """La fenêtre au premier plan ; si c'est Jarvis, celle juste derrière."""
    windows = list_windows()
    foreground = user32.GetForegroundWindow()
    return next((w for w in windows if w.hwnd == foreground), windows[0] if windows else None)


def find_window(spoken: str) -> Window | None:
    """Retrouve une fenêtre par nom d'application ou par titre (« spotify », « whatsapp »...)."""
    windows = list_windows()
    names: dict[str, Window] = {}
    for w in reversed(windows):  # la plus en avant gagne en cas d'égalité
        names[norm(w.app)] = w
        names[norm(w.title)] = w
    return best_match(spoken, names, cutoff=0.7)


def focus(hwnd: int) -> None:
    """Met une fenêtre au premier plan (Windows bloque ce changement sans ces précautions)."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    current_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(current_thread, foreground_thread, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(current_thread, foreground_thread, False)
    time.sleep(0.3)


def close(hwnd: int) -> None:
    """Demande polie de fermeture (comme la croix) : l'appli peut proposer d'enregistrer."""
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
