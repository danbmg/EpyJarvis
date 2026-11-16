"""Client REST minimal de l'API Google Calendar v3 (bibliothèque standard uniquement).

Le jeton d'accès vient de jarvis.tools.google_auth. Les erreurs de Google deviennent des phrases pour
l'utilisateur (CalendarError). Les agendas se lisent en parallèle : une requête par calendrier.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from jarvis.tools.google_auth import GoogleAuth

log = logging.getLogger("jarvis.tools.google_calendar")

API = "https://www.googleapis.com/calendar/v3"
TIMEZONE = "Europe/Paris"
_MAX_EVENTS = 1000  # par calendrier et par lecture
_TIMEOUT = 8        # secondes par requête : une lecture en panne est réessayée une fois, la voix n'attend pas 15 s


class CalendarError(Exception):
    """Erreur à dire telle quelle à l'utilisateur."""


class ApiError(CalendarError):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status, self.reason = status, reason


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")  # identifiants avec @ et # (« fr.french#holiday@… »)


class CalendarApi:
    def __init__(self, auth: GoogleAuth, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.auth = auth
        self._open = opener

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                etag: str = "", retry: bool = True, attempt: int = 1) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": "Bearer " + self.auth.access_token(), "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-Match"] = etag  # l'événement n'a pas changé depuis qu'on l'a lu
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(request, timeout=_TIMEOUT) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry:  # jeton d'accès expiré plus tôt que prévu
                self.auth.invalidate()
                return self.request(method, path, params, body, etag, retry=False, attempt=attempt)
            raise _api_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if method == "GET" and attempt == 1:  # une lecture ne change rien : réessayée une fois
                log.info("Lecture Google Agenda réessayée : %s", getattr(exc, "reason", exc))
                return self.request(method, path, params, body, etag, retry, attempt=2)
            raise CalendarError("Google Agenda est injoignable. Vérifie la connexion internet.") from exc

    # ---- Calendriers ----

    def calendars(self) -> list[dict]:
        items, token = [], None
        while True:
            params = {"maxResults": 250, "showHidden": "true", **({"pageToken": token} if token else {})}
            page = self.request("GET", "/users/me/calendarList", params)
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return items

    # ---- Événements ----

    def events(self, calendar_id: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
        """Événements qui chevauchent [start, end), occurrences des séries développées, triés."""
        items, token = [], None
        while len(items) < _MAX_EVENTS:
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "orderBy": "startTime", "timeZone": TIMEZONE, "maxResults": 250,
                      **({"pageToken": token} if token else {})}
            page = self.request("GET", "/calendars/{}/events".format(_quote(calendar_id)), params)
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                break
        return items

    def events_many(self, calendar_ids: list[str], start: dt.datetime,
                    end: dt.datetime) -> dict[str, list[dict] | CalendarError]:
        """Lit plusieurs calendriers en parallèle ; un calendrier en erreur n'empêche pas les autres."""
        if not calendar_ids:
            return {}
        self.auth.access_token()  # renouvelé une fois ici, pas par chaque fil
        results: dict[str, list[dict] | CalendarError] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(calendar_ids))) as pool:
            futures = {pool.submit(self.events, cid, start, end): cid for cid in calendar_ids}
            for future in concurrent.futures.as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except CalendarError as exc:
                    results[futures[future]] = exc
        return results

    def get(self, calendar_id: str, event_id: str) -> dict:
        return self.request("GET", "/calendars/{}/events/{}".format(_quote(calendar_id), _quote(event_id)),
                            {"timeZone": TIMEZONE})

    def insert(self, calendar_id: str, body: dict, send_updates: str = "none") -> dict:
        """Crée l'événement. Son identifiant est choisi par Jarvis : refaite après une coupure, la création
        ne fait pas de doublon (Google répond 409 et l'événement existant est renvoyé)."""
        try:
            return self.request("POST", "/calendars/{}/events".format(_quote(calendar_id)),
                                {"sendUpdates": send_updates}, body)
        except ApiError as exc:
            if exc.status == 409 and body.get("id"):
                return self.get(calendar_id, body["id"])
            raise

    def patch(self, calendar_id: str, event_id: str, body: dict, etag: str = "",
              send_updates: str = "none") -> dict:
        return self.request("PATCH", "/calendars/{}/events/{}".format(_quote(calendar_id), _quote(event_id)),
                            {"sendUpdates": send_updates}, body, etag)

    def delete(self, calendar_id: str, event_id: str, etag: str = "", send_updates: str = "none") -> None:
        self.request("DELETE", "/calendars/{}/events/{}".format(_quote(calendar_id), _quote(event_id)),
                     {"sendUpdates": send_updates}, etag=etag)

    def move(self, calendar_id: str, event_id: str, destination: str, send_updates: str = "none") -> dict:
        return self.request("POST", "/calendars/{}/events/{}/move".format(_quote(calendar_id), _quote(event_id)),
                            {"destination": destination, "sendUpdates": send_updates})


def _api_error(exc: urllib.error.HTTPError) -> ApiError:
    try:
        error = json.loads(exc.read()).get("error", {})
    except (ValueError, AttributeError):
        error = {}
    error = error if isinstance(error, dict) else {}
    reasons = [e.get("reason", "") for e in error.get("errors", []) if isinstance(e, dict)]
    reasons += [d.get("reason", "") for d in error.get("details", []) if isinstance(d, dict)]
    reason = next((r for r in reasons if r), "")
    message = error.get("message", "") or str(exc.reason)
    code = exc.code
    if code in (403, 429) and ("RateLimit" in reason or "rateLimit" in reason or code == 429):
        text = "Google limite les requêtes, réessaie dans un instant."
    elif reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
        text = "Jarvis n'a pas cette permission sur Google Agenda : relance la connexion (login)."
    elif reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        text = "L'API Google Calendar n'est pas activée dans le projet Google Cloud."
    elif reason == "forbiddenForNonOrganizer":
        text = "Seul l'organisateur de cet événement peut le modifier."
    elif code == 403:
        text = "Google refuse : tu n'as pas le droit de modifier ce calendrier ou cet événement."
    elif code == 404:
        text = "Événement ou calendrier introuvable : il a peut-être été supprimé entre-temps."
    elif code == 410:
        text = "Cet événement a déjà été supprimé."
    elif code == 412:
        text = "L'événement a changé entre-temps : redemande-moi, je le relirai."
    elif code >= 500:
        text = "Google Agenda a un problème, réessaie dans un instant."
    else:
        text = "Google Agenda refuse la demande ({}) : {}".format(code, message)
    log.info("Erreur Google Agenda %s %s : %s", code, reason, message[:200])
    return ApiError(code, reason, text)
