"""A local fake of the Google Calendar API and Google's token endpoint, for
``test_google_calendar.py``.

Nothing here touches the network. The fake is a ``Transport``: it is handed each ``Request``
the connector would have sent and answers with a ``Response`` in the shapes the real API uses
(``events.list`` on one calendar, the OAuth token endpoint), so the connector under test is the
one that will run against Google, with only the wire swapped.

Everything in it is invented: the addresses use the RFC 2606 domains, the tokens are strings
that say they are fake, and the events are written for these tests. No real account, token or
calendar ever reaches this file (issue #57).

The fake is also a witness. It records every request, answers anything that is not a ``GET``
with a 405, refuses ``singleEvents=true`` (the connector must never ask Google to expand a
recurring series), and raises outright if asked for anything but ``events.list``, so a test can
show not only what the connector did but what it never did.

How the fake decides what to list follows Google's documented rules for ``events.list``:

* ``updatedMin`` keeps events whose ``updated`` is at or after it, and, when given, deleted
  events are included whatever ``showDeleted`` says.
* Without ``updatedMin``, a cancelled event is listed only with ``showDeleted=true``.
* An ``updatedMin`` older than ``too_old_before`` is answered ``410 updatedMinTooLongAgo``.
* A cancelled event may come back with only ``id`` (and, for an instance of a series,
  ``recurringEventId`` and ``originalStartTime``): ``hidden_updated`` holds the time the fake
  filters it by, which the real API also knows and does not send.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from walle.google_calendar import API, TOKEN_URL, Request, Response

ACCESS = "FAKE-ACCESS-TOKEN-NOT-REAL-0101"
REFRESH = "FAKE-REFRESH-TOKEN-NOT-REAL-0102"
CLIENT_ID = "fake-calendar-client-id.apps.example.com"
CLIENT_SECRET = "FAKE-CLIENT-SECRET-NOT-REAL-0103"
SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(
        status=status,
        headers={"Content-Type": "application/json; charset=UTF-8", **(headers or {})},
        body=json.dumps(payload).encode(),
    )


def _error(
    status: int, message: str, reason: str, headers: dict[str, str] | None = None
) -> Response:
    return _json(
        status,
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason}],
            }
        },
        headers,
    )


def when(value: str, zone: str | None = None) -> dict[str, str]:
    """A ``start`` or ``end``: a date (``YYYY-MM-DD``, all day) or a date-time."""
    key = "date" if len(value) == 10 else "dateTime"
    out = {key: value}
    if zone is not None:
        out["timeZone"] = zone
    return out


def event(
    event_id: str,
    *,
    updated: str | None,
    summary: str | None = "Event",
    start: dict[str, str] | None = None,
    end: dict[str, str] | None = None,
    status: str = "confirmed",
    **extra: Any,
) -> dict[str, Any]:
    """One event in the ``events`` resource shape. ``extra`` goes in as given."""
    out: dict[str, Any] = {"kind": "calendar#event", "id": event_id, "status": status}
    if updated is not None:
        out["updated"] = updated
    if summary is not None:
        out["summary"] = summary
    if start is not None:
        out["start"] = start
    if end is not None:
        out["end"] = end
    out.update(extra)
    return out


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class FakeCalendar:
    """The fake API. Call it as a transport; read its attributes as a witness."""

    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Calendar id to its events, in the order the API would return them."""
    time_zones: dict[str, str] = field(default_factory=dict)
    """Calendar id to the calendar's own time zone, as ``events.list`` reports it."""
    hidden_updated: dict[str, str] = field(default_factory=dict)
    """Event id to the ``updated`` time the fake filters by when the event does not carry one."""
    calendar_updated: str = "2026-11-04T12:00:00.000Z"
    """The collection's ``updated``: the calendar's last modification time."""
    too_old_before: str | None = None
    """An ``updatedMin`` earlier than this is answered ``410 updatedMinTooLongAgo``."""
    access_token: str = ACCESS
    refresh_token: str = REFRESH
    requests: list[Request] = field(default_factory=list)
    """Every request received, in order."""
    rate_limits: list[tuple[int, str | None]] = field(default_factory=list)
    """Pending rate-limit replies ``(status, retry_after_header)``, consumed one per API call."""
    failures: list[int] = field(default_factory=list)
    """Pending plain failures by status, consumed one per API call, after the rate limits."""
    fail_page: dict[str, int] = field(default_factory=dict)
    """``{page_token: status}``: the page asked for with that token fails with ``status``, every
    time it is asked for. Pages after the first are the ones a partial sync would leave."""
    refresh_ok: bool = True
    """Whether a refresh grant is honoured. ``False`` answers ``invalid_grant``."""
    echo_token_in_errors: bool = False
    """An adversarial mode: error bodies quote the Authorization header back, as some proxies
    do, so a test can show the connector's own messages never carry it."""
    refreshed: int = 0

    # --- building the calendar --------------------------------------------------------

    def add(
        self, calendar_id: str, ev: dict[str, Any], *, hidden_updated: str | None = None
    ) -> None:
        self.events.setdefault(calendar_id, []).append(ev)
        if hidden_updated is not None:
            self.hidden_updated[ev["id"]] = hidden_updated

    def replace(self, calendar_id: str, ev: dict[str, Any]) -> None:
        """The event with ``ev``'s id is changed in place, as an edit on the calendar is."""
        found = self.events[calendar_id]
        for i, old in enumerate(found):
            if old["id"] == ev["id"]:
                found[i] = ev
                return
        raise KeyError(ev["id"])

    def expire_token(self) -> None:
        """The current access token stops working; a refresh will mint a new one."""
        self.access_token = "FAKE-ACCESS-TOKEN-NOT-REAL-9999-EXPIRED"

    # --- the witness ------------------------------------------------------------------

    @property
    def api_requests(self) -> list[Request]:
        return [r for r in self.requests if r.url.startswith(API)]

    @property
    def token_requests(self) -> list[Request]:
        return [r for r in self.requests if r.url == TOKEN_URL]

    @property
    def list_params(self) -> list[dict[str, str]]:
        return [
            {k: v[0] for k, v in parse_qs(urlsplit(r.url).query).items()} for r in self.api_requests
        ]

    # --- the transport ----------------------------------------------------------------

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if request.url == TOKEN_URL:
            return self._token(request)
        if request.url.startswith(API + "/"):
            return self._api(request)
        return _json(404, {"error": {"code": 404, "message": "unknown host or path"}})

    def _token(self, request: Request) -> Response:
        if request.method != "POST":
            return _json(405, {"error": "method not allowed"})
        form = {k: v[0] for k, v in parse_qs((request.body or b"").decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return _json(401, {"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "refresh_token":
            if not self.refresh_ok or form.get("refresh_token") != self.refresh_token:
                return _json(
                    400,
                    {
                        "error": "invalid_grant",
                        "error_description": "Token has been expired or revoked.",
                    },
                )
            self.refreshed += 1
            self.access_token = f"FAKE-ACCESS-TOKEN-NOT-REAL-{1100 + self.refreshed:04d}"
            return _json(
                200,
                {
                    "access_token": self.access_token,
                    "expires_in": 3599,
                    "scope": SCOPE,
                    "token_type": "Bearer",
                },
            )
        if grant == "authorization_code":
            if form.get("code") != "fake-auth-code" or not form.get("code_verifier"):
                return _json(400, {"error": "invalid_grant"})
            return _json(
                200,
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": 3599,
                    "scope": SCOPE,
                    "token_type": "Bearer",
                },
            )
        return _json(400, {"error": "unsupported_grant_type"})

    def _api(self, request: Request) -> Response:
        split = urlsplit(request.url)
        path = split.path.removeprefix(urlsplit(API).path)
        if request.method != "GET":
            return _error(405, "method not allowed", "methodNotAllowed")
        parts = path.split("/")
        # Only ``/calendars/{id}/events`` exists here. Anything else is a request the
        # connector has no business making, and the test that caused it should fail loudly.
        if len(parts) != 4 or parts[1] != "calendars" or parts[3] != "events":
            raise AssertionError(
                f"the connector asked for something other than events.list: {path}"
            )
        calendar_id = unquote(parts[2])
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.access_token}":
            quoted = f" ({auth})" if self.echo_token_in_errors else ""
            return _error(
                401, f"Request had invalid authentication credentials.{quoted}", "authError"
            )
        if self.rate_limits:
            status, retry_after = self.rate_limits.pop(0)
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            reason = "rateLimitExceeded" if status == 429 else "userRateLimitExceeded"
            return _error(status, "Rate Limit Exceeded", reason, headers)
        if self.failures:
            return _error(self.failures.pop(0), "Backend Error", "backendError")
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        failing = self.fail_page.get(params.get("pageToken", ""))
        if failing is not None:
            return _error(failing, "Backend Error", "backendError")
        return self._list(calendar_id, params)

    def _updated(self, ev: dict[str, Any]) -> str | None:
        return ev.get("updated") or self.hidden_updated.get(ev["id"])

    def _list(self, calendar_id: str, params: dict[str, str]) -> Response:
        if params.get("singleEvents", "false") != "false":
            return _error(400, "the fake never expands a series", "badRequest")
        if calendar_id not in self.events:
            return _error(404, "Not Found", "notFound")
        since = params.get("updatedMin")
        too_old = self.too_old_before
        if since is not None and too_old is not None and _moment(since) < _moment(too_old):
            return _error(
                410,
                "The requested minimum modification time lies too far in the past.",
                "updatedMinTooLongAgo",
            )
        show_deleted = params.get("showDeleted") == "true" or since is not None
        listed: list[dict[str, Any]] = []
        for ev in self.events[calendar_id]:
            if ev.get("status") == "cancelled" and not show_deleted:
                continue
            if since is not None:
                updated = self._updated(ev)
                if updated is None or _moment(updated) < _moment(since):
                    continue
            listed.append(ev)
        size = int(params.get("maxResults", "250"))
        start = int(params["pageToken"].removeprefix("page-")) if "pageToken" in params else 0
        page = listed[start : start + size]
        payload: dict[str, Any] = {
            "kind": "calendar#events",
            "summary": calendar_id,
            "updated": self.calendar_updated,
            "timeZone": self.time_zones.get(calendar_id, "UTC"),
            "accessRole": "owner",
            "items": page,
        }
        if start + size < len(listed):
            payload["nextPageToken"] = f"page-{start + size}"
        else:
            payload["nextSyncToken"] = "fake-sync-token-never-used"
        return _json(200, payload)
