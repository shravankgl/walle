"""Task 5's verify criterion, and the cases around it (D8, D58, D93).

  supersede hides            a superseded assertion is not current; its retraction keeps valid_to
  non-overlapping windows    two assertions whose world-time windows do not overlap never conflict
  overlapping, later-valid   the later-valid assertion closes the earlier one's window at its start
  overlapping, neither later both stay current and the contradiction is reported open, not guessed
  supersede of a supersede   the tombstone is retracted, so its target is current again
  reported, not silent       a supersede or contradiction the fold cannot apply is listed
  pure                       no IO, no clock, no model; input order does not change the answer

Every event here is synthetic: a made-up person's dinners and moves, no real data.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from walle.events import (
    ACTOR_CURATOR,
    ACTOR_USER,
    AnyEvent,
    Assertion,
    Contradiction,
    Observation,
    Proposal,
    Supersede,
)
from walle.reader import Record, read_stream
from walle.resolver import (
    Resolution,
    ResolutionError,
    Window,
    resolve,
    staged,
    starts_later,
    windows_overlap,
)
from walle.writer import StreamWriter

TS = "2026-11-04T09:00:00.000000+00:00"
RESOLVER_SOURCE = Path(__file__).parent.parent / "src" / "walle" / "resolver.py"


def _assertion(
    summary: str, valid_from: str | None = None, valid_to: str | None = None
) -> Assertion:
    return Assertion(
        ts=TS,
        actor=ACTOR_USER,
        body=summary,
        summary=summary,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _supersede(target: str, valid_to: str | None = None) -> Supersede:
    return Supersede(ts=TS, actor=ACTOR_USER, target=target, valid_to=valid_to)


def _contradiction(*refs: str) -> Contradiction:
    return Contradiction(ts=TS, actor=ACTOR_CURATOR, caused_by=list(refs))


def _stream(*events: AnyEvent, device: str = "mbp") -> list[Record]:
    """Records as ``reader`` would hand them over: seq from 1, refs ``d-<device>:<seq>``."""
    out: list[Record] = []
    for seq, event in enumerate(events, start=1):
        stored = event.model_copy(update={"seq": seq})
        out.append(Record(ref=f"d-{device}:{seq}", seq=seq, event=stored, line=seq))
    return out


def _current(resolution: Resolution) -> list[str]:
    return [belief.ref for belief in resolution.current]


# --- the verify criterion ----------------------------------------------------------------


def test_supersede_hides() -> None:
    records = _stream(
        _assertion("Lives in Pune"),
        _assertion("Cooks vegetarian on weekdays"),
        _supersede("d-mbp:1", valid_to="2026-03-01"),
    )

    resolution = resolve(records)

    assert _current(resolution) == ["d-mbp:2"]
    assert [r.ref for r in resolution.retired] == ["d-mbp:1"]
    retired = resolution.retired[0]
    assert retired.by == ("d-mbp:3",)
    assert retired.valid_to == "2026-03-01"  # the retraction keeps its world time (D93)
    assert resolution.conflicts == ()
    assert resolution.unapplied == ()


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # touching: one ends exactly where the other starts (half-open windows)
        (("2025-01-01", "2026-03-01"), ("2026-03-01", None)),
        # a gap between them
        (("2024-01-01", "2024-06-01"), ("2025-01-01", "2025-06-01")),
        # open start on the earlier one
        ((None, "2026-03-01"), ("2026-03-01", None)),
        # instants in different offsets that touch in UTC
        (
            ("2026-01-01T00:00:00+00:00", "2026-03-01T05:30:00+05:30"),
            ("2026-03-01T00:00:00+00:00", None),
        ),
        # mixed precision, a different day
        ((None, "2026-02-27"), ("2026-03-01T09:00:00+05:30", None)),
    ],
)
def test_non_overlapping_windows_never_conflict(
    a: tuple[str | None, str | None], b: tuple[str | None, str | None]
) -> None:
    assert windows_overlap(Window(*a), Window(*b)) is False
    assert windows_overlap(Window(*b), Window(*a)) is False

    records = _stream(
        _assertion("Lives in Pune", *a),
        _assertion("Lives in Bangalore", *b),
        _contradiction("d-mbp:1", "d-mbp:2"),
    )
    resolution = resolve(records)

    assert resolution.conflicts == ()
    assert _current(resolution) == ["d-mbp:1", "d-mbp:2"]
    # nothing was closed: each keeps the window it was written with
    for belief, written in zip(resolution.current, (a, b), strict=True):
        assert belief.window == Window(*written)
        assert belief.closed_by == ()


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        (("2025-01-01", None), ("2026-03-01", None)),
        ((None, None), ("2026-03-01", None)),  # an open start is earlier than any start
        (("2025-01-01", "2026-12-31"), ("2026-03-01", "2027-01-01")),
        (("2026-01-01T00:00:00+00:00", None), ("2026-03-01T09:00:00+05:30", None)),
    ],
)
def test_overlapping_later_valid_closes_earlier(
    earlier: tuple[str | None, str | None], later: tuple[str | None, str | None]
) -> None:
    assert windows_overlap(Window(*earlier), Window(*later)) is True
    assert starts_later(Window(*later), Window(*earlier)) is True

    # written in both orders: which one the log learned first must not matter (D93)
    for first, second in ((earlier, later), (later, earlier)):
        records = _stream(
            _assertion("first", *first),
            _assertion("second", *second),
            _contradiction("d-mbp:1", "d-mbp:2"),
        )
        resolution = resolve(records)
        old_ref, new_ref = ("d-mbp:1", "d-mbp:2") if first == earlier else ("d-mbp:2", "d-mbp:1")

        assert resolution.conflicts == ()
        old, new = resolution.belief(old_ref), resolution.belief(new_ref)
        assert old.window == Window(earlier[0], later[0])  # closed at the later fact's start
        assert old.closed_by == (new_ref,)
        assert new.window == Window(*later)  # the later fact is untouched
        assert new.closed_by == ()
        assert old.assertion.valid_to == earlier[1]  # the event itself is not rewritten


# --- overlap without an order ----------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ((None, None), (None, None)),
        (("2026-03-01", None), ("2026-03-01", "2026-09-01")),
        # same day, one a date and one an instant: which started first is not knowable
        (("2026-03-01", None), ("2026-03-01T09:00:00+05:30", None)),
    ],
)
def test_overlapping_with_no_later_start_is_reported_open(
    a: tuple[str | None, str | None], b: tuple[str | None, str | None]
) -> None:
    records = _stream(
        _assertion("Eats fish", *a),
        _assertion("Vegetarian", *b),
        _contradiction("d-mbp:2", "d-mbp:1"),
    )
    resolution = resolve(records)

    assert _current(resolution) == ["d-mbp:1", "d-mbp:2"]  # both presented, neither closed
    assert all(b.closed_by == () for b in resolution.current)
    assert len(resolution.conflicts) == 1
    conflict = resolution.conflicts[0]
    assert conflict.ref == "d-mbp:3"
    assert conflict.between == ("d-mbp:1", "d-mbp:2")


def test_an_uncertain_overlap_is_reported_not_treated_as_disjoint() -> None:
    # A ends on 2026-03-01 (a date), B starts during that day (an instant): they may overlap.
    a, b = Window("2025-01-01", "2026-03-01"), Window("2026-03-01T09:00:00+05:30", None)
    assert windows_overlap(a, b) is None

    records = _stream(
        _assertion("Lives in Pune", a.valid_from, a.valid_to),
        _assertion("Lives in Bangalore", b.valid_from, b.valid_to),
        _contradiction("d-mbp:1", "d-mbp:2"),
    )
    resolution = resolve(records)
    assert [c.ref for c in resolution.conflicts] == ["d-mbp:3"]
    assert all(belief.closed_by == () for belief in resolution.current)


def test_without_a_contradiction_event_overlap_alone_is_not_a_conflict() -> None:
    # Whether two assertions are about the same thing is the Curator's judgment, recorded as
    # a contradiction event (D58). The fold never guesses it from text (constraint 3).
    records = _stream(_assertion("Likes tea", None, None), _assertion("Likes coffee", None, None))
    resolution = resolve(records)
    assert resolution.conflicts == ()
    assert _current(resolution) == ["d-mbp:1", "d-mbp:2"]


def test_the_earliest_closer_wins() -> None:
    records = _stream(
        _assertion("Lives in Pune", "2020-01-01"),
        _assertion("Lives in Mumbai", "2024-01-01"),
        _assertion("Lives in Bangalore", "2026-03-01"),
        _contradiction("d-mbp:1", "d-mbp:3"),
        _contradiction("d-mbp:1", "d-mbp:2"),
        _contradiction("d-mbp:2", "d-mbp:3"),
    )
    resolution = resolve(records)

    pune, mumbai, bangalore = (resolution.belief(f"d-mbp:{n}") for n in (1, 2, 3))
    assert pune.window == Window("2020-01-01", "2024-01-01")
    assert pune.closed_by == ("d-mbp:2", "d-mbp:3")
    assert mumbai.window == Window("2024-01-01", "2026-03-01")
    assert bangalore.window == Window("2026-03-01", None)
    assert resolution.conflicts == ()


# --- supersede and contradiction together ----------------------------------------------


def test_a_superseded_contradiction_is_dismissed() -> None:
    records = _stream(
        _assertion("Eats fish"),
        _assertion("Vegetarian"),
        _contradiction("d-mbp:1", "d-mbp:2"),
        _supersede("d-mbp:3"),  # a person said: these do not conflict
    )
    resolution = resolve(records)
    assert resolution.conflicts == ()
    assert _current(resolution) == ["d-mbp:1", "d-mbp:2"]


def test_a_contradiction_with_a_superseded_side_is_moot() -> None:
    records = _stream(
        _assertion("Lives in Pune", "2020-01-01"),
        _assertion("Lives in Bangalore", "2026-03-01"),
        _contradiction("d-mbp:1", "d-mbp:2"),
        _supersede("d-mbp:2"),  # Bangalore was wrong
    )
    resolution = resolve(records)
    pune = resolution.belief("d-mbp:1")
    assert pune.window == Window("2020-01-01", None)  # no longer closed by a retracted fact
    assert pune.closed_by == ()
    assert resolution.conflicts == ()
    assert resolution.unapplied == ()


def test_superseding_a_supersede_brings_its_target_back() -> None:
    records = _stream(
        _assertion("Cooks vegetarian on weekdays"),
        _supersede("d-mbp:1"),
        _supersede("d-mbp:2"),  # the retraction was the mistake
    )
    resolution = resolve(records)
    assert _current(resolution) == ["d-mbp:1"]
    assert resolution.retired == ()


def test_a_three_deep_supersede_chain_hides_again() -> None:
    records = _stream(
        _assertion("a"),
        _supersede("d-mbp:1"),
        _supersede("d-mbp:2"),
        _supersede("d-mbp:3"),
    )
    resolution = resolve(records)
    assert _current(resolution) == []
    assert [(r.ref, r.by) for r in resolution.retired] == [("d-mbp:1", ("d-mbp:2",))]


def test_two_supersedes_of_one_assertion_both_count() -> None:
    records = _stream(_assertion("a"), _supersede("d-mbp:1"), _supersede("d-mbp:1", "2026-01-01"))
    resolution = resolve(records)
    assert _current(resolution) == []
    assert [(r.by, r.valid_to) for r in resolution.retired] == [
        (("d-mbp:2", "d-mbp:3"), "2026-01-01")
    ]
    # retracting one of them leaves the other standing
    records += _stream(_supersede("d-mbp:2"), device="phone")
    resolution = resolve(records)
    assert _current(resolution) == []
    assert [(r.ref, r.by, r.valid_to) for r in resolution.retired] == [
        ("d-mbp:1", ("d-mbp:3",), "2026-01-01")
    ]


def test_a_supersede_cycle_is_refused_by_name() -> None:
    mbp = _stream(_assertion("a"), _supersede("d-phone:1"), device="mbp")
    phone = _stream(_supersede("d-mbp:2"), device="phone")
    with pytest.raises(ResolutionError, match=r"cycle.*d-mbp:2.*d-phone:1"):
        resolve(mbp + phone)


def test_a_supersede_of_itself_is_refused_as_a_cycle() -> None:
    with pytest.raises(ResolutionError, match=r"cycle: d-mbp:2 -> d-mbp:2"):
        resolve(_stream(_assertion("a"), _supersede("d-mbp:2")))


# --- reported, not silent -------------------------------------------------------------


def test_a_supersede_of_an_event_the_fold_was_not_given_is_reported() -> None:
    records = _stream(_assertion("a"), _supersede("d-phone:9"))
    resolution = resolve(records)
    assert _current(resolution) == ["d-mbp:1"]
    assert [(u.ref, "d-phone:9" in u.reason) for u in resolution.unapplied] == [("d-mbp:2", True)]


@pytest.mark.parametrize(
    "caused_by",
    [
        ("d-mbp:1",),  # one side only
        ("d-mbp:1", "d-phone:4"),  # a side the fold was not given
        ("d-mbp:1", "d-mbp:2", "d-mbp:3"),  # three assertions
        ("d-mbp:1", "d-mbp:4"),  # a side that is not an assertion
    ],
)
def test_a_contradiction_that_does_not_name_two_assertions_is_reported(
    caused_by: tuple[str, ...],
) -> None:
    observation = Observation(
        ts=TS, actor="observer", source="transcript", observed_at=TS, body="said so"
    )
    records = _stream(
        _assertion("a", "2026-01-01"),
        _assertion("b", "2026-02-01"),
        _assertion("c", "2026-03-01"),
        observation,
        _contradiction(*caused_by),
    )
    resolution = resolve(records)
    assert resolution.conflicts == ()
    assert all(b.closed_by == () for b in resolution.current)
    assert [u.ref for u in resolution.unapplied] == ["d-mbp:5"]


def test_observations_ride_along_as_evidence_in_a_contradiction() -> None:
    observation = Observation(
        ts=TS, actor="observer", source="transcript", observed_at=TS, body="said so"
    )
    records = _stream(
        observation,
        _assertion("a", "2026-01-01"),
        _assertion("b", "2026-03-01"),
        _contradiction("d-mbp:1", "d-mbp:2", "d-mbp:3"),
    )
    resolution = resolve(records)
    assert resolution.belief("d-mbp:2").window == Window("2026-01-01", "2026-03-01")
    assert resolution.unapplied == ()


@pytest.mark.parametrize("value", ["20260301", "2026-W09-1", "2026-03-01 09:00:00+00:00", "soon"])
def test_a_window_refuses_a_spelling_the_log_refuses(value: str) -> None:
    with pytest.raises(ValueError):
        Window(value, None)
    with pytest.raises(ValueError):
        Window(None, value)


# --- pure ------------------------------------------------------------------------------


def test_input_order_does_not_change_the_answer() -> None:
    mbp = _stream(
        _assertion("Lives in Pune", "2020-01-01"),
        _assertion("Cooks vegetarian"),
        _supersede("d-mbp:2"),
        device="mbp",
    )
    phone = _stream(
        _assertion("Lives in Bangalore", "2026-03-01"),
        _contradiction("d-mbp:1", "d-phone:1"),
        _assertion("Eats fish"),
        _assertion("Vegetarian"),
        _contradiction("d-phone:3", "d-phone:4"),
        device="phone",
    )
    expected = resolve(mbp + phone)
    shuffled = mbp + phone
    for seed in range(20):
        random.Random(seed).shuffle(shuffled)
        assert resolve(shuffled) == expected
    assert _current(expected) == ["d-mbp:1", "d-phone:1", "d-phone:3", "d-phone:4"]
    assert [c.ref for c in expected.conflicts] == ["d-phone:5"]


def test_the_same_record_twice_is_one_record() -> None:
    records = _stream(_assertion("a"), _supersede("d-mbp:1"))
    assert resolve(records + records) == resolve(records)


def test_one_ref_naming_two_different_events_is_refused() -> None:
    clash = _stream(_assertion("something else"))
    with pytest.raises(ResolutionError, match="d-mbp:1"):
        resolve(_stream(_assertion("a")) + clash)


def test_the_resolver_does_no_io_and_reads_no_clock() -> None:
    """Constraint 3, checked on the source: `test_constraints` covers models and network;
    this covers the rest of what would make the fold depend on something besides its input."""
    tree = ast.parse(RESOLVER_SOURCE.read_text())
    runtime_imports: set[str] = set()
    for node in tree.body:  # top level only: imports under `if TYPE_CHECKING:` never run
        if isinstance(node, ast.Import):
            runtime_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            runtime_imports.add(node.module)
    allowed = {
        "__future__",
        "dataclasses",
        "datetime",
        "pydantic",
        "typing",
        "walle.events",
        "walle.merge",  # issue #120: the shared orders; test_merge pins that it reads no clock
    }
    assert runtime_imports <= allowed

    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }
    assert not called & {"open", "now", "today", "utcnow", "now_instant", "time", "input"}


# --- read through walle.reader ---------------------------------------------------------


def test_the_fold_runs_over_streams_read_back_from_disk(tmp_path: Path) -> None:
    mbp = StreamWriter(tmp_path, "maya", "mbp")
    phone = StreamWriter(tmp_path, "maya", "phone")
    mbp.append(_assertion("Lives in Pune", "2020-01-01"))  # d-mbp:1
    mbp.append(_assertion("Cooks vegetarian on weekdays"))  # d-mbp:2
    phone.append(_assertion("Lives in Bangalore", "2026-03-01"))  # d-phone:1
    phone.append(_contradiction("d-mbp:1", "d-phone:1"))  # d-phone:2
    mbp.append(_supersede("d-mbp:2", valid_to="2026-10-01"))  # d-mbp:3

    streams = [read_stream(tmp_path, "maya", d) for d in ("mbp", "phone")]
    resolution = resolve(record for stream in streams for record in stream)

    assert _current(resolution) == ["d-mbp:1", "d-phone:1"]
    assert resolution.belief("d-mbp:1").window == Window("2020-01-01", "2026-03-01")
    assert [(r.ref, r.valid_to) for r in resolution.retired] == [("d-mbp:2", "2026-10-01")]
    assert resolution.conflicts == () and resolution.unapplied == ()


def test_belief_names_a_ref_that_is_not_current() -> None:
    resolution = resolve(_stream(_assertion("a"), _supersede("d-mbp:1")))
    with pytest.raises(KeyError, match="d-mbp:1"):
        resolution.belief("d-mbp:1")


# --- the staging queue (task 9, D49, D60) -----------------------------------------------


def _proposal(summary: str) -> Proposal:
    return Proposal(ts=TS, actor=ACTOR_USER, body=summary, summary=summary)


def _kept(proposal_ref: str, summary: str) -> Assertion:
    return Assertion(
        ts=TS, actor=ACTOR_USER, body=summary, summary=summary, caused_by=[proposal_ref]
    )


def test_a_proposal_is_staged_and_never_current() -> None:
    records = _stream(_proposal("likes rasam"))
    assert [s.ref for s in staged(records)] == ["d-mbp:1"]
    assert resolve(records).current == ()


def test_an_assertion_citing_a_proposal_decides_it() -> None:
    records = _stream(_proposal("likes rasam"), _kept("d-mbp:1", "likes rasam"))
    assert staged(records) == ()
    assert [b.ref for b in resolve(records).current] == ["d-mbp:2"]


def test_a_superseded_proposal_is_not_staged_and_still_in_the_records() -> None:
    records = _stream(_proposal("likes rasam"), _supersede("d-mbp:1"))
    assert staged(records) == ()
    assert resolve(records).unapplied == ()  # the tombstone applied: its target was given


def test_superseding_the_tombstone_stages_the_proposal_again() -> None:
    """Rule 1 for proposals too: a drop is undone by retracting it, not by editing."""
    records = _stream(_proposal("likes rasam"), _supersede("d-mbp:1"), _supersede("d-mbp:2"))
    assert [s.ref for s in staged(records)] == ["d-mbp:1"]


def test_a_kept_proposal_stays_decided_after_its_assertion_is_forgotten() -> None:
    """Forgetting the belief later must not put the proposal back in the review batch."""
    records = _stream(
        _proposal("likes rasam"), _kept("d-mbp:1", "likes rasam"), _supersede("d-mbp:2")
    )
    assert staged(records) == ()
    assert resolve(records).current == ()


def test_staged_is_in_ref_order_whatever_the_input_order() -> None:
    records = _stream(_proposal("a"), _proposal("b"), device="phone") + _stream(_proposal("c"))
    shuffled = list(records)
    random.Random(9).shuffle(shuffled)
    assert [s.ref for s in staged(shuffled)] == ["d-mbp:1", "d-phone:1", "d-phone:2"]


# --- issue 23: low findings from the review of PR 22 -----------------------------------


def test_one_moment_in_two_offsets_compares_the_same_against_a_date() -> None:
    """Two spellings of one instant are equal, so neither may order differently against a
    date. Comparing the instant's *written* day put the first on 1 March and the second on
    2 March."""
    utc, ist, day = "2026-03-01T20:30:00+00:00", "2026-03-02T02:00:00+05:30", "2026-03-01"
    assert starts_later(Window(utc), Window(ist)) is False
    assert starts_later(Window(ist), Window(utc)) is False
    assert starts_later(Window(utc), Window(day)) == starts_later(Window(ist), Window(day))
    assert windows_overlap(Window(None, day), Window(utc)) == windows_overlap(
        Window(None, day), Window(ist)
    )


def test_an_instant_far_from_a_date_is_still_ordered_against_it() -> None:
    """Outside every time zone's reading of the day, the order is known."""
    assert starts_later(Window("2026-03-03T00:00:00+00:00"), Window("2026-03-01")) is True
    assert starts_later(Window("2026-03-01"), Window("2026-02-27T23:00:00+00:00")) is True


def test_two_contradictions_naming_one_pair_list_the_closer_once() -> None:
    """Written independently on two devices before sync: one closing, listed once."""
    records = _stream(
        _assertion("Lives in Pune", "2020-01-01"),
        _assertion("Lives in Bangalore", "2026-03-01"),
        _contradiction("d-mbp:1", "d-mbp:2"),
    ) + _stream(_contradiction("d-mbp:1", "d-mbp:2"), device="phone")
    pune = resolve(records).belief("d-mbp:1")
    assert pune.closed_by == ("d-mbp:2",)
    assert pune.window == Window("2020-01-01", "2026-03-01")


@pytest.mark.parametrize(
    "empty",
    [("2026-03-01", "2026-03-01"), ("2026-03-05", "2026-03-01"), (TS, TS)],
    ids=["empty date window", "inverted", "empty instant window"],
)
def test_an_empty_or_inverted_window_overlaps_nothing(empty: tuple[str, str]) -> None:
    """``[from, to)`` with ``from >= to`` holds no moment, so it cannot overlap one."""
    assert windows_overlap(Window(*empty), Window(None, None)) is False
    assert windows_overlap(Window(None, None), Window(*empty)) is False

    records = _stream(
        _assertion("Lives in Pune", "2020-01-01"),
        _assertion("Lived in Bangalore, never", *empty),
        _contradiction("d-mbp:1", "d-mbp:2"),
    )
    resolution = resolve(records)
    assert resolution.conflicts == ()
    assert resolution.belief("d-mbp:1").window == Window("2020-01-01", None)
