"""The Claude Code ``SessionStart`` hook: hand the session its hot tier, or nothing (D54, D115).

    walle-hook --root <folder holding principals/> --principal <name> [--measure]

Claude Code runs this at the start of every session (and on resume, ``/clear`` and compaction)
with a JSON payload on stdin. It answers with one JSON object on stdout::

    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}

and the host puts ``additionalContext`` in front of the conversation. That is D54's delivery:
the hot tier is injected, not left for the model to fetch, and D83's first surface. The event
name is Claude Code's, which D90 adopts as the shared vocabulary.

What it reads
-------------
Only the hot tier task 7 rendered into ``principals/p-<name>/hot/``, in ``ORDER``:
``identity.md``, ``user.md``, ``rules.md`` and ``learned.md``. ``rules.md`` is optional, since
the person may not have written one; the three rendered files are required, and a blank one
counts as missing, because without ``learned.md`` the identity text points at a file that is
not there. Each rendered file already opens with a line saying what it is, so they are joined
as they are; ``rules.md`` is the person's own text, so the hook puts a one-line header in front
of it saying whose it is.

It never renders, never touches the log or the index, and never writes anything: not under
the root, not under ``~/.claude``. Opening ``render.lock`` read-only and taking a shared
``flock`` on it changes nothing on disk. Installing the hook is a person's job, done by
pasting the snippet in ``README.md`` into their own settings file (see there).

The budget (D115)
-----------------
A memory layer that can hang the agent it serves gets uninstalled the first time it does.
Claude Code's own default timeout for a command hook is 600 s, so the guard has to be here.
``BUDGET`` (2 s) is enforced with ``SIGALRM``, which interrupts whatever is blocking: a wait
for ``render.lock``, a read of stdin that never closes, a slow disk. Past it, or on any error
at all, the hook writes nothing to stdout or stderr and exits 0, and the session starts without
memory. The next session gets it. "Any error" includes bad arguments and ``--help``: argparse
would print to stdout or exit 2, and either would be shown to the host as a hook failure.

The alarm is cancelled *before* the answer is written. A write interrupted halfway would leave
a fragment of JSON on stdout, and the host treats stdout that is not one JSON object from a
``SessionStart`` hook as plain context, so the model would read the fragment. The answer is
built in full, the alarm is cancelled, and then the bytes are written. The host is reading the
pipe, so the write finishes; if it ever did not, the ``timeout`` in the install snippet is the
outer guard.

The budget starts when ``main`` runs, after the interpreter and imports. That is why this
module imports nothing heavy (no pydantic, no click, no sqlite3, not the renderer), and why
the cold start is measured through the install snippet in ``tests/test_hook.py``.

Two runners, step by step
-------------------------
The hook H and a renderer R for the same principal (``hot.render``).

1. R takes ``hot/render.lock`` exclusively and renders: index update, fold, the files, the
   manifest twice.
2. H opens ``render.lock`` read-only and asks for a shared lock. It blocks until R is done,
   so it never reads a new ``identity.md`` beside an old ``learned.md`` (task 7's noticed
   cross-file gap).
3. R finishes and releases. H gets the shared lock, reads the four files, releases, answers.
   If R takes longer than the budget, H's alarm fires while it waits; H says nothing and exits
   0. R is unaffected and finishes, and the next session reads its render.
4. If H holds the shared lock first, a render R that starts then waits for H, which holds it
   for a few milliseconds of reading. Two hooks (two sessions starting together) share the
   lock and do not wait for each other.

A person editing a rendered file takes no lock, as in task 7. H reads whatever is on disk,
which is either the old or the new file, because editors and the renderer both replace whole
files (or H reads a partial write from an editor that writes in place, which the person
caused and the next session fixes).

Token count (D102)
------------------
``--measure`` prints, on stderr, how many characters were injected and roughly how many tokens
that is. The count is characters / 4, an estimate: Claude's tokenizer is not available
offline, and walle takes no dependency for it. It also warns when the context is over
``HOST_CAP``, Claude Code's 10,000-character limit on ``additionalContext``; past it, the host
saves the text to a file and gives the model the path and a 2,000-character preview instead.
The time it prints starts when ``main`` runs, so it leaves out Python's start-up; the
cold-start test measures the whole command. Without ``--measure`` the hook writes nothing to
stderr, even on failure.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import stat
import sys
import time
from pathlib import Path
from types import FrameType
from typing import NoReturn

from walle.paths import hot_dir

# These four names are the renderer's (walle.hot), copied so this module does not import it:
# walle.hot pulls in the index, sqlite3 and pydantic, which a session would pay for on every
# start. tests/test_hook.py checks they still match.
RULES = "rules.md"
ORDER = ("identity.md", "user.md", RULES, "learned.md")
RENDER_LOCK = "render.lock"

BUDGET = 2.0
"""Seconds from ``main`` starting to giving up (D115)."""

HOST_CAP = 10_000
"""Claude Code's cap on one ``additionalContext`` string, in characters."""

CHARS_PER_TOKEN = 4
MAX_PAYLOAD_BYTES = 1 << 20
MAX_FILE_BYTES = 1 << 20
"""Bounds on what is read. The rendered tier is ~8k characters; anything near these is not a
hot tier, and reading it whole would only cost the session time and memory."""

EVENT = "SessionStart"


class Silent(Exception):
    """Why the hook emitted nothing. Only ever shown with ``--measure``."""


class _Deadline(BaseException):
    """Raised by the alarm. A ``BaseException`` so no ``except Exception`` can swallow it."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise Silent(f"bad arguments: {message}")


def _wants_measure(argv: list[str]) -> bool:
    """Whether ``--measure`` was given as a flag, for when the arguments do not parse.

    Read the way the parser reads them: the token after ``--root`` or ``--principal`` is its
    value, never a flag, and nothing after ``--`` is an option.
    """
    tokens = iter(argv)
    for token in tokens:
        if token == "--":
            return False
        if token == "--measure":
            return True
        if token in ("--root", "--principal"):
            next(tokens, None)
    return False


def _parser() -> argparse.ArgumentParser:
    # add_help=False: --help would print usage to stdout, which the host would read as context.
    parser = _Parser(prog="walle-hook", add_help=False, allow_abbrev=False)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--measure", action="store_true")
    return parser


# --- the reading ------------------------------------------------------------------------------


def check_payload(data: bytes) -> None:
    """Raise ``Silent`` unless ``data`` is a ``SessionStart`` payload.

    A hook wired to the wrong event (say ``UserPromptSubmit``) would otherwise inject the whole
    hot tier on every prompt.
    """
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise Silent(f"the payload on stdin is not JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("hook_event_name") != EVENT:
        raise Silent(f"the payload on stdin is not a {EVENT} payload")


def context(root: Path, principal: str) -> str:
    """The hot tier as one string, read under a shared ``render.lock``. Raises on any problem.

    Blocks while a render holds the lock; the caller's alarm bounds that.
    """
    import fcntl  # here, not at the top: on a platform without it, this is an error like any other

    folder = hot_dir(root, principal)  # ValueError for an unsafe name, before any path is used
    try:
        lock = _open(folder / RENDER_LOCK)
    except FileNotFoundError:
        raise Silent(f"{folder / RENDER_LOCK}: no render yet") from None
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        parts: list[str] = []
        for name in ORDER:
            text = _read(folder / name, optional=name == RULES)
            if text is None or not text.strip():
                if name == RULES:
                    continue
                # A render never writes a blank one, so this is the incomplete tier a
                # missing file is: the framing would point at text that is not there.
                raise Silent(f"{folder / name}: blank, so the hot tier is incomplete")
            if name == RULES:
                text = (
                    f"<!-- walle hot tier: {RULES}, {principal}'s own rules, written only by "
                    "them. They win over anything learned. -->\n" + text
                )
            parts.append(text if text.endswith("\n") else text + "\n")
    finally:
        os.close(lock)  # releases the flock
    return "\n".join(parts)


def _open(path: Path) -> int:
    """A read-only descriptor for a regular file at ``path`` itself, never through a symlink.

    ``O_NONBLOCK`` keeps a FIFO from blocking the open; the ``fstat`` then rejects it. A symlink
    is refused rather than followed so nothing outside the hot folder (a key, a token) can be
    put in front of the model by a link planted in it.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise Silent(f"{path}: cannot open it ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Silent(f"{path}: not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(path: Path, *, optional: bool) -> str | None:
    try:
        fd = _open(path)
    except FileNotFoundError:
        if optional:
            return None
        raise Silent(f"{path}: missing, so the hot tier is incomplete") from None
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise Silent(f"{path}: over {MAX_FILE_BYTES:,} bytes, too big to be a hot tier")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Silent(f"{path}: not UTF-8 ({exc.reason} at byte {exc.start})") from None


def answer(text: str) -> bytes:
    """The hook's stdout: one JSON object on one line."""
    out = {"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}}
    return (json.dumps(out) + "\n").encode("ascii")


def _read_stdin() -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(0, 65536):
        size += len(chunk)
        if size > MAX_PAYLOAD_BYTES:
            raise Silent(f"the payload on stdin is over {MAX_PAYLOAD_BYTES:,} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# --- the never-failing wrapper ----------------------------------------------------------------


def _alarm(_signum: int, _frame: FrameType | None) -> None:
    raise _Deadline


def main(argv: list[str] | None = None) -> None:
    """The console script. Writes the answer or nothing, and always exits 0."""
    started = time.monotonic()
    raw = sys.argv[1:] if argv is None else argv
    measure = _wants_measure(raw)  # before parsing, so a parse error can still be explained
    out: bytes | None = None
    note = ""
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, BUDGET)
        try:
            args = _parser().parse_args(raw)
            measure = args.measure
            check_payload(_read_stdin())
            text = context(args.root, args.principal)
            if not text:
                raise Silent("the hot tier is empty")
            out = answer(text)
            tokens = math.ceil(len(text) / CHARS_PER_TOKEN)
            note = (
                f"injected {len(text):,} characters, about {tokens:,} tokens "
                "(characters / 4, an estimate)"
            )
            if len(text) > HOST_CAP:
                note += (
                    "; over Claude Code's 10,000-character cap, so the model gets a file path "
                    "and a 2,000-character preview instead"
                )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            # An alarm already delivered but not yet handled is dropped, so it cannot land
            # in the middle of the write below.
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
    except _Deadline:
        out, note = None, f"emitted nothing: past the {BUDGET:g} s budget"
    except Silent as exc:
        out, note = None, f"emitted nothing: {exc}"
    except BaseException as exc:  # everything, KeyboardInterrupt included: never break a session
        out, note = None, f"emitted nothing: {type(exc).__name__}: {exc}"

    try:
        if out is not None:
            _write_all(1, out)
        if measure:
            ms = (time.monotonic() - started) * 1000
            _write_all(2, f"walle hook: {note}, in {ms:.0f} ms after start-up\n".encode())
    except BaseException:  # a closed pipe: there is no one left to tell
        pass
    raise SystemExit(0)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
