"""Spotify par sa Web API : lancer une playlist, un artiste ou un titre, pause, ce qui passe.

Connexion une seule fois (le navigateur s'ouvre) : python -m jarvis.tools.spotify login | logout | status

Flux OAuth « Authorization Code with PKCE » : Jarvis ne connaît aucun secret d'application. Seul le
jeton de l'utilisateur est gardé, dans spotify_token.json (à ne pas partager). Piloter la lecture
exige Spotify Premium.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import http.server
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from jarvis.tools import Param, Result, Tool, Toolbox, norm

log = logging.getLogger("jarvis.tools.spotify")

_ACCOUNTS = "https://accounts.spotify.com"
_API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative")
_LOGIN_TIMEOUT = 180       # secondes pour accepter dans le navigateur
_PLAYLISTS_TTL = 300       # la liste des playlists est gardée 5 minutes
_DEVICE_WAIT = 15          # secondes d'attente d'un appareil après avoir ouvert Spotify
_PLAY_WAIT = 4             # secondes pour voir la lecture démarrer (Spotify accepte parfois sans rien jouer)
_PLAY_POLL = 0.5
_NOT_CONNECTED = "Spotify n'est pas connecté. Lance une fois : python -m jarvis.tools.spotify login"
_REASONS = {
    "NO_ACTIVE_DEVICE": "Aucun appareil Spotify actif : ouvre Spotify.",
    "PREMIUM_REQUIRED": "Il faut Spotify Premium pour piloter la lecture.",
    "ALREADY_PAUSED": "Spotify est déjà en pause.",
    "NOT_PAUSED": "Spotify joue déjà.",
}


class NotConnected(Exception):
    pass


class SpotifyError(Exception):
    pass


class ApiError(SpotifyError):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status, self.reason = status, reason


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) du flux PKCE."""
    verifier = secrets.token_urlsafe(64)  # 86 caractères, dans les 43 à 128 exigés
    return verifier, challenge(verifier)


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str, token_file: Path) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_file = Path(token_file)
        self._token: dict[str, Any] = {}
        self._playlists: tuple[float, list[dict]] = (0.0, [])

    # ---- Connexion ----

    def login(self, open_browser: Callable[[str], Any] = webbrowser.open) -> str:
        """Ouvre la page d'autorisation, attend le retour sur redirect_uri, garde le jeton.
        Renvoie le nom du compte connecté."""
        verifier, code_challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        target = urllib.parse.urlparse(self.redirect_uri)
        received: dict[str, str] = {}

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (nom imposé par http.server)
                url = urllib.parse.urlparse(self.path)
                if url.path != target.path:
                    self.send_error(404)
                    return
                received.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
                ok = "code" in received and received.get("state") == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h2>Jarvis : connexion Spotify {}.</h2><p>Tu peux fermer cet onglet.</p>"
                                 .format("réussie" if ok else "refusée").encode("utf-8"))

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer((target.hostname, target.port), Callback)
        server.timeout = 1
        url = _ACCOUNTS + "/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256", "code_challenge": code_challenge, "scope": SCOPES,
            "state": state})
        open_browser(url)
        deadline = time.monotonic() + _LOGIN_TIMEOUT
        try:
            while "code" not in received and "error" not in received and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if "code" not in received or received.get("state") != state:
            raise SpotifyError("connexion refusée ou expirée ({})".format(received.get("error", "délai dépassé")))
        self._save(self._token_request({
            "grant_type": "authorization_code", "code": received["code"], "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier}))
        me = self.api("GET", "/me")
        return me.get("display_name") or me.get("id", "?")

    def logout(self) -> None:
        self.token_file.unlink(missing_ok=True)
        self._token = {}

    def _token_request(self, form: dict[str, str]) -> dict:
        req = urllib.request.Request(_ACCOUNTS + "/api/token", data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if "invalid_grant" in detail:  # jeton révoqué ou expiré pour de bon
                self.logout()
                raise NotConnected from exc
            raise SpotifyError("Spotify refuse la connexion : {}".format(detail)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SpotifyError("Spotify est injoignable : {}".format(exc)) from exc

    def _save(self, data: dict) -> None:
        self._token = {
            "access_token": data["access_token"],
            # Avec PKCE, le jeton de renouvellement change à chaque renouvellement.
            "refresh_token": data.get("refresh_token") or self._token.get("refresh_token"),
            "expires_at": time.time() + data.get("expires_in", 3600) - 60,
        }
        self.token_file.write_text(json.dumps(self._token), encoding="utf-8")

    def _access_token(self) -> str:
        if not self._token:
            if not self.token_file.exists():
                raise NotConnected
            self._token = json.loads(self.token_file.read_text(encoding="utf-8"))
        if time.time() >= self._token["expires_at"]:
            self._save(self._token_request({"grant_type": "refresh_token", "client_id": self.client_id,
                                            "refresh_token": self._token["refresh_token"]}))
        return self._token["access_token"]

    # ---- Web API ----

    def api(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
            retry: bool = True) -> dict:
        url = _API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._access_token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry:  # jeton expiré plus tôt que prévu
                self._token["expires_at"] = 0
                return self.api(method, path, params, body, retry=False)
            raise _api_error(exc) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SpotifyError("Spotify est injoignable : {}".format(exc)) from exc

    def _device(self) -> dict:
        """Appareil actif, sinon ce PC ; ouvre Spotify s'il n'y en a aucun."""
        devices = self._devices()
        if not devices:
            os.startfile("spotify:")
            deadline = time.monotonic() + _DEVICE_WAIT
            while not devices and time.monotonic() < deadline:
                time.sleep(1)
                devices = self._devices()
        if not devices:
            raise SpotifyError("Spotify ne répond pas sur ce PC. Ouvre Spotify et réessaie.")
        computer = norm(os.environ.get("COMPUTERNAME", ""))
        return (next((d for d in devices if d.get("is_active")), None)
                or next((d for d in devices if norm(d.get("name", "")) == computer), None)
                or devices[0])

    def _start(self, device: dict, body: dict | None, expected: str = "") -> bool:
        """Lance la lecture et vérifie qu'elle démarre vraiment : Spotify répond OK même quand l'appli
        dort (ouverte mais inactive, ou à peine lancée) et ne joue rien. Dans ce cas, le transfert de la
        lecture vers l'appareil le réveille, puis la demande est refaite une fois."""
        self.api("PUT", "/me/player/play", {"device_id": device["id"]}, body)
        if self._wait_playing(body, expected):
            return True
        log.info("Spotify n'a pas démarré sur %s : transfert puis nouvel essai.", device.get("name"))
        self.api("PUT", "/me/player", body={"device_ids": [device["id"]], "play": False})
        self.api("PUT", "/me/player/play", {"device_id": device["id"]}, body)
        return self._wait_playing(body, expected)

    def _wait_playing(self, body: dict | None, expected: str) -> bool:
        deadline = time.monotonic() + _PLAY_WAIT
        while True:
            if _is_playing(self.api("GET", "/me/player"), body, expected):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_PLAY_POLL)

    def _devices(self) -> list[dict]:
        return [d for d in self.api("GET", "/me/player/devices").get("devices", []) if not d.get("is_restricted")]

    def _my_playlists(self) -> list[dict]:
        loaded_at, playlists = self._playlists
        if time.monotonic() - loaded_at < _PLAYLISTS_TTL and playlists:
            return playlists
        playlists, offset = [], 0
        while offset < 200:
            page = self.api("GET", "/me/playlists", {"limit": 50, "offset": offset})
            playlists += [p for p in page.get("items", []) if p]
            if not page.get("next"):
                break
            offset += 50
        self._playlists = (time.monotonic(), playlists)
        return playlists

    def _find(self, query: str, kind: str) -> tuple[str, dict, str] | None:
        """(ce qu'on annonce, corps de la requête de lecture, nom trouvé), ou None."""
        if re.search(r"\bplaylists?\b", query, re.I):
            kind = kind or "playlist"
            query = re.sub(r"\b(ma|mes|mon|la|une|playlists?)\b", " ", query, flags=re.I)
        query = " ".join(query.split())
        if kind in ("", "playlist"):  # d'abord les playlists de l'utilisateur
            mine = _match_playlist(query, self._my_playlists())
            if mine:
                return "ta playlist {}".format(mine["name"]), {"context_uri": mine["uri"]}, mine["name"]
        types = kind or "artist,playlist,album,track"
        # Pas de market=from_token : il exige le droit user-read-private (erreur 403 « Insufficient client scope »).
        data = self.api("GET", "/search", {"q": query, "type": types, "limit": 5})
        items = {t: [i for i in data.get(t + "s", {}).get("items") or [] if i]
                 for t in ("artist", "playlist", "album", "track")}
        if kind:
            item = next(iter(items[kind]), None)
        else:  # un artiste, une playlist ou un album au nom exact, sinon le premier titre
            item = (next((i for t in ("artist", "playlist", "album") for i in items[t][:1]
                          if norm(i["name"]) == norm(query)), None)
                    or next(iter(items["track"]), None))
        if item is None:
            return None
        if item["type"] == "track":
            return "« {} » de {}".format(item["name"], _artists(item)), {"uris": [item["uri"]]}, item["name"]
        label = {"artist": "{}", "playlist": "la playlist {}", "album": "l'album {}"}[item["type"]]
        return label.format(item["name"]), {"context_uri": item["uri"]}, item["name"]

    # ---- Outils ----

    def play(self, query: str = "", kind: str = "") -> Result:
        device = self._device()
        where = _on_device(device)
        if not query:
            try:
                started = self._start(device, None)
            except ApiError as exc:
                if exc.reason != "NOT_PAUSED":
                    raise
                started = True
            if not started:
                return Result(False, "Spotify ne reprend pas la lecture{}. Ouvre Spotify et réessaie.".format(where))
            return Result(True, "Je relance la musique{}.".format(where))
        found = self._find(query, kind)
        if found is None:
            return Result(False, "Je ne trouve rien pour « {} » sur Spotify.".format(query))
        label, body, name = found
        if not self._start(device, body, name):
            return Result(False, "Spotify a accepté {}, mais la lecture n'a pas démarré{}. Ouvre Spotify et "
                                 "réessaie.".format(label, where))
        return Result(True, "Je lance {}{}.".format(label, where))

    def pause(self) -> Result:
        try:
            self.api("PUT", "/me/player/pause")
        except ApiError as exc:
            if exc.reason != "ALREADY_PAUSED":
                raise
        return Result(True, "Musique en pause.")

    def now_playing(self) -> Result:
        data = self.api("GET", "/me/player", {"market": "from_token"})
        item = data.get("item")
        if not item:
            return Result(True, "Rien ne passe sur Spotify.")
        who = item.get("show", {}).get("name", "") if item.get("type") == "episode" else _artists(item)
        context, where = data.get("context") or {}, ""
        if context.get("type") == "playlist":
            playlist_id = context["uri"].rsplit(":", 1)[-1]
            try:
                where = ", playlist {}".format(self.api("GET", "/playlists/" + playlist_id, {"fields": "name"})["name"])
            except SpotifyError:
                pass
        return Result(True, "« {} » de {}{}, {} sur {}.".format(
            item["name"], who, where, "en lecture" if data.get("is_playing") else "en pause",
            data.get("device", {}).get("name", "Spotify")))

    def playlists(self) -> Result:
        names = [p["name"] for p in self._my_playlists()]
        if not names:
            return Result(True, "Tu n'as aucune playlist.")
        return Result(True, "{} playlists : {}.".format(len(names), ", ".join(names[:80])))


def _match_playlist(query: str, playlists: list[dict]) -> dict | None:
    """Nom exact, début du nom (« phonk » -> « Phonk 🔥 ») ou faute proche. Pas de nom contenu dans la
    phrase : une playlist « work » ne doit pas répondre à « mets un son de work hard »."""
    names = {norm(p["name"]): p for p in playlists}
    key = norm(query)
    if key in names:
        return names[key]
    starts = [n for n in names if len(key) >= 4 and n.startswith(key)]
    close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.8)
    found = min(starts, key=len) if starts else (close[0] if close else None)
    return names[found] if found else None


def _artists(item: dict) -> str:
    return ", ".join(a["name"] for a in item.get("artists", [])) or "?"


def _is_playing(state: dict, body: dict | None, expected: str = "") -> bool:
    """L'état du lecteur (GET /me/player) montre-t-il la lecture demandée ? body None : n'importe quoi."""
    if not state.get("is_playing"):
        return False
    if not body:
        return True
    if "context_uri" in body:  # comparé par identifiant : « spotify:playlist:x » ou « spotify:user:u:playlist:x »
        wanted = body["context_uri"].rsplit(":", 1)[-1]
        return ((state.get("context") or {}).get("uri") or "").rsplit(":", 1)[-1] == wanted
    item = state.get("item") or {}
    uris = {item.get("uri"), (item.get("linked_from") or {}).get("uri")}  # titre remplacé selon le pays
    return bool(uris & set(body.get("uris", []))) or bool(expected and norm(item.get("name", "")) == norm(expected))


def _on_device(device: dict) -> str:
    """« sur AUTRE-PC » si la musique part ailleurs que sur ce PC, sinon rien."""
    name = device.get("name", "")
    return "" if not name or norm(name) == norm(os.environ.get("COMPUTERNAME", "")) else " sur {}".format(name)


def _api_error(exc: urllib.error.HTTPError) -> ApiError:
    try:
        error = json.loads(exc.read()).get("error", {})
    except (ValueError, AttributeError):
        error = {}
    reason = error.get("reason", "") if isinstance(error, dict) else ""
    message = error.get("message", "") if isinstance(error, dict) else str(error)
    if exc.code == 429:
        text = "Spotify limite les requêtes, réessaie dans un instant."
    elif exc.code == 403 and "not registered" in message.lower():
        text = "Ce compte n'est pas autorisé dans l'application Spotify développeur (User Management)."
    else:
        text = _REASONS.get(reason) or "Spotify refuse ({}) : {}".format(exc.code, message or exc.reason)
    return ApiError(exc.code, reason, text)


def _safe(method: Callable[..., Result]) -> Callable[..., Result]:
    """Transforme les erreurs Spotify en message pour le modèle et l'utilisateur."""
    def run(**kwargs) -> Result:
        try:
            return method(**kwargs)
        except NotConnected:
            return Result(False, _NOT_CONNECTED)
        except SpotifyError as exc:
            return Result(False, str(exc))
    return run


def register(box: Toolbox, client: SpotifyClient) -> None:
    box.add(Tool("spotify_play",
                 "Lance de la musique sur Spotify, choix par défaut pour toute musique, même sans le mot "
                 "« Spotify » : une playlist de l'utilisateur (« mets ma playlist phonk »), un artiste, un album, "
                 "un titre (« lance The Mysterious Altar de Hastron »), un genre (« mets du rap »). Sans "
                 "recherche, reprend la lecture.", _safe(client.play), {
                     "query": Param("ce qu'il faut jouer, avec le titre ET l'artiste si l'utilisateur donne les "
                                    "deux (« formidable stromae »), sans « ma playlist » (ex. « phonk », "
                                    "« Daft Punk ») ; vide pour reprendre la lecture", required=False),
                     "kind": Param("type cherché, seulement si l'utilisateur le précise",
                                   enum=["playlist", "artist", "album", "track"], required=False, aliases={
                                       "playlists": "playlist", "artiste": "artist", "groupe": "artist",
                                       "titre": "track", "chanson": "track", "morceau": "track", "son": "track"}),
                 }))
    box.add(Tool("spotify_pause", "Met Spotify en pause. Pour reprendre, suivant ou précédent : media.",
                 _safe(client.pause)))
    box.add(Tool("spotify_now_playing",
                 "Dit ce qui passe sur Spotify : titre, artiste, playlist, en lecture ou en pause.",
                 _safe(client.now_playing), reads=True, untrusted=True))
    box.add(Tool("spotify_playlists", "Liste les playlists Spotify de l'utilisateur.", _safe(client.playlists),
                 reads=True, untrusted=True))


def client_from_config(cfg) -> SpotifyClient | None:
    client_id = cfg.get("spotify.client_id")
    if not client_id:
        return None
    return SpotifyClient(client_id, cfg.get("spotify.redirect_uri", "http://127.0.0.1:8765/callback"),
                         cfg.path("spotify.token_file", "spotify_token.json"))


if __name__ == "__main__":
    from jarvis.config import Config

    parser = argparse.ArgumentParser(description="Connexion de Jarvis à Spotify")
    parser.add_argument("action", choices=["login", "logout", "status"])
    action = parser.parse_args().action
    spotify = client_from_config(Config())
    if spotify is None:
        raise SystemExit("spotify.client_id manque dans config.yaml")
    if action == "login":
        print("Le navigateur s'ouvre : accepte l'accès pour Jarvis ({} s au plus).".format(_LOGIN_TIMEOUT))
        print("Connecté au compte Spotify :", spotify.login(lambda url: print(url) or webbrowser.open(url)))
    elif action == "logout":
        spotify.logout()
        print("Déconnecté de Spotify.")
    else:
        try:
            print("Connecté au compte Spotify :", spotify.api("GET", "/me").get("display_name"))
        except NotConnected:
            print("Non connecté.", _NOT_CONNECTED)
