"""Issue #95's verify criteria, and the cases around them (D6, D114, D72).

  an eighth is refused   with seven active, an eighth add writes nothing and names which one
                         to retire: the oldest unpinned; retiring it, or swapping it out in the
                         same hold, makes room; an eighth that reached the log some other way
                         is reported, never applied
  rebuild stays green    the register is a fold over the log: the same from the JSONL, from a
                         rebuilt index and from an incremental one, over a golden log
  pinned is honoured     a pinned automation is never offered for retirement, cannot be retired
                         or swapped out until a person unpins it, and no tombstone removes it
  human-only (D72)       no machine actor adds, retires, pins or unpins, through the API or by a
                         line written into the log
  off by default         nothing writes unless the owner has set the switch, outside the log

Every automation here is synthetic: a made-up household, no real data and no real tool.
"""

from __future__ import annotations

import ast
import json
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from test_constraints import forbidden_in_closure
from walle import automations
from walle.events import (
    ACTOR_CURATOR,
    ACTOR_KERNEL,
    ACTOR_MCP,
    ACTOR_OBSERVER,
    ACTOR_PROPOSER,
    ACTOR_USER,
    Automation,
    AutomationVerb,
    Event,
    Supersede,
    now_instant,
)
from walle.index import load_records, rebuild, update
from walle.paths import automations_lock_path, log_dir
from walle.reader import Record, read_file, read_stream
from walle.writer import StreamWriter

P = "ines"
D = "mbp"
ON = {automations.AUTOMATIONS_SWITCH: automations.SWITCH_ON}
TS = "2026-11-05T09:00:00.000000+00:00"
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "automations.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v1" / "automations"
MACHINES = (ACTOR_KERNEL, ACTOR_OBSERVER, ACTOR_PROPOSER, ACTOR_CURATOR, ACTOR_MCP)
NAMES = (
    "water-plants",
    "bin-night",
    "school-run",
    "piano-practice",
    "pay-rent",
    "dog-walk",
    "library-books",
)


# --- builders -----------------------------------------------------------------------------


def _add(root: Path, name: str, **extra: Any) -> automations.Entry:
    extra.setdefault("title", f"synthetic {name}")
    return automations.add(root, P, D, name=name, environ=ON, **extra)


def _fill(root: Path) -> list[automations.Entry]:
    """Seven active automations, the cap, added in ``NAMES`` order."""
    return [_add(root, name) for name in NAMES]


def _records(root: Path) -> list[Record]:
    log = log_dir(root, P)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _register(root: Path) -> automations.Register:
    return automations.fold_automations(_records(root))


def _automation_events(root: Path) -> list[Automation]:
    return [r.event for r in _records(root) if isinstance(r.event, Automation)]


def _write(root: Path, device: str, event: Event) -> str:
    """Append bypassing the register: a second device's log, or a hand-written line."""
    written = StreamWriter(root, P, device).append(event)
    return f"d-{device}:{written.seq}"


def _raw(verb: AutomationVerb, **extra: Any) -> Automation:
    fields: dict[str, Any] = {"ts": TS, "actor": P, "verb": verb}
    fields.update(extra)
    return Automation(**fields)


def _raw_add(name: str, **extra: Any) -> Automation:
    return _raw(AutomationVerb.ADD, name=name, title=f"synthetic {name}", **extra)


# --- verify: an eighth is refused and says which to retire ----------------------------------


def test_an_eighth_is_refused_and_says_which_to_retire(tmp_path: Path) -> None:
    first, *_ = _fill(tmp_path)
    before = _automation_events(tmp_path)

    with pytest.raises(automations.CapReachedError) as refused:
        _add(tmp_path, "car-service")

    message = str(refused.value)
    assert "7 of 7" in message
    assert "car-service" in message
    assert f"'{first.name}' ({first.ref})" in message
    assert refused.value.suggestion == first
    assert _automation_events(tmp_path) == before  # nothing written
    assert len(_register(tmp_path).active) == automations.WIP_CAP == 7


def test_the_suggestion_is_the_oldest_unpinned_by_when_it_was_added(tmp_path: Path) -> None:
    """Oldest by the add's ``ts``, not by ref: a second device's earlier add sorts later by
    ref (``d-phone`` after ``d-mbp``) but is the older automation."""
    _write(tmp_path, "phone", _raw_add("water-plants", ts="2026-11-01T08:00:00.000000+00:00"))
    for i, name in enumerate(NAMES[1:], start=2):
        _write(tmp_path, "mbp", _raw_add(name, ts=f"2026-11-0{i}T08:00:00.000000+00:00"))
    register = _register(tmp_path)
    assert register.full
    assert register.suggestion is not None
    assert register.suggestion.ref == "d-phone:1"


def test_retiring_the_suggested_one_makes_room_for_the_eighth(tmp_path: Path) -> None:
    _fill(tmp_path)
    with pytest.raises(automations.CapReachedError) as refused:
        _add(tmp_path, "car-service")
    assert refused.value.suggestion is not None

    gone = automations.retire(tmp_path, P, D, refused.value.suggestion.name, environ=ON)
    added = _add(tmp_path, "car-service")

    register = _register(tmp_path)
    assert gone.status == "retired"
    assert [e.name for e in register.active] == [*NAMES[1:], "car-service"]
    assert register.find("car-service") == added
    assert [e.name for e in register.retired] == ["water-plants"]


def test_an_add_that_retires_one_swaps_in_a_single_hold(tmp_path: Path) -> None:
    _fill(tmp_path)
    added = _add(tmp_path, "car-service", retire="bin-night")

    events = _automation_events(tmp_path)
    assert [e.verb for e in events[-2:]] == [AutomationVerb.RETIRE, AutomationVerb.ADD]
    register = _register(tmp_path)
    assert len(register.active) == 7
    assert register.find("bin-night") is None
    assert register.find("car-service") == added


def test_an_eighth_written_into_the_log_by_hand_is_reported_not_applied(tmp_path: Path) -> None:
    _fill(tmp_path)
    eighth = _write(tmp_path, "mbp", _raw_add("car-service"))

    register = _register(tmp_path)
    assert len(register.active) == 7
    assert register.find("car-service") is None
    reasons = {u.ref: u.reason for u in register.unapplied}
    assert "past the cap" in reasons[eighth]
    assert "water-plants" in reasons[eighth]  # even the fold says which to retire


def test_two_runners_adding_the_seventh_at_once_give_one_seventh(tmp_path: Path) -> None:
    """Runners A and B each add a seventh automation at the same moment. One is added; the
    other waits for ``automations.lock``, reads the log only then, finds it full and writes
    nothing."""
    for name in NAMES[:6]:
        _add(tmp_path, name)
    barrier = threading.Barrier(2)
    added: list[automations.Entry] = []
    refused: list[automations.CapReachedError] = []
    errors: list[BaseException] = []

    def runner(name: str, device: str) -> None:
        try:
            barrier.wait()
            added.append(automations.add(tmp_path, P, device, name=name, title=name, environ=ON))
        except automations.CapReachedError as exc:
            refused.append(exc)
        except BaseException as exc:  # reported below, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=runner, args=("car-service", "mbp")),
        threading.Thread(target=runner, args=("gym-class", "phone")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    assert len(added) == 1
    assert len(refused) == 1
    assert len(_automation_events(tmp_path)) == 7
    assert len(_register(tmp_path).active) == 7
    assert _register(tmp_path).unapplied == ()


def test_two_devices_that_never_shared_a_lock_still_fold_to_seven(tmp_path: Path) -> None:
    """Two devices each added a seventh before their logs met. The first in ref order is
    added on every machine; the other is reported as past the cap, not dropped."""
    for name in NAMES[:6]:
        _write(tmp_path, "mbp", _raw_add(name))
    on_mbp = _write(tmp_path, "mbp", _raw_add("car-service"))
    on_phone = _write(tmp_path, "phone", _raw_add("gym-class"))

    register = _register(tmp_path)
    assert len(register.active) == 7
    assert register.find("car-service") is not None
    assert register.find("car-service").ref == on_mbp  # type: ignore[union-attr]
    assert [u.ref for u in register.unapplied] == [on_phone]


def test_the_fold_gives_the_same_register_in_any_order(tmp_path: Path) -> None:
    _fill(tmp_path)
    automations.pin(tmp_path, P, D, "pay-rent", environ=ON)
    automations.retire(tmp_path, P, D, "bin-night", environ=ON)
    records = _records(tmp_path)
    assert automations.fold_automations(reversed(records)) == automations.fold_automations(records)
    assert automations.fold_automations([*records, *records]) == automations.fold_automations(
        records
    )


# --- verify: pinned is honoured ----------------------------------------------------------------


def test_a_pinned_automation_is_never_offered_for_retirement(tmp_path: Path) -> None:
    _fill(tmp_path)
    automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    automations.pin(tmp_path, P, D, "bin-night", environ=ON)

    with pytest.raises(automations.CapReachedError) as refused:
        _add(tmp_path, "car-service")

    assert refused.value.suggestion is not None
    assert refused.value.suggestion.name == "school-run"
    assert [e.name for e in refused.value.pinned] == ["water-plants", "bin-night"]
    assert "pinned, so not offered" in str(refused.value).lower()


def test_when_every_active_automation_is_pinned_the_refusal_says_to_unpin_first(
    tmp_path: Path,
) -> None:
    _fill(tmp_path)
    for name in NAMES:
        automations.pin(tmp_path, P, D, name, environ=ON)

    with pytest.raises(automations.CapReachedError) as refused:
        _add(tmp_path, "car-service")

    assert refused.value.suggestion is None
    assert "every active automation is pinned" in str(refused.value)
    assert "unpin" in str(refused.value)


def test_a_pinned_automation_cannot_be_retired_until_a_person_unpins_it(tmp_path: Path) -> None:
    _add(tmp_path, "water-plants")
    automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    before = _automation_events(tmp_path)

    with pytest.raises(automations.RefusedError, match="pinned"):
        automations.retire(tmp_path, P, D, "water-plants", environ=ON)
    assert _automation_events(tmp_path) == before

    automations.unpin(tmp_path, P, D, "water-plants", environ=ON)
    assert automations.retire(tmp_path, P, D, "water-plants", environ=ON).status == "retired"


def test_a_swap_naming_a_pinned_automation_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    _fill(tmp_path)
    automations.pin(tmp_path, P, D, "bin-night", environ=ON)
    before = _automation_events(tmp_path)

    with pytest.raises(automations.RefusedError, match="pinned"):
        _add(tmp_path, "car-service", retire="bin-night")

    assert _automation_events(tmp_path) == before


def test_a_retire_of_a_pinned_automation_written_into_the_log_is_reported(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants").ref
    automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    bad = _write(tmp_path, "mbp", _raw(AutomationVerb.RETIRE, target=ref))

    register = _register(tmp_path)
    assert register.find("water-plants") is not None
    assert "pinned" in {u.ref: u.reason for u in register.unapplied}[bad]


def test_no_tombstone_removes_an_automation(tmp_path: Path) -> None:
    """A ``supersede`` of an automation event is reported and changes nothing: retire is how
    an automation leaves, so a pinned one cannot be made to leave by a tombstone (D114)."""
    entry = _add(tmp_path, "water-plants")
    pin = automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    assert pin.pin_ref is not None
    tombstones = [
        _write(tmp_path, "mbp", Supersede(ts=TS, actor=P, target=entry.ref)),
        _write(tmp_path, "mbp", Supersede(ts=TS, actor=P, target=pin.pin_ref)),
    ]

    register = _register(tmp_path)
    found = register.find("water-plants")
    assert found is not None
    assert found.pinned
    reasons = {u.ref: u.reason for u in register.unapplied}
    assert sorted(reasons) == sorted(tombstones)
    assert all("retire" in reason for reason in reasons.values())


def test_pin_and_unpin_are_reported_when_they_change_nothing(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants").ref
    automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    twice = _write(tmp_path, "mbp", _raw(AutomationVerb.PIN, target=ref))
    with pytest.raises(automations.RefusedError, match="already pinned"):
        automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    automations.unpin(tmp_path, P, D, "water-plants", environ=ON)
    with pytest.raises(automations.RefusedError, match="not pinned"):
        automations.unpin(tmp_path, P, D, "water-plants", environ=ON)
    assert "already pinned" in {u.ref: u.reason for u in _register(tmp_path).unapplied}[twice]


# --- human-only (D72) -------------------------------------------------------------------------


@pytest.mark.parametrize("machine", MACHINES)
def test_no_machine_actor_can_add_retire_pin_or_unpin(machine: str, tmp_path: Path) -> None:
    _add(tmp_path, "water-plants")
    automations.pin(tmp_path, P, D, "water-plants", environ=ON)
    before = _automation_events(tmp_path)
    calls = [
        lambda: _add(tmp_path, "bin-night", actor=machine),
        lambda: automations.retire(tmp_path, P, D, "water-plants", actor=machine, environ=ON),
        lambda: automations.pin(tmp_path, P, D, "water-plants", actor=machine, environ=ON),
        lambda: automations.unpin(tmp_path, P, D, "water-plants", actor=machine, environ=ON),
    ]
    for call in calls:
        with pytest.raises(automations.RefusedError, match="person"):
            call()
    assert _automation_events(tmp_path) == before


@pytest.mark.parametrize("machine", MACHINES)
def test_a_machine_actor_is_refused_before_any_lock_or_folder_exists(
    machine: str, tmp_path: Path
) -> None:
    with pytest.raises(automations.RefusedError, match="person"):
        _add(tmp_path, "water-plants", actor=machine)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("machine", MACHINES)
def test_a_machine_written_automation_event_is_reported_not_applied(
    machine: str, tmp_path: Path
) -> None:
    ref = _add(tmp_path, "water-plants").ref
    lines = [
        _write(tmp_path, "mbp", _raw_add("bin-night", actor=machine)),
        _write(tmp_path, "mbp", _raw(AutomationVerb.PIN, target=ref, actor=machine)),
        _write(tmp_path, "mbp", _raw(AutomationVerb.RETIRE, target=ref, actor=machine)),
    ]
    register = _register(tmp_path)
    assert [e.name for e in register.active] == ["water-plants"]
    assert not register.active[0].pinned
    reasons = {u.ref: u.reason for u in register.unapplied}
    assert sorted(reasons) == sorted(lines)
    assert all("person" in reason for reason in reasons.values())


# --- names, targets and shapes -------------------------------------------------------------


def test_a_name_already_active_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    first = _add(tmp_path, "water-plants")
    with pytest.raises(automations.RefusedError, match=first.ref):
        _add(tmp_path, "water-plants")
    assert len(_automation_events(tmp_path)) == 1


def test_a_retired_name_can_be_added_again_as_a_new_automation(tmp_path: Path) -> None:
    old = _add(tmp_path, "water-plants")
    automations.retire(tmp_path, P, D, "water-plants", note="away for a month", environ=ON)
    new = _add(tmp_path, "water-plants")
    register = _register(tmp_path)
    assert new.ref != old.ref
    assert [e.ref for e in register.active] == [new.ref]
    assert [e.ref for e in register.retired] == [old.ref]
    assert register.retired[0].retired_ref is not None


def test_retiring_or_pinning_what_is_not_active_is_refused(tmp_path: Path) -> None:
    _add(tmp_path, "water-plants")
    for call in (automations.retire, automations.pin, automations.unpin):
        with pytest.raises(automations.RefusedError, match="no active automation"):
            call(tmp_path, P, D, "car-service", environ=ON)
    with pytest.raises(automations.RefusedError, match="no active automation"):
        _add(tmp_path, "bin-night", retire="car-service")


def test_verbs_naming_what_is_not_an_add_are_reported(tmp_path: Path) -> None:
    obs = _add(tmp_path, "water-plants").ref
    lines = [
        _write(tmp_path, "mbp", _raw(AutomationVerb.RETIRE, target="d-mbp:99")),
        _write(tmp_path, "mbp", _raw(AutomationVerb.PIN, target="d-mbp:2")),  # the line above
    ]
    register = _register(tmp_path)
    assert [e.ref for e in register.active] == [obs]
    assert sorted(u.ref for u in register.unapplied) == sorted(lines)


def test_writing_to_a_principal_with_no_log_creates_nothing(tmp_path: Path) -> None:
    for call in (automations.retire, automations.pin, automations.unpin):
        with pytest.raises(FileNotFoundError):
            call(tmp_path, P, D, "water-plants", environ=ON)
    with pytest.raises(FileNotFoundError):
        automations.active(tmp_path, P)
    assert list(tmp_path.iterdir()) == []


def test_the_event_shape_is_held_to_its_verb() -> None:
    with pytest.raises(ValidationError, match="needs"):
        Automation(ts=TS, actor=P, verb=AutomationVerb.ADD, name="water-plants")
    with pytest.raises(ValidationError, match="does not carry"):
        _raw_add("water-plants", target="d-mbp:1")
    with pytest.raises(ValidationError, match="needs"):
        Automation(ts=TS, actor=P, verb=AutomationVerb.RETIRE)
    for verb in (AutomationVerb.RETIRE, AutomationVerb.PIN, AutomationVerb.UNPIN):
        with pytest.raises(ValidationError, match="does not carry"):
            _raw(verb, target="d-mbp:1", name="water-plants")
    with pytest.raises(ValidationError):
        _raw_add("Water Plants")  # a name is a path-safe token, like a node id
    with pytest.raises(ValidationError):
        _raw(AutomationVerb.ADD, name="water-plants", title="")
    with pytest.raises(ValidationError):
        Automation.model_validate(
            {"ts": TS, "actor": P, "verb": "add", "name": "a", "title": "t", "pinned": True}
        )


def test_now_instant_and_a_person_are_what_the_register_stamps(tmp_path: Path) -> None:
    before = now_instant()
    _add(tmp_path, "water-plants")
    event = _automation_events(tmp_path)[0]
    assert before <= event.ts <= now_instant()
    assert event.actor == ACTOR_USER


# --- the dormant rule ---------------------------------------------------------------------


def test_the_register_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Switch unset: nothing adds, retires, pins or unpins, and nothing is created on disk."""
    monkeypatch.delenv(automations.AUTOMATIONS_SWITCH, raising=False)
    assert not automations.enabled()
    calls = [
        lambda: automations.add(tmp_path, P, D, name="water-plants", title="t"),
        lambda: automations.retire(tmp_path, P, D, "water-plants"),
        lambda: automations.pin(tmp_path, P, D, "water-plants"),
        lambda: automations.unpin(tmp_path, P, D, "water-plants"),
    ]
    for call in calls:
        with pytest.raises(automations.AutomationsOffError):
            call()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "on\n"])
def test_only_the_one_spelling_turns_the_register_on(value: str) -> None:
    assert not automations.enabled({automations.AUTOMATIONS_SWITCH: value})
    assert automations.enabled({automations.AUTOMATIONS_SWITCH: "on"})


def test_reading_the_register_is_not_acting_and_works_while_off(tmp_path: Path) -> None:
    ref = _add(tmp_path, "water-plants").ref
    assert [e.ref for e in automations.active(tmp_path, P)] == [ref]


def test_nothing_in_the_log_can_turn_the_register_on() -> None:
    """The switch is the environment only: no event field turns it on."""
    assert not any(f in Automation.model_fields for f in ("enabled", "on", "active", "cap"))
    tree = ast.parse(SOURCE.read_text())
    reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "environ" and isinstance(n.value, ast.Name)
    ]
    assert all(n.value.id == "os" for n in reads)  # type: ignore[union-attr]


def test_the_cli_gains_no_automation_command() -> None:
    from walle.cli import main

    assert not any("automation" in name or "register" in name for name in main.commands)


def test_the_lock_is_per_principal_inside_their_folder(tmp_path: Path) -> None:
    _add(tmp_path, "water-plants")
    lock = automations_lock_path(tmp_path, P)
    assert lock.is_file()
    assert lock.parent == log_dir(tmp_path, P).parent
    assert lock.read_bytes() == b""


# --- pure fold, golden log --------------------------------------------------------------------


def test_the_register_reaches_no_model_and_no_network() -> None:
    assert forbidden_in_closure(PACKAGE, ("automations",)) == {}


def test_the_fold_reads_no_clock_and_opens_no_file() -> None:
    """``fold_automations`` and ``refusal`` are pure functions of their arguments (constraint 3)."""
    tree = ast.parse(SOURCE.read_text())
    pure = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in {"fold_automations", "refusal"}
    ]
    assert len(pure) == 2
    for fn in pure:
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert not names & {"open", "now_instant", "time", "datetime", "os"}
        assert not attrs & {"read_text", "now", "environ"}


def _golden_root(tmp_path: Path) -> tuple[Path, str]:
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):  # a read-only checkout must not fail the rebuild
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    return root, expected["principal"]


def test_the_register_over_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    """The fold the shipping build saw, pinned here because ``test_golden`` pins the belief
    hash, which no automation event moves. Rebuilt, incremental and straight from the JSONL
    agree: the register keeps no state outside the log (hard constraint 5)."""
    root, principal = _golden_root(tmp_path)

    report = rebuild(root, principal)
    from_index = automations.fold_automations(load_records(root, principal))
    from_log = automations.fold_automations(read_stream(root, principal, "mbp").records)
    assert automations.fold_automations(report.records) == from_index == from_log

    shutil.rmtree(tmp_path / "root")
    root, principal = _golden_root(tmp_path)
    update(root, principal)
    assert automations.fold_automations(load_records(root, principal)) == from_log

    assert [(e.ref, e.name, e.status, e.pinned) for e in from_log.entries] == [
        ("d-mbp:1", "water-plants", "active", False),
        ("d-mbp:2", "bin-night", "active", False),
        ("d-mbp:3", "school-run", "retired", False),
        ("d-mbp:11", "school-run", "active", True),
    ]
    assert [u.ref for u in from_log.unapplied] == [
        "d-mbp:6",
        "d-mbp:7",
        "d-mbp:8",
        "d-mbp:9",
    ]


# --- issue #120 (audit 5.1): two devices fold in time order, not stream order ----------------


def _at(hhmm: str, day: int = 5) -> str:
    return f"2026-11-{day:02d}T{hhmm}:00.000000+00:00"


def test_a_swap_from_the_laptop_of_an_automation_added_on_the_phone_holds(tmp_path: Path) -> None:
    """The audit's reproduction. ``gym-class`` was added first, on the phone. Six more and a
    swap come from the laptop. Folded in stream order every ``d-mbp`` event came before
    ``d-phone:1``: the retire found nothing, ``a-7`` took the seventh slot and ``gym-class``,
    the oldest automation, was then refused as an eighth and vanished. In time order the
    swap holds, on this machine and on any other that folds the same two streams."""
    gym = automations.add(tmp_path, P, "phone", name="gym-class", title="Gym", environ=ON)
    for i in range(1, 7):
        automations.add(tmp_path, P, "mbp", name=f"a-{i}", title=f"A {i}", environ=ON)

    swapped = automations.add(
        tmp_path, P, "mbp", name="a-7", title="A 7", retire="gym-class", environ=ON
    )

    register = _register(tmp_path)
    assert register.unapplied == ()
    assert [e.name for e in register.active] == [f"a-{i}" for i in range(1, 8)]
    retired = register.entry(gym.ref)
    assert retired is not None and retired.status == "retired"
    assert retired.retired_ref == "d-mbp:7"
    assert register.entry(swapped.ref) == swapped
    assert [e.name for e in automations.active(tmp_path, P)] == [e.name for e in register.active]


def test_what_a_write_returns_is_what_the_next_read_folds(tmp_path: Path) -> None:
    """``pin`` and ``retire`` fold, check, append and return the entry as changed. The next
    read must agree, whichever device each event came from."""
    automations.add(tmp_path, P, "phone", name="gym-class", title="Gym", environ=ON)
    pinned = automations.pin(tmp_path, P, "mbp", "gym-class", environ=ON)
    assert pinned.pinned and pinned.pin_ref == "d-mbp:1"
    assert _register(tmp_path).entry(pinned.ref) == pinned
    assert _register(tmp_path).unapplied == ()

    automations.unpin(tmp_path, P, "tab", "gym-class", environ=ON)
    retired = automations.retire(tmp_path, P, "mbp", "gym-class", environ=ON)
    assert retired.status == "retired" and retired.retired_ref == "d-mbp:2"
    assert _register(tmp_path).entry(retired.ref) == retired
    assert _register(tmp_path).unapplied == ()
    assert automations.active(tmp_path, P) == ()


def test_events_on_two_devices_apply_in_time_order(tmp_path: Path) -> None:
    """Hand-written stamps, so the order is the fold's and not the test clock's: the phone's
    add at 10:01 comes before the laptop's retire of it at 10:10, although every ``d-mbp``
    ref sorts before every ``d-phone`` ref."""
    _write(tmp_path, "mbp", _raw_add("car-service", ts=_at("10:00")))  # d-mbp:1
    _write(tmp_path, "phone", _raw_add("gym-class", ts=_at("10:01")))  # d-phone:1
    _write(tmp_path, "mbp", _raw(AutomationVerb.RETIRE, target="d-phone:1", ts=_at("10:10")))
    _write(tmp_path, "phone", _raw(AutomationVerb.PIN, target="d-mbp:1", ts=_at("10:20")))

    register = _register(tmp_path)
    assert register.unapplied == ()
    assert [(e.ref, e.status, e.pinned) for e in register.entries] == [
        ("d-mbp:1", "active", True),
        ("d-phone:1", "retired", False),
    ]


def test_a_clock_that_steps_back_on_one_device_never_reorders_that_device(tmp_path: Path) -> None:
    """The phone adds ``a-1`` at 10:10, its clock steps back, it adds ``a-2`` at 09:50 and
    retires ``a-1`` at 09:55. A plain sort by stamp would apply the retire before the add it
    names and report it. The merge keeps the phone's seq order, so everything applies; the
    laptop's 10:00 add is interleaved before the phone's run, and the answer is the same
    however many times, and in whatever order, the log is folded."""
    _write(tmp_path, "phone", _raw_add("a-1", ts=_at("10:10")))  # d-phone:1
    _write(tmp_path, "phone", _raw_add("a-2", ts=_at("09:50")))  # d-phone:2
    _write(tmp_path, "phone", _raw(AutomationVerb.RETIRE, target="d-phone:1", ts=_at("09:55")))
    _write(tmp_path, "mbp", _raw_add("b-1", ts=_at("10:00")))  # d-mbp:1

    records = _records(tmp_path)
    register = automations.fold_automations(records)
    assert register.unapplied == ()
    assert [(e.ref, e.status) for e in register.entries] == [
        ("d-mbp:1", "active"),
        ("d-phone:1", "retired"),
        ("d-phone:2", "active"),
    ]
    assert automations.fold_automations(records) == register
    assert automations.fold_automations(reversed(records)) == register
    assert automations.fold_automations([*records, *records]) == register


def test_the_same_instant_on_two_devices_falls_back_to_stream_order(tmp_path: Path) -> None:
    """A tie is ordered ``(stream, seq)`` (D57), so it is still one answer: with two
    sevenths stamped alike, the laptop's is applied and the phone's is reported."""
    for name in NAMES[:6]:
        _write(tmp_path, "mbp", _raw_add(name, ts=_at("09:00")))
    on_phone = _write(tmp_path, "phone", _raw_add("gym-class", ts=_at("10:00")))
    on_mbp = _write(tmp_path, "mbp", _raw_add("car-service", ts=_at("10:00")))

    register = _register(tmp_path)
    assert register.find("car-service") is not None
    assert register.find("car-service").ref == on_mbp  # type: ignore[union-attr]
    assert [u.ref for u in register.unapplied] == [on_phone]
