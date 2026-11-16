"""Jarvis comme application de bureau : raccourcis « Jarvis » sur le Bureau et dans le menu Démarrer.

Double-clic -> la fenêtre s'ouvre (sans console), Jarvis dit « Jarvis Activated » et écoute. Les raccourcis
portent l'identité Windows de Jarvis (APP_ID) : la fenêtre ouverte se range sous l'icône épinglée à la barre
des tâches, et « Épingler à la barre des tâches » épingle bien Jarvis, pas Python.

Usage : python -m jarvis.shortcut install | remove | status
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from jarvis import APP_ID
from jarvis.config import ROOT
from jarvis.ui.theme import ICON

NAME = "Jarvis"
_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"   # FOLDERID_Desktop
_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"  # FOLDERID_Programs (menu Démarrer)
_PKEY_APP_ID = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 5)  # PKEY_AppUserModel_ID
_VT_LPWSTR = 31


def _known_folder(folder_id: str) -> Path:
    from comtypes import GUID

    path = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    if shell32.SHGetKnownFolderPath(ctypes.byref(GUID(folder_id)), 0, None, ctypes.byref(path)):
        raise OSError("Dossier Windows introuvable : {}".format(folder_id))
    try:
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def shortcut_paths() -> list[Path]:
    return [_known_folder(folder) / (NAME + ".lnk") for folder in (_DESKTOP, _PROGRAMS)]


def _pythonw() -> Path:
    """pythonw.exe de l'environnement : pas de console derrière la fenêtre."""
    return Path(sys.executable).with_name("pythonw.exe")


def _property_store():
    """Interface IPropertyStore (absente de comtypes), pour écrire l'identité de l'appli dans le raccourci."""
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):  # réduit au cas VT_LPWSTR
        _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                    ("value", ctypes.c_wchar_p), ("padding", ctypes.c_void_p)]

    class IPropertyStore(IUnknown):
        _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], ctypes.POINTER(wintypes.DWORD), "count")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], wintypes.DWORD, "index"),
                      (["out"], ctypes.POINTER(PROPERTYKEY), "key")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], ctypes.POINTER(PROPERTYKEY), "key"),
                      (["out"], ctypes.POINTER(PROPVARIANT), "value")),
            COMMETHOD([], HRESULT, "SetValue", (["in"], ctypes.POINTER(PROPERTYKEY), "key"),
                      (["in"], ctypes.POINTER(PROPVARIANT), "value")),
            COMMETHOD([], HRESULT, "Commit"),
        ]

    return IPropertyStore, PROPERTYKEY, PROPVARIANT


def _create(path: Path) -> None:
    from comtypes import GUID, CoCreateInstance
    from comtypes.persist import IPersistFile
    from comtypes.shelllink import IShellLinkW, ShellLink

    link = CoCreateInstance(ShellLink._reg_clsid_, IShellLinkW)
    link.SetPath(str(_pythonw()))
    link.SetArguments('"{}"'.format(ROOT / "main.py"))
    link.SetWorkingDirectory(str(ROOT))
    link.SetIconLocation(str(ICON), 0)
    link.SetDescription("Jarvis, assistant vocal local")

    IPropertyStore, PROPERTYKEY, PROPVARIANT = _property_store()
    store = link.QueryInterface(IPropertyStore)
    key = PROPERTYKEY(GUID(_PKEY_APP_ID[0]), _PKEY_APP_ID[1])
    value = PROPVARIANT(vt=_VT_LPWSTR, value=APP_ID)
    store.SetValue(ctypes.byref(key), ctypes.byref(value))
    store.Commit()

    path.parent.mkdir(parents=True, exist_ok=True)
    link.QueryInterface(IPersistFile).Save(str(path), True)


def install() -> None:
    if not _pythonw().is_file():
        sys.exit("pythonw.exe introuvable à côté de {} : lance ce script avec le Python du projet.".format(
            sys.executable))
    for path in shortcut_paths():
        _create(path)
        print("Raccourci créé :", path)
    print("Double-clique sur « Jarvis » (Bureau ou menu Démarrer). Pour l'épingler : clic droit sur le raccourci "
          "du menu Démarrer > Épingler à la barre des tâches.")


def remove() -> None:
    for path in shortcut_paths():
        if path.exists():
            path.unlink()
            print("Raccourci supprimé :", path)


def status() -> None:
    for path in shortcut_paths():
        print(("Présent : " if path.exists() else "Absent :  ") + str(path))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raccourcis de l'application Jarvis")
    parser.add_argument("action", choices=["install", "remove", "status"])
    {"install": install, "remove": remove, "status": status}[parser.parse_args().action]()
