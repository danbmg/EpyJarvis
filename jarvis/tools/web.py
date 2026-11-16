"""Web : ouvrir un site, rechercher dans le navigateur, ou chercher et lire pour répondre soi-même."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser

from jarvis.tools import Param, Result, Tool, Toolbox, norm

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130 Safari/537.36",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6"}


def fetch(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read(3_000_000).decode(charset, errors="replace")


def _normalize_url(url: str) -> str | None:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    return url if url.lower().startswith(("http://", "https://")) else None


def open_url(url: str) -> Result:
    target = _normalize_url(url)
    if target is None:
        return Result(False, "Seuls les liens http et https sont autorisés.")
    webbrowser.open(target)
    return Result(True, "J'ouvre {}.".format(target))


def browser_search(query: str, site: str = "google") -> Result:
    q = urllib.parse.quote_plus(query)
    urls = {"google": "https://www.google.com/search?q=" + q,
            "youtube": "https://www.youtube.com/results?search_query=" + q,
            "twitch": "https://www.twitch.tv/search?term=" + q}
    webbrowser.open(urls.get(site, urls["google"]))
    return Result(True, "Je cherche « {} » sur {}.".format(query, site.capitalize()))


_YT_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})"')  # vraies vidéos (pas les Shorts ni la pub)
_YT_TITLE = re.compile(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"')


def youtube_play(query: str, rank: int = 1) -> Result:
    """Lance la vidéo trouvée à ce rang (la première par défaut), au lieu d'afficher la page de résultats."""
    results = "https://www.youtube.com/results?hl=fr&gl=FR&search_query=" + urllib.parse.quote_plus(query)
    page = fetch(results)
    videos = []
    for match in _YT_VIDEO.finditer(page):
        if match[1] not in (v[1] for v in videos):  # une vidéo peut revenir plus bas dans la page
            videos.append(match)
    if not videos:
        webbrowser.open(results)
        return Result(True, "Je n'ai pas trouvé de vidéo précise : j'affiche les résultats YouTube pour « {} ».".format(query))
    video = videos[min(max(rank, 1), len(videos)) - 1]
    title = _YT_TITLE.search(page, video.end(), video.end() + 3000)
    webbrowser.open("https://www.youtube.com/watch?v=" + video[1])
    return Result(True, "Je lance « {} » sur YouTube.".format(json.loads('"' + title[1] + '"') if title else query))


def twitch_watch(channel: str) -> Result:
    name = re.sub(r"[^a-z0-9_]", "", norm(channel))  # « Skyy Art » -> skyyart
    if not name:
        return Result(False, "Nom de chaîne Twitch invalide : {}.".format(channel))
    webbrowser.open("https://www.twitch.tv/" + name)
    return Result(True, "J'ouvre la chaîne Twitch {}.".format(name))


def web_search(query: str) -> Result:
    page = fetch("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
    results = []
    for block in page.split('class="result__body"')[1:9]:
        title = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        if not title:
            continue
        link = title.group(1)
        if "uddg=" in link:  # lien de redirection DuckDuckGo
            link = urllib.parse.unquote(link.split("uddg=")[1].split("&")[0])
        results.append("- {} ({})\n  {}".format(_strip_tags(title.group(2)), link,
                                                _strip_tags(snippet.group(1)) if snippet else ""))
    return Result(True, "Résultats pour « {} » :\n{}".format(query, "\n".join(results)) if results
                  else "Aucun résultat pour « {} ».".format(query))


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping and data.strip():
            self.parts.append(" ".join(data.split()))


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def read_webpage(url: str) -> Result:
    target = _normalize_url(url)
    if target is None:
        return Result(False, "Seuls les liens http et https sont autorisés.")
    extractor = _TextExtractor()
    extractor.feed(fetch(target))
    text = "\n".join(extractor.parts)
    return Result(True, "Contenu de {} :\n{}".format(target, text[:6000]))


def register(box: Toolbox) -> None:
    box.add(Tool("open_url", "Ouvre un site web dans le navigateur (ex. « twitch.tv/skyyart »).", open_url, {
        "url": Param("adresse du site"),
    }, after_untrusted_confirm=True))
    box.add(Tool("youtube_play",
                 "Lance directement une vidéo YouTube, la première trouvée (« mets une vidéo de MrBeast », "
                 "« lance le clip de Formidable de Stromae »), seulement si l'utilisateur parle de vidéo, de "
                 "clip, de YouTube ou d'internet : une chanson ou un artiste sans précision se joue sur Spotify "
                 "(spotify_play). Juste après une recherche YouTube, « lance la deuxième vidéo » : même recherche "
                 "avec rank=2. Pas pour un jeu ni une application.", youtube_play, {
                     "query": Param("ce qu'il faut chercher sur YouTube"),
                     "rank": Param("rang de la vidéo dans les résultats (« la deuxième » = 2), 1 par défaut",
                                   type="integer", required=False),
                 }, untrusted=True))
    box.add(Tool("twitch_watch", "Ouvre le live d'une chaîne Twitch (« mets skyyart sur Twitch »).", twitch_watch, {
        "channel": Param("nom de la chaîne Twitch"),
    }))
    box.add(Tool("browser_search",
                 "Affiche des résultats de recherche dans le navigateur sans rien lancer, quand l'utilisateur "
                 "veut les parcourir lui-même (« cherche X sur Google »).", browser_search, {
                     "query": Param("termes de recherche"),
                     "site": Param("où chercher", enum=["google", "youtube", "twitch"], required=False),
                 }))
    box.add(Tool("web_search",
                 "Cherche sur internet et renvoie les résultats pour que TU répondes à l'utilisateur "
                 "(actualité, informations récentes, questions dont tu n'es pas sûr).", web_search, {
                     "query": Param("termes de recherche"),
                 }, reads=True, untrusted=True))
    box.add(Tool("read_webpage", "Lit le texte d'une page web pour l'analyser ou la résumer.", read_webpage, {
        "url": Param("adresse de la page"),
    }, reads=True, untrusted=True))
