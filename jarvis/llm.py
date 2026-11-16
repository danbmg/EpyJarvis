"""Client minimal pour l'API HTTP d'Ollama (bibliothèque standard uniquement)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("jarvis.llm")


class OllamaClient:
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434",
                 timeout: float = 60, keep_alive: str = "30m", num_ctx: int = 16384,
                 think: bool | None = None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx  # le prompt, les outils et les fenêtres lues dépassent les 4096 par défaut
        self.think = think      # False : coupe la « réflexion » des modèles qui en ont (Qwen3...), trop lente à l'oral

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> dict[str, Any]:
        """Renvoie le message de l'assistant : {"content": ..., "tool_calls": [...]}."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        if tools:
            payload["tools"] = tools
        if self.think is not None:
            payload["think"] = self.think
        return self._request("/api/chat", payload).get("message", {})

    def check(self) -> str | None:
        """Renvoie un message d'erreur si Ollama ou le modèle est indisponible, sinon None."""
        try:
            models = [m.get("name", "") for m in self._request("/api/tags").get("models", [])]
        except ConnectionError:
            return "Ollama ne répond pas sur {}. Lancez l'application Ollama.".format(self.base_url)
        if not any(m == self.model or m.startswith(self.model + ":") for m in models):
            return "Modèle '{0}' absent d'Ollama. Lancez : ollama pull {0}".format(self.model)
        return None

    def is_up(self) -> bool:
        """Ollama répond-il ? (sans écrire d'erreur dans le journal : sert à attendre son démarrage)"""
        try:
            with urllib.request.urlopen(self.base_url + "/api/version", timeout=2):
                return True
        except urllib.error.HTTPError:
            return True  # il répond, même par une erreur
        except (urllib.error.URLError, OSError):
            return False

    def start_server(self, wait: float = 20.0) -> bool:
        """Lance Ollama en arrière-plan (« ollama serve », sans fenêtre) s'il ne répond pas, puis attend qu'il
        réponde. Seulement pour un Ollama local. Renvoie True s'il répond."""
        if self.is_up():
            return True
        if urlparse(self.base_url).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        exe = _ollama_exe()
        if not exe:
            log.warning("Ollama introuvable : impossible de le lancer.")
            return False
        log.info("Lancement d'Ollama : %s serve", exe)
        subprocess.Popen([exe, "serve"], creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self.is_up():
                return True
        log.warning("Ollama ne répond toujours pas après %.0f s.", wait)
        return False

    def warm_up(self) -> None:
        """Charge le modèle en mémoire (le premier chargement peut prendre ~20 s)."""
        self._request("/api/chat", {"model": self.model, "messages": [], "keep_alive": self.keep_alive,
                                    "options": {"num_ctx": self.num_ctx}}, timeout=180)

    def unload(self) -> None:
        """Libère la mémoire de la carte graphique (veille de Jarvis)."""
        self._request("/api/chat", {"model": self.model, "messages": [], "keep_alive": 0})

    def _request(self, endpoint: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base_url + endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:  # Ollama répond, mais refuse la requête
            detail = exc.read().decode("utf-8", errors="replace")
            log.error("Ollama a refusé la requête (%s) : %s", endpoint, detail)
            raise RuntimeError("Ollama a renvoyé une erreur : {}".format(detail)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            log.error("Requête Ollama échouée (%s) : %s", endpoint, exc)
            raise ConnectionError(str(exc)) from exc


def _ollama_exe() -> str | None:
    """ollama.exe : dans le PATH, sinon à l'emplacement d'installation par défaut."""
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return shutil.which("ollama") or (str(default) if default.is_file() else None)
