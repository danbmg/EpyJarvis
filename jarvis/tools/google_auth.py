"""Connexion OAuth 2.0 à Google pour une application de bureau : PKCE, retour sur 127.0.0.1.

Une seule connexion dans le navigateur : Google rend un jeton de renouvellement, gardé chiffré par DPAPI
(jarvis.secret_store) dans %LOCALAPPDATA%\\Jarvis. Le jeton d'accès (1 h) n'est jamais écrit sur le disque : il
est renouvelé seul, une minute avant d'expirer ou après un refus 401.

Les identifiants du client OAuth (fichier JSON téléchargé dans Google Cloud) sont importés une fois, chiffrés
de la même façon ; le fichier téléchargé peut ensuite être supprimé.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from jarvis import secret_store

log = logging.getLogger("jarvis.tools.google_auth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_LOGIN_TIMEOUT = 180   # secondes pour accepter dans le navigateur
_MARGIN = 60           # le jeton d'accès est renouvelé une minute avant son expiration


class AuthError(Exception):
    pass


class NotConnected(AuthError):
    pass


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256) du flux PKCE (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # 86 caractères, dans les 43 à 128 exigés
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GoogleAuth:
    def __init__(self, account: str, scopes: tuple[str, ...], client_store: Path, token_store: Path,
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.account = account
        self.scopes = scopes
        self.client_store = Path(client_store)
        self.token_store = Path(token_store)
        self._open = opener                 # remplacé dans les tests : aucune requête réelle
        self._lock = threading.Lock()       # les lectures de plusieurs agendas se font en parallèle
        self._access, self._expires = "", 0.0
        self._refresh: str | None = None

    # ---- Identifiants du client OAuth ----

    def import_client(self, json_path: Path) -> None:
        """Importe le JSON téléchargé dans Google Cloud (client « Application de bureau »), chiffré."""
        try:
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthError("fichier d'identifiants illisible : {}".format(exc)) from exc
        installed = data.get("installed") if isinstance(data, dict) else None
        if not installed or not installed.get("client_id") or not installed.get("client_secret"):
            raise AuthError("ce fichier n'est pas un client OAuth « Application de bureau » (clé « installed »)")
        secret_store.save(self.client_store, {"client_id": installed["client_id"],
                                              "client_secret": installed["client_secret"]})

    def has_client(self) -> bool:
        return self.client_store.exists()

    def _client(self) -> dict:
        client = secret_store.load(self.client_store)
        if not client:
            raise AuthError("identifiants OAuth absents : lance la connexion (login) avec le fichier JSON du client")
        return client

    # ---- Connexion ----

    def login(self, open_browser: Callable[[str], Any] = webbrowser.open) -> list[str]:
        """Ouvre la page de consentement de Google, attend le retour sur 127.0.0.1, garde le jeton de
        renouvellement. Renvoie les permissions accordées. AuthError si une permission manque."""
        client = self._client()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        received: dict[str, str] = {}

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (nom imposé par http.server)
                url = urllib.parse.urlparse(self.path)
                if url.path != "/" or received:
                    self.send_error(404)
                    return
                received.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
                ok = "code" in received and received.get("state") == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h2>Jarvis : connexion à Google Agenda {}.</h2><p>Tu peux fermer cet onglet.</p>"
                                 .format("réussie" if ok else "refusée").encode("utf-8"))

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Callback)  # port libre, choisi par Windows
        server.timeout = 1
        redirect_uri = "http://127.0.0.1:{}/".format(server.server_address[1])
        open_browser(AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(self.scopes), "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "access_type": "offline", "prompt": "consent", "login_hint": self.account}))
        deadline = time.monotonic() + _LOGIN_TIMEOUT
        try:
            while not received and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if received.get("state") != state or "code" not in received:
            raise AuthError("connexion refusée ou expirée ({})".format(received.get("error", "délai dépassé")))

        data = self._token_request({
            "grant_type": "authorization_code", "code": received["code"], "redirect_uri": redirect_uri,
            "client_id": client["client_id"], "client_secret": client["client_secret"], "code_verifier": verifier})
        if not data.get("refresh_token"):
            raise AuthError("Google n'a pas donné de jeton de renouvellement")
        granted = data.get("scope", "").split()
        with self._lock:
            self._refresh = data["refresh_token"]
            self._keep_access(data)
        missing = [s for s in self.scopes if s not in granted]
        if missing:  # l'écran de consentement laisse décocher chaque permission
            self.logout()
            raise AuthError("permissions refusées : {}. Relance la connexion et coche toutes les cases."
                            .format(", ".join(s.rsplit("/", 1)[-1] for s in missing)))
        stored = {"refresh_token": data["refresh_token"], "scopes": granted,
                  "connected_at": datetime.datetime.now().isoformat(timespec="seconds")}
        if data.get("refresh_token_expires_in"):  # application restée en mode « Test » : 7 jours seulement
            stored["refresh_expires_at"] = (datetime.datetime.now() + datetime.timedelta(
                seconds=int(data["refresh_token_expires_in"]))).isoformat(timespec="seconds")
        secret_store.save(self.token_store, stored)
        return granted

    def refresh_expires_at(self) -> str:
        """Date d'expiration du jeton de renouvellement si Google en donne une (mode « Test »), sinon ""."""
        return (self._stored() or {}).get("refresh_expires_at", "")

    def logout(self) -> None:
        """Révoque l'accès chez Google (au mieux) et efface le jeton."""
        with self._lock:
            token = self._refresh or (self._stored() or {}).get("refresh_token")
            self._refresh, self._access, self._expires = None, "", 0.0
        if token:
            try:
                self._post(REVOKE_URL, {"token": token})
            except AuthError as exc:
                log.warning("Révocation Google impossible : %s", exc)
        self.token_store.unlink(missing_ok=True)

    def connected(self) -> bool:
        return self.token_store.exists()

    def granted(self) -> list[str]:
        return list((self._stored() or {}).get("scopes", []))

    def _stored(self) -> dict | None:
        try:
            return secret_store.load(self.token_store)
        except secret_store.SecretError:
            return None

    # ---- Jeton d'accès ----

    def access_token(self) -> str:
        with self._lock:
            if self._access and time.time() < self._expires:
                return self._access
            if self._refresh is None:
                stored = self._stored()
                if not stored or not stored.get("refresh_token"):
                    raise NotConnected
                self._refresh = stored["refresh_token"]
            client = self._client()
            data = self._token_request({"grant_type": "refresh_token", "refresh_token": self._refresh,
                                        "client_id": client["client_id"], "client_secret": client["client_secret"]})
            if data.get("refresh_token") and data["refresh_token"] != self._refresh:  # rare chez Google
                self._refresh = data["refresh_token"]
                stored = self._stored() or {}
                secret_store.save(self.token_store, {**stored, "refresh_token": self._refresh})
            self._keep_access(data)
            return self._access

    def invalidate(self) -> None:
        """Le jeton d'accès a été refusé (401) : le prochain appel le renouvelle."""
        with self._lock:
            self._expires = 0.0

    def _keep_access(self, data: dict) -> None:
        self._access = data["access_token"]
        self._expires = time.time() + int(data.get("expires_in", 3600)) - _MARGIN

    # ---- Requêtes ----

    def _token_request(self, form: dict[str, str]) -> dict:
        try:
            return self._post(TOKEN_URL, form)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if "invalid_grant" in detail:  # révoqué, expiré pour de bon, ou mot de passe changé
                self._refresh, self._access = None, ""
                self.token_store.unlink(missing_ok=True)
                raise NotConnected from exc
            raise AuthError("Google refuse la connexion ({}) : {}".format(exc.code, detail)) from exc

    def _post(self, url: str, form: dict[str, str]) -> dict:
        request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._open(request, timeout=15) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError:
            if url == REVOKE_URL:
                raise AuthError("révocation refusée")
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AuthError("Google est injoignable : {}".format(getattr(exc, "reason", exc))) from exc
