"""Outcomes, and how many shipped automations still run at day 30 (D6, D19, D20, issue #97).

D20's second stage is *"30 days later: the automation is still in use"*, and PLAN §3 makes
30-day survival Phase 3's metric. This module is that number, computed from the log alone:
``survival(records, as_of)`` is a pure function of the records and an explicit date. It reads
no clock, opens no file and calls no model (hard constraint 3), and keeps nothing outside the
log (hard constraint 5).

The rules
---------
1. **An outcome is one observed run.** ``outcome`` events carry the ref of an automation's add
   as ``target`` and a ``result``, ``ok`` or ``failed`` (D19: outcomes are logged, not just
   changes). Recording one is not a change to the register, so D72's human-only floor does
   not reach it: the runner (``kernel``) writes them, and a person may too.
2. **Shipped means in the register.** The cohort is every add ``walle.automations`` applied
   whose day 30 (the add's ``ts`` plus ``SURVIVAL_DAYS``) is on or before ``as_of``. An add
   the register refused, past the cap or written by a machine, was never shipped and is not
   counted. One whose day 30 is still to come is listed in ``not_yet``, not in the rate.
3. **Still running at day 30** means both: it was not retired on or before its day 30, and it
   has at least one live ``ok`` outcome in the ``RECENT_DAYS`` before it, ``(day 30 - 7 days,
   day 30]``. Anything else **stopped and counts against** the rate: retired, gone silent, or
   only failing in that week. Silence is how a stopped automation shows in the log, so there
   is no ``stopped`` result to forget to write.
4. **As of a date, not as of now.** Only events whose ``ts`` is on or before ``as_of`` are
   read, so a report about day 40 is the same before and after the log grows past day 40.
   A naive ``as_of`` is refused rather than guessed at.
5. **A tombstoned outcome did not happen as recorded.** A live ``supersede`` of an outcome
   takes it out of the count. This is the opposite of ``walle.automations`` rule 5, and
   deliberately: there a tombstone would remove an automation without a retire on record;
   here it corrects a record of a run, which is what a supersede is for.
6. **Anything not counted is reported.** A live outcome whose target is not an add the
   register holds is listed in ``unapplied`` with the reason, never dropped.
7. **Off by default.** ``record`` writes nothing unless the owner has set
   ``WALLE_OUTCOMES=on`` in the environment, outside the log (the owner's condition on #97,
   D117). It is its own switch: turning the register on does not turn this on. Reading is not
   acting: ``survival`` and ``report`` work either way. Nothing calls ``record`` on its own,
   and the CLI has no command for any of it.

Two writers, step by step
-------------------------
``record`` takes no lock of its own beyond the stream's. Writer A records an ``ok`` run of
``bin-night`` from the laptop while writer B, a person on the phone, retires it.

1. A reads every stream (each under its own ``LOCK_SH`` for a moment) and folds the register:
   ``bin-night`` is active.
2. B, under ``automations.lock``, appends the retire to ``d-phone`` and releases.
3. A appends its outcome to ``d-mbp`` under that stream's ``LOCK_EX``, fsynced.

The log now holds an outcome recorded a moment after the retire. It changes no answer: if the
retire is on or before day 30 the automation stopped (rule 3) whatever ran after it, and if it
is after day 30 the outcome is outside the week that counts. So a lock around read-and-append
would buy no correctness, and ``record`` does not take ``automations.lock``, which keeps the
runner from ever waiting on a person's register change.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from walle.automations import Entry, Unapplied, fold_automations
from walle.events import ACTOR_KERNEL, Outcome, OutcomeResult, make_ref, now_instant, parse_ref
from walle.paths import log_dir
from walle.reader import Record, read_file
from walle.resolver import live_events
from walle.writer import StreamWriter

OUTCOMES_SWITCH = "WALLE_OUTCOMES"
"""The environment variable the owner sets to turn recording on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_AUTOMATIONS``: a stray ``1`` is not consent."""

SURVIVAL_DAYS = 30
"""D20's and PLAN §3's 30 days. A constant: a second caller with another number would report
a different metric under the same name."""

RECENT_DAYS = 7
"""How recent an ``ok`` run must be at day 30 to count as still running: D114's reference
interval of 7 days, the longest gap a weekly automation leaves between runs."""


class OutcomesOffError(Exception):
    """The owner has not turned recording on. Raised before anything is read or written."""


class RefusedError(Exception):
    """This outcome may not be recorded: it names no active automation. Raised before anything
    is written."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned recording on: ``WALLE_OUTCOMES`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(OUTCOMES_SWITCH) == SWITCH_ON


# --- the fold -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One automation in the cohort, and whether it still ran at its day 30."""

    ref: str
    name: str
    title: str
    added: str
    day30: str
    survived: bool
    reason: str
    last_ok: str | None
    """The ``ts`` of its last live ``ok`` run on or before day 30, ``None`` if it never had one."""


@dataclass(frozen=True)
class Survival:
    """The report as of ``as_of``: the cohort, the ones not there yet, what was not counted."""

    as_of: str
    cohort: tuple[Verdict, ...]
    not_yet: tuple[Entry, ...] = ()
    unapplied: tuple[Unapplied, ...] = ()

    @property
    def cohort_refs(self) -> tuple[str, ...]:
        return tuple(v.ref for v in self.cohort)

    @property
    def survived(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.cohort if v.survived)

    @property
    def stopped(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.cohort if not v.survived)

    @property
    def rate(self) -> float | None:
        """Survived over cohort, or ``None`` when nothing has reached day 30: unknown, not 0."""
        return len(self.survived) / len(self.cohort) if self.cohort else None

    def summary(self) -> str:
        """The report in a sentence or three, for a person."""
        waiting = f" {len(self.not_yet)} not at day {SURVIVAL_DAYS} yet." if self.not_yet else ""
        if self.rate is None:
            return (
                f"As of {self.as_of}, no automation has reached day {SURVIVAL_DAYS}: survival "
                f"is unknown, not zero.{waiting}"
            )
        head = (
            f"As of {self.as_of}, {len(self.survived)} of {len(self.cohort)} automations "
            f"still ran at day {SURVIVAL_DAYS} ({self.rate:.0%})."
        )
        stopped = "; ".join(f"'{v.name}' ({v.ref}): {v.reason}" for v in self.stopped)
        return head + (f" Stopped: {stopped}." if stopped else "") + waiting


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def survival(records: Iterable[Record], *, as_of: datetime) -> Survival:
    """How many shipped automations still ran at their day 30, from the records as of
    ``as_of`` (rules 2 to 6). Pure: the same records, in any order and with duplicates, and
    the same ``as_of`` give the same report."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must carry a timezone: a naive date is not an instant")
    known = [r for r in records if _at(r.event.ts) <= as_of]
    register = fold_automations(known)
    events, live = live_events(known)

    ok_runs: dict[str, list[datetime]] = {}
    unapplied: list[Unapplied] = []
    for ref in sorted(events, key=parse_ref):
        event = events[ref]
        if not isinstance(event, Outcome) or not live[ref]:
            continue
        if register.entry(event.target) is None:
            unapplied.append(
                Unapplied(
                    ref,
                    f"names {event.target}, which is not an automation the register holds",
                )
            )
            continue
        if event.result is OutcomeResult.OK:
            ok_runs.setdefault(event.target, []).append(_at(event.ts))

    window = timedelta(days=RECENT_DAYS)
    cohort: list[Verdict] = []
    not_yet: list[Entry] = []
    for entry in register.entries:
        day30 = _at(entry.added) + timedelta(days=SURVIVAL_DAYS)
        if day30 > as_of:
            not_yet.append(entry)
            continue
        oks = [t for t in ok_runs.get(entry.ref, []) if t <= day30]
        last_ok = max(oks) if oks else None
        retired_at = _at(events[entry.retired_ref].ts) if entry.retired_ref else None
        if retired_at is not None and retired_at <= day30:
            survived = False
            reason = f"retired at {_spell(retired_at)} ({entry.retired_ref}), by its day 30"
        elif last_ok is not None and last_ok > day30 - window:
            survived = True
            reason = f"ran ok at {_spell(last_ok)}, within {RECENT_DAYS} days of its day 30"
        else:
            survived = False
            seen = _spell(last_ok) if last_ok is not None else "never"
            reason = (
                f"no ok run in the {RECENT_DAYS} days up to its day 30 "
                f"({_spell(day30)}); last ok run: {seen}"
            )
        cohort.append(
            Verdict(
                ref=entry.ref,
                name=entry.name,
                title=entry.title,
                added=entry.added,
                day30=_spell(day30),
                survived=survived,
                reason=reason,
                last_ok=_spell(last_ok) if last_ok is not None else None,
            )
        )
    return Survival(
        as_of=_spell(as_of),
        cohort=tuple(cohort),
        not_yet=tuple(not_yet),
        unapplied=tuple(unapplied),
    )


def _spell(moment: datetime) -> str:
    """A moment back in the log's own spelling, so a verdict quotes the ``ts`` it read."""
    return moment.isoformat(timespec="microseconds")


# --- reading and writing -------------------------------------------------------------------


def report(root: Path, principal: str, *, as_of: datetime | None = None) -> Survival:
    """The survival report from the principal's log as it is now, as of ``as_of`` (now, when
    not given). Works while recording is off: reading is not acting. A principal with no log
    raises ``FileNotFoundError``."""
    root = Path(root)
    log = log_dir(root, principal)
    if not log.is_dir():
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}")
    when = as_of if as_of is not None else _at(now_instant())
    return survival(_read(root, principal), as_of=when)


def record(
    root: Path,
    principal: str,
    device: str,
    name: str,
    result: str,
    *,
    note: str | None = None,
    actor: str = ACTOR_KERNEL,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Record one run of the active automation called ``name`` and return the outcome's ref.

    Raises ``OutcomesOffError`` while off; ``ValueError`` for a result other than ``ok`` or
    ``failed``, or a malformed device; ``FileNotFoundError`` for a principal with no log; and
    ``RefusedError`` when no automation of that name is active. All before anything is
    written.
    """
    if not enabled(environ):
        raise OutcomesOffError(
            f"recording outcomes is off; the owner turns it on with {OUTCOMES_SWITCH}={SWITCH_ON}"
        )
    outcome = OutcomeResult(result)
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    log = log_dir(root, principal)
    if not log.is_dir():
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to record")
    target = fold_automations(_read(root, principal)).find(name)
    if target is None:
        raise RefusedError(f"no active automation named '{name}'")
    event = Outcome(ts=now_instant(), actor=actor, target=target.ref, result=outcome, note=note)
    written = writer.append(event)
    assert written.seq is not None  # the writer always assigns one
    return make_ref(writer.stream, written.seq)


def _read(root: Path, principal: str) -> list[Record]:
    """Every record in every stream of the principal's log, each read under its own
    ``LOCK_SH``. No index: the report must not depend on a derived file."""
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]
