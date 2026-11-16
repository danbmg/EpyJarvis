"""Messages Discord et WhatsApp, envoyés en ton nom par leurs applications de bureau (UI Automation).

Aucune API gratuite et autorisée n'envoie un message au nom de l'utilisateur : Discord interdit d'automatiser
un compte personnel par son API (« self-bot »), et l'API WhatsApp n'existe que pour les comptes Business.
Jarvis pilote donc les applications, comme le ferait l'utilisateur.

Discord : un serveur s'ouvre par la navigation rapide (Ctrl+K ; « *nom » n'y cherche que les serveurs), un
salon par son lien dans la barre de gauche (« général (salon textuel) »), une conversation privée par Ctrl+K.
Le titre de la fenêtre (« #général | Dan's office - Discord ») confirme la destination.
WhatsApp : la recherche filtre la liste des discussions, la ligne du contact est cliquée, et le champ
« Écrivez un message à Maman » confirme la destination.

Ce sont deux pages web (Electron, WebView2) dont l'arbre d'accessibilité se construit à la première lecture :
les éléments sont attendus en relisant la fenêtre (_until), pas avec des pauses fixes, trop courtes la
première fois.

Envoi : la conversation est ouverte avant la confirmation (prepare), qui nomme donc la vraie destination ; le
texte est écrit après un vrai clic dans le champ (SetFocus n'y donne pas le focus), relu avant Entrée, et
l'envoi n'est annoncé que si le message apparaît ensuite dans la conversation.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import time
from typing import Any, Callable

from jarvis.tools import Check, Param, Result, Tool, Toolbox, confirm, norm
from jarvis.tools import windows as win
from jarvis.tools.desktop import VK, press
from jarvis.tools.ui import _safe_walk

log = logging.getLogger("jarvis.tools.messaging")

_WHATSAPP_APP = r"shell:AppsFolder\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"
_MAX_NODES = 25000
# Lien d'un salon dans la barre de gauche : « général (salon textuel) », « epigimp (salon textuel), Salon privé
# (verrouillé) », parfois précédé d'un état (« non lu, ... »).
_CHANNEL_LINK = re.compile(r"(?:^|, )(?P<name>[^,()]+?) \((?P<kind>salon [^)]*|[^)]*channel)\)")
# Recherche WhatsApp : en-têtes de section, et nom d'une ligne avant son heure ou sa date (« Maman ❤️ 01:07 Stop… »)
_WA_SECTION = re.compile(r"(?i)^(discussions|contacts|groupes en commun|messages|chats|groups in common)$")
_WA_ROW = re.compile(r"(?i)^(?:\d+ messages? non lus? )?(?P<name>.+?)(?: (?:\d{1,2}:\d{2}|\d{1,2}/\d{1,2}/\d{2,4}"
                     r"|hier|aujourd.hui|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b.*)?$")
_DISCORD_TITLE = re.compile(r"^(?:\(\d+\) |• )?(?P<place>.*?)(?: \| (?P<server>.*?))? - Discord$")
_NO_DISCORD = Result(False, "Discord n'est pas ouvert : ouvre-le d'abord (open_app discord).")
_NO_SWITCHER = Result(False, "La navigation rapide de Discord (Ctrl+K) ne s'est pas ouverte.")
# Sans exemple de phrase : le modèle 8B recopiait l'exemple comme message (« Demande-lui pourquoi elle est en
# retard » envoyé pour « demande à maman si elle vient »).
_MESSAGE = ("seulement le texte du message, sans la consigne, le destinataire ni l'application ; adressé au "
            "destinataire (une question se pose directement) ; garde « Jarvis » s'il fait partie du texte")
_SERVER = ("serveur Discord, seulement si l'utilisateur le nomme dans sa demande (jamais celui affiché à "
           "l'écran, jamais pour une personne)")
_UNSURE = ("J'ai appuyé sur Entrée, mais je ne vois pas le message {} : regarde la conversation avant de le "
           "renvoyer.")


def _plain(text: str) -> str:
    """« 💬｜Général » -> « general » : pour comparer des noms avec émojis et séparateurs."""
    return " ".join(re.sub(r"[^\w']+", " ", norm(text).replace("’", "'")).split())


def _same(found: str, wanted: str) -> float:
    """Ressemblance d'un nom affiché avec un nom dit, de 0 (aucune) à 3 :
    3 identique ou mots du début (« Mathis Epitech » pour « Mathis ») ; 2,5 mots entiers ailleurs (« Julie Dan
    Moreau ») ; 2 début d'un mot (« Nourya ») ; 1,8 à 2 très proche, mots du début compris (« Dans office »,
    « Nour » pour « Noure » : Whisper écorche les noms) ; 1 lettres contenues dans un mot (« Benoureau ») ; 0 sinon.
    À égalité, l'ordre de l'application (conversations récentes d'abord) départage."""
    found, wanted = _plain(found).replace("'", ""), _plain(wanted).replace("'", "")
    if not found or not wanted:
        return 0
    if found == wanted or found.startswith(wanted + " "):
        return 3
    if " {} ".format(wanted) in " {} ".format(found):
        return 2.5
    if found.startswith(wanted):
        return 2
    lead = " ".join(found.split()[:len(wanted.split())])
    close = max(difflib.SequenceMatcher(None, found, wanted).ratio(), difflib.SequenceMatcher(None, lead, wanted).ratio())
    return 1 + close if close >= 0.8 else 1 if wanted in found else 0


def _variants(name: str) -> list[str]:
    """Recherches à essayer pour un nom dit : tel quel, sans accents (la recherche de Discord en tient compte),
    son premier mot, puis son début (« Noure » -> « Nour », « Matis » -> « Mati ») ; _same trie ensuite."""
    first = norm(name).split()[0] if norm(name) else ""
    found: list[str] = []
    for query in (name, norm(name), first, first[:4] if len(first) >= 5 else first[:3] if len(first) == 4 else ""):
        if query and query.lower() not in (f.lower() for f in found):
            found.append(query)
    return found


def _until(probe: Callable[[], Any], timeout: float, every: float = 0.15) -> Any:
    """Relit jusqu'à obtenir un résultat (ou la fin du délai) : ces fenêtres se construisent après coup."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = probe()
        except Exception:  # élément disparu pendant la lecture
            found = None
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(every)


def _stable(probe: Callable[[], list], timeout: float, key: Callable[[Any], str] = lambda c: c.Name,
            allow_empty: bool = False) -> list:
    """Résultats d'une recherche une fois la liste stabilisée (deux lectures identiques) : cliquer pendant
    qu'elle se filtre ouvrirait la mauvaise ligne. allow_empty : une liste vide stable est une réponse (« rien
    trouvé »), au lieu d'attendre tout le délai."""
    previous: list[str] | None = None

    def settled():
        nonlocal previous
        found = probe()
        names = [key(c) for c in found]
        ready = names == previous and (bool(found) or allow_empty)
        previous = names
        return (found or [None]) if ready else None  # [None] : vide mais stable

    return [f for f in (_until(settled, timeout, every=0.25) or []) if f is not None]


def _walk(root, test: Callable[[Any], bool], limit: int = _MAX_NODES, first: bool = False) -> list:
    found = []
    for i, (control, _) in enumerate(_safe_walk(root)):
        if i > limit:
            break
        try:
            if test(control):
                found.append(control)
                if first:
                    break
        except Exception:  # élément disparu pendant la lecture
            continue
    return found


def _first(root, test: Callable[[Any], bool], limit: int = _MAX_NODES):
    found = _walk(root, test, limit=limit, first=True)
    return found[0] if found else None


def _text(control) -> str:
    """Texte affiché dans un élément (ses textes et liens)."""
    parts = [c.Name.strip() for c in _walk(control, lambda c: c.ControlTypeName in ("TextControl", "HyperlinkControl")
                                           and (c.Name or "").strip(), limit=300)]
    return " ".join(p for i, p in enumerate(parts) if not i or p != parts[i - 1])


def _paste(text: str) -> None:
    """Colle un texte là où est le curseur (les champs de Discord et WhatsApp refusent l'écriture directe)."""
    import uiautomation as auto

    previous = auto.GetClipboardText()
    auto.SetClipboardText(text)
    press(VK["ctrl"], ord("V"))
    time.sleep(0.4)  # le collage est traité après coup : ne pas remettre l'ancien presse-papiers trop tôt
    auto.SetClipboardText(previous or "")


def _window(process: str) -> win.Window | None:
    return next((w for w in win.list_windows() if w.process == process), None)


def _value(control) -> str:
    import uiautomation as auto

    pattern = control.GetPattern(auto.PatternId.ValuePattern)
    return (pattern.Value or "") if pattern else ""


def _write(window: win.Window, composer, message: str) -> Result | None:
    """Écrit le message dans le champ, sans l'envoyer ; None si le texte y est bien, sinon l'échec.
    Ctrl+A remplace un brouillon éventuel plutôt que de l'envoyer avec."""
    win.focus(window.hwnd)
    composer.Click(simulateMove=False)  # SetFocus ne donne pas le focus clavier à ces champs web
    time.sleep(0.15)
    press(VK["ctrl"], ord("A"))
    _paste(message)
    wanted = _plain(message)[:40]
    if wanted and not _until(lambda: wanted in _plain(_value(composer)), 2):
        return Result(False, "Je n'ai pas réussi à écrire dans le champ de message : rien n'est parti.")
    return None


def _send(composer_of: Callable[[], Any], message: str, last_message: Callable[[], str]) -> bool:
    """Appuie sur Entrée ; vrai si le champ s'est vidé et que le message apparaît dans la conversation."""
    press(VK["enter"])
    wanted = _plain(message)[:40]

    def sent():
        composer = composer_of()
        return composer is not None and not _plain(_value(composer)) and wanted in _plain(last_message())

    return bool(_until(sent, 5, every=0.3))


# ---- Discord ----

def _score(result: str, target: str, server: str) -> int:
    """Note d'un résultat de la navigation rapide (« general, Salon textuel, night owls », « Nour, utilisateur,
    petitsoleil. »)."""
    name, _, rest = result.partition(",")
    kind, _, guild = rest.strip().partition(",")
    user = norm(kind).startswith(("utilisateur", "user"))
    score = _same(name, target)
    if not score and user:
        score = min(_same(guild, target), 2)        # son nom d'utilisateur
    if score and ("vocal" in norm(kind) or "voice" in norm(kind)):
        score -= 2                                   # un salon vocal n'a pas de messages
    if score and server and not user:               # une personne n'appartient pas à un serveur
        score += 3 if _same(guild, server) >= 1.8 else -5
    return score


def _discord_place() -> tuple[str, str]:
    """Titre « #général | Dan's office - Discord » -> (« #général », « Dan's office ») ; (« @Nour », « ») en privé."""
    window = _window("discord.exe")
    match = _DISCORD_TITLE.match(window.title) if window else None
    return (match["place"], match["server"] or "") if match else ("", "")


def _label(place: str, server: str) -> str:
    return "{} de {}".format(place, server) if server else place


def _here(target: str, server: str) -> bool:
    place, current = _discord_place()
    return _same(place.lstrip("#@"), target) >= 2 and (not server or _same(current, server) >= 2)


# La navigation rapide (Ctrl+K) ouverte par Jarvis : sa fenêtre, None si elle est fermée. Son état se lit à ses
# résultats, pas au focus : après la saisie, le focus est sur un résultat ; après Échap, il reste sur le champ.
_switch_dialog = None


def _switch_items(dialog) -> list:
    return _walk(dialog, lambda c: c.ControlTypeName == "ListItemControl"
                 and "quick-switcher" in (c.AutomationId or ""), limit=800)


def _switcher():
    """La fenêtre de la navigation rapide, si elle vient de prendre le focus avec ses résultats."""
    import uiautomation as auto

    focused = auto.GetFocusedControl()
    if not focused or not (norm(focused.Name or "") in ("navigation rapide", "quick switcher")
                           or (focused.AutomationId or "").startswith("quick-switcher")):
        return None
    dialog = focused
    while dialog.ControlTypeName != "WindowControl" and dialog.GetParentControl():
        dialog = dialog.GetParentControl()
    return dialog if _switch_items(dialog) else None


def _close_switcher() -> None:
    """Échap vide d'abord la recherche ; un second Échap ferme la navigation."""
    global _switch_dialog
    for _ in range(2):
        press(VK["esc"])
        time.sleep(0.2)
        if _switch_dialog is None or not _switch_items(_switch_dialog):
            break
    _switch_dialog = None


def _quick_switch(window: win.Window, query: str) -> list | None:
    """Navigation rapide (Ctrl+K) : y colle la recherche et renvoie ses résultats une fois stables, le premier
    étant sélectionné ; None si elle ne s'ouvre pas. Déjà ouverte, elle est réutilisée."""
    global _switch_dialog
    win.focus(window.hwnd)
    if _switch_dialog is None or not _switch_items(_switch_dialog):
        _switch_dialog = None
        for _ in range(2):  # Ctrl+K referme la navigation si elle était déjà ouverte : un second appui la rouvre
            press(VK["ctrl"], ord("K"))
            _switch_dialog = _until(_switcher, 2.5)
            if _switch_dialog:
                break
        if _switch_dialog is None:
            return None
    press(VK["ctrl"], ord("A"))  # remplace une recherche précédente
    _paste(query)
    time.sleep(0.3)
    dialog = _switch_dialog
    return _stable(lambda: _switch_items(dialog), 3, allow_empty=True)


def _choose(index: int) -> None:
    global _switch_dialog
    press(VK["down"], times=index)
    press(VK["enter"])
    _switch_dialog = None  # Entrée ferme la navigation


def _discord_server(window: win.Window, server: str) -> Result:
    """Affiche un serveur ; Result(True, son nom exact)."""
    current = _discord_place()[1]
    if current and _same(current, server) >= 2:
        return Result(True, current)
    results = _quick_switch(window, "*" + server)
    if results is None:
        return _NO_SWITCHER
    guilds = [(i, re.sub(r"(?i),\s*(serveur|server)$", "", r.Name or "")) for i, r in enumerate(results)
              if re.search(r"(?i),\s*(serveur|server)$", r.Name or "")]
    best = max(guilds, key=lambda g: _same(g[1], server), default=None)
    if best is None or _same(best[1], server) < 1.8:  # des lettres en commun ne suffisent pas pour un serveur
        _close_switcher()
        return Result(False, "Je ne trouve pas le serveur « {} » dans Discord.".format(server))
    _choose(best[0])
    if not _until(lambda: _same(_discord_place()[1], best[1]) == 3, 4):
        return Result(False, "Le serveur « {} » ne s'est pas ouvert.".format(best[1]))
    return Result(True, best[1])


def _channel_link(window: win.Window, target: str) -> bool:
    """Ouvre un salon du serveur affiché par son lien dans la barre de gauche (Invoke : sans la souris, même
    s'il faut faire défiler la liste) ; faux s'il n'y est pas (catégorie repliée)."""
    import uiautomation as auto

    links = []
    for link in _walk(auto.ControlFromHandle(window.hwnd), lambda c: c.ControlTypeName == "HyperlinkControl"
                      and _CHANNEL_LINK.search(c.Name or "")):
        match = _CHANNEL_LINK.search(link.Name)
        if "vocal" not in norm(match["kind"]) and "voice" not in norm(match["kind"]):
            links.append((_same(match["name"], target), match["name"], link))
    score, name, link = max(links, key=lambda l: l[0], default=(0, "", None))
    if not score:
        return False
    pattern = link.GetPattern(auto.PatternId.InvokePattern)
    if pattern:
        pattern.Invoke()
    else:
        link.Click(simulateMove=False)
    return bool(_until(lambda: _same(_discord_place()[0].lstrip("#"), name) == 3, 3))


def _switch_to(window: win.Window, target: str, server: str, people_only: bool = False) -> Result:
    """Ouvre une conversation privée ou un salon par la navigation rapide. Les variantes du nom (_variants)
    sont cherchées jusqu'à un résultat très proche ; la plus ressemblante l'emporte (« Noure » : « Nour »
    plutôt que « Benoureau »). people_only : seulement une personne, telle que dite."""
    def scores(results: list) -> list[float]:
        return [_score(r.Name or "", target, server) if not people_only or norm(r.Name or "").partition(",")[2]
                .strip().startswith(("utilisateur", "user")) else 0 for r in results]

    best = (0.0, "", 0)  # (note, recherche, rang)
    queries = [target] if people_only else _variants(target)
    for query in queries:
        results = _quick_switch(window, query)
        if results is None:
            return _NO_SWITCHER
        found = scores(results)
        if found and max(found) > best[0]:
            best = (max(found), query, found.index(max(found)))
        if best[0] >= 1.8:  # très proche ou mieux : inutile d'essayer les variantes suivantes (~2 s chacune)
            break
    if not best[0]:
        _close_switcher()
        where = " sur le serveur {}".format(server) if server else ""
        return Result(False, "Je ne trouve pas « {} »{} dans Discord.".format(target, where))
    if best[1] != query:  # meilleur résultat trouvé par une recherche précédente
        results = _quick_switch(window, best[1]) or []
        found = scores(results)
        best = (best[0], best[1], found.index(max(found)) if found and max(found) else -1)
        if best[2] < 0:
            _close_switcher()
            return Result(False, "Je ne retrouve plus « {} » dans Discord.".format(target))
    name = (results[best[2]].Name or "").partition(",")[0]
    before = _discord_place()
    _choose(best[2])
    # Le titre change ; il peut porter un autre nom que le résultat (surnom sur un serveur). Inchangé, c'était
    # déjà la conversation affichée.
    if _until(lambda: _discord_place() != before, 3) or _same(before[0].lstrip("#@"), name) >= 1.8:
        return Result(True, name)
    return Result(False, "Discord n'a pas ouvert « {} ».".format(name))


def _discord_goto(window: win.Window, target: str, server: str = "") -> Result:
    """Affiche la conversation privée, le salon ou le serveur demandé ; Result(True, « #général de Dan's office »,
    « @Nour » ou « @Mat (Mathis) » si le titre porte un autre nom que le résultat choisi)."""
    target = target.strip().lstrip("#@*").strip()
    chosen = ""
    if server:
        opened = _discord_server(window, server)
        if not opened.ok:
            # Serveur introuvable : il a pu être ajouté à tort (celui affiché à l'écran) ; une personne se
            # trouve sans lui, un salon non.
            person = _switch_to(window, target, "", people_only=True) if target else None
            if not person or not person.ok:
                return opened
            chosen, target, server = person.message, "", ""
        else:
            server = opened.message
    if target and not _here(target, server) and not (server and _channel_link(window, target)):
        opened = _switch_to(window, target, server)
        if not opened.ok:
            return opened
        chosen = opened.message
    place, current = _discord_place()
    label = _label(place, current)
    if chosen and _same(place.lstrip("#@"), chosen) < 2:
        label += " ({})".format(chosen)
    return Result(True, label)


def _to(label: str) -> str:
    return ("à " if label.startswith("@") else "dans ") + label


def _discord_composer(window: win.Window):
    import uiautomation as auto

    return _first(auto.ControlFromHandle(window.hwnd), lambda c: c.ControlTypeName == "EditControl" and norm(
        c.Name or "").startswith(("envoyer un message", "message ")))


def _discord_messages(window: win.Window) -> tuple[str, list[str]] | None:
    """Conversation affichée : (nom de la liste, « - auteur : texte » du plus ancien au plus récent)."""
    import uiautomation as auto

    found = _first(auto.ControlFromHandle(window.hwnd), lambda c: c.ControlTypeName == "ListControl"  # pas la
                   and re.match("messages (sur|dans|de|in|for) ", norm(c.Name or "")))       # liste « Messages privés »
    if found is None:
        return None
    lines, author = [], "?"
    for item in found.GetChildren():  # l'auteur n'est écrit qu'au premier message d'une série
        if item.ControlTypeName != "ListItemControl" or not (item.AutomationId or "").startswith("chat-messages"):
            continue
        user = _walk(item, lambda c: (c.AutomationId or "").startswith("message-username"), limit=200, first=True)
        content = _walk(item, lambda c: (c.AutomationId or "").startswith("message-content"), limit=200, first=True)
        if user:  # le nom est un bouton (ouvre le profil), suivi du « Tag du serveur »
            names = _walk(user[0], lambda c: c.ControlTypeName in ("ButtonControl", "TextControl") and (c.Name or "")
                          .strip() and not norm(c.Name).startswith(("tag du serveur", "server tag")), limit=50, first=True)
            author = names[0].Name.strip() if names else author
        text = _text(content[0]) if content else ""
        if text:
            lines.append("- {} : {}".format(author, text[:300]))
    return found.Name, lines


def _discord_last(window: win.Window) -> str:
    shown = _discord_messages(window)
    return shown[1][-1] if shown and shown[1] else ""


def discord_open(target: str = "", server: str = "") -> Result:
    window = _window("discord.exe")
    if window is None:
        return _NO_DISCORD
    opened = _discord_goto(window, target, server) if target or server else Result(True, _label(*_discord_place()))
    if opened.ok:
        win.focus(window.hwnd)
        return Result(True, "J'ai ouvert {} sur Discord.".format(opened.message) if opened.message else
                      "J'affiche Discord.")
    return opened


def _copied_request(message: str, recipient: str) -> Result | None:
    """Le modèle a recopié la demande au lieu d'écrire le message (« demande à maman sur whatsapp si elle vient
    ce soir ») : refusé avant toute action, avec de quoi corriger."""
    text = norm(message)
    if re.match(r"(demande|dis|envoie|ecris|previens|reponds?)\b", text) and (
            norm(recipient) in text or re.search(r"\b(whatsapp|discord)\b", text)):
        # Sans exemple : le modèle le recopierait (« Tu viens ? » au lieu de « Tu viens ce soir ? »).
        return Result(False, "Le message recopie la demande de l'utilisateur. Rappelle l'outil avec seulement le "
                             "texte à envoyer, écrit directement à {} en le tutoyant, avec tous les détails de la "
                             "demande.".format(recipient))
    return None


def _prepare_discord(target: str, message: str, server: str = "") -> Result:
    copied = _copied_request(message, target)
    if copied:
        return copied
    window = _window("discord.exe")
    if window is None:
        return _NO_DISCORD
    opened = _discord_goto(window, target, server)
    return Result(True, "J'envoie « {} » {} sur Discord ?".format(message, _to(opened.message))) if opened.ok else opened


def discord_send(target: str, message: str, server: str = "") -> Result:
    window = _window("discord.exe")
    if window is None:
        return _NO_DISCORD
    opened = _discord_goto(window, target, server)  # déjà fait par la préparation : vérifie seulement le titre
    if not opened.ok:
        return opened
    composer = _until(lambda: _discord_composer(window), 3)
    if composer is None:
        return Result(False, "J'ai ouvert {}, mais sans champ de message (salon en lecture seule ?) : rien "
                             "n'est parti.".format(opened.message))
    failed = _write(window, composer, message)
    if failed:
        return failed
    if not _send(lambda: _discord_composer(window), message, lambda: _discord_last(window)):
        return Result(False, _UNSURE.format(_to(opened.message)), retryable=False)
    return Result(True, "J'ai envoyé « {} » {} sur Discord.".format(message, _to(opened.message)))


def discord_read(target: str = "", server: str = "", count: int = 10) -> Result:
    window = _window("discord.exe")
    if window is None:
        return Result(False, "Discord n'est pas ouvert.")
    if target or server:
        opened = _discord_goto(window, target, server)
        if not opened.ok:
            return opened
    shown = _until(lambda: _discord_messages(window), 3)
    if shown is None:
        return Result(False, "Aucune conversation Discord n'est affichée.")
    name, lines = shown
    if not lines:
        return Result(True, "{} : aucun message.".format(name))
    count = max(1, min(count, 30))
    return Result(True, "{} (les {} derniers) :\n{}".format(name, min(count, len(lines)), "\n".join(lines[-count:])))


# ---- WhatsApp ----

def _whatsapp_window() -> win.Window | None:
    """La fenêtre de WhatsApp, ouverte au besoin (l'application reste souvent cachée dans la barre)."""
    window = _window("whatsapp.root.exe")
    if window is None:
        os.startfile(_WHATSAPP_APP)
        window = _until(lambda: _window("whatsapp.root.exe"), 10, every=0.5)
    return window


def _whatsapp_root(window: win.Window):
    """La page de WhatsApp (son document web) : l'arbre de la fenêtre la contient parfois deux fois. Attendue
    jusqu'à 8 s, le temps que WhatsApp construise son arbre d'accessibilité."""
    import uiautomation as auto

    root = auto.ControlFromHandle(window.hwnd)
    return _until(lambda: _first(root, lambda c: c.ControlTypeName == "DocumentControl", limit=300), 8) or root


def _is_composer(control) -> bool:
    """Le champ de message garde son nom (« Écrivez un message à Maman ») ; la recherche n'a que son texte
    indicatif (« Rechercher… »), qui disparaît dès qu'elle contient du texte."""
    return control.ControlTypeName == "EditControl" and norm(control.Name or "").startswith(
        ("ecrivez un message", "ecrire un message", "type a message"))


def _is_search(control) -> bool:
    return control.ControlTypeName == "EditControl" and not _is_composer(control)


def _whatsapp_composer(root):
    return _first(root, _is_composer, limit=6000)


def _whatsapp_with(composer) -> str:
    """« Écrivez un message à Maman❤️💯 Dupont » -> « Maman❤️💯 Dupont »."""
    name = " ".join((composer.Name or "").split())
    match = re.match(r"(?i)(?:[ée]crivez un message à|[ée]crire un message à|type a message to)\s+(.+)", name)
    return match[1] if match else name


def _whatsapp_rows(root) -> list[tuple[str, Any]]:
    """(nom affiché, ligne) des discussions et contacts listés, dans l'ordre de WhatsApp (récents d'abord).
    Les résultats d'une recherche sont rangés en sections : « Contacts » donne aussi les contacts sans
    discussion ; « Groupes en commun » (un membre porte ce nom) et « Messages » (un message le cite) sont écartés."""
    grids = _walk(root, lambda c: c.ControlTypeName == "DataGridControl", limit=2000)
    grid = next((g for g in grids if norm(g.Name or "").startswith(("resultats", "search results"))),
                grids[0] if grids else None)
    rows, section = [], ""
    for item in grid.GetChildren() if grid else []:
        name = " ".join((item.Name or "").split())
        if item.ControlTypeName != "DataItemControl" or not name:
            continue
        if _WA_SECTION.match(name):
            section = norm(name)
        elif section not in ("groupes en commun", "messages", "groups in common"):
            rows.append((_WA_ROW.match(name)["name"], item))
    return rows


def _whatsapp_last(root) -> str:
    """Texte du dernier message de la discussion : la dernière ligne avant le champ de message (la liste des
    discussions vient avant la conversation)."""
    last = None
    for i, (control, _) in enumerate(_safe_walk(root)):
        if i > 6000:
            break
        try:
            kind = control.ControlTypeName
            if _is_composer(control):
                break
            if kind == "DataItemControl":
                last = control
        except Exception:
            continue
    return _text(last) if last is not None else ""


def _whatsapp_goto(contact: str) -> Result:
    """Ouvre la discussion du contact ou du groupe (le plus ressemblant, le plus récent à égalité) ;
    Result(True, nom affiché) une fois le bon champ de message à l'écran."""
    if not _plain(contact):
        return Result(False, "Il me faut le nom du contact.")
    window = _whatsapp_window()
    if window is None:
        return Result(False, "WhatsApp ne s'ouvre pas.")
    root = _whatsapp_root(window)

    def chat(name: str):
        composer = _whatsapp_composer(root)
        return composer if composer is not None and _same(_whatsapp_with(composer), name) >= 2 else None

    composer = chat(contact)  # déjà ouverte
    if composer is None:
        win.focus(window.hwnd)
        search = _until(lambda: _first(root, _is_search, limit=4000), 8)
        if search is None:
            return Result(False, "La recherche de WhatsApp n'apparaît pas.")
        picked = None
        for query in _variants(contact):  # Whisper écorche les noms : « Hugo » écrit « Ugo »
            _type_search(search, query)
            rows = _stable(lambda: _whatsapp_rows(root), 4, key=lambda row: row[0], allow_empty=True)
            scores = [_same(name, contact) for name, _ in rows]
            if scores and max(scores):
                picked = rows[scores.index(max(scores))]
                break
        if picked is None:
            _type_search(search, "")
            return Result(False, "Je ne trouve pas « {} » dans WhatsApp.".format(contact))
        name, row = picked
        _click(row)
        composer = _until(lambda: chat(name), 4)
        _type_search(search, "")  # la liste des discussions redevient complète
        if composer is None:
            return Result(False, "La discussion avec « {} » ne s'est pas ouverte.".format(name))
    return Result(True, _whatsapp_with(composer))


def _type_search(search, text: str) -> None:
    search.Click(simulateMove=False)
    press(VK["ctrl"], ord("A"))
    if text:
        _paste(text)
        time.sleep(0.4)
    else:
        press(VK["backspace"])


def _click(control) -> None:
    """Clic réel, après avoir fait défiler la liste si l'élément est hors de vue."""
    import uiautomation as auto

    if control.IsOffscreen:
        pattern = control.GetPattern(auto.PatternId.ScrollItemPattern)
        if pattern:
            pattern.ScrollIntoView()
            time.sleep(0.3)
    control.Click(simulateMove=False)


def _prepare_whatsapp(contact: str, message: str) -> Result:
    copied = _copied_request(message, contact)
    if copied:
        return copied
    opened = _whatsapp_goto(contact)
    return Result(True, "J'envoie « {} » à {} sur WhatsApp ?".format(message, opened.message)) if opened.ok else opened


def whatsapp_send(contact: str, message: str) -> Result:
    opened = _whatsapp_goto(contact)  # déjà fait par la préparation : vérifie seulement le champ
    if not opened.ok:
        return opened
    window = _whatsapp_window()
    root = _whatsapp_root(window)
    composer = _whatsapp_composer(root)
    failed = _write(window, composer, message) if composer is not None else \
        Result(False, "La discussion avec « {} » s'est fermée : rien n'est parti.".format(opened.message))
    if failed:
        return failed
    if not _send(lambda: _whatsapp_composer(root), message, lambda: _whatsapp_last(root)):
        return Result(False, _UNSURE.format("dans la discussion avec " + opened.message), retryable=False)
    return Result(True, "J'ai envoyé « {} » à {} sur WhatsApp.".format(message, opened.message))


def whatsapp_unread() -> Result:
    """Discussions avec des messages non lus, lues dans la liste sans les ouvrir (pas d'accusé de lecture)."""
    window = _whatsapp_window()
    if window is None:
        return Result(False, "WhatsApp ne s'ouvre pas.")
    root = _whatsapp_root(window)
    if not _until(lambda: _first(root, lambda c: c.ControlTypeName == "DataItemControl", limit=4000), 8):
        return Result(False, "La liste des discussions de WhatsApp n'apparaît pas.")
    rows = _walk(root, lambda c: c.ControlTypeName == "DataItemControl" and re.match(r"\d+\s+messages? non lus?",
                                                                                   norm(c.Name or "")), limit=6000)
    names = list(dict.fromkeys(" ".join(r.Name.split())[:160] for r in rows))  # lignes imbriquées en double
    if not names:
        return Result(True, "Aucun message non lu sur WhatsApp.")
    return Result(True, "Discussions WhatsApp avec des messages non lus :\n" + "\n".join("- " + n for n in names))


# ---- Garde-fous ----

def _guard_discord(target: str, message: str, server: str = "") -> Check:
    return confirm("J'envoie « {} » à {}{} sur Discord ?".format(
        message, target, " sur le serveur {}".format(server) if server else ""))


def _guard_whatsapp(contact: str, message: str) -> Check:
    return confirm("J'envoie « {} » à {} sur WhatsApp ?".format(message, contact))


def register(box: Toolbox) -> None:
    box.add(Tool("discord_send",
                 "Envoie un message Discord, en privé à une personne (« envoie coucou à Nour sur Discord ») ou "
                 "dans un salon d'un serveur (« écris salut dans le salon général du serveur EPITECH »). "
                 "L'utilisateur confirme avant l'envoi.", discord_send, {
                     "target": Param("personne ou salon, tel que dit (« Nour », « général »)"),
                     "message": Param(_MESSAGE),
                     "server": Param(_SERVER, required=False),
                 }, private=True, guard=_guard_discord, prepare=_prepare_discord))
    box.add(Tool("discord_open",
                 "Affiche dans Discord une conversation privée, un salon d'un serveur ou un serveur (« ouvre le "
                 "salon général d'EPITECH », « va sur la conv avec Nour », « ouvre le serveur EPITECH »).",
                 discord_open, {
                     "target": Param("personne ou salon ; vide pour ouvrir seulement le serveur", required=False),
                     "server": Param(_SERVER, required=False),
                 }))
    box.add(Tool("discord_read",
                 "Lit les derniers messages Discord d'une conversation privée ou d'un salon (« qu'est-ce que "
                 "Nour m'a écrit ? », « lis le salon général d'EPITECH ») ; sans nom, la conversation affichée.",
                 discord_read, {
                     "target": Param("personne ou salon ; vide pour la conversation affichée", required=False),
                     "server": Param(_SERVER, required=False),
                     "count": Param("nombre de messages (10 par défaut)", type="integer", required=False),
                 }, reads=True, untrusted=True))
    box.add(Tool("whatsapp_send",
                 "Envoie un message WhatsApp à un contact ou un groupe (« envoie j'arrive à Maman sur WhatsApp »). "
                 "L'utilisateur confirme avant l'envoi.", whatsapp_send, {
                     "contact": Param("nom du contact ou du groupe, tel que dit"),
                     "message": Param(_MESSAGE),
                 }, private=True, guard=_guard_whatsapp, prepare=_prepare_whatsapp))
    box.add(Tool("whatsapp_unread",
                 "Dit qui a écrit sur WhatsApp (discussions avec des messages non lus et leur aperçu), sans "
                 "ouvrir les discussions.", whatsapp_unread, reads=True, untrusted=True))
