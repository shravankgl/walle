"""Issue #106's verify criterion: the Phase 3 gate report (D6, D22).

  a dead automation shows up   one planted automation that went silent is named in the report,
                               as dead, with why: it stopped by its day 30 and rot labels it
  unproven until real use      the report never calls the 30-day gate passed, whatever the
                               survival number on the log it was pointed at
  off by default               nothing is read unless the owner sets WALLE_GATE3=on, and the
                               rot switch too, since the report labels rot from the same log

Every automation, outcome and approval here is synthetic: a made-up household, no real data.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from click.testing import CliRunner

from test_constraints import forbidden_in_closure
from walle import gate3, rot
from walle.cli import main
from walle.events import (
    ACTOR_KERNEL,
    Answer,
    AnswerOutcome,
    Ask,
    AskKind,
    Automation,
    AutomationVerb,
    Event,
    Outcome,
    OutcomeResult,
)
from walle.paths import log_dir
from walle.reader import Record, read_file
from walle.writer import StreamWriter

P = "ines"
D = "mbp"
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
ON = {gate3.GATE3_SWITCH: gate3.SWITCH_ON, rot.ROT_SWITCH: rot.SWITCH_ON}
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "gate3.py"


# --- builders -----------------------------------------------------------------------------


def _ts(days: float) -> str:
    return (T0 + timedelta(days=days)).isoformat(timespec="microseconds")


def _at(days: float) -> datetime:
    return T0 + timedelta(days=days)


def _write(root: Path, event: Event) -> str:
    written = StreamWriter(root, P, D).append(event)
    return f"d-{D}:{written.seq}"


def _add(root: Path, name: str, day: float = 0) -> str:
    return _write(
        root,
        Automation(ts=_ts(day), actor=P, verb=AutomationVerb.ADD, name=name, title="synthetic"),
    )


def _retire(root: Path, ref: str, day: float) -> str:
    return _write(root, Automation(ts=_ts(day), actor=P, verb=AutomationVerb.RETIRE, target=ref))


def _ran(root: Path, ref: str, day: float, result: str = "ok") -> str:
    return _write(
        root, Outcome(ts=_ts(day), actor=ACTOR_KERNEL, target=ref, result=OutcomeResult(result))
    )


def _ask(root: Path, session: str, call: str, day: float) -> str:
    return _write(
        root,
        Ask(
            ts=_ts(day),
            actor=ACTOR_KERNEL,
            kind=AskKind.APPROVAL,
            session=session,
            tool_call_id=call,
            body="synthetic: send the bin-night reminder?",
        ),
    )


def _answer(root: Path, ask: str, outcome: AnswerOutcome, day: float) -> str:
    actor = ACTOR_KERNEL if outcome is AnswerOutcome.ORPHANED else P
    return _write(root, Answer(ts=_ts(day), actor=actor, ask=ask, outcome=outcome))


def _records(root: Path) -> list[Record]:
    log = log_dir(root, P)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _tree(path: Path) -> dict[str, str]:
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _household(root: Path) -> dict[str, str]:
    """Three automations added on day 0, seen on day 45.

    water-plants  runs every few days, still running at day 30: survived, active
    school-run    the planted dead one: ran until day 5, then nothing. Still in the register
    bin-night     retired on day 12 by a person: a retirement, not a dead automation

    And three approvals: one approved, one denied, one still pending, in two sessions.
    """
    refs = {name: _add(root, name) for name in ("water-plants", "school-run", "bin-night")}
    for day in range(2, 45, 4):
        _ran(root, refs["water-plants"], day)
    for day in (1, 3, 5):
        _ran(root, refs["school-run"], day)
    _ran(root, refs["bin-night"], 4)
    _retire(root, refs["bin-night"], 12)
    first = _ask(root, "s-1", "toolu_1", 2)
    _answer(root, first, AnswerOutcome.APPROVED, 2.1)
    second = _ask(root, "s-1", "toolu_2", 3)
    _answer(root, second, AnswerOutcome.DENIED, 3.1)
    _ask(root, "s-2", "toolu_3", 9)
    return refs


# --- verify: a planted dead automation shows up -------------------------------------------


def test_a_planted_dead_automation_shows_up(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(45))

    assert [d.ref for d in report.dead] == [refs["school-run"]]
    dead = report.dead[0]
    assert dead.name == "school-run"
    assert any("day 30" in why for why in dead.reasons)  # survival: it stopped
    assert any("stale" in why for why in dead.reasons)  # rot: idle 40 days
    text = "\n".join(gate3.render(report))
    assert "school-run" in text and "dead" in text


def test_a_running_or_retired_automation_is_not_dead(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(45))
    dead = {d.ref for d in report.dead}
    assert refs["water-plants"] not in dead
    assert refs["bin-night"] not in dead
    assert [r.ref for r in report.retirements] == [refs["bin-night"]]
    assert report.retirements[0].name == "bin-night"


def test_a_dead_automation_rot_alone_sees_shows_up(tmp_path: Path) -> None:
    """Added on day 0, ran on day 26 and never again: it survived its day 30 (an ok run in the
    week before it), but by day 70 rot labels it stale. Still dead, for the rot reason."""
    ref = _add(tmp_path, "dog-walk")
    _ran(tmp_path, ref, 26)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(70))
    assert report.survival.rate == 1.0
    assert [d.ref for d in report.dead] == [ref]
    assert all("day 30" not in why for why in report.dead[0].reasons)


def test_a_young_automation_gone_quiet_is_not_dead_yet(tmp_path: Path) -> None:
    """Idle 10 days, not at day 30: neither survival nor rot can call it dead."""
    _add(tmp_path, "new-one")
    report = gate3.gate_report(_records(tmp_path), as_of=_at(10))
    assert report.dead == ()


# --- verify: the 30-day gate is unproven until real use -----------------------------------


def test_with_nothing_at_day_30_the_gate_is_unproven(tmp_path: Path) -> None:
    _add(tmp_path, "new-one")
    report = gate3.gate_report(_records(tmp_path), as_of=_at(10))
    assert report.survival.rate is None
    assert report.above_bar is None
    assert "unproven until real use" in report.gate
    assert "unknown, not zero" in report.gate


def test_even_above_the_bar_the_gate_is_unproven(tmp_path: Path) -> None:
    """Every automation survived on this log, 100% against a bar of more than 60%. The code
    cannot tell real use from a planted log, so it still does not call the gate passed."""
    ref = _add(tmp_path, "water-plants")
    for day in range(2, 40, 4):
        _ran(tmp_path, ref, day)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(40))
    assert report.survival.rate == 1.0
    assert report.above_bar is True
    assert "unproven until real use" in report.gate
    assert "passed" not in report.gate.replace("call the gate passed", "")
    assert any("unproven until real use" in line for line in gate3.render(report))


def test_below_the_bar_says_so_and_is_still_unproven(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(45))
    # water-plants survived; school-run went silent and bin-night was retired: 1 of 3
    assert len(report.survival.cohort) == 3 and len(report.survival.survived) == 1
    assert report.above_bar is False
    assert "unproven until real use" in report.gate
    assert "1 of 3" in report.gate and "33.3%" in report.gate
    assert refs  # the household is the one planted above


def test_the_bar_is_more_than_sixty_percent_not_sixty() -> None:
    assert Fraction(3, 5) == gate3.GATE_BAR
    assert not gate3.above(3, 5)
    assert gate3.above(4, 6)
    assert gate3.above(None, 0) is None


# --- the rest of the report ---------------------------------------------------------------


def test_permission_counts(tmp_path: Path) -> None:
    _household(tmp_path)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(45))
    p = report.permissions
    assert (p.asked, p.approved, p.denied, p.orphaned, p.pending) == (3, 1, 1, 0, 1)
    assert (p.sessions, p.most_in_one_session) == (2, 2)


def test_register_counts_against_the_wip_cap(tmp_path: Path) -> None:
    _household(tmp_path)
    report = gate3.gate_report(_records(tmp_path), as_of=_at(45))
    assert (report.active, report.cap) == (2, 7)


def test_as_of_is_a_cut_events_after_it_change_nothing(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    before = gate3.gate_report(_records(tmp_path), as_of=_at(45))
    _retire(tmp_path, refs["school-run"], 50)
    _ask(tmp_path, "s-3", "toolu_9", 51)
    after = gate3.gate_report(_records(tmp_path), as_of=_at(45))
    assert after == before
    later = gate3.gate_report(_records(tmp_path), as_of=_at(52))
    assert later.dead == ()
    assert {r.name for r in later.retirements} == {"bin-night", "school-run"}
    assert later.permissions.asked == 4


def test_the_report_is_pure_order_and_duplicates_do_not_matter(tmp_path: Path) -> None:
    _household(tmp_path)
    records = _records(tmp_path)
    one = gate3.gate_report(records, as_of=_at(45))
    two = gate3.gate_report(list(reversed(records)) + records[:3], as_of=_at(45))
    assert one == two


def test_a_naive_as_of_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        gate3.gate_report([], as_of=datetime(2026, 10, 1))


def test_render_prints_names_and_counts_never_titles_or_bodies(tmp_path: Path) -> None:
    _household(tmp_path)
    text = "\n".join(gate3.render(gate3.gate_report(_records(tmp_path), as_of=_at(45))))
    assert "synthetic" not in text  # titles and ask bodies are the person's words
    for word in ("survival", "retired", "approvals", "dead", "unproven until real use"):
        assert word in text


# --- off by default -----------------------------------------------------------------------


def test_the_report_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(gate3.GATE3_SWITCH, raising=False)
    monkeypatch.delenv(rot.ROT_SWITCH, raising=False)
    assert not gate3.enabled()
    with pytest.raises(gate3.Gate3OffError):
        gate3.report(tmp_path, P, as_of=_at(0))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {gate3.GATE3_SWITCH: gate3.SWITCH_ON},
        {rot.ROT_SWITCH: rot.SWITCH_ON},
        {gate3.GATE3_SWITCH: "1", rot.ROT_SWITCH: rot.SWITCH_ON},
        {gate3.GATE3_SWITCH: "ON", rot.ROT_SWITCH: rot.SWITCH_ON},
    ],
)
def test_off_is_checked_before_the_log_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environ: dict[str, str]
) -> None:
    _household(tmp_path)
    monkeypatch.setattr(gate3, "_read", lambda *a: pytest.fail("read the log while off"))
    with pytest.raises(gate3.Gate3OffError):
        gate3.report(tmp_path, P, as_of=_at(45), environ=environ)


def test_on_it_reads_the_log_and_writes_nothing(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    before = _tree(tmp_path)
    report = gate3.report(tmp_path, P, as_of=_at(45), environ=ON)
    assert report == gate3.gate_report(_records(tmp_path), as_of=_at(45))
    assert [d.ref for d in report.dead] == [refs["school-run"]]
    assert _tree(tmp_path) == before
    with pytest.raises(FileNotFoundError):
        gate3.report(tmp_path, "nobody", as_of=_at(45), environ=ON)


def test_the_command_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(gate3.GATE3_SWITCH, raising=False)
    monkeypatch.delenv(rot.ROT_SWITCH, raising=False)
    _household(tmp_path)
    before = _tree(tmp_path)
    result = CliRunner().invoke(main, ["gate3", "--root", str(tmp_path), "--principal", P])
    assert result.exit_code == 2
    assert "WALLE_GATE3=on" in result.output and "nothing was read" in result.output
    assert _tree(tmp_path) == before


def test_the_command_prints_the_report_when_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in ON.items():
        monkeypatch.setenv(name, value)
    _household(tmp_path)
    args = ["gate3", "--root", str(tmp_path), "--principal", P, "--as-of", _ts(45)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "school-run" in result.output
    assert "unproven until real use" in result.output


@pytest.mark.parametrize("as_of", ["2026-10-01", "2026-10-01T09:00:00", "yesterday"])
def test_the_command_refuses_an_as_of_without_a_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, as_of: str
) -> None:
    for name, value in ON.items():
        monkeypatch.setenv(name, value)
    _household(tmp_path)
    args = ["gate3", "--root", str(tmp_path), "--principal", P, "--as-of", as_of]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2
    assert "nothing was read" in result.output or "timezone" in result.output


def test_the_command_with_no_log_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in ON.items():
        monkeypatch.setenv(name, value)
    result = CliRunner().invoke(main, ["gate3", "--root", str(tmp_path), "--principal", P])
    assert result.exit_code == 2
    assert "no log" in result.output


# --- what the module may touch ------------------------------------------------------------


def test_the_report_reaches_no_model_or_network() -> None:
    assert forbidden_in_closure(PACKAGE, ("gate3",)) == {}


def test_the_module_imports_no_writer() -> None:
    tree = ast.parse(SOURCE.read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "walle.writer" not in imported
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not names & {"StreamWriter", "add", "retire", "record", "ask", "answer", "grant"}


def test_only_the_command_line_reaches_the_report() -> None:
    """Nothing in the package runs the report on its own: only ``cli.py`` imports it, to
    register ``walle gate3``, which refuses while the switches are off."""
    users = sorted(
        p.name
        for p in PACKAGE.rglob("*.py")
        if p.name != "gate3.py"
        and any(
            (
                isinstance(n, ast.ImportFrom)
                and (
                    n.module == "walle.gate3"
                    or (n.module == "walle" and any(a.name == "gate3" for a in n.names))
                )
            )
            or (isinstance(n, ast.Import) and any(a.name == "walle.gate3" for a in n.names))
            for n in ast.walk(ast.parse(p.read_text()))
        )
    )
    assert users == ["cli.py"]
