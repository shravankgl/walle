"""Writing a snapshot: check the gate and the forgets, then append, under one lock (D89, D95).

The digest itself comes from the Curator's model, which does not exist yet and must never be
reachable from here: this module takes the ``body`` as text, the way ``staging`` takes what a
person typed. What it owns is the write. Under ``consolidate.lock`` it brings the index up to
date, asks ``consolidation.gate`` whether a run is due, checks ``forgets`` with
``consolidation.check_forgets``, and appends one ``snapshot`` event. Anything refused is
refused before the append, so a refusal always means nothing was written.

Two runners, step by step (the walkthrough this file is required to carry)
--------------------------------------------------------------------------
Curator runs A and B for principal maya, both started at 09:00 on a profile that has been
quiet since 08:00 and has 45 observations since a run yesterday morning. Both planned the
same ``forgets`` and both have a digest. Observer W appends turns meanwhile, and a Curator C
writes a contradiction.

1. A takes ``consolidate.lock`` (exclusive). B blocks here, having read nothing.
2. A brings the index up to date (``index.lock``, then every stream file shared, for a
   moment) and folds the records it committed: idle 60 min, 45 new, 24 h since the last run.
   Due. Every ref in ``forgets`` is an observation nothing names. A appends its snapshot to
   ``d-srv.jsonl`` under that stream's lock, which the writer holds until the bytes are
   ``fsync``ed, then releases ``consolidate.lock``.
3. W appends a turn to ``d-mbp.jsonl``. It needs only its stream's lock. A new observation
   cannot change what A's snapshot hides, so W never takes ``consolidate.lock``.
4. B wakes **and only now** reads the log. It sees A's snapshot: 0 h since the last run, 1
   new observation. Not due. B raises ``NotDueError`` naming both, having written nothing.
5. C appends a contradiction naming ``d-mbp:7``, one of the observations A forgot. C takes
   no consolidation lock either, and does not need one: ``consolidation.view`` decides what
   to hide on every fold, so from now on ``d-mbp:7`` is *held*, back in the view and listed
   with the reason. The fold over the view still equals the fold over the log.

If B had checked the gate before waiting for the lock, it would wake with a stale "due" and
append a second snapshot forgetting the same refs: two digests for one run. The fold would
survive it (both hide the same refs, so the view is the same) but a log that says the same
week was summarised twice, minutes apart, is a bug to prevent, not to fold around. This is
D79's skip on overlap, made by the gate rather than by a second rule.

Step 5 is why the lock covers only the runs, not every writer. Making each writer take
``consolidate.lock`` would serialise the observer behind a model call's worth of digest
writing for no gain, because the view is what keeps the fold identical, and it is a fold.

Lock order is ``consolidate.lock``, then ``index.lock``, then stream files (shared),
released, then one stream file (exclusive). Nothing else takes ``consolidate.lock``, and
nothing takes it while holding another lock, so no cycle exists with ``staging.lock`` or the
indexer.

What this does not cover: two devices that do not share a filesystem each take their own
lock, and both may write a run before their logs meet. The view stays identical; the log says
two runs happened. Sync is deferred (D57).
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from walle import index
from walle.consolidation import Gate, Policy, SnapshotError, check_forgets, gate
from walle.events import ACTOR_CURATOR, Snapshot, make_ref, parse_ref
from walle.paths import log_dir, principal_dir
from walle.writer import StreamWriter

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's snapshot writer needs POSIX file locking (fcntl); Windows is not supported "
        "in Phase 0. Run it under WSL."
    )

import fcntl

CONSOLIDATE_LOCK_FILENAME = "consolidate.lock"


class NotDueError(Exception):
    """The gate says no run is due. Raised before anything is written."""

    def __init__(self, result: Gate) -> None:
        super().__init__("not due: " + "; ".join(result.reasons))
        self.gate = result


def consolidate_lock_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/consolidate.lock`` — held for one run's write.

    Per principal, like ``staging.lock``: two devices' runs on one profile must see each
    other. Kept here rather than in ``paths`` while other tasks edit that file; it moves there
    when it gains a second user.
    """
    return principal_dir(Path(root), principal) / CONSOLIDATE_LOCK_FILENAME


def write_snapshot(
    root: Path,
    principal: str,
    device: str,
    *,
    forgets: Sequence[str],
    body: str,
    model: str | None,
    now: datetime | None = None,
    policy: Policy | None = None,
) -> str:
    """Append one snapshot to ``device``'s stream if a run is due. Returns its ref.

    ``forgets`` is written in ref order. ``model`` names what produced ``body``, the
    snapshot's own provenance (D89). ``now`` defaults to the clock and is also the event's
    ``ts``, so the gate and the record agree about when the run happened.

    Raises, having written nothing: ``FileNotFoundError`` for a principal with no log,
    ``NotDueError`` when the gate is shut, ``SnapshotError`` for an empty ``body`` or a
    ``forgets`` the view would not apply in full, and ``ValueError`` for a bad name, ref or
    naive ``now``.
    """
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    if not body.strip():
        raise SnapshotError("a snapshot needs a body: the digest is the point of it")
    if isinstance(forgets, str):
        raise TypeError("forgets is a list of refs, not one string")
    ordered = sorted(forgets, key=parse_ref)  # a malformed ref is a ValueError, before any lock
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError(f"now must carry a time zone, got {now!r}")
    log = log_dir(root, principal)
    if not log.is_dir():
        # Checked before the lock file is created: a misspelt principal must not get a folder.
        raise FileNotFoundError(
            f"{log}: no log for principal {principal!r}, nothing to consolidate"
        )

    with _exclusive(consolidate_lock_path(root, principal)):
        # The clock is read here, after the lock, never before it: a run that waited behind
        # another would otherwise be gated on a stale "now" and stamped earlier than the run
        # it waited for. The same rule as the writer's seq.
        at = datetime.now(UTC) if now is None else now
        ts = at.astimezone(UTC).isoformat(timespec="microseconds")
        records = index.update(root, principal).records
        result = gate(records, at, policy)
        if not result.due:
            raise NotDueError(result)
        check_forgets(records, ordered)
        written = writer.append(
            Snapshot(ts=ts, actor=ACTOR_CURATOR, forgets=ordered, body=body, model=model)
        )
    assert written.seq is not None  # the writer always assigns one
    return make_ref(writer.stream, written.seq)


@contextlib.contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    """Hold ``consolidate.lock`` exclusively: one run's check and write at a time."""
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
