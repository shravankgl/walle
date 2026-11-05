"""Issue #120's shared merge order: the one order every fold applies events in.

  streams merge by time    an event the phone wrote at 10:01 is applied before one the laptop
                           wrote at 10:10, although ``d-mbp`` sorts before ``d-phone``
  a stream keeps its seq   within one stream the order is always ``seq`` (D57), whatever the
                           stamps say; one device's log merges to exactly its ref order
  ties fall back to D57    equal instants are ordered ``(stream, seq)``
  a clock that ran back    never reorders its own stream and never breaks determinism: the
                           later seq waits behind the earlier one, then interleaves from there
  forgetting can move it   the merge is not stable under taking events away when a clock ran
                           back; ``ref_order``, D57's ``(stream, seq)``, is, and is what the
                           resolver lists its answer in
  one order everywhere     every fold that applies events imports it, the resolver lists by
                           ``ref_order``, and none sorts events by ref on its own

Every ref and stamp here is made up.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from walle.events import ACTOR_USER, Assertion, parse_ref
from walle.merge import fold_order, merge_order, ref_order

PACKAGE = Path(__file__).parent.parent / "src" / "walle"
APPLIERS = ("automations", "missions", "inbox", "survival", "stats")
LISTERS = ("resolver",)


def _at(hhmm: str, offset: str = "+00:00") -> str:
    return f"2026-11-05T{hhmm}:00.000000{offset}"


# --- streams merge by time ------------------------------------------------------------------


def test_two_streams_interleave_by_instant_not_by_stream_name() -> None:
    stamps = {
        "d-mbp:1": _at("10:00"),
        "d-phone:1": _at("10:01"),
        "d-mbp:2": _at("10:10"),
        "d-phone:2": _at("10:20"),
    }
    assert merge_order(stamps) == ("d-mbp:1", "d-phone:1", "d-mbp:2", "d-phone:2")


def test_instants_compare_as_moments_not_as_strings() -> None:
    """10:00 in Kolkata is 04:30 in UTC, so it comes before 05:00 UTC although the string
    ``2026-11-05T10:00`` sorts after ``2026-11-05T05:00``."""
    stamps = {"d-mbp:1": _at("05:00"), "d-phone:1": _at("10:00", "+05:30")}
    assert merge_order(stamps) == ("d-phone:1", "d-mbp:1")


# --- a stream keeps its seq ----------------------------------------------------------------


def test_one_stream_is_its_seq_order_whatever_the_stamps_say() -> None:
    stamps = {"d-mbp:3": _at("09:00"), "d-mbp:1": _at("10:10"), "d-mbp:2": _at("09:50")}
    assert merge_order(stamps) == ("d-mbp:1", "d-mbp:2", "d-mbp:3")


def test_seqs_need_not_be_contiguous() -> None:
    """A fold given part of a stream — a filter by time, a snapshot's leftovers — still sees
    what it was given in seq order."""
    stamps = {"d-mbp:40": _at("10:00"), "d-mbp:7": _at("09:00"), "d-mbp:12": _at("09:30")}
    assert merge_order(stamps) == ("d-mbp:7", "d-mbp:12", "d-mbp:40")


# --- ties fall back to D57 -----------------------------------------------------------------


def test_equal_instants_are_ordered_by_stream_then_seq() -> None:
    same = _at("10:00")
    stamps = {"d-phone:1": same, "d-mbp:2": same, "d-mbp:1": same, "d-tab:1": same}
    assert merge_order(stamps) == ("d-mbp:1", "d-mbp:2", "d-phone:1", "d-tab:1")


def test_two_spellings_of_one_moment_tie() -> None:
    stamps = {"d-phone:1": _at("04:30"), "d-mbp:1": _at("10:00", "+05:30")}
    assert merge_order(stamps) == ("d-mbp:1", "d-phone:1")  # a tie: D57's order


# --- a clock that ran back -----------------------------------------------------------------


def test_a_backwards_clock_delays_its_own_later_events_and_moves_nothing_else() -> None:
    """The phone's clock stepped back between seq 1 and seq 2. Seq 2 cannot overtake seq 1
    (D57), so it waits behind it and is interleaved from there: the laptop's 09:30 goes
    first, then the phone's two in their own order."""
    stamps = {"d-phone:1": _at("10:10"), "d-phone:2": _at("09:00"), "d-mbp:1": _at("09:30")}
    assert merge_order(stamps) == ("d-mbp:1", "d-phone:1", "d-phone:2")


def test_after_the_step_back_the_stream_interleaves_from_where_it_stands() -> None:
    """Once the phone's seq 1 is applied, its seq 2 (09:00) is the earliest eligible event,
    so it goes before the laptop's 09:45 — not after it, as a plain sort by stamp would put
    it, and not before the laptop's 09:30, which seq 1 had let through."""
    stamps = {
        "d-phone:1": _at("10:10"),
        "d-phone:2": _at("09:00"),
        "d-mbp:1": _at("09:30"),
        "d-mbp:2": _at("10:15"),
        "d-mbp:3": _at("10:20"),
    }
    assert merge_order(stamps) == ("d-mbp:1", "d-phone:1", "d-phone:2", "d-mbp:2", "d-mbp:3")


def test_with_monotone_clocks_the_merge_is_the_plain_sort_by_instant_stream_seq() -> None:
    """When every stream's stamps are non-decreasing, the merge is the sort by
    ``(instant, stream, seq)``: the simple rule holds whenever clocks behave."""
    rng = random.Random(120)
    for _ in range(50):
        stamps: dict[str, str] = {}
        for stream in ("d-mbp", "d-phone", "d-tab"):
            minute = 0
            for seq in range(1, rng.randint(1, 6)):
                minute += rng.randint(0, 3)  # non-decreasing, ties allowed
                stamps[f"{stream}:{seq}"] = _at(f"{10 + minute // 60:02d}:{minute % 60:02d}")
        plain = sorted(stamps, key=lambda r: (stamps[r], parse_ref(r)))
        assert list(merge_order(stamps)) == plain


# --- determinism --------------------------------------------------------------------------


def test_the_order_does_not_depend_on_the_order_given_and_is_a_permutation() -> None:
    rng = random.Random(7)
    stamps = {
        f"{stream}:{seq}": _at(f"{rng.randint(8, 12):02d}:{rng.randint(0, 59):02d}")
        for stream in ("d-mbp", "d-phone", "d-tab")
        for seq in range(1, 9)
    }
    first = merge_order(stamps)
    assert sorted(first) == sorted(stamps)
    for _ in range(20):
        items = list(stamps.items())
        rng.shuffle(items)
        assert merge_order(dict(items)) == first
    assert merge_order(stamps) == first  # the same log twice: the same answer


def test_nothing_merges_to_nothing() -> None:
    assert merge_order({}) == ()
    assert ref_order(()) == ()


def test_fold_order_reads_each_events_ts() -> None:
    def assertion(ts: str) -> Assertion:
        return Assertion(ts=ts, actor=ACTOR_USER, body="made up", summary="made up")

    events = {"d-mbp:1": assertion(_at("10:10")), "d-phone:1": assertion(_at("10:01"))}
    assert fold_order(events) == ("d-phone:1", "d-mbp:1")


@pytest.mark.parametrize(
    ("ref", "stamp"),
    [
        ("mbp:1", _at("10:00")),  # no d- prefix
        ("d-mbp", _at("10:00")),  # no seq
        ("d-mbp:1", "yesterday"),  # not an instant at all
        ("d-mbp:1", "2026-11-05T10:00:00"),  # naive: no offset
    ],
)
def test_a_malformed_ref_or_stamp_is_a_value_error(ref: str, stamp: str) -> None:
    with pytest.raises(ValueError):
        merge_order({ref: stamp, "d-tab:1": _at("09:00")})


# --- forgetting can move it ----------------------------------------------------------------


def test_taking_an_event_away_can_move_its_streams_later_events_when_a_clock_ran_back() -> None:
    """The limit, stated: with the phone's seq 1 there, seq 2 waits behind it (10:10); with
    seq 1 forgotten, seq 2 is applied at its own stamp (09:50). Folds that apply events run
    over whole streams, so they never see this; a listing of a set must not, so the
    resolver lists by ``ref_order``."""
    whole = {"d-phone:1": _at("10:10"), "d-phone:2": _at("09:50"), "d-mbp:1": _at("10:00")}
    assert merge_order(whole) == ("d-mbp:1", "d-phone:1", "d-phone:2")
    without = {ref: ts for ref, ts in whole.items() if ref != "d-phone:1"}
    assert merge_order(without) == ("d-phone:2", "d-mbp:1")


def test_ref_order_is_d57s_stream_then_seq_once_each_and_stable_under_any_subset() -> None:
    refs = ["d-phone:2", "d-mbp:10", "d-mbp:9", "d-phone:1", "d-mbp:9"]
    assert ref_order(refs) == ("d-mbp:9", "d-mbp:10", "d-phone:1", "d-phone:2")
    whole = ref_order(refs)
    for drop in whole:
        kept = ref_order(r for r in refs if r != drop)
        assert kept == tuple(r for r in whole if r != drop)
    with pytest.raises(ValueError):
        ref_order(["mbp:1"])


# --- one order everywhere -----------------------------------------------------------------


@pytest.mark.parametrize("module", APPLIERS)
def test_every_fold_that_applies_events_uses_the_shared_merge(module: str) -> None:
    source = (PACKAGE / f"{module}.py").read_text()
    assert "from walle.merge import" in source, f"{module} does not import walle.merge"
    assert "fold_order(events)" in source
    for private in ("sorted(events, key=parse_ref)", "sorted(events, key=_ref_key)"):
        assert private not in source, f"{module} still orders events on its own: {private}"


@pytest.mark.parametrize("module", LISTERS)
def test_what_lists_a_set_of_refs_uses_the_shared_ref_order(module: str) -> None:
    source = (PACKAGE / f"{module}.py").read_text()
    assert "from walle.merge import" in source
    assert "ref_order(" in source
    assert "key=parse_ref" not in source and "_ref_key" not in source


def test_the_merge_reads_no_clock_and_opens_no_file() -> None:
    source = (PACKAGE / "merge.py").read_text()
    for name in ("now_instant", "datetime.now", "open(", "Path("):
        assert name not in source
