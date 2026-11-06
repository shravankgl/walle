"""Task 3's verify criteria, as tests (D69).

  contiguous seqs under contention   two *processes*, no gaps, no duplicates
  no torn line                       every line in the file parses afterwards
  traversal id rejected              a bad name never becomes a path (D85)
  torn tail healed, not truncated    an interrupted append is stepped over, not erased (D80)
  corrupt complete line is fatal     JSON with no usable seq: guessing the next seq risks
                                     reusing one; a non-JSON line is a fragment, stepped past

The race test spawns real subprocesses rather than threads. ``flock`` serialises between
*processes*; threads in one interpreter share a file description and would pass a test the
mechanism does not actually earn.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from walle.events import ACTOR_OBSERVER, ACTOR_USER, Assertion, Observation, now_instant
from walle.writer import CorruptLogError, StreamWriter, encode

# Each child appends `count` events to the same stream. Started together, so interpreter
# startup overlaps and the appends genuinely contend.
_CHILD = """
import os, sys
from pathlib import Path
from walle.events import Observation, now_instant, ACTOR_OBSERVER
from walle.writer import StreamWriter

root, principal, device, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
writer = StreamWriter(Path(root), principal, device)
for i in range(count):
    writer.append(Observation(
        ts=now_instant(), actor=ACTOR_OBSERVER, source="race",
        observed_at=now_instant(), body=f"{os.getpid()}:{i}",
    ))
"""


def _observation(body: str = "b") -> Observation:
    now = now_instant()
    return Observation(
        ts=now, actor=ACTOR_OBSERVER, source="transcript", observed_at=now, body=body
    )


def _lines(path: Path) -> list[bytes]:
    return [line for line in path.read_bytes().split(b"\n") if line.strip()]


# --- the ordinary path -------------------------------------------------------------------


def test_first_append_is_seq_one_and_creates_the_tree(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "mbp")
    written = writer.append(_observation())

    assert written.seq == 1
    assert writer.path == tmp_path / "principals" / "p-maya" / "log" / "d-mbp.jsonl"
    assert writer.path.exists()


def test_seqs_are_contiguous_within_one_writer(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "mbp")
    assert [writer.append(_observation(str(i))).seq for i in range(5)] == [1, 2, 3, 4, 5]


def test_a_fresh_writer_continues_the_stream(tmp_path: Path) -> None:
    """seq comes from the file, never from memory — the reason a cached one would be wrong
    the moment another process appended."""
    StreamWriter(tmp_path, "maya", "mbp").append(_observation())
    StreamWriter(tmp_path, "maya", "mbp").append(_observation())
    assert StreamWriter(tmp_path, "maya", "mbp").append(_observation()).seq == 3


def test_every_line_is_one_json_object(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "mbp")
    for i in range(3):
        writer.append(_observation(str(i)))

    raw = writer.path.read_bytes()
    assert raw.endswith(b"\n") and b"\n\n" not in raw
    assert [json.loads(line)["seq"] for line in _lines(writer.path)] == [1, 2, 3]


def test_streams_are_independent(tmp_path: Path) -> None:
    """seq is per-stream (D57): two devices both start at 1, which is exactly why a bare
    number is not a usable ref."""
    assert StreamWriter(tmp_path, "maya", "mbp").append(_observation()).seq == 1
    assert StreamWriter(tmp_path, "maya", "phone").append(_observation()).seq == 1
    assert StreamWriter(tmp_path, "dev", "mbp").append(_observation()).seq == 1


def test_the_file_keeps_text_readable(tmp_path: Path) -> None:
    """The file is for a person with `cat`; the hash form is the escaped one (D10)."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("भारत — the café is closed"))
    assert "भारत — the café is closed" in writer.path.read_text("utf-8")


def test_encoded_keys_are_sorted_and_seq_is_present() -> None:
    event = Assertion(ts=now_instant(), actor=ACTOR_USER, body="b", summary="s", seq=7)
    record = json.loads(encode(event))
    assert list(record) == sorted(record)
    assert record["seq"] == 7  # unlike canonical_json, which drops it


# --- contention --------------------------------------------------------------------------


def test_concurrent_writer_processes_produce_contiguous_seqs(tmp_path: Path) -> None:
    """The race test (coding rule 5). Four processes, twenty-five appends each.

    A duplicate seq is the failure that matters: not a lost append, but one ref naming two
    events for the rest of the log's life.
    """
    processes, per_child = 4, 25
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(tmp_path), "maya", "mbp", str(per_child)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(processes)
    ]
    for child in children:
        _, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr.decode()

    path = StreamWriter(tmp_path, "maya", "mbp").path
    records = [json.loads(line) for line in _lines(path)]  # raises if any line is torn

    total = processes * per_child
    assert [r["seq"] for r in records] == list(range(1, total + 1))
    assert len({r["body"] for r in records}) == total  # nothing lost, nothing written twice


# --- interrupted and corrupt files -------------------------------------------------------


def test_a_torn_tail_is_stepped_over_not_erased(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An interrupted append left bytes nobody acknowledged. They stay (hard constraint 2),
    the next record starts on its own line, and seq comes from the last *complete* one."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    first = writer.append(_observation("first"))
    fragment = b'{"seq":2,"ts":"2026-10-10T14:37:02+0'
    with open(writer.path, "ab") as f:
        f.write(fragment)

    with caplog.at_level(logging.WARNING):
        healed = writer.append(_observation("after the tear"))

    assert first.seq == 1 and healed.seq == 2  # the fragment never existed
    assert fragment in writer.path.read_bytes()  # ... but its bytes are still there
    assert "interrupted" in caplog.text

    lines = _lines(writer.path)
    assert len(lines) == 3
    assert json.loads(lines[2])["body"] == "after the tear"
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[1])  # the fragment — what D80's reader skips


def test_a_second_append_after_a_tear_stays_clean(tmp_path: Path) -> None:
    """One tear does not poison the stream: everything after it is ordinary.

    The readable lines are found by *attempting the parse*, not by looking at the first
    character — a truncated record still starts with `{`, so a prefix test would call the
    fragment a record and then choke on it. That is the rule task 4's reader inherits.
    """
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("first"))
    with open(writer.path, "ab") as f:
        f.write(b'{"seq":2,"ts":"tr')
    writer.append(_observation("second"))
    writer.append(_observation("third"))

    seqs = []
    for line in _lines(writer.path):
        try:
            seqs.append(json.loads(line)["seq"])
        except json.JSONDecodeError:
            continue  # the fragment, which the reader skips (D80)
    assert seqs == [1, 2, 3]


@pytest.mark.parametrize(
    "last_line",
    [b'{"ts":"2026-10-10T14:37:02+00:00"}', b'{"seq":"12"}', b'{"seq":0}', b"[1]"],
    ids=["no-seq", "seq-not-int", "seq-zero", "not-an-object"],
)
def test_a_corrupt_complete_line_refuses_rather_than_guesses(
    tmp_path: Path, last_line: bytes
) -> None:
    """A torn tail is an interrupted write; a complete line that is JSON but not a record is
    something else.

    The next seq comes from this line, so guessing risks reusing a number — which loses no
    event but makes one ref ambiguous forever. Loud beats tidy. The reader halts on the same
    line, so the writer refusing is the two agreeing.
    """
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation())
    with open(writer.path, "ab") as f:
        f.write(last_line + b"\n")

    with pytest.raises(CorruptLogError, match="seq"):
        writer.append(_observation())


def test_a_complete_line_that_is_not_json_is_stepped_past_as_the_reader_skips_it(
    tmp_path: Path,
) -> None:
    """Until issue #128 this case was ``unparseable`` in the test above, and the writer refused
    it. The reader skips a non-JSON line as a stepped-over fragment (D80), and a heal cut
    after its newline leaves one *last* — so a writer that refused it left a stream that
    loads and can never grow (finding 3.1). The writer now steps back past it to the last
    record, as the reader does."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation())
    with open(writer.path, "ab") as f:
        f.write(b"not json at all\n")

    assert writer.append(_observation("after")).seq == 2


def test_a_long_record_is_found_past_the_tail_window(tmp_path: Path) -> None:
    """The backward read doubles its window, so one record larger than it still works."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("x" * (70 * 1024)))
    assert writer.append(_observation("after the big one")).seq == 2


# --- names become paths ------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal,device",
    [
        ("../etc", "mbp"),
        ("maya", "../../mbp"),
        ("maya", "mb/p"),
        ("Maya", "mbp"),
        ("maya", "MBP"),
        ("", "mbp"),
        ("maya", ""),
        ("-maya", "mbp"),
        ("maya", "m" * 64),
    ],
    ids=[
        "traversal-principal",
        "traversal-device",
        "separator",
        "uppercase-principal",
        "uppercase-device",
        "empty-principal",
        "empty-device",
        "leading-dash",
        "too-long",
    ],
)
def test_bad_names_never_become_paths(tmp_path: Path, principal: str, device: str) -> None:
    """Rejected at construction, before anything is opened — a sanitized name would be a
    valid path somewhere unintended, an error message is not (D85)."""
    with pytest.raises(ValueError):
        StreamWriter(tmp_path, principal, device)


def test_a_failed_directory_fsync_leaves_no_record_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status that contradicts the file is the worst bug a log can have.

    The directory entry is made durable *before* the record is written, so a failure there
    means nothing was appended — which is what the exception then truthfully says. Written
    the other way round, the caller would be told the append failed while the record sat on
    disk, and a retry would append the same event twice.
    """
    import walle.writer as writer_mod

    def boom(_: Path) -> None:
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(writer_mod, "_fsync_dir", boom)
    writer = StreamWriter(tmp_path, "maya", "mbp")

    with pytest.raises(OSError, match="simulated"):
        writer.append(_observation("must not survive"))

    assert writer.path.read_bytes() == b""  # created, but nothing claimed to be in it


def test_the_first_record_fsyncs_the_directory_even_if_the_file_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newness is decided inside the lock, from the file's size — not from `exists()`.

    `open(..., "a+b")` creates the file *before* the lock is taken, so a pre-lock `exists()`
    races: writer A opens, writer B then sees the file and concludes it is not new, wins the
    lock, writes the first record and returns claiming durability on a directory entry nobody
    fsynced. An empty file left behind by another writer's `open()` is exactly that case.
    """
    import walle.writer as writer_mod

    calls: list[Path] = []
    monkeypatch.setattr(writer_mod, "_fsync_dir", lambda p: calls.append(p))

    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.path.parent.mkdir(parents=True, exist_ok=True)
    writer.path.touch()  # what another writer's open() leaves behind

    writer.append(_observation("the first record"))
    assert calls == [writer.path.parent]

    calls.clear()
    writer.append(_observation("the second"))
    assert calls == []  # already durable; not repeated on every append


# --- a locked session (task 11) ------------------------------------------------------------


def test_a_locked_session_reads_and_appends_under_one_lock(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path, "maya", "mbp")
    writer.append(_observation("before"))
    with writer.locked() as stream:
        assert [r.seq for r in stream.read()] == [1]
        appended = [stream.append(_observation(f"in {i}")) for i in range(3)]
        assert [r.seq for r in stream.read()] == [1, 2, 3, 4]
    assert [e.seq for e in appended] == [2, 3, 4]
    assert writer.append(_observation("after")).seq == 5


def test_a_locked_session_holds_off_another_writer(tmp_path: Path) -> None:
    """Another process's append waits until the session ends, and then follows it."""
    writer = StreamWriter(tmp_path, "maya", "mbp")
    with writer.locked() as stream:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(tmp_path), "maya", "mbp", "1"],
            stderr=subprocess.PIPE,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=1.5)  # blocked on the lock, not finished
        stream.append(_observation("mine"))
    _, err = child.communicate(timeout=60)
    assert child.returncode == 0, err.decode()
    first, second = (json.loads(line) for line in _lines(writer.path))
    assert (first["body"], first["seq"], second["seq"]) == ("mine", 1, 2)
