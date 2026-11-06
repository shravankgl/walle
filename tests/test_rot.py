"""Issue #96's verify criteria, and the cases around them (D8, D114, D72, the owner's three
decisions of 2026-11-06).

  never deletes          ``rot`` writes nothing: an archived automation is a label, it stays in
                         the register, and a person's ``restore`` brings it back to active
  pinned is never        a pinned automation is labelled active however long it is idle, and is
  labelled               never offered to make room
  dependencies hold      nothing a live automation names in ``depends_on`` is labelled stale or
                         archived, or offered to make room; ``depends_on`` refs are checked
                         against the log before anything is written
  pure                   ``rot`` is a function of the register, the records and an explicit
                         ``now``: folding it twice, in any order, gives the same answer
  off by default         nothing reads the log for labels unless the owner has set
                         ``WALLE_ROT=on``; ``restore`` is a register write, off with the register

Every automation, outcome and date here is synthetic: a made-up household, no real data.
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
from walle import automations, rot
from walle.events import (
    ACTOR_KERNEL,
    Automation,
    AutomationVerb,
    Event,
    Outcome,
    OutcomeResult,
    Supersede,
)
from walle.index import load_records, rebuild
from walle.paths import log_dir
from walle.reader import Record, read_file, read_stream
from walle.writer import StreamWriter

P = "ines"
D = "mbp"
ON = {automations.AUTOMATIONS_SWITCH: automations.SWITCH_ON}
ROT_ON = {rot.ROT_SWITCH: rot.SWITCH_ON}
BOTH = {**ON, **ROT_ON}
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "rot.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v2" / "automations"


# --- builders: hand-stamped events, so days can pass -------------------------------------


def _ts(days: float) -> str:
    return (T0 + timedelta(days=days)).isoformat(timespec="microseconds")


def _at(days: float) -> datetime:
    return T0 + timedelta(days=days)


def _write(root: Path, event: Event, device: str = D) -> str:
    written = StreamWriter(root, P, device).append(event)
    return f"d-{device}:{written.seq}"


def _add(
    root: Path, name: str, day: float = 0, *, depends_on: list[str] | None = None, device: str = D
) -> str:
    event = Automation(
        ts=_ts(day),
        actor=P,
        verb=AutomationVerb.ADD,
        name=name,
        title=f"synthetic {name}",
        depends_on=depends_on,
    )
    return _write(root, event, device)


def _verb(root: Path, verb: AutomationVerb, ref: str, day: float, actor: str = P) -> str:
    return _write(root, Automation(ts=_ts(day), actor=actor, verb=verb, target=ref))


def _ran(root: Path, ref: str, day: float, result: str = "ok") -> str:
    event = Outcome(ts=_ts(day), actor=ACTOR_KERNEL, target=ref, result=OutcomeResult(result))
    return _write(root, event)


def _records(root: Path) -> list[Record]:
    log = log_dir(root, P)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _rot(root: Path, day: float, **kw: Any) -> rot.Rot:
    records = _records(root)
    return rot.rot(automations.fold_automations(records), records, _at(day), **kw)


def _status(result: rot.Rot, ref: str) -> str:
    label = result.label(ref)
    assert label is not None
    return label.status


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` and its bytes: what "writes nothing" is checked against."""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _household(root: Path) -> dict[str, str]:
    """Five automations added on day 0, and what happened to each by day 120.

    water-plants  ran every week until day 119: active
    bin-night     pinned on day 1 and never ran again: pinned, never labelled
    school-run    last ran on day 70: stale at day 120 (idle 50 days)
    dog-walk      never ran: archived at day 120 (idle 120 days)
    car-service   never ran, but pay-rent names it in depends_on: held, active
    pay-rent      added on day 100, names car-service: active
    """
    refs = {n: _add(root, n) for n in ("water-plants", "bin-night", "school-run", "dog-walk")}
    refs["car-service"] = _add(root, "car-service")
    for day in range(7, 120, 7):
        _ran(root, refs["water-plants"], day)
    _verb(root, AutomationVerb.PIN, refs["bin-night"], 1)
    _ran(root, refs["school-run"], 70, "failed")
    refs["pay-rent"] = _add(root, "pay-rent", 100, depends_on=[refs["car-service"]])
    return refs


# --- verify: never deletes, only archives, and can be restored ----------------------------


def test_rot_labels_by_idle_time_and_writes_nothing(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    before = _tree(tmp_path)

    result = _rot(tmp_path, 120)

    assert _tree(tmp_path) == before  # not a byte written, no index, no lock file
    assert _status(result, refs["water-plants"]) == "active"
    assert _status(result, refs["school-run"]) == "stale"
    assert _status(result, refs["dog-walk"]) == "archived"
    assert [label.ref for label in result.archived] == [refs["dog-walk"]]
    assert [label.ref for label in result.stale] == [refs["school-run"]]


def test_an_archived_automation_stays_in_the_register(tmp_path: Path) -> None:
    """Archived is a label, not a transition: the register still holds it as active, and
    only a person's retire takes it out (owner's decision 1, D72)."""
    refs = _household(tmp_path)
    result = _rot(tmp_path, 120)
    register = automations.fold_automations(_records(tmp_path))

    assert _status(result, refs["dog-walk"]) == "archived"
    entry = register.entry(refs["dog-walk"])
    assert entry is not None and entry.status == "active"
    assert refs["dog-walk"] in [e.ref for e in register.active]
    assert len(result.labels) == len(register.active)


def test_a_restore_brings_an_archived_automation_back_and_resets_the_clock(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    assert _status(_rot(tmp_path, 120), refs["dog-walk"]) == "archived"

    restored = _verb(tmp_path, AutomationVerb.RESTORE, refs["dog-walk"], 121)

    assert _status(_rot(tmp_path, 122), refs["dog-walk"]) == "active"
    assert _status(_rot(tmp_path, 151), refs["dog-walk"]) == "stale"  # 30 days after restore
    label = _rot(tmp_path, 122).label(refs["dog-walk"])
    assert label is not None and label.last_activity == _ts(121)
    assert restored in label.reason


def test_restore_through_the_api_appends_one_person_written_event(tmp_path: Path) -> None:
    entry = automations.add(tmp_path, P, D, name="dog-walk", title="synthetic", environ=ON)
    before = len(_records(tmp_path))

    restored = automations.restore(tmp_path, P, D, "dog-walk", environ=ON)

    records = _records(tmp_path)
    assert len(records) == before + 1
    event = records[-1].event
    assert isinstance(event, Automation)
    assert event.verb is AutomationVerb.RESTORE and event.target == entry.ref
    assert event.actor == P or event.actor == "user"
    assert restored.ref == entry.ref and restored.status == "active"


@pytest.mark.parametrize("machine", [ACTOR_KERNEL, "curator", "observer"])
def test_restore_is_person_only(machine: str, tmp_path: Path) -> None:
    """Through the API it is refused before anything is written; written into the log by
    hand it is reported, and it does not reset the clock."""
    automations.add(tmp_path, P, D, name="dog-walk", title="synthetic", environ=ON)
    before = _tree(tmp_path)
    with pytest.raises(automations.RefusedError, match="not a person"):
        automations.restore(tmp_path, P, D, "dog-walk", actor=machine, environ=ON)
    assert _tree(tmp_path) == before

    root = tmp_path / "hand"
    added = _add(root, "dog-walk", 0)
    sneaky = _verb(root, AutomationVerb.RESTORE, added, 100, actor=machine)
    register = automations.fold_automations(_records(root))
    assert sneaky in [u.ref for u in register.unapplied]
    assert _status(_rot(root, 101), added) == "archived"


def test_a_restore_of_a_retired_automation_is_refused(tmp_path: Path) -> None:
    """Retire is how an automation leaves; adding it again is a new automation (#95 rule 1)."""
    automations.add(tmp_path, P, D, name="dog-walk", title="synthetic", environ=ON)
    automations.retire(tmp_path, P, D, "dog-walk", environ=ON)
    with pytest.raises(automations.RefusedError):
        automations.restore(tmp_path, P, D, "dog-walk", environ=ON)

    root = tmp_path / "hand"
    added = _add(root, "dog-walk", 0)
    _verb(root, AutomationVerb.RETIRE, added, 5)
    late = _verb(root, AutomationVerb.RESTORE, added, 6)
    register = automations.fold_automations(_records(root))
    assert late in [u.ref for u in register.unapplied]
    assert _rot(root, 10).labels == ()  # a retired automation is not labelled at all


def test_rot_has_no_way_to_retire_delete_or_write() -> None:
    """The module imports no writer and calls nothing that appends or removes."""
    tree = ast.parse(SOURCE.read_text())
    imported = {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names
    }
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "walle.writer" not in modules and "StreamWriter" not in imported
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not attrs & {"append", "unlink", "rmdir", "write_text", "write_bytes", "rename"}
    assert not names & {"retire", "open", "remove"}


# --- verify: pinned is never labelled -----------------------------------------------------


def test_a_pinned_automation_is_never_labelled(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    for day in (120, 365, 3650):
        label = _rot(tmp_path, day).label(refs["bin-night"])
        assert label is not None
        assert label.status == "active" and label.pinned
        assert "pinned" in label.reason


def test_an_unpin_starts_the_clock_from_the_unpin(tmp_path: Path) -> None:
    added = _add(tmp_path, "bin-night", 0)
    _verb(tmp_path, AutomationVerb.PIN, added, 1)
    _verb(tmp_path, AutomationVerb.UNPIN, added, 200)
    assert _status(_rot(tmp_path, 229), added) == "active"
    assert _status(_rot(tmp_path, 230), added) == "stale"


def test_a_pin_or_unpin_the_register_refused_does_not_move_the_clock(tmp_path: Path) -> None:
    added = _add(tmp_path, "bin-night", 0)
    refused = _verb(tmp_path, AutomationVerb.UNPIN, added, 80)  # not pinned: reported
    machine = _verb(tmp_path, AutomationVerb.PIN, added, 85, actor=ACTOR_KERNEL)
    register = automations.fold_automations(_records(tmp_path))
    assert {refused, machine} <= {u.ref for u in register.unapplied}
    assert _status(_rot(tmp_path, 91), added) == "archived"


# --- verify: nothing a live automation depends on is labelled or offered -----------------


def test_what_a_live_automation_names_is_never_labelled(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    label = _rot(tmp_path, 400).label(refs["car-service"])
    assert label is not None
    assert label.status == "active"
    assert label.held_by == (refs["pay-rent"],)
    assert refs["pay-rent"] in label.reason


def test_once_the_one_that_names_it_is_retired_it_ages_like_any_other(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    _verb(tmp_path, AutomationVerb.RETIRE, refs["pay-rent"], 130)
    label = _rot(tmp_path, 131).label(refs["car-service"])
    assert label is not None and label.status == "archived" and label.held_by == ()


def test_the_wip_suggestion_never_offers_what_a_live_automation_names(tmp_path: Path) -> None:
    """car-service is the oldest unpinned by add, but pay-rent names it: the D6 rule skips it,
    with the rot switch on or off."""
    target = automations.add(tmp_path, P, D, name="car-service", title="synthetic", environ=ON)
    automations.add(
        tmp_path, P, D, name="pay-rent", title="synthetic", depends_on=[target.ref], environ=ON
    )
    for i in range(5):
        automations.add(tmp_path, P, D, name=f"a-{i}", title="synthetic", environ=ON)
    register = automations.fold_automations(_records(tmp_path))
    assert register.full
    assert register.suggestion is not None and register.suggestion.name == "pay-rent"
    for env in (ON, BOTH):
        with pytest.raises(automations.CapReachedError) as raised:
            automations.add(tmp_path, P, D, name="extra", title="synthetic", environ=env)
        assert raised.value.suggestion is not None
        assert raised.value.suggestion.ref != target.ref
        assert "Named by a live automation, so not offered: 'car-service'" in str(raised.value)


def test_with_rot_on_the_cap_refusal_offers_archived_first(tmp_path: Path) -> None:
    """Seven automations, the oldest by add still running weekly, one archived and one stale.
    With ``WALLE_ROT`` off the refusal offers the oldest by add (D6); on, the archived one,
    and with none archived, the stale one (owner's decision 1)."""
    now = datetime.now(UTC)
    days = lambda n: (now - timedelta(days=n)).isoformat(timespec="microseconds")  # noqa: E731
    stamps = [days(200), days(150), days(140), days(10), days(9), days(8), days(7)]
    names = ["water-plants", "dog-walk", "school-run", "a-1", "a-2", "a-3", "a-4"]
    refs = {}
    for name, ts in zip(names, stamps, strict=True):
        refs[name] = _write(
            tmp_path,
            Automation(ts=ts, actor=P, verb=AutomationVerb.ADD, name=name, title="synthetic"),
        )
    _write(tmp_path, Outcome(ts=days(1), actor=ACTOR_KERNEL, target=refs["water-plants"],
                             result=OutcomeResult.OK))  # fmt: skip
    _write(tmp_path, Outcome(ts=days(40), actor=ACTOR_KERNEL, target=refs["school-run"],
                             result=OutcomeResult.OK))  # fmt: skip

    with pytest.raises(automations.CapReachedError) as off:
        automations.add(tmp_path, P, D, name="extra", title="synthetic", environ=ON)
    assert off.value.suggestion is not None and off.value.suggestion.name == "water-plants"

    with pytest.raises(automations.CapReachedError) as on:
        automations.add(tmp_path, P, D, name="extra", title="synthetic", environ=BOTH)
    assert on.value.suggestion is not None and on.value.suggestion.name == "dog-walk"
    assert "archived" in str(on.value)

    automations.restore(tmp_path, P, D, "dog-walk", environ=ON)
    with pytest.raises(automations.CapReachedError) as stale:
        automations.add(tmp_path, P, D, name="extra", title="synthetic", environ=BOTH)
    assert stale.value.suggestion is not None and stale.value.suggestion.name == "school-run"
    assert "stale" in str(stale.value)


# --- depends_on: checked against the log before any write ---------------------------------


def test_depends_on_naming_nothing_in_the_log_is_refused_before_any_write(tmp_path: Path) -> None:
    automations.add(tmp_path, P, D, name="car-service", title="synthetic", environ=ON)
    before = _tree(tmp_path)
    with pytest.raises(automations.RefusedError, match="d-mbp:99"):
        automations.add(
            tmp_path, P, D, name="pay-rent", title="synthetic", depends_on=["d-mbp:99"], environ=ON
        )
    assert _tree(tmp_path) == before


def test_a_swap_with_a_bad_dependency_writes_neither_half(tmp_path: Path) -> None:
    """The retire half of a swap is the first write, so the check has to come before it."""
    for i in range(7):
        automations.add(tmp_path, P, D, name=f"a-{i}", title="synthetic", environ=ON)
    before = _tree(tmp_path)
    with pytest.raises(automations.RefusedError, match="d-phone:1"):
        automations.add(
            tmp_path,
            P,
            D,
            name="pay-rent",
            title="synthetic",
            retire="a-0",
            depends_on=["d-phone:1"],
            environ=ON,
        )
    assert _tree(tmp_path) == before


def test_depends_on_is_written_on_the_add_and_read_back_on_the_entry(tmp_path: Path) -> None:
    car = automations.add(tmp_path, P, D, name="car-service", title="synthetic", environ=ON)
    pay = automations.add(
        tmp_path, P, D, name="pay-rent", title="synthetic", depends_on=[car.ref], environ=ON
    )
    assert pay.depends_on == (car.ref,)
    register = automations.fold_automations(_records(tmp_path))
    entry = register.entry(pay.ref)
    assert entry is not None and entry.depends_on == (car.ref,)
    line = json.loads((log_dir(tmp_path, P) / "d-mbp.jsonl").read_text().splitlines()[-1])
    assert line["depends_on"] == [car.ref]
    first = json.loads((log_dir(tmp_path, P) / "d-mbp.jsonl").read_text().splitlines()[0])
    assert "depends_on" not in first  # an add without dependencies writes no new bytes


def test_depends_on_may_name_any_event_in_the_principals_log(tmp_path: Path) -> None:
    """A ref is a ref: a kept workflow or a fact may be depended on as well as an automation.
    Only an automation it names is held, because only automations are labelled."""
    other = _write(tmp_path, Outcome(ts=_ts(0), actor=P, target="d-mbp:1", result=OutcomeResult.OK))
    pay = automations.add(
        tmp_path, P, D, name="pay-rent", title="synthetic", depends_on=[other], environ=ON
    )
    assert pay.depends_on == (other,)


def test_a_hand_written_add_with_a_dangling_or_self_dependency_is_reported(tmp_path: Path) -> None:
    dangling = _add(tmp_path, "pay-rent", 0, depends_on=["d-phone:4"])
    selfish = _add(tmp_path, "car-service", 0, depends_on=["d-mbp:2"])  # its own ref
    register = automations.fold_automations(_records(tmp_path))
    reasons = {u.ref: u.reason for u in register.unapplied}
    assert dangling in reasons and "d-phone:4" in reasons[dangling]
    assert selfish in reasons and "itself" in reasons[selfish]
    assert register.active == ()


def test_depends_on_is_an_add_field_only_and_names_each_ref_once() -> None:
    ts = _ts(0)
    with pytest.raises(ValidationError, match="does not carry"):
        Automation(
            ts=ts, actor=P, verb=AutomationVerb.PIN, target="d-mbp:1", depends_on=["d-mbp:2"]
        )
    with pytest.raises(ValidationError, match="twice"):
        Automation(
            ts=ts,
            actor=P,
            verb=AutomationVerb.ADD,
            name="a",
            title="t",
            depends_on=["d-mbp:1", "d-mbp:1"],
        )
    with pytest.raises(ValidationError, match="empty"):
        Automation(ts=ts, actor=P, verb=AutomationVerb.ADD, name="a", title="t", depends_on=[])
    with pytest.raises(ValidationError):
        Automation(ts=ts, actor=P, verb=AutomationVerb.ADD, name="a", title="t", depends_on=["x"])


def test_restore_carries_only_its_target() -> None:
    ts = _ts(0)
    Automation(ts=ts, actor=P, verb=AutomationVerb.RESTORE, target="d-mbp:1")
    with pytest.raises(ValidationError, match="needs"):
        Automation(ts=ts, actor=P, verb=AutomationVerb.RESTORE)
    with pytest.raises(ValidationError, match="does not carry"):
        Automation(ts=ts, actor=P, verb=AutomationVerb.RESTORE, target="d-mbp:1", name="a")


# --- the schema: restore is a new verb, so automation is v2 (D88, D96) ---------------------


def test_automation_is_at_v2_and_a_v1_line_still_loads() -> None:
    assert Automation.SCHEMA_VERSION == 2
    old = Automation.model_validate(
        {"ts": _ts(0), "actor": P, "verb": "add", "name": "a", "title": "t", "v": 1, "seq": 1}
    )
    assert old.v == 2 and old.depends_on is None
    fresh = Automation(ts=_ts(0), actor=P, verb=AutomationVerb.RESTORE, target="d-mbp:1")
    assert fresh.v == 2


@pytest.mark.parametrize(
    "line",
    [
        {"verb": "restore", "target": "d-mbp:1"},
        {"verb": "add", "name": "a", "title": "t", "depends_on": ["d-mbp:1"]},
    ],
    ids=["restore", "depends_on"],
)
def test_a_v1_line_cannot_carry_what_v1_never_had(line: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="v1"):
        Automation.model_validate({"ts": _ts(0), "actor": P, "v": 1, **line})


# --- pure: a function of the register, the records and now --------------------------------


def test_folding_rot_twice_gives_the_same_answer(tmp_path: Path) -> None:
    """The same log and ``now`` give the same labels: called twice, over the records
    shuffled and duplicated, and over a register folded again from them."""
    _household(tmp_path)
    records = _records(tmp_path)
    now = _at(120)
    register = automations.fold_automations(records)

    first = rot.rot(register, records, now)
    second = rot.rot(register, records, now)
    assert first == second

    shuffled = [*records, *records]
    random.Random(96).shuffle(shuffled)
    again = rot.rot(automations.fold_automations(shuffled), shuffled, now)
    assert again == first
    rebuilt = rebuild(tmp_path, P).records
    assert rot.rot(automations.fold_automations(rebuilt), rebuilt, now) == first
    indexed = load_records(tmp_path, P)
    assert rot.rot(automations.fold_automations(indexed), indexed, now) == first


def test_rot_has_no_clock_of_its_own() -> None:
    with pytest.raises(ValueError, match="timezone"):
        rot.rot(automations.Register(entries=()), [], datetime(2026, 10, 1, 9, 0))


def test_rot_refuses_a_register_from_other_records(tmp_path: Path) -> None:
    _household(tmp_path)
    register = automations.fold_automations(_records(tmp_path))
    with pytest.raises(ValueError, match="register"):
        rot.rot(register, [], _at(120))


def test_events_after_now_do_not_move_the_clock(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    before = _rot(tmp_path, 120)
    _ran(tmp_path, refs["dog-walk"], 125)
    _verb(tmp_path, AutomationVerb.RESTORE, refs["school-run"], 126)
    assert _rot(tmp_path, 120).labels == before.labels


def test_the_thresholds_are_30_and_90_days_inclusive(tmp_path: Path) -> None:
    added = _add(tmp_path, "dog-walk", 0)
    assert rot.STALE_AFTER.days == 30
    assert rot.ARCHIVE_AFTER.days == 90
    assert _status(_rot(tmp_path, 29.999), added) == "active"
    assert _status(_rot(tmp_path, 30), added) == "stale"
    assert _status(_rot(tmp_path, 89.999), added) == "stale"
    assert _status(_rot(tmp_path, 90), added) == "archived"


def test_the_thresholds_are_configurable(tmp_path: Path) -> None:
    added = _add(tmp_path, "dog-walk", 0)
    short = {"stale_after": timedelta(days=7), "archive_after": timedelta(days=14)}
    assert _status(_rot(tmp_path, 8, **short), added) == "stale"
    assert _status(_rot(tmp_path, 15, **short), added) == "archived"


@pytest.mark.parametrize(
    ("stale", "archive"), [(0, 90), (-1, 90), (30, 30), (90, 30)], ids=["zero", "neg", "eq", "inv"]
)
def test_thresholds_that_cannot_order_the_labels_are_refused(stale: int, archive: int) -> None:
    with pytest.raises(ValueError, match="stale_after"):
        rot.rot(
            automations.Register(entries=()),
            [],
            _at(0),
            stale_after=timedelta(days=stale),
            archive_after=timedelta(days=archive),
        )


def test_any_outcome_ok_or_failed_resets_the_clock_but_a_tombstoned_one_does_not(
    tmp_path: Path,
) -> None:
    a = _add(tmp_path, "a", 0)
    b = _add(tmp_path, "b", 0)
    _ran(tmp_path, a, 80, "failed")
    gone = _ran(tmp_path, b, 80)
    _write(tmp_path, Supersede(ts=_ts(81), actor=P, target=gone))
    result = _rot(tmp_path, 100)
    assert _status(result, a) == "active"
    assert _status(result, b) == "archived"


def test_the_computation_reads_no_clock_and_opens_no_file() -> None:
    tree = ast.parse(SOURCE.read_text())
    pure = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "rot"]
    assert len(pure) == 1
    names = {n.id for n in ast.walk(pure[0]) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(pure[0]) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "now_instant", "time", "os", "_read"}
    assert not attrs & {"read_text", "now", "environ", "today", "glob"}


def test_rot_reaches_no_model_and_no_network() -> None:
    assert forbidden_in_closure(PACKAGE, ("rot",)) == {}


# --- the dormant rule ---------------------------------------------------------------------


def test_rot_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Switch unset: ``labels`` refuses before it reads the log, and nothing is created."""
    monkeypatch.delenv(rot.ROT_SWITCH, raising=False)
    assert not rot.enabled()
    with pytest.raises(rot.RotOffError):
        rot.labels(tmp_path, P, now=_at(0))
    assert list(tmp_path.iterdir()) == []


def test_off_is_checked_before_the_log_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(rot.ROT_SWITCH, raising=False)
    _household(tmp_path)
    monkeypatch.setattr(rot, "_read", lambda *a: pytest.fail("read the log while off"))
    with pytest.raises(rot.RotOffError):
        rot.labels(tmp_path, P, now=_at(120))


def test_labels_reads_the_log_once_the_owner_turns_it_on(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    before = _tree(tmp_path)
    result = rot.labels(tmp_path, P, now=_at(120), environ=ROT_ON)
    assert result == _rot(tmp_path, 120)
    assert _status(result, refs["dog-walk"]) == "archived"
    assert _tree(tmp_path) == before
    with pytest.raises(FileNotFoundError):
        rot.labels(tmp_path, "nobody", now=_at(0), environ=ROT_ON)


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "on\n"])
def test_only_the_one_spelling_turns_rot_on(value: str) -> None:
    assert not rot.enabled({rot.ROT_SWITCH: value})
    assert rot.enabled({rot.ROT_SWITCH: "on"})


def test_the_register_switch_does_not_turn_rot_on() -> None:
    assert rot.ROT_SWITCH not in (automations.AUTOMATIONS_SWITCH, "WALLE_OUTCOMES")
    assert not rot.enabled(ON)


def test_restore_is_off_with_the_register(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(automations.AUTOMATIONS_SWITCH, raising=False)
    with pytest.raises(automations.AutomationsOffError):
        automations.restore(tmp_path, P, D, "dog-walk")
    with pytest.raises(automations.AutomationsOffError):
        automations.restore(tmp_path, P, D, "dog-walk", environ=ROT_ON)
    assert list(tmp_path.iterdir()) == []


def test_nothing_in_the_log_can_turn_rot_on() -> None:
    fields = set(Automation.model_fields) | set(Outcome.model_fields)
    assert not fields & {"enabled", "on", "stale", "archived", "status", "rot"}
    tree = ast.parse(SOURCE.read_text())
    reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "environ" and isinstance(n.value, ast.Name)
    ]
    assert all(n.value.id == "os" for n in reads)  # type: ignore[union-attr]


def test_the_cli_gains_no_rot_or_restore_command() -> None:
    from walle.cli import main

    assert not any(w in name for name in main.commands for w in ("rot", "restore", "stale"))


def test_nothing_in_the_package_calls_rot_on_its_own() -> None:
    """Only ``automations.add``'s cap refusal reads the labels, and only while ``WALLE_ROT``
    is on, and the model maintenance pass (#98), which runs ``rot`` first and is itself off
    by default and called by nothing; no other module imports ``walle.rot``."""
    users = sorted(
        p.name
        for p in PACKAGE.rglob("*.py")
        if p.name != "rot.py"
        and any(
            (isinstance(n, ast.ImportFrom) and (n.module == "walle.rot" or (
                n.module == "walle" and any(a.name == "rot" for a in n.names))))
            for n in ast.walk(ast.parse(p.read_text()))
        )
    )  # fmt: skip
    assert users == ["automations.py", "maintenance.py"]


# --- the golden log at v2 -----------------------------------------------------------------


def _golden_root(tmp_path: Path) -> tuple[Path, str]:
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    return root, expected["principal"]


def test_rot_over_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    root, principal = _golden_root(tmp_path)
    now = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    from_log = read_stream(root, principal, "mbp").records
    from_index = rebuild(root, principal).records
    results = [rot.rot(automations.fold_automations(r), r, now) for r in (from_log, from_index)]
    assert results[0] == results[1]
    assert [(label.name, label.status) for label in results[0].labels] == [
        ("water-plants", "active"),
        ("bin-night", "active"),
        ("school-run", "stale"),
        ("dog-walk", "active"),
        ("car-service", "active"),
        ("piano-practice", "archived"),
        ("pay-rent", "active"),
    ]
