"""Secrets chiffrés par DPAPI (Windows) : jetons et identifiants OAuth.

DPAPI chiffre avec une clé liée au compte Windows de l'utilisateur : le fichier ne se déchiffre que par ce
compte, sur ce PC. Copié ailleurs (sauvegarde, autre utilisateur), il est illisible. Aucune dépendance :
crypt32.dll est appelée par ctypes.

Les secrets vivent dans %LOCALAPPDATA%\\Jarvis, hors du dossier du projet : les sauvegardes du projet ne
les emportent pas.
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

_ENTROPY = b"Jarvis secret store v1"  # complément de clé : un autre programme du même compte doit le connaître
_UI_FORBIDDEN = 0x01                   # CRYPTPROTECT_UI_FORBIDDEN : jamais de fenêtre


class SecretError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _api():
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    for name in ("CryptProtectData", "CryptUnprotectData"):
        function = getattr(crypt32, name)
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data: bytes) -> tuple[_Blob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer  # buffer : garder en vie


def _call(name: str, data: bytes) -> bytes:
    crypt32, kernel32 = _api()
    source, keep = _blob(data)
    entropy, keep_entropy = _blob(_ENTROPY)
    out = _Blob()
    if not getattr(crypt32, name)(ctypes.byref(source), None, ctypes.byref(entropy), None, None, _UI_FORBIDDEN,
                                  ctypes.byref(out)):
        raise SecretError("{} a échoué : {}".format(name, ctypes.FormatError()))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    return _call("CryptProtectData", data)


def unprotect(data: bytes) -> bytes:
    return _call("CryptUnprotectData", data)


def store_dir() -> Path:
    """%LOCALAPPDATA%\\Jarvis (créé au besoin)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    folder = Path(base) / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save(path: Path, value: Any) -> None:
    """Écrit `value` (JSON) chiffré. Écriture atomique : un arrêt brutal ne laisse pas de fichier à moitié écrit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(protect(json.dumps(value).encode("utf-8")))
    os.replace(temporary, path)


def load(path: Path) -> Any | None:
    """Valeur déchiffrée, ou None si le fichier n'existe pas. SecretError s'il est illisible (autre compte, abîmé)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(unprotect(path.read_bytes()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SecretError("secret illisible : {}".format(path.name)) from exc
