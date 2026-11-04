"""Task 6's verify criterion, and the cases around it (D10, D59, D66, D96, D111).

  destroy, rebuild, same hash   delete the index, rebuild from the log, resolved-state hash equal
  incremental                   a second update indexes only the seqs after the tracking record
  future v halts by name        an event from a newer build stops the build and names itself
  dangling caused_by fails      a ref to a seq that does not exist fails the build
  the index is never truth      a tracking record the log disagrees with forces a full rebuild
  a failed build changes nothing the index file is byte-identical after any refused build
  one cut across streams        a writer cannot slip a cause/effect pair between two reads

Every event here is synthetic: a made-up person's dinners and moves, no real data.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

import walle.index as index_module
from walle.cli import main
from walle.events import (
    ACTOR_CURATOR,
    ACTOR_OBSERVER,
    ACTOR_USER,
    Assertion,
    Contradiction,
    Event,
    FutureSchemaError,
    Observation,
    Supersede,
)
from walle.index import (
    DanglingRefError,
    IndexReport,
    StaleIndexError,
    load_records,
    rebuild,
    state_hash,
    update,
)
from walle.paths import index_path, log_dir, stream_path
from walle.reader import Record, StreamContents, read_stream
from walle.resolver import resolve
from walle.writer import StreamWriter

Build = Callable[[Path, str], IndexReport]
TS = "2026-11-04T09:00:00.000000+00:00"
P = "maya"


def _obs(body: str) -> Observation:
    return Observation(ts=TS, actor=ACTOR_OBSERVER, source="transcript", observed_at=TS, body=body)


def _assertion(
    summary: str, *, caused_by: list[str] | None = None, valid_from: str | None = None
) -> Assertion:
    return Assertion(
        ts=TS,
        actor=ACTOR_USER,
        body=summary,
        summary=summary,
        caused_by=caused_by or [],
        valid_from=valid_from,
    )


def _write(root: Path, device: str, *events: Event) -> list[str]:
    writer = StreamWriter(root, P, device)
    return [f"d-{device}:{writer.append(e).seq}" for e in events]


def _log_state(root: Path) -> str:
    """The resolved-state hash computed from the log itself, no index involved."""
    records = [r for f in sorted(log_dir(root, P).glob("*.jsonl")) for r in _read(root, f.stem)]
    return state_hash(resolve(records))


def _read(root: Path, stream: str) -> tuple[Record, ...]:
    return read_stream(root, P, stream.removeprefix("d-")).records


def _index_state(root: Path) -> str:
    return state_hash(resolve(load_records(root, P)))


def _history(root: Path) -> None:
    """Two devices, with provenance across them, a supersede and a contradiction."""
    (o1,) = _write(root, "mbp", _obs("Said they moved to Pune in March."))
    (a1,) = _write(
        root, "phone", _assertion("Lives in Pune", caused_by=[o1], valid_from="2026-03-01")
    )
    _write(
        root,
        "mbp",
        _assertion("Lives in Bangalore", caused_by=[o1], valid_from="2026-09-01"),
        _assertion("Likes dosa for dinner"),
        Supersede(ts=TS, actor=ACTOR_USER, target="d-mbp:3", reason="was a one-off"),
        Contradiction(ts=TS, actor=ACTOR_CURATOR, caused_by=[a1, "d-mbp:2"]),
    )


# --- verify: destroy -> rebuild -> resolved-state hash equal (D66) ---------------------


def test_destroy_rebuild_gives_the_same_resolved_state_hash(tmp_path: Path) -> None:
    _history(tmp_path)
    update(tmp_path, P)
    before = _index_state(tmp_path)
    assert before == _log_state(tmp_path)

    index_path(tmp_path, P).unlink()  # destroy every derived byte
    report = rebuild(tmp_path, P)

    assert report.full
    assert _index_state(tmp_path) == before


def test_an_index_built_in_steps_hashes_the_same_as_one_built_from_nothing(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "mbp", _obs("Cooked rajma."))
    update(tmp_path, P)
    _history(tmp_path)
    update(tmp_path, P)
    stepped = _index_state(tmp_path)

    rebuild(tmp_path, P)
    assert _index_state(tmp_path) == stepped == _log_state(tmp_path)


def test_the_state_hash_sees_a_change_in_belief(tmp_path: Path) -> None:
    """Otherwise the equality above could hold for a hash that ignores its input."""
    _history(tmp_path)
    update(tmp_path, P)
    before = _index_state(tmp_path)
    _write(tmp_path, "phone", Supersede(ts=TS, actor=ACTOR_USER, target="d-phone:1"))
    update(tmp_path, P)
    assert _index_state(tmp_path) != before


def test_the_state_hash_sees_an_index_row_whose_content_drifted(tmp_path: Path) -> None:
    """Same refs, different content: the drift a rebuild test exists to catch (D66)."""
    _history(tmp_path)
    update(tmp_path, P)
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        (data,) = db.execute("SELECT data FROM events WHERE ref = 'd-phone:1'").fetchone()
        drifted = data.replace("Lives in Pune", "Lives in Mumbai")
        db.execute("UPDATE events SET data = ? WHERE ref = 'd-phone:1'", (drifted,))
    assert _index_state(tmp_path) != _log_state(tmp_path)
    rebuild(tmp_path, P)
    assert _index_state(tmp_path) == _log_state(tmp_path)


# --- verify: incremental rebuild picks up only new seqs (D96) --------------------------


def test_incremental_update_picks_up_only_new_seqs(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"), _obs("two"), _obs("three"))
    first = update(tmp_path, P)
    assert first.full  # no index yet
    assert first.added == ("d-mbp:1", "d-mbp:2", "d-mbp:3")

    _write(tmp_path, "mbp", _obs("four"), _obs("five"))
    _write(tmp_path, "phone", _obs("six"))
    second = update(tmp_path, P)

    assert not second.full
    assert second.added == ("d-mbp:4", "d-mbp:5", "d-phone:1")
    assert update(tmp_path, P).added == ()
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        tracking = dict(db.execute("SELECT stream, last_seq FROM streams").fetchall())
        rows = db.execute("SELECT count(*) FROM events").fetchone()[0]
    assert tracking == {"d-mbp": 5, "d-phone": 1}
    assert rows == 6


def test_incremental_does_not_rewrite_rows_it_already_has(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    update(tmp_path, P)
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        db.execute("UPDATE events SET actor = 'marker' WHERE ref = 'd-mbp:1'")
    _write(tmp_path, "mbp", _obs("two"))
    update(tmp_path, P)
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        actors = dict(db.execute("SELECT ref, actor FROM events").fetchall())
    assert actors == {"d-mbp:1": "marker", "d-mbp:2": ACTOR_OBSERVER}


# --- verify: a v newer than this build halts the fold by name (D111) ------------------


def _append_raw(root: Path, device: str, record: dict[str, object]) -> None:
    path = stream_path(root, P, device)
    with open(path, "ab") as f:
        f.write(json.dumps(record).encode() + b"\n")


def _future_assertion(seq: int) -> dict[str, object]:
    return {
        "type": "assertion",
        "v": 2,
        "seq": seq,
        "ts": TS,
        "actor": ACTOR_USER,
        "body": "x",
        "summary": "x",
    }


@pytest.mark.parametrize("build", [update, rebuild])
def test_a_future_schema_version_halts_the_build_by_name(tmp_path: Path, build: Build) -> None:
    _write(tmp_path, "mbp", _obs("Cooked rajma."))
    update(tmp_path, P)
    before = index_path(tmp_path, P).read_bytes()
    _append_raw(tmp_path, "mbp", _future_assertion(2))

    with pytest.raises(FutureSchemaError) as caught:
        build(tmp_path, P)

    message = str(caught.value)
    assert "assertion" in message and "v2" in message and "v1" in message
    notes = " ".join(getattr(caught.value, "__notes__", []))
    assert str(stream_path(tmp_path, P, "mbp")) in notes
    assert "index was not changed" in notes
    assert index_path(tmp_path, P).read_bytes() == before


def test_a_future_version_with_no_index_leaves_no_index_behind(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("Cooked rajma."))
    _append_raw(tmp_path, "mbp", _future_assertion(2))
    with pytest.raises(FutureSchemaError):
        update(tmp_path, P)
    assert not index_path(tmp_path, P).exists()


# --- verify: a dangling caused_by ref fails the rebuild (D111) -------------------------


@pytest.mark.parametrize("missing", ["d-mbp:99", "d-ghost:1"])
@pytest.mark.parametrize("build", [update, rebuild])
def test_a_dangling_caused_by_fails_the_build(tmp_path: Path, build: Build, missing: str) -> None:
    _write(tmp_path, "mbp", _obs("Cooked rajma."))
    update(tmp_path, P)
    before = index_path(tmp_path, P).read_bytes()
    _write(tmp_path, "phone", _assertion("Likes rajma", caused_by=["d-mbp:1", missing]))

    with pytest.raises(DanglingRefError) as caught:
        build(tmp_path, P)

    assert caught.value.dangling == (("d-phone:1", missing),)
    assert "d-phone:1" in str(caught.value) and missing in str(caught.value)
    assert index_path(tmp_path, P).read_bytes() == before


def test_a_cause_in_another_stream_is_not_dangling(tmp_path: Path) -> None:
    _history(tmp_path)
    update(tmp_path, P)
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        edges = set(db.execute("SELECT src, dst, kind FROM edges").fetchall())
    assert edges == {
        ("d-phone:1", "d-mbp:1", "caused_by"),
        ("d-mbp:2", "d-mbp:1", "caused_by"),
        ("d-mbp:5", "d-phone:1", "caused_by"),
        ("d-mbp:5", "d-mbp:2", "caused_by"),
    }


# --- the index is never truth (D10, D96) -----------------------------------------------


def test_a_tracking_record_the_log_disagrees_with_forces_a_full_rebuild(tmp_path: Path) -> None:
    """The shape a crash between flush and fsync leaves: seq 3 lost, then reused.

    The index saw the first seq 3. The log now holds a different seq 3. Indexing "only seqs
    after 3" would keep the lost event forever and never index its replacement.
    """
    _write(tmp_path, "mbp", _obs("one"), _obs("two"), _obs("three"))
    update(tmp_path, P)
    path = stream_path(tmp_path, P, "mbp")
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:2]))  # simulate the lost, never-fsynced tail
    _write(tmp_path, "mbp", _obs("three, again"), _obs("four"))

    report = update(tmp_path, P)

    assert report.full
    assert "d-mbp" in report.reason and "seq 3" in report.reason
    bodies = [getattr(r.event, "body", None) for r in load_records(tmp_path, P)]
    assert bodies == ["one", "two", "three, again", "four"]


def test_a_tracking_record_past_the_end_of_the_log_forces_a_full_rebuild(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "mbp", _obs("one"), _obs("two"))
    update(tmp_path, P)
    path = stream_path(tmp_path, P, "mbp")
    path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])

    report = update(tmp_path, P)
    assert report.full and "seq 2" in report.reason
    assert [r.ref for r in load_records(tmp_path, P)] == ["d-mbp:1"]


def test_a_stream_the_index_knows_but_the_log_lost_forces_a_full_rebuild(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    _write(tmp_path, "phone", _obs("two"))
    update(tmp_path, P)
    stream_path(tmp_path, P, "phone").unlink()

    report = update(tmp_path, P)
    assert report.full and "d-phone" in report.reason
    assert [r.ref for r in load_records(tmp_path, P)] == ["d-mbp:1"]


def test_a_garbage_index_file_is_replaced_not_trusted(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    index_path(tmp_path, P).write_bytes(b"not a database at all, just bytes" * 100)
    report = update(tmp_path, P)
    assert report.full
    assert [r.ref for r in load_records(tmp_path, P)] == ["d-mbp:1"]


def test_an_index_from_another_index_schema_is_rebuilt(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    update(tmp_path, P)
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        db.execute("UPDATE meta SET value = '999' WHERE key = 'index_schema'")
    with pytest.raises(StaleIndexError):
        load_records(tmp_path, P)
    assert update(tmp_path, P).full
    assert [r.ref for r in load_records(tmp_path, P)] == ["d-mbp:1"]


def test_a_write_that_fails_on_a_new_index_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the next run would find an empty file and give the wrong reason for it."""
    _write(tmp_path, "mbp", _obs("one"))

    def broken(event: Event) -> bytes:
        raise OSError("disk full")

    monkeypatch.setattr(index_module, "encode", broken)
    with pytest.raises(OSError, match="disk full"):
        update(tmp_path, P)
    assert not index_path(tmp_path, P).exists()


def test_a_write_that_fails_on_an_existing_index_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    update(tmp_path, P)
    _write(tmp_path, "mbp", _obs("two"), _obs("three"))
    calls: list[Event] = []
    real = index_module.encode

    def fails_second(event: Event) -> bytes:
        calls.append(event)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(event)

    monkeypatch.setattr(index_module, "encode", fails_second)
    with pytest.raises(OSError, match="disk full"):
        update(tmp_path, P)
    monkeypatch.setattr(index_module, "encode", real)
    assert [r.ref for r in load_records(tmp_path, P)] == ["d-mbp:1"]
    assert update(tmp_path, P).added == ("d-mbp:2", "d-mbp:3")


def test_a_busy_index_is_an_error_not_a_broken_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked file is not an unreadable one. Deleting a good index over a timeout, and
    saying it was unreadable, would be a status that lies."""
    _write(tmp_path, "mbp", _obs("one"))
    update(tmp_path, P)
    before = index_path(tmp_path, P).read_bytes()
    monkeypatch.setattr(index_module, "_BUSY_TIMEOUT_S", 0.05)
    holder = sqlite3.connect(index_path(tmp_path, P), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            update(tmp_path, P)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            load_records(tmp_path, P)
    finally:
        holder.close()
    assert index_path(tmp_path, P).read_bytes() == before


def test_loading_with_no_index_is_an_error_not_an_empty_history(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    with pytest.raises(FileNotFoundError):
        load_records(tmp_path, P)


def test_a_misspelt_principal_creates_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    with pytest.raises(FileNotFoundError):
        update(tmp_path, "mayaa")
    assert not (tmp_path / "principals" / "p-mayaa").exists()


# --- what is in the index ---------------------------------------------------------------


def test_fts_finds_an_event_by_a_word_in_its_text(tmp_path: Path) -> None:
    _history(tmp_path)
    update(tmp_path, P)
    with sqlite3.connect(index_path(tmp_path, P)) as db:
        hits = db.execute(
            "SELECT ref FROM events_fts WHERE events_fts MATCH ? ORDER BY ref", ("bangalore",)
        ).fetchall()
        reason = db.execute(
            "SELECT ref FROM events_fts WHERE events_fts MATCH ?", ('"one-off"',)
        ).fetchall()
    assert hits == [("d-mbp:2",)]
    assert reason == [("d-mbp:4",)]


def test_a_torn_tail_is_reported_and_not_indexed_until_it_is_whole(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    with open(stream_path(tmp_path, P, "mbp"), "ab") as f:
        f.write(b'{"type":"observation","seq":2,"bo')
    report = update(tmp_path, P)
    assert [(stream, skip.line) for stream, skip in report.skips] == [("d-mbp", 2)]
    assert report.added == ("d-mbp:1",)

    _write(tmp_path, "mbp", _obs("two"))  # the writer steps over the fragment
    assert update(tmp_path, P).added == ("d-mbp:2",)


def test_a_stray_jsonl_file_in_the_log_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _obs("one"))
    (log_dir(tmp_path, P) / "notes.jsonl").write_text("")
    with pytest.raises(ValueError, match=r"notes\.jsonl"):
        update(tmp_path, P)


# --- one cut across streams (the two-runner walkthrough, as a test) --------------------


def test_a_cause_and_effect_written_mid_read_never_look_dangling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer A appends d-mbp:2, writer B then appends d-phone:2 caused by it, both while
    the build is between reading d-mbp and reading d-phone.

    Reading each stream under its own short lock would see d-phone:2 and not d-mbp:2, and
    fail the build on a ref that is not dangling at all. Holding every stream's shared lock
    for the whole read makes both writers wait, so the build sees neither.
    """
    _write(tmp_path, "mbp", _obs("one"))
    _write(tmp_path, "phone", _obs("two"))
    update(tmp_path, P)

    def writers() -> None:
        (cause,) = _write(tmp_path, "mbp", _obs("three"))
        _write(tmp_path, "phone", _assertion("four", caused_by=[cause]))

    real = index_module.parse_stream
    started: list[threading.Thread] = []

    def read_then_race(data: bytes, path: Path, stream: str) -> StreamContents:
        contents = real(data, path, stream)
        if stream == "d-mbp" and not started:
            thread = threading.Thread(target=writers)
            thread.start()
            started.append(thread)
            time.sleep(0.3)  # long enough for both appends, were they not blocked
        return contents

    monkeypatch.setattr(index_module, "parse_stream", read_then_race)
    report = update(tmp_path, P)
    started[0].join(timeout=10)

    assert report.added == ()
    monkeypatch.setattr(index_module, "parse_stream", real)
    assert update(tmp_path, P).added == ("d-mbp:2", "d-phone:2")


# --- the command ------------------------------------------------------------------------


def test_walle_rebuild_reports_what_it_did(tmp_path: Path) -> None:
    _history(tmp_path)
    runner = CliRunner()
    first = runner.invoke(main, ["rebuild", "--root", str(tmp_path), "--principal", P])
    assert first.exit_code == 0, first.output
    assert "from nothing" in first.output and "no index yet" in first.output
    assert _index_state(tmp_path) in first.output

    _write(tmp_path, "mbp", _obs("Cooked rajma."))
    second = runner.invoke(main, ["rebuild", "--root", str(tmp_path), "--principal", P])
    assert second.exit_code == 0, second.output
    assert "1 new event" in second.output and "incremental" in second.output

    full = runner.invoke(main, ["rebuild", "--root", str(tmp_path), "--principal", P, "--full"])
    assert full.exit_code == 0, full.output
    assert "from nothing" in full.output


def test_walle_rebuild_fails_loudly_on_a_dangling_ref(tmp_path: Path) -> None:
    _write(tmp_path, "mbp", _assertion("Likes rajma", caused_by=["d-mbp:7"]))
    result = CliRunner().invoke(main, ["rebuild", "--root", str(tmp_path), "--principal", P])
    assert result.exit_code == 1
    assert "d-mbp:7" in result.output and "dangling" in result.output


# --- task 6 follow-up (issue #25): readers, recovery, one lock per read ----------------

_SLOW_BUILDER = r"""
import sys
import time
from pathlib import Path

import walle.index as ix

root, ready, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
real = ix._open_index


def slow(*args, **kwargs):
    opened = real(*args, **kwargs)
    ready.write_text("the index file is open and nothing is committed yet")
    time.sleep(1.0)  # a reader that does not wait sees this uncommitted file
    return opened


ix._open_index = slow
(ix.rebuild if mode == "full" else ix.update)(root, "maya")
"""


@pytest.mark.parametrize("start", ["no index", "garbage index", "full rebuild"])
def test_a_reader_during_a_fresh_build_gets_committed_state_or_waits(
    tmp_path: Path, start: str
) -> None:
    """Issue #25.1. A second *process* builds while this one reads; the reader must see the
    index before or after the commit, never the empty file in between."""
    _write(tmp_path, "mbp", _obs("one"), _obs("two"))
    before: set[tuple[str, ...]] = set()
    if start == "garbage index":
        index_path(tmp_path, P).write_bytes(b"not a database" * 100)
    elif start == "full rebuild":
        update(tmp_path, P)
        before.add(("d-mbp:1", "d-mbp:2"))
    _write(tmp_path, "phone", _obs("three"))
    after = ("d-mbp:1", "d-mbp:2", "d-phone:1")

    ready = tmp_path / "ready"
    builder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SLOW_BUILDER,
            str(tmp_path),
            str(ready),
            "full" if start == "full rebuild" else "update",
        ]
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert builder.poll() is None, "the builder exited before opening the index"
            assert time.monotonic() < deadline, "the builder never opened the index"
            time.sleep(0.01)
        seen = tuple(r.ref for r in load_records(tmp_path, P))
    finally:
        assert builder.wait(timeout=20) == 0
    assert seen in before | {after}


def _corrupt_past_meta(root: Path) -> None:
    """Overwrite the back half of the file: the header, sqlite_master and meta still read."""
    path = index_path(root, P)
    data = bytearray(path.read_bytes())
    page = int.from_bytes(data[16:18], "big")
    pages = len(data) // page
    assert pages >= 8, "the fixture is too small to corrupt past meta"
    for n in range(pages // 2, pages):
        data[n * page : (n + 1) * page] = b"\xa5" * page
    path.write_bytes(bytes(data))
    with contextlib.closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT value FROM meta").fetchone() == ("1",)
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("SELECT count(*), max(length(data)) FROM events").fetchone()


def _many(root: Path) -> None:
    _history(root)
    _write(
        root, "tab", *(_obs(f"Dinner {n}: rajma, rice and a long note. " * 8) for n in range(60))
    )


@pytest.mark.parametrize("damage", ["drop events", "drop edges", "drop streams", "drop events_fts"])
def test_walle_rebuild_full_recovers_a_dropped_table(tmp_path: Path, damage: str) -> None:
    """Issue #25.2. The recovery command must always rebuild from the log (constraint 5)."""
    _history(tmp_path)
    update(tmp_path, P)
    with contextlib.closing(sqlite3.connect(index_path(tmp_path, P))) as db:
        db.execute(f"DROP TABLE {damage.removeprefix('drop ')}")
        db.commit()

    full = CliRunner().invoke(
        main, ["rebuild", "--root", str(tmp_path), "--principal", P, "--full"]
    )

    assert full.exit_code == 0, full.output
    assert f"resolved state: {_log_state(tmp_path)}" in full.output
    assert _index_state(tmp_path) == _log_state(tmp_path)


def test_walle_rebuild_full_recovers_an_index_corrupted_past_meta(tmp_path: Path) -> None:
    _many(tmp_path)
    update(tmp_path, P)
    _corrupt_past_meta(tmp_path)

    full = CliRunner().invoke(
        main, ["rebuild", "--root", str(tmp_path), "--principal", P, "--full"]
    )

    assert full.exit_code == 0, full.output
    assert f"resolved state: {_log_state(tmp_path)}" in full.output
    assert _index_state(tmp_path) == _log_state(tmp_path)


@pytest.mark.parametrize("table", ["events", "edges", "streams", "events_fts"])
def test_an_index_missing_a_table_is_not_trusted(tmp_path: Path, table: str) -> None:
    """The incremental path and readers check every table, not only ``meta``."""
    _history(tmp_path)
    update(tmp_path, P)
    with contextlib.closing(sqlite3.connect(index_path(tmp_path, P))) as db:
        db.execute(f"DROP TABLE {table}")
        db.commit()

    with pytest.raises(StaleIndexError, match=table):
        load_records(tmp_path, P)
    report = update(tmp_path, P)
    assert report.full and table in report.reason
    assert _index_state(tmp_path) == _log_state(tmp_path)


def test_a_stream_is_locked_only_once_per_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #25.3. A second ``LOCK_SH`` through another ``open()`` can queue behind a
    waiting ``LOCK_EX`` on some kernels, and the first one is never released: deadlock."""
    _history(tmp_path)
    streams = {f.stat().st_ino: f.stem for f in log_dir(tmp_path, P).glob("*.jsonl")}
    shared: list[str] = []
    real = fcntl.flock

    def counting(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_SH and os.fstat(fd).st_ino in streams:
            shared.append(streams[os.fstat(fd).st_ino])
        real(fd, operation)

    monkeypatch.setattr(fcntl, "flock", counting)
    for build in (update, update, rebuild):
        shared.clear()
        build(tmp_path, P)
        assert sorted(shared) == ["d-mbp", "d-phone"]


def test_read_paths_open_the_index_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #25.4. Task 15 reads under a read-only profile: ``mode=ro``, never ``rw``."""
    _history(tmp_path)
    update(tmp_path, P)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database: str | Path, *args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(str(database))
        return real(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", spy)
    load_records(tmp_path, P)
    assert opened
    assert all(uri.startswith("file:") and uri.endswith("?mode=ro") for uri in opened), opened


@pytest.mark.parametrize("lock", ["lock file present", "lock file absent"])
def test_read_paths_work_on_a_read_only_profile(tmp_path: Path, lock: str) -> None:
    _history(tmp_path)
    update(tmp_path, P)
    expected = _log_state(tmp_path)
    folder = index_path(tmp_path, P).parent
    lock_file = folder / "index.lock"
    if lock == "lock file absent":
        lock_file.unlink()
    frozen = [p for p in (index_path(tmp_path, P), lock_file) if p.exists()]
    for path in frozen:
        path.chmod(0o444)
    folder.chmod(0o555)
    try:
        probe = folder / "probe"
        with contextlib.suppress(PermissionError):
            probe.touch()
            probe.unlink()
            pytest.skip("this process can write a 0o555 folder (root or CAP_DAC_OVERRIDE)")
        assert _index_state(tmp_path) == expected
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            ["index.sqlite", "log", *(["index.lock"] if lock_file in frozen else [])]
        )
    finally:
        folder.chmod(0o755)
        for path in frozen:
            path.chmod(0o644)


def test_walle_rebuild_prints_the_hash_of_what_it_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #25.5. Another builder landing between this build and its hash must not make
    the printed hash disagree with the printed counts."""
    _history(tmp_path)
    built = _log_state(tmp_path)
    real = index_module.update

    def then_another_builder(root: Path, principal: str) -> IndexReport:
        report = real(root, principal)
        _write(root, "phone", _assertion("Likes chai, written and indexed by someone else"))
        real(root, principal)
        return report

    monkeypatch.setattr(index_module, "update", then_another_builder)
    result = CliRunner().invoke(main, ["rebuild", "--root", str(tmp_path), "--principal", P])

    assert result.exit_code == 0, result.output
    assert "6 events" in result.output
    assert f"resolved state: {built}" in result.output


def test_walle_rebuild_prints_the_index_hash_so_drift_shows(tmp_path: Path) -> None:
    """The printed hash is folded from the index rows, not from the log just read: an old row
    that drifted shows as a hash that differs from the log's until ``--full``."""
    _history(tmp_path)
    update(tmp_path, P)
    with contextlib.closing(sqlite3.connect(index_path(tmp_path, P))) as db:
        (data,) = db.execute("SELECT data FROM events WHERE ref = 'd-phone:1'").fetchone()
        db.execute(
            "UPDATE events SET data = ? WHERE ref = 'd-phone:1'",
            (data.replace("Lives in Pune", "Lives in Mumbai"),),
        )
        db.commit()
    args = ["rebuild", "--root", str(tmp_path), "--principal", P]

    drifted = CliRunner().invoke(main, args)
    assert drifted.exit_code == 0, drifted.output
    assert f"resolved state: {_index_state(tmp_path)}" in drifted.output
    assert _log_state(tmp_path) not in drifted.output

    full = CliRunner().invoke(main, [*args, "--full"])
    assert f"resolved state: {_log_state(tmp_path)}" in full.output


def test_a_reader_that_cannot_lock_reports_its_own_error(tmp_path: Path) -> None:
    """No lock file, a read-only folder and no index: the error is "no index yet", and it
    does not arrive chained to the lock file's EACCES as if one caused the other."""
    _write(tmp_path, "mbp", _obs("one"))
    folder = index_path(tmp_path, P).parent
    folder.chmod(0o555)
    try:
        probe = folder / "probe"
        with contextlib.suppress(PermissionError):
            probe.touch()
            probe.unlink()
            pytest.skip("this process can write a 0o555 folder (root or CAP_DAC_OVERRIDE)")
        with pytest.raises(FileNotFoundError, match="no index yet") as caught:
            load_records(tmp_path, P)
        assert caught.value.__context__ is None
        assert not (folder / "index.lock").exists()
    finally:
        folder.chmod(0o755)
