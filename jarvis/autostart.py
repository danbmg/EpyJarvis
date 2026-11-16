"""Démarrage automatique de Jarvis avec Windows (clé Run du registre utilisateur).

Usage : python -m jarvis.autostart enable | disable | status
"""

from __future__ import annotations

import argparse
import sys
import winreg
from pathlib import Path

from jarvis.config import ROOT

_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_NAME = "Jarvis"


def _command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # pas de console au démarrage
    return '"{}" "{}"'.format(pythonw, ROOT / "main.py")


def enable() -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _NAME, 0, winreg.REG_SZ, _command())
    print("Démarrage automatique activé :", _command())


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _NAME)
    except FileNotFoundError:
        pass
    print("Démarrage automatique désactivé.")


def status() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY) as key:
            print("Activé :", winreg.QueryValueEx(key, _NAME)[0])
    except FileNotFoundError:
        print("Désactivé.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Démarrage automatique de Jarvis")
    parser.add_argument("action", choices=["enable", "disable", "status"])
    {"enable": enable, "disable": disable, "status": status}[parser.parse_args().action]()
