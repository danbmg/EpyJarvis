"""Navigation dans les applications par leurs éléments nommés (UI Automation de Windows).

Jarvis lit la liste des boutons, champs, onglets... d'une fenêtre, chacun avec un numéro,
puis clique ou écrit sur un numéro. Il vise donc un élément par son nom, jamais une coordonnée.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from jarvis.tools import ALLOW, Check, Param, Result, Tool, Toolbox, Verdict, confirm, forbid, norm
from jarvis.tools import windows as win
from jarvis.tools.desktop import VK, key_codes, press

log = logging.getLogger("jarvis.tools.ui")

_TYPES = {
    "ButtonControl": "bouton", "SplitButtonControl": "bouton", "EditControl": "champ",
    "ListItemControl": "élément", "TreeItemControl": "élément", "DataItemControl": "ligne",
    "HyperlinkControl": "lien", "TabItemControl": "onglet", "MenuItemControl": "menu",
    "CheckBoxControl": "case", "RadioButtonControl": "option", "ComboBoxControl": "liste",
    "SliderControl": "curseur", "DocumentControl": "document",
}
_MAX_NODES = 3000
_MAX_ELEMENTS = 150
_MAX_TEXTS = 60
# Boutons de la barre de titre : en tête de liste, « la deuxième vidéo » devenait [2] = Agrandir.
_CAPTION_BUTTONS = {"reduire", "agrandir", "restaurer", "fermer", "minimize", "maximize", "restore", "close"}

# Noms d'éléments sur lesquels Jarvis ne clique jamais (tes interdits), ou seulement après confirmation.
_FORBIDDEN_NAMES = re.compile(
    r"\b(supprimer|suppression|delete|effacer|erase|desinstaller|uninstall|formater|reinitialiser|reset"
    r"|mot de passe|password|vider la corbeille|empty recycle bin|arreter|redemarrer|restart|shut ?down"
    r"|eteindre|se deconnecter|deconnexion|sign out|log ?out)\b")
_CONFIRM_NAMES = re.compile(
    r"\b(envoyer|send|acheter|buy|achat|payer|pay|paiement|commander|order|publier|post|partager|share"
    r"|s'abonner|subscribe|transferer|transfer|installer|install|confirmer|valider)\b")
_BLOCKED_KEYS = [{VK["delete"]}, {VK["win"], ord("R")}, {VK["win"], ord("X")}, {VK["alt"], VK["f4"]}]


@dataclass
class _Element:
    control: Any
    kind: str
    name: str


class Navigator:
    """Garde le dernier relevé d'une fenêtre : les numéros renvoient à ses éléments."""

    def __init__(self) -> None:
        self._elements: dict[int, _Element] = {}
        self._window: win.Window | None = None

    # ---- Lecture ----

    def read(self, window: str = "") -> Result:
        target = win.find_window(window) if window else win.current_window()
        if target is None:
            return Result(False, "Je ne trouve pas de fenêtre « {} ».".format(window) if window
                          else "Aucune fenêtre ouverte.")
        return self._read(target)

    def _read(self, target: win.Window) -> Result:
        import uiautomation as auto

        root = auto.ControlFromHandle(target.hwnd)
        elements, texts = self._walk(root)
        if len(elements) < 5:  # les applis Chromium (Spotify, Discord...) activent leur arbre au 1er accès
            time.sleep(1.0)
            elements, texts = self._walk(root)

        self._window = target
        self._elements = {i: e for i, e in enumerate(elements, 1)}
        lines = ["Fenêtre « {} » ({}), {} éléments :".format(target.title, target.app, len(elements))]
        for i, e in self._elements.items():
            value = ""
            if e.kind == "champ":
                pattern = e.control.GetPattern(auto.PatternId.ValuePattern)
                value = " = « {} »".format(pattern.Value[:80]) if pattern and pattern.Value else ""
            lines.append("[{}] {} « {} »{}".format(i, e.kind, e.name, value))
        if texts:
            lines.append("Textes affichés : " + " | ".join(texts))
        return Result(True, "\n".join(lines))

    def _walk(self, root) -> tuple[list[_Element], list[str]]:
        elements: list[_Element] = []
        texts: list[str] = []
        seen: set[tuple[str, str]] = set()
        try:
            frame = root.BoundingRectangle
        except Exception:
            frame = None
        for count, (control, _) in enumerate(_safe_walk(root)):
            if count > _MAX_NODES or len(elements) >= _MAX_ELEMENTS:
                break
            try:
                ctype, name = control.ControlTypeName, (control.Name or "").strip()
                if ctype not in _TYPES and ctype != "TextControl":
                    continue
                if not name and ctype != "EditControl":
                    continue
                rect = control.BoundingRectangle
                if control.IsOffscreen or rect.width() <= 0 or rect.height() <= 0:
                    continue
            except Exception:
                continue
            name = " ".join(name.split())[:100]
            if ctype == "TextControl":
                if len(texts) < _MAX_TEXTS and name not in texts:
                    texts.append(name)
                continue
            if (ctype, name) in seen and ctype != "EditControl":
                continue
            if frame and ctype == "ButtonControl" and _is_caption(norm(name), rect, frame):
                continue
            seen.add((ctype, name))
            elements.append(_Element(control, _TYPES[ctype], name or "(sans nom)"))
        return elements, texts

    # ---- Actions ----

    def _unknown(self, element: int) -> Result:
        """Fenêtre pas encore lue ou numéro hors liste : relit la fenêtre au lieu d'agir à l'aveugle."""
        still_open = self._window and any(w.hwnd == self._window.hwnd for w in win.list_windows())
        target = self._window if still_open else win.current_window()
        if target is None:
            return Result(False, "Aucune fenêtre ouverte.")
        read = self._read(target)
        if not read.ok:
            return read
        return Result(False, "Rien n'a été fait : l'élément {} n'était pas dans la liste à jour. Voici la "
                             "fenêtre ; refais l'action avec le bon numéro.\n{}".format(element, read.message))

    def click(self, element: int, double: bool = False) -> Result:
        import uiautomation as auto

        if element not in self._elements:
            return self._unknown(element)
        e = self._elements[element]
        win.focus(self._window.hwnd)
        c = e.control
        if double:
            c.DoubleClick(simulateMove=False)
        elif (p := c.GetPattern(auto.PatternId.InvokePattern)):
            p.Invoke()
        elif (p := c.GetPattern(auto.PatternId.SelectionItemPattern)):
            p.Select()
        elif (p := c.GetPattern(auto.PatternId.TogglePattern)):
            p.Toggle()
        elif (p := c.GetPattern(auto.PatternId.ExpandCollapsePattern)):
            p.Expand()
        else:
            c.Click(simulateMove=False)
        time.sleep(0.8)
        return Result(True, "Clic sur {} « {} ». Relis la fenêtre pour voir le résultat.".format(e.kind, e.name))

    def type(self, element: int, text: str, submit: bool = False) -> Result:
        import uiautomation as auto

        if element not in self._elements:
            return self._unknown(element)
        e = self._elements[element]
        win.focus(self._window.hwnd)
        c = e.control
        try:
            c.SetFocus()
            c.BoundingRectangle  # l'élément existe-t-il encore ? (la page a pu changer depuis la lecture)
        except Exception:
            self._elements.pop(element, None)
            return self._unknown(element)
        pattern = c.GetPattern(auto.PatternId.ValuePattern)
        if not (pattern and not pattern.IsReadOnly and pattern.SetValue(text) and pattern.Value == text):
            # Champs qui refusent l'écriture directe (messageries web...) : coller le texte.
            previous = auto.GetClipboardText()
            auto.SetClipboardText(text)
            c.Click(simulateMove=False)
            press(VK["ctrl"], ord("A"))
            press(VK["ctrl"], ord("V"))
            time.sleep(0.2)
            auto.SetClipboardText(previous or "")
        if submit:
            time.sleep(0.2)
            press(VK["enter"])
        time.sleep(0.6)
        return Result(True, "Texte écrit dans {} « {} »{}.".format(
            e.kind, e.name, " puis validé" if submit else ""))

    def keys(self, keys: str) -> Result:
        codes = key_codes(keys)
        target = self._window or win.current_window()
        if target:
            win.focus(target.hwnd)
        press(*codes)
        time.sleep(0.5)
        return Result(True, "Touches « {} » envoyées.".format(keys))

    # ---- Garde-fous ----
    # Un numéro d'élément inconnu est autorisé : l'outil relit alors la fenêtre sans agir.

    @staticmethod
    def _guard_window(target: win.Window | None) -> Check:
        if target and target.process in win.SENSITIVE_PROCESSES:
            return forbid("je n'agis pas dans {} (fenêtre système ou console)".format(target.app))
        return ALLOW

    def guard_click(self, element: int, double: bool = False) -> Check:
        check = self._guard_window(self._window)
        if check.verdict is not Verdict.ALLOW or element not in self._elements:
            return check
        e = self._elements[element]
        if _FORBIDDEN_NAMES.search(norm(e.name)):
            return forbid("le bouton « {} » fait partie de ce que je n'ai pas le droit de faire".format(e.name))
        if _CONFIRM_NAMES.search(norm(e.name)):
            return confirm("Je clique sur « {} » dans {} ?".format(e.name, self._window.app))
        return ALLOW

    def guard_type(self, element: int, text: str, submit: bool = False) -> Check:
        check = self._guard_window(self._window)
        if check.verdict is not Verdict.ALLOW or element not in self._elements:
            return check
        e = self._elements[element]
        if self._window.process == "explorer.exe" and "recherch" not in norm(e.name) \
                and "search" not in norm(e.name):
            return forbid("dans l'explorateur, je n'écris que dans la recherche (la barre d'adresse "
                          "peut lancer des commandes)")
        if submit and self._window.process in win.MESSAGING_PROCESSES:
            return confirm("J'envoie dans {} : « {} ». Je confirme ?".format(self._window.app, text))
        return ALLOW

    def guard_keys(self, keys: str) -> Check:
        target = self._window or win.current_window()  # les touches partent dans cette fenêtre
        check = self._guard_window(target)
        if check.verdict is not Verdict.ALLOW:
            return check
        try:
            codes = set(key_codes(keys))
        except ValueError as exc:
            return forbid(str(exc))
        if any(blocked <= codes for blocked in _BLOCKED_KEYS):
            return forbid("la combinaison « {} » est interdite (suppression, exécution ou fermeture "
                          "système)".format(keys))
        if VK["enter"] in codes and target and target.process in win.MESSAGING_PROCESSES:
            return confirm("J'appuie sur Entrée dans {} : cela peut envoyer le message en cours. "
                           "Je confirme ?".format(target.app))
        return ALLOW


def _is_caption(name: str, rect, frame) -> bool:
    """Bouton Réduire/Agrandir/Fermer de la fenêtre elle-même : en haut à droite. Les « Fermer » des
    fenêtres intérieures (popup, panneau) restent cliquables."""
    return name in _CAPTION_BUTTONS and rect.top - frame.top < 60 and frame.right - rect.right < 250


def _safe_walk(root):
    import uiautomation as auto
    try:
        yield from auto.WalkControl(root, maxDepth=60)
    except Exception as exc:  # une fenêtre qui se ferme pendant la lecture
        log.debug("Lecture interrompue : %s", exc)


def register(box: Toolbox, nav: Navigator) -> None:
    box.add(Tool("ui_read",
                 "Lit le contenu d'une fenêtre : ses boutons, champs, onglets, listes et textes, "
                 "chacun avec un numéro. À utiliser pour savoir ce qu'affiche une application (« qu'est-ce "
                 "qu'il y a dans cette fenêtre ? »), avant d'y cliquer ou d'y écrire, et après chaque action "
                 "pour voir le résultat.",
                 nav.read, {"window": Param("nom de l'application ou titre de fenêtre ; vide pour "
                                            "la fenêtre au premier plan", required=False)},
                 reads=True, untrusted=True))
    box.add(Tool("ui_click", "Clique sur un élément lu avec ui_read (par son numéro).", nav.click, {
        "element": Param("numéro [n] de l'élément dans la liste lue par ui_read. Ce n'est pas un rang : « la "
                         "deuxième vidéo » est la deuxième vidéo de la liste, avec son propre numéro",
                         type="integer"),
        "double": Param("true pour un double-clic (ouvrir un fichier, un dossier)", type="boolean",
                        required=False),
    }, reads=True, acts=True, untrusted=True, guard=nav.guard_click))  # untrusted : peut renvoyer la fenêtre relue
    box.add(Tool("ui_type", "Écrit un texte dans un champ lu avec ui_read (par son numéro).", nav.type, {
        "element": Param("numéro du champ", type="integer"),
        "text": Param("texte à écrire"),
        "submit": Param("true pour valider avec Entrée (lancer une recherche, envoyer)", type="boolean",
                        required=False),
    }, reads=True, acts=True, untrusted=True, private=True, guard=nav.guard_type))
    box.add(Tool("ui_keys", "Envoie un raccourci clavier à la fenêtre lue (ex. « ctrl+f », « enter », "
                            "« down », « esc », « space »).", nav.keys, {
        "keys": Param("touches séparées par +"),
    }, reads=True, acts=True, guard=nav.guard_keys))
