"""Tasks 22-23's verify criterion, and the cases around it (D89, D95).

  folds identically      a snapshot folds the same as the events it replaces: resolve and staged
                         over the view equal resolve and staged over the full log
  any log, any forgets   for random logs and snapshots that forget anything at all, the view
                         still folds identically, because it only hides what nothing points at
  held, not hidden       a forgotten ref that a supersede or contradiction still names stays in
                         the view and is reported with the reason
  undo                   superseding a snapshot brings back what it forgot
  forgets check          a snapshot may forget only observations and snapshots nothing names,
                         each once, all of them in the log
  plan                   the older half of what is not yet forgotten, by the source's own time
  gate                   idle >= 30 min, >= 20 new observations, >= 12 h since the last run
  pure                   the gate and the view read no clock, file or model

Every event here is synthetic: a made-up person's dinners and plants, no real data.
"""

from __future__ import annotations

import ast
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from walle.consolidation import (
    MIN_GAP,
    MIN_IDLE,
    MIN_NEW,
    Policy,
    SnapshotError,
    check_forgets,
    forgettable,
    gate,
    plan_forgets,
    view,
)
from walle.events import (
    ACTOR_CURATOR,
    ACTOR_OBSERVER,
    ACTOR_USER,
    AnyEvent,
    Assertion,
    Contradiction,
    Observation,
    Proposal,
    Snapshot,
    Supersede,
)
from walle.reader import Record
from walle.resolver import ResolutionError, resolve, staged

PACKAGE = Path(__file__).parent.parent / "src" / "walle"
T0 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="microseconds")


def _obs(text: str, minute: float = 0, observed: float | None = None) -> Observation:
    return Observation(
        ts=_at(minute),
        actor=ACTOR_OBSERVER,
        source="transcript",
        observed_at=_at(minute if observed is None else observed),
        body=text,
    )


def _assertion(text: str, *causes: str, valid_from: str | None = None) -> Assertion:
    return Assertion(
        ts=_at(0),
        actor=ACTOR_USER,
        body=text,
        summary=text,
        caused_by=list(causes),
        valid_from=valid_from,
    )


def _proposal(text: str) -> Proposal:
    return Proposal(ts=_at(0), actor=ACTOR_USER, body=text, summary=text)


def _supersede(target: str) -> Supersede:
    return Supersede(ts=_at(0), actor=ACTOR_USER, target=target)


def _contradiction(*refs: str) -> Contradiction:
    return Contradiction(ts=_at(0), actor=ACTOR_CURATOR, caused_by=list(refs))


def _snapshot(*forgets: str, minute: float = 0) -> Snapshot:
    return Snapshot(
        ts=_at(minute),
        actor=ACTOR_CURATOR,
        forgets=list(forgets),
        body="Maya cooked at home most evenings this week.",
        model="synthetic-model",
    )


def _stream(*events: AnyEvent, device: str = "mbp", start: int = 1) -> list[Record]:
    out: list[Record] = []
    for seq, event in enumerate(events, start=start):
        stored = event.model_copy(update={"seq": seq})
        out.append(Record(ref=f"d-{device}:{seq}", seq=seq, event=stored, line=seq))
    return out


def _refs(records: tuple[Record, ...] | list[Record]) -> list[str]:
    return [record.ref for record in records]


# --- the verify criterion ----------------------------------------------------------------


def _a_week() -> list[Record]:
    """A small log with every type in it, across two devices."""
    mbp = _stream(
        _obs("made dal for dinner", 1),  # d-mbp:1
        _obs("watered the basil", 2),  # d-mbp:2
        _obs("made rice and dal again", 3),  # d-mbp:3
        _assertion("cooks dal often", "d-mbp:1", "d-mbp:3"),  # d-mbp:4
        _obs("bought chillies", 5),  # d-mbp:5
        _proposal("prefers mild food"),  # d-mbp:6
        _assertion("prefers mild food", "d-mbp:6"),  # d-mbp:7
        _obs("ordered a spicy curry", 8),  # d-mbp:8
        _contradiction("d-mbp:7", "d-mbp:8"),  # d-mbp:9
        _proposal("has a balcony garden"),  # d-mbp:10
    )
    phone = _stream(
        _obs("photo of the basil", 4),  # d-phone:1
        _assertion("lives in Pune", valid_from="2025-01-01"),  # d-phone:2
        _supersede("d-phone:2"),  # d-phone:3
        _obs("asked about tomato seedlings", 6),  # d-phone:4
        device="phone",
    )
    return mbp + phone


def test_a_snapshot_folds_identically_to_the_events_it_replaces() -> None:
    log = _a_week()
    forgets = plan_forgets(log)
    assert forgets  # the plan found something to forget, or this proves nothing
    check_forgets(log, forgets)
    snapshot = _stream(_snapshot(*forgets), device="srv")
    with_snapshot = [*log, *snapshot]

    seen = view(with_snapshot)
    assert seen.forgotten == forgets
    assert not set(forgets) & set(_refs(seen.records))
    assert resolve(seen.records) == resolve(log)
    assert staged(seen.records) == staged(log)
    # The snapshot appended without the view changes nothing either: the fold ignores it.
    assert resolve(with_snapshot) == resolve(log)


def test_the_view_of_a_log_with_no_snapshot_is_the_log() -> None:
    log = _a_week()
    seen = view(log)
    assert _refs(seen.records) == _refs(log)
    assert seen.forgotten == ()
    assert seen.held == ()


def _random_log(rng: random.Random) -> list[Record]:
    """A synthetic log of 10-60 events on two streams, every type, forgets naming anything."""
    counters = {"mbp": 0, "phone": 0}
    refs: list[str] = []
    out: list[Record] = []
    dates = [None, "2025-01-01", "2025-06-01", "2026-01-01"]
    for i in range(rng.randint(10, 60)):
        kind = rng.choice(
            ["obs", "obs", "obs", "assertion", "proposal", "supersede", "contra", "snapshot"]
        )
        event: AnyEvent
        if kind == "obs" or not refs:
            event = _obs(f"synthetic note {i}", rng.uniform(0, 600), rng.uniform(0, 600))
        elif kind == "assertion":
            causes = rng.sample(refs, k=min(len(refs), rng.randint(0, 2)))
            event = _assertion(f"belief {i}", *causes, valid_from=rng.choice(dates))
        elif kind == "proposal":
            event = _proposal(f"maybe {i}")
        elif kind == "supersede":
            event = _supersede(rng.choice(refs))
        elif kind == "contra":
            event = _contradiction(*rng.sample(refs, k=min(len(refs), 2)))
        else:
            event = _snapshot(*rng.sample(refs, k=rng.randint(1, len(refs))))
        device = rng.choice(["mbp", "phone"])
        counters[device] += 1
        seq = counters[device]
        record = _stream(event, device=device, start=seq)[0]
        refs.append(record.ref)
        out.append(record)
    rng.shuffle(out)
    return out


@pytest.mark.parametrize("seed", range(200))
def test_any_snapshot_in_any_log_folds_identically(seed: int) -> None:
    log = _random_log(random.Random(seed))
    try:
        expected = resolve(log), staged(log)
    except ResolutionError:
        with pytest.raises(ResolutionError):
            view(log)
        return
    seen = view(log)
    assert (resolve(seen.records), staged(seen.records)) == expected


# --- what the view hides and what it holds back -------------------------------------------


def test_a_forgotten_observation_a_contradiction_names_is_held_not_hidden() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:8", "d-mbp:2"), device="srv")]
    seen = view(log)
    assert seen.forgotten == ("d-mbp:2",)
    assert "d-mbp:8" in _refs(seen.records)
    [held] = seen.held
    assert (held.ref, held.by) == ("d-mbp:8", "d-srv:1")
    assert "d-mbp:9" in held.reason
    assert resolve(seen.records) == resolve(log)


def test_a_contradiction_written_after_the_snapshot_pins_what_it_names() -> None:
    """The view is decided at fold time, so a later event cannot make it lie."""
    log = [*_a_week(), *_stream(_snapshot("d-mbp:2"), device="srv")]
    assert view(log).forgotten == ("d-mbp:2",)
    later = [*log, *_stream(_contradiction("d-mbp:7", "d-mbp:2"), device="srv", start=2)]
    seen = view(later)
    assert seen.forgotten == ()
    assert [h.ref for h in seen.held] == ["d-mbp:2"]
    assert resolve(seen.records) == resolve(later)


def test_a_snapshot_cannot_hide_an_assertion_or_a_proposal() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:4", "d-mbp:10"), device="srv")]
    seen = view(log)
    assert seen.forgotten == ()
    assert {h.ref: h.reason for h in seen.held} == {
        "d-mbp:4": "an assertion event: only observations and snapshots can be forgotten",
        "d-mbp:10": "a proposal event: only observations and snapshots can be forgotten",
    }
    assert staged(seen.records) == staged(log)


def test_a_ref_not_in_the_log_is_held_and_reported() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:99"), device="srv")]
    [held] = view(log).held
    assert (held.ref, held.reason) == ("d-mbp:99", "not in the log")


def test_superseding_a_snapshot_brings_back_what_it_forgot() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:2"), _supersede("d-srv:1"), device="srv")]
    seen = view(log)
    assert seen.forgotten == ()
    assert "d-mbp:2" in _refs(seen.records)
    assert "d-srv:1" in _refs(seen.records)  # the snapshot is pinned by its own supersede


def test_summaries_get_summarized_and_the_first_forgets_still_apply() -> None:
    """D89: a later snapshot may forget an earlier one; what the earlier one forgot stays
    forgotten, because forgetting is not superseding."""
    log = [*_a_week(), *_stream(_snapshot("d-mbp:2"), _snapshot("d-srv:1"), device="srv")]
    seen = view(log)
    assert seen.forgotten == ("d-mbp:2", "d-srv:1")
    assert resolve(seen.records) == resolve(log)


def test_one_ref_naming_two_events_is_still_refused() -> None:
    """Hiding a ref must not hide a corrupt log: the full set is checked before anything is."""
    log = [
        *_a_week(),
        *_stream(_obs("a different note", 2), start=2),
        *_stream(_snapshot("d-mbp:2"), device="srv"),
    ]
    with pytest.raises(ResolutionError, match="d-mbp:2"):
        view(log)


def test_the_view_keeps_the_input_order() -> None:
    log = list(reversed([*_a_week(), *_stream(_snapshot("d-mbp:2"), device="srv")]))
    expected = [r for r in _refs(log) if r != "d-mbp:2"]
    assert _refs(view(log).records) == expected


# --- what a snapshot may forget ----------------------------------------------------------


def test_forgettable_is_observations_and_snapshots_nothing_names() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:2"), device="srv")]
    assert forgettable(log) == frozenset(
        {"d-mbp:1", "d-mbp:2", "d-mbp:3", "d-mbp:5", "d-phone:1", "d-phone:4", "d-srv:1"}
    )


@pytest.mark.parametrize(
    ("forgets", "message"),
    [
        ((), "forgets nothing"),
        (("d-mbp:1", "d-mbp:1"), "d-mbp:1 is named twice"),
        (("d-mbp:99",), "d-mbp:99 is not in the log"),
        (("d-mbp:4",), "d-mbp:4 is an assertion event"),
        (("d-mbp:8",), "d-mbp:8 is named by d-mbp:9"),
        (("d-phone:2",), "d-phone:2 is an assertion event"),
    ],
)
def test_check_forgets_refuses(forgets: tuple[str, ...], message: str) -> None:
    with pytest.raises(SnapshotError, match=message):
        check_forgets(_a_week(), forgets)


def test_check_forgets_refuses_what_a_live_snapshot_already_forgot() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:2"), device="srv")]
    with pytest.raises(SnapshotError, match="d-mbp:2 is already forgotten by d-srv:1"):
        check_forgets(log, ("d-mbp:1", "d-mbp:2"))


def test_check_forgets_names_every_problem_at_once() -> None:
    with pytest.raises(SnapshotError) as info:
        check_forgets(_a_week(), ("d-mbp:4", "d-mbp:99"))
    assert "d-mbp:4" in str(info.value) and "d-mbp:99" in str(info.value)


def test_check_forgets_rejects_a_malformed_ref() -> None:
    with pytest.raises(SnapshotError, match="not a provenance ref"):
        check_forgets(_a_week(), ("mbp:1",))


def test_plan_forgets_the_older_half_by_the_sources_own_time() -> None:
    """D89: condense the older half, keep the recent half verbatim. Older means observed_at,
    the source's time (D93), not the order the observer happened to read them in."""
    log = _stream(
        _obs("read first, said last week", 10, observed=-10_000),  # d-mbp:1
        _obs("read second, said today", 11, observed=11),  # d-mbp:2
        _obs("read third, said yesterday", 12, observed=-1_000),  # d-mbp:3
        _obs("read fourth, said just now", 13, observed=13),  # d-mbp:4
        _obs("read fifth, said two weeks ago", 14, observed=-20_000),  # d-mbp:5
    )
    assert plan_forgets(log) == ("d-mbp:1", "d-mbp:5")


def test_plan_skips_what_is_pinned_or_already_forgotten() -> None:
    log = [*_a_week(), *_stream(_snapshot("d-mbp:1", "d-mbp:2"), device="srv")]
    # Observations left in the view and unnamed: d-mbp:3, d-mbp:5, d-phone:1, d-phone:4.
    assert plan_forgets(log) == ("d-mbp:3", "d-phone:1")
    check_forgets(log, plan_forgets(log))


def test_plan_of_a_single_observation_forgets_nothing() -> None:
    assert plan_forgets(_stream(_obs("one note"))) == ()


# --- the gate (D95) ----------------------------------------------------------------------


def _observations(n: int, *, from_minute: float = 0, device: str = "mbp") -> list[Record]:
    return _stream(*(_obs(f"note {i}", from_minute + i) for i in range(n)), device=device)


def test_the_starting_values_are_d95s() -> None:
    assert MIN_IDLE.total_seconds() == 30 * 60
    assert MIN_NEW == 20
    assert MIN_GAP.total_seconds() == 12 * 3600
    assert Policy() == Policy(idle=MIN_IDLE, new=MIN_NEW, gap=MIN_GAP)


def test_the_gate_opens_when_all_three_hold() -> None:
    log = _observations(20)  # last one at minute 19
    result = gate(log, T0 + timedelta(minutes=19 + 30))
    assert result.due
    assert result.reasons == ()
    assert result.new_observations == 20
    assert result.idle_for == timedelta(minutes=30)
    assert result.last_run is None and result.since_last_run is None


def test_the_gate_stays_shut_during_a_session() -> None:
    log = _observations(40)
    result = gate(log, T0 + timedelta(minutes=39 + 29))
    assert not result.due
    assert result.reasons == ("idle 29 min, needs 30 min",)


def test_the_gate_stays_shut_with_too_few_new() -> None:
    result = gate(_observations(19), T0 + timedelta(days=1))
    assert not result.due
    assert result.reasons == ("19 new observations, needs 20",)


def test_the_gate_counts_only_observations_since_the_last_run() -> None:
    log = [
        *_observations(30),
        *_stream(_snapshot("d-mbp:1", minute=100), device="srv"),
        *_observations(5, from_minute=200, device="phone"),
    ]
    result = gate(log, T0 + timedelta(days=2))
    assert result.new_observations == 5
    assert result.last_run == "d-srv:1"


def test_the_gate_stays_shut_too_soon_after_the_last_run() -> None:
    log = [
        *_stream(_snapshot("d-mbp:99", minute=0), device="srv"),
        *_observations(25, from_minute=60),
    ]
    now = T0 + timedelta(hours=11, minutes=59)
    result = gate(log, now)
    assert not result.due
    assert result.reasons == ("11.98 h since the last run (d-srv:1), needs 12.00 h",)
    assert gate(log, T0 + timedelta(hours=12)).due
    # Floored, never rounded up into a number that says it should have passed.
    almost = gate(log, T0 + timedelta(hours=12) - timedelta(seconds=1))
    assert almost.reasons == ("11.99 h since the last run (d-srv:1), needs 12.00 h",)


def test_a_snapshot_is_not_activity() -> None:
    """The Curator's own write must not reset the idle clock it is gated on."""
    log = [*_observations(20), *_stream(_snapshot("d-mbp:1", minute=-10_000), device="srv")]
    log += _stream(_snapshot("d-mbp:2", minute=10_000), device="srv", start=2)
    result = gate(log, T0 + timedelta(minutes=10_000) + timedelta(hours=12))
    assert result.idle_for is not None and result.idle_for > timedelta(days=1)


def test_an_event_from_the_future_is_not_idle() -> None:
    result = gate(_observations(25), T0)
    assert not result.due
    assert result.reasons == ("idle -24 min, needs 30 min",)


def test_an_empty_log_is_not_due() -> None:
    result = gate([], T0)
    assert not result.due
    assert result.idle_for is None
    assert result.reasons == ("0 new observations, needs 20",)


def test_the_gate_refuses_a_naive_now() -> None:
    with pytest.raises(ValueError, match="time zone"):
        gate([], datetime(2026, 11, 4, 9, 0))  # naive on purpose


def test_a_policy_with_nothing_to_wait_for_is_refused() -> None:
    with pytest.raises(ValueError, match="new"):
        Policy(new=0)
    with pytest.raises(ValueError, match="idle"):
        Policy(idle=timedelta(minutes=-1))
    with pytest.raises(ValueError, match="gap"):
        Policy(gap=timedelta(hours=-1))


def test_the_gate_is_a_function_of_its_arguments() -> None:
    log = _observations(20)
    now = T0 + timedelta(hours=1)
    assert gate(log, now) == gate(list(reversed(log)), now)
    assert gate(log, now, Policy(new=21)).reasons == ("20 new observations, needs 21",)


def test_the_pure_module_reads_no_clock_file_or_model() -> None:
    """D95's gate and D89's view are folds. No IO module, no clock read, no model import."""
    tree = ast.parse((PACKAGE / "consolidation.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"now", "utcnow", "today", "time"}, ast.unparse(node)
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "math",
        "typing",
        "collections.abc",
        "walle.events",
        "walle.reader",
        "walle.resolver",
    }
    assert imported <= allowed, imported - allowed
