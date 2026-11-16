"""Commandes fréquentes reconnues sans le modèle : instantanées et toujours exactes.

Seules les phrases courtes et sans ambiguïté passent ici (« pause », « monte le son », « bonne nuit »,
« mets un pouce »). La phrase entière doit correspondre : « mets en pause et lance ma playlist »
va au modèle, qui sait enchaîner.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from jarvis.agent import _DAYS, _MONTHS
from jarvis.tools import norm


@dataclass
class Shortcut:
    tool: str | None                  # None : réponse directe, sans outil
    args: dict[str, Any] = field(default_factory=dict)
    answer: str = ""


# Mots autour de la commande : « Jarvis, monte le son s'il te plaît », « tu peux mettre pause ? »
_AROUND = re.compile(r"^(?:(?:hey|ok|okay|dis|eh|oh) )?jarvis |^(?:est-ce que tu peux|tu peux|tu pourrais|peux-tu|"
                     r"pourrais-tu|je veux que tu|j'aimerais que tu|je voudrais que tu|vas-y|allez) "
                     r"| (?:s'il te plait|s'il vous plait|stp|svp|merci|jarvis|maintenant|vite)$")
_V = {  # verbes à l'impératif, à l'infinitif (« tu peux mettre ») ou au subjonctif (« que tu mettes »)
    "mettre": r"(?:mets|met|mettre|mettes)", "monter": r"(?:monte|monter|montes)",
    "baisser": r"(?:baisse|baisser|baisses)", "couper": r"(?:coupe|couper|coupes)",
    "passer": r"(?:passe|passer|passes)", "fermer": r"(?:ferme|fermer|fermes)",
    "reprendre": r"(?:reprends|reprend|reprendre|reprennes)", "faire": r"(?:fais|fait|faire|fasses)",
    "afficher": r"(?:affiche|afficher|affiches|montre|montrer|montres)(?: moi|-moi)?",
    "ouvrir": r"(?:ouvre|ouvrir|ouvres|va dans|va sur)", "remettre": r"(?:remets|remet|remettre|remettes)",
    "avancer": r"(?:avance|avancer|avances)", "reculer": r"(?:recule|reculer|recules)",
    "activer": r"(?:active|activer|actives|enleve|enlever|desactive|desactiver|affiche|afficher|coupe|couper)",
    "cocher": r"(?:coche|cocher|coches|termine|terminer|ferme|fermer|valide|valider|finis|finir)",
}
_TODO_WHERE = r"(?: (?:dans|sur|de) (?:ma |la )?(?:todo(?: ?list)?|to do|liste de taches|microsoft to do))"
_SONG = r"(?:(?:la |le |cette |ce )?(?:chanson|musique|morceau|titre|piste|son|video) |la |le )?"
# Google Agenda : périodes reconnues dans les lectures directes (le calcul des dates se fait dans tools/dates.py)
_PERIOD = (r"(?P<when>aujourd'hui|demain(?: matin| soir| apres-midi)?|apres-demain|ce matin|cet apres-midi|ce soir"
           r"|cette semaine|la semaine prochaine|la semaine|ce week-end|ce weekend|le week-end prochain"
           r"|(?:ce |le )?(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)(?: prochain)?"
           r"(?: matin| soir| apres-midi)?)")
_TELL = r"(?:c'est quoi |quel est |quels sont |donne-moi |donne moi |dis-moi |dis moi |lis-moi |lis moi |lis )?"
_IN_AGENDA = r"(?: (?:dans|sur) (?:mon|l') ?(?:agenda|planning|calendrier))?"
_MEDIA = r"(?: (?:la|ma) musique| la video| spotify| youtube| la chanson| le morceau| le son)?"


def _seconds(match: re.Match) -> int:
    """« de 30 secondes », « d'une minute », « de 2 minutes » -> secondes (10 par défaut)."""
    if not match["amount"]:
        return 10
    amount = 1 if match["amount"] in ("une", "un") else int(match["amount"])
    return amount * 60 if match["unit"].startswith("minute") else amount


_AMOUNT = r"(?: la video)?(?: (?:de|d') ?(?P<amount>\d+|une|un) (?P<unit>secondes?|minutes?))?"


def _clock(now: datetime.datetime) -> str:
    return "Il est {} h {:02d}.".format(now.hour, now.minute)


def _date(now: datetime.datetime) -> str:
    return "Nous sommes le {} {} {} {}.".format(_DAYS[now.weekday()], now.day, _MONTHS[now.month - 1], now.year)


_RULES: list[tuple[str, Callable[[re.Match], Shortcut]]] = [
    # Musique et vidéo (Spotify par son API s'il joue, sinon touches multimédia : YouTube, VLC...)
    (r"pause|stop|{m} (?:(?:la|ma) musique |la video |spotify |youtube |la chanson |le morceau )?en pause{media}"
     r"|{f} (?:une |la )?pause|{m} pause{media}|pause{media}|{c} la musique|arrete la musique|stop la musique"
     .format(m=_V["mettre"], f=_V["faire"], c=_V["couper"], media=_MEDIA),
     lambda m: Shortcut("media", {"command": "pause"})),
    (r"{r}{media}|{r} la lecture|play|lecture|{rm} la musique|relance la musique|enleve la pause"
     r"|{m} (?:la )?lecture".format(m=_V["mettre"], r=_V["reprendre"], rm=_V["remettre"], media=_MEDIA),
     lambda m: Shortcut("media", {"command": "play"})),
    (r"(?:{p} (?:a )?)?{song}suivante?|next|skip|zappe|{p}(?: cette chanson| ce morceau| ce son)?"
     r"|change de (?:chanson|musique|morceau)".format(p=_V["passer"], song=_SONG),
     lambda m: Shortcut("media", {"command": "next"})),
    (r"(?:(?:{p}|{rm}|{m}) (?:a )?)?{song}(?:precedente?|d'avant)|previous".format(
        p=_V["passer"], rm=_V["remettre"], m=_V["mettre"], song=_SONG),
     lambda m: Shortcut("media", {"command": "previous"})),
    # Son
    (r"(?:{mo}|augmente|augmenter) (?:un peu )?(?:le son|le volume|la musique)(?: un peu)?|{mo}|plus fort"
     r"|{m} (?:le son )?plus fort|{mo} un peu".format(mo=_V["monter"], m=_V["mettre"]),
     lambda m: Shortcut("volume", {"level": "up"})),
    (r"(?:{b}|diminue|diminuer) (?:un peu )?(?:le son|le volume|la musique)(?: un peu)?|{b}|moins fort"
     r"|(?:c'est )?(?:beaucoup |un peu |vraiment )?trop fort|{m} (?:le son )?moins fort|{b} un peu".format(
         b=_V["baisser"], m=_V["mettre"]),
     lambda m: Shortcut("volume", {"level": "down"})),
    (r"{c} le son|enleve le son|mute|unmute|silence|{m} (?:en )?sourdine|{rm} le son".format(
        c=_V["couper"], m=_V["mettre"], rm=_V["remettre"]),
     lambda m: Shortcut("volume", {"level": "mute"})),
    (r"(?:(?:{m}|regle|regler) )?(?:le )?(?:son|volume) (?:a |au )?(?P<level>\d{{1,3}})(?: ?%| pour ?cent)?".format(
        m=_V["mettre"]),
     lambda m: Shortcut("volume", {"level": m["level"]})),
    (r"(?:{m} )?(?:le )?(?:son|volume) (?:a fond|au max(?:imum)?)".format(m=_V["mettre"]),
     lambda m: Shortcut("volume", {"level": "100"})),
    # YouTube (vidéo affichée dans le navigateur)
    (r"(?:{m} |laisse )?(?:un )?(?:pouce|like|j'aime)(?: (?:(?:a|sur|pour) )?(?:cette|la) video)?"
     r"|like (?:cette|la) video|{m} un pouce (?:bleu|en l'air)".format(m=_V["mettre"]),
     lambda m: Shortcut("youtube_control", {"action": "like"})),
    (r"(?:{m} )?(?:la video )?(?:en )?plein ecran|(?:quitte|quitter|enleve|enlever|sors|sortir) (?:du |le )?plein ecran"
     .format(m=_V["mettre"]),
     lambda m: Shortcut("youtube_control", {"action": "fullscreen"})),
    (r"{a}{amount}".format(a=_V["avancer"], amount=_AMOUNT),
     lambda m: Shortcut("youtube_control", {"action": "forward", "seconds": _seconds(m)})),
    (r"(?:{r}(?: en arriere)?|(?:reviens|revenir) en arriere){amount}".format(r=_V["reculer"], amount=_AMOUNT),
     lambda m: Shortcut("youtube_control", {"action": "back", "seconds": _seconds(m)})),
    (r"(?:(?:{a}|{m}) )?(?:les )?sous[- ]?titres?(?: (?:de|sur) (?:la|cette) video)?".format(
        a=_V["activer"], m=_V["mettre"]),
     lambda m: Shortcut("youtube_control", {"action": "subtitles"})),
    # Bureau et navigateur
    (r"(?:{f}|prends|prendre) (?:une )?capture(?: d'ecran)?|capture d'ecran|screenshot".format(f=_V["faire"]),
     lambda m: Shortcut("screenshot")),
    (r"{a} le bureau|reduis tout|reduis toutes les fenetres".format(a=_V["afficher"]),
     lambda m: Shortcut("show_desktop")),
    (r"{f} (?:cet |l'|l' |le dernier )?onglet".format(f=_V["fermer"]), lambda m: Shortcut("close_tab")),
    # Steam
    (r"(?:{o}|{a}) (?:ma |la )?biblio(?:theque)?(?: de jeux)? (?:de |sur )?steam".format(o=_V["ouvrir"], a=_V["afficher"]),
     lambda m: Shortcut("steam_open", {"page": "library"})),
    (r"(?:(?:{o}|{a}) )?(?:s'il y a |si il y a |si j'ai |est-ce qu'il y a |y a-t-il |il y a |j'ai )?(?:des |les |mes )?"
     r"(?:telechargements|mises a jour)(?: (?:de|des|pour) (?:mes )?jeux)? (?:de |sur |dans )?steam".format(
         o=_V["ouvrir"], a=_V["afficher"]),
     lambda m: Shortcut("steam_open", {"page": "downloads"})),
    # Microsoft To Do (ajouter reste au modèle : le titre de la tâche doit garder ses accents et majuscules)
    (r"{o} (?:ma |la |mes )?(?:todo(?: ?list)?|to do(?: list)?|liste de taches|microsoft to do)".format(o=_V["ouvrir"]),
     lambda m: Shortcut("todo", {"action": "open"})),
    (r"(?:{t}|j'ai (?:fini|termine)|c'est fait pour) (?:la )?tache (?P<task>.+?){w}|{t} (?P<other>.+?){where}".format(
        t=_V["cocher"], w=_TODO_WHERE + "?", where=_TODO_WHERE),
     lambda m: Shortcut("todo", {"action": "complete", "task": m["task"] or m["other"]})),
    (r"(?:rouvre|rouvrir|reouvre|reouvrir|decoche|decocher) (?:la )?tache (?P<task>.+?){w}".format(w=_TODO_WHERE + "?"),
     lambda m: Shortcut("todo", {"action": "reopen", "task": m["task"]})),
    # Google Agenda : lectures seulement (créer, déplacer, supprimer restent au modèle, avec les confirmations)
    (r"(?:qu'est-ce que j'ai|qu'est ce que j'ai|j'ai quoi|qu'ai-je|qu'est-ce qui est prevu|qu'est ce qui est prevu)"
     r"(?: de prevu| au programme)?{i} {p}{i}".format(p=_PERIOD, i=_IN_AGENDA),
     lambda m: Shortcut("agenda", {"action": "list", "when": m["when"]})),
    (r"{t}mon (?:planning|agenda|programme|emploi du temps)(?: (?:de |d'|pour )?{p})?".format(t=_TELL, p=_PERIOD),
     lambda m: Shortcut("agenda", {"action": "list", "when": m["when"] or "aujourd'hui"})),
    (r"{t}mes (?:creneaux libres|creneaux de libre|disponibilites|dispos)(?: (?:pour )?{p})?".format(t=_TELL, p=_PERIOD),
     lambda m: Shortcut("agenda", {"action": "free", "when": m["when"] or "aujourd'hui"})),
    (r"quand (?:est-ce que )?(?:je suis|suis-je) (?:libre|dispo|disponible)(?: {p})?".format(p=_PERIOD),
     lambda m: Shortcut("agenda", {"action": "free", "when": m["when"] or "cette semaine"})),
    (r"(?:est-ce que )?(?:je suis|suis-je) (?:libre|dispo|disponible) {p}".format(p=_PERIOD),
     lambda m: Shortcut("agenda", {"action": "free", "when": m["when"]})),
    (r"{t}(?:mes|les) calendriers|liste mes calendriers|combien (?:j'ai de|ai-je de) calendriers".format(t=_TELL),
     lambda m: Shortcut("agenda", {"action": "calendars"})),
    # Veille
    (r"bonne nuit|{m}[- ]toi en veille|tu peux te mettre en veille|(?:va|passe) en veille|au revoir|a plus(?: tard)?"
     r"|a demain|a toute|bye|dors|veille".format(m=_V["mettre"]),
     lambda m: Shortcut("jarvis_sleep")),
    # Heure et date
    (r"quelle heure (?:est-il|il est)|il est quelle heure|(?:t'as|tu as) l'heure|donne(?:-moi| moi) l'heure",
     lambda m: Shortcut(None, answer=_clock(datetime.datetime.now()))),
    (r"(?:on est|nous sommes) quel jour(?: aujourd'hui)?|quel jour (?:on est|sommes-nous|est-on|nous sommes)"
     r"(?: aujourd'hui)?|(?:c'est quoi|quelle est) la date(?: d'aujourd'hui)?|on est le combien(?: aujourd'hui)?",
     lambda m: Shortcut(None, answer=_date(datetime.datetime.now()))),
]
_COMPILED = [(re.compile(pattern), build) for pattern, build in _RULES]

# Interdits de l'utilisateur, refusés sans même consulter le modèle (les outils les bloquent aussi).
# Le verbe doit ouvrir la demande : « quels jeux sont installés ? » n'est pas « installe ».
_FORBIDDEN = [(re.compile(pattern), answer) for pattern, answer in [
    (r"^(?:eteins|eteindre|redemarre|redemarrer|arrete|arreter|reboot)\b.*\b(?:ordinateur|ordi|pc|windows|machine)\b",
     "Je n'ai pas le droit d'éteindre ni de redémarrer le PC."),
    # Un événement d'agenda n'est pas un fichier : « supprime mon rendez-vous au bureau » va à l'outil agenda.
    (r"^(?!.*\b(?:rendez-vous|rendez vous|rdv|reunion|evenement|cours|seance|agenda|calendrier|planning"
     r"|invitation)s?\b)"
     r"(?:supprime|supprimer|efface|effacer|vide|vider|formate|formater)\b(?: \S+){0,4} (?:fichiers?|dossiers?|corbeille"
     r"|telechargements|documents?|photos?|bureau|disque)\b|\brm -rf\b|\bdel /",
     "Je ne supprime jamais de fichiers ni de dossiers."),
    (r"^(?:des)?install(?:e|er)\b", "Je n'installe ni ne désinstalle de logiciels."),
    (r"^(?:change|changer|modifie|modifier|reinitialise|reinitialiser)\b.*\bmot de passe\b",
     "Je ne touche pas aux mots de passe."),
    (r"^(?:passe|passer|mets|met|mettre|change|changer|modifie|modifier|active|activer|desactive|desactiver|regle"
     r"|regler)\b.*\bparametres\b|^(?:desactive|desactiver|coupe|couper|arrete|arreter)\b.*\b(?:antivirus"
     r"|pare-feu|firewall|defender)\b",
     "Je ne modifie pas les paramètres de Windows."),
]]


def clean(text: str) -> str:
    """« Jarvis, monte le son s'il te plaît ! » -> « monte le son »."""
    text = re.sub(r"[.!?,;:…]+", " ", norm(text).replace("’", "'"))
    text = " ".join(text.split())
    while True:
        shorter = _AROUND.sub("", text).strip()
        if shorter == text:
            return text
        text = shorter


def match(text: str) -> Shortcut | None:
    phrase = clean(text)
    for pattern, answer in _FORBIDDEN:
        if pattern.search(phrase):
            return Shortcut(None, answer=answer)
    for pattern, build in _COMPILED:
        found = pattern.fullmatch(phrase)
        if found:
            return build(found)
    return None
