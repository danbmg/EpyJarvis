"""Mémoire longue durée : des paires sujet -> valeur stockées dans SQLite."""

from __future__ import annotations

import datetime
import difflib
import sqlite3
import threading
from pathlib import Path

from jarvis.tools import Param, Result, Tool, Toolbox

_CATEGORY = "facts"  # conservé pour rester compatible avec les bases existantes


class Memory:
    def __init__(self, db_path: Path) -> None:
        # Créée dans le thread de l'interface, utilisée dans le thread de travail.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS memory (category TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (category, key))")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _query(self, sql: str, *params: str) -> list[tuple]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            self._conn.commit()
            return rows

    def _find_key(self, key: str) -> str | None:
        """Clé exacte, sinon la plus proche (« wifi » retrouve « code wifi »)."""
        keys = [k for (k,) in self._query("SELECT key FROM memory WHERE category = ?", _CATEGORY)]
        key = key.lower().strip()
        if key in keys:
            return key
        close = difflib.get_close_matches(key, keys, n=1, cutoff=0.6)
        return close[0] if close else next((k for k in keys if key in k or k in key), None)

    def remember(self, key: str, value: str) -> Result:
        self._query("INSERT OR REPLACE INTO memory VALUES (?, ?, ?, ?)", _CATEGORY,
                    key.lower().strip(), value, datetime.datetime.now().isoformat())
        return Result(True, "C'est noté : {} = {}.".format(key, value))

    def recall(self, key: str) -> Result:
        found = self._find_key(key)
        if found is None:
            return Result(False, "Rien de retenu à propos de « {} ».".format(key))
        (value,), = self._query("SELECT value FROM memory WHERE category = ? AND key = ?", _CATEGORY, found)
        return Result(True, "{} : {}".format(found, value))

    def forget(self, key: str) -> Result:
        found = self._find_key(key)
        if found is None:
            return Result(False, "Je n'ai rien à oublier à propos de « {} ».".format(key))
        self._query("DELETE FROM memory WHERE category = ? AND key = ?", _CATEGORY, found)
        return Result(True, "J'ai oublié « {} ».".format(found))

    def facts(self, limit: int = 30) -> list[tuple[str, str]]:
        """Les informations retenues, les plus récentes d'abord (données au modèle dans son prompt)."""
        rows = self._query("SELECT key, value FROM memory WHERE category = ? ORDER BY updated_at DESC LIMIT {}"
                           .format(int(limit)), _CATEGORY)
        return [(k, v[:200]) for k, v in rows]

    def list_all(self) -> Result:
        rows = self._query("SELECT key, value FROM memory WHERE category = ? ORDER BY key", _CATEGORY)
        if not rows:
            return Result(True, "Je n'ai encore rien retenu.")
        return Result(True, "Ce que j'ai retenu :\n" + "\n".join("- {} : {}".format(k, v) for k, v in rows))


def register(box: Toolbox, memory: Memory) -> None:
    key = Param("sujet en un ou deux mots, en minuscules, sans article (ex. « prénom », « code wifi »)")
    box.add(Tool("remember", "Enregistre durablement une information que l'utilisateur demande de mémoriser "
                             "(« retiens que… », « souviens-toi que… », « note que… »). Sans cet outil, rien "
                             "n'est retenu. Pour corriger une valeur (« en fait c'est 5678 »), rappelle "
                             "remember avec la même clé : la nouvelle valeur remplace l'ancienne.",
                 memory.remember, {"key": key, "value": Param("l'information à retenir")}, private=True))
    box.add(Tool("recall", "Retrouve une information retenue (son prénom, un code, une date...).",
                 memory.recall, {"key": key}, reads=True, private=True))
    box.add(Tool("forget", "Oublie une information retenue, quand l'utilisateur demande de l'oublier (pas "
                           "pour la corriger : remember la remplace).", memory.forget, {"key": key}))
    box.add(Tool("list_memory", "Liste tout ce que Jarvis a retenu.", memory.list_all, reads=True, private=True))
