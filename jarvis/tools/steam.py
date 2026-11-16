"""Steam : bibliothèque (lue sur le disque), lancement des jeux, magasin en ligne."""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.parse
import winreg
from pathlib import Path

from jarvis.tools import Param, Result, Tool, Toolbox, best_match, norm
from jarvis.tools.web import fetch

_STORE = "https://store.steampowered.com"
_LOCALE = "cc=FR&l=french"
_NOT_GAMES = re.compile(r"redistributable|proton|steam linux runtime|steamworks", re.I)
_TAGS = {"coop": 1685, "coop en ligne": 3843, "coop locale": 3841, "multijoueur": 3859, "survie": 1662,
         "horreur": 1667, "rpg": 122, "fps": 1663, "monde ouvert": 1695, "indépendant": 492,
         "simulation": 599, "stratégie": 9, "course": 699, "sport": 701, "puzzle": 1664,
         "aventure": 21, "action": 19, "roguelike": 1716, "bac à sable": 3810, "construction": 7332}


def _steam_dir() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return None


def _vdf_values(text: str, key: str) -> list[str]:
    return re.findall(r'"{}"\s+"([^"]*)"'.format(key), text)


def installed_games() -> list[dict]:
    """Jeux installés, avec temps de jeu et dernière partie (compte Steam le plus récent)."""
    steam = _steam_dir()
    if steam is None:
        return []
    libraries = {steam}
    folders = steam / "steamapps" / "libraryfolders.vdf"
    if folders.exists():
        libraries |= {Path(p.replace("\\\\", "\\")) for p in _vdf_values(folders.read_text("utf-8", "replace"), "path")}

    games = {}
    for library in libraries:
        for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
            text = manifest.read_text("utf-8", "replace")
            appid, name = _vdf_values(text, "appid"), _vdf_values(text, "name")
            if appid and name and not _NOT_GAMES.search(name[0]):
                games[appid[0]] = {"appid": appid[0], "name": name[0], "playtime": 0, "last_played": 0}

    configs = sorted((steam / "userdata").glob("*/config/localconfig.vdf"), key=os.path.getmtime)
    if configs:
        text = configs[-1].read_text("utf-8", "replace")
        for appid, block in re.findall(r'"(\d+)"\s*\{\s*((?:[^{}]|\{[^{}]*\})*)\}', text):
            if appid in games:
                played, playtime = _vdf_values(block, "LastPlayed"), _vdf_values(block, "Playtime")
                games[appid]["last_played"] = int(played[0]) if played else 0
                games[appid]["playtime"] = int(playtime[0]) if playtime else 0
    return sorted(games.values(), key=lambda g: g["last_played"], reverse=True)


def steam_library() -> Result:
    games = installed_games()
    if not games:
        return Result(False, "Je ne trouve pas de jeux Steam installés.")
    lines = []
    for g in games:
        played = datetime.date.fromtimestamp(g["last_played"]).strftime("%d/%m/%Y") if g["last_played"] else "jamais"
        lines.append("- {} : {:.0f} h de jeu, dernière partie {}".format(g["name"], g["playtime"] / 60, played))
    most_played = sorted(games, key=lambda g: g["playtime"], reverse=True)[:5]
    return Result(True, "{} jeux installés.\nLes plus joués : {}.\nDu plus récemment joué au plus ancien :\n{}".format(
        len(games), ", ".join("{} ({:.0f} h)".format(g["name"], g["playtime"] / 60) for g in most_played),
        "\n".join(lines)))


def _store_search(term: str) -> list[dict]:
    data = json.loads(fetch("{}/api/storesearch/?term={}&{}".format(_STORE, urllib.parse.quote(term), _LOCALE)))
    return data.get("items", [])


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _words(text: str) -> list[str]:
    """« Age of Empires II: Definitive Edition » -> age of empires 2 definitive edition."""
    return [_ROMAN.get(w, w) for w in re.findall(r"[a-z0-9]+", norm(text))]


def _numbers(text: str) -> set[str]:
    return {w for w in _words(text) if w.isdigit()}


def _find_installed(game: str, games: list[dict]) -> dict | None:
    """Jeu installé qui correspond au nom dit : « Age of Empires 2 », « AoE 2 », « cs2 ». Un autre
    numéro n'est pas le même jeu : « Age of Empires 4 » ne lance pas le II."""
    names, sigles = {}, {}
    for g in games:
        names.setdefault(" ".join(_words(g["name"])), g)
        title = _words(re.split(r":| - | – ", g["name"])[0])  # sans le sous-titre
        if len(title) >= 2:
            sigles.setdefault("".join(w if w.isdigit() else w[0] for w in title), g)
    spoken = " ".join(_words(game))
    found = sigles.get(spoken.replace(" ", "")) or best_match(spoken, names, cutoff=0.6)
    return None if found and _numbers(game) - _numbers(found["name"]) else found


# « Lance le premier », « lance-le » : le modèle a passé la référence au lieu du nom du jeu.
_VAGUE = re.compile(r"(?:le |la |l'|ce |cet )?(?:premier|premiere|deuxieme|second|troisieme|dernier|derniere"
                    r"|celui-la|celui-ci|celui la|ca|jeu|ce jeu|le jeu)")


def steam_launch(game: str) -> Result:
    if _VAGUE.fullmatch(norm(game)):
        return Result(False, "« {} » n'est pas un nom de jeu : relis la conversation et donne le nom exact du "
                             "jeu.".format(game))
    games = installed_games()
    found = _find_installed(game, games)
    results = [] if found else _store_search(game)
    if results and not found:  # le magasin reconnaît mieux les noms approchants
        found = next((g for g in games if g["appid"] == str(results[0]["id"])), None)
    if found:
        os.startfile("steam://rungameid/" + found["appid"])
        return Result(True, "Je lance {}.".format(found["name"]))
    if not results:
        return Result(False, "Je ne trouve pas « {} », ni dans ta bibliothèque, ni sur Steam.".format(game))
    os.startfile("steam://store/{}".format(results[0]["id"]))
    return Result(True, "{} n'est pas installé. J'ouvre sa page Steam.".format(results[0]["name"]))


_PAGES = {"library": ("steam://open/games", "ta bibliothèque Steam"),
          "downloads": ("steam://open/downloads", "les téléchargements et mises à jour Steam"),
          "store": ("steam://store", "le magasin Steam"),
          "friends": ("steam://open/friends", "ta liste d'amis Steam")}


def steam_open(page: str = "library") -> Result:
    url, label = _PAGES[page]
    os.startfile(url)  # Steam s'ouvre au besoin et se met au premier plan
    return Result(True, "J'ouvre {}.".format(label))


def _price(item: dict) -> str:
    final = item.get("final_price")
    if final is None:
        return ""
    if final == 0:
        return " (gratuit)"
    discount = item.get("discount_percent") or 0
    return " ({:.2f} €{})".format(final / 100, ", -{} %".format(discount) if discount else "")


def steam_store(category: str = "top_sellers", tag: str = "", query: str = "") -> Result:
    if query:
        items = _store_search(query)
        lines = ["- {} (id {})".format(i["name"], i["id"]) for i in items[:15]]
        title = "Résultats Steam pour « {} »".format(query)
    elif tag:
        tag_id = best_match(tag, {norm(k): v for k, v in _TAGS.items()}, cutoff=0.6)
        if tag_id is None:
            return Result(False, "Catégorie inconnue : {}. Disponibles : {}.".format(tag, ", ".join(_TAGS)))
        order = "sort_by=Released_DESC" if category == "new_releases" else "filter=topsellers"
        data = json.loads(fetch("{}/search/results/?tags={}&{}&infinite=1&category1=998&{}".format(
            _STORE, tag_id, order, _LOCALE)))
        page = data.get("results_html", "")
        names = re.findall(r'<span class="title">(.*?)</span>', page)
        dates = re.findall(r'<div class="search_released[^"]*">\s*(.*?)\s*</div>', page)
        lines = ["- {}{}".format(n, " (sortie : {})".format(d) if d else "") for n, d in zip(names, dates + [""] * len(names))][:20]
        title = "Jeux « {} » ({})".format(tag, "nouveautés" if category == "new_releases" else "meilleures ventes")
    else:
        data = json.loads(fetch("{}/api/featuredcategories?{}".format(_STORE, _LOCALE)))
        items = data.get(category, {}).get("items", [])
        seen, lines = set(), []
        for i in items:
            if i["name"] not in seen:
                seen.add(i["name"])
                lines.append("- {}{}".format(i["name"], _price(i)))
        title = {"top_sellers": "Meilleures ventes Steam", "new_releases": "Nouveautés Steam",
                 "specials": "Promotions Steam", "coming_soon": "Bientôt sur Steam"}.get(category, category)
    return Result(True, "{} :\n{}".format(title, "\n".join(lines) or "(aucun)"))


def steam_game_details(game: str) -> Result:
    results = _store_search(game)
    if not results:
        return Result(False, "Je ne trouve pas « {} » sur Steam.".format(game))
    appid = str(results[0]["id"])
    data = json.loads(fetch("{}/api/appdetails?appids={}&{}".format(_STORE, appid, _LOCALE)))[appid].get("data", {})
    if not data:
        return Result(False, "Pas de détails disponibles pour {}.".format(results[0]["name"]))
    price = data.get("price_overview", {}).get("final_formatted", "gratuit" if data.get("is_free") else "?")
    lines = [
        "{} — {}".format(data.get("name"), price),
        "Sortie : {}".format(data.get("release_date", {}).get("date", "?")),
        "Genres : {}".format(", ".join(g["description"] for g in data.get("genres", []))),
        "Modes : {}".format(", ".join(c["description"] for c in data.get("categories", [])[:10])),
        "Résumé : {}".format(re.sub(r"<[^>]+>", "", data.get("short_description", ""))),
    ]
    if data.get("metacritic"):
        lines.append("Metacritic : {}".format(data["metacritic"]["score"]))
    if data.get("recommendations"):
        lines.append("Avis positifs : {}".format(data["recommendations"]["total"]))
    return Result(True, "\n".join(lines))


def register(box: Toolbox) -> None:
    box.add(Tool("steam_open",
                 "Affiche une page dans l'application Steam : la bibliothèque (« ouvre ma bibliothèque "
                 "Steam »), les téléchargements et mises à jour des jeux (« y a-t-il des mises à jour de mes jeux "
                 "Steam ? »), le magasin ou les amis.", steam_open, {
                     "page": Param("page à afficher", enum=list(_PAGES), required=False, aliases={
                         "bibliotheque": "library", "jeux": "library", "telechargements": "downloads",
                         "mises a jour": "downloads", "updates": "downloads", "magasin": "store",
                         "boutique": "store", "amis": "friends"}),
                 }))
    box.add(Tool("steam_library",
                 "Donne la liste des jeux Steam installés, avec le temps de jeu et la dernière partie, pour "
                 "répondre (« à quoi j'ai le plus joué ? »). N'ouvre rien à l'écran : pour afficher la "
                 "bibliothèque dans Steam, steam_open.", steam_library, reads=True))
    box.add(Tool("steam_launch",
                 "Lance un jeu Steam installé. S'il n'est pas installé, ouvre sa page dans le magasin.",
                 steam_launch, {"game": Param("nom du jeu")}))
    box.add(Tool("steam_store",
                 "Consulte le magasin Steam : meilleures ventes, nouveautés, promotions, jeux à venir, "
                 "une catégorie (coop, survie, horreur...) ou une recherche par nom.", steam_store, {
                     "category": Param("liste à consulter",
                                       enum=["top_sellers", "new_releases", "specials", "coming_soon"],
                                       required=False, aliases={
                                           "meilleures ventes": "top_sellers", "top": "top_sellers",
                                           "nouveautes": "new_releases", "promo": "specials",
                                           "promos": "specials", "promotions": "specials",
                                           "a venir": "coming_soon", "bientot": "coming_soon"}),
                     "tag": Param("catégorie de jeux : " + ", ".join(_TAGS), required=False),
                     "query": Param("nom de jeu à rechercher", required=False),
                 }, reads=True, untrusted=True))
    box.add(Tool("steam_game_details",
                 "Donne la fiche d'un jeu Steam : résumé, genres, modes (coop, multijoueur...), prix, "
                 "avis. Utile pour dire si un jeu peut plaire.", steam_game_details,
                 {"game": Param("nom du jeu")}, reads=True, untrusted=True))
