"""``walle gate``: the Phase 2 gate as one command (PLAN §3, D22, issue #75).

PLAN's Phase 2 row asks for two things before self-modification is trusted: *rebuild test
green* and *no eval regressions*. This module runs both and gives one answer.

The rebuild half (hard constraint 5, D10, D88)
----------------------------------------------
- Every golden fixture under ``tests/fixtures/v<N>/<name>/`` is copied to a scratch folder,
  its index is rebuilt from the log alone, and the resolved state must hash to the
  ``state_hash`` in its ``__expected__.json``, with the same event count and no skipped
  line. The repo copy is never written to.
- Every log the eval below builds is rebuilt too, and the state folded from the rebuilt index
  must equal the state folded straight from the JSONL. These logs are fresh each run, so they
  catch a rebuild bug on shapes the frozen fixtures do not hold.

The regression half (D22)
-------------------------
The fake eval (``walle eval --model fake``) runs over ``tests/corpora`` and the synthetic
LongMemEval and LoCoMo files under ``tests/fixtures/evals``. Its D77 ship gate must pass, and
each metric in ``METRICS`` must hold the rule ``reports/floor.txt`` gives it.

D22 says a regression guard is a **floor, not a delta**: measured against the capability walle
started with, never against the previous run, or a slow series of individually-acceptable
drops walks the system downhill. So the floor file holds the first measurement, and nothing
here reads an earlier report or writes the floor. Rules are declarative, in D22's own form::

    keep <metric> <= <limit>                  an absolute limit
    keep <metric> >= -1% of <floor>           a limit relative to the floor

``<floor>`` and an absolute ``<limit>`` are non-negative integers or fractions ``n/d``, so a
share such as 19 of 30 items is exact. A relative limit is a signed percentage of the floor.
The bound is ``floor * (1 + pct/100)`` and never moves. The ship gate already fails on one
false-allow. The floor adds what it cannot see: a proposer gone quiet refuses and leaks
nothing, so it passes the ship gate, but it proposes less than walle started with.

Dormant (the owner's condition on #75)
--------------------------------------
Off unless ``WALLE_GATE`` is exactly ``on``. While it is off, ``run`` raises ``GateOff`` before
it reads a file, and the command prints that nothing was checked and exits 2: not 0, because
a gate that did not run has not passed, and not 1, because nothing failed. Nothing calls
``run`` on its own. It takes no walle root: it reads the repo's synthetic fixtures, and it
writes only under a temporary folder it deletes. Only the fake model runs, so no key is
read and nothing leaves the machine.

Exit codes: 0 every check passed; 1 a check failed; 2 the gate is off or cannot start (an
input is missing, or the floor file does not read cleanly).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import click

from walle import index
from walle.evals import capability, corpus, report
from walle.evals.capability import CapabilityResult, Dataset
from walle.evals.corpus import Item
from walle.paths import log_dir
from walle.proposer import FakeModel
from walle.reader import Record, read_file
from walle.resolver import ResolutionError, resolve

GATE_SWITCH = "WALLE_GATE"
"""The owner's switch. The gate runs only when it is exactly ``SWITCH_ON``."""

SWITCH_ON = "on"

OFF_MESSAGE = (
    f"the gate is off; the owner turns it on with {GATE_SWITCH}={SWITCH_ON}. nothing was checked."
)

FLOOR_NAME = "floor.txt"

EXPECTED = "__expected__.json"

METRICS: tuple[str, ...] = (
    "dangerous.false_allows",
    "injection.false_allows",
    "benign.refused",
    "benign.proposed",
    "longmemeval.hit_at_5",
    "locomo.hit_at_5",
)
"""What the gate measures. ``*.false_allows`` and ``benign.refused`` are counts over both
boundary legs; ``benign.proposed`` is the share of benign items the proposer leg proposed
from; ``*.hit_at_5`` is hits over questions with evidence. Every one needs a rule."""


class GateOff(Exception):
    """The owner has not turned the gate on."""


class InputError(Exception):
    """An input is missing or does not load. Raised before anything is built."""


class FloorError(InputError):
    """The floor file does not read cleanly. The gate cannot start without every rule."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``WALLE_GATE`` is exactly ``on``. The process environment when no mapping is
    given; a mapping is the whole answer when one is."""
    env = os.environ if environ is None else environ
    return env.get(GATE_SWITCH) == SWITCH_ON


# --- the floor file ---------------------------------------------------------------------

_NUMBER = r"\d+(?:/\d+)?"
_PERCENT = r"[+-]\d+(?:\.\d+)?%"
_RULE = re.compile(
    r"keep\s+(?P<metric>[a-z0-9_.]+)\s+(?P<op><=|>=)\s+(?P<limit>\S+)"
    r"(?:\s+of\s+(?P<floor>\S+))?"
)


@dataclass(frozen=True)
class Rule:
    metric: str
    op: str
    """``<=`` or ``>=``."""
    limit: Fraction
    """The absolute bound, or the relative change as a fraction (``-1%`` is ``-1/100``)."""
    floor: Fraction | None
    """The first measurement, for a relative rule; ``None`` for an absolute one."""

    @property
    def bound(self) -> Fraction:
        return self.limit if self.floor is None else self.floor * (1 + self.limit)

    def holds(self, value: Fraction) -> bool:
        return value <= self.bound if self.op == "<=" else value >= self.bound

    def describe(self) -> str:
        if self.floor is None:
            return f"{self.op} {_show(self.bound)}"
        pct = f"{float(self.limit * 100):+g}%"
        return f"{self.op} {_show(self.bound)} ({pct} of the floor {_show(self.floor)})"


def _number(text: str, line: int) -> Fraction:
    if not re.fullmatch(_NUMBER, text) or text.endswith("/0"):
        raise FloorError(f"line {line}: {text!r} is not a number (an integer or n/d, not negative)")
    return Fraction(text)


def parse_floor(text: str, *, metrics: Sequence[str] = METRICS) -> tuple[Rule, ...]:
    """Every rule in ``text``, one per metric in ``metrics``, or ``FloorError``."""
    rules: dict[str, Rule] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _RULE.fullmatch(line)
        if match is None:
            raise FloorError(
                f"line {number}: not a floor line; expected "
                "'keep <metric> <= | >= <limit> [of <floor>]'"
            )
        metric, op, limit, floor = match.group("metric", "op", "limit", "floor")
        if metric not in metrics:
            raise FloorError(f"line {number}: unknown metric {metric!r}")
        if metric in rules:
            raise FloorError(f"line {number}: {metric} has a rule twice")
        if re.fullmatch(_PERCENT, limit):
            if floor is None:
                raise FloorError(f"line {number}: a relative limit needs a floor ('of <floor>')")
            rule = Rule(metric, op, Fraction(limit[:-1]) / 100, _number(floor, number))
        elif floor is not None:
            raise FloorError(f"line {number}: only a percentage is measured 'of' a floor")
        else:
            rule = Rule(metric, op, _number(limit, number), None)
        rules[metric] = rule
    missing = [m for m in metrics if m not in rules]
    if missing:
        raise FloorError(f"no rule for {', '.join(missing)}")
    return tuple(rules[m] for m in metrics)


def load_floor(path: Path) -> tuple[Rule, ...]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FloorError(f"{path}: {exc}") from exc
    return parse_floor(text)


# --- inputs and results -----------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    """The synthetic files the gate reads. Nothing else is read."""

    fixtures: Path
    corpora: Path
    longmemeval: Path
    locomo: Path
    floor: Path

    @classmethod
    def under(cls, repo: Path) -> Inputs:
        repo = Path(repo)
        return cls(
            fixtures=repo / "tests" / "fixtures",
            corpora=repo / "tests" / "corpora",
            longmemeval=repo / "tests" / "fixtures" / "evals" / "longmemeval.json",
            locomo=repo / "tests" / "fixtures" / "evals" / "locomo.json",
            floor=repo / "reports" / FLOOR_NAME,
        )

    def missing(self) -> list[Path]:
        wanted = (self.fixtures, self.corpora, self.longmemeval, self.locomo, self.floor)
        return [p for p in wanted if not p.exists()]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateResult:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def _show(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator} ({float(value):.4f})"


# --- the rebuild half -------------------------------------------------------------------


def _hash(records: Sequence[Record]) -> str:
    return index.state_hash(resolve(records))


def _from_log(root: Path, principal: str) -> tuple[list[Record], int]:
    """Every record read straight from the JSONL, no index involved, and the lines skipped."""
    records: list[Record] = []
    skips = 0
    for path in sorted(log_dir(root, principal).glob("*.jsonl")):
        contents = read_file(path, path.stem)
        records.extend(contents.records)
        skips += len(contents.skips)
    return records, skips


def _fixture(fixture: Path, scratch: Path) -> Check:
    name = f"rebuild {fixture.parent.name}/{fixture.name}"
    try:
        expected = json.loads((fixture / EXPECTED).read_text(encoding="utf-8"))
        principal, want_events, want_hash = (
            expected["principal"],
            expected["events"],
            expected["state_hash"],
        )
        root = scratch / fixture.parent.name / fixture.name
        shutil.copytree(fixture, root, ignore=shutil.ignore_patterns(EXPECTED))
        # A read-only checkout makes a read-only copy, and the rebuild would fail for a
        # reason that has nothing to do with the log.
        for path in (root, *root.rglob("*")):
            mode = path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0)
            path.chmod(mode)
        built = index.rebuild(root, principal)
        got = _hash(built.records)
    except Exception as exc:  # any failure to rebuild is this check failing, said plainly
        return Check(name, False, f"could not rebuild: {type(exc).__name__}: {exc}")
    problems = []
    if built.skips:
        problems.append(f"{len(built.skips)} skipped lines")
    if built.events != want_events:
        problems.append(f"{built.events} events, expected {want_events}")
    if got != want_hash:
        problems.append(f"state {got[:12]}, expected {str(want_hash)[:12]}")
    if problems:
        return Check(name, False, "; ".join(problems))
    return Check(name, True, f"{built.events} events, state {got[:12]}")


def rebuild_fixtures(fixtures: Path, scratch: Path) -> list[Check]:
    """One check per golden fixture, each rebuilt from a copy under ``scratch``."""
    golden = sorted(p.parent for p in Path(fixtures).glob(f"v[0-9]*/*/{EXPECTED}"))
    if not golden:
        return [Check("rebuild golden fixtures", False, f"no golden fixtures under {fixtures}")]
    return [_fixture(fixture, Path(scratch)) for fixture in golden]


def rebuild_logs(root: Path) -> Check:
    """Every principal's log under ``root/<leg>/principals/``: rebuilt, then folded from the
    index and from the JSONL, and the two states must be one."""
    name = "rebuild eval logs"
    found = sorted(Path(root).glob("*/principals/p-*"))
    if not found:
        return Check(name, False, f"no logs under {root}")
    problems = []
    for folder in found:
        base, principal = folder.parent.parent, folder.name.removeprefix("p-")
        label = f"{base.name}/{principal}"
        try:
            records, skips = _from_log(base, principal)
            built = index.rebuild(base, principal)
            if not records:
                problems.append(f"{label}: an empty log, so nothing was checked")
            elif skips or built.skips:
                problems.append(f"{label}: skipped lines")
            elif built.events != len(records):
                problems.append(f"{label}: {built.events} events indexed, {len(records)} in log")
            elif _hash(built.records) != _hash(records):
                problems.append(f"{label}: the rebuilt state is not the log's")
        except (ResolutionError, ValueError, OSError) as exc:
            problems.append(f"{label}: {type(exc).__name__}: {exc}")
    if problems:
        return Check(name, False, "; ".join(problems))
    return Check(name, True, f"{len(found)} logs rebuilt, each state equal to its log's")


# --- the regression half ----------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    rules: tuple[Rule, ...]
    items: Mapping[str, Sequence[Item]]
    datasets: tuple[Dataset, ...]


def _load(inputs: Inputs) -> _Loaded:
    """Every input read and checked, or ``InputError`` saying which one is wrong."""
    missing = inputs.missing()
    if missing:
        raise InputError("missing: " + ", ".join(str(p) for p in missing))
    rules = load_floor(inputs.floor)
    try:
        items = corpus.load_all(inputs.corpora)
        datasets = (
            capability.load_longmemeval(inputs.longmemeval),
            capability.load_locomo(inputs.locomo),
        )
    except (ValueError, OSError) as exc:
        raise InputError(str(exc)) from exc
    return _Loaded(rules, items, datasets)


def _evaluate(loaded: _Loaded, root: Path) -> report.EvalResult:
    return report.evaluate(root, loaded.items, FakeModel(), datasets=loaded.datasets)


def evaluate(inputs: Inputs, root: Path) -> report.EvalResult:
    """The fake eval over the synthetic corpora and capability files, built under ``root``."""
    return _evaluate(_load(inputs), root)


def measure(result: report.EvalResult) -> dict[str, Fraction | None]:
    """Each metric in ``METRICS``. ``None`` is a metric that could not be measured, such as a
    share of nothing; the gate fails it rather than guess."""
    outcomes = result.boundary.outcomes
    benign = [o for o in outcomes if o.corpus == "benign" and o.leg == "proposer"]
    proposed = sum(o.outcome == "proposed" for o in benign)
    by_name: dict[str, CapabilityResult] = {c.dataset: c for c in result.capability}

    def hit_rate(name: str) -> Fraction | None:
        ran = by_name.get(name)
        return None if ran is None or ran.scored == 0 else Fraction(ran.hits, ran.scored)

    gate = result.boundary.gate
    return {
        "dangerous.false_allows": Fraction(gate.false_allows.get("dangerous", 0)),
        "injection.false_allows": Fraction(gate.false_allows.get("injection", 0)),
        "benign.refused": Fraction(gate.benign_refused),
        "benign.proposed": Fraction(proposed, len(benign)) if benign else None,
        "longmemeval.hit_at_5": hit_rate("longmemeval"),
        "locomo.hit_at_5": hit_rate("locomo"),
    }


def floor_checks(rules: Sequence[Rule], measured: Mapping[str, Fraction | None]) -> list[Check]:
    checks = []
    for rule in rules:
        name = f"floor {rule.metric}"
        value = measured.get(rule.metric)
        if value is None:
            checks.append(Check(name, False, f"not measured; the rule is {rule.describe()}"))
        else:
            checks.append(
                Check(name, rule.holds(value), f"measured {_show(value)}, keep {rule.describe()}")
            )
    return checks


# --- the whole gate ---------------------------------------------------------------------


def run(inputs: Inputs, *, environ: Mapping[str, str] | None = None) -> GateResult:
    """Both halves, under a temporary folder deleted afterwards. ``GateOff`` while the owner
    has not turned the gate on, before anything is read; ``InputError`` before anything is
    built when an input is missing or does not load."""
    if not enabled(environ):
        raise GateOff(OFF_MESSAGE)
    loaded = _load(inputs)
    with tempfile.TemporaryDirectory(prefix="walle-gate-") as scratch:
        base = Path(scratch)
        checks = rebuild_fixtures(inputs.fixtures, base / "fixtures")
        result = _evaluate(loaded, base / "eval")
        checks.append(rebuild_logs(base / "eval"))
        ship = result.boundary.gate
        checks.append(
            Check(
                "ship gate",
                ship.passed,
                f"dangerous {ship.false_allows.get('dangerous', 0)} and injection "
                f"{ship.false_allows.get('injection', 0)} false-allows, "
                f"benign {ship.benign_refused} refused and {ship.benign_proposed} proposed "
                "from (D77)"
                + (f"; did not run: {', '.join(ship.missing)}" if ship.missing else ""),
            )
        )
        checks.extend(floor_checks(loaded.rules, measure(result)))
    return GateResult(tuple(checks))


@click.command("gate")
@click.option(
    "--repo",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="The walle checkout whose synthetic fixtures, corpora and reports/floor.txt to read.",
)
def gate_command(repo: Path) -> None:
    """The Phase 2 gate: the rebuild test, then the fake eval held to reports/floor.txt (D22).

    Off unless WALLE_GATE=on. Reads only the repo's synthetic fixtures and writes only to a
    temporary folder. Exits 0 when every check passes, 1 when one fails, and 2 when the gate
    is off or cannot start.
    """
    try:
        result = run(Inputs.under(repo))
    except GateOff as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2) from exc
    except InputError as exc:
        click.echo(f"the gate cannot start, nothing was checked: {exc}", err=True)
        raise SystemExit(2) from exc
    for check in result.checks:
        click.echo(f"{'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    click.echo(f"GATE: {'PASS' if result.passed else 'FAIL'}")
    if not result.passed:
        raise SystemExit(1)
