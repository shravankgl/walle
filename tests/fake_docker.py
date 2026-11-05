"""A fake ``docker`` CLI for ``tests/test_executor.py``.

Run as a subprocess in place of the real client, it records every invocation — the docker
argv it was handed, as one JSON line — in the file named by ``--record``, so a test can see
exactly what the executor asked for. It starts no container and talks to no daemon. ``--mode``
picks one of the ways the real client can behave, so each path in the executor can be
exercised on its own:

``normal``       ``run`` prints ``ran`` and exits 0; ``info`` prints a version; ``ps`` lists
                 nothing; ``rm`` exits 0.
``fail-run``     ``run`` prints ``boom`` on stderr and exits 7.
``hang``         ``run`` never returns (sleeps until killed); ``rm`` exits 0; ``ps`` lists
                 nothing afterwards.
``hang-racy``    ``run`` never returns; ``rm`` loses the race with ``--rm`` ("removal ... is
                 already in progress", exit 1) but ``ps`` lists nothing: the container is gone.
``hang-stuck``   ``run`` never returns, ``rm`` fails, and ``ps`` keeps listing the container.
``no-daemon``    every command fails with the client's "Cannot connect" line: ``run`` exits
                 125, as the real client does for its own failure, the rest exit 1.
``stale``        ``ps`` lists two container ids, as after a crashed parent, until an ``rm``
                 has been seen; ``rm`` exits 0.
``stale-racy``   as ``stale``, but ``rm`` exits 1 with the "already in progress" line.
``stale-stuck``  ``ps`` lists the two ids before and after ``rm``, and ``rm`` fails.
``ps-hang``      ``ps`` never returns (sleeps until killed), as a wedged daemon would leave it.

The record is also the fake's memory: ``ps`` reads it to know whether an ``rm`` came before.

Synthetic throughout: there is no image, no container and no host behind this.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

NO_DAEMON = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)
RACE = "Error response from daemon: removal of container walle-x is already in progress"
STUCK = "Error response from daemon: cannot remove"
STALE_IDS = ("0123456789ab", "fedcba987654")


def main(argv: list[str]) -> int:
    record = ""
    mode = "normal"
    rest = list(argv)
    while rest and rest[0] in ("--record", "--mode"):
        flag, value = rest[0], rest[1]
        rest = rest[2:]
        if flag == "--record":
            record = value
        else:
            mode = value
    prior: list[list[str]] = []
    if record:
        path = Path(record)
        if path.exists():
            prior = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rest) + "\n")
    removed_before = any(call and call[0] == "rm" for call in prior)
    command = rest[0] if rest else ""

    if mode == "no-daemon":
        sys.stderr.write(NO_DAEMON + "\n")
        return 125 if command == "run" else 1
    if command == "info":
        sys.stdout.write("27.0.0\n")
        return 0
    if command == "run":
        if mode == "fail-run":
            sys.stderr.write("boom\n")
            return 7
        if mode.startswith("hang"):
            while True:
                time.sleep(3600)
        sys.stdout.write("ran\n")
        return 0
    if command == "ps":
        if mode == "ps-hang":
            while True:
                time.sleep(3600)
        if mode == "stale-stuck" or (mode in ("stale", "stale-racy") and not removed_before):
            sys.stdout.write("\n".join(STALE_IDS) + "\n")
        elif mode == "hang-stuck":
            sys.stdout.write("deadbeef0000\n")
        return 0
    if command == "rm":
        if mode in ("hang-stuck", "stale-stuck"):
            sys.stderr.write(STUCK + "\n")
            return 1
        if mode in ("hang-racy", "stale-racy"):
            sys.stderr.write(RACE + "\n")
            return 1
        return 0
    sys.stderr.write(f"fake docker: unknown command {command!r}\n")
    return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
