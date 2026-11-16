"""Connexion OAuth Google et coffre DPAPI, avec un faux Google : python -m unittest tests.test_google_auth

Le navigateur est simulé par une requête vers le petit serveur local de la connexion (127.0.0.1).
"""

import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from jarvis import secret_store
from jarvis.tools.google_auth import AuthError, GoogleAuth, NotConnected, REVOKE_URL, TOKEN_URL, pkce_pair

SCOPES = ("https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.calendarlist.readonly")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeGoogle:
    """Répond aux requêtes de jeton et de révocation, et les garde."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        form = dict(urllib.parse.parse_qsl(request.data.decode())) if request.data else {}
        self.requests.append((request.full_url, form))
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode())


def http_error(body: str, code: int = 400):
    return urllib.error.HTTPError(TOKEN_URL, code, "Bad Request", {}, io.BytesIO(body.encode()))


class AuthTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        client = self.dir / "client.json"
        client.write_text(json.dumps({"installed": {"client_id": "id-123.apps.googleusercontent.com",
                                                    "client_secret": "secret-xyz"}}), encoding="utf-8")
        self.client_file = client

    def auth(self, google):
        auth = GoogleAuth("test@example.com", SCOPES, self.dir / "client.bin", self.dir / "token.bin", google)
        auth.import_client(self.client_file)
        return auth

    def browser(self, state=None, code="code-abc", error=None):
        """Simule le navigateur : Google renvoie vers le serveur local de Jarvis."""
        opened = []

        def open_browser(url):
            opened.append(url)
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            back = {"state": state or query["state"], **({"error": error} if error else {"code": code})}
            target = query["redirect_uri"] + "?" + urllib.parse.urlencode(back)
            no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            threading.Thread(target=lambda: no_proxy.open(target, timeout=5).read(), daemon=True).start()

        return open_browser, opened

    def test_pkce_s256(self):
        verifier, challenge = pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)
        self.assertTrue(43 <= len(verifier) <= 128)

    def test_client_is_stored_encrypted(self):
        self.auth(FakeGoogle())
        raw = (self.dir / "client.bin").read_bytes()
        self.assertNotIn(b"secret-xyz", raw)
        self.assertEqual(secret_store.load(self.dir / "client.bin")["client_secret"], "secret-xyz")

    def test_web_client_is_refused(self):
        self.client_file.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")
        with self.assertRaises(AuthError):
            self.auth(FakeGoogle())

    def test_login_with_pkce(self):
        google = FakeGoogle({"access_token": "access-1", "expires_in": 3599, "refresh_token": "refresh-123456",
                             "scope": " ".join(SCOPES), "token_type": "Bearer"})
        auth = self.auth(google)
        open_browser, opened = self.browser()
        self.assertEqual(auth.login(open_browser), list(SCOPES))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(opened[0]).query))
        self.assertEqual((query["code_challenge_method"], query["access_type"], query["prompt"], query["login_hint"]),
                         ("S256", "offline", "consent", "test@example.com"))
        self.assertTrue(query["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertEqual(query["scope"].split(), list(SCOPES))
        url, form = google.requests[0]
        self.assertEqual((url, form["grant_type"], form["code"]), (TOKEN_URL, "authorization_code", "code-abc"))
        verifier = form["code_verifier"]
        self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
                         query["code_challenge"])
        self.assertNotIn(b"refresh-123456", (self.dir / "token.bin").read_bytes())  # chiffré
        self.assertEqual(auth.access_token(), "access-1")                            # pas de nouvelle requête
        self.assertEqual(len(google.requests), 1)
        self.assertEqual(auth.refresh_expires_at(), "")

    def test_testing_mode_expiry_is_kept(self):
        google = FakeGoogle({"access_token": "a", "expires_in": 3599, "refresh_token": "r-1", "scope": " ".join(SCOPES),
                             "refresh_token_expires_in": 604799})
        auth = self.auth(google)
        auth.login(self.browser()[0])
        self.assertTrue(auth.refresh_expires_at())

    def test_wrong_state_is_refused(self):
        auth = self.auth(FakeGoogle())
        with self.assertRaises(AuthError):
            auth.login(self.browser(state="forged")[0])
        self.assertFalse(auth.connected())

    def test_refused_consent(self):
        auth = self.auth(FakeGoogle())
        with self.assertRaises(AuthError):
            auth.login(self.browser(error="access_denied")[0])

    def test_missing_permission_revokes_and_stores_nothing(self):
        google = FakeGoogle({"access_token": "a", "expires_in": 3599, "refresh_token": "r-1", "scope": SCOPES[0]}, {})
        auth = self.auth(google)
        with self.assertRaises(AuthError) as raised:
            auth.login(self.browser()[0])
        self.assertIn("calendarlist.readonly", str(raised.exception))
        self.assertEqual(google.requests[1], (REVOKE_URL, {"token": "r-1"}))
        self.assertFalse(auth.connected())

    def test_refresh_then_cache(self):
        google = FakeGoogle({"access_token": "access-2", "expires_in": 3599})
        auth = self.auth(google)
        secret_store.save(auth.token_store, {"refresh_token": "refresh-9", "scopes": list(SCOPES)})
        self.assertEqual(auth.access_token(), "access-2")
        self.assertEqual(auth.access_token(), "access-2")
        url, form = google.requests[0]
        self.assertEqual((len(google.requests), form["grant_type"], form["refresh_token"], form["client_secret"]),
                         (1, "refresh_token", "refresh-9", "secret-xyz"))
        auth.invalidate()  # refus 401 : le prochain appel renouvelle
        google.answers.append({"access_token": "access-3", "expires_in": 3599})
        self.assertEqual(auth.access_token(), "access-3")

    def test_revoked_access_disconnects(self):
        auth = self.auth(FakeGoogle(http_error('{"error": "invalid_grant"}')))
        secret_store.save(auth.token_store, {"refresh_token": "refresh-9"})
        with self.assertRaises(NotConnected):
            auth.access_token()
        self.assertFalse(auth.connected())

    def test_not_connected(self):
        with self.assertRaises(NotConnected):
            self.auth(FakeGoogle()).access_token()

    def test_logout_revokes(self):
        google = FakeGoogle({})
        auth = self.auth(google)
        secret_store.save(auth.token_store, {"refresh_token": "refresh-9"})
        auth.logout()
        self.assertEqual(google.requests, [(REVOKE_URL, {"token": "refresh-9"})])
        self.assertFalse(auth.connected())


if __name__ == "__main__":
    unittest.main()
