"""Writing a snapshot into the log (D89), gated (D95), under ``consolidate.lock``.

  on disk, folds identically  the snapshot written to a real log leaves the resolved-state hash
                              of the view equal to the hash of the log before it
  gated                       a run the gate says is not due writes nothing
  checked                     a snapshot that forgets the wrong thing writes nothing
  one run at a time           a second run waits for the lock, then finds the first one's
                              snapshot and writes nothing

Every event here is synthetic: a made-up person's dinners and plants, no real data.
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from walle.consolidation import Policy, SnapshotError, plan_forgets, view
from walle.events import (
    ACTOR_CURATOR,
    ACTOR_OBSERVER,
    ACTOR_USER,
    Assertion,
    Observation,
    Snapshot,
)
from walle.index import rebuild, state_hash, update
from walle.paths import principal_dir, stream_path
from walle.reader import Record, read_stream
from walle.resolver import resolve, staged
from walle.snapshots import NotDueError, consolidate_lock_path, write_snapshot
from walle.writer import StreamWriter

P = "maya"
T0 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=1)
BODY = "Maya cooked at home most evenings and kept the basil alive."


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="microseconds")


def _seed(root: Path, n: int = 24) -> None:
    """n observations on the laptop and one belief citing the first, all synthetic."""
    mbp = StreamWriter(root, P, "mbp")
    for i in range(n):
        mbp.append(
            Observation(
                ts=_at(i),
                actor=ACTOR_OBSERVER,
                source="transcript",
                observed_at=_at(i),
                body=f"synthetic dinner note {i}",
            )
        )
    StreamWriter(root, P, "phone").append(
        Assertion(
            ts=_at(n),
            actor=ACTOR_USER,
            body="cooks at home",
            summary="cooks at home",
            caused_by=["d-mbp:1"],
        )
    )


def _records(root: Path) -> tuple[Record, ...]:
    return update(root, P).records


def test_a_written_snapshot_folds_identically_on_disk(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = _records(tmp_path)
    forgets = plan_forgets(before)
    assert len(forgets) == 12

    ref = write_snapshot(
        tmp_path, P, "srv", forgets=forgets, body=BODY, model="synthetic-model", now=LATER
    )

    assert ref == "d-srv:1"
    [record] = read_stream(tmp_path, P, "srv").records
    snapshot = record.event
    assert isinstance(snapshot, Snapshot)
    assert snapshot.forgets == list(forgets)
    assert (snapshot.actor, snapshot.body, snapshot.model) == (
        ACTOR_CURATOR,
        BODY,
        "synthetic-model",
    )
    assert snapshot.ts == LATER.isoformat(timespec="microseconds")

    after = rebuild(tmp_path, P).records  # destroy the index and rebuild it from the log
    seen = view(after)
    assert seen.forgotten == forgets
    assert state_hash(resolve(seen.records)) == state_hash(resolve(before))
    assert staged(seen.records) == staged(before)


def test_forgets_are_written_in_ref_order(tmp_path: Path) -> None:
    _seed(tmp_path)
    write_snapshot(
        tmp_path, P, "srv", forgets=["d-mbp:10", "d-mbp:2"], body=BODY, model=None, now=LATER
    )
    [record] = read_stream(tmp_path, P, "srv").records
    assert isinstance(record.event, Snapshot)
    assert record.event.forgets == ["d-mbp:2", "d-mbp:10"]


def test_a_run_that_is_not_due_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    too_soon = T0 + timedelta(minutes=30)  # the last event, the belief, was at minute 24
    with pytest.raises(NotDueError, match="idle 6 min, needs 30 min") as info:
        write_snapshot(tmp_path, P, "srv", forgets=["d-mbp:2"], body=BODY, model=None, now=too_soon)
    assert not info.value.gate.due
    assert not stream_path(tmp_path, P, "srv").exists()


def test_a_second_run_inside_the_gap_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path, n=45)
    write_snapshot(tmp_path, P, "srv", forgets=["d-mbp:2"], body=BODY, model=None, now=LATER)
    with pytest.raises(NotDueError, match="since the last run"):
        write_snapshot(tmp_path, P, "srv", forgets=["d-mbp:3"], body=BODY, model=None, now=LATER)
    assert len(read_stream(tmp_path, P, "srv")) == 1


def test_a_snapshot_that_forgets_a_belief_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(SnapshotError, match="d-phone:1 is an assertion event"):
        write_snapshot(tmp_path, P, "srv", forgets=["d-phone:1"], body=BODY, model=None, now=LATER)
    assert not stream_path(tmp_path, P, "srv").exists()


@pytest.mark.parametrize("body", ["", "   \n"])
def test_an_empty_digest_writes_nothing(tmp_path: Path, body: str) -> None:
    _seed(tmp_path)
    with pytest.raises(SnapshotError, match="body"):
        write_snapshot(tmp_path, P, "srv", forgets=["d-mbp:2"], body=body, model=None, now=LATER)
    assert not stream_path(tmp_path, P, "srv").exists()


def test_a_misspelt_principal_gets_no_folder(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no log"):
        write_snapshot(tmp_path, "nobody", "srv", forgets=["d-mbp:1"], body=BODY, model=None)
    assert not principal_dir(tmp_path, "nobody").exists()


def test_a_naive_now_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(ValueError, match="time zone"):
        write_snapshot(
            tmp_path,
            P,
            "srv",
            forgets=["d-mbp:2"],
            body=BODY,
            model=None,
            now=datetime(2026, 11, 5, 9, 0),  # naive on purpose
        )


def test_one_string_is_not_a_list_of_refs(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(TypeError, match="not one string"):
        write_snapshot(tmp_path, P, "srv", forgets="d-mbp:2", body=BODY, model=None, now=LATER)


def test_the_clock_is_read_after_the_lock(tmp_path: Path) -> None:
    """With no ``now``, the snapshot's ``ts`` is the moment the run got the lock."""
    long_ago = "2020-01-01T09:00:00.000000+00:00"  # so the real clock always finds it idle
    StreamWriter(tmp_path, P, "mbp").append(
        Observation(
            ts=long_ago, actor=ACTOR_OBSERVER, source="transcript", observed_at=long_ago, body="x"
        )
    )
    started = datetime.now(UTC)
    write_snapshot(
        tmp_path, P, "srv", forgets=["d-mbp:1"], body=BODY, model=None, policy=Policy(new=1)
    )
    [record] = read_stream(tmp_path, P, "srv").records
    assert datetime.fromisoformat(record.event.ts) >= started


def test_the_policy_is_the_callers(tmp_path: Path) -> None:
    _seed(tmp_path, n=3)
    ref = write_snapshot(
        tmp_path,
        P,
        "srv",
        forgets=["d-mbp:1"],
        body=BODY,
        model=None,
        now=LATER,
        policy=Policy(new=3),
    )
    assert ref == "d-srv:1"


_SECOND_RUN = """
import sys
from datetime import datetime
from pathlib import Path
from walle.snapshots import NotDueError, write_snapshot
try:
    ref = write_snapshot(
        Path(sys.argv[1]), "maya", "srv", forgets=["d-mbp:3"], body="second run",
        model=None, now=datetime.fromisoformat(sys.argv[2]),
    )
except NotDueError as exc:
    print("not due:", exc)
else:
    print("wrote", ref)
"""


def test_a_second_run_waits_for_the_lock_and_finds_the_first_runs_snapshot(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, n=45)
    _records(tmp_path)  # the index exists before the race starts
    lock = consolidate_lock_path(tmp_path, P)
    with open(lock, "ab") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)  # runner A has the lock
        second = subprocess.Popen(
            [sys.executable, "-c", _SECOND_RUN, str(tmp_path), LATER.isoformat()],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1.0)
            assert second.poll() is None, second.communicate()  # B is waiting on the lock
            assert not stream_path(tmp_path, P, "srv").exists()  # and has written nothing
            # A writes its snapshot while it holds the lock, as write_snapshot would.
            StreamWriter(tmp_path, P, "srv").append(
                Snapshot(
                    ts=LATER.isoformat(timespec="microseconds"),
                    actor=ACTOR_CURATOR,
                    forgets=["d-mbp:2"],
                    body=BODY,
                )
            )
        except BaseException:
            second.kill()
            raise
    out, err = second.communicate(timeout=30)
    assert second.returncode == 0, err
    assert out.startswith("not due:"), out + err
    assert "0.00 h since the last run (d-srv:1)" in out
    assert len(read_stream(tmp_path, P, "srv")) == 1
