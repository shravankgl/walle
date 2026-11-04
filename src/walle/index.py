"""The SQLite index: a rebuildable cache of one principal's log (D10, D59, D96, D111).

``principals/p-<name>/index.sqlite`` holds four things, all derived from the JSONL streams
beside it and none of them truth:

``events``      one row per event: ref, stream, seq, type, v, ts, actor, content hash, the line
                it was read from, and the event as JSON so a fold can run off the index.
``edges``       ``(src, dst, 'caused_by')`` for every ref in an event's ``caused_by``: ``src``
                is the event, ``dst`` the event it came from. ``ancestors``, behind
                ``walle why``, is a recursive query over this table (D59).
``events_fts``  FTS5 over each event's text: summary, body, reason or note.
``streams``     the tracking record, ``(stream, last_seq, last_hash)`` (D96).

Hard constraint 5 is the design: the log is canonical and this file can be deleted at any
time. ``rebuild`` makes it from nothing; ``update`` is the normal path and adds only the seqs
after each stream's tracking record. The rebuild test is that the two agree, compared by
``state_hash`` over the resolved state (D66).

What the index refuses (D111)
-----------------------------
* **An event from a newer build** halts the build. The reader raises ``FutureSchemaError``
  naming the event type, its ``v`` and the ``v`` this build knows; a note adds the file and
  this build's walle version.
* **A dangling ref**, a ``caused_by`` naming a seq no stream holds, fails the build with
  ``DanglingRefError`` listing every one. That means a truncated stream, a hand-edited log or
  a bug.
* Both are found while reading the log, before the index file is opened. A refused build
  leaves the index exactly as it was.

When the index disagrees with the log, the log wins
---------------------------------------------------
Before an incremental update, each tracking record is checked against the log. The log must
still have that seq, holding an event with that content hash. If not, the index is rebuilt
from nothing and the report says why. This is not paranoia. A writer that dies after
``flush`` and before ``fsync`` can lose its last event, and the next append then reuses that
seq (``writer.py``). An index that had already seen the lost event, and then took "only seqs
after N", would keep the lost event forever and never index the one that replaced it. A
stream the index knows whose file has gone, an unreadable file, and a file from another
index schema are handled the same way: start over, and say so.

Two runners, step by step
-------------------------
Writers A on ``d-mbp`` and B on ``d-phone``; this module's builder R. A appends ``d-mbp:4``;
B, having seen it, appends ``d-phone:2`` with ``caused_by: [d-mbp:4]``.

1. R takes ``index.lock`` (exclusive). A second builder R2 blocks here until R is done, so it
   never reads a tracking record that R is about to change.
2. R lists the log and takes ``LOCK_SH`` on **every** stream file, then lists again. If a new
   stream appeared meanwhile it drops the locks and starts over.
3. While R holds them, A's ``LOCK_EX`` on ``d-mbp`` blocks, and B cannot append either. R
   reads every stream through the handle it locked, so each stream is locked once: a second
   ``open`` and ``LOCK_SH`` could queue behind A's waiting ``LOCK_EX`` on kernels that put new
   shared locks behind a queued writer, while R's first lock keeps A waiting forever. What R
   has is a single cut. Either ``d-mbp:4`` was already written,
   and R sees it, or it was not, and then B, which only cites what it has seen, cannot have
   written ``d-phone:2`` yet.
4. R drops the stream locks, so A and B continue, and checks the cut for dangling refs and
   future versions without touching the index.
5. R writes the new rows and tracking records in one SQLite transaction and commits. For a
   fresh index (none yet, an unusable one, or ``rebuild``) R first deletes the old file and
   creates an empty one; between that and the commit the file is empty. R reads its own
   committed rows back for the report, still under the lock.
6. R drops ``index.lock``. R2 now reads the log, which is a superset of R's cut because the
   log only grows, and finds R's tracking records already there.

A reader Q (``load_records``) runs while R builds a fresh index.

1. Q takes ``index.lock`` **shared**. If R holds it, Q waits; if Q got it first, R's
   exclusive lock waits for Q, so R cannot delete or empty a file Q is reading.
2. Q, holding it, finds the file, opens it ``mode=ro``, checks the schema and reads every row.
3. Q drops the lock. What it read is the state before R started or after R committed.

Without step 1, Q can arrive in R's step 5 between "empty file created" and "commit", see no
tables and raise ``StaleIndexError``, telling the user to rebuild an index that is being
rebuilt. ``test_a_reader_during_a_fresh_build_gets_committed_state_or_waits`` stages that
with a second process. Readers share the lock, so they never wait for each other.

Reading each stream under its own short lock instead (``read_stream`` one at a time) fails
step 3. R reads ``d-mbp`` up to 3, A and B both append, R reads ``d-phone`` including seq 2,
and the build fails on a "dangling" ref to an event that exists. The status would lie.
``test_a_cause_and_effect_written_mid_read_never_look_dangling`` stages that race.

Lock order is always ``index.lock``, then stream files (shared), then SQLite. Readers take
``index.lock`` (shared), then SQLite. Writers take one stream lock and nothing else, so no
cycle exists. ``flock`` locks belong to an open file, not a process, so code holding
``index.lock`` must not call ``load_records``: the second open of the lock would wait for
the first.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from walle import __version__
from walle.events import AnyEvent, FutureSchemaError, content_hash, make_ref, parse_event, parse_ref
from walle.paths import STREAM_SUFFIX, index_lock_path, index_path, log_dir
from walle.reader import CorruptStreamError, Record, Skip, StreamContents, parse_stream
from walle.writer import encode

if TYPE_CHECKING:  # annotations only: the index stores records, it does not fold them
    from walle.resolver import Resolution

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's index needs POSIX file locking (fcntl); Windows is not supported in Phase 0. "
        "Run it under WSL."
    )

import fcntl

INDEX_SCHEMA = 1
"""The shape of the tables below. A file with any other value is rebuilt, never migrated:
there is nothing in it the log does not have (D10, D111)."""

_CUT_ATTEMPTS = 5
_BUSY_TIMEOUT_S = 10.0
_BUSY = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED"})

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE streams (stream TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, "
    "last_hash TEXT NOT NULL)",
    "CREATE TABLE events (ref TEXT PRIMARY KEY, stream TEXT NOT NULL, seq INTEGER NOT NULL, "
    "type TEXT NOT NULL, v INTEGER NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL, "
    "hash TEXT NOT NULL, line INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE (stream, seq))",
    "CREATE TABLE edges (src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, "
    "PRIMARY KEY (src, dst, kind))",
    "CREATE INDEX edges_dst ON edges (dst)",
    "CREATE VIRTUAL TABLE events_fts USING fts5 (ref UNINDEXED, text)",
)
_TABLES = ("events", "edges", "events_fts", "streams")
_TEXT_FIELDS = ("summary", "body", "reason", "note")


class DanglingRefError(Exception):
    """A ``caused_by`` names an event that no stream holds (D111). The build fails."""

    def __init__(self, dangling: tuple[tuple[str, str], ...], log: Path) -> None:
        shown = ", ".join(f"{src} -> {dst}" for src, dst in dangling[:5])
        more = f" and {len(dangling) - 5} more" if len(dangling) > 5 else ""
        super().__init__(
            f"{len(dangling)} dangling caused_by ref(s) in {log}: {shown}{more}. Each names "
            "an event no stream holds: a truncated stream, a hand-edited log or a bug (D111). "
            "The index was not changed."
        )
        self.dangling = dangling
        """``(src, dst)`` pairs: the event holding the ref, and the ref. In ref order."""


class StaleIndexError(Exception):
    """The index file cannot be read by this build. Run ``walle rebuild``."""


class CorruptIndexError(StaleIndexError):
    """A row in ``events`` does not parse as an event: the file was damaged or edited.

    Raised instead of the parser's own error (pydantic's ``ValidationError``, a JSON error),
    which quotes the row in its message. The row is log text, and a caller that shows this
    message to a model must treat it as such; ``mcp_server`` envelopes it. The index is
    derived, so the cure is the same as for any unusable index: ``walle rebuild``.
    """

    def __init__(self, path: Path, ref: str, cause: Exception) -> None:
        super().__init__(
            f"{path}: row {ref} in the index does not parse as an event ({cause}). "
            "Run `walle rebuild`."
        )
        self.ref = ref


@dataclass(frozen=True)
class IndexReport:
    """What one ``update`` or ``rebuild`` did."""

    path: Path
    full: bool
    """True when the index was rebuilt from nothing, whether asked for or forced."""

    reason: str
    """Why it was rebuilt from nothing, or ``"incremental"``."""

    added: tuple[str, ...]
    """Refs written this time, in ``(stream, seq)`` order. All of them, after a full build."""

    events: int
    """Events in the index afterwards."""

    streams: tuple[str, ...]
    skips: tuple[tuple[str, Skip], ...]
    """``(stream, skip)`` for every line the reader skipped (D80). Reported, not indexed."""

    records: tuple[Record, ...] = field(repr=False)
    """The index's rows as this build committed them, read back before ``index.lock`` was
    released. What ``events`` counts, and what to fold for this build's resolved state:
    ``load_records`` afterwards may already include another builder's work."""


# --- public -----------------------------------------------------------------------------


def update(root: Path, principal: str) -> IndexReport:
    """Index whatever the log has that the index does not. The normal path (D96).

    Falls back to a full rebuild, and says why in ``reason``, when there is no usable index
    or its tracking records disagree with the log.
    """
    return _build(Path(root), principal, full=False)


def rebuild(root: Path, principal: str) -> IndexReport:
    """Discard the index file and index the log from nothing. The rebuild test (D10).

    The old file is deleted, not reused, so this works on an index that is damaged anywhere,
    including past ``meta``: it is the recovery command and must always succeed from the log
    (hard constraint 5). It never reads the old rows.
    """
    return _build(Path(root), principal, full=True)


def load_records(root: Path, principal: str) -> tuple[Record, ...]:
    """Every event in the index, as the ``Record`` s the resolver folds.

    Says nothing about freshness: this is what the index holds, which may be behind the log.
    A missing index raises ``FileNotFoundError``, because no index must not read as no history.
    A row that does not parse raises ``CorruptIndexError``, never the parser's own error.

    Holds ``index.lock`` shared for the whole read, so a builder in progress is waited out
    and the rows are the state before or after its commit, never a file it has just created
    or emptied. Opens the file ``mode=ro`` and needs no write access anywhere (task 15).
    """
    root = Path(root)
    path = index_path(root, principal)
    with _shared(index_lock_path(root, principal)):
        if not path.is_file():
            raise FileNotFoundError(f"{path}: no index yet. Run `walle rebuild`.")
        try:
            db = _connect(path, mode="ro")
        except sqlite3.DatabaseError as exc:
            if exc.sqlite_errorname in _BUSY:
                raise
            raise StaleIndexError(
                f"{path}: the index file is unreadable ({exc}). Run `walle rebuild`."
            ) from exc
        with contextlib.closing(db):
            reason = _check(db)
            if reason is not None:
                raise StaleIndexError(f"{path}: {reason}. Run `walle rebuild`.")
            return _rows(db, path)


# `why` (D59). `chain` carries (ref, depth); UNION drops a repeated pair, and the depth bound
# (the number of events: no simple path is longer) ends a cycle a hand-edited log could hold.
# Each ancestor is reported once, at the shortest distance it was reached by.
_ANCESTORS = """
WITH RECURSIVE chain (ref, depth) AS (
    SELECT ref, 0 FROM events WHERE ref = ?
    UNION
    SELECT edges.dst, chain.depth + 1
    FROM edges JOIN chain ON edges.src = chain.ref
    WHERE edges.kind = 'caused_by' AND chain.depth < (SELECT COUNT(*) FROM events)
)
SELECT chain.ref, MIN(chain.depth), events.data
FROM chain JOIN events ON events.ref = chain.ref
GROUP BY chain.ref
"""


def ancestors(root: Path, principal: str, ref: str) -> tuple[tuple[str, int, AnyEvent], ...]:
    """``ref`` and every event its ``caused_by`` reaches, as ``(ref, depth, event)`` (D59).

    A ``WITH RECURSIVE`` query over ``edges``: ``ref`` itself at depth 0, what it cites at 1,
    and so on, each event once at its shortest depth. Ordered by depth, then ref.

    Reads the index only, under ``index.lock`` shared like ``load_records``, so it answers for
    what the index holds. ``KeyError`` when ``ref`` is not in it; ``ValueError`` for a
    malformed ref; ``CorruptIndexError`` for a row that does not parse. Every ancestor is in
    the index whenever ``ref`` is, because a build refuses a dangling ``caused_by`` (D111).
    """
    parse_ref(ref)  # a malformed ref is a ValueError, before any lock or file
    root = Path(root)
    path = index_path(root, principal)
    with _shared(index_lock_path(root, principal)):
        if not path.is_file():
            raise FileNotFoundError(f"{path}: no index yet. Run `walle rebuild`.")
        with contextlib.closing(_connect(path, mode="ro")) as db:
            reason = _check(db)
            if reason is not None:
                raise StaleIndexError(f"{path}: {reason}. Run `walle rebuild`.")
            rows = db.execute(_ANCESTORS, (ref,)).fetchall()
    if not rows:
        raise KeyError(f"no event {ref} in the index")
    chain = [(r, depth, _parsed(path, r, data)) for r, depth, data in rows]
    return tuple(sorted(chain, key=lambda row: (row[1], parse_ref(row[0]))))


def _parsed(path: Path, ref: str, data: str) -> AnyEvent:
    """The event in an index row, or ``CorruptIndexError`` naming the row.

    ``ValueError`` covers pydantic's ``ValidationError`` and ``json.JSONDecodeError``, whose
    messages quote the row. ``FutureSchemaError`` is not a ``ValueError`` and passes through:
    it names a version, not the row, and every reader already treats it as log text.
    """
    try:
        return parse_event(json.loads(data))
    except ValueError as exc:
        raise CorruptIndexError(path, ref, exc) from exc


def state_hash(resolution: Resolution) -> str:
    """SHA-256 of the resolved state in canonical JSON (D66).

    Covers everything the fold decides: which assertions are current and over what window,
    which are retired and by what, the open conflicts, and what could not be applied. Events
    enter by content hash, so the hash changes when a belief does and not when an index row
    is merely rewritten.
    """
    state = {
        "current": [
            {
                "ref": b.ref,
                "event": content_hash(b.assertion),
                "window": [b.window.valid_from, b.window.valid_to],
                "closed_by": list(b.closed_by),
            }
            for b in resolution.current
        ],
        "retired": [
            {
                "ref": r.ref,
                "event": content_hash(r.assertion),
                "by": list(r.by),
                "valid_to": r.valid_to,
            }
            for r in resolution.retired
        ],
        "conflicts": [{"ref": c.ref, "between": list(c.between)} for c in resolution.conflicts],
        "unapplied": [{"ref": u.ref, "reason": u.reason} for u in resolution.unapplied],
    }
    text = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


# --- the build --------------------------------------------------------------------------


def _build(root: Path, principal: str, *, full: bool) -> IndexReport:
    log = log_dir(root, principal)  # validates the name before it touches the filesystem
    if not log.is_dir():
        # Checked before index.lock is created: a misspelt principal must not get a folder.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to index")
    path = index_path(root, principal)

    with _exclusive(index_lock_path(root, principal)):
        cut = _read_cut(log)  # raises FutureSchemaError / CorruptStreamError, index untouched
        dangling = _dangling(cut)
        if dangling:
            raise DanglingRefError(dangling, log)

        conn, reason = _open_index(path, cut, full=full)
        with contextlib.closing(conn):
            tracked = {} if reason is not None else _tracked(conn)
            new = tuple(r for c in cut for r in c.records if r.seq > tracked.get(c.stream, 0))
            _write(conn, path, new, fresh=reason is not None)
            committed = _rows(conn, path)  # under index.lock: exactly what this build left

    return IndexReport(
        path=path,
        full=reason is not None,
        reason=reason or "incremental",
        added=tuple(r.ref for r in new),
        events=len(committed),
        streams=tuple(c.stream for c in cut),
        skips=tuple((c.stream, skip) for c in cut for skip in c.skips),
        records=committed,
    )


@contextlib.contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    """Hold ``index.lock`` exclusively: one builder per principal at a time."""
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.


_CANNOT_CREATE = frozenset({errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT})


def _open_for_shared_lock(lock: Path) -> int | None:
    """``index.lock`` opened read-only, created if need be, or ``None`` if it cannot be."""
    try:
        return os.open(lock, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        pass
    try:
        return os.open(lock, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError as exc:
        if exc.errno in _CANNOT_CREATE:
            return None
        raise


@contextlib.contextmanager
def _shared(lock: Path) -> Iterator[None]:
    """Hold ``index.lock`` shared while reading the index: wait for a builder, not a reader.

    Opened read-only, so a read-only lock file or folder is fine. If the lock file does not
    exist and cannot be created (a read-only or missing folder), the read goes ahead
    unlocked: a builder must create that same file before it touches the index, so a builder
    sharing this view of the folder cannot be running either.
    """
    fd = _open_for_shared_lock(lock)
    if fd is None:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)  # the lock goes with it


def _stream_files(log: Path) -> list[Path]:
    files = sorted(p for p in log.iterdir() if p.suffix == STREAM_SUFFIX)
    for path in files:
        try:
            make_ref(path.stem, 1)
        except ValueError as exc:
            # Refused, not skipped: a stream we cannot name is a stream whose events would be
            # silently left out of every fold.
            raise ValueError(
                f"{path}: not a stream file. Streams are named d-<device>{STREAM_SUFFIX}."
            ) from exc
    return files


def _read_cut(log: Path) -> tuple[StreamContents, ...]:
    """Every stream, read while all of them are share-locked at once (walkthrough, step 2)."""
    for _ in range(_CUT_ATTEMPTS):
        files = _stream_files(log)
        with contextlib.ExitStack() as held:
            handles = []
            for path in files:
                f = held.enter_context(open(path, "rb"))
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                handles.append((path, f))
            if _stream_files(log) != files:
                continue  # a stream appeared while locking; it is not in this cut
            # Read through the handle that holds the lock. Each stream is locked once.
            cut = [_read_one(path, f.read()) for path, f in handles]
            return tuple(sorted(cut, key=lambda c: c.stream))
    raise RuntimeError(
        f"{log}: new streams kept appearing while taking a consistent read. Nothing was "
        "indexed; run it again."
    )


def _read_one(path: Path, data: bytes) -> StreamContents:
    try:
        return parse_stream(data, path, path.stem)
    except (FutureSchemaError, CorruptStreamError) as exc:
        exc.add_note(f"found in {path} by walle {__version__}; the index was not changed")
        raise


def _dangling(cut: tuple[StreamContents, ...]) -> tuple[tuple[str, str], ...]:
    """``(src, dst)`` for each ``caused_by`` ref no stream in the cut holds (D111)."""
    known = {r.ref for c in cut for r in c.records}
    found = {
        (r.ref, cause)
        for c in cut
        for r in c.records
        for cause in r.event.caused_by
        if cause not in known
    }
    return tuple(sorted(found, key=lambda pair: (parse_ref(pair[0]), parse_ref(pair[1]))))


# --- the file ---------------------------------------------------------------------------


def _connect(path: Path, *, mode: Literal["rwc", "rw", "ro"]) -> sqlite3.Connection:
    """``rwc`` creates the file, ``rw`` (builders) never does, ``ro`` (readers) never writes.

    Autocommit, with explicit BEGIN/COMMIT below: the module's implicit transactions would
    commit around DDL behind our back.
    """
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=_BUSY_TIMEOUT_S)


def _check(db: sqlite3.Connection) -> str | None:
    """Why the open file is not an index this build can use, or ``None`` if it is.

    Looks at the schema only: ``meta``, then every table. Damage inside a table's pages is
    not found here; ``rebuild`` never reads the old rows, so it recovers from that too.
    """
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        if not names:
            return "the index file is empty"
        if "meta" not in names:
            return "not a walle index"
        row = db.execute("SELECT value FROM meta WHERE key = 'index_schema'").fetchone()
    except sqlite3.DatabaseError as exc:
        # Busy is not broken. ``OperationalError: database is locked`` is a DatabaseError too,
        # and treating it as "unreadable" would delete a good index over a timeout.
        if exc.sqlite_errorname in _BUSY:
            raise
        return f"the index file is unreadable ({exc})"
    found = row[0] if row else "unknown"
    if found != str(INDEX_SCHEMA):
        return f"index schema {found}, this build writes {INDEX_SCHEMA}"
    missing = [table for table in _TABLES if table not in names]
    if missing:
        return f"the index is missing table(s): {', '.join(missing)}"
    return None


def _unusable(path: Path) -> str | None:
    """``_check`` on the file at ``path``, for a builder (``mode=rw``)."""
    try:
        db = _connect(path, mode="rw")
    except sqlite3.DatabaseError as exc:
        if exc.sqlite_errorname in _BUSY:
            raise
        return f"the index file is unreadable ({exc})"
    with contextlib.closing(db):
        return _check(db)


def _rows(db: sqlite3.Connection, path: Path) -> tuple[Record, ...]:
    rows = db.execute("SELECT ref, seq, line, data FROM events ORDER BY stream, seq")
    return tuple(
        Record(ref=ref, seq=seq, event=_parsed(path, ref, data), line=line)
        for ref, seq, line, data in rows
    )


def _remove(path: Path) -> None:
    """Delete an index file and whatever SQLite left beside it. Only under ``index.lock``.

    The journal goes too: a hot journal left beside a *new* file of the same name would be
    "rolled back" into it on the next open, writing an old file's pages into a new one.
    """
    for leftover in (path, *(path.with_name(path.name + s) for s in ("-journal", "-wal", "-shm"))):
        leftover.unlink(missing_ok=True)


def _open_index(
    path: Path, cut: tuple[StreamContents, ...], *, full: bool
) -> tuple[sqlite3.Connection, str | None]:
    """A connection to the index, and why it is starting from an empty file (``None`` if not).

    Every full rebuild starts from an empty file: ``full``, an unusable file, and tracking
    records that disagree with ``cut``. Reusing the old one would mean reading and deleting
    its rows, and a file damaged past ``meta`` fails exactly there. Readers wait on
    ``index.lock`` (``load_records``), so none has the old file open while it is removed.
    """
    if not path.exists():
        reason: str | None = "no index yet"
    else:
        reason = _unusable(path)
        if reason is None and full:
            reason = "a full rebuild was asked for"
        elif reason is None:
            conn = _connect(path, mode="rw")
            try:
                reason = _disagreement(conn, cut)
            except BaseException:
                conn.close()
                raise
            if reason is None:
                return conn, None
            conn.close()  # closed before the file is removed below
        _remove(path)
    return _connect(path, mode="rwc"), reason


def _tracked(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT stream, last_seq FROM streams").fetchall())


def _disagreement(conn: sqlite3.Connection, cut: tuple[StreamContents, ...]) -> str | None:
    """Why the tracking records cannot be trusted against this log, or ``None``."""
    by_stream = {c.stream: c for c in cut}
    for stream, last_seq, last_hash in conn.execute(
        "SELECT stream, last_seq, last_hash FROM streams ORDER BY stream"
    ):
        contents = by_stream.get(stream)
        if contents is None:
            return f"{stream} is in the index but its log file is gone"
        record = next((r for r in contents.records if r.seq == last_seq), None)
        if record is None:
            return f"{stream}: the index has seq {last_seq} and the log does not"
        if content_hash(record.event) != last_hash:
            return f"{stream}: seq {last_seq} in the log is not the event the index has"
    return None


def _text(event: AnyEvent) -> str:
    parts = (getattr(event, name, None) for name in _TEXT_FIELDS)
    return "\n".join(p for p in parts if isinstance(p, str) and p)


def _write(
    conn: sqlite3.Connection,
    path: Path,
    records: tuple[Record, ...],
    *,
    fresh: bool,
) -> None:
    """Write ``records`` and their tracking records in one transaction."""
    if not records and not fresh:
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        if fresh:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO meta VALUES ('index_schema', ?)", (str(INDEX_SCHEMA),))
        last: dict[str, Record] = {}
        for record in records:
            event = record.event
            stream, _ = parse_ref(record.ref)
            conn.execute(
                "INSERT INTO events (ref, stream, seq, type, v, ts, actor, hash, line, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.ref,
                    stream,
                    record.seq,
                    event.type,
                    event.v,
                    event.ts,
                    event.actor,
                    content_hash(event),
                    record.line,
                    encode(event).decode("utf-8").rstrip("\n"),
                ),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO edges (src, dst, kind) VALUES (?, ?, 'caused_by')",
                [(record.ref, cause) for cause in event.caused_by],  # a repeated cause, once
            )
            if text := _text(event):
                conn.execute("INSERT INTO events_fts (ref, text) VALUES (?, ?)", (record.ref, text))
            last[stream] = record  # records arrive in (stream, seq) order
        conn.executemany(
            "INSERT INTO streams (stream, last_seq, last_hash) VALUES (?, ?, ?) "
            "ON CONFLICT (stream) DO UPDATE SET last_seq = excluded.last_seq, "
            "last_hash = excluded.last_hash",
            [(s, r.seq, content_hash(r.event)) for s, r in last.items()],
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            # A failed ROLLBACK must not replace the error that caused it. SQLite rolls an
            # uncommitted transaction back on close or on the next open in any case.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        if fresh:
            conn.close()
            _remove(path)  # an empty file would read as "the index file is empty" next time
        raise
