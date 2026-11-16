"""Outils de Jarvis : tout ce que le LLM peut demander, avec leurs garde-fous.

Chaque outil déclare :
  - sa description et ses paramètres (envoyés au LLM),
  - s'il « lit » (résultat renvoyé au LLM) ou « agit » (message destiné à l'utilisateur),
  - un éventuel garde-fou qui autorise, demande confirmation ou refuse selon les arguments.
"""

from __future__ import annotations

import difflib
import enum
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("jarvis.tools")

_THINK_TAG = re.compile(r"/no_think\b|/think\b", re.IGNORECASE)


def norm(text: str) -> str:
    """Minuscules, sans accents, espaces simples : pour comparer ce qui a été dit."""
    text = unicodedata.normalize("NFD", str(text).lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def best_match(spoken: str, names: dict[str, Any], cutoff: float = 0.75) -> Any | None:
    """Retrouve une valeur à partir d'un nom dit à l'oral : exact, inclus, puis approchant.

    names : {nom ou alias déjà normalisé (norm): valeur}
    """
    key = norm(spoken)
    if not key:
        return None
    if key in names:
        return names[key]
    for name in sorted(names, key=len, reverse=True):  # « ouvre l'appli discord » contient « discord »
        if len(name) >= 3 and name in key:
            return names[name]
    for name in sorted(names, key=len):                 # « spot » est le début de « spotify »
        if len(key) >= 4 and name.startswith(key):
            return names[name]
    close = difflib.get_close_matches(key, list(names), n=1, cutoff=cutoff)
    return names[close[0]] if close else None


@dataclass
class Result:
    ok: bool
    message: str
    retryable: bool = True  # en échec : rien n'a été fait, l'action peut être refaite sans risque


class Verdict(enum.Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    FORBID = "forbid"


@dataclass
class Check:
    verdict: Verdict
    reason: str = ""  # question de confirmation, ou raison du refus


ALLOW = Check(Verdict.ALLOW)


def confirm(question: str) -> Check:
    return Check(Verdict.CONFIRM, question)


def forbid(reason: str) -> Check:
    return Check(Verdict.FORBID, reason)


@dataclass
class Param:
    description: str
    type: str = "string"
    enum: list[str] | None = None
    required: bool = True
    aliases: dict[str, str] | None = None  # autre façon de dire une valeur de enum (déjà normalisée) -> valeur


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Result]
    params: dict[str, Param] = field(default_factory=dict)
    reads: bool = False            # True : le résultat est renvoyé au LLM pour qu'il l'exploite
    acts: bool = False             # avec reads : l'outil agit quand même (clic, frappe), il ne fait pas que lire
    untrusted: bool = False        # le résultat contient du contenu externe (web, fenêtres, messages)
    after_untrusted_confirm: bool = False  # demande confirmation si du contenu externe a été lu
    private: bool = False          # n'écrit pas les arguments dans le journal
    notice: str | None = None      # phrase dite avant un outil lent
    guard: Callable[..., Check] | None = None
    # Avant une confirmation : prépare l'action (ouvre la conversation) et renvoie la question, qui nomme alors
    # la vraie destination ; en échec, rien n'est demandé et le modèle reçoit l'erreur.
    prepare: Callable[..., Result] | None = None

    def schema(self) -> dict:
        props: dict[str, Any] = {}
        for name, p in self.params.items():
            props[name] = {"type": p.type, "description": p.description}
            if p.enum:
                props[name]["enum"] = p.enum
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": props,
                           "required": [n for n, p in self.params.items() if p.required]},
        }}

    def clean_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Garde les paramètres déclarés, convertit les types. Lève ValueError si un requis manque."""
        clean: dict[str, Any] = {}
        for name, p in self.params.items():
            value = args.get(name)
            if isinstance(value, str):  # qwen3 recopie parfois sa consigne « /no_think » comme argument
                value = _THINK_TAG.sub(" ", value)
            if value is None or (isinstance(value, str) and not value.strip()):
                if p.required:
                    raise ValueError("paramètre manquant : {}".format(name))
                continue
            if p.type == "boolean":
                value = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "oui", "yes")
            elif p.type == "integer":
                value = int(value)
            elif p.type == "number":
                value = float(str(value).replace(",", "."))
            else:
                value = str(value).strip()
                if p.enum and value not in p.enum:
                    key = norm(value)
                    value = (p.aliases or {}).get(key) or next((e for e in p.enum if norm(e) == key), None)
                    if value is None:
                        raise ValueError("{} doit valoir {}".format(name, " ou ".join(p.enum)))
            clean[name] = value
        return clean


class Toolbox:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)
