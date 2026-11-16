"""Chargement de config.yaml (à défaut config.example.yaml) et configuration des logs."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Accès aux valeurs de config.yaml en notation pointée : cfg.get("llm.model")."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path:
            path = Path(path)
        else:  # config.yaml est personnel (non versionné) : à défaut, le modèle du dépôt
            path = ROOT / "config.yaml"
            if not path.exists():
                path = ROOT / "config.example.yaml"
        with open(path, encoding="utf-8") as fh:
            self._data: dict[str, Any] = yaml.safe_load(fh) or {}

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in dotted_key.split("."):
            if not isinstance(node, dict) or node.get(key) is None:
                return default
            node = node[key]
        return node

    def path(self, dotted_key: str, default: str) -> Path:
        """Chemin lu dans la config, relatif au dossier du projet s'il n'est pas absolu."""
        return ROOT / Path(self.get(dotted_key, default))


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    logger = logging.getLogger("jarvis")
    logger.setLevel(level.upper())
    logger.handlers.clear()

    handlers: list[logging.Handler] = []
    if sys.stdout is not None:  # None quand Jarvis est lancé avec pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s -- %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    for handler in handlers:
        handler.setFormatter(fmt)
        logger.addHandler(handler)
