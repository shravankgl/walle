"""Google Calendar, read-only, on the connector base (D55, D92, D93).

The second live provider for ``walle.connector``, built to the pattern of ``walle.gmail``. It
signs the owner in with OAuth, asks the Calendar API for the events that changed since the
cursor, and turns each event into an ``Item`` the base appends as an ``observation``. The base
owns the cursor, the lock and the duplicate check; this module owns only what is Google
Calendar's: the wire, the token, the scope, and the shape of an event.

Four rules, each with a test that would fail without it (``tests/test_google_calendar.py``):

* **One scope, and it is read-only.** ``SCOPES`` is ``calendar.events.readonly`` and nothing
  else, named in exactly one place. The sign-in asks for no previously granted scopes, and a
  token whose grant comes back wider than ``SCOPES`` is refused, not kept. Every request to the
  API is a ``GET`` on ``events.list``; the only ``POST`` goes to the token endpoint. Nothing here
  knows how to create, change or delete an event, because no such endpoint is named.
* **Tokens live outside the log and outside the repo.** A ``TokenStore`` writes one private
  file (``0600``, in a ``0700`` folder) at a path the owner chooses, by default under the user's
  config folder and apart from Gmail's, and ``sync_calendar`` refuses a path under the walle
  root. No token is ever in an ``Item``, and every error message is scrubbed of every secret
  the client has held before it is raised.
* **Event text is untrusted (D55).** Titles, descriptions, locations, the names of organizers
  and guests: anyone who can send an invitation can write them, and an invitation can land on
  the calendar before it is accepted. The body opens with the connector's own line, never a turn
  label, so a ``user:`` line in a description is a forged turn to ``walle.hazards``. Every field
  but the description is folded onto one line, so none can open a turn or start the text block.
  An HTML description is reduced to its text, hidden parts included, so the gate sees what a
  model would. Attachments and conference links are counted, never followed.
* **A failed sync appends nothing partial.** Every failure here raises from ``pull``, before the
  base takes the lock, and the base appends nothing from a pull that raised: not the pages
  before the failure either (task 54).

Why the stdlib and not Google's client
--------------------------------------
The same reason as ``walle.gmail``: ``tests/test_constraints.py`` allows four runtime
dependencies and Google's is not one (D87, constraint 7). One request shape and a token refresh
need no more than ``urllib``. The wire is a ``Transport``, a callable from ``Request`` to
``Response``, so the tests run the whole connector against a local fake of the API.

What an event becomes
---------------------
::

    Google Calendar event <id> on calendar <calendar id>
    Status: confirmed | tentative | cancelled
    Title: ...
    When: 2026-11-10T18:00:00+05:30 to 2026-11-10T18:30:00+05:30 (Asia/Kolkata)
    In UTC: 2026-11-10T12:30:00+00:00 to 2026-11-10T13:00:00+00:00
    Repeats: RRULE:FREQ=WEEKLY;BYDAY=TU          (a series, one line per rule)
    Instance of recurring event <id>, originally <time>   (an exception to a series)
    Location: ...
    Organizer: Name <address> (you)
    Attendees: Name <address> (accepted), ..., and 5 more
    Attachments not read: 2 (application/pdf, image/png)

    <the description, as text>

Lines with nothing to say are left out, so a deleted event Google sends bare is its first
two lines (three when Google gave no time for it, below).

**Recurring events are not expanded.** ``singleEvents=false``: a weekly lesson is one event,
its series, carrying its ``RRULE``; a moved or cancelled occurrence is its own event, naming
the series and the occurrence it replaces. Expanding would write an observation for every week
until the rule ends, which for a rule with no end is as many as Google cares to hand back,
every one a copy of the same fact. "One observation per event" is read literally.

**Time zones.** A time is shown as the calendar wrote it, with its zone, and once more in UTC
when that moment is known. Google always sends an offset unless the event names a zone; a time
with neither takes the calendar's zone. A zone this machine does not know is said so, and no UTC
time is invented for it. An all-day event is dates, not moments, and its end, which Google
gives as the day after, is shown as the last day.

The cursor (D92, D93)
---------------------
``observed_at`` is the event's ``updated``, the moment the calendar last changed it, in the
log's one spelling. That is the source's clock for the *item*; when the event happens is world
time and stays in the body. So the cursor, ``max(observed_at)``, is exactly what the API's
``updatedMin`` wants, less an overlap, and the base throws away what it already holds. An event
changed after it was logged comes back with a new ``updated`` and is a second observation, the
first left as it was (append, never mutate). ``showDeleted=true`` asks for cancellations.

Google guarantees nothing but ``id`` on a deleted event. One that arrives without ``updated``
takes the list's own ``updated`` (the calendar's last change, at or after the deletion) and its
body says so; a live event without one is a broken reply, and the sync is refused. An
``updatedMin`` Google calls too far in the past (``410 updatedMinTooLongAgo``) falls back to one
full listing; the content fold appends only what is new. Nothing is stored between runs but the
log: no sync token, which would be a side store (D92).

Expired tokens and rate limits
------------------------------
As in ``walle.gmail``: an access token past its expiry is refreshed before the call; one the
API rejects with ``401`` is refreshed once and the call retried; a second ``401``, or a refresh
the token endpoint refuses, is an ``AuthError`` and the owner signs in again. ``429``, a ``403``
whose reason is a rate limit, and ``5xx`` are retried with ``Retry-After`` (at most 60 seconds)
or ``1, 2, 4, 8...`` seconds, up to ``max_attempts``; then ``RateLimited`` or ``ApiError``.

Running it
----------
``python -m walle.google_calendar sign-in --client-id ...`` and ``python -m
walle.google_calendar sync --root ... --principal ... --device ... [--calendar ID]``. The
``walle`` command gains no subcommand here; that is one change for both connectors, later.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import click
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from walle.connector import Cursor, Item, SyncReport, sync
from walle.writer import StreamWriter

READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
"""The one scope: read the events on the owner's calendars, and nothing that writes."""

SCOPES: tuple[str, ...] = (READONLY_SCOPE,)
"""Everything the sign-in asks for, and everything a token may carry. A test holds this."""

API = "https://www.googleapis.com/calendar/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SOURCE = "google-calendar"
"""The ``source`` on every observation. One source, one name, forever (D92)."""

SOURCE_VERSION = "calendar-v3"
"""The API version the items came through (D101)."""

MAX_TEXT = 16_000
"""Characters of a description kept before it is cut with a note."""

MAX_LINE = 1_000
"""Characters of any one-line field kept before it is cut."""

MAX_ATTENDEES = 20
"""Guests named on the ``Attendees`` line before the rest are counted."""

_API_HOST = urllib.parse.urlsplit(API).hostname
_API_PATH = urllib.parse.urlsplit(API).path + "/"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RATE_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}
)
_TOO_OLD = "updatedMinTooLongAgo"
_ANSWERS = {
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "maybe",
    "needsAction": "not answered",
}
_REDACTED = "[redacted]"


# --- errors ------------------------------------------------------------------------------------


class CalendarError(Exception):
    """Anything the connector cannot get past. Its message never carries a token."""


class AuthError(CalendarError):
    """The token is no good and a refresh did not help, or the sign-in did not complete."""


class RateLimited(CalendarError):
    """The API kept answering with a rate limit for every attempt allowed."""


class ApiError(CalendarError):
    """The API answered with an error that is not about the token and was not a rate limit,
    or with a reply the connector cannot read."""

    def __init__(self, status: int, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


# --- the wire ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        """The body as JSON, or ``None`` when it is not."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None


class Transport(Protocol):
    """How a request reaches the network. The tests hand in a local fake of the API."""

    def __call__(self, request: Request) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(request: Request, *, timeout: float = 30.0) -> Response:
    """The real wire, from the standard library. Follows no redirect, so a token never travels
    to a host Google did not name; refuses plain ``http`` anywhere but the loopback. A network
    failure of any kind, a timeout included, is a ``CalendarError``."""
    url = urllib.parse.urlsplit(request.url)
    if url.scheme != "https" and url.hostname not in _LOOPBACK:
        raise ValueError(f"only https is sent anywhere but the loopback, not {url.scheme}")
    req = urllib.request.Request(
        request.url, data=request.body, method=request.method, headers=dict(request.headers)
    )
    try:
        with _OPENER.open(req, timeout=timeout) as reply:
            return Response(reply.status, dict(reply.headers.items()), reply.read())
    except urllib.error.HTTPError as error:
        with error:
            return Response(error.code, dict(error.headers.items()), error.read())
    except urllib.error.URLError as error:
        raise CalendarError(f"could not reach {url.hostname}: {error.reason}") from error
    except OSError as error:
        # A read that times out, or a connection reset mid-reply, is not a URLError.
        raise CalendarError(f"could not reach {url.hostname}: {error}") from error


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    return next((v for k, v in headers.items() if k.lower() == wanted), None)


# --- the token ---------------------------------------------------------------------------------


class Token(BaseModel):
    """What the owner's sign-in produced. Secrets are ``SecretStr``: ``repr`` and ``str``
    show none of them, and only ``TokenStore.save`` writes them out."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str
    client_secret: SecretStr
    refresh_token: SecretStr
    access_token: SecretStr | None = None
    expires_at: str | None = None
    """When ``access_token`` stops working, as an RFC 3339 instant; ``None`` means unknown,
    which is treated as expired."""

    def expired(self, now: float, *, margin: float = 60.0) -> bool:
        """Whether the access token needs refreshing before use. Unknown is expired."""
        if self.access_token is None or self.expires_at is None:
            return True
        try:
            ends = datetime.fromisoformat(self.expires_at).timestamp()
        except ValueError:
            return True
        return ends - margin <= now

    def secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in a message or a log line."""
        found = [self.client_secret.get_secret_value(), self.refresh_token.get_secret_value()]
        if self.access_token is not None:
            found.append(self.access_token.get_secret_value())
        return tuple(s for s in found if s)


class TokenStore:
    """One private JSON file, replaced whole. Lives wherever the owner says, never under the
    walle root (``sync_calendar`` refuses that) and never in the repo."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Token:
        """Raises ``FileNotFoundError`` when there is no file and ``ValueError`` when the file
        is not a token file. The ``ValueError`` names the fields at fault, never their values:
        a validation message that quoted the input would print the secret it refused."""
        data = self.path.read_text(encoding="utf-8")
        try:
            return Token.model_validate_json(data)
        except ValidationError as error:
            where = sorted({".".join(str(p) for p in e["loc"]) or "<root>" for e in error.errors()})
            raise ValueError(
                f"{self.path}: not a walle google calendar token file (at {', '.join(where)})"
            ) from None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {
                "client_id": token.client_id,
                "client_secret": token.client_secret.get_secret_value(),
                "refresh_token": token.refresh_token.get_secret_value(),
                "access_token": None
                if token.access_token is None
                else token.access_token.get_secret_value(),
                "expires_at": token.expires_at,
            },
            indent=2,
        )
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=".google-calendar-token.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def default_token_path() -> Path:
    """``$XDG_CONFIG_HOME/walle/google-calendar-token.json``, else under ``~/.config``. Apart
    from Gmail's: each connector has its own sign-in, and its own narrow grant."""
    base = os.environ.get("XDG_CONFIG_HOME")
    config = Path(base) if base else Path.home() / ".config"
    return config / "walle" / "google-calendar-token.json"


def _epoch_instant(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="microseconds")


def _granted_too_much(data: Mapping[str, Any]) -> str | None:
    """The scopes a token-endpoint reply grants beyond ``SCOPES``, as text, or ``None``."""
    granted = data.get("scope")
    if not isinstance(granted, str):
        return None
    extra = sorted(set(granted.split()) - set(SCOPES))
    return _one_line(" ".join(extra))[:200] if extra else None


def _token_from(
    client_id: str,
    client_secret: SecretStr,
    refresh_token: str,
    data: Mapping[str, Any],
    now: float,
) -> Token:
    """A ``Token`` from the token endpoint's reply. A refresh reply may omit
    ``refresh_token``; it is then the one already held. Raises ``AuthError`` for a grant wider
    than ``SCOPES``: a token that could write is not kept, even if Google offers one."""
    if (extra := _granted_too_much(data)) is not None:
        raise AuthError(
            f"Google granted more than the read-only scope ({extra}); nothing was kept. "
            "Remove walle's access in your Google account and run sign-in again"
        )
    expires_in = data.get("expires_in", 3600)
    lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
    return Token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=SecretStr(str(data.get("refresh_token") or refresh_token)),
        access_token=SecretStr(str(data["access_token"])),
        expires_at=_epoch_instant(now + lifetime),
    )


def _token_request(form: Mapping[str, str]) -> Request:
    return Request(
        "POST",
        TOKEN_URL,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        urllib.parse.urlencode(dict(form)).encode(),
    )


# --- the client ----------------------------------------------------------------------------------


class CalendarClient:
    """``GET`` on the Calendar API with the owner's token, refreshing and backing off as
    needed. ``sleep`` and ``now`` are injectable so the tests show the backoff without waiting
    for it. ``store`` receives every refreshed token."""

    def __init__(
        self,
        token: Token,
        *,
        transport: Transport = urllib_transport,
        store: TokenStore | None = None,
        sleep: Callable[[float], object] = time.sleep,
        now: Callable[[], float] = time.time,
        max_attempts: int = 5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._token = token
        self._transport = transport
        self._store = store
        self._sleep = sleep
        self._now = now
        self._max_attempts = max_attempts
        self._secrets: set[str] = set(token.secrets())

    @property
    def token(self) -> Token:
        return self._token

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        """One ``GET``, as parsed JSON. ``path`` is relative to ``API``, or a full URL under it;
        anything else, another host or another Google API on the same host, is refused before
        any request is made, because the bearer token goes with it."""
        url = path if path.startswith("https://") else API + path
        split = urllib.parse.urlsplit(url)
        segments = split.path.split("/")
        if (
            split.scheme != "https"
            or split.hostname != _API_HOST
            or split.port is not None
            or not split.path.startswith(_API_PATH)
            or ".." in segments
            or "." in segments
        ):
            raise ValueError(f"refusing to send the token to {split.hostname!r}{split.path!r}")
        if params:
            url = f"{url}?{urllib.parse.urlencode(dict(params))}"
        # One refresh per call, whether it happens here or on a 401 below: a token minted a
        # moment ago and refused is not going to be helped by another.
        refreshed = False
        if self._token.expired(self._now()):
            self.refresh()
            refreshed = True
        attempts = 0
        while True:
            response = self._transport(self._request(url))
            if response.status == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise ApiError(200, "Google Calendar answered 200 with something unreadable")
                return data
            if response.status == 401:
                if refreshed:
                    raise AuthError(
                        "Google Calendar refused the access token again after a refresh; "
                        "run sign-in again"
                    )
                self.refresh()
                refreshed = True
                continue
            reason = _reason(response)
            rate = response.status == 429 or (response.status == 403 and reason in _RATE_REASONS)
            if rate or response.status in _RETRY_STATUSES:
                attempts += 1
                if attempts >= self._max_attempts:
                    if rate:
                        raise RateLimited(
                            f"Google Calendar rate-limited every one of {attempts} attempts; "
                            "try later"
                        )
                    raise ApiError(response.status, self._describe(response), reason)
                self._sleep(_delay(response, attempts))
                continue
            raise ApiError(response.status, self._describe(response), reason)

    def _request(self, url: str) -> Request:
        access = self._token.access_token
        bearer = "" if access is None else access.get_secret_value()
        return Request(
            "GET", url, {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}, None
        )

    def _describe(self, response: Response) -> str:
        data = response.json()
        detail = ""
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                detail = f"{error.get('status', '')} {error.get('message', '')}".strip()
            elif isinstance(error, str):
                detail = f"{error} {data.get('error_description', '')}".strip()
        detail = _one_line(self._scrub(detail))[:200]
        return f"Google Calendar answered {response.status}" + (f": {detail}" if detail else "")

    def refresh(self) -> Token:
        """Trade the refresh token for a new access token, and save it when there is a store.
        Raises ``AuthError`` when the token endpoint refuses, which means the owner signs in
        again; nothing retries a refresh, because a revoked grant does not come back."""
        old = self._token
        response = self._transport(
            _token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": old.refresh_token.get_secret_value(),
                    "client_id": old.client_id,
                    "client_secret": old.client_secret.get_secret_value(),
                }
            )
        )
        data = response.json()
        if response.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            raise AuthError(
                f"Google would not refresh the token ({self._describe(response)}); "
                "run sign-in again"
            )
        self._token = _token_from(
            old.client_id,
            old.client_secret,
            old.refresh_token.get_secret_value(),
            data,
            self._now(),
        )
        self._secrets.update(self._token.secrets())
        if self._store is not None:
            self._store.save(self._token)
        return self._token


def _reason(response: Response) -> str | None:
    data = response.json()
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        return reason if isinstance(reason, str) else None
    return None


def _delay(response: Response, attempt: int) -> float:
    """``Retry-After`` in seconds when the API gives one, else ``1, 2, 4, 8...``, at most 60."""
    retry_after = _header(response.headers, "Retry-After")
    if retry_after is not None and retry_after.strip().isdigit():
        return float(min(int(retry_after.strip()), 60))
    return float(min(2 ** (attempt - 1), 60))


# --- the source ----------------------------------------------------------------------------------


class CalendarSource:
    """The ``connector.Source`` for one calendar. ``calendar_id`` is ``primary`` (the owner's
    own) unless another calendar the owner can read is named."""

    name = SOURCE

    def __init__(
        self,
        client: CalendarClient,
        *,
        calendar_id: str = "primary",
        page_size: int = 250,
        overlap_seconds: int = 3600,
    ) -> None:
        if not 1 <= page_size <= 2500:
            raise ValueError("page_size must be between 1 and 2500")
        if not calendar_id:
            raise ValueError("calendar_id must not be empty")
        if overlap_seconds < 0:
            raise ValueError("overlap_seconds must not be negative: it would skip changes")
        self._client = client
        self._calendar_id = calendar_id
        self._page_size = page_size
        self._overlap = overlap_seconds

    def pull(self, cursor: Cursor) -> list[Item]:
        """Every event changed since the cursor, less the overlap, oldest change first. Raises
        on any failure, and the base then appends nothing."""
        since: str | None = None
        if cursor.observed_at is not None:
            moment = datetime.fromisoformat(cursor.observed_at).astimezone(UTC)
            since = (moment - timedelta(seconds=self._overlap)).isoformat(timespec="microseconds")
        try:
            items = self._list(since)
        except ApiError as error:
            if since is None or error.status != 410 or error.reason != _TOO_OLD:
                raise
            # The cursor is advice (task 54). Google will not answer it, so ask for everything
            # once; the content fold appends only what the stream does not hold.
            items = self._list(None)
        items.sort(key=lambda item: datetime.fromisoformat(item.observed_at))
        return items

    def _list(self, since: str | None) -> list[Item]:
        path = f"/calendars/{urllib.parse.quote(self._calendar_id, safe='')}/events"
        base: dict[str, str] = {
            "maxResults": str(self._page_size),
            "showDeleted": "true",
            "singleEvents": "false",
        }
        if since is not None:
            base["updatedMin"] = since
        items: list[Item] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params = dict(base)
            if page_token is not None:
                params["pageToken"] = page_token
            page = self._client.get(path, params)
            entries = page.get("items") or []
            if not isinstance(entries, list):
                raise ApiError(200, "events.list came back without a list of items")
            zone = page.get("timeZone")
            listed = page.get("updated")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ApiError(200, "events.list holds an event that is not an object")
                try:
                    observed_at, body = render_event(
                        entry,
                        calendar_id=self._calendar_id,
                        calendar_time_zone=zone if isinstance(zone, str) else None,
                        listed_updated=listed if isinstance(listed, str) else None,
                    )
                except ValueError as error:
                    raise ApiError(200, str(error)) from None
                items.append(
                    Item(observed_at=observed_at, body=body, source_version=SOURCE_VERSION)
                )
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return items
            if next_token in seen_tokens:
                raise ApiError(200, "events.list handed back a page token it already gave")
            seen_tokens.add(next_token)
            page_token = next_token


# --- rendering an event --------------------------------------------------------------------------


def render_event(
    event: Mapping[str, Any],
    *,
    calendar_id: str,
    calendar_time_zone: str | None = None,
    listed_updated: str | None = None,
) -> tuple[str, str]:
    """One event of ``events.list`` to ``(observed_at, body)``. Pure: no IO, no clock.

    ``calendar_time_zone`` is the zone a time with neither offset nor zone of its own is read
    in; ``listed_updated`` is the list's own ``updated``, used only for a cancelled event that
    came without one. Raises ``ValueError`` for an event whose change time cannot be known.
    Everything in the body but the connector's own words is untrusted text (D55).
    """
    event_id = _one_line(event.get("id"))
    status = _one_line(event.get("status")) or "confirmed"
    lines = [f"Google Calendar event {event_id} on calendar {_one_line(calendar_id)}"]
    lines.append(f"Status: {status}")

    updated = event.get("updated")
    if isinstance(updated, str) and updated:
        observed_at = _log_instant(updated, event_id)
    elif status == "cancelled" and listed_updated:
        observed_at = _log_instant(listed_updated, event_id)
        lines.append("Changed: Google gave no time; the calendar's last change is used")
    else:
        raise ValueError(f"event {event_id!r} came back without an updated time")

    if summary := _one_line(event.get("summary")):
        lines.append(f"Title: {summary}")
    lines.extend(_when(event.get("start"), event.get("end"), calendar_time_zone))
    recurrence = event.get("recurrence")
    if isinstance(recurrence, list):
        lines.extend(f"Repeats: {rule}" for r in recurrence if (rule := _one_line(r)))
    if series := _one_line(event.get("recurringEventId")):
        original = event.get("originalStartTime")
        at = _point_text(original, calendar_time_zone) if isinstance(original, dict) else ""
        lines.append(f"Instance of recurring event {series}" + (f", originally {at}" if at else ""))
    if location := _one_line(event.get("location")):
        lines.append(f"Location: {location}")
    organizer = event.get("organizer")
    if isinstance(organizer, dict) and (who := _person(organizer, answer=False)):
        lines.append(f"Organizer: {who}")
    attendees = event.get("attendees")
    if isinstance(attendees, list):
        people = [p for a in attendees if isinstance(a, dict) and (p := _person(a, answer=True))]
        if people:
            shown = ", ".join(people[:MAX_ATTENDEES])
            more = len(people) - MAX_ATTENDEES
            lines.append(f"Attendees: {shown}" + (f", and {more} more" if more > 0 else ""))
    attachments = event.get("attachments")
    if isinstance(attachments, list) and attachments:
        kinds: list[str] = []
        for a in attachments:
            kind = _one_line(a.get("mimeType")) if isinstance(a, dict) else ""
            kind = kind or "unknown type"
            if kind not in kinds:
                kinds.append(kind)
        lines.append(f"Attachments not read: {len(attachments)} ({', '.join(kinds)})")

    sections = ["\n".join(lines)]
    description = event.get("description")
    if isinstance(description, str) and description.strip():
        text = _html_to_text(description) if _HTML_TAG.search(description) else description
        text = _tidy(text)
        if len(text) > MAX_TEXT:
            text = f"{text[:MAX_TEXT]}\n[truncated: {len(text) - MAX_TEXT} more characters]"
        if text:
            sections.append(text)
    return observed_at, "\n\n".join(sections)


def _one_line(value: Any) -> str:
    """Any value as one line of text, cut at ``MAX_LINE``. A line break in a field could
    otherwise end the header block or open a turn."""
    if value is None or isinstance(value, dict | list):
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_LINE else f"{text[:MAX_LINE]} [cut]"


def _log_instant(value: str, event_id: str) -> str:
    """An RFC 3339 time from Google, in the log's one spelling: UTC, microseconds (D93)."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"event {event_id!r} has an unreadable updated time") from None
    if moment.tzinfo is None:
        raise ValueError(f"event {event_id!r} has an updated time with no offset")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _zone(name: str) -> ZoneInfo | None:
    """The zone by IANA name, or ``None`` when this machine does not know it. The name is the
    sender's text; ``zoneinfo`` refuses a name shaped like a path."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


@dataclass(frozen=True)
class _Point:
    text: str
    """The time as the calendar wrote it."""
    label: str | None
    """The zone, as it will be shown. Two ends with one label share it."""
    utc: datetime | None
    """The moment, when it is known."""


def _point(point: Mapping[str, Any], default_zone: str | None) -> _Point:
    text = _one_line(point.get("dateTime"))
    zone = _one_line(point.get("timeZone")) or (_one_line(default_zone) or None)
    known = _zone(zone) if zone else None
    label = None if zone is None else zone if known else f"{zone}, a time zone not known here"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return _Point(text, label, None)
    if moment.tzinfo is None:
        if known is None:
            if zone is None:
                label = "no time zone given"
            return _Point(text, label, None)
        early, late = moment.replace(tzinfo=known, fold=0), moment.replace(tzinfo=known, fold=1)
        if early.utcoffset() != late.utcoffset():
            # The clock went back (the time happens twice) or forward (it never happens). A
            # UTC time chosen here would be a guess; say so instead of picking one.
            return _Point(text, f"{label}, at a clock change, so ambiguous", None)
        moment = early
    return _Point(text, label, moment.astimezone(UTC))


def _point_text(point: Mapping[str, Any], default_zone: str | None) -> str:
    if "date" in point and "dateTime" not in point:
        return f"{_one_line(point.get('date'))} (all day)"
    p = _point(point, default_zone)
    return p.text + (f" ({p.label})" if p.label else "")


def _when(start: Any, end: Any, default_zone: str | None) -> list[str]:
    """The ``When`` line, and ``In UTC`` when both moments are known."""
    if not isinstance(start, dict):
        return []
    if "date" in start and "dateTime" not in start:
        return [_all_day(start, end)]
    if not _one_line(start.get("dateTime")):
        return []
    first = _point(start, default_zone)
    last = _point(end, default_zone) if isinstance(end, dict) and end.get("dateTime") else None
    if last is None:
        lines = [f"When: {first.text}" + (f" ({first.label})" if first.label else "")]
        return lines + ([f"In UTC: {first.utc.isoformat()}"] if first.utc else [])
    if first.label == last.label:
        line = f"When: {first.text} to {last.text}" + (f" ({first.label})" if first.label else "")
    else:
        line = (
            f"When: {first.text}"
            + (f" ({first.label})" if first.label else "")
            + f" to {last.text}"
            + (f" ({last.label})" if last.label else "")
        )
    lines = [line]
    if first.utc is not None and last.utc is not None:
        lines.append(f"In UTC: {first.utc.isoformat()} to {last.utc.isoformat()}")
    return lines


def _all_day(start: Mapping[str, Any], end: Any) -> str:
    """An all-day event. Google's end date is the day after the last; show the last."""
    raw_start = _one_line(start.get("date"))
    try:
        first = date.fromisoformat(raw_start)
    except ValueError:
        return f"When: all day on {raw_start}"
    last = first
    if isinstance(end, dict):
        try:
            last = date.fromisoformat(_one_line(end.get("date"))) - timedelta(days=1)
        except ValueError:
            last = first
    if last <= first:
        return f"When: all day on {first.isoformat()}"
    return f"When: all day, {first.isoformat()} through {last.isoformat()}"


def _person(entry: Mapping[str, Any], *, answer: bool) -> str:
    """``Name <address> (you, accepted)``, with whatever parts the entry has."""
    name = _one_line(entry.get("displayName"))
    email = _one_line(entry.get("email"))
    who = f"{name} <{email}>" if name and email else name or email
    if not who:
        return ""
    notes: list[str] = []
    if entry.get("self") is True:
        notes.append("you")
    if answer:
        status = entry.get("responseStatus")
        if isinstance(status, str) and status in _ANSWERS:
            notes.append(_ANSWERS[status])
    return who + (f" ({', '.join(notes)})" if notes else "")


_HTML_TAGS = (
    "a|b|i|u|s|em|strong|small|sub|sup|span|font|br|p|div|ul|ol|li|h[1-6]|blockquote|pre|code"
    "|table|thead|tbody|tr|td|th|hr|img|html|head|body|title|script|style"
)
_HTML_TAG = re.compile(rf"</?(?:{_HTML_TAGS})(?=[\s/>])", re.IGNORECASE)
"""What makes a description HTML: one of the tags Google's editor writes, or the ones a page
would. Anything else shaped like a tag, a forged envelope marker above all, is text."""
_NOT_A_TAG = re.compile(rf"<(?!/?(?:{_HTML_TAGS})(?=[\s/>]))", re.IGNORECASE)


class _TextOf(HTMLParser):
    """The text of an HTML description: scripts and styles dropped, block tags as line
    breaks. Text styled to be invisible is kept: the gate must see what a model would."""

    _SKIP = frozenset({"script", "style", "head", "title"})
    _BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
        | {"blockquote", "pre", "table", "ul", "ol", "hr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self.pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.pieces.append(data)


def _html_to_text(html: str) -> str:
    """The text of ``html``. Every ``<`` that does not open a known tag is escaped first, so
    the parser keeps it as text: a forged ``<<<untrusted:...>>>`` marker or an HTML comment
    reaches the gate as written instead of vanishing as an unknown tag."""
    parser = _TextOf()
    parser.feed(_NOT_A_TAG.sub("&lt;", html))
    parser.close()
    return "".join(parser.pieces)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


# --- sign-in -------------------------------------------------------------------------------------


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(client_id: str, redirect_uri: str, *, state: str, code_challenge: str) -> str:
    """The consent page, asking for ``SCOPES`` and a refresh token, with PKCE, and for no
    scope granted to this client before: the token is for reading events and nothing else."""
    return f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )


def exchange_code(
    transport: Transport,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    now: Callable[[], float] = time.time,
) -> Token:
    response = transport(
        Request(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                }
            ).encode(),
        )
    )
    data = response.json()
    if (
        response.status != 200
        or not isinstance(data, dict)
        or not data.get("access_token")
        or not data.get("refresh_token")
    ):
        kind = data.get("error") if isinstance(data, dict) else None
        detail = f" ({_one_line(kind)[:80]})" if isinstance(kind, str) else ""
        raise AuthError(f"Google would not exchange the sign-in code{detail}; try sign-in again")
    return _token_from(client_id, SecretStr(client_secret), str(data["refresh_token"]), data, now())


def sign_in(
    client_id: str,
    client_secret: str,
    *,
    transport: Transport = urllib_transport,
    open_url: Callable[[str], object] = webbrowser.open,
    timeout: float = 300.0,
    now: Callable[[], float] = time.time,
) -> Token:
    """The installed-app flow on a loopback port: open the consent page, take one redirect,
    check its ``state``, exchange the code with PKCE. Nothing is exchanged on a wrong state,
    a refusal, or a timeout."""
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "code" not in query and "error" not in query:
                # A favicon request, or a stray tab: answer it and keep waiting.
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            received.update({k: v[0] for k, v in query.items()})
            page = (
                b"<!doctype html><html><body><p>walle: signed in. You can close this tab and "
                b"return to the terminal.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    deadline = time.monotonic() + timeout
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        open_url(
            authorization_url(
                client_id, redirect_uri, state=state, code_challenge=pkce_challenge(verifier)
            )
        )
        while not received and (remaining := deadline - time.monotonic()) > 0:
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        raise AuthError(f"sign-in timed out after {timeout:g} seconds with no consent")
    if "error" in received:
        raise AuthError(f"sign-in refused: {_one_line(received['error'])[:80]}")
    if received.get("state") != state:
        raise AuthError("sign-in redirect carried the wrong state; nothing was exchanged")
    code = received.get("code")
    if not code:
        raise AuthError("sign-in redirect carried no code")
    return exchange_code(
        transport,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        now=now,
    )


# --- putting it together -----------------------------------------------------------------------


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def sync_calendar(
    root: Path,
    principal: str,
    device: str,
    token_path: Path,
    *,
    calendar_id: str = "primary",
    transport: Transport = urllib_transport,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.time,
    page_size: int = 250,
    overlap_seconds: int = 3600,
) -> SyncReport:
    """One sync of one calendar into one stream. Refuses a token file under the walle root:
    tokens live outside the log and outside the repo (issue #57)."""
    root = Path(root)
    token_path = Path(token_path)
    if _is_under(token_path, root):
        raise ValueError(
            f"the token file must live outside the walle root {root}; "
            "tokens are never kept in or beside the log"
        )
    store = TokenStore(token_path)
    client = CalendarClient(store.load(), transport=transport, store=store, sleep=sleep, now=now)
    source = CalendarSource(
        client, calendar_id=calendar_id, page_size=page_size, overlap_seconds=overlap_seconds
    )
    return sync(source, StreamWriter(root, principal, device))


@click.group()
def main() -> None:
    """Google Calendar, read-only, into the walle log. Run as ``python -m
    walle.google_calendar``."""


@main.command("sign-in")
@click.option(
    "--client-id", required=True, help="The OAuth client id of your own Google Cloud project."
)
@click.option(
    "--client-secret",
    prompt=True,
    hide_input=True,
    help="Its client secret. Prompted for, so it stays out of your shell history.",
)
@click.option(
    "--token-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where to keep the token. Default: $XDG_CONFIG_HOME/walle/google-calendar-token.json.",
)
def sign_in_command(client_id: str, client_secret: str, token_file: Path | None) -> None:
    """Open the consent page (read-only scope), take the redirect, write the token file."""
    path = token_file or default_token_path()
    try:
        token = sign_in(client_id, client_secret)
    except CalendarError as error:
        raise click.ClickException(str(error)) from None
    TokenStore(path).save(token)
    click.echo(f"signed in; token written to {path} (scope: {READONLY_SCOPE})")


@main.command("sync")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="The folder that holds principals/.",
)
@click.option("--principal", required=True, help="Whose log, without the p- prefix.")
@click.option("--device", required=True, help="This device's stream, without the d- prefix.")
@click.option(
    "--token-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The token file sign-in wrote. Default: "
    "$XDG_CONFIG_HOME/walle/google-calendar-token.json.",
)
@click.option(
    "--calendar",
    "calendar_id",
    default="primary",
    show_default=True,
    help="Which calendar: primary, or the id of another calendar you can read.",
)
def sync_command(
    root: Path, principal: str, device: str, token_file: Path | None, calendar_id: str
) -> None:
    """Append every event that changed since the last sync."""
    path = token_file or default_token_path()
    try:
        report = sync_calendar(root, principal, device, path, calendar_id=calendar_id)
    except FileNotFoundError:
        raise click.ClickException(
            f"no token file at {path}; run `python -m walle.google_calendar sign-in` first"
        ) from None
    except (ValueError, CalendarError) as error:
        raise click.ClickException(str(error)) from None
    click.echo(
        f"google-calendar: appended {len(report.appended)}, "
        f"already logged {len(report.already_logged)}; "
        f"cursor {report.after.observed_at or 'none'} over {report.after.events} observations"
    )


if __name__ == "__main__":
    main()
