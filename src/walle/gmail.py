"""Gmail, read-only, on the connector base (D55, D77, D92, D93).

The first live provider for ``walle.connector``. It signs the owner in with OAuth, asks Gmail
for the threads that changed since the cursor, and turns each message into an ``Item`` the
base appends as an ``observation``. The base owns the cursor, the lock and the duplicate
check; this module owns only what is Gmail's: the wire, the token, the scope, and the shape
of a message.

Four rules, each with a test that would fail without it (``tests/test_gmail.py``):

* **One scope, and it is read-only.** ``SCOPES`` is the ``gmail.readonly`` scope and nothing
  else, named in exactly one place. Every request to the Gmail API is a ``GET``; the only
  ``POST`` goes to the token endpoint. There is no code path that could send, modify, label,
  trash or draft a mail, because nothing here knows those endpoints exist.
* **Attachments are never read.** A part with a filename or an ``attachmentId`` is noted by
  type and size and skipped. ``users.messages.attachments.get`` is never called, and the fake
  in the tests raises if it is. A PDF, an image or a calendar file in a mail is where a payload
  would hide, and a memory layer has no business opening it.
* **Tokens live outside the log and outside the repo.** A ``TokenStore`` writes one private
  file (``0600``, in a ``0700`` folder) at a path the owner chooses, by default under the user's
  config folder, and ``sync_gmail`` refuses a path under the walle root. No token is ever in an
  ``Item``, and no error message carries one: every message is scrubbed of every secret the
  client has held before it is raised.
* **Mail text is untrusted (D55).** Headers, bodies, sender names: all of it was written by
  someone who is not the principal, and some of it was written for the model. The body the
  connector produces never opens with a turn label, so any ``user:`` line in a mail is a forged
  turn to ``walle.hazards``; header values are folded onto one line so none can open one.
  Email is the likeliest route for a planted instruction, so the injection corpus gains seven
  e-mail shapes (``tests/corpora/generate.py``, ``mail-*``), and each one, rendered as this
  module renders a message, is flagged by the gate the proposer uses.

Why the stdlib and not Google's client
--------------------------------------
``tests/test_constraints.py`` allows four runtime dependencies, and Google's is not among them
(D87: libraries, not frameworks; constraint 7: no third-party code without a decision). The
connector needs three request shapes and a token refresh, which ``urllib`` covers in forty
lines. The wire is a ``Transport``: a callable from ``Request`` to ``Response``, so the tests
run the whole connector against a local fake of the API and nothing here is special-cased for
testing. ``urllib_transport`` is the real one: it follows no redirect, so a token is never
carried to a host Google did not name, and it sends plain ``http`` only to the loopback, which
the sign-in redirect needs.

What a message becomes
----------------------
::

    Gmail message <id> in thread <threadId>
    From: ...
    To: ...
    Cc: ...                (only when present)
    Subject: ...
    Date: ...              (the sender's claim; ``observed_at`` is Gmail's ``internalDate``)
    Message-ID: ...

    <the message's own text, with quoted earlier messages removed>

    [quoted earlier message omitted]
    [attachment not read: application/pdf, 48213 bytes]

``observed_at`` is ``internalDate``, the moment Gmail received the message, in the log's one
spelling (D93). The ``Date`` header is kept as text because a reader wants it, and not used
as time because the sender wrote it. Plain text is preferred; an HTML-only message is reduced
to its text with scripts and styles dropped. Quoted replies are removed so a thread of ten
replies does not log the first mail ten times: ``>`` lines, the ``On ... wrote:`` line that
introduces them (even wrapped over two lines), and everything after an Outlook-style
separator. Bottom-posted text after a quote survives. A body longer than ``MAX_TEXT`` is cut
with a note. Only the headers a reader needs are kept; ``Received``, ``Delivered-To`` and the
``X-`` headers are routing and software, not what the mail said.

The cursor (D92, D93)
---------------------
``pull`` turns ``cursor.observed_at`` into Gmail's ``after:`` query term, less an overlap, and
lets the base throw away what it already holds. Gmail lists a *thread* when any message in
it matches, then hands back every message in the thread, so a long-running conversation
re-arrives on every sync; the base's content fold appends only the new replies, and the test
``test_a_rerun_with_the_overlap_refetching_a_thread_appends_nothing`` holds that. Nothing is
stored between runs but the log.

Expired tokens and rate limits
------------------------------
An access token past its expiry is refreshed before the first call. One Gmail rejects with
``401`` is refreshed once and the call retried; a second ``401`` is an ``AuthError`` and the
owner signs in again. A refresh the token endpoint refuses (``invalid_grant``: revoked, or the
consent withdrawn) is an ``AuthError`` too. ``429``, a ``403`` whose reason is a rate limit,
and ``5xx`` are retried with ``Retry-After`` when Gmail gives one, else ``1, 2, 4, 8...``
seconds, up to ``max_attempts`` in all; then ``RateLimited`` or ``ApiError``. Every one of
these raises before anything is appended, which is the base's promise (a failed pull appends
nothing), so a sync is whole or it is nothing.

Running it
----------
``python -m walle.gmail sign-in --client-id ...`` opens the consent page in a browser, takes
the redirect on a loopback port, exchanges the code with PKCE, and writes the token file.
``python -m walle.gmail sync --root ... --principal ... --device ...`` appends what is new.
The ``walle`` command gains no subcommand here on purpose: ``cli.py`` is shared with #57 and
one line there wires this in once both connectors have landed.
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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol

import click
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from walle.connector import Cursor, Item, SyncReport, sync
from walle.writer import StreamWriter

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
"""The one scope. Read-only: list, get, and nothing that writes."""

SCOPES: tuple[str, ...] = (READONLY_SCOPE,)
"""Everything the sign-in asks for. A test holds this at exactly the one above."""

API = "https://gmail.googleapis.com/gmail/v1/users/me"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SOURCE = "gmail"
"""The ``source`` on every observation. One source, one name, forever (D92)."""

SOURCE_VERSION = "gmail-v1"
"""The API version the items came through (D101)."""

MAX_TEXT = 16_000
"""Characters of a message's own text kept before it is cut with a note."""

_HOSTS = frozenset({"gmail.googleapis.com", "oauth2.googleapis.com"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RATE_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}
)
_KEPT_HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID")
_REDACTED = "[redacted]"


# --- errors ------------------------------------------------------------------------------------


class GmailError(Exception):
    """Anything the connector cannot get past. Its message never carries a token."""


class AuthError(GmailError):
    """The token is no good and a refresh did not help, or the sign-in did not complete."""


class RateLimited(GmailError):
    """Gmail kept answering with a rate limit for every attempt allowed."""


class ApiError(GmailError):
    """Gmail answered with an error that is not about the token and was not a rate limit."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


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
    to a host Google did not name; refuses plain ``http`` anywhere but the loopback."""
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
        raise GmailError(f"could not reach {url.hostname}: {error.reason}") from error


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
        """Whether the access token needs refreshing before use. Unknown is expired: a
        missing or unreadable ``expires_at`` costs one refresh, never a crash."""
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
    walle root (``sync_gmail`` refuses that) and never in the repo."""

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
                f"{self.path}: not a walle gmail token file (at {', '.join(where)})"
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
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".gmail-token.", suffix=".tmp")
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
    """``$XDG_CONFIG_HOME/walle/gmail-token.json``, or ``~/.config/walle/gmail-token.json``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    config = Path(base) if base else Path.home() / ".config"
    return config / "walle" / "gmail-token.json"


def _instant(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="microseconds")


# --- the client ----------------------------------------------------------------------------------


class GmailClient:
    """``GET`` on the Gmail API with the owner's token, refreshing and backing off as needed.

    ``sleep`` and ``now`` are injectable so the tests can show the backoff without waiting
    for it. ``store`` receives every refreshed token; without one the refresh is kept in memory
    only, which is right for a one-off.
    """

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
        """One ``GET``, as parsed JSON. ``path`` is relative to ``API`` or a full URL on one
        of the two Google hosts; anything else is refused before any request is made."""
        url = path if path.startswith("https://") else API + path
        host = urllib.parse.urlsplit(url).hostname
        if host not in _HOSTS:
            raise ValueError(f"refusing to send the token to host {host!r}")
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
                    raise ApiError(200, "Gmail answered 200 with something other than an object")
                return data
            if response.status == 401:
                if refreshed:
                    raise AuthError(
                        "Gmail refused the access token again after a refresh; run sign-in again"
                    )
                self.refresh()
                refreshed = True
                continue
            rate = response.status == 429 or (
                response.status == 403 and _reason(response) in _RATE_REASONS
            )
            if rate or response.status in _RETRY_STATUSES:
                attempts += 1
                if attempts >= self._max_attempts:
                    if rate:
                        raise RateLimited(
                            f"Gmail rate-limited every one of {attempts} attempts; try later"
                        )
                    raise ApiError(response.status, self._describe(response))
                self._sleep(_delay(response, attempts))
                continue
            raise ApiError(response.status, self._describe(response))

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
        detail = self._scrub(detail)[:200]
        return f"Gmail answered {response.status}" + (f": {detail}" if detail else "")

    def refresh(self) -> Token:
        """Trade the refresh token for a new access token, and save it when there is a store.
        Raises ``AuthError`` when the token endpoint refuses, which means the owner signs in
        again; nothing retries a refresh, because a revoked grant does not come back."""
        old = self._token
        form = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": old.refresh_token.get_secret_value(),
                "client_id": old.client_id,
                "client_secret": old.client_secret.get_secret_value(),
            }
        ).encode()
        response = self._transport(
            Request(
                "POST",
                TOKEN_URL,
                {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                form,
            )
        )
        data = response.json()
        if response.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            raise AuthError(
                f"Gmail would not refresh the token ({self._describe(response)}); run sign-in again"
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


def _token_from(
    client_id: str,
    client_secret: SecretStr,
    refresh_token: str,
    data: Mapping[str, Any],
    now: float,
) -> Token:
    """A ``Token`` from the token endpoint's reply. The reply may omit ``refresh_token`` (a
    refresh does); ``refresh_token`` is then the one already held."""
    expires_in = data.get("expires_in", 3600)
    lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
    return Token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=SecretStr(str(data.get("refresh_token") or refresh_token)),
        access_token=SecretStr(str(data["access_token"])),
        expires_at=_instant(now + lifetime),
    )


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
    """``Retry-After`` in seconds when Gmail gives one, else ``1, 2, 4, 8...``, at most 60."""
    retry_after = _header(response.headers, "Retry-After")
    if retry_after is not None and retry_after.strip().isdigit():
        return float(min(int(retry_after.strip()), 60))
    return float(min(2 ** (attempt - 1), 60))


# --- the source ----------------------------------------------------------------------------------


class GmailSource:
    """The ``connector.Source`` for one mailbox. ``query`` is any Gmail search the owner wants
    to limit the sync to (``label:inbox``, ``-category:promotions``); ``after:`` is added from
    the cursor."""

    name = SOURCE

    def __init__(
        self,
        client: GmailClient,
        *,
        query: str = "",
        page_size: int = 100,
        overlap_seconds: int = 3600,
    ) -> None:
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        self._client = client
        self._query = " ".join(query.split())
        self._page_size = page_size
        self._overlap = overlap_seconds

    def pull(self, cursor: Cursor) -> list[Item]:
        """Every message in every thread that changed since the cursor, oldest first. Raises
        on any failure, and the base then appends nothing."""
        query = self._query
        if cursor.observed_at is not None:
            since = int(datetime.fromisoformat(cursor.observed_at).timestamp()) - self._overlap
            query = f"{query} after:{max(since, 0)}".strip()
        items: list[Item] = []
        for thread_id in self._thread_ids(query):
            thread = self._client.get(
                f"/threads/{urllib.parse.quote(thread_id, safe='')}", {"format": "full"}
            )
            messages = thread.get("messages")
            if not isinstance(messages, list):
                raise ApiError(200, f"thread {thread_id!r} came back without messages")
            for message in messages:
                if not isinstance(message, dict):
                    raise ApiError(
                        200, f"thread {thread_id!r} holds a message that is not an object"
                    )
                observed_at, body = render_message(message)
                items.append(
                    Item(observed_at=observed_at, body=body, source_version=SOURCE_VERSION)
                )
        items.sort(key=lambda item: datetime.fromisoformat(item.observed_at))
        return items

    def _thread_ids(self, query: str) -> Iterator[str]:
        page_token: str | None = None
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        while True:
            params: dict[str, str] = {"maxResults": str(self._page_size)}
            if query:
                params["q"] = query
            if page_token is not None:
                params["pageToken"] = page_token
            page = self._client.get("/threads", params)
            threads = page.get("threads") or []
            if not isinstance(threads, list):
                raise ApiError(200, "threads.list came back without a list")
            for entry in threads:
                thread_id = entry.get("id") if isinstance(entry, dict) else None
                if isinstance(thread_id, str) and thread_id and thread_id not in seen_ids:
                    seen_ids.add(thread_id)
                    yield thread_id
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return
            if next_token in seen_tokens:
                raise ApiError(200, "threads.list handed back a page token it already gave")
            seen_tokens.add(next_token)
            page_token = next_token


# --- rendering a message -----------------------------------------------------------------------


def render_message(message: Mapping[str, Any]) -> tuple[str, str]:
    """One Gmail message, ``format=full``, to ``(observed_at, body)``. Pure: no IO, no clock.

    Everything in the result is untrusted text (D55). Header values are folded onto one line,
    the first line is always the connector's own, attachments are named by type and size
    only, and nothing is ever fetched from here.
    """
    raw = message.get("internalDate")
    try:
        internal = int(raw)  # type: ignore[arg-type]  # Gmail sends it as a string of digits
    except (TypeError, ValueError):
        raise ValueError(f"message {_one_line(message.get('id'))!r} has no internalDate") from None
    moment = datetime.fromtimestamp(internal // 1000, UTC) + timedelta(milliseconds=internal % 1000)
    observed_at = moment.isoformat(timespec="microseconds")

    payload = message.get("payload") or {}
    msg_id, thread_id = _one_line(message.get("id")), _one_line(message.get("threadId"))
    lines = [f"Gmail message {msg_id} in thread {thread_id}"]
    found = _headers(payload.get("headers") if isinstance(payload, dict) else None)
    for name in _KEPT_HEADERS:
        value = found.get(name.lower())
        if value:
            lines.append(f"{name}: {value}")

    plain: list[str] = []
    html: list[str] = []
    notes: list[str] = []
    if isinstance(payload, dict):
        _collect(payload, plain, html, notes)
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    text, quoted = _strip_quotes(text)
    text = _tidy(text)
    if len(text) > MAX_TEXT:
        text = f"{text[:MAX_TEXT]}\n[truncated: {len(text) - MAX_TEXT} more characters]"
    if quoted:
        notes.insert(0, "[quoted earlier message omitted]")

    sections = ["\n".join(lines)]
    if text:
        sections.append(text)
    if notes:
        sections.append("\n".join(notes))
    return observed_at, "\n\n".join(sections)


def _one_line(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split())


def _headers(entries: Any) -> dict[str, str]:
    """The first value of each header, by lowercased name, folded onto one line. A header
    value with a line break in it could otherwise end the header block or open a turn."""
    found: dict[str, str] = {}
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, value = entry.get("name"), entry.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.lower() not in found:
            found[name.lower()] = _one_line(value)
    return found


def _collect(part: Mapping[str, Any], plain: list[str], html: list[str], notes: list[str]) -> None:
    """Walk the MIME tree. An attachment is noted and never descended into or fetched."""
    mime = str(part.get("mimeType") or "").lower()
    raw_body = part.get("body")
    body: Mapping[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    if part.get("filename") or body.get("attachmentId"):
        size = body.get("size")
        notes.append(
            f"[attachment not read: {mime or 'unknown type'}, "
            f"{size if isinstance(size, int) else 0} bytes]"
        )
        return
    parts = part.get("parts")
    if isinstance(parts, list) and parts:
        for child in parts:
            if isinstance(child, dict):
                _collect(child, plain, html, notes)
        return
    if mime == "text/plain":
        plain.append(_decode(part, body))
    elif mime == "text/html":
        html.append(_decode(part, body))


def _decode(part: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return ""
    charset = "utf-8"
    content_type = _headers(part.get("headers")).get("content-type", "")
    match = re.search(r"charset=\"?([A-Za-z0-9_.:-]+)\"?", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _TextOf(HTMLParser):
    """The text of an HTML part: scripts and styles dropped, block tags as line breaks."""

    _SKIP = frozenset({"script", "style", "head", "title"})
    _BLOCK = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "pre",
            "table",
            "ul",
            "ol",
            "hr",
        }
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
    if not html:
        return ""
    parser = _TextOf()
    parser.feed(html)
    parser.close()
    return "".join(parser.pieces)


_SEPARATOR = re.compile(
    r"^(?:-{2,}\s*Original Message\s*-{2,}|_{8,}|-{3,}\s*Forwarded message\s*-{3,})\s*$",
    re.IGNORECASE,
)


def _strip_quotes(text: str) -> tuple[str, bool]:
    """``text`` without quoted earlier messages, and whether anything was removed.

    Removed: every ``>`` line; an ``On ... wrote:`` line (wrapped over up to three lines)
    when the next non-blank line is a ``>`` line or there is none; everything from an
    Outlook-style separator to the end. Kept: the rest, bottom-posted replies included.
    """
    lines = text.split("\n")
    kept: list[str] = []
    removed = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if _SEPARATOR.match(stripped):
            removed = True
            break
        if stripped.startswith(">"):
            removed = True
            i += 1
            continue
        if stripped.startswith("On ") and (end := _attribution_end(lines, i)) is not None:
            removed = True
            i = end
            continue
        kept.append(lines[i])
        i += 1
    return "\n".join(kept), removed


def _attribution_end(lines: list[str], start: int) -> int | None:
    """If an ``On ... wrote:`` attribution begins at ``start``, the index after it."""
    joined = ""
    for j in range(start, min(start + 3, len(lines))):
        joined = f"{joined} {lines[j].strip()}".strip()
        if joined.endswith("wrote:"):
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k >= len(lines) or lines[k].lstrip().startswith(">"):
                return j + 1
            return None
    return None


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
    """The consent page, asking for ``SCOPES`` and a refresh token, with PKCE."""
    return f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
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
    form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode()
    response = transport(
        Request(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            form,
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
        detail = f" ({kind})" if isinstance(kind, str) else ""
        raise AuthError(f"Gmail would not exchange the sign-in code{detail}; try sign-in again")
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
        # One request at a time until the redirect arrives or the time is up. A browser
        # asks for a favicon too, and that must not count as the answer.
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


def sync_gmail(
    root: Path,
    principal: str,
    device: str,
    token_path: Path,
    *,
    query: str = "",
    transport: Transport = urllib_transport,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.time,
    page_size: int = 100,
    overlap_seconds: int = 3600,
) -> SyncReport:
    """One sync of one mailbox into one stream. Refuses a token file under the walle root:
    tokens live outside the log and outside the repo (issue #58)."""
    root = Path(root)
    token_path = Path(token_path)
    if _is_under(token_path, root):
        raise ValueError(
            f"the token file must live outside the walle root {root}; "
            "tokens are never kept in or beside the log"
        )
    store = TokenStore(token_path)
    client = GmailClient(store.load(), transport=transport, store=store, sleep=sleep, now=now)
    source = GmailSource(client, query=query, page_size=page_size, overlap_seconds=overlap_seconds)
    return sync(source, StreamWriter(root, principal, device))


@click.group()
def main() -> None:
    """Gmail, read-only, into the walle log. Run as ``python -m walle.gmail``."""


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
    help="Where to keep the token. Default: $XDG_CONFIG_HOME/walle/gmail-token.json.",
)
def sign_in_command(client_id: str, client_secret: str, token_file: Path | None) -> None:
    """Open the consent page (read-only scope), take the redirect, write the token file."""
    path = token_file or default_token_path()
    try:
        token = sign_in(client_id, client_secret)
    except GmailError as error:
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
    help="The token file sign-in wrote. Default: $XDG_CONFIG_HOME/walle/gmail-token.json.",
)
@click.option("--query", default="", help="A Gmail search to limit the sync to, e.g. label:inbox.")
def sync_command(
    root: Path, principal: str, device: str, token_file: Path | None, query: str
) -> None:
    """Append every message that is new since the last sync."""
    path = token_file or default_token_path()
    try:
        report = sync_gmail(root, principal, device, path, query=query)
    except FileNotFoundError:
        raise click.ClickException(
            f"no token file at {path}; run `python -m walle.gmail sign-in` first"
        ) from None
    except (ValueError, GmailError) as error:
        raise click.ClickException(str(error)) from None
    click.echo(
        f"gmail: appended {len(report.appended)}, already logged {len(report.already_logged)}; "
        f"cursor {report.after.observed_at or 'none'} over {report.after.events} observations"
    )


if __name__ == "__main__":
    main()
