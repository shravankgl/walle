"""Task 9's verify criterion, and the cases around it (D49, D60, D81, D86).

  staged, not hot               `walle remember` stages a proposal; the hot tier does not carry it
  approved, then hot            `walle review` keep turns it into an assertion the render carries
  edit                          the assertion carries the edited text and cites the proposal
  drop is a tombstone           the proposal stays in the log, byte for byte; a supersede shadows it
  decided once                  a decided proposal is never offered again, even to a second review
  two reviewers                 one that waited on another finds the item decided, writes nothing
  summary                       every proposal has one line, written when it is proposed (D81)

Every event here is synthetic: a made-up person's dinners and plants, no real data.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import staging
from walle.cli import main
from walle.events import ACTOR_USER, Assertion, Proposal, Supersede
from walle.hot import LEARNED, render
from walle.index import load_records, rebuild, update
from walle.paths import hot_dir, staging_lock_path, stream_path
from walle.reader import Record, read_stream
from walle.resolver import resolve, staged

P = "maya"
D = "mbp"
ROOT_ARGS = ("--principal", P)


def _cli(root: Path, *args: str, input: str | None = None) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, [args[0], "--root", str(root), *args[1:]], input=input)
    return result.exit_code, result.stdout, result.stderr


def _remember(root: Path, text: str, *extra: str) -> str:
    code, out, err = _cli(root, "remember", *ROOT_ARGS, "--device", D, text, *extra)
    assert code == 0, out + err
    return staging.pending(root, P)[-1].ref


def _records(root: Path) -> tuple[Record, ...]:
    """The log as it is now. ``load_records`` alone is whatever the index last saw."""
    return update(root, P).records


def _learned(root: Path) -> str:
    render(root, P)
    return (hot_dir(root, P) / LEARNED).read_text(encoding="utf-8")


def _review(root: Path, answers: str) -> tuple[int, str, str]:
    return _cli(root, "review", *ROOT_ARGS, "--device", D, input=answers)


# --- the verify criterion ---------------------------------------------------------------


def test_a_staged_item_is_absent_from_the_hot_tier_until_approved(tmp_path: Path) -> None:
    """D49: nothing reaches every future context without a human yes. D86: `remember` only
    stages, and the hot tier is rebuilt only after review."""
    ref = _remember(tmp_path, "Maya keeps the basil on the east windowsill.")

    learned = _learned(tmp_path)
    assert "basil" not in learned
    assert resolve(_records(tmp_path)).current == ()
    assert [s.ref for s in staging.pending(tmp_path, P)] == [ref]

    code, out, err = _review(tmp_path, "k\n")
    assert code == 0, out + err

    learned = _learned(tmp_path)
    assert "Maya keeps the basil on the east windowsill." in learned
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.caused_by == [ref]
    assert belief.assertion.actor == ACTOR_USER
    assert staging.pending(tmp_path, P) == ()


def test_a_drop_leaves_a_tombstone_not_a_deletion(tmp_path: Path) -> None:
    """Hard constraint 2: the dropped proposal is still in the log, unchanged, at its ref, and
    a `supersede` is what hides it. The file only ever grew."""
    ref = _remember(tmp_path, "Maya dislikes coriander.")
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()

    code, out, err = _review(tmp_path, "d\n")
    assert code == 0, out + err

    after = log.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    records = {r.ref: r.event for r in read_stream(tmp_path, P, D)}
    proposal = records[ref]
    assert isinstance(proposal, Proposal) and proposal.body == "Maya dislikes coriander."
    (tombstone,) = [e for e in records.values() if isinstance(e, Supersede)]
    assert tombstone.target == ref and tombstone.actor == ACTOR_USER
    assert not any(isinstance(e, Assertion) for e in records.values())

    assert staging.pending(tmp_path, P) == ()
    assert "coriander" not in _learned(tmp_path)
    assert f"dropped {ref}" in out and "stays in the log" in out


# --- review ------------------------------------------------------------------------------


def test_edit_writes_the_edited_text_and_cites_the_proposal(tmp_path: Path) -> None:
    ref = _remember(tmp_path, "Maya waters the fern on Sundays.")
    code, out, err = _review(
        tmp_path,
        "e\nwaters the fern twice a week\nMaya waters the fern on Sundays and Wednesdays.\n",
    )
    assert code == 0, out + err

    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.summary == "waters the fern twice a week"
    assert belief.assertion.body == "Maya waters the fern on Sundays and Wednesdays."
    assert belief.assertion.caused_by == [ref]
    # The proposal's own text is still in the log: an edit is a new event, not a rewrite.
    proposal = {r.ref: r.event for r in read_stream(tmp_path, P, D)}[ref]
    assert isinstance(proposal, Proposal) and proposal.body == "Maya waters the fern on Sundays."


def test_edit_keeps_what_the_reviewer_leaves_blank(tmp_path: Path) -> None:
    _remember(tmp_path, "Maya's dinner is at eight.", "--summary", "dinner at eight")
    code, out, err = _review(tmp_path, "e\n\n\n")
    assert code == 0, out + err
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.summary == "dinner at eight"
    assert belief.assertion.body == "Maya's dinner is at eight."


def test_an_edit_to_an_empty_or_overlong_summary_is_asked_again(tmp_path: Path) -> None:
    _remember(tmp_path, "Maya's dinner is at eight.")
    answers = "e\n" + "x" * 121 + "\ndinner at eight\n\n"
    code, out, err = _review(tmp_path, answers)
    assert code == 0, out + err
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.summary == "dinner at eight"


def test_keep_carries_the_proposal_fields_and_starts_at_d60_confidence(tmp_path: Path) -> None:
    staged_item = staging.remember(
        tmp_path,
        P,
        D,
        "Maya moved to Pune in March.",
        summary="lives in Pune",
        valid_from="2026-03-01",
    )
    new_ref = staging.keep(tmp_path, P, D, staged_item.ref)
    event = {r.ref: r.event for r in read_stream(tmp_path, P, D)}[new_ref]
    assert isinstance(event, Assertion)
    assert (event.body, event.summary, event.valid_from) == (
        "Maya moved to Pune in March.",
        "lives in Pune",
        "2026-03-01",
    )
    assert event.confidence == 0.3 and event.evidence_count == 1  # D60: new is 0.3


def test_skip_leaves_it_staged_and_quit_stops_the_batch(tmp_path: Path) -> None:
    first = _remember(tmp_path, "Maya likes rasam.")
    second = _remember(tmp_path, "Maya's plants are on the balcony.")
    code, out, err = _review(tmp_path, "s\nq\n")
    assert code == 0, out + err
    assert [s.ref for s in staging.pending(tmp_path, P)] == [first, second]
    assert "skipped 1" in out


def test_input_ending_mid_batch_reports_what_was_written(tmp_path: Path) -> None:
    """A review cut off part-way must say what is already in the log, not just "Aborted"."""
    first = _remember(tmp_path, "Maya likes rasam.")
    second = _remember(tmp_path, "Maya likes sambar.")
    code, out, _ = _review(tmp_path, "k\n")
    assert code != 0
    assert "kept 1, edited 0, dropped 0, skipped 0, 1 not reached" in out
    assert [s.ref for s in staging.pending(tmp_path, P)] == [second]
    assert [b.assertion.caused_by for b in resolve(_records(tmp_path)).current] == [[first]]


def test_a_batch_reviews_every_item_in_order(tmp_path: Path) -> None:
    refs = [_remember(tmp_path, f"Maya fact number {n}.") for n in range(3)]
    code, out, err = _review(tmp_path, "k\nd\nk\n")
    assert code == 0, out + err
    assert out.index(refs[0]) < out.index(refs[1]) < out.index(refs[2])
    current = resolve(_records(tmp_path)).current
    assert [b.assertion.caused_by for b in current] == [[refs[0]], [refs[2]]]
    assert "kept 2, edited 0, dropped 1, skipped 0" in out


def test_a_decided_proposal_is_never_offered_again(tmp_path: Path) -> None:
    _remember(tmp_path, "Maya likes rasam.")
    _review(tmp_path, "k\n")
    code, out, _ = _review(tmp_path, "")
    assert code == 0
    assert "nothing staged" in out


def test_deciding_a_decided_proposal_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    ref = staging.remember(tmp_path, P, D, "Maya likes rasam.").ref
    kept = staging.keep(tmp_path, P, D, ref)
    size = stream_path(tmp_path, P, D).stat().st_size
    with pytest.raises(staging.NotStagedError, match=f"already kept as {kept}"):
        staging.drop(tmp_path, P, D, ref)
    with pytest.raises(staging.NotStagedError, match=f"already kept as {kept}"):
        staging.keep(tmp_path, P, D, ref)
    assert stream_path(tmp_path, P, D).stat().st_size == size


def test_deciding_something_that_is_not_a_proposal_is_refused(tmp_path: Path) -> None:
    ref = staging.remember(tmp_path, P, D, "Maya likes rasam.").ref
    kept = staging.keep(tmp_path, P, D, ref)
    with pytest.raises(staging.NotStagedError, match="not a proposal"):
        staging.drop(tmp_path, P, D, kept)
    with pytest.raises(staging.NotStagedError, match="no event"):
        staging.drop(tmp_path, P, D, "d-mbp:99")


def test_a_dropped_proposal_is_refused_too(tmp_path: Path) -> None:
    ref = staging.remember(tmp_path, P, D, "Maya likes rasam.").ref
    tomb = staging.drop(tmp_path, P, D, ref)
    with pytest.raises(staging.NotStagedError, match=f"already dropped by {tomb}"):
        staging.keep(tmp_path, P, D, ref)


def test_a_review_on_another_device_cites_across_streams(tmp_path: Path) -> None:
    ref = staging.remember(tmp_path, P, "phone", "Maya likes rasam.").ref
    kept = staging.keep(tmp_path, P, D, ref)
    assert kept.startswith("d-mbp:") and ref.startswith("d-phone:")
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.caused_by == [ref]


def test_staging_survives_a_rebuild_from_nothing(tmp_path: Path) -> None:
    """The queue is a fold over the log (hard constraint 5), not a file of its own."""
    a = staging.remember(tmp_path, P, D, "Maya likes rasam.").ref
    b = staging.remember(tmp_path, P, D, "Maya likes sambar.").ref
    staging.remember(tmp_path, P, D, "Maya likes idli.")
    staging.keep(tmp_path, P, D, a)
    staging.drop(tmp_path, P, D, b)
    before = staging.pending(tmp_path, P)
    rebuild(tmp_path, P)
    assert staged(load_records(tmp_path, P)) == before
    assert [s.proposal.body for s in before] == ["Maya likes idli."]


# --- two reviewers ----------------------------------------------------------------------

_HOLD_AND_KEEP = """
import fcntl, os, sys, time
from pathlib import Path
from walle import staging
from walle.events import ACTOR_USER, Assertion, now_instant
from walle.writer import StreamWriter

root, ref, lock = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(1.0)
StreamWriter(root, "maya", "phone").append(
    Assertion(ts=now_instant(), actor=ACTOR_USER, body="kept by A", summary="kept by A",
              caused_by=[ref])
)
os.close(fd)
"""


def test_a_second_reviewer_waits_and_finds_the_item_decided(tmp_path: Path) -> None:
    """Reviewer A holds staging.lock and keeps the item. Reviewer B, deciding the same item,
    waits for A, re-reads the log, and refuses: one proposal, one decision.

    Had B checked "still staged?" before waiting, it would wake holding a stale answer and
    append a drop next to A's keep."""
    ref = staging.remember(tmp_path, P, D, "Maya likes rasam.").ref
    staging.pending(tmp_path, P)  # creates the index, so A's process is the only writer
    lock = staging_lock_path(tmp_path, P)
    lock.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    reviewer_a = subprocess.Popen(
        [sys.executable, "-c", _HOLD_AND_KEEP, str(tmp_path), ref, str(lock)],
        stdout=subprocess.PIPE,
        env=env,
    )
    try:
        assert reviewer_a.stdout is not None
        assert reviewer_a.stdout.readline().strip() == b"held"
        started = time.monotonic()
        with pytest.raises(staging.NotStagedError, match="already kept as d-phone:1"):
            staging.drop(tmp_path, P, D, ref)
        waited = time.monotonic() - started
    finally:
        reviewer_a.wait(timeout=10)
    assert waited >= 0.5
    assert not any(isinstance(r.event, Supersede) for r in read_stream(tmp_path, P, D))


# --- remember ---------------------------------------------------------------------------


def test_remember_reports_the_ref_and_says_it_is_staged(tmp_path: Path) -> None:
    code, out, err = _cli(tmp_path, "remember", *ROOT_ARGS, "--device", D, "Maya likes rasam.")
    assert code == 0, out + err
    assert "staged d-mbp:1" in out
    assert "walle review" in out


def test_every_proposal_carries_a_summary_from_the_start(tmp_path: Path) -> None:
    """D81: the summary is written when the item is proposed, not derived later."""
    text = "Maya   keeps\tthe basil\non the east windowsill.\nShe moved it in May."
    item = staging.remember(tmp_path, P, D, text)
    assert item.proposal.summary == "Maya keeps the basil"
    assert item.proposal.body == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Maya likes rasam.", "Maya likes rasam."),
        ("\n\n  first line  \nsecond", "first line"),
        ("word " * 40, ("word " * 40)[:119].rstrip() + "…"),
    ],
)
def test_the_default_summary_is_the_first_line_at_most_120(text: str, expected: str) -> None:
    summary = staging.summarise(text)
    assert summary == expected
    assert len(summary) <= 120 and "\n" not in summary


def test_a_given_summary_is_used_and_kept_to_one_line(tmp_path: Path) -> None:
    item = staging.remember(tmp_path, P, D, "Maya likes rasam.", summary="likes\nrasam")
    assert item.proposal.summary == "likes rasam"


@pytest.mark.parametrize(
    ("text", "extra", "message"),
    [
        ("", (), "nothing to remember"),
        ("   \n\t", (), "nothing to remember"),
        ("Maya likes rasam.", ("--summary", "x" * 121), "120"),
        ("Maya likes rasam.", ("--summary", "  "), "summary"),
    ],
)
def test_bad_input_is_refused_and_writes_nothing(
    tmp_path: Path, text: str, extra: tuple[str, ...], message: str
) -> None:
    code, out, err = _cli(tmp_path, "remember", *ROOT_ARGS, "--device", D, text, *extra)
    assert code != 0
    assert message in out + err
    assert not stream_path(tmp_path, P, D).exists()


def test_an_unsafe_device_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    code, out, err = _cli(tmp_path, "remember", *ROOT_ARGS, "--device", "../x", "Maya likes rasam.")
    assert code != 0 and "device" in out + err
    assert not (tmp_path / "principals").exists()


def test_review_of_a_principal_with_no_log_is_an_error_not_an_empty_queue(tmp_path: Path) -> None:
    code, out, err = _review(tmp_path, "")
    assert code != 0
    assert "no log" in out + err
