"""A local fake of the Gmail REST API and Google's token endpoint, for ``test_gmail.py``.

Nothing here touches the network. The fake is a ``Transport``: it is handed each ``Request``
the connector would have sent and answers with a ``Response`` in the shapes the real API
uses (``users.threads.list``, ``users.threads.get`` with ``format=full``, the OAuth token
endpoint), so the connector under test is the one that will run against Google, with only
the wire swapped.

Everything in it is invented: the addresses use the RFC 2606 domains, the tokens are strings
that say they are fake, and the messages are written for these tests. No real account, token
or mail ever reaches this file (issue #58).

The fake is also a witness. It records every request, refuses anything that is not a ``GET``
on the Gmail API with a 405, and raises outright if asked for an attachment, so a test can
show not only what the connector did but what it never did.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from walle.gmail import API, TOKEN_URL, Request, Response

ACCESS = "FAKE-ACCESS-TOKEN-NOT-REAL-0001"
REFRESH = "FAKE-REFRESH-TOKEN-NOT-REAL-0002"
CLIENT_ID = "fake-client-id.apps.example.com"
CLIENT_SECRET = "FAKE-CLIENT-SECRET-NOT-REAL-0003"


def b64url(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(
        status=status,
        headers={"Content-Type": "application/json; charset=UTF-8", **(headers or {})},
        body=json.dumps(payload).encode(),
    )


def _error(status: int, message: str, reason: str, status_name: str) -> Response:
    return _json(
        status,
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason}],
                "status": status_name,
            }
        },
    )


def part(
    mime: str,
    *,
    text: str | bytes | None = None,
    filename: str = "",
    attachment_id: str | None = None,
    size: int | None = None,
    parts: list[dict[str, Any]] | None = None,
    headers: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """One MIME part in the ``users.messages`` payload shape."""
    body: dict[str, Any] = {}
    if text is not None:
        raw = text.encode() if isinstance(text, str) else text
        body = {"size": len(raw), "data": b64url(raw)}
    elif attachment_id is not None:
        body = {"size": size or 0, "attachmentId": attachment_id}
    else:
        body = {"size": 0}
    out: dict[str, Any] = {"mimeType": mime, "filename": filename, "body": body}
    if headers:
        out["headers"] = [{"name": n, "value": v} for n, v in headers]
    if parts is not None:
        out["parts"] = parts
    return out


def message(
    *,
    msg_id: str,
    thread_id: str,
    internal_ms: int,
    headers: list[tuple[str, str]],
    payload: dict[str, Any] | None = None,
    text: str | None = None,
    labels: tuple[str, ...] = ("INBOX",),
) -> dict[str, Any]:
    """A full message. Give either ``payload`` (a part tree) or ``text`` (one plain part)."""
    if payload is None:
        payload = part("text/plain", text=text or "", headers=[("Content-Type", "text/plain")])
    payload = dict(payload)
    payload["headers"] = [{"name": n, "value": v} for n, v in headers] + payload.get("headers", [])
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "snippet": "",
        "historyId": "1",
        "internalDate": str(internal_ms),
        "sizeEstimate": 1000,
        "payload": payload,
    }


@dataclass
class FakeGmail:
    """The fake API. Call it as a transport; read its attributes as a witness."""

    threads: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Thread id to its messages, in the order the API would return them."""
    access_token: str = ACCESS
    refresh_token: str = REFRESH
    requests: list[Request] = field(default_factory=list)
    """Every request received, in order."""
    rate_limits: list[tuple[int, str | None]] = field(default_factory=list)
    """Pending rate-limit replies ``(status, retry_after_header)``, consumed one per API call."""
    failures: list[int] = field(default_factory=list)
    """Pending plain failures by status, consumed one per API call, after the rate limits."""
    refresh_ok: bool = True
    """Whether a refresh grant is honoured. ``False`` answers ``invalid_grant``."""
    echo_token_in_errors: bool = False
    """An adversarial mode: error bodies quote the Authorization header back, as some proxies
    do, so a test can show the connector's own messages never carry it."""
    refreshed: int = 0

    # --- building the mailbox ---------------------------------------------------------

    def add(self, msg: dict[str, Any]) -> None:
        self.threads.setdefault(msg["threadId"], []).append(msg)

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

    # --- the transport ----------------------------------------------------------------

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if request.url == TOKEN_URL:
            return self._token(request)
        if request.url.startswith(API):
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
            self.access_token = f"FAKE-ACCESS-TOKEN-NOT-REAL-{1000 + self.refreshed:04d}"
            return _json(
                200,
                {
                    "access_token": self.access_token,
                    "expires_in": 3599,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
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
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                    "token_type": "Bearer",
                },
            )
        return _json(400, {"error": "unsupported_grant_type"})

    def _api(self, request: Request) -> Response:
        split = urlsplit(request.url)
        path = split.path.removeprefix(urlsplit(API).path)
        if "attachments" in path:
            raise AssertionError(f"the connector asked for an attachment: {path}")
        if request.method != "GET":
            return _error(405, "method not allowed", "methodNotAllowed", "METHOD_NOT_ALLOWED")
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.access_token}":
            quoted = f" ({auth})" if self.echo_token_in_errors else ""
            return _error(
                401,
                f"Request had invalid authentication credentials.{quoted}",
                "authError",
                "UNAUTHENTICATED",
            )
        if self.rate_limits:
            status, retry_after = self.rate_limits.pop(0)
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            if status == 429:
                return Response(
                    429,
                    {"Content-Type": "application/json", **headers},
                    _error(429, "Quota exceeded", "rateLimitExceeded", "RESOURCE_EXHAUSTED").body,
                )
            return Response(
                status,
                {"Content-Type": "application/json", **headers},
                _error(status, "User-rate limit exceeded", "userRateLimitExceeded", "X").body,
            )
        if self.failures:
            status = self.failures.pop(0)
            return _error(status, "Backend Error", "backendError", "INTERNAL")
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        if path == "/threads":
            return self._list(params)
        if path.startswith("/threads/"):
            return self._get(path.removeprefix("/threads/"), params)
        return _error(404, "Not Found", "notFound", "NOT_FOUND")

    def _matches(self, msg: dict[str, Any], q: str) -> bool:
        for term in q.split():
            if term.startswith("after:"):
                since = int(term.removeprefix("after:"))
                if int(msg["internalDate"]) // 1000 < since:
                    return False
            elif term.startswith("label:") and (
                term.removeprefix("label:").upper() not in msg["labelIds"]
            ):
                return False
        return True

    def _list(self, params: dict[str, str]) -> Response:
        q = params.get("q", "")
        ids = [tid for tid, msgs in self.threads.items() if any(self._matches(m, q) for m in msgs)]
        # Newest first, as Gmail lists them.
        ids.sort(key=lambda tid: -max(int(m["internalDate"]) for m in self.threads[tid]))
        size = int(params.get("maxResults", "100"))
        start = int(params["pageToken"].removeprefix("page-")) if "pageToken" in params else 0
        page = ids[start : start + size]
        payload: dict[str, Any] = {
            "threads": [{"id": tid, "snippet": "", "historyId": "1"} for tid in page],
            "resultSizeEstimate": len(ids),
        }
        if start + size < len(ids):
            payload["nextPageToken"] = f"page-{start + size}"
        return _json(200, payload)

    def _get(self, thread_id: str, params: dict[str, str]) -> Response:
        if params.get("format") != "full":
            return _error(400, "format must be full", "badRequest", "INVALID_ARGUMENT")
        if thread_id not in self.threads:
            return _error(404, "Not Found", "notFound", "NOT_FOUND")
        return _json(
            200, {"id": thread_id, "historyId": "1", "messages": list(self.threads[thread_id])}
        )
