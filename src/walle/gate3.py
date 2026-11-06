"""``walle gate3``: the Phase 3 gate report as one command (PLAN §3, D6, D22, issue #106).

PLAN's Phase 3 row has one gate: *30-day survival > 60%*. This module puts that number beside
the three things that explain it, read from one principal's log as of one date:

- **survival** — ``walle.survival``: how many shipped automations still ran at their day 30;
- **retirements** — the register (``walle.automations``): what a person retired, and how many
  are active against D6's cap. D6 says restraint is enforced and dead automations erode trust
  silently, so the report names the **dead** ones too: automations still active in the
  register that are not running. One is dead when survival says it stopped by its day 30, or
  ``walle.rot`` labels it stale or archived. A retired automation is a retirement, not dead:
  a person already acted on it;
- **permission counts** — the inbox (``walle.inbox``): how many approvals the runs asked a
  person for, how each was resolved, and the most asked in one session, which is the number
  approval fatigue shows in (PLAN §6, D73).

The rules
---------
1. **A pure function of the log and an explicit date.** ``gate_report(records, as_of)`` reads
   no clock, opens no file and calls no model (hard constraint 3). Only events whose ``ts`` is
   on or before ``as_of`` are read, so a report about day 45 does not change as the log grows.
   A naive ``as_of`` is refused rather than guessed at. Order and duplicates do not matter.
2. **The 30-day gate is unproven until real use.** The report gives the measured rate and
   whether it is above the bar, but it never calls the gate passed. Code cannot tell a log of
   real use from a planted or test one, so passing the gate is the owner's call after 30 days
   of his own use. With nothing at day 30 yet the rate is *unknown, not zero*, as
   ``walle.survival`` says, and the report repeats that rather than a 0%.
3. **The bar is strictly more than 60%** (``GATE_BAR``), compared exactly as a fraction, so 3
   of 5 is not above it.
4. **D22's floor is not this gate's.** D22 guards against regressions from initial capability,
   and ``walle gate`` (Phase 2) holds the eval to ``reports/floor.txt``. Survival has no floor
   yet: there is no first real measurement to hold it to. This report only measures; it reads
   no floor and writes none.
5. **Names and counts, never words.** ``render`` prints automation names, refs, timestamps and
   counts. It never prints a title, a retire note or an ask's body: those are a person's own
   words, as ``walle stats`` never prints what a proposal says.
6. **Anything not counted is said.** What the register, survival and the inbox folds could not
   apply is counted under *not counted*, never dropped silently.
7. **Off by default** (the owner's dormant condition on #106, D117). ``report`` reads a log only
   once the owner has set ``WALLE_GATE3=on``, and ``WALLE_ROT=on`` as well, because the dead
   column labels rot from the same log (as ``walle.maintenance`` asks for both). It refuses
   before reading anything otherwise. Neither switch turns the other on, and the switches for
   recording outcomes or changing the register are not needed: reading is not acting. The
   command prints that nothing was read and exits 2.

There is no two-writer walkthrough because there is no writer. ``report`` reads each stream
under its own ``LOCK_SH`` for a moment, as ``walle.survival.report`` does, and imports nothing
that appends.

Exit codes for the command: 0 the report was printed; 2 the report is off or cannot start (no
log for the principal, an ``as_of`` without a timezone, a log that does not read). There is no
"gate failed" exit, because by rule 2 there is no gate verdict for code to give.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import click

from walle import rot as walle_rot
from walle.automations import WIP_CAP, fold_automations
from walle.events import AnswerOutcome, AskKind, FutureSchemaError, now_instant
from walle.inbox import fold_inbox
from walle.paths import log_dir
from walle.reader import CorruptStreamError, Record, read_file, without_content
from walle.resolver import ResolutionError, live_events
from walle.rot import Rot
from walle.survival import SURVIVAL_DAYS, Survival, survival

GATE3_SWITCH = "WALLE_GATE3"
"""The environment variable the owner sets to let the report read a log. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on: a stray ``1`` is not consent."""

GATE_BAR = Fraction(3, 5)
"""PLAN §3's Phase 3 gate: 30-day survival strictly more than this."""

OFF_MESSAGE = (
    f"the Phase 3 gate report is off; the owner turns it on with {GATE3_SWITCH}={SWITCH_ON} "
    f"and {walle_rot.ROT_SWITCH}={walle_rot.SWITCH_ON}. nothing was read."
)

UNPROVEN = "unproven until real use"


class Gate3OffError(Exception):
    """The owner has not turned the report on. Raised before anything is read."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether both ``WALLE_GATE3`` and ``WALLE_ROT`` are exactly ``on`` (rule 7).

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(GATE3_SWITCH) == SWITCH_ON and walle_rot.enabled(env)


def above(survived: int | None, cohort: int) -> bool | None:
    """Whether ``survived`` of ``cohort`` is strictly above ``GATE_BAR``; ``None`` when nothing
    was measured (rule 3)."""
    if survived is None or cohort == 0:
        return None
    return Fraction(survived, cohort) > GATE_BAR


# --- the report ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Retirement:
    """One automation a person retired on or before ``as_of``."""

    ref: str
    name: str
    retired_ref: str
    retired_at: str


@dataclass(frozen=True)
class Dead:
    """One automation still active in the register that is not running, and why."""

    ref: str
    name: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Permissions:
    """The approvals the runs asked a person for, on or before ``as_of``."""

    asked: int
    approved: int
    denied: int
    orphaned: int
    pending: int
    sessions: int
    most_in_one_session: int


@dataclass(frozen=True)
class GateReport:
    as_of: str
    survival: Survival
    rot: Rot
    active: int
    cap: int
    retirements: tuple[Retirement, ...]
    dead: tuple[Dead, ...]
    permissions: Permissions
    not_counted: tuple[tuple[str, int], ...]
    """Per fold, how many events it could not apply (rule 6)."""

    @property
    def above_bar(self) -> bool | None:
        return above(len(self.survival.survived), len(self.survival.cohort))

    @property
    def gate(self) -> str:
        """The gate line (rule 2). It never says the gate passed."""
        bar = f"more than {GATE_BAR.numerator / GATE_BAR.denominator:.0%}"
        cohort = len(self.survival.cohort)
        if cohort == 0:
            return (
                f"30-day gate: {UNPROVEN}. No automation has reached day {SURVIVAL_DAYS} on "
                f"this log, so survival is unknown, not zero; the bar is {bar}."
            )
        survived = len(self.survival.survived)
        side = "above" if self.above_bar else "at or below"
        return (
            f"30-day gate: {UNPROVEN}. On this log {survived} of {cohort} automations still "
            f"ran at day {SURVIVAL_DAYS} ({survived / cohort:.1%}), {side} the bar of {bar}. "
            "walle cannot tell real use from a planted or test log, so only the owner can "
            f"call the gate passed, after {SURVIVAL_DAYS} days of his own use."
        )


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def gate_report(records: Iterable[Record], *, as_of: datetime) -> GateReport:
    """The report from ``records`` as of ``as_of`` (rules 1 to 6). Pure."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must carry a timezone: a naive date is not an instant")
    known = [r for r in records if _at(r.event.ts) <= as_of]
    register = fold_automations(known)
    lived = survival(known, as_of=as_of)
    labels = walle_rot.rot(register, known, as_of)
    inbox = fold_inbox(known)
    events, _ = live_events(known)

    retirements = tuple(
        Retirement(e.ref, e.name, e.retired_ref, events[e.retired_ref].ts)
        for e in register.retired
        if e.retired_ref is not None
    )

    stopped = {v.ref: v for v in lived.stopped}
    dead: list[Dead] = []
    for entry in register.active:
        reasons: list[str] = []
        if (verdict := stopped.get(entry.ref)) is not None:
            reasons.append(f"stopped by its day {SURVIVAL_DAYS}: {verdict.reason}")
        label = labels.label(entry.ref)
        if label is not None and label.status != "active":
            reasons.append(f"labelled {label.status} by rot: {label.reason}")
        if reasons:
            dead.append(Dead(entry.ref, entry.name, tuple(reasons)))

    approvals = [item for item in inbox.items if item.ask.kind is AskKind.APPROVAL]
    per_session: dict[str, int] = {}
    for item in approvals:
        per_session[item.ask.session] = per_session.get(item.ask.session, 0) + 1
    permissions = Permissions(
        asked=len(approvals),
        approved=sum(i.outcome is AnswerOutcome.APPROVED for i in approvals),
        denied=sum(i.outcome is AnswerOutcome.DENIED for i in approvals),
        orphaned=sum(i.outcome is AnswerOutcome.ORPHANED for i in approvals),
        pending=sum(i.outcome is None for i in approvals),
        sessions=len(per_session),
        most_in_one_session=max(per_session.values(), default=0),
    )

    return GateReport(
        as_of=lived.as_of,
        survival=lived,
        rot=labels,
        active=len(register.active),
        cap=WIP_CAP,
        retirements=retirements,
        dead=tuple(dead),
        permissions=permissions,
        not_counted=(
            ("register", len(register.unapplied)),
            ("survival", len(lived.unapplied)),
            ("inbox", len(inbox.unapplied)),
        ),
    )


def render(report: GateReport) -> list[str]:
    """The report as lines for a person: names, refs, timestamps and counts only (rule 5)."""
    s = report.survival
    lines = [f"as of {report.as_of}"]
    if s.rate is None:
        lines.append(f"survival: no automation at day {SURVIVAL_DAYS} yet; unknown, not zero")
    else:
        lines.append(
            f"survival: {len(s.survived)} of {len(s.cohort)} still ran at day {SURVIVAL_DAYS} "
            f"({s.rate:.1%})"
        )
    lines.append(f"  not at day {SURVIVAL_DAYS} yet: {len(s.not_yet)}")
    for v in s.stopped:
        lines.append(f"  stopped: '{v.name}' ({v.ref}): {v.reason}")
    lines.append(
        f"register: {report.active} active of a cap of {report.cap} (D6); "
        f"{len(report.retirements)} retired"
    )
    for r in report.retirements:
        lines.append(f"  retired: '{r.name}' ({r.ref}) by {r.retired_ref} at {r.retired_at}")
    lines.append(f"dead: {len(report.dead)} active but not running")
    for d in report.dead:
        lines.append(f"  dead: '{d.name}' ({d.ref}): {'; '.join(d.reasons)}")
    p = report.permissions
    lines.append(
        f"approvals: {p.asked} asked in {p.sessions} sessions, at most {p.most_in_one_session} "
        f"in one; {p.approved} approved, {p.denied} denied, {p.orphaned} orphaned, "
        f"{p.pending} pending"
    )
    lines.append(
        "not counted: " + ", ".join(f"{name} {count}" for name, count in report.not_counted)
    )
    lines.append(report.gate)
    return lines


# --- reading --------------------------------------------------------------------------------


def report(
    root: Path,
    principal: str,
    *,
    as_of: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> GateReport:
    """The report from the principal's log as it is now, as of ``as_of`` (now, when not given).

    Raises ``Gate3OffError`` before anything is read unless both switches are on (rule 7), and
    ``FileNotFoundError`` for a principal with no log. Writes nothing.
    """
    if not enabled(environ):
        raise Gate3OffError(OFF_MESSAGE)
    root = Path(root)
    log = log_dir(root, principal)
    if not log.is_dir():
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}")
    when = as_of if as_of is not None else _at(now_instant())
    return gate_report(_read(root, principal), as_of=when)


def _read(root: Path, principal: str) -> list[Record]:
    """Every record in every stream of the principal's log, each read under its own
    ``LOCK_SH``. No index: the report must not depend on a derived file."""
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


_READ_ERRORS = (
    OSError,
    ValueError,
    ResolutionError,
    CorruptStreamError,
    FutureSchemaError,
)


@click.command("gate3")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="The folder that holds principals/.",
)
@click.option("--principal", required=True, help="Whose log to report on, without the p- prefix.")
@click.option(
    "--as-of",
    "as_of",
    default=None,
    help="An RFC 3339 instant with a timezone, such as 2026-12-01T09:00:00+05:30. Default: now.",
)
def gate3_command(root: Path, principal: str, as_of: str | None) -> None:
    """The Phase 3 gate report: survival, retirements, dead automations and approvals (D6).

    Off unless WALLE_GATE3=on and WALLE_ROT=on. Reads one principal's log and writes nothing.
    Never calls the 30-day gate passed: that needs real use, and is the owner's call. Exits 0
    when the report was printed, 2 when it is off or cannot start.
    """
    if not enabled():
        click.echo(OFF_MESSAGE, err=True)
        raise SystemExit(2)
    when: datetime | None = None
    if as_of is not None:
        try:
            when = datetime.fromisoformat(as_of)
        except ValueError:
            when = None
        if when is None or when.tzinfo is None or when.utcoffset() is None:
            click.echo(
                f"--as-of {as_of!r} is not an instant with a timezone; nothing was read.",
                err=True,
            )
            raise SystemExit(2)
    try:
        result = report(root, principal, as_of=when)
    except _READ_ERRORS as exc:
        click.echo(
            f"the report cannot start, nothing was reported: {without_content(exc)}", err=True
        )
        raise SystemExit(2) from exc
    click.echo(f"Phase 3 gate report for {principal}")
    for line in render(result):
        click.echo(line)
