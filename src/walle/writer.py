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
line that *is a record* by the reader's rules. The reader skips the fragment (D80).
Truncating would be tidier and is exactly what "append, never mutate" forbids (hard
constraint 2) — the bytes are evidence that a crash happened here.

How a tear is healed, byte by byte (issue #128)
-----------------------------------------------
The writer and the reader are one protocol: what the reader loads, the writer must be able to
continue, and what the writer never acknowledged, the reader must never load. Two rules keep
that true under any crash, full disk or torn write, and both are exhaustively tested
(``tests/test_audit_log.py``) by cutting an append, then a heal, then a heal of that heal,
at every byte offset:

* **A heal begins with ``TORN_MARK``, never a bare newline.** B's first byte lands on the end
  of A's torn line. A bare ``\n`` there would *terminate* A's line — and if A had written its
  whole record but the newline, a heal cut after that one byte would turn an unacknowledged
  record into a terminated one. The reader would load it; the next writer would build on its
  ``seq``; an event nobody acknowledged would be back as a fact. ``TORN_MARK`` is a byte no
  JSON line can end in, so A's line stays unparseable however B's heal is then cut: after
  the mark alone (still a torn tail), after the newline (a complete line that is not JSON,
  which the reader skips), or anywhere inside B's record (another torn tail, healed the
  same way).
* **The next ``seq`` comes from the last line that is a record, stepping back past what the
  reader skips.** A heal cut after its newline leaves a complete non-JSON line *last*. The
  reader skips it (D80); a writer that refused to number from it would leave a stream that
  loads and cannot grow — forever, since nothing truncates. So the writer steps back past
  blank lines and non-JSON lines exactly as the reader does, and refuses only what the
  reader refuses: a complete line that is JSON but not a record with a usable ``seq``.

Streams healed before this change begin a heal with ``\n``; the reader keeps the rule that
loads them (a record with no newline that shares its ``seq`` with the very next line is the
earlier, unacknowledged one), and a writer continuing such a stream takes its ``seq`` from the
later line, as the reader does (D88).

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

A cause must already be in the log (D111, issue #121)
------------------------------------------------------
``caused_by`` is provenance: each ref in it must name an event some stream of this principal
holds. The index refuses a log with a dangling one (``walle.index.DanglingRefError``), and the
log is append-only, so a single event appended with a ref that does not exist — a typo, a
ref from another principal, the event's own ref — would break ``index.update`` and the
rebuild for the rest of the log's life. Every writer used to be trusted to get this right.
Now the check is here, in ``LockedStream.append``, the one place a stream grows: before a
byte is written, every cause is looked for — on this stream through the locked handle, on
any other stream of the same principal by reading its file — and ``DanglingCauseError`` is
raised with the refs that are missing. A cause on this stream with the ``seq`` about to be
assigned is the event naming itself, and is missing like any other.

The other streams are read *without* taking their lock. Taking ``LOCK_SH`` on another
stream while holding ``LOCK_EX`` on this one would deadlock against a writer doing the same
the other way round. A lockless read is sound here because the reader's rules already make
it so (D80): a line is a record only once its newline is on disk, and the only line a
concurrent writer can be mid-way through is the last one, which is skipped as torn. What is
not there yet makes a cause missing, which refuses — fail closed — rather than lets through.

What is there is not yet necessarily *durable*, and that is the one gap a lockless read
opens. Writers A on ``d-mbp`` and B on ``d-phone``, where B cites what A writes:

1. A holds ``LOCK_EX`` on ``d-mbp``, writes ``d-mbp:45`` and flushes. The bytes are in the
   page cache; A has not yet called ``fsync``.
2. B holds ``LOCK_EX`` on ``d-phone`` and reads ``d-mbp`` without its lock. It sees 45,
   whole and newline-terminated: a record by the reader's rules. A reader taking ``LOCK_SH``
   would have waited for A; B does not, which is the point, and so B sees what is not yet
   on disk.
3. **B ``fsync``s ``d-mbp``** through the descriptor it read from, before writing anything.
   ``fsync`` on a file flushes every dirty page of that inode, whoever wrote them, so 45 is
   durable when the call returns. No lock is taken: ``flock`` does not gate ``fsync``.
4. B writes ``d-phone:1`` with ``caused_by: [d-mbp:45]``, flushes, ``fsync``s.
5. Power is lost. Whatever else happens, there is no state in which ``d-phone:1`` is on disk
   and ``d-mbp:45`` is not: B made 45 durable before B's own write began.

Without step 3, a crash between B's step 4 and A's own ``fsync`` leaves ``d-phone:1`` durably
citing an event that is gone, and ``index.update`` refuses the whole log with
``DanglingRefError`` for the rest of its life. With it, the worst case is that B pays for A's
``fsync``, and A's own ``fsync`` then finds nothing left to do. If A is still mid-write in
step 2, B sees a torn tail, 45 is not a record, and B refuses: still fail closed.
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

from walle.events import Event, parse_ref
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

TORN_MARK = b"~"
"""The first byte of a heal: what the writer puts at the end of a torn line before starting
its own record on a fresh one (the module docstring, *How a tear is healed*).

Any byte would do that is not whitespace and cannot end a JSON value — so that the torn line,
whatever it held, stays unparseable once this byte is on it, even if the heal is cut right
after it or right after the newline that follows. ``~`` rather than ``NUL`` because the file is
for a person with ``cat`` and ``grep`` (D10), and a NUL byte makes ``grep`` call the whole
stream binary. Fragments on disk were always arbitrary non-JSON bytes the reader skips (D80);
this changes what a stepped-over fragment ends in, not what the reader does with it.
"""


class CorruptLogError(Exception):
    """The last record-shaped line of a stream is JSON, but not a readable event.

    Deliberately fatal rather than repaired. The next ``seq`` is derived from that line, and
    guessing it risks reusing a number — which would not lose an event but would make one ref
    name two of them, permanently and silently. The reader halts on the same line
    (``reader.CorruptStreamError``): refusing here is agreement with it, not strictness.

    Not raised for a line that is not JSON at all. That is a fragment an interrupted append
    left and a heal stepped over, which the reader skips (D80), and the writer steps back past
    it to the last real record — a stream the reader loads must stay a stream the writer can
    grow (issue #128, finding 3.1).
    """


class DanglingCauseError(ValueError):
    """An event's ``caused_by`` names a ref no stream of this principal holds. Raised before
    anything is written (D111, issue #121): an append-only log cannot take this back."""

    def __init__(self, stream: str, missing: list[str]) -> None:
        super().__init__(
            f"refusing to append to {stream}: caused_by names {', '.join(missing)}, which "
            f"{'is' if len(missing) == 1 else 'are'} not in the log; a cause must already be "
            "an event of this principal (D111)"
        )
        self.missing = tuple(missing)


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


_NO_RECORD = object()
"""What ``_last_record`` returns in place of a decoded line when the stream holds none. A
sentinel rather than ``None``, because ``null`` is a JSON line too."""


def _decode(line: bytes) -> object:
    """The JSON value a complete line holds, or ``_NO_RECORD`` when it is not JSON."""
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _NO_RECORD


def _last_record(f: BinaryIO, size: int, path: Path) -> tuple[object, bool]:
    """``(the last complete line that is JSON, decoded; tail was torn)``, reading backwards
    from the end. The first element is ``_NO_RECORD`` when no complete line is JSON.

    "Complete" means newline-terminated. Complete lines that are blank or not JSON are stepped
    back past, by the reader's rule (D80): they are fragments an interrupted append left and a
    later heal stepped over, and a heal cut after its own newline leaves one of them *last*.
    The window doubles until a JSON line or byte zero is reached, so a fragment or a record
    larger than the window still works — it just costs another read.

    A window that starts mid-file may open in the middle of a line, so its first line is
    only trusted when the window reaches byte zero.
    """
    if size == 0:
        return _NO_RECORD, False
    window = _TAIL_WINDOW
    while True:
        passed = 0  # each pass rescans from the end, so the count is per pass
        start = max(0, size - window)
        f.seek(start)
        chunk = f.read(size - start)
        torn = not chunk.endswith(b"\n")
        cut = chunk.rfind(b"\n")
        lines = chunk[:cut].split(b"\n") if cut != -1 else []
        if start != 0:
            lines = lines[1:]  # may have begun mid-line; the next, wider read will hold it whole
        for line in reversed(lines):
            if not line.strip():
                continue
            record = _decode(line)
            if record is not _NO_RECORD:
                if passed:
                    logger.warning(
                        "%s: stepped back past %d complete line(s) that are not JSON to find "
                        "the last record — heals of an interrupted append were themselves "
                        "interrupted. Readers skip them (D80).",
                        path,
                        passed,
                    )
                return record, torn
            passed += 1
        if start == 0:
            return _NO_RECORD, torn
        window *= 2


def _seq_of(record: object, path: Path) -> int:
    """The ``seq`` of a decoded event line, or ``CorruptLogError`` naming what was wrong.

    The line was JSON (``_last_record`` saw to that); what can still be wrong is its shape.
    """
    seq = record.get("seq") if isinstance(record, dict) else None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise CorruptLogError(
            f"{path}: the last complete JSON line has no usable seq (found {seq!r}). Refusing "
            "to append rather than guess at the next one; the reader halts on this line too."
        )
    return seq


def _plan(f: BinaryIO, size: int, path: Path, event: Event) -> tuple[bytes, int]:
    """``(bytes to append, seq assigned)`` for ``event`` on a stream whose ``size`` bytes
    ``f`` can read. Reads; writes nothing; the one place the heal and the ``seq`` are decided,
    so the exhaustive crash test can drive it over a ``BytesIO`` and know it is testing what
    ``LockedStream.append`` does.
    """
    last, torn = _last_record(f, size, path)
    next_seq = 1 if last is _NO_RECORD else _seq_of(last, path) + 1
    payload = encode(event.model_copy(update={"seq": next_seq}))
    if torn:
        logger.warning(
            "%s: last line was not newline-terminated — an append was interrupted. "
            "Leaving the fragment in place, marking it, and starting seq %d on a fresh "
            "line; readers skip it.",
            path,
            next_seq,
        )
        payload = TORN_MARK + b"\n" + payload
    return payload, next_seq


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
        directory entry is too. Raises ``DanglingCauseError`` with nothing written — and, on
        a stream that does not exist yet, nothing created.
        """
        if event.caused_by and not self.path.exists():
            # `locked()` creates the directory and the file. A refusal after that would leave
            # an empty stream behind for the index to list (issue #128, finding 3.3), so the
            # causes are checked first. A stream that does not exist holds nothing, so any
            # cause on it is missing. Racy only in the harmless direction: if another writer
            # creates the file between here and `locked()`, the check under the lock below
            # runs again against what it wrote.
            _refuse_dangling(event, self.stream, self.path, held_here=set())
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
        """Append one event, returning it with its ``seq`` filled in. Durable on return.

        Raises ``DanglingCauseError``, with nothing written, when a ref in ``caused_by`` is
        not an event of this principal (see the module docstring).
        """
        self._check_causes(event)
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

        # Decided and encoded before anything is written, so a failure here leaves the file
        # untouched rather than leaving a stray newline behind it (coding rule 7).
        payload, next_seq = _plan(f, size, self.path, event)
        f.seek(0, os.SEEK_END)  # "a" mode appends anyway; this keeps the position honest
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
        return event.model_copy(update={"seq": next_seq})

    def _check_causes(self, event: Event) -> None:
        """Every ref in ``event.caused_by`` is an event of this principal, or
        ``DanglingCauseError`` names the ones that are not. This stream is read through the
        locked handle; any other stream of the principal is read from its file, without its
        lock, and ``fsync``ed before we write (the module docstring says why that is both
        necessary and sound)."""
        if not event.caused_by:
            return
        held = (
            {r.ref for r in self.read().records}
            if any(parse_ref(cause)[0] == self.stream for cause in event.caused_by)
            else set()
        )
        _refuse_dangling(event, self.stream, self.path, held_here=held)


def _refuse_dangling(event: Event, stream: str, path: Path, *, held_here: set[str]) -> None:
    """Raise ``DanglingCauseError`` unless every cause is held: on ``stream`` by ``held_here``
    (what the caller read of its own stream), on any other stream by its file."""
    if not event.caused_by:
        return
    wanted: dict[str, set[str]] = {}
    for cause in event.caused_by:
        source, _ = parse_ref(cause)
        wanted.setdefault(source, set()).add(cause)
    missing: list[str] = []
    for source, refs in wanted.items():
        held = (
            held_here if source == stream else _refs_of(path.with_name(f"{source}.jsonl"), source)
        )
        missing.extend(refs - held)
    if missing:
        raise DanglingCauseError(stream, sorted(missing, key=parse_ref))


def _refs_of(path: Path, stream: str) -> set[str]:
    """The refs a sibling stream file holds now, read without its lock and then made durable:
    what we are about to cite must be on disk before our citation is (the module docstring,
    *What is there is not yet necessarily durable*). A file that is not there holds nothing."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return set()
    with os.fdopen(fd, "rb") as f:
        data = f.read()
        # After the read, so everything the read saw is covered; before the parse, so a
        # failure here refuses the append with nothing written.
        os.fsync(fd)
    return {r.ref for r in parse_stream(data, path, stream).records}
