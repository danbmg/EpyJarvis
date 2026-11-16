"""Microsoft To Do, par son application de bureau (UI Automation).

To Do est une application Windows native (XAML) : listes, tâches et cases à cocher y ont des noms et des
identifiants stables (« SidebarListView », « TodoLabel », « IsCompletedCheckBox ») et se pilotent sans la
souris (Invoke, Toggle, Value). Toute la fenêtre tient en ~170 éléments, lus en 0,25 s. Pas d'API à
configurer : les tâches se synchronisent d'elles-mêmes avec le compte Microsoft (l'API Microsoft Graph
demanderait d'inscrire une application chez Microsoft).

Ajouter, terminer ou rouvrir une tâche se fait sans confirmation : c'est ce que l'utilisateur demande, ça se
défait, et la réponse nomme la tâche et la liste. Chaque action est vérifiée dans l'application : la tâche
apparaît dans la liste, ou passe parmi les tâches terminées.
"""

from __future__ import annotations

import collections
import os
import re
from typing import Any

from jarvis.tools import Param, Result, Tool, Toolbox, norm
from jarvis.tools import windows as win
from jarvis.tools.desktop import VK, press
from jarvis.tools.messaging import _first, _same, _stable, _until, _variants

_APP = r"shell:AppsFolder\Microsoft.Todos_8wekyb3d8bbwe!App"
_DEFAULT_LIST = "Tâches"
_NO_APP = Result(False, "Microsoft To Do ne s'ouvre pas.")
_SELECTED = re.compile(r"La liste (?P<name>.+?) est sélectionnée")
_ADD_IN = re.compile(r"[“\"«]\s*(?P<name>.+?)\s*[”\"»]")             # « Ajoutez une tâche dans “Code” »
_COUNT = re.compile(r"(\d+) tâches? non effectuées?")                 # « Code, 8 tâches non effectuées, … »
# Résultat de recherche : « Tâche Jarvis, dans Code., plus d’options… », « Tâche effectuée Jarvis, dans Code. »
_FOUND = re.compile(r"^T[âa]che (?P<done>effectu[ée]e )?(?P<title>.+), dans (?P<list>.+?)\.(?:,|$)")


def _window() -> win.Window | None:
    """La fenêtre de To Do, ouverte au besoin."""
    def find():
        return next((w for w in win.list_windows() if norm(w.title) == "microsoft to do"), None)

    window = find()
    if window is None:
        os.startfile(_APP)
        window = _until(find, 10, every=0.3)
    return window


def _sidebar(root):
    return _first(root, lambda c: c.AutomationId == "SidebarListView", limit=400)


def _root():
    """L'application au premier plan et prête (réduite, Windows la suspend et elle ne répond plus)."""
    import uiautomation as auto

    window = _window()
    if window is None:
        return None
    win.focus(window.hwnd)
    root = auto.ControlFromHandle(window.hwnd)
    return root if _until(lambda: _sidebar(root), 8) else None


def _lists(root) -> list[tuple[str, Any]]:
    """(nom, ligne) des listes de la barre de gauche, dans l'ordre."""
    sidebar = _sidebar(root)
    found = []
    for item in sidebar.GetChildren() if sidebar else []:
        label = _first(item, lambda c: c.AutomationId == "StaticRenderer", limit=20)
        if item.ControlTypeName == "ListItemControl" and label is not None:
            found.append((label.Name, item))
    return found


def _add_box(root):
    return _first(root, lambda c: c.ControlTypeName == "EditControl" and c.AutomationId == "TextBox"
                  and norm(c.Name or "").startswith(("ajoutez une tache", "ajouter une tache", "add a task")), limit=400)


def _shown(root) -> str:
    """La liste affichée : « Ajoutez une tâche dans “Code” », sinon le titre « La liste Code est sélectionnée »."""
    box = _add_box(root)
    match = _ADD_IN.search(box.Name or "") if box else None
    if match:
        return match["name"]
    title = _first(root, lambda c: c.AutomationId == "Renderer" and c.ControlTypeName == "ButtonControl", limit=400)
    match = _SELECTED.search(title.Name or "") if title else None
    return match["name"] if match else ""


def _open_list(root, name: str) -> Result:
    """Affiche la liste la plus ressemblante ; Result(True, son nom exact)."""
    lists = _lists(root)
    score, found, item = max(((_same(n, name), n, i) for n, i in lists), key=lambda s: s[0], default=(0, "", None))
    if score < 1.8:
        return Result(False, "Je ne trouve pas la liste « {} » dans To Do. Tes listes : {}.".format(
            name, ", ".join(n for n, _ in lists)))
    if _same(_shown(root), found) < 3:
        item.GetInvokePattern().Invoke()
        if not _until(lambda: _same(_shown(root), found) == 3, 3):
            return Result(False, "La liste « {} » ne s'est pas ouverte.".format(found))
    return Result(True, found)


def _tasks(root) -> list[tuple[str, Any]]:
    """(titre, ligne) des tâches à faire de la liste affichée."""
    view = _first(root, lambda c: c.AutomationId == "TodosListView", limit=400)
    found = []
    for item in view.GetChildren() if view else []:
        label = _first(item, lambda c: c.AutomationId == "TodoLabel", limit=20)
        if item.ControlTypeName == "ListItemControl" and label is not None \
                and not norm(item.Name or "").startswith("tache effectuee"):
            found.append((label.Name, item))
    return found


def _search_box(root):
    return _first(root, lambda c: c.ControlTypeName == "EditControl" and c.AutomationId == "TextBox"
                  and norm(c.Name or "").startswith(("rechercher", "search")), limit=100)


def _found(root) -> list[tuple[str, str, bool, Any]]:
    """Résultats de la recherche : (titre, liste, terminée, ligne)."""
    view = _first(root, lambda c: c.AutomationId == "SearchListView", limit=400)
    found = []
    for item in view.GetChildren() if view else []:
        match = _FOUND.match(" ".join((item.Name or "").split()))
        if item.ControlTypeName == "ListItemControl" and match:
            found.append((match["title"], match["list"], bool(match["done"]), item))
    return found


def _search(root, box, text: str) -> list[tuple[str, str, bool, Any]]:
    box.GetValuePattern().SetValue(text)
    return _stable(lambda: _found(root), 2.5, key=lambda r: (r[0], r[1], r[2]), allow_empty=True)


def _end_search(box) -> None:
    """Vide la recherche : To Do réaffiche la liste d'avant."""
    box.GetValuePattern().SetValue("")
    box.SetFocus()
    press(VK["esc"])


# ---- Outils ----

def todo_open(list: str = "") -> Result:  # « list » : le nom vu par le modèle
    root = _root()
    if root is None:
        return _NO_APP
    if not list:
        return Result(True, "J'ai ouvert To Do.")
    opened = _open_list(root, list)
    return Result(True, "J'ai ouvert ta liste {} dans To Do.".format(opened.message)) if opened.ok else opened


def todo_add(task: str, list: str = "") -> Result:
    root = _root()
    if root is None:
        return _NO_APP
    opened = _open_list(root, list or _DEFAULT_LIST)
    if not opened.ok:
        return opened
    box = _until(lambda: _add_box(root), 2)
    if box is None:
        return Result(False, "To Do ne permet pas d'ajouter une tâche dans « {} ».".format(opened.message))
    before = collections.Counter(t for t, _ in _tasks(root))
    box.GetValuePattern().SetValue(task)
    box.SetFocus()
    press(VK["enter"])
    # To Do peut retirer une date du titre (« … demain » devient une échéance) : la tâche est celle qui apparaît.
    added = _until(lambda: _new_titles(before, [t for t, _ in _tasks(root)]), 3)
    if not added:
        return Result(False, "La tâche « {} » n'apparaît pas dans {} : elle n'a peut-être pas été ajoutée.".format(
            task, opened.message))
    return Result(True, "J'ai ajouté « {} » dans {}.".format(added[0], opened.message))


def _new_titles(before: collections.Counter, after: list[str]) -> list[str]:
    """Titres apparus depuis « before » (une tâche peut exister deux fois)."""
    return [*(collections.Counter(after) - before).elements()]


def todo_complete(task: str, list: str = "", reopen: bool = False) -> Result:
    """Coche (ou décoche) la tâche la plus ressemblante. Cherchée d'abord par la recherche de To Do (toutes les
    listes, terminées comprises) ; celle-ci tient compte des accents (« ecole » ne trouve pas « école »), alors
    une tâche à faire introuvable est cherchée liste par liste (~3 s)."""
    root = _root()
    if root is None:
        return _NO_APP
    box = _search_box(root)
    if box is None:
        return Result(False, "La recherche de To Do n'apparaît pas.")
    try:
        candidates = []
        for query in _variants(task):  # Whisper écorche les mots : « hackaton » pour « hackathon »
            candidates = [(_same(title, task), title, where, item) for title, where, done, item in _search(root, box, query)
                          if done == reopen and (not list or _same(where, list) >= 1.8)]
            if _best(candidates) >= 1.8:
                break
        if _best(candidates) >= 1.8:
            chosen = _pick(candidates, task)
            if not isinstance(chosen, tuple):
                return chosen
            _, title, where, item = chosen

            def state() -> int:
                return sum(1 for t, w, d, _ in _found(root) if (t, w, d) == (title, where, not reopen))
            return _toggle(item, "IsCompletedCheckBox", title, where, reopen, state, more=True)
    finally:
        _end_search(box)
    if reopen:
        return Result(False, "Je ne trouve pas de tâche terminée « {} » dans To Do.".format(task))
    chosen = _pick(_from_lists(root, task, list), task)
    if not isinstance(chosen, tuple):
        return chosen
    _, title, where, _ = chosen
    if not _open_list(root, where).ok:
        return Result(False, "La liste « {} » ne s'est pas rouverte.".format(where))
    item = next((i for t, i in _tasks(root) if t == title), None)
    if item is None:
        return Result(False, "La tâche « {} » a disparu de {}.".format(title, where))
    return _toggle(item, "CompleteTodoCheckBox", title, where, reopen,
                   lambda: sum(1 for t, _ in _tasks(root) if t == title), more=False)


_SMART_LISTS = ("ma journee", "important", "planifie", "affectees a moi", "courrier avec indicateur")


def _from_lists(root, task: str, where: str) -> list[tuple[float, str, str, None]]:
    """Tâches à faire ressemblantes, liste par liste (sans les listes intelligentes, qui reprennent les autres) ;
    s'arrête à la première tâche au nom identique."""
    found = []
    for name, _ in _lists(root):
        if norm(name) in _SMART_LISTS or (where and _same(name, where) < 1.8) or not _open_list(root, name).ok:
            continue
        found += [(_same(title, task), title, name, None) for title, _ in _tasks(root) if _same(title, task)]
        if any(score == 3 for score, *_ in found):
            break
    return found


def _best(candidates: list) -> float:
    return max((c[0] for c in candidates), default=0)


def _pick(candidates: list, task: str) -> tuple | Result:
    """La tâche la plus ressemblante (note ≥ 1,8), ou l'échec à dire : introuvable, ou plusieurs possibles."""
    best = _best(candidates)
    if best < 1.8:
        return Result(False, "Je ne trouve pas de tâche à faire « {} » dans To Do.".format(task))
    top = [*dict.fromkeys((title, where) for score, title, where, _ in candidates if score == best)]
    if len(top) > 1:
        return Result(False, "Plusieurs tâches correspondent : {}. Laquelle ?".format(
            ", ".join("« {} » ({})".format(t, w) for t, w in top[:5])))
    return next(c for c in candidates if c[0] == best)


def _toggle(item, checkbox: str, title: str, where: str, reopen: bool, count, more: bool) -> Result:
    """Coche ou décoche, puis vérifie : la tâche passe de l'autre côté (count augmente, ou diminue)."""
    before = count()
    _first(item, lambda c: c.AutomationId == checkbox, limit=20).GetTogglePattern().Toggle()
    if not _until(lambda: count() > before if more else count() < before, 3):
        return Result(False, "La case de « {} » ne s'est pas {}.".format(title, "décochée" if reopen else "cochée"))
    return Result(True, "J'ai {} « {} » dans {}.".format("rouvert" if reopen else "terminé", title, where))


def todo_list(list: str = "") -> Result:
    root = _root()
    if root is None:
        return _NO_APP
    if not list:  # une phrase prête à dire, total compris : le modèle lisait mal « Tâches (2), Nice (3)… »
        counts = [(int(match[1]), name) for name, item in _lists(root) for match in [_COUNT.search(item.Name or "")]
                  if match]
        if not counts:
            return Result(True, "Tu n'as aucune tâche à faire dans To Do.")
        # Pas de consigne ajoutée ici (« rappelle todo avec list… ») : le modèle inventait alors des tâches.
        return Result(True, "Tu as {} tâches à faire au total : {}.".format(
            sum(n for n, _ in counts), ", ".join("{} dans {}".format(n, name) for n, name in counts)))
    opened = _open_list(root, list)
    if not opened.ok:
        return opened
    tasks = [t for t, _ in _tasks(root)]
    if not tasks:
        return Result(True, "Rien à faire dans {}.".format(opened.message))
    # Les tâches d'abord, cinq au plus comme le veut le prompt : sinon le modèle ne citait que leur nombre.
    shown = ", ".join(tasks[:5]) + (" et {} autres ({})".format(len(tasks) - 5, ", ".join(tasks[5:30]))
                                    if len(tasks) > 5 else "")
    return Result(True, "À faire dans {} : {}.".format(opened.message, shown))


def todo(action: str, task: str = "", list: str = "") -> Result:
    """Un seul outil pour To Do : avec quatre outils séparés, le modèle se trompait ailleurs (« va sur
    github.com » ouvrait le navigateur sans le site, « lance le premier » partait sur Spotify)."""
    if action in ("add", "complete", "reopen") and not task:
        return Result(False, "Il me faut l'intitulé de la tâche.")
    if action == "add":
        return todo_add(task, list)
    if action in ("complete", "reopen"):
        return todo_complete(task, list, reopen=action == "reopen")
    return todo_list(list) if action == "list" else todo_open(list)


def register(box: Toolbox) -> None:
    box.add(Tool("todo",
                 "Microsoft To Do, la liste de tâches de l'utilisateur. add : ajoute une tâche (une par appel ; "
                 "sans liste nommée, dans « Tâches ») ; complete : la coche (« j'ai fini… », « ferme la tâche… ») ; "
                 "reopen : décoche une tâche terminée (« rouvre la tâche… ») ; list : lit ce qu'il y a à faire, dans "
                 "la liste nommée s'il y en a une (cite les tâches) ; open : affiche l'application.", todo, {
                     "action": Param("add, complete, reopen, list ou open", enum=["add", "complete", "reopen", "list", "open"],
                                     aliases={"ajouter": "add", "terminer": "complete", "cocher": "complete",
                                              "rouvrir": "reopen", "lire": "list", "ouvrir": "open"}),
                     "task": Param("intitulé de la tâche, tel que dit", required=False),
                     "list": Param("nom de la liste quand l'utilisateur en nomme une (« ma liste X » : X)",
                                   required=False),
                 }))
