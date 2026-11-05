"""A fake ``docker`` CLI for ``tests/test_executor.py``.

Run as a subprocess in place of the real client, it records every invocation — the docker
argv it was handed, as one JSON line — in the file named by ``--record``, so a test can see
exactly what the executor asked for. It starts no container and talks to no daemon. ``--mode``
picks one of the ways the real client can behave, so each path in the executor can be
exercised on its own:

``normal``       ``run`` prints ``ran`` and exits 0; ``info`` prints a version; ``ps`` lists
                 nothing; ``rm`` exits 0.
``fail-run``     ``run`` prints ``boom`` on stderr and exits 7.
``hang``         ``run`` never returns (sleeps until killed); ``rm`` exits 0.
``hang-stuck``   ``run`` never returns and ``rm`` fails too, so nothing cleans up.
``no-daemon``    every command fails with the client's "Cannot connect" line: ``run`` exits
                 125, as the real client does for its own failure, the rest exit 1.
``stale``        ``ps`` lists two container ids, as after a crashed parent; ``rm`` exits 0.
``stale-stuck``  ``ps`` lists two ids and ``rm`` fails.

Synthetic throughout: there is no image, no container and no host behind this.
"""

from __future__ import annotations

import json
import sys
import time

NO_DAEMON = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)


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
    if record:
        with open(record, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rest) + "\n")
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
        if mode in ("hang", "hang-stuck"):
            while True:
                time.sleep(3600)
        sys.stdout.write("ran\n")
        return 0
    if command == "ps":
        if mode in ("stale", "stale-stuck"):
            sys.stdout.write("0123456789ab\nfedcba987654\n")
        return 0
    if command == "rm":
        if mode in ("hang-stuck", "stale-stuck"):
            sys.stderr.write("Error response from daemon: cannot remove\n")
            return 1
        return 0
    sys.stderr.write(f"fake docker: unknown command {command!r}\n")
    return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
