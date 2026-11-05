"""A local fake of the self-hosted Opik REST API, for ``test_opik_export.py`` (issue #80).

A real HTTP server on ``127.0.0.1`` and a free port, so the exporter under test sends
through the same ``urllib`` wire it would use against a real Opik, with only the far end
swapped. It does not need the ``opik`` package: CI never installs the extra.

It speaks the three calls the exporter makes, in the shapes Opik documents for its local
server (``http://localhost:5173/api``):

- ``GET  /api/v1/private/traces/{id}``  200 with the stored trace, or 404
- ``POST /api/v1/private/spans/batch``  ``{"spans": [...]}``, 204
- ``POST /api/v1/private/traces/batch`` ``{"traces": [...]}``, 204

It is also a witness: every request is recorded with its exact body bytes as they arrived
on the socket, so a test can scan what was actually received rather than what the exporter
meant to send. ``fail`` makes one path answer with a status instead; ``redirect`` makes
every request answer 307 to another URL.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

PREFIX = "/api/v1/private"


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    body: bytes


@dataclass
class FakeOpik:
    """Start with ``with FakeOpik() as opik:``; ``opik.url`` is the base to export to."""

    fail: dict[str, int] = field(default_factory=dict)
    """``{path: status}``: answer that path with that status and store nothing."""

    redirect: str | None = None
    received: list[Received] = field(default_factory=list)
    traces: dict[str, dict[str, Any]] = field(default_factory=dict)
    spans: dict[str, dict[str, Any]] = field(default_factory=dict)
    trace_posts: int = 0
    span_posts: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "start the fake first"
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"

    @property
    def posts(self) -> list[Received]:
        return [r for r in self.received if r.method == "POST"]

    def __enter__(self) -> FakeOpik:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        path = handler.path
        with self._lock:
            self.received.append(Received(method, path, body))
            status, reply = self._answer(method, path, body)
        handler.send_response(status)
        if status in (301, 302, 303, 307, 308):
            handler.send_header("Location", f"{self.redirect}{path}")
        payload = json.dumps(reply).encode() if reply is not None else b""
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _answer(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        if self.redirect is not None:
            return 307, None
        if path in self.fail:
            return self.fail[path], {"code": self.fail[path], "message": "fake failure"}
        if method == "GET" and path.startswith(f"{PREFIX}/traces/"):
            trace = self.traces.get(path.rsplit("/", 1)[1])
            return (200, trace) if trace is not None else (404, {"code": 404})
        if method == "POST" and path == f"{PREFIX}/spans/batch":
            self.span_posts += 1
            for span in json.loads(body)["spans"]:
                self.spans[span["id"]] = span
            return 204, None
        if method == "POST" and path == f"{PREFIX}/traces/batch":
            self.trace_posts += 1
            for trace in json.loads(body)["traces"]:
                self.traces[trace["id"]] = trace
            return 204, None
        return 404, {"code": 404, "message": f"no route {method} {path}"}


def closed_port_url() -> str:
    """A loopback URL nothing is listening on: a missing Opik server."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/api"
