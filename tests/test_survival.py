"""Issue #97's verify criteria, and the cases around them (D6, D20, D114).

  from the log alone     survival is a pure function of the log and an explicit ``as_of``: the
                         same from the JSONL, from a rebuilt index, from an incremental one and
                         from the records in any order; events after ``as_of`` change nothing
  stopped counts against an automation retired before its day 30, or silent or only failing in
                         the week before it, is in the cohort and did not survive
  off by default         nothing records an outcome unless the owner has set the switch,
                         outside the log; reading the report is not acting

Every automation and outcome here is synthetic: a made-up household, no real data and no real
tool.
"""

from __future__ import annotations

import ast
import json
import random
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from test_constraints import forbidden_in_closure
from walle import automations, survival
from walle.events import (
    ACTOR_KERNEL,
    ACTOR_USER,
    Automation,
    AutomationVerb,
    Event,
    Outcome,
    OutcomeResult,
    Supersede,
)
from walle.index import load_records, rebuild, update
from walle.paths import log_dir
from walle.reader import Record, read_file, read_stream
from walle.writer import StreamWriter

P = "ines"
D = "mbp"
ON = {survival.OUTCOMES_SWITCH: survival.SWITCH_ON}
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "survival.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v1" / "outcomes"


# --- builders -----------------------------------------------------------------------------


def _ts(days: float) -> str:
    """An instant ``days`` after ``T0``, in the log's own format."""
    return (T0 + timedelta(days=days)).isoformat(timespec="microseconds")


def _write(root: Path, event: Event, device: str = D) -> str:
    written = StreamWriter(root, P, device).append(event)
    return f"d-{device}:{written.seq}"


def _add(root: Path, name: str, day: float = 0, actor: str = P, device: str = D) -> str:
    event = Automation(
        ts=_ts(day), actor=actor, verb=AutomationVerb.ADD, name=name, title=f"synthetic {name}"
    )
    return _write(root, event, device)


def _retire(root: Path, ref: str, day: float) -> str:
    return _write(root, Automation(ts=_ts(day), actor=P, verb=AutomationVerb.RETIRE, target=ref))


def _ran(root: Path, ref: str, day: float, result: str = "ok", device: str = D) -> str:
    event = Outcome(ts=_ts(day), actor=ACTOR_KERNEL, target=ref, result=OutcomeResult(result))
    return _write(root, event, device)


def _records(root: Path) -> list[Record]:
    log = log_dir(root, P)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _report(root: Path, day: float) -> survival.Survival:
    return survival.survival(_records(root), as_of=T0 + timedelta(days=day))


def _verdict(report: survival.Survival, ref: str) -> survival.Verdict:
    return next(v for v in report.cohort if v.ref == ref)


def _household(root: Path) -> dict[str, str]:
    """Four automations added on day 0, and what happened to each by day 40.

    water-plants  ran every few days and is still running: survived
    bin-night     retired on day 12: stopped, counts against
    school-run    ran until day 10, then went silent: stopped, counts against
    dog-walk      ran on day 26 but only failed after: still survived, an ok run is in the week
    """
    refs = {name: _add(root, name) for name in ("water-plants", "bin-night", "school-run")}
    refs["dog-walk"] = _add(root, "dog-walk")
    for day in (3, 10, 17, 24, 29):
        _ran(root, refs["water-plants"], day)
    _ran(root, refs["bin-night"], 5)
    _retire(root, refs["bin-night"], 12)
    for day in (2, 6, 10):
        _ran(root, refs["school-run"], day)
    _ran(root, refs["dog-walk"], 26)
    _ran(root, refs["dog-walk"], 28, "failed")
    return refs


# --- verify: survival is computed from the log alone --------------------------------------


def test_survival_is_computed_from_the_log_alone(tmp_path: Path) -> None:
    """The same log and ``as_of`` give the same report from the JSONL, a rebuilt index, an
    incremental index and the records shuffled: no state outside the log (constraint 5), no
    model (constraint 3)."""
    _household(tmp_path)
    as_of = T0 + timedelta(days=40)

    from_log = survival.survival(_records(tmp_path), as_of=as_of)
    report = rebuild(tmp_path, P)
    assert survival.survival(report.records, as_of=as_of) == from_log
    assert survival.survival(load_records(tmp_path, P), as_of=as_of) == from_log
    update(tmp_path, P)
    assert survival.survival(load_records(tmp_path, P), as_of=as_of) == from_log
    shuffled = list(_records(tmp_path))
    random.Random(97).shuffle(shuffled)
    assert survival.survival(shuffled, as_of=as_of) == from_log
    assert survival.survival(_records(tmp_path) * 2, as_of=as_of) == from_log  # a dup counts once

    assert len(from_log.cohort) == 4
    assert len(from_log.survived) == 2
    assert from_log.rate == 0.5


def test_what_the_log_learns_after_as_of_changes_nothing(tmp_path: Path) -> None:
    """A report as of day 40 is the same before and after the log grows past day 40."""
    refs = _household(tmp_path)
    before = _report(tmp_path, 40)
    _retire(tmp_path, refs["water-plants"], 41)
    _ran(tmp_path, refs["school-run"], 41)
    _add(tmp_path, "pay-rent", 41)
    assert _report(tmp_path, 40) == before


def test_the_report_has_no_clock_of_its_own(tmp_path: Path) -> None:
    """``as_of`` is an argument, never read inside: a naive one is refused, not guessed at."""
    with pytest.raises(ValueError, match="timezone"):
        survival.survival([], as_of=datetime(2026, 10, 1, 9, 0))


# --- verify: a stopped automation counts against it ---------------------------------------


def test_a_stopped_automation_counts_against_survival(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    report = _report(tmp_path, 40)

    retired = _verdict(report, refs["bin-night"])
    assert not retired.survived
    assert "retired" in retired.reason and refs["bin-night"] in report.cohort_refs
    silent = _verdict(report, refs["school-run"])
    assert not silent.survived
    assert "no ok run" in silent.reason
    assert silent.last_ok == _ts(10)
    assert {v.ref for v in report.stopped} == {refs["bin-night"], refs["school-run"]}
    assert report.rate == 0.5  # two of four, the stopped two in the denominator


def test_an_automation_that_only_failed_in_its_last_week_did_not_survive(tmp_path: Path) -> None:
    ref = _add(tmp_path, "pay-rent")
    _ran(tmp_path, ref, 20)
    for day in (24, 27, 30):
        _ran(tmp_path, ref, day, "failed")
    verdict = _verdict(_report(tmp_path, 31), ref)
    assert not verdict.survived
    assert "no ok run" in verdict.reason


def test_an_automation_retired_after_its_day_30_survived(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants")
    _ran(tmp_path, ref, 28)
    _retire(tmp_path, ref, 35)
    verdict = _verdict(_report(tmp_path, 40), ref)
    assert verdict.survived


def test_the_window_is_the_week_up_to_day_30_inclusive(tmp_path: Path) -> None:
    on_day_30 = _add(tmp_path, "water-plants")
    _ran(tmp_path, on_day_30, 30)
    just_before = _add(tmp_path, "bin-night")
    _ran(tmp_path, just_before, 23)  # exactly seven days before day 30: outside
    after = _add(tmp_path, "school-run")
    _ran(tmp_path, after, 30.5)  # after day 30: too late to count
    report = _report(tmp_path, 31)
    assert _verdict(report, on_day_30).survived
    assert not _verdict(report, just_before).survived
    assert not _verdict(report, after).survived
    assert survival.SURVIVAL_DAYS == 30 and survival.RECENT_DAYS == 7


def test_an_automation_younger_than_30_days_is_not_counted_yet(tmp_path: Path) -> None:
    old = _add(tmp_path, "water-plants")
    _ran(tmp_path, old, 29)
    young = _add(tmp_path, "bin-night", day=15)
    report = _report(tmp_path, 31)
    assert report.cohort_refs == (old,)
    assert [e.ref for e in report.not_yet] == [young]


def test_with_no_automation_at_day_30_the_rate_is_unknown_not_zero(tmp_path: Path) -> None:
    _add(tmp_path, "water-plants", day=10)
    report = _report(tmp_path, 20)
    assert report.cohort == ()
    assert report.rate is None
    assert "no automation has reached day 30" in report.summary()


def test_the_summary_says_how_many_survived_and_which_stopped(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    text = _report(tmp_path, 40).summary()
    assert "2 of 4" in text and "50%" in text
    assert f"'bin-night' ({refs['bin-night']})" in text
    assert f"'school-run' ({refs['school-run']})" in text


def test_an_add_the_register_refused_was_never_shipped(tmp_path: Path) -> None:
    """A machine-written add is not in the register (D72), so it is not in the cohort."""
    shipped = _add(tmp_path, "water-plants")
    _ran(tmp_path, shipped, 29)
    refused = _add(tmp_path, "car-service", actor=ACTOR_KERNEL)
    _ran(tmp_path, refused, 29)
    report = _report(tmp_path, 31)
    assert report.cohort_refs == (shipped,)
    assert any(u.ref == _ref_of_last_outcome(tmp_path) for u in report.unapplied)


def _ref_of_last_outcome(root: Path) -> str:
    return max((r.ref for r in _records(root) if isinstance(r.event, Outcome)), key=_seq)


def _seq(ref: str) -> int:
    return int(ref.rsplit(":", 1)[1])


def test_a_tombstoned_outcome_does_not_count(tmp_path: Path) -> None:
    """A supersede says the run it names did not happen as recorded: it is not counted."""
    ref = _add(tmp_path, "water-plants")
    wrong = _ran(tmp_path, ref, 29)
    _write(tmp_path, Supersede(ts=_ts(29.5), actor=P, target=wrong, reason="it never sent"))
    verdict = _verdict(_report(tmp_path, 31), ref)
    assert not verdict.survived
    assert verdict.last_ok is None


def test_an_outcome_naming_no_automation_is_reported(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants")
    stray = _ran(tmp_path, "d-mbp:99", 5)
    not_an_add = _ran(tmp_path, ref.replace(":1", ":2"), 5)  # names the first outcome
    report = _report(tmp_path, 31)
    assert [u.ref for u in report.unapplied] == [stray, not_an_add]
    assert all("not an automation" in u.reason for u in report.unapplied)


def test_a_second_devices_outcome_counts_like_any_other(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants")
    _ran(tmp_path, ref, 29, device="phone")
    assert _verdict(_report(tmp_path, 31), ref).survived


# --- recording an outcome ------------------------------------------------------------------


def test_record_appends_an_outcome_against_the_active_automation(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants")
    written = survival.record(tmp_path, P, D, "water-plants", "ok", note="sent", environ=ON)
    outcome = next(r for r in _records(tmp_path) if r.ref == written)
    assert isinstance(outcome.event, Outcome)
    assert outcome.event.target == ref
    assert outcome.event.result is OutcomeResult.OK
    assert outcome.event.actor == ACTOR_KERNEL  # the runner, by default
    assert outcome.event.note == "sent"


def test_a_person_may_record_an_outcome_too(tmp_path: Path) -> None:
    _add(tmp_path, "water-plants")
    written = survival.record(
        tmp_path, P, D, "water-plants", "failed", actor=ACTOR_USER, environ=ON
    )
    outcome = next(r for r in _records(tmp_path) if r.ref == written)
    assert outcome.event.actor == ACTOR_USER


def test_record_refuses_what_is_not_active_and_writes_nothing(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants")
    _retire(tmp_path, ref, 1)
    before = _records(tmp_path)
    for name in ("water-plants", "never-added"):
        with pytest.raises(survival.RefusedError, match="no active automation"):
            survival.record(tmp_path, P, D, name, "ok", environ=ON)
    assert _records(tmp_path) == before


def test_record_refuses_an_unknown_result_before_writing(tmp_path: Path) -> None:
    _add(tmp_path, "water-plants")
    before = _records(tmp_path)
    with pytest.raises(ValueError):
        survival.record(tmp_path, P, D, "water-plants", "stopped", environ=ON)
    assert _records(tmp_path) == before


def test_recording_for_a_principal_with_no_log_creates_nothing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        survival.record(tmp_path, "nobody", D, "water-plants", "ok", environ=ON)
    with pytest.raises(FileNotFoundError):
        survival.report(tmp_path, "nobody")
    assert list(tmp_path.iterdir()) == []


def test_the_event_shape() -> None:
    fields: dict[str, Any] = {"ts": _ts(0), "actor": ACTOR_KERNEL, "target": "d-mbp:1"}
    Outcome(**fields, result=OutcomeResult.FAILED)
    with pytest.raises(ValidationError):
        Outcome.model_validate(fields)  # a result is required
    with pytest.raises(ValidationError):
        Outcome.model_validate({"ts": _ts(0), "actor": ACTOR_KERNEL, "result": "ok"})  # a target
    with pytest.raises(ValidationError):
        Outcome.model_validate({**fields, "result": "ok", "survived": True})  # no such field
    assert {r.value for r in OutcomeResult} == {"ok", "failed"}


# --- the dormant rule ---------------------------------------------------------------------


def test_recording_outcomes_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Switch unset: nothing is recorded, and nothing is created on disk."""
    monkeypatch.delenv(survival.OUTCOMES_SWITCH, raising=False)
    assert not survival.enabled()
    with pytest.raises(survival.OutcomesOffError):
        survival.record(tmp_path, P, D, "water-plants", "ok")
    assert list(tmp_path.iterdir()) == []


def test_off_is_checked_before_the_log_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(survival.OUTCOMES_SWITCH, raising=False)
    _add(tmp_path, "water-plants")
    before = _records(tmp_path)
    with pytest.raises(survival.OutcomesOffError):
        survival.record(tmp_path, P, D, "water-plants", "ok")
    assert _records(tmp_path) == before


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "on\n"])
def test_only_the_one_spelling_turns_recording_on(value: str) -> None:
    assert not survival.enabled({survival.OUTCOMES_SWITCH: value})
    assert survival.enabled({survival.OUTCOMES_SWITCH: "on"})


def test_the_automations_switch_does_not_turn_recording_on() -> None:
    assert survival.OUTCOMES_SWITCH != "WALLE_AUTOMATIONS"
    assert not survival.enabled({"WALLE_AUTOMATIONS": "on"})


def test_reading_the_report_is_not_acting_and_works_while_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(survival.OUTCOMES_SWITCH, raising=False)
    refs = _household(tmp_path)
    report = survival.report(tmp_path, P, as_of=T0 + timedelta(days=40))
    assert report.cohort_refs == tuple(refs.values())


def test_nothing_in_the_log_can_turn_recording_on() -> None:
    assert not any(f in Outcome.model_fields for f in ("enabled", "on", "survived", "active"))
    tree = ast.parse(SOURCE.read_text())
    reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "environ" and isinstance(n.value, ast.Name)
    ]
    assert all(n.value.id == "os" for n in reads)  # type: ignore[union-attr]


def test_the_cli_gains_no_outcome_or_survival_command() -> None:
    from walle.cli import main

    assert not any("outcome" in name or "surviv" in name for name in main.commands)


# --- purity and the golden log -------------------------------------------------------------


def test_survival_reaches_no_model_and_no_network() -> None:
    assert forbidden_in_closure(PACKAGE, ("survival",)) == {}


def test_the_computation_reads_no_clock_and_opens_no_file() -> None:
    """``survival`` is a pure function of the records and ``as_of`` (constraint 3)."""
    tree = ast.parse(SOURCE.read_text())
    pure = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "survival"]
    assert len(pure) == 1
    names = {n.id for n in ast.walk(pure[0]) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(pure[0]) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "now_instant", "time", "os"}
    assert not attrs & {"read_text", "now", "environ", "today"}


def _golden_root(tmp_path: Path) -> tuple[Path, str]:
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):  # a read-only checkout must not fail the rebuild
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    return root, expected["principal"]


def test_survival_over_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    root, principal = _golden_root(tmp_path)
    as_of = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    report = rebuild(root, principal)
    from_index = survival.survival(load_records(root, principal), as_of=as_of)
    from_log = survival.survival(read_stream(root, principal, "mbp").records, as_of=as_of)
    assert survival.survival(report.records, as_of=as_of) == from_index == from_log

    assert [(v.ref, v.name, v.survived) for v in from_log.cohort] == [
        ("d-mbp:1", "water-plants", True),
        ("d-mbp:2", "bin-night", False),
        ("d-mbp:3", "school-run", False),
    ]
    assert [e.ref for e in from_log.not_yet] == ["d-mbp:4"]
    assert [u.ref for u in from_log.unapplied] == ["d-mbp:11"]
    assert from_log.rate == pytest.approx(1 / 3)


# --- issue #120 (audit 5.1): the register and the report agree across devices ----------------


def test_a_swap_from_the_laptop_keeps_the_phones_automation_and_its_runs(tmp_path: Path) -> None:
    """The audit's reproduction, through the real write paths. ``gym-class`` was added and
    ran on the phone; six more and a swap that retires it come from the laptop. Folded in
    stream order the swap missed, ``gym-class`` vanished from the register and its run was
    reported as naming nothing. In time order the retire lands, the automation is in the
    cohort as retired, and nothing is unapplied."""
    both = {**ON, automations.AUTOMATIONS_SWITCH: automations.SWITCH_ON}
    gym = automations.add(tmp_path, P, "phone", name="gym-class", title="Gym", environ=both)
    ran = survival.record(tmp_path, P, "phone", "gym-class", "ok", environ=both)
    for i in range(1, 7):
        automations.add(tmp_path, P, "mbp", name=f"a-{i}", title=f"A {i}", environ=both)
    automations.add(tmp_path, P, "mbp", name="a-7", title="A 7", retire="gym-class", environ=both)

    register = automations.fold_automations(_records(tmp_path))
    assert register.unapplied == ()
    assert [e.name for e in register.entries] == ["gym-class", *(f"a-{i}" for i in range(1, 8))]
    assert register.entry(gym.ref) is not None and register.entry(gym.ref).status == "retired"  # type: ignore[union-attr]

    report = survival.report(tmp_path, P, as_of=datetime(2027, 1, 1, tzinfo=UTC))
    assert report.unapplied == ()
    assert ran == "d-phone:2"
    verdict = _verdict(report, gym.ref)
    assert verdict.survived is False and "retired" in verdict.reason
    assert [e.ref for e in report.not_yet] == []
    assert len(report.cohort) == 8


def test_outcomes_on_two_devices_are_counted_in_time_order(tmp_path: Path) -> None:
    """Hand-written stamps: the add on the phone, the retire on the laptop, runs on both.
    Every run before the retire counts; the report is the same from any order of records."""
    added = _add(tmp_path, "gym-class", day=0, device="phone")
    _ran(tmp_path, added, day=5, device="mbp")
    _ran(tmp_path, added, day=12, device="phone")
    _retire(tmp_path, added, day=40)

    report = _report(tmp_path, 45)
    assert report.unapplied == ()
    verdict = _verdict(report, added)
    assert verdict.survived is False and verdict.last_ok == _ts(12)
    records = _records(tmp_path)
    as_of = T0 + timedelta(days=45)
    assert survival.survival(reversed(records), as_of=as_of) == report
    assert survival.survival([*records, *records], as_of=as_of) == report
