"""Issue #128: the writer and the reader are one protocol, and a crash never breaks it
(D8, D80, D88, D111).

The second audit found that an interrupted append could do what D80 exists to prevent: leave
a stream the writer refuses to append to forever (3.1), or make a record out of bytes no
writer ever acknowledged (3.2). Each reproduction below was re-run on ``main`` at 8af004b
and held, except where a test's docstring says otherwise; each is a failing test first.

The property these tests hold the log to, as one sentence: **no sequence of crashes, full
disks or torn writes leaves a stream that cannot be appended to or a log that will not
rebuild, and an unacknowledged event never comes back as acknowledged.** The exhaustive
tests at the end do not reason about which crashes matter; they interrupt an append at every
byte offset, then a heal at every byte offset, then a heal of a heal at every byte offset,
and check every state the file can be in against the reader and the writer.

  3.1  a heal torn before its newline left a non-JSON *complete* line last; the reader
       skipped it (D80) and the writer refused it — a stream that loads but cannot grow.
       Now the writer steps back past what the reader skips, by the same rule.
  3.2  a heal began with a bare ``\\n``. Torn after that one byte, it turned a complete
       record that no writer acknowledged into a terminated line: resurrected, with a seq
       the next writer then built on. Now a heal begins with a byte that no JSON line can
       end in, so a torn tail stays torn however the heal is interrupted.
  3.3  ``locked()`` created the directory and an empty stream file before ``_check_causes``
       could refuse. Now a refusal on a stream that does not exist creates nothing.
  4.1  ``Event._upcast`` took ``v: true`` as 1 and blamed a missing upcaster for ``v: 0``.
       Now ``v`` must be a JSON integer >= 1, the rule ``seq`` already followed.
  race ``_check_causes`` read a sibling stream without its lock — necessary, or two writers
       citing each other deadlock — but a cause seen in the page cache before its writer's
       ``fsync`` could be lost with our citation already durable. Now the sibling is
       ``fsync``ed before a byte of ours is written.

Every file here is synthetic and lives under ``tmp_path``; the events are one-word bodies
with a fixed timestamp.
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import walle.writer as writer_mod
from walle import index
from walle.events import Observation, parse_event
from walle.reader import CorruptStreamError, Record, parse_stream, read_file, read_stream
from walle.writer import DanglingCauseError, StreamWriter, encode

TS = "2026-11-06T00:00:00+00:00"


def _obs(body: str, **extra: Any) -> Observation:
    return Observation(
        ts=TS, actor="observer", source="transcript", observed_at=TS, body=body, **extra
    )


def _raw(seq: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "observation",
        "seq": seq,
        "ts": TS,
        "actor": "observer",
        "source": "transcript",
        "observed_at": TS,
        "body": "b",
        **extra,
    }


def _body(record: Record) -> str:
    assert isinstance(record.event, Observation)
    return record.event.body


def _bodies(root: Path, device: str = "mbp") -> list[tuple[str, str]]:
    return [(r.ref, _body(r)) for r in read_stream(root, "maya", device).records]


# --- a disk that fills, or a process that dies, n bytes into a write ----------------------


class _Cut:
    """A file whose ``write`` takes ``limit`` bytes in total and then fails with ``ENOSPC``.

    Wraps the real file the writer opened, so the lock, the seeks and the reads are real;
    only the write is cut short. This is what a full disk does: part of the payload reaches
    the file, the rest does not, and the writer sees an ``OSError``.
    """

    def __init__(self, inner: Any, limit: int) -> None:
        self._inner = inner
        self._left = limit

    def write(self, data: bytes) -> int:
        taken = data[: self._left]
        self._left -= len(taken)
        if taken:
            self._inner.write(taken)
        self._inner.flush()
        if len(taken) < len(data):
            raise OSError(errno.ENOSPC, "No space left on device")
        return len(taken)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> _Cut:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._inner.__exit__(*exc)


@contextmanager
def _disk_full_after(monkeypatch: pytest.MonkeyPatch, limit: int) -> Iterator[None]:
    """The next file the writer opens accepts ``limit`` bytes of writes, then ``ENOSPC``."""
    real_open = open

    def cut_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        f = real_open(path, mode, *args, **kwargs)
        return _Cut(f, limit) if "b" in mode and "a" in mode else f

    with monkeypatch.context() as patched:
        patched.setattr(writer_mod, "open", cut_open, raising=False)
        yield


def _append_cut(monkeypatch: pytest.MonkeyPatch, writer: StreamWriter, body: str, at: int) -> None:
    """Interrupt an append ``at`` bytes into its payload, as a full disk would."""
    with _disk_full_after(monkeypatch, at), pytest.raises(OSError, match="No space"):
        writer.append(_obs(body))


# --- 3.1: a heal torn before its newline -------------------------------------------------


def test_3_1_a_heal_torn_before_its_newline_does_not_brick_the_stream(tmp_path: Path) -> None:
    """The issue's reproduction. Crash 1 tears an append; crash 2 interrupts the heal after
    it has terminated the first fragment but before its own newline. The reader loads the
    stream with two skips; the writer must then append to it, not refuse."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("turn 1"))
    with open(w.path, "ab") as f:
        f.write(b'{"actor":"observer","body":"crash 1')
    with open(w.path, "ab") as f:
        f.write(b'\n{"actor":"observer","body":"crash 2')
    assert [r.ref for r in read_stream(tmp_path, "maya", "mbp").records] == ["d-mbp:1"]

    healed = w.append(_obs("turn 2"))

    assert healed.seq == 2
    assert _bodies(tmp_path) == [("d-mbp:1", "turn 1"), ("d-mbp:2", "turn 2")]


def test_3_1_the_writer_steps_back_past_every_line_the_reader_skips(tmp_path: Path) -> None:
    """Several stepped-over fragments and blank lines in a row, then a record: the next seq
    comes from the record, which is what the reader would load."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("one"))
    w.append(_obs("two"))
    with open(w.path, "ab") as f:
        f.write(b'{"seq":3,"ts":"tr\n')
        f.write(b"\n")
        f.write(b"not json at all\n")
        f.write(b"\xff\xfe binary\n")
    assert [r.seq for r in read_stream(tmp_path, "maya", "mbp").records] == [1, 2]

    assert w.append(_obs("three")).seq == 3
    assert [r.seq for r in read_stream(tmp_path, "maya", "mbp").records] == [1, 2, 3]


def test_3_1_a_stream_of_nothing_but_fragments_starts_at_seq_one(tmp_path: Path) -> None:
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.path.parent.mkdir(parents=True)
    w.path.write_bytes(b'{"seq":1,"ts":"tr\n\n{"seq":1')
    assert read_stream(tmp_path, "maya", "mbp").records == ()

    assert w.append(_obs("first")).seq == 1
    assert _bodies(tmp_path) == [("d-mbp:1", "first")]


def test_3_1_a_fragment_larger_than_the_tail_window_is_still_stepped_past(tmp_path: Path) -> None:
    """A torn snapshot body can be bigger than the window the writer reads backwards. The
    window doubles until it reaches a record or byte zero, past the fragment."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("before"))
    with open(w.path, "ab") as f:
        f.write(b'{"seq":2,"body":"' + b"x" * (3 * writer_mod._TAIL_WINDOW) + b"\n")
    assert w.append(_obs("after")).seq == 2


def test_3_1_valid_json_that_is_not_a_record_still_refuses(tmp_path: Path) -> None:
    """What stays fatal: a complete line that *is* JSON but has no usable seq. The reader
    halts on it too (``CorruptStreamError``), so refusing to number from it is agreement,
    not strictness; no interrupted append produces valid JSON with the wrong seq."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("one"))
    with open(w.path, "ab") as f:
        f.write(b'{"seq":"2"}\n')
    with pytest.raises(writer_mod.CorruptLogError, match="seq"):
        w.append(_obs("two"))
    with pytest.raises(CorruptStreamError):
        read_stream(tmp_path, "maya", "mbp")


def test_3_1_seam_a_doubly_torn_stream_rebuilds_and_grows(tmp_path: Path) -> None:
    """Through the real index: the file after two crashes indexes, the next append lands,
    and an incremental update sees exactly the records the reader does."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("one"))
    with open(w.path, "ab") as f:
        f.write(b'{"seq":2,"body":"crash 1')
    with open(w.path, "ab") as f:
        f.write(b'\n{"seq":2,"body":"crash 2')
    index.rebuild(tmp_path, "maya")
    assert [r.ref for r in index.load_records(tmp_path, "maya")] == ["d-mbp:1"]

    w.append(_obs("two"))
    index.update(tmp_path, "maya")

    assert [(r.ref, _body(r)) for r in index.load_records(tmp_path, "maya")] == [
        ("d-mbp:1", "one"),
        ("d-mbp:2", "two"),
    ]


# --- 3.2: a heal torn after its first byte -----------------------------------------------


def test_3_2_a_heal_torn_after_its_first_byte_never_acknowledges_the_torn_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resurrection. A record is written whole but for its newline, so no writer ever
    acknowledged it and the reader skips it. The heal is then cut after one byte — the
    exact failure from the issue. The record must still not be a record, and the next
    writer must still give its seq to something else."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("acknowledged"))
    with open(w.path, "ab") as f:
        f.write(encode(_obs("never acknowledged").model_copy(update={"seq": 2}))[:-1])
    assert _bodies(tmp_path) == [("d-mbp:1", "acknowledged")]

    _append_cut(monkeypatch, w, "heal, cut after one byte", at=1)

    assert _bodies(tmp_path) == [("d-mbp:1", "acknowledged")]
    real = w.append(_obs("the real seq 2"))
    assert real.seq == 2
    assert _bodies(tmp_path) == [("d-mbp:1", "acknowledged"), ("d-mbp:2", "the real seq 2")]


def test_3_2_a_heal_s_first_byte_cannot_end_a_json_line() -> None:
    """The rule the fix rests on: whatever the torn tail is, appending the heal's first byte
    leaves a line that does not parse. Checked over every tail a single record can be cut
    to, plus the hand-made shapes a hostile fragment could take."""
    mark = writer_mod.TORN_MARK
    assert len(mark) == 1 and not mark.isspace() and mark not in b'"}]0123456789eEl'
    record = encode(_obs("café ☕").model_copy(update={"seq": 2}))
    tails = [record[:k] for k in range(len(record))] + [b"1", b"true", b'"s"', b"[1]", b"{}"]
    for tail in tails:
        with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
            json.loads(tail + mark)
        with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
            json.loads(tail + mark + mark)


def test_3_2_the_torn_record_s_bytes_are_kept_not_erased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard constraint 2 still holds under the fix: the heal marks the tear, it does not
    truncate it. The unacknowledged bytes are evidence a crash happened here."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("one"))
    torn = encode(_obs("never acknowledged").model_copy(update={"seq": 2}))[:-1]
    with open(w.path, "ab") as f:
        f.write(torn)
    w.append(_obs("two"))
    assert torn in w.path.read_bytes()


def test_3_2_1_the_issue_s_reader_shape_still_halts_as_corruption() -> None:
    """Finding 3.2's first half reproduces as written — the reader raises on a repeated
    seq with a blank line between — and that is kept, deliberately. No heal, old or new,
    produces the shape: the old heal only terminated a torn record by writing the next
    record's bytes right after it, and the new heal never terminates one as JSON at all.
    The exhaustive tests below are the proof that no reachable state raises; this shape
    means a hand-edited log, and one ref naming two events is what the reader exists to
    refuse. The fix for 3.2 is in the writer."""
    e1 = encode(_obs("1").model_copy(update={"seq": 1}))
    unacked = encode(_obs("unack").model_copy(update={"seq": 2}))[:-1]
    acked = encode(_obs("ack").model_copy(update={"seq": 2}))
    with pytest.raises(CorruptStreamError, match="repeats line 2"):
        parse_stream(e1 + unacked + b"\n\n" + acked, Path("d-mbp.jsonl"), "d-mbp")


def test_3_2_a_log_healed_by_the_old_writer_still_loads(tmp_path: Path) -> None:
    """D88: streams healed before this change have the bare-newline shape — a complete
    record with no newline, then the next record sharing its seq on the very next line. The
    reader keeps that rule for them, and the writer continues such a stream correctly."""
    e1 = encode(_obs("one").model_copy(update={"seq": 1}))
    unacked = encode(_obs("never acknowledged").model_copy(update={"seq": 2}))[:-1]
    acked = encode(_obs("the real two").model_copy(update={"seq": 2}))
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.path.parent.mkdir(parents=True)
    w.path.write_bytes(e1 + unacked + b"\n" + acked)

    assert _bodies(tmp_path) == [("d-mbp:1", "one"), ("d-mbp:2", "the real two")]
    assert w.append(_obs("three")).seq == 3
    assert [r.seq for r in read_stream(tmp_path, "maya", "mbp").records] == [1, 2, 3]


# --- 3.3: a refusal creates nothing ------------------------------------------------------


def test_3_3_a_dangling_cause_on_a_new_stream_leaves_no_file_or_directory(
    tmp_path: Path,
) -> None:
    """The issue's finding and Audit 2's finding 6: ``DanglingCauseError`` says it is raised
    before anything is written. On a stream that does not exist yet that must include the
    stream file and its directory, or the index then lists a phantom device."""
    w = StreamWriter(tmp_path, "maya", "mbp")
    with pytest.raises(DanglingCauseError):
        w.append(_obs("x", caused_by=["d-phone:1"]))
    assert not w.path.exists()
    assert not w.path.parent.exists()
    assert not (tmp_path / "principals").exists()


def test_3_3_a_dangling_cause_on_this_stream_before_it_exists_is_refused_the_same_way(
    tmp_path: Path,
) -> None:
    w = StreamWriter(tmp_path, "maya", "mbp")
    with pytest.raises(DanglingCauseError, match="d-mbp:1"):
        w.append(_obs("x", caused_by=["d-mbp:1"]))
    assert not w.path.exists()


def test_3_3_seam_the_index_lists_no_phantom_stream_after_a_refusal(tmp_path: Path) -> None:
    StreamWriter(tmp_path, "maya", "phone").append(_obs("real"))
    with pytest.raises(DanglingCauseError):
        StreamWriter(tmp_path, "maya", "mbp").append(_obs("x", caused_by=["d-phone:7"]))
    index.rebuild(tmp_path, "maya")
    assert sorted(p.name for p in (tmp_path / "principals/p-maya/log").glob("*.jsonl")) == [
        "d-phone.jsonl"
    ]
    assert [r.ref for r in index.load_records(tmp_path, "maya")] == ["d-phone:1"]


def test_3_3_a_cause_that_exists_on_a_sibling_still_creates_the_new_stream(tmp_path: Path) -> None:
    StreamWriter(tmp_path, "maya", "phone").append(_obs("cause"))
    written = StreamWriter(tmp_path, "maya", "mbp").append(_obs("effect", caused_by=["d-phone:1"]))
    assert written.seq == 1
    assert _bodies(tmp_path) == [("d-mbp:1", "effect")]


def test_3_3_a_refusal_on_an_existing_stream_writes_nothing(tmp_path: Path) -> None:
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.append(_obs("one"))
    before = w.path.read_bytes()
    with pytest.raises(DanglingCauseError):
        w.append(_obs("x", caused_by=["d-mbp:9"]))
    assert w.path.read_bytes() == before


# --- 4.1: v is a JSON integer >= 1 -------------------------------------------------------


def test_4_1_v_true_is_refused_not_read_as_one() -> None:
    with pytest.raises(ValidationError, match="v must be"):
        parse_event(_raw(1, v=True))


@pytest.mark.parametrize("v", [0, False, -1], ids=["zero", "false", "negative"])
def test_4_1_v_below_one_is_refused_as_a_bad_v_not_a_missing_upcaster(v: object) -> None:
    with pytest.raises(ValidationError, match="v must be") as caught:
        parse_event(_raw(1, v=v))
    assert "upcaster" not in str(caught.value)


def test_4_1_the_reader_halts_on_a_bad_v_naming_the_line(tmp_path: Path) -> None:
    path = tmp_path / "d-mbp.jsonl"
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in (_raw(1), _raw(2, v=True))))
    with pytest.raises(CorruptStreamError, match=r"d-mbp\.jsonl:2"):
        read_file(path, "d-mbp")


def test_4_1_v_one_and_a_missing_v_still_load() -> None:
    assert parse_event(_raw(1, v=1)).v == 1
    assert parse_event(_raw(1)).v == 1


# --- the lockless sibling read is made durable -------------------------------------------


def test_the_sibling_stream_is_fsynced_before_our_first_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer B on ``d-phone`` cites ``d-mbp:1``, which it reads without d-mbp's lock. If
    d-mbp's writer has written but not yet ``fsync``ed, the cause is in the page cache
    only. B must make it durable before B's own citation can be, or a power cut leaves a
    durable ref to an event that is gone (``DanglingRefError`` on every rebuild)."""
    mbp = StreamWriter(tmp_path, "maya", "mbp")
    mbp.append(_obs("cause"))
    phone = StreamWriter(tmp_path, "maya", "phone")
    phone.append(_obs("earlier"))  # so this is not a first append, with its directory fsync
    mbp_inode = os.stat(mbp.path).st_ino
    phone_size_before = os.path.getsize(phone.path)
    synced: list[tuple[int, int]] = []  # (inode fsynced, bytes d-phone held at that moment)
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced.append((os.fstat(fd).st_ino, os.path.getsize(phone.path)))
        real_fsync(fd)

    monkeypatch.setattr(writer_mod.os, "fsync", recording_fsync)
    phone.append(_obs("effect", caused_by=["d-mbp:1"]))

    inodes = [inode for inode, _ in synced]
    assert mbp_inode in inodes, "the sibling stream was never fsynced"
    first_sibling_sync = next(i for i, (inode, _) in enumerate(synced) if inode == mbp_inode)
    assert synced[first_sibling_sync][1] == phone_size_before, (
        "the sibling was fsynced only after our own bytes were already in the file"
    )


def test_a_sibling_cause_not_yet_terminated_is_refused_not_cited(tmp_path: Path) -> None:
    """The fail-closed half: a sibling record whose newline is not on disk is not a cause,
    however complete its bytes look. The other writer has not acknowledged it."""
    mbp = StreamWriter(tmp_path, "maya", "mbp")
    mbp.append(_obs("one"))
    with open(mbp.path, "ab") as f:
        f.write(encode(_obs("two, mid-write").model_copy(update={"seq": 2}))[:-1])
    phone = StreamWriter(tmp_path, "maya", "phone")
    with pytest.raises(DanglingCauseError, match="d-mbp:2"):
        phone.append(_obs("effect", caused_by=["d-mbp:2"]))
    assert not phone.path.exists()


def test_two_writers_citing_each_other_s_streams_do_not_deadlock(tmp_path: Path) -> None:
    """A holds d-mbp's lock and reads d-phone; B holds d-phone's lock and reads d-mbp.
    Neither asks for the other's lock, so both finish. Done with a session held open on
    one stream while the other appends, which is the interleaving a deadlock needs."""
    mbp = StreamWriter(tmp_path, "maya", "mbp")
    phone = StreamWriter(tmp_path, "maya", "phone")
    mbp.append(_obs("m1"))
    phone.append(_obs("p1"))
    with mbp.locked() as held:
        # B appends to d-phone citing d-mbp:1 while A holds d-mbp's LOCK_EX: B must not
        # need d-mbp's lock to read it.
        assert phone.append(_obs("p2", caused_by=["d-mbp:1"])).seq == 2
        # And A, still holding d-mbp, cites d-phone:2, which B has just fsynced.
        assert held.append(_obs("m2", caused_by=["d-phone:2"])).seq == 2
    index.rebuild(tmp_path, "maya")  # no dangling ref either way


# --- every byte offset, every heal, every heal of a heal ---------------------------------
#
# The model. A file state is bytes. An append from state S is what the real writer would
# write: the payload it computes from S (through the same `_plan` the writer uses, over a
# BytesIO of S), so the model and the writer cannot drift. A crash at offset k leaves
# S + payload[:k]; k == len(payload) is the append that returned, and only then is the
# event acknowledged. Every state is checked with the real reader and the real planner:
#
#   loads      parse_stream does not raise
#   honest     the records are exactly the acknowledged events, by (seq, body), in order
#   grows      the planner yields a next seq, and it is one past the last record
#
# `honest` is the property the issue names: a torn append is never a record, however many
# heals are cut on top of it; and nothing acknowledged is lost.


def _plan(state: bytes, event: Observation) -> tuple[bytes, int]:
    """``(payload the writer would append to a file holding state, seq it would assign)``."""
    f = io.BytesIO(state)
    return writer_mod._plan(f, len(state), Path("d-mbp.jsonl"), event)


def _check(state: bytes, acked: list[tuple[int, str]]) -> None:
    """``loads`` and ``honest``, for one state."""
    contents = parse_stream(state, Path("d-mbp.jsonl"), "d-mbp")
    got = [(r.seq, _body(r)) for r in contents.records]
    assert got == acked, f"state {state!r}: records {got} != acknowledged {acked}"


def _next(state: bytes, acked: list[tuple[int, str]], event: Observation) -> bytes:
    """``grows``: the payload the writer would append to ``state``, whose seq must be one past
    the last acknowledged record."""
    payload, seq = _plan(state, event)
    assert seq == (acked[-1][0] if acked else 0) + 1, f"state {state!r}: seq {seq} after {acked}"
    return payload


@pytest.fixture
def quiet(caplog: pytest.LogCaptureFixture) -> None:
    """The reader warns on every skip; tens of thousands of parses would spend most of the
    test formatting warnings nobody reads."""
    caplog.set_level(logging.ERROR, logger="walle.reader")
    caplog.set_level(logging.ERROR, logger="walle.writer")


def _first() -> tuple[bytes, list[tuple[int, str]]]:
    first = encode(_obs("first").model_copy(update={"seq": 1}))
    return first, [(1, "first")]


def test_every_byte_offset_an_append_can_be_cut_at_then_healed(quiet: None) -> None:
    """Depth one: the append of ``second`` is cut at every offset 0..len; each state loads
    and is honest; then a heal appends ``third`` whole and the result is honest again."""
    state0, acked0 = _first()
    second, third = _obs("second"), _obs("third")
    payload = _next(state0, acked0, second)
    for k in range(len(payload) + 1):
        state = state0 + payload[:k]
        acked = acked0 + ([(2, "second")] if k == len(payload) else [])
        _check(state, acked)
        heal = _next(state, acked, third)
        _check(state + heal, [*acked, (acked[-1][0] + 1, "third")])


def test_every_byte_offset_a_heal_can_be_cut_at_after_every_cut_append(
    quiet: None,
) -> None:
    """Depth two: for every cut of the append (k1) and every cut of the heal that follows
    (k2) — including the heal's first byte alone, finding 3.2 — the state loads and is
    honest, and a whole append afterwards lands with the right seq."""
    state0, acked0 = _first()
    second, third, fourth = _obs("second"), _obs("third"), _obs("fourth")
    p1 = _next(state0, acked0, second)
    for k1 in range(len(p1) + 1):
        s1 = state0 + p1[:k1]
        a1 = acked0 + ([(2, "second")] if k1 == len(p1) else [])
        p2 = _next(s1, a1, third)
        for k2 in range(len(p2) + 1):
            s2 = s1 + p2[:k2]
            a2 = a1 + ([(a1[-1][0] + 1, "third")] if k2 == len(p2) else [])
            _check(s2, a2)
            p3 = _next(s2, a2, fourth)
            _check(s2 + p3, [*a2, (a2[-1][0] + 1, "fourth")])


def test_every_byte_offset_a_heal_of_a_heal_can_be_cut_at(quiet: None) -> None:
    """Depth three: the append is torn mid-record, then the heal is cut at every offset,
    then the heal of that heal is cut at every offset, then an append lands whole."""
    state0, acked0 = _first()
    second, third, fourth, fifth = _obs("second"), _obs("third"), _obs("fourth"), _obs("fifth")
    p1 = _next(state0, acked0, second)
    s1 = state0 + p1[: len(p1) // 2]  # torn in the middle of the record
    p2 = _next(s1, acked0, third)
    for k2 in range(len(p2) + 1):
        s2 = s1 + p2[:k2]
        a2 = acked0 + ([(2, "third")] if k2 == len(p2) else [])
        p3 = _next(s2, a2, fourth)
        for k3 in range(len(p3) + 1):
            s3 = s2 + p3[:k3]
            a3 = a2 + ([(a2[-1][0] + 1, "fourth")] if k3 == len(p3) else [])
            _check(s3, a3)
            p4 = _next(s3, a3, fifth)
            _check(s3 + p4, [*a3, (a3[-1][0] + 1, "fifth")])


def test_a_complete_record_cut_only_at_its_newline_then_healed_at_every_offset(
    quiet: None,
) -> None:
    """The sharpest depth-two column on its own, so a failure names it: the append lacks
    only its newline (it parses), and the heal is cut at every offset. The record must
    never become acknowledged."""
    state0, acked0 = _first()
    p1 = _next(state0, acked0, _obs("never acknowledged"))
    s1 = state0 + p1[:-1]
    p2 = _next(s1, acked0, _obs("real"))
    after = _obs("after")
    for k2 in range(len(p2) + 1):
        s2 = s1 + p2[:k2]
        a2 = acked0 + ([(2, "real")] if k2 == len(p2) else [])
        _check(s2, a2)
        p3 = _next(s2, a2, after)
        _check(s2 + p3, [*a2, (a2[-1][0] + 1, "after")])


def test_a_torn_write_that_fills_the_rest_with_zeros_is_healed(quiet: None) -> None:
    """Some filesystems extend the file and lose the data: the tail is NUL bytes. Treated
    like any other torn tail, at every cut."""
    state0, acked0 = _first()
    p1 = _next(state0, acked0, _obs("second"))
    third = _obs("third")
    for k1 in range(len(p1)):
        s1 = state0 + p1[:k1] + b"\x00" * (len(p1) - k1)
        _check(s1, acked0)
        p2 = _next(s1, acked0, third)
        _check(s1 + p2, [*acked0, (2, "third")])


def test_the_model_s_payload_is_what_the_real_writer_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet: None
) -> None:
    """The exhaustive tests trust ``_plan`` over a BytesIO. This runs the real
    ``StreamWriter`` — lock, seeks, write, fsync — on a real file at every depth-one cut and
    checks the bytes it leaves equal the model's, and that a disk that fills at every offset
    of the heal leaves exactly the model's prefix."""
    state0, acked0 = _first()
    p1 = _next(state0, acked0, _obs("second"))
    w = StreamWriter(tmp_path, "maya", "mbp")
    w.path.parent.mkdir(parents=True)
    for k1 in range(len(p1) + 1):
        s1 = state0 + p1[:k1]
        a1 = acked0 + ([(2, "second")] if k1 == len(p1) else [])
        w.path.write_bytes(s1)
        heal = _next(s1, a1, _obs("third"))
        assert w.append(_obs("third")).seq == a1[-1][0] + 1
        assert w.path.read_bytes() == s1 + heal
        _check(w.path.read_bytes(), [*a1, (a1[-1][0] + 1, "third")])

    torn = state0 + p1[: len(p1) // 2]
    heal = _next(torn, acked0, _obs("third"))
    for k2 in range(len(heal)):
        w.path.write_bytes(torn)
        _append_cut(monkeypatch, w, "third", at=k2)
        assert w.path.read_bytes() == torn + heal[:k2]
