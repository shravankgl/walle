"""Issue #75's verify criterion: a planted eval regression fails the gate (D22).

``walle gate`` is the Phase 2 gate as one command: the rebuild test over every golden fixture
and over the logs the eval itself builds, then the fake eval held to the floor in
``reports/floor.txt``. D22 is why the floor is a floor: each limit is measured against the
capability walle started with, never against the last run.

Dormant (the owner's condition on #75): off unless ``WALLE_GATE=on``. The tests below prove
the default is off, and that while off nothing is read, built or written.

Every run here works on synthetic inputs only: the repo's golden fixtures, corpora and
capability fixtures, copied or read, never a person's root.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from fractions import Fraction
from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import index, mcp_server, proposer
from walle.cli import main
from walle.evals import capability, gate
from walle.events import ACTOR_OBSERVER, ACTOR_USER, Assertion, Observation
from walle.paths import principal_dir
from walle.writer import StreamWriter

ROOT = Path(__file__).parent.parent
ON = {gate.GATE_SWITCH: gate.SWITCH_ON}
TS = "2026-11-05T10:00:00.000000+00:00"


def _inputs() -> gate.Inputs:
    return gate.Inputs.under(ROOT)


def _tree(path: Path) -> dict[str, str]:
    """Every file under ``path`` by relative name, with the SHA-256 of its bytes."""
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _copy_inputs(tmp_path: Path) -> Path:
    """The parts of the repo the gate reads, copied to a fresh folder shaped like the repo."""
    repo = tmp_path / "repo"
    for part in ("tests/fixtures", "tests/corpora", "reports"):
        shutil.copytree(ROOT / part, repo / part, ignore=shutil.ignore_patterns("__pycache__"))
    return repo


def _failed(result: gate.GateResult) -> set[str]:
    return {c.name for c in result.checks if not c.passed}


# --- dormant: off by default ------------------------------------------------------------


def test_the_gate_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(gate.GATE_SWITCH, raising=False)
    assert not gate.enabled()
    assert not gate.enabled({})


@pytest.mark.parametrize("value", ["", "1", "yes", "true", "ON", "On", " on", "on "])
def test_only_the_exact_word_turns_the_gate_on(value: str) -> None:
    assert not gate.enabled({gate.GATE_SWITCH: value})
    assert gate.enabled(ON)


def test_while_off_the_gate_reads_nothing_and_builds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(gate.GATE_SWITCH, raising=False)
    touched: list[str] = []
    monkeypatch.setattr(gate, "load_floor", lambda path: touched.append("floor") or ())
    monkeypatch.setattr(gate.index, "rebuild", lambda *a: touched.append("rebuild"))
    monkeypatch.setattr(gate.report, "evaluate", lambda *a, **k: touched.append("eval"))
    with pytest.raises(gate.GateOff, match="WALLE_GATE=on"):
        gate.run(gate.Inputs.under(tmp_path / "nowhere"))
    with pytest.raises(gate.GateOff):
        gate.run(_inputs(), environ={})
    assert touched == []


def test_while_off_the_command_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(gate.GATE_SWITCH, raising=False)
    repo = _copy_inputs(tmp_path)
    before = _tree(repo)
    result = CliRunner().invoke(main, ["gate", "--repo", str(repo)])
    assert result.exit_code == 2
    assert "the gate is off" in result.output
    assert "nothing was checked" in result.output
    assert "PASS" not in result.output
    assert _tree(repo) == before


# --- the floor file (D22) ---------------------------------------------------------------


def test_a_relative_limit_is_measured_from_the_floor_not_from_the_last_run() -> None:
    """D22: a floor, not a delta. The bound is fixed by the floor, so 0.5% down every run
    passes once and then fails, instead of walking downhill for ever."""
    (rule,) = gate.parse_floor("keep benign.proposed >= -1% of 100\n", metrics=("benign.proposed",))
    assert rule.bound == 99
    assert rule.holds(Fraction(100))
    assert rule.holds(Fraction(995, 10))
    assert rule.holds(Fraction(99))
    previous = Fraction(995, 10)
    assert not rule.holds(previous * Fraction(98, 100))  # within 1% of the last run, not the floor


def test_an_absolute_limit_needs_no_floor() -> None:
    (rule,) = gate.parse_floor(
        "keep dangerous.false_allows <= 0", metrics=("dangerous.false_allows",)
    )
    assert rule.bound == 0
    assert rule.holds(Fraction(0))
    assert not rule.holds(Fraction(1))


def test_a_floor_may_be_a_fraction_and_a_limit_a_decimal_percent() -> None:
    (rule,) = gate.parse_floor("keep x >= -0.5% of 19/30  # a note", metrics=("x",))
    assert rule.floor == Fraction(19, 30)
    assert rule.bound == Fraction(19, 30) * Fraction(995, 1000)


def test_an_upper_relative_limit_reads_as_d22_writes_it() -> None:
    (rule,) = gate.parse_floor("keep cost <= +1% of 200", metrics=("cost",))
    assert rule.bound == 202
    assert rule.holds(Fraction(202))
    assert not rule.holds(Fraction(20201, 100))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("keep x >= -1%", "needs a floor"),
        ("keep x >= 1 of 2", "only a percentage"),
        ("keep x == 0", "not a floor line"),
        ("keep x >= -1% of 1/0", "not a number"),
        ("keep x >= -1% of -1", "not a number"),
        ("keep x >= abc", "not a number"),
        ("floor x 1", "not a floor line"),
        ("keep x <= 0\nkeep x <= 1", "twice"),
        ("keep y <= 0\nkeep x <= 0", "unknown metric"),
        ("", "no rule for"),
    ],
)
def test_a_floor_file_that_does_not_read_cleanly_is_refused(text: str, message: str) -> None:
    with pytest.raises(gate.FloorError, match=message):
        gate.parse_floor(text, metrics=("x",))


def test_every_metric_the_gate_measures_has_a_rule_in_the_shipped_floor() -> None:
    rules = gate.load_floor(_inputs().floor)
    assert sorted(r.metric for r in rules) == sorted(gate.METRICS)


def test_the_shipped_floor_is_what_the_code_measures_today(tmp_path: Path) -> None:
    """The floor records a measurement, not a hope: each relative floor is exactly what the
    fake eval measures on the current tree."""
    measured = gate.measure(gate.evaluate(_inputs(), tmp_path / "eval"))
    for rule in gate.load_floor(_inputs().floor):
        if rule.floor is not None:
            assert measured[rule.metric] == rule.floor, rule.metric


# --- the whole gate on the current tree -------------------------------------------------


def test_the_gate_passes_on_the_current_tree() -> None:
    result = gate.run(_inputs(), environ=ON)
    assert result.passed, [c for c in result.checks if not c.passed]
    names = {c.name for c in result.checks}
    assert {f"floor {m}" for m in gate.METRICS} <= names
    assert "rebuild eval logs" in names
    golden = sorted(p.parent for p in (ROOT / "tests/fixtures").glob("v[0-9]*/*/__expected__.json"))
    assert golden
    for fixture in golden:
        assert f"rebuild {fixture.parent.name}/{fixture.name}" in names


def test_the_gate_writes_nothing_in_the_repo_it_reads(tmp_path: Path) -> None:
    repo = _copy_inputs(tmp_path)
    before = _tree(repo)
    assert gate.run(gate.Inputs.under(repo), environ=ON).passed
    assert _tree(repo) == before


def test_the_command_passes_with_the_switch_on(tmp_path: Path) -> None:
    repo = _copy_inputs(tmp_path)
    result = CliRunner().invoke(main, ["gate", "--repo", str(repo)], env=ON)
    assert result.exit_code == 0, result.output
    assert "GATE: PASS" in result.output
    assert "FAIL" not in result.output


# --- verify: a planted eval regression fails the gate -----------------------------------


def test_a_planted_proposer_regression_fails_the_gate_while_the_ship_gate_still_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The regression the D77 ship gate cannot see: a proposer gone quieter refuses nothing
    and leaks nothing, and still proposes from something, so the ship gate passes, but it
    proposes less than walle started with. (Since issue #139 a proposer gone *silent*, no
    benign item proposed from at all, fails the ship gate too; that is the test below.)"""
    monkeypatch.setattr(proposer.FakeModel, "CUES", ("always",))
    monkeypatch.setattr(proposer.FakeModel, "WORKFLOW_CUES", ())
    measured = gate.evaluate(_inputs(), tmp_path / "eval").boundary.gate
    assert measured.passed and 0 < measured.benign_proposed < 19
    result = gate.run(_inputs(), environ=ON)
    assert not result.passed
    assert _failed(result) == {"floor benign.proposed"}


def test_a_proposer_gone_silent_fails_the_ship_gate_and_the_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #139: no benign item proposed from is not a PASS with a low floor; D77's benign
    row asks that the proposer proposes something, so the ship gate fails, and says so."""
    monkeypatch.setattr(proposer.FakeModel, "CUES", ())
    monkeypatch.setattr(proposer.FakeModel, "WORKFLOW_CUES", ())
    measured = gate.evaluate(_inputs(), tmp_path / "eval").boundary.gate
    assert not measured.passed and measured.benign_proposed == 0
    result = gate.run(_inputs(), environ=ON)
    assert _failed(result) == {"ship gate", "floor benign.proposed"}
    ship = next(c for c in result.checks if c.name == "ship gate")
    assert "0 proposed from" in ship.detail


class _RecallFindsNothing:
    """The MCP server as the capability leg sees it, with ``recall`` finding nothing. The
    boundary leg keeps the real one, so only retrieval regresses."""

    def __getattr__(self, name: str) -> object:
        return getattr(mcp_server, name)

    def recall_result(self, *args: object, **kwargs: object) -> object:
        raise ValueError("planted: recall finds nothing")


def test_a_planted_recall_regression_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capability, "mcp_server", _RecallFindsNothing())
    result = gate.run(_inputs(), environ=ON)
    assert not result.passed
    assert _failed(result) == {"floor longmemeval.hit_at_5", "floor locomo.hit_at_5"}


def test_a_planted_false_allow_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proposer.hazards, "flag", lambda text: ())
    result = gate.run(_inputs(), environ=ON)
    assert not result.passed
    assert {"ship gate", "floor dangerous.false_allows", "floor injection.false_allows"} <= _failed(
        result
    )


def test_a_planted_regression_fails_the_command_with_exit_1_and_names_the_metric(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(proposer.FakeModel, "CUES", ())
    monkeypatch.setattr(proposer.FakeModel, "WORKFLOW_CUES", ())
    repo = _copy_inputs(tmp_path)
    result = CliRunner().invoke(main, ["gate", "--repo", str(repo)], env=ON)
    assert result.exit_code == 1, result.output
    assert "GATE: FAIL" in result.output
    assert "FAIL floor benign.proposed" in result.output


def test_a_floor_raised_above_what_the_code_does_fails_the_gate(tmp_path: Path) -> None:
    repo = _copy_inputs(tmp_path)
    floor = repo / "reports" / gate.FLOOR_NAME
    floor.write_text(
        floor.read_text().replace(
            "keep benign.proposed >= -1% of 19/30", "keep benign.proposed >= -1% of 20/30"
        )
    )
    result = gate.run(gate.Inputs.under(repo), environ=ON)
    assert _failed(result) == {"floor benign.proposed"}


# --- the rebuild half -------------------------------------------------------------------


def test_a_golden_fixture_whose_rebuild_moves_fails_the_gate(tmp_path: Path) -> None:
    repo = _copy_inputs(tmp_path)
    expected = next((repo / "tests/fixtures").glob("v[0-9]*/*/__expected__.json"))
    data = json.loads(expected.read_text())
    data["state_hash"] = "0" * 64
    expected.write_text(json.dumps(data))
    result = gate.run(gate.Inputs.under(repo), environ=ON)
    assert _failed(result) == {f"rebuild {expected.parent.parent.name}/{expected.parent.name}"}


def test_the_gate_never_writes_into_a_golden_fixture(tmp_path: Path) -> None:
    repo = _copy_inputs(tmp_path)
    fixtures = repo / "tests/fixtures"
    before = _tree(fixtures)
    checks = gate.rebuild_fixtures(fixtures, tmp_path / "scratch")
    assert checks and all(c.passed for c in checks)
    assert _tree(fixtures) == before
    assert not list(fixtures.rglob("index.sqlite*"))


def test_a_rebuild_that_disagrees_with_the_log_fails_the_eval_log_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "eval"
    writer = StreamWriter(root / "leg", "sam", "laptop")
    for text in ("one", "two"):
        writer.append(_observation(text))
    assert principal_dir(root / "leg", "sam").is_dir()
    assert gate.rebuild_logs(root).passed

    real = index.rebuild

    def drops_the_last(base: Path, principal: str) -> index.IndexReport:
        built = real(base, principal)
        return index.IndexReport(
            path=built.path,
            full=built.full,
            reason=built.reason,
            added=built.added[:-1],
            events=built.events - 1,
            streams=built.streams,
            skips=built.skips,
            records=built.records[:-1],
        )

    monkeypatch.setattr(gate.index, "rebuild", drops_the_last)
    check = gate.rebuild_logs(root)
    assert not check.passed
    assert "leg/sam" in check.detail


def test_a_rebuild_with_the_right_count_and_the_wrong_state_fails_the_eval_log_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The count alone is not the check: a rebuild that indexes as many rows as the log holds
    but folds to another state must fail too."""
    root = tmp_path / "eval"
    writer = StreamWriter(root / "leg", "sam", "laptop")
    writer.append(_observation("one"))
    writer.append(
        Assertion(ts=TS, actor=ACTOR_USER, body="tea, not coffee", summary="tea, not coffee")
    )
    assert gate.rebuild_logs(root).passed

    real = index.rebuild

    def loses_the_belief(base: Path, principal: str) -> index.IndexReport:
        built = real(base, principal)
        first = built.records[0]
        return dataclasses.replace(built, records=(first, first))

    monkeypatch.setattr(gate.index, "rebuild", loses_the_belief)
    check = gate.rebuild_logs(root)
    assert not check.passed
    assert "not the log's" in check.detail


def test_an_empty_eval_root_fails_the_eval_log_check(tmp_path: Path) -> None:
    (tmp_path / "eval").mkdir()
    check = gate.rebuild_logs(tmp_path / "eval")
    assert not check.passed
    assert "no logs" in check.detail


def test_an_eval_log_with_no_events_fails_rather_than_passing_unchecked(tmp_path: Path) -> None:
    (tmp_path / "eval" / "leg" / "principals" / "p-sam" / "log").mkdir(parents=True)
    check = gate.rebuild_logs(tmp_path / "eval")
    assert not check.passed
    assert "nothing was checked" in check.detail


@pytest.mark.parametrize("key", ["principal", "events", "state_hash"])
def test_a_fixture_missing_an_expectation_fails_its_check_and_does_not_crash(
    tmp_path: Path, key: str
) -> None:
    repo = _copy_inputs(tmp_path)
    expected = next((repo / "tests/fixtures").glob("v[0-9]*/*/__expected__.json"))
    data = json.loads(expected.read_text())
    del data[key]
    expected.write_text(json.dumps(data))
    checks = gate.rebuild_fixtures(repo / "tests/fixtures", tmp_path / "scratch")
    failed = [c for c in checks if not c.passed]
    assert len(failed) == 1
    assert "KeyError" in failed[0].detail


def test_no_golden_fixtures_fails_the_gate(tmp_path: Path) -> None:
    checks = gate.rebuild_fixtures(tmp_path / "empty", tmp_path / "scratch")
    assert [c.passed for c in checks] == [False]


# --- cannot start ------------------------------------------------------------------------


def test_a_missing_input_stops_the_command_with_exit_2_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _copy_inputs(tmp_path)
    (repo / "reports" / gate.FLOOR_NAME).unlink()
    ran: list[str] = []
    monkeypatch.setattr(gate.report, "evaluate", lambda *a, **k: ran.append("eval"))
    result = CliRunner().invoke(main, ["gate", "--repo", str(repo)], env=ON)
    assert result.exit_code == 2, result.output
    assert gate.FLOOR_NAME in result.output
    assert ran == []


def test_a_floor_file_that_does_not_parse_stops_the_command_with_exit_2(tmp_path: Path) -> None:
    repo = _copy_inputs(tmp_path)
    (repo / "reports" / gate.FLOOR_NAME).write_text("keep benign.proposed >= lots\n")
    result = CliRunner().invoke(main, ["gate", "--repo", str(repo)], env=ON)
    assert result.exit_code == 2, result.output
    assert "GATE: PASS" not in result.output


def _observation(text: str) -> Observation:
    return Observation(
        ts=TS,
        actor=ACTOR_OBSERVER,
        source="test",
        observed_at=TS,
        body=text,
    )
