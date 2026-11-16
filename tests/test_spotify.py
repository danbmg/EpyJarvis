"""Spotify avec une fausse Web API : aucune requête réelle. python -m unittest tests.test_spotify"""

import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from jarvis.tools import Toolbox, spotify
from jarvis.tools.spotify import ApiError, SpotifyClient, _match_playlist, _safe, challenge

PLAYLISTS = {"items": [{"name": "Chill du soir", "uri": "spotify:playlist:chill"},
                       {"name": "Phonk 🔥", "uri": "spotify:playlist:phonk"}], "next": None}
DEVICES = {"devices": [{"id": "tel", "name": "Téléphone", "is_active": False},
                       {"id": "pc", "name": "MON-PC", "is_active": True}]}
SEARCH = {
    "artists": {"items": [{"type": "artist", "name": "Daft Punk", "uri": "spotify:artist:daft"}]},
    "playlists": {"items": [None, {"type": "playlist", "name": "Daft Punk Mix", "uri": "spotify:playlist:mix"}]},
    "albums": {"items": []},
    "tracks": {"items": [{"type": "track", "name": "Get Lucky", "uri": "spotify:track:lucky",
                          "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}]}]},
}


class FakeSpotify(SpotifyClient):
    """Répond aux appels de l'API à partir de réponses écrites à l'avance, et les garde. Sans réponse écrite
    pour GET /me/player, le lecteur joue ce qui a été demandé, à partir de la `starts_after`-ième demande
    (0 : jamais, comme une appli Spotify endormie)."""

    def __init__(self, responses=None, starts_after=1):
        super().__init__("id", "http://127.0.0.1:8765/callback", Path("inexistant.json"))
        self.calls = []
        self.responses = {("GET", "/me/playlists"): PLAYLISTS, ("GET", "/me/player/devices"): DEVICES,
                          ("GET", "/search"): SEARCH, **(responses or {})}
        self.starts_after = starts_after
        self.state = {}

    def api(self, method, path, params=None, body=None, retry=True):
        self.calls.append((method, path, params, body))
        if (method, path) == ("PUT", "/me/player/play") and len(self.played()) == self.starts_after:
            body = body or {}
            self.state = {"is_playing": True, "context": {"uri": body.get("context_uri")},
                          "item": {"uri": (body.get("uris") or ["spotify:track:autre"])[0], "name": "?"}}
        if (method, path) == ("GET", "/me/player") and (method, path) not in self.responses:
            return self.state
        response = self.responses.get((method, path), {})
        if isinstance(response, Exception):
            raise response
        return response

    def played(self):
        return [(params, body) for method, path, params, body in self.calls if path == "/me/player/play"]


class PkceTest(unittest.TestCase):
    def test_rfc_7636_example(self):
        self.assertEqual(challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


class PlayTest(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.dict(spotify.os.environ, {"COMPUTERNAME": "MON-PC"}),
                   mock.patch.object(spotify, "_PLAY_WAIT", 0), mock.patch.object(spotify, "_PLAY_POLL", 0)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_own_playlist_first(self):
        client = FakeSpotify()
        self.assertEqual(client.play("ma playlist phonk").message, "Je lance ta playlist Phonk 🔥.")
        self.assertEqual(client.played(), [({"device_id": "pc"}, {"context_uri": "spotify:playlist:phonk"})])

    def test_artist_with_exact_name(self):
        client = FakeSpotify()
        self.assertEqual(client.play("daft punk").message, "Je lance Daft Punk.")
        self.assertEqual(client.played()[0][1], {"context_uri": "spotify:artist:daft"})

    def test_track_otherwise(self):
        client = FakeSpotify()
        self.assertEqual(client.play("get lucky").message, "Je lance « Get Lucky » de Daft Punk, Pharrell Williams.")
        self.assertEqual(client.played()[0][1], {"uris": ["spotify:track:lucky"]})

    def test_playlist_matching_is_strict(self):
        lists = [{"name": "Phonk 🔥", "uri": "p"}, {"name": "work", "uri": "w"}, {"name": "US", "uri": "u"}]
        self.assertEqual(_match_playlist("phonk", lists)["uri"], "p")    # début du nom
        self.assertEqual(_match_playlist("fonk", lists), None)            # trop loin
        self.assertEqual(_match_playlist("Work", lists)["uri"], "w")      # nom exact
        self.assertIsNone(_match_playlist("work hard", lists))            # nom contenu : non
        self.assertIsNone(_match_playlist("formidable stromae", lists))

    def test_search_without_market(self):  # market=from_token exige un droit que Jarvis ne demande pas
        client = FakeSpotify()
        client.play("formidable stromae")
        search = next(params for _, path, params, _ in client.calls if path == "/search")
        self.assertNotIn("market", search)

    def test_kind_forces_search_type(self):
        client = FakeSpotify()
        client.play("daft punk", kind="playlist")
        search = next(params for _, path, params, _ in client.calls if path == "/search")
        self.assertEqual(search["type"], "playlist")
        self.assertEqual(client.played()[0][1], {"context_uri": "spotify:playlist:mix"})

    def test_nothing_found(self):
        client = FakeSpotify({("GET", "/search"): {}})
        self.assertFalse(client.play("zzzz").ok)
        self.assertEqual(client.played(), [])

    def test_resume_without_query(self):
        client = FakeSpotify()
        self.assertEqual(client.play().message, "Je relance la musique.")
        self.assertEqual(client.played(), [({"device_id": "pc"}, None)])

    def test_opens_spotify_when_no_device(self):
        client = FakeSpotify({("GET", "/me/player/devices"): {"devices": []}})
        with mock.patch.object(spotify.os, "startfile") as startfile, mock.patch.object(spotify, "_DEVICE_WAIT", 0):
            result = _safe(client.play)(query="phonk")
        startfile.assert_called_once_with("spotify:")
        self.assertFalse(result.ok)
        self.assertIn("Ouvre Spotify", result.message)

    def test_sleeping_app_is_woken_then_asked_again(self):  # OK de Spotify, mais rien ne joue la 1re fois
        client = FakeSpotify(starts_after=2)
        self.assertEqual(client.play("get lucky").message, "Je lance « Get Lucky » de Daft Punk, Pharrell Williams.")
        transfer = [body for method, path, _, body in client.calls if (method, path) == ("PUT", "/me/player")]
        self.assertEqual(transfer, [{"device_ids": ["pc"], "play": False}])
        self.assertEqual(len(client.played()), 2)

    def test_never_started_is_a_failure(self):  # plus jamais « Je lance… » sans musique
        result = FakeSpotify(starts_after=0).play("get lucky")
        self.assertFalse(result.ok)
        self.assertIn("la lecture n'a pas démarré", result.message)
        self.assertFalse(FakeSpotify(starts_after=0).play().ok)

    def test_other_track_playing_is_not_success(self):
        client = FakeSpotify({("GET", "/me/player"): {"is_playing": True, "item": {"uri": "spotify:track:x",
                                                                                     "name": "Autre"}}})
        self.assertFalse(client.play("get lucky").ok)

    def test_relinked_track_counts(self):  # Spotify remplace parfois le titre par sa version du pays
        client = FakeSpotify({("GET", "/me/player"): {"is_playing": True, "item": {
            "uri": "spotify:track:fr", "name": "Get Lucky", "linked_from": {"uri": "spotify:track:lucky"}}}})
        self.assertTrue(client.play("get lucky").ok)

    def test_other_device_is_named(self):
        devices = {"devices": [{"id": "pc", "name": "MON-PC"}, {"id": "bureau", "name": "PC-BUREAU", "is_active": True}]}
        client = FakeSpotify({("GET", "/me/player/devices"): devices})
        self.assertEqual(client.play("phonk").message, "Je lance ta playlist Phonk 🔥 sur PC-BUREAU.")

    def test_think_tag_is_not_a_search(self):  # qwen3 a passé query="/no_think" pour « lance-le »
        box = Toolbox()
        spotify.register(box, FakeSpotify())
        tool = box.get("spotify_play")
        self.assertEqual(tool.clean_args({"query": "/no_think"}), {})
        self.assertEqual(tool.clean_args({"query": "papaoutai /no_think"}), {"query": "papaoutai"})


class OtherToolsTest(unittest.TestCase):
    def test_pause_when_already_paused(self):
        client = FakeSpotify({("PUT", "/me/player/pause"): ApiError(403, "ALREADY_PAUSED", "déjà")})
        self.assertTrue(client.pause().ok)

    def test_premium_error_is_explained(self):
        client = FakeSpotify({("PUT", "/me/player/pause"): ApiError(403, "PREMIUM_REQUIRED", "Il faut Premium.")})
        result = _safe(client.pause)()
        self.assertEqual((result.ok, result.message), (False, "Il faut Premium."))

    def test_now_playing(self):
        client = FakeSpotify({
            ("GET", "/me/player"): {"is_playing": True, "device": {"name": "MON-PC"},
                                    "context": {"type": "playlist", "uri": "spotify:playlist:phonk"},
                                    "item": SEARCH["tracks"]["items"][0]},
            ("GET", "/playlists/phonk"): {"name": "Phonk 🔥"}})
        self.assertEqual(client.now_playing().message,
                         "« Get Lucky » de Daft Punk, Pharrell Williams, playlist Phonk 🔥, en lecture sur MON-PC.")
        self.assertEqual(FakeSpotify().now_playing().message, "Rien ne passe sur Spotify.")

    def test_not_connected(self):
        client = SpotifyClient("id", "http://127.0.0.1:8765/callback", Path("inexistant.json"))
        result = _safe(client.pause)()
        self.assertFalse(result.ok)
        self.assertIn("spotify login", result.message)


class LoginTest(unittest.TestCase):
    def test_pkce_login_saves_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = SpotifyClient("id", "http://127.0.0.1:8766/callback", Path(tmp) / "token.json")

            sent = {}

            def browser(url):  # le navigateur : l'utilisateur accepte, Spotify renvoie vers le callback
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                self.assertEqual(query["code_challenge_method"], ["S256"])
                sent["challenge"] = query["code_challenge"][0]
                callback = "http://127.0.0.1:8766/callback?code=abc&state=" + query["state"][0]
                threading.Thread(target=lambda: urllib.request.urlopen(callback, timeout=5).read()).start()

            tokens = {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}
            with mock.patch.object(SpotifyClient, "_token_request", return_value=tokens) as token_request, \
                    mock.patch.object(SpotifyClient, "api", return_value={"display_name": "Dan"}):
                self.assertEqual(client.login(browser), "Dan")
            form = token_request.call_args[0][0]
            self.assertEqual((form["grant_type"], form["code"]), ("authorization_code", "abc"))
            self.assertEqual(challenge(form["code_verifier"]), sent["challenge"])
            self.assertIn('"refresh_token": "ref"', (Path(tmp) / "token.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
