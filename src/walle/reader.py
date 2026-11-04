"""Reading a stream back — the other half of ``writer.py``.

One file per ``(principal, device)``, events in ``seq`` order. Everything downstream (the
resolver, the SQLite index, the rebuild test) reads the log through here, so the rule for
what counts as an event is decided once, in this module.

What counts as an event (D80)
-----------------------------
A crash mid-append leaves a fragment, and a bare ``json.loads`` over the file would then
fail on every read for the rest of the log's life — one bad line bricking a stream. So:

* **An unterminated final line is torn.** It is skipped without being parsed, even if it
  happens to parse: the writer never acknowledged it, and the next append will take its
  ``seq``. Whether the bytes form valid JSON says nothing about whether the write finished.
* **A line that is not JSON is a fragment the writer stepped over.** The writer leaves
  fragments in place and starts its next record on a fresh line (hard constraint 2), so
  after the next append a torn tail sits mid-file. Skipped.
* **A complete record with no newline, later stepped over, shares its ``seq`` with the line
  right after it.** The writer derived the next ``seq`` from the last *terminated* line, so
  it reused this one's number. The later record is the acknowledged one; the earlier is
  skipped, because one ref naming two events is the failure this log exists to prevent.
* **Everything else that is wrong is fatal.** Valid JSON that is not a valid event, a
  missing ``seq``, a ``seq`` that is not a JSON integer a ref can carry (``"2"``, ``true``,
  ``2.0``, more than 19 digits), a ``seq`` that goes backwards: none of these is an
  interrupted write, and skipping them would silently drop or misnumber a real record.
  ``CorruptStreamError`` names the file and line. An event from a newer schema raises
  ``FutureSchemaError`` (D111).

Every skip is returned in ``StreamContents.skips`` *and* logged as a warning — reported, not
silent. Gaps in ``seq`` are allowed and are not skips: a process that dies after ``flush``
and before ``fsync`` loses an event nobody referenced, and every remaining ref still names
exactly one event.

The reader never modifies the file. Healing is the writer's job, and even the writer only
steps over a tear; nothing truncates the log.

Two runners, step by step (a writer W and this reader R on ``d-mbp.jsonl``, ending at 44)
----------------------------------------------------------------------------------------
1. W holds ``LOCK_EX`` and is part-way through writing seq 45.
2. R opens the file and asks for ``LOCK_SH``. It blocks: shared and exclusive ``flock``
   locks exclude each other. R has read nothing.
3. W ``fsync``s and closes; its lock goes.
4. R gets ``LOCK_SH`` and reads the whole file: 44 *and* 45, both newline-terminated.
5. A second writer W2 arriving now blocks in ``LOCK_EX`` until R closes. R's snapshot is
   consistent: it never sees half of an append that is still in progress.

Without step 2, R could read W's half-written 45 and report "an append was interrupted"
about a write that was merely still happening — a status that lies. If W instead *dies*
in step 1, the OS drops its lock, R reads the fragment, and the report is true.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from walle.events import AnyEvent, make_ref, parse_event, parse_ref
from walle.paths import stream_id, stream_path

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's stream reader needs POSIX file locking (fcntl); Windows is not supported "
        "in Phase 0. Run it under WSL."
    )

import fcntl

logger = logging.getLogger(__name__)

_PREVIEW_BYTES = 80


class CorruptStreamError(Exception):
    """A complete line of a stream is wrong in a way no interrupted append explains.

    Fatal rather than skipped: skipping would drop or renumber a real record without anyone
    noticing. The message names the file and the 1-based line.
    """


@dataclass(frozen=True)
class Skip:
    """One line that was not loaded as an event, and why."""

    line: int
    """1-based line number in the file."""

    offset: int
    """Byte offset of the line's first byte."""

    reason: str
    preview: str
    """The first bytes of the line, decoded leniently — enough to find it with ``grep``."""


@dataclass(frozen=True)
class Record:
    """One event, with where it sits in the log."""

    ref: str
    """This event's own ref, ``d-<device>:<seq>`` (D57)."""

    seq: int
    """The event's ``seq``, never ``None`` here: a record without one is refused."""

    event: AnyEvent
    line: int
    """1-based line number in the file it was read from."""

    @property
    def causes(self) -> tuple[tuple[str, int], ...]:
        """``caused_by`` parsed into ``(stream, seq)`` pairs. They may name other streams."""
        return tuple(parse_ref(ref) for ref in self.event.caused_by)


@dataclass(frozen=True)
class StreamContents:
    """Everything one read of a stream found: the events, in ``seq`` order, and the skips."""

    stream: str
    path: Path
    records: tuple[Record, ...]
    skips: tuple[Skip, ...]

    def __iter__(self) -> Iterator[Record]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def events(self) -> tuple[AnyEvent, ...]:
        return tuple(record.event for record in self.records)


def read_stream(root: Path, principal: str, device: str) -> StreamContents:
    """Read ``<root>/principals/p-<principal>/log/d-<device>.jsonl``.

    A missing file raises ``FileNotFoundError``: a misspelt principal must not read as
    someone with no history.
    """
    return read_file(stream_path(Path(root), principal, device), stream_id(device))


def read_file(path: Path, stream: str) -> StreamContents:
    """Read one stream file whose events belong to ``stream`` (``d-<device>``)."""
    make_ref(stream, 1)  # validates the stream id before it is used to name anything
    path = Path(path)
    with open(path, "rb") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # see the walkthrough above
        data = f.read()
        # The lock goes with close.
    return _parse(data, path, stream)


def parse_stream(data: bytes, path: Path, stream: str) -> StreamContents:
    """Parse the bytes of one stream file that the caller read under its own ``LOCK_SH``.

    For a caller that already holds the lock (the index holds every stream's at once). Going
    through ``read_file`` instead would open the file again and ask for a second shared lock,
    which can queue behind a writer waiting for ``LOCK_EX`` while the first is still held.
    ``path`` is only used to name the file in errors and warnings.
    """
    make_ref(stream, 1)
    return _parse(data, Path(path), stream)


def _parse(data: bytes, path: Path, stream: str) -> StreamContents:
    # Split on the byte b"\n" only. ``str.splitlines`` would also break on U+2028 and other
    # separators, which ``encode`` writes raw (``ensure_ascii=False``) inside a body.
    lines = data.split(b"\n")
    tail = lines.pop()  # b"" when the file ends with a newline, as it should
    records: list[Record] = []
    skips: list[Skip] = []

    def skip(number: int, offset: int, raw: bytes, reason: str) -> None:
        entry = Skip(
            line=number,
            offset=offset,
            reason=reason,
            preview=raw[:_PREVIEW_BYTES].decode("utf-8", "replace"),
        )
        skips.append(entry)
        logger.warning("%s:%d: skipped — %s", path, number, reason)

    offset = 0
    number = 0
    for number, raw in enumerate(lines, start=1):
        start, offset = offset, offset + len(raw) + 1
        if not raw.strip():
            continue  # carries nothing, e.g. a heal's leading newline torn on its own
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            skip(number, start, raw, "not JSON — most likely an append interrupted mid-write (D80)")
            continue
        record = _record(parsed, path, number, stream)
        previous = records[-1] if records else None
        if previous is not None and record.seq == previous.seq:
            if previous.line != number - 1:
                raise CorruptStreamError(
                    f"{path}:{number}: seq {record.seq} repeats line {previous.line}, and "
                    "not on the next line — no interrupted append produces that"
                )
            records.pop()
            previous_raw = lines[previous.line - 1]
            skip(
                previous.line,
                start - len(previous_raw) - 1,
                previous_raw,
                f"seq {record.seq} is reused by the next line — the shape the writer leaves "
                "after stepping over an append interrupted before its newline; the later "
                "record is the acknowledged one",
            )
        elif previous is not None and record.seq < previous.seq:
            raise CorruptStreamError(
                f"{path}:{number}: seq {record.seq} after seq {previous.seq} "
                f"(line {previous.line}) — the stream is out of order"
            )
        records.append(record)

    if tail.strip():
        skip(
            number + 1,
            offset,
            tail,
            "unterminated final line: an append was interrupted before it finished (D80)",
        )

    return StreamContents(stream=stream, path=path, records=tuple(records), skips=tuple(skips))


def _record(data: object, path: Path, number: int, stream: str) -> Record:
    if not isinstance(data, dict):
        raise CorruptStreamError(f"{path}:{number}: a complete line that is not a JSON object")
    # Taken before parsing: the model validates ``seq`` in lax mode, so ``"2"``, ``true`` and
    # ``2.0`` all come out as ints, and only the raw value shows what was in the file.
    raw_seq = data.get("seq")
    try:
        event = parse_event(data)  # first, so a newer schema still halts by name (D111)
    except ValidationError as exc:
        raise CorruptStreamError(
            f"{path}:{number}: a complete line that is not a valid event — {exc}"
        ) from exc
    if event.seq is None:
        raise CorruptStreamError(f"{path}:{number}: an event with no seq")
    # The writer's rule (``writer._seq_of``): a JSON integer, not a bool, at least 1. A line
    # the writer would refuse to number from is not one the reader may load.
    if not isinstance(raw_seq, int) or isinstance(raw_seq, bool) or raw_seq < 1:
        raise CorruptStreamError(
            f"{path}:{number}: seq must be a JSON integer >= 1, found {raw_seq!r}"
        )
    try:
        ref = make_ref(stream, event.seq)
    except ValueError as exc:  # the stream id was checked in read_file; this is the seq
        raise CorruptStreamError(
            f"{path}:{number}: seq {raw_seq} is too large for a ref — {exc}"
        ) from exc
    return Record(ref=ref, seq=event.seq, event=event, line=number)
