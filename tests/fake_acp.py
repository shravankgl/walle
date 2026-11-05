"""A fake ACP host (an *Agent*, in the protocol's words) for ``tests/test_acp.py``.

Run as a subprocess, it speaks the Agent Client Protocol's stdio transport (protocol version 1,
schema v1.29.1): one JSON-RPC 2.0 message per line on stdin and stdout, nothing else on stdout.
It records every message it receives, one JSON line each, in the file named by ``--record``,
so a test can see exactly what the client sent and in what order.

It is not Codex or Gemini CLI, and it has no model. On a prompt it echoes the text blocks it
was given back as one ``agent_message_chunk`` and ends the turn. ``--mode`` picks one of the
ways a real host can misbehave, so the client's failure paths can be exercised one at a time:

``normal``            answers everything; advertises ``embeddedContext``.
``text-only``         the same, but advertises no prompt capabilities, so a ``resource``
                      block would be out of contract and only ``text`` may carry the tier.
``old-version``       answers ``initialize`` with protocol version 0.
``init-error``        answers ``initialize`` with a JSON-RPC error.
``error-first``       answers the first ``session/prompt`` with ``-32000`` (auth required),
                      then behaves normally.
``die-on-prompt``     exits with status 3 on the first ``session/prompt``, answering nothing.
``hang-on-prompt``    never answers ``session/prompt`` (keeps reading stdin until EOF).
``hang-deaf``         never answers ``session/prompt``, stops reading stdin and ignores
                      ``SIGTERM``, so only ``SIGKILL`` ends it.
``garbage``           writes a line that is not JSON to stdout on the first prompt, then
                      answers it normally.
``ask-permission``    on a prompt, sends ``session/request_permission`` first and echoes the
                      client's answer in its message.
``ask-fs``            on a prompt, sends ``fs/read_text_file`` first and echoes the client's
                      answer (expected: an error) in its message.
``stderr-noise``      writes a few hundred kilobytes to stderr during a prompt, then answers.

Synthetic throughout: no account, no token, no transcript.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from typing import Any

PROTOCOL_VERSION = 1


class Fake:
    def __init__(self, mode: str, record: str) -> None:
        self.mode = mode
        self.record = open(record, "a", encoding="utf-8")  # noqa: SIM115 - lives as long as the process
        self.next_id = 1000
        self.prompts = 0
        self.sessions = 0

    # --- wire -----------------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def reply(self, id_: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": id_, "result": result})

    def fail(self, id_: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def ask(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request to the client and read until its answer arrives."""
        self.next_id += 1
        id_ = self.next_id
        self.send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        while True:
            line = sys.stdin.readline()
            if not line:
                sys.exit(0)
            message = self.received(line)
            if message.get("id") == id_ and ("result" in message or "error" in message):
                return message
            self.handle(message)

    def received(self, line: str) -> dict[str, Any]:
        self.record.write(line if line.endswith("\n") else line + "\n")
        self.record.flush()
        return json.loads(line)

    # --- methods --------------------------------------------------------------------------

    def run(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            self.handle(self.received(line))

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            return  # a stray response; nothing to do
        id_ = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            self.initialize(id_)
        elif method == "session/new":
            self.sessions += 1
            self.reply(id_, {"sessionId": f"fake-session-{self.sessions}"})
        elif method == "session/prompt":
            self.prompt(id_, params)
        elif method == "session/cancel":
            pass
        elif id_ is not None:
            self.fail(id_, -32601, f"Method not found: {method}")

    def initialize(self, id_: Any) -> None:
        if self.mode == "old-version":
            self.reply(id_, {"protocolVersion": 0, "agentCapabilities": {}})
            return
        if self.mode == "init-error":
            self.fail(id_, -32603, "the fake host refuses to initialize")
            return
        embedded = self.mode != "text-only"
        self.reply(
            id_,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {
                        "image": False,
                        "audio": False,
                        "embeddedContext": embedded,
                    },
                },
                "agentInfo": {"name": "fake-acp", "version": "0.0"},
                "authMethods": [],
            },
        )

    def prompt(self, id_: Any, params: dict[str, Any]) -> None:
        self.prompts += 1
        first = self.prompts == 1
        session = params.get("sessionId")
        if self.mode == "die-on-prompt" and first:
            sys.exit(3)
        if self.mode == "hang-on-prompt":
            return
        if self.mode == "hang-deaf":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                time.sleep(3600)
        if self.mode == "error-first" and first:
            self.fail(id_, -32000, "Authentication required")
            return
        if self.mode == "garbage" and first:
            sys.stdout.write("fake-acp: BANNER-3f9c, not JSON\n")
            sys.stdout.flush()
        if self.mode == "stderr-noise":
            for _ in range(4000):
                sys.stderr.write("fake-acp: noise " * 6 + "\n")
            sys.stderr.flush()
        notes: list[str] = []
        if self.mode == "ask-permission":
            answer = self.ask(
                "session/request_permission",
                {
                    "sessionId": session,
                    "toolCall": {"toolCallId": "call-1", "title": "Run a command"},
                    "options": [
                        {"optionId": "yes-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "no-once", "name": "Reject once", "kind": "reject_once"},
                    ],
                },
            )
            notes.append("permission=" + json.dumps(answer.get("result"), sort_keys=True))
        if self.mode == "ask-fs":
            answer = self.ask(
                "fs/read_text_file", {"sessionId": session, "path": "/nonexistent/file"}
            )
            notes.append("fs=" + json.dumps(answer.get("error"), sort_keys=True))
        texts = [
            block.get("text", "")
            for block in params.get("prompt", [])
            if block.get("type") == "text"
        ]
        echo = "echo: " + " | ".join(texts)
        if notes:
            echo += " [" + "; ".join(notes) + "]"
        self.notify(
            "session/update",
            {
                "sessionId": session,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": echo},
                },
            },
        )
        time.sleep(0.01)
        self.reply(id_, {"stopReason": "end_turn"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True)
    parser.add_argument("--mode", default="normal")
    args = parser.parse_args()
    Fake(args.mode, args.record).run()


if __name__ == "__main__":
    main()
