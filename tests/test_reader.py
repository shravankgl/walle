"""Task 4's verify criterion, and the cases around it (D80).

  torn tail                  a truncated last line loads N-1 events and reports exactly one skip
  healed fragment            a tear the writer stepped over is skipped and reported, mid-file
  unterminated but parseable an unacknowledged record is not an event, even if it parses
  corrupt is not torn        a complete line that is not a valid event halts, it is not skipped
  seq order                  seqs must rise; a file that goes backwards halts
  refs                       each record knows its own ref and its parsed causes

The fixture is synthetic (``tests/fixtures/torn_tail.jsonl``): five events written with the
real encoder, the fifth cut in half with no newline — what a crash mid-append leaves.
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
from pathlib import Path

import pytest

from walle.events import ACTOR_OBSERVER, FutureSchemaError, Observation, now_instant
from walle.reader import CorruptStreamError, StreamContents, read_file, read_stream
from walle.writer import StreamWriter, encode

FIXTURES = Path(__file__).parent / "fixtures"


def _observation(body: str = "b") -> Observation:
    now = now_instant()
    return Observation(
        ts=now, actor=ACTOR_OBSERVER, source="transcript", observed_at=now, body=body
    )


# --- the verify criterion ----------------------------------------------------------------


def test_a_truncated_last_line_loads_n_minus_one_events_and_reports_one_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = FIXTURES / "torn_tail.jsonl"
    written = path.read_bytes().count(b"\n") + 1  # N lines, the last one torn
    assert written == 5 and not path.read_bytes().endswith(b"\n")

    with caplog.at_level(logging.WARNING):
        contents = read_file(path, "d-mbp")

    assert len(contents) == written - 1
    assert [r.event.seq for r in contents] == [1, 2, 3, 4]
    assert len(contents.skips) == 1
    skip = contents.skips[0]
    assert skip.line == 5
    assert "interrupted" in skip.reason
    assert "torn_tail.jsonl:5" in caplog.text  # reported, not silent


# --- the ordinary path -------------------------------------------------------------------


def test_reads_back_what_the_writer_wrote(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "mbp")
    written = [writer.append(_observation(f"e{i}")) for i in range(3)]

    contents = read_stream(tmp_path, "maya", "mbp")

    assert contents.events == tuple(written)
    assert contents.skips == ()


def test_an_empty_stream_has_nothing_and_skips_nothing(tmp_path: Path) -> None:
    path = tmp_path / "d-mbp.jsonl"
    path.write_bytes(b"")
    contents = read_file(path, "d-mbp")
    assert contents.records == () and contents.skips == ()


def test_a_missing_stream_is_an_error_not_an_empty_one(tmp_path: Path) -> None:
    """A typo in a principal name must not read as "this person has no history"."""
    with pytest.raises(FileNotFoundError):
        read_stream(tmp_path, "maya", "mbp")


def test_a_traversal_name_never_becomes_a_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_stream(tmp_path, "../maya", "mbp")
    with pytest.raises(ValueError):
        read_file(tmp_path / "x.jsonl", "d-../x")


def test_reading_never_changes_the_file() -> None:
    path = FIXTURES / "torn_tail.jsonl"
    before = path.read_bytes()
    read_file(path, "d-mbp")
    assert path.read_bytes() == before  # append, never mutate — the reader does not heal


# --- refs --------------------------------------------------------------------------------


def test_each_record_carries_its_ref_and_parsed_causes() -> None:
    contents = read_file(FIXTURES / "torn_tail.jsonl", "d-mbp")
    supersede = contents.records[3]
    assert supersede.ref == "d-mbp:4"
    assert supersede.line == 4
    assert supersede.causes == (("d-mbp", 2), ("d-mbp", 3))
    assert contents.records[0].causes == ()


def test_a_ref_belongs_to_the_stream_it_was_read_from(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "phone")
    writer.append(_observation())
    assert read_stream(tmp_path, "maya", "phone").records[0].ref == "d-phone:1"


# --- tears the writer has already stepped over -------------------------------------------


def test_a_healed_fragment_mid_file_is_skipped_and_reported(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("first"))
    with open(writer.path, "ab") as f:
        f.write(b'{"seq":2,"ts":"tr')  # a crash mid-append
    writer.append(_observation("second"))  # steps over it on a fresh line
    writer.append(_observation("third"))

    contents = read_stream(tmp_path, "maya", "mbp")

    assert [r.event.seq for r in contents] == [1, 2, 3]
    assert [r.line for r in contents] == [1, 3, 4]
    assert len(contents.skips) == 1 and contents.skips[0].line == 2


def test_a_tail_that_parses_but_has_no_newline_is_still_torn(tmp_path: Path) -> None:
    """The write stopped one byte short. The writer never acknowledged it and will give the
    next append the same seq, so it is not an event — however complete it looks."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("acknowledged"))
    unacked = _observation("never acknowledged").model_copy(update={"seq": 2})
    with open(writer.path, "ab") as f:
        f.write(encode(unacked).rstrip(b"\n"))

    contents = read_stream(tmp_path, "maya", "mbp")

    assert [r.event.seq for r in contents] == [1]
    assert len(contents.skips) == 1 and contents.skips[0].line == 2


def test_an_unacknowledged_record_does_not_share_a_ref_after_healing(tmp_path: Path) -> None:
    """After the writer heals the tail above, the unacknowledged line is newline-terminated
    and the next append reuses its seq. Two events must never answer to one ref: the later,
    acknowledged one is kept and the earlier is reported as a skip."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("acknowledged"))
    unacked = _observation("never acknowledged").model_copy(update={"seq": 2})
    with open(writer.path, "ab") as f:
        f.write(encode(unacked).rstrip(b"\n"))
    healed = writer.append(_observation("the real seq 2"))
    assert healed.seq == 2

    contents = read_stream(tmp_path, "maya", "mbp")

    assert [r.ref for r in contents] == ["d-mbp:1", "d-mbp:2"]
    assert contents.records[1].event == healed
    assert len(contents.skips) == 1 and contents.skips[0].line == 2


def test_a_tear_that_splits_a_multibyte_character_is_still_just_a_tear(tmp_path: Path) -> None:
    path = tmp_path / "d-mbp.jsonl"
    good = encode(_observation("first").model_copy(update={"seq": 1}))
    torn = encode(_observation("café ☕").model_copy(update={"seq": 2}))
    cut = torn.index("☕".encode()) + 1  # one byte into a three-byte character
    path.write_bytes(good + torn[:cut])

    contents = read_file(path, "d-mbp")

    assert len(contents) == 1 and len(contents.skips) == 1


# --- corruption is not a torn tail -------------------------------------------------------


def _write(path: Path, *records: dict[str, object]) -> None:
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records))


def _record(seq: int, **extra: object) -> dict[str, object]:
    now = now_instant()
    return {
        "type": "observation",
        "seq": seq,
        "ts": now,
        "actor": "observer",
        "source": "transcript",
        "observed_at": now,
        "body": "b",
        **extra,
    }


def test_a_complete_line_that_is_not_a_valid_event_halts(tmp_path: Path) -> None:
    """Skipping it would silently drop a real record; that is data loss, not tolerance."""
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), _record(2, surprise="field"), _record(3))
    with pytest.raises(CorruptStreamError, match=r"d-mbp\.jsonl:2"):
        read_file(path, "d-mbp")


def test_a_record_without_a_seq_halts(tmp_path: Path) -> None:
    path = tmp_path / "d-mbp.jsonl"
    record = _record(1)
    del record["seq"]
    _write(path, record)
    with pytest.raises(CorruptStreamError, match="seq"):
        read_file(path, "d-mbp")


def test_seqs_that_go_backwards_halt(tmp_path: Path) -> None:
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), _record(3), _record(2))
    with pytest.raises(CorruptStreamError, match="order"):
        read_file(path, "d-mbp")


def test_a_repeated_seq_that_is_not_adjacent_halts(tmp_path: Path) -> None:
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), _record(2), _record(3), _record(2))
    with pytest.raises(CorruptStreamError):
        read_file(path, "d-mbp")


def test_a_gap_in_seq_is_allowed(tmp_path: Path) -> None:
    """A process can die after flush and before fsync; the lost event was never referenced,
    so a gap leaves every ref unambiguous (DEVLOG, 2026-10-11)."""
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), _record(3))
    assert [r.event.seq for r in read_file(path, "d-mbp")] == [1, 3]


def test_an_event_from_a_newer_build_halts_by_name(tmp_path: Path) -> None:
    """D111: not skippable, and not folded into a generic corruption error."""
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), _record(2, v=99))
    with pytest.raises(FutureSchemaError, match="v99"):
        read_file(path, "d-mbp")


def test_a_repeated_seq_with_a_line_between_halts(tmp_path: Path) -> None:
    """The writer's heal puts the reused seq on the very next line; anything else is not
    an interrupted append, so the earlier record is not quietly dropped."""
    path = tmp_path / "d-mbp.jsonl"
    path.write_bytes(
        json.dumps(_record(1)).encode()
        + b"\n"
        + b'{"seq":2,"ts":"tr\n'
        + json.dumps(_record(1)).encode()
        + b"\n"
    )
    with pytest.raises(CorruptStreamError, match="repeats line 1"):
        read_file(path, "d-mbp")


# --- seq is an integer in range, or the line is corrupt (issue #20) ----------------------


@pytest.mark.parametrize(
    "seq",
    ["2", True, 2.0, 10_000_000_000_000_000_000],
    ids=["string", "bool", "float", "out-of-range"],
)
def test_a_seq_that_is_not_a_json_integer_in_range_halts_naming_the_line(
    tmp_path: Path, seq: object
) -> None:
    """The same rule as the writer's ``_seq_of``: an int, not a bool, at least 1. Pydantic's
    lax mode would coerce the first three, and the fourth overflows the ref format; each is
    a complete line that is not a valid event, so it halts with ``file:line``. ``true`` is
    the sharpest case: coerced to 1, it would make line 1 look like an unacknowledged
    duplicate and silently drop it."""
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), {**_record(2), "seq": seq}, _record(3))
    with pytest.raises(CorruptStreamError, match=r"d-mbp\.jsonl:2: .*seq"):
        read_file(path, "d-mbp")


def test_the_largest_seq_a_ref_can_name_is_still_read(tmp_path: Path) -> None:
    """The range edge: 19 digits is the most a ref carries, and it is not corrupt."""
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(9_999_999_999_999_999_999))
    assert [r.ref for r in read_file(path, "d-mbp")] == ["d-mbp:9999999999999999999"]


def test_an_event_from_a_newer_build_still_halts_by_name_whatever_its_seq(
    tmp_path: Path,
) -> None:
    """D111 keeps priority: a newer schema is named as such, not reported as a bad seq."""
    path = tmp_path / "d-mbp.jsonl"
    _write(path, _record(1), {**_record(2, v=99), "seq": "2"})
    with pytest.raises(FutureSchemaError, match="v99"):
        read_file(path, "d-mbp")


# --- where a skip says it is -------------------------------------------------------------


def test_each_skip_reports_the_byte_offset_and_preview_of_its_line(tmp_path: Path) -> None:
    """All three skip branches in one synthetic file, with offsets worked out from the bytes
    written rather than from the reader's own arithmetic:

      line 1  seq 1                    kept
      line 2  a mid-file fragment      skipped: not JSON
      line 3  seq 2, unacknowledged    skipped: line 4 reuses its seq
      line 4  seq 2                    kept
      line 5  seq 3, cut, no newline   skipped: torn tail
    """
    path = tmp_path / "d-mbp.jsonl"
    first = encode(_observation("first").model_copy(update={"seq": 1}))
    fragment = b'{"seq":2,"ts":"tr\n'
    unacked = encode(_observation("never acknowledged " * 4).model_copy(update={"seq": 2}))
    acked = encode(_observation("the real seq 2").model_copy(update={"seq": 2}))
    torn = encode(_observation("torn " * 30).model_copy(update={"seq": 3}))[:100]
    lines = [first, fragment, unacked, acked, torn]
    data = b"".join(lines)
    path.write_bytes(data)
    starts = [sum(len(line) for line in lines[:i]) for i in range(len(lines))]
    assert len(unacked) > 80 and len(torn) > 80  # so the previews really are cut

    contents = read_file(path, "d-mbp")

    assert [r.line for r in contents] == [1, 4]
    assert [s.line for s in contents.skips] == [2, 3, 5]
    assert [s.offset for s in contents.skips] == [starts[1], starts[2], starts[4]]
    assert (
        [s.preview for s in contents.skips]
        == [
            '{"seq":2,"ts":"tr',  # the whole fragment, without its newline
            unacked[:80].decode(),
            torn[:80].decode(),
        ]
    )
    for skip in contents.skips:  # each offset lands on the first byte of its preview
        assert data[skip.offset :].startswith(skip.preview.encode())


def test_a_preview_cut_inside_a_multibyte_character_is_replaced_not_an_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "d-mbp.jsonl"
    good = encode(_observation("first").model_copy(update={"seq": 1}))
    torn = encode(_observation("☕☕☕").model_copy(update={"seq": 2}))
    cut = torn.index("☕".encode()) + 1
    path.write_bytes(good + torn[:cut])

    (skip,) = read_file(path, "d-mbp").skips

    assert skip.offset == len(good)
    assert cut < 80  # the whole torn line is the preview, ending mid-character
    assert skip.preview == torn[:cut].decode("utf-8", "replace")
    assert skip.preview.endswith("\ufffd")


def test_a_unicode_line_separator_inside_a_body_does_not_split_the_record(
    tmp_path: Path,
) -> None:
    """The file keeps text unescaped (ensure_ascii=False), so U+2028 sits raw in a line.
    Only b"\\n" ends a record."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    written = writer.append(_observation("one\u2028two\u0085three"))
    contents = read_stream(tmp_path, "maya", "mbp")
    assert contents.events == (written,) and contents.skips == ()


def test_a_read_waits_for_an_append_in_progress(tmp_path: Path) -> None:
    """The two-runner walkthrough's step 2: a writer holding LOCK_EX keeps the reader out,
    so a half-written append is never reported as an interrupted one."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("first"))
    second = encode(_observation("second").model_copy(update={"seq": 2}))
    result: list[StreamContents] = []

    with open(writer.path, "ab") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(second[:20])  # half an append, lock held
        f.flush()
        reader = threading.Thread(
            target=lambda: result.append(read_stream(tmp_path, "maya", "mbp"))
        )
        reader.start()
        reader.join(timeout=0.3)
        assert reader.is_alive()  # blocked on the lock, has read nothing
        f.write(second[20:])
        f.flush()
    reader.join(timeout=5)

    assert [r.seq for r in result[0]] == [1, 2]
    assert result[0].skips == ()
