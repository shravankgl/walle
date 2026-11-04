"""Appending to a stream — the only code that makes the log grow.

One file per ``(principal, device)``; ``seq`` is assigned here, because only the stream knows
what comes next (D57). Several processes append to the same stream — the hook, the CLI, the
observer, the proposer — so this is the first place where being wrong corrupts data rather
than failing a test.

Two writers, step by step (the walkthrough this file is required to carry)
--------------------------------------------------------------------------
A and B both append to ``d-mbp.jsonl``, which ends at seq 44.

1. Both open the file ``a+b``. Opening is not ordering: neither has looked at anything yet.
2. A takes the ``flock``. B blocks inside ``flock``, having read nothing.
3. A seeks to the end, reads the last complete line, sees 44, writes 45, flushes, ``fsync``s.
4. A closes. The kernel releases its lock.
5. B wakes, **and only now seeks and reads**. It sees A's 45 — same inode, and A's bytes
   reached the OS before the lock went. B writes 46.

The ordering in 3 and 5 is the whole thing. A writer that computed its ``seq`` before
blocking on the lock would wake holding a stale number and write a duplicate — and a
duplicate ``seq`` is not a lost append but a permanently ambiguous ref, since
``d-mbp:45`` would name two different events for the rest of the log's life.

If A dies between 3 and 4, the OS drops its lock and B finds a line with no newline: an
append that was never acknowledged, so nothing ever depended on it. B leaves those bytes
where they are, starts its own record on a fresh line, and takes its ``seq`` from the last
*complete* line. The reader skips the fragment (D80). Truncating would be tidier and is
exactly what "append, never mutate" forbids (hard constraint 2) — the bytes are evidence
that a crash happened here.

A locked session (``StreamWriter.locked``) is the same lock held across a read and any
number of appends, for a caller whose decision to append depends on what the stream already
holds. Say A and B both run "append each turn not already logged":

1. A takes the lock and reads the stream: the turn is not there.
2. B asks for the lock and blocks, having read nothing.
3. A appends the turn, ``fsync``s, and closes. The lock goes.
4. B wakes, reads the stream, and finds A's record: it appends nothing.

Read before the lock instead, and both A and B would see an empty stream in step 1 and both
would append the turn: two events, two refs, for one thing that happened.

What this does not survive: a stream on NFS, where ``flock`` does not reliably serialise
between hosts. Local-first is the premise (D10); a shared-filesystem log would need a
different mechanism, and would silently interleave with this one.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TypeVar

from walle.events import Event
from walle.paths import stream_id, stream_path
from walle.reader import StreamContents, parse_stream

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's stream writer needs POSIX file locking (fcntl); Windows is not supported "
        "in Phase 0. Run it under WSL."
    )

import fcntl

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=Event)
"""`append` returns the same type it was given, with `seq` set.

The earlier version re-parsed the record to satisfy a wider return type — validating *after*
the durable write, so a validation failure would have raised at a caller whose event was
already on disk. A status that contradicts the file is the worst kind of bug to have in a log."""

# How much of the file's tail to read looking for the last line. Doubled until a line
# boundary is found, so a single very long record (a snapshot body) still works — it just
# costs a second read.
_TAIL_WINDOW = 64 * 1024


class CorruptLogError(Exception):
    """The last complete line of a stream is not a readable event.

    Deliberately fatal rather than repaired. The next ``seq`` is derived from that line, and
    guessing it risks reusing a number — which would not lose an event but would make one ref
    name two of them, permanently and silently. A torn *tail* is an interrupted write and is
    healed (D80); a corrupt complete line is something else, and the operator should see it.
    """


def _fsync_dir(path: Path) -> None:
    """Durably record a directory entry, so a created file survives a crash.

    ``fsync`` on the file is not enough: the file's *name* lives in its directory, and
    without this a crash can leave a durable inode nothing points at.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tail(f: BinaryIO, size: int) -> tuple[bytes | None, bool]:
    """``(last complete line, tail was torn)``, reading backwards from the end.

    "Complete" means newline-terminated. A window that starts mid-file may open in the middle
    of a line, so its first element is only trusted when a second line follows it or the
    window reaches byte zero.
    """
    if size == 0:
        return None, False
    window = _TAIL_WINDOW
    while True:
        start = max(0, size - window)
        f.seek(start)
        chunk = f.read(size - start)
        torn = not chunk.endswith(b"\n")
        cut = chunk.rfind(b"\n")
        complete = chunk[: cut + 1] if cut != -1 else b""
        lines = [line for line in complete.split(b"\n") if line.strip()]
        if lines and (len(lines) >= 2 or start == 0):
            return lines[-1], torn
        if start == 0:
            return (lines[-1] if lines else None), torn
        window *= 2


def _seq_of(line: bytes, path: Path) -> int:
    """The ``seq`` of an encoded event, or ``CorruptLogError`` naming what was wrong."""
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorruptLogError(
            f"{path}: the last complete line is not JSON ({exc}). Refusing to append: the "
            "next seq comes from it, and guessing would risk reusing one."
        ) from exc
    seq = record.get("seq") if isinstance(record, dict) else None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise CorruptLogError(
            f"{path}: the last complete line has no usable seq (found {seq!r}). Refusing to "
            "append rather than guess at the next one."
        )
    return seq


def encode(event: Event) -> bytes:
    """One event as the line that goes in the file.

    Not ``canonical_json``, deliberately: that form is for hashing — ASCII-escaped, ``seq``
    stripped — and this one is for a person with ``cat``. Keys are sorted so a diff between
    two versions of a log is readable, and ``ensure_ascii`` is off so text stays greppable in
    the language it was written in (D10).
    """
    body = json.dumps(
        event.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return body.encode("utf-8") + b"\n"


class StreamWriter:
    """Appends to one ``(principal, device)`` stream.

    Holds no state between appends on purpose. A cached ``seq`` would be wrong the moment
    another process appended, and the lock is only held inside ``append`` — so the only
    correct source for the next number is the file itself, read under the lock.
    """

    def __init__(self, root: Path, principal: str, device: str) -> None:
        self.root = Path(root)
        self.principal = principal
        self.device = device
        self.stream = stream_id(device)  # validates, and so does stream_path below
        self.path = stream_path(self.root, principal, device)

    def append(self, event: E) -> E:
        """Append one event, returning it with its ``seq`` filled in.

        Durable when it returns: the bytes are ``fsync``ed, and on the first append the
        directory entry is too.
        """
        with self.locked() as stream:
            return stream.append(event)

    @contextmanager
    def locked(self) -> Iterator[LockedStream]:
        """Hold the stream's lock for a read and any number of appends (see the walkthrough).

        Opening the file creates it, empty, if it is not there yet. Keep the session short:
        every other writer to this stream, and every reader, waits for it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+b") as f:
            # Lock first. Everything below reads the file, and reading before the lock would
            # mean acting on what some other writer has already superseded.
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield LockedStream(f, self.path, self.stream)
            # The lock goes with close, after the data is on disk — never before.


class LockedStream:
    """One stream, held under ``LOCK_EX`` by ``StreamWriter.locked`` until the block ends.

    Not usable after the block: the file is closed and the lock is gone.
    """

    def __init__(self, f: BinaryIO, path: Path, stream: str) -> None:
        self._f = f
        self.path = path
        self.stream = stream

    def read(self) -> StreamContents:
        """Everything in the stream now, parsed by the reader's rules (D80).

        Read through the locked handle. Opening the file again for ``reader.read_file``
        would ask for ``LOCK_SH`` on a second open file, which waits for this lock forever.
        """
        size = self._f.seek(0, os.SEEK_END)
        self._f.seek(0)
        return parse_stream(self._f.read(size), self.path, self.stream)

    def append(self, event: E) -> E:
        """Append one event, returning it with its ``seq`` filled in. Durable on return."""
        f = self._f
        size = f.seek(0, os.SEEK_END)

        # Whether this is the stream's first record is decided *here*, inside the lock,
        # from the file's own size — never from an `exists()` check before it.
        #
        # `open(..., "a+b")` creates the file before the lock is taken, so a check made
        # outside races: writer A opens (the file now exists), writer B's `exists()` says
        # True, B wins the lock, writes seq 1, skips this fsync and returns claiming
        # durability — on a directory entry nobody has made durable. An empty file means
        # nothing has been written yet, which is exactly the condition that needs it.
        #
        # And it runs *before* the write: a crash in between would otherwise leave
        # durably written bytes in a non-durable entry, and a failure here after a
        # successful write would tell the caller the append failed while the record sat
        # on disk. Ordered first, a failure truthfully means nothing was written.
        if size == 0:
            _fsync_dir(self.path.parent)

        last_line, torn = _tail(f, size)
        next_seq = _seq_of(last_line, self.path) + 1 if last_line else 1

        record = event.model_copy(update={"seq": next_seq})
        # Encoded before anything is written, so a failure here leaves the file
        # untouched rather than leaving a stray newline behind it (coding rule 7).
        payload = encode(record)
        if torn:
            logger.warning(
                "%s: last line was not newline-terminated — an append was interrupted. "
                "Leaving the fragment in place and starting seq %d on a fresh line; "
                "readers skip it.",
                self.path,
                next_seq,
            )
            payload = b"\n" + payload
        f.seek(0, os.SEEK_END)  # "a" mode appends anyway; this keeps the position honest
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
        return record
