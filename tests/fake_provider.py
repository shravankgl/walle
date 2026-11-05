"""A local fake of an OpenAI-shaped chat endpoint, for ``test_models.py`` (issue #52).

Ollama (under ``/v1``), OpenRouter and OpenAI all speak the same wire: ``POST
<base>/chat/completions`` with a JSON body naming the model, and a JSON reply holding
``choices[0].message.content``. aisuite's providers for all three go through the ``openai``
SDK, so one fake on ``127.0.0.1`` stands in for each of them: the adapter under test is the
one that runs against the real vendor, with only the host swapped.

The fake is a witness as well as a stand-in. It records every request it was sent, path,
headers and body, so a test can show what reached the provider (the model name, unchanged)
and how many times (once, never a retry).

Nothing here touches the network beyond the loopback interface, and every key is a string
that says it is fake.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FAKE_KEY = "sk-or-v1-FAKE-KEY-NOT-REAL-0000000000000000"
"""A key-shaped string for providers that refuse to start without one. Never a real key."""


@dataclass(frozen=True)
class Seen:
    """One request the fake was sent."""

    path: str
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def prompt(self) -> str:
        return str(self.body["messages"][-1]["content"])


@dataclass(frozen=True)
class Reply:
    """What the fake answers: a status, a JSON payload, extra headers, and a delay first."""

    status: int = 200
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


def completion(content: str | None, model: str = "fake") -> dict[str, Any]:
    """An OpenAI chat-completion body carrying ``content``."""
    return {
        "id": "chatcmpl-fake-0001",
        "object": "chat.completion",
        "created": 1_790_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


_EXCERPT = re.compile(r"^excerpt (\S+)$", re.MULTILINE)
_ITEM = re.compile(r"^item (\S+)$", re.MULTILINE)


def proposer_reply(seen: Seen) -> Reply:
    """A well-behaved model: one proposal citing the first excerpt it was shown, and a keep
    for every item it is asked to judge. Grounded in the request, as the proposer demands."""
    prompt = seen.prompt
    if prompt.startswith("You are the proposer for walle, judging"):
        out: list[dict[str, str]] = [
            {"ref": ref, "verdict": "keep", "reason": "reads as lasting"}
            for ref in _ITEM.findall(prompt)
        ]
    else:
        first = _EXCERPT.search(prompt)
        out = []
        if first:
            out.append(
                {
                    "excerpt": first.group(1),
                    "summary": "Waters the allotment before work on dry days",
                    "body": "Waters the allotment before work on dry days, so plan around it.",
                    "level": "explicit",
                }
            )
    return Reply(payload=completion(json.dumps(out), model=str(seen.body.get("model"))))


class FakeProvider:
    """A threaded HTTP server on an ephemeral loopback port, answering with ``respond``."""

    def __init__(self, respond: Callable[[Seen], Reply] = proposer_reply) -> None:
        self.respond = respond
        self.seen: list[Seen] = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"unparsed": raw.decode("utf-8", "replace")}
                seen = Seen(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                with fake._lock:
                    fake.seen.append(seen)
                reply = fake.respond(seen)
                if reply.delay:
                    time.sleep(reply.delay)
                data = json.dumps(reply.payload).encode()
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for name, value in reply.headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up first, which is what a timeout test wants

            def log_message(self, format: str, *args: Any) -> None:
                pass  # quiet: nothing on stderr from the fake

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def base_url(self) -> str:
        """Where an OpenAI-shaped client points: ``<url>/v1``."""
        return f"{self.url}/v1"

    def start(self) -> FakeProvider:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def serving(respond: Callable[[Seen], Reply] = proposer_reply) -> Iterator[FakeProvider]:
    fake = FakeProvider(respond).start()
    try:
        yield fake
    finally:
        fake.stop()


def closed_port() -> str:
    """A loopback URL nothing listens on: bound, then released."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    host, port = server.server_address[:2]
    server.server_close()
    return f"http://{host!s}:{port}/v1"
