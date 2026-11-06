"""Rot detection: which active automations have gone stale or archived (D8, D114, issue #96).

D114 copies Hermes's curator: timestamps move things between ``active``, ``stale`` and
``archived`` with no model in the loop, ``pinned`` bypasses every automatic transition, and
nothing a live automation depends on ages out. D72 says only a person changes the register,
narrowing included. The owner reconciled the two on 2026-11-06 (decision 1 on #96): the
status is **derived and writes nothing**. ``rot(register, records, now)`` is a pure function
of the log and an explicit ``now``; an automation it labels archived stays in the register,
active, until a person retires it, and the WIP cap's refusal offers archived ones first
(``walle.automations.add``). Whether D72 should be amended is the owner's call after 30
days of real use, not this module's.

The rules
---------
1. **Idle means nothing happened to it.** An automation's last activity is the latest of
   its add, and every applied ``pin``, ``unpin`` and ``restore`` of it, and every live
   ``outcome`` naming it, ``ok`` or ``failed`` (decision 3; outcomes are #97's). A pin or
   restore the register refused (a machine wrote it, it changed nothing) is not activity,
   and neither is an outcome a live ``supersede`` took back.
2. **Stale at 30 days, archived at 90**, both inclusive and both configurable per call
   (``stale_after``, ``archive_after``). ``STALE_AFTER`` and ``ARCHIVE_AFTER`` are the
   owner's defaults. D114 quotes Hermes's 14 and 30 as a reference point; the owner chose
   longer.
3. **``pinned`` is never labelled** (D114). A pinned automation is ``active`` however long it
   has been idle, and its label says why. Its clock still runs, so an unpin starts from the
   unpin, which is itself activity.
4. **What a live automation names is never labelled** (D114, decision 2). An active
   automation whose ref is in another active automation's ``depends_on`` is ``active``, and
   its label names who holds it. "Live" is "active in the register": an automation labelled
   archived is still in the register, so it still holds what it names. That is the
   conservative reading, and the one that can never label too much.
5. **As of ``now``, and nothing after.** An event stamped after ``now`` does not move the
   clock, so the labels for a moment do not change as the log grows past it. An add stamped
   after ``now`` (a device whose clock is ahead) is idle for zero time, never negative.
   A naive ``now`` is refused rather than guessed at.
6. **Labels only, never a write.** This module imports no writer and appends, renames and
   removes nothing. ``restore`` is the register's write (``walle.automations.restore``),
   person-only and off with the register.
7. **Off by default** (the owner's dormant condition on #96, D117). ``labels`` reads a
   principal's log only once the owner has set ``WALLE_ROT=on`` in the environment, outside
   the log; it refuses before reading anything otherwise. The register's switch does not
   turn this on. The pure ``rot`` reads nothing but its arguments, so it needs no switch.
   Nothing calls ``labels`` on its own, the CLI has no command for it, and the only other
   caller of ``rot`` is the register's cap refusal, while both switches are on.

There is no two-writer walkthrough because there is no writer: ``rot`` reads records it is
handed and ``labels`` reads each stream under its own ``LOCK_SH`` for a moment, like
``walle.automations.active``. Two readers see the same labels for the same log and ``now``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from walle.automations import Entry, Register, fold_automations
from walle.events import Automation, AutomationVerb, Outcome, now_instant, parse_ref
from walle.merge import fold_order
from walle.paths import log_dir
from walle.reader import Record, read_file
from walle.resolver import live_events

ROT_SWITCH = "WALLE_ROT"
"""The environment variable the owner sets to let walle read a log for labels. Nothing else."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_AUTOMATIONS``: a stray ``1`` is not consent."""

STALE_AFTER = timedelta(days=30)
"""The owner's default idle time before an automation is labelled stale (decision 3)."""

ARCHIVE_AFTER = timedelta(days=90)
"""The owner's default idle time before an automation is labelled archived (decision 3)."""

type RotStatus = Literal["active", "stale", "archived"]

_CLOCK_VERBS = (AutomationVerb.PIN, AutomationVerb.UNPIN, AutomationVerb.RESTORE)


class RotOffError(Exception):
    """The owner has not turned rot detection on. Raised before anything is read."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned rot detection on: ``WALLE_ROT`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(ROT_SWITCH) == SWITCH_ON


@dataclass(frozen=True)
class Label:
    """One active automation, and how long it has been idle as of ``now``."""

    ref: str
    name: str
    status: RotStatus
    last_activity: str
    """The ``ts`` of its latest activity on or before ``now`` (rule 1): its add at the least."""
    idle: timedelta
    reason: str
    pinned: bool = False
    held_by: tuple[str, ...] = ()
    """The active automations whose ``depends_on`` names it (rule 4)."""


@dataclass(frozen=True)
class Rot:
    """The labels as of ``now``: one per active automation, in the register's order."""

    now: str
    labels: tuple[Label, ...]

    @property
    def active(self) -> tuple[Label, ...]:
        return tuple(label for label in self.labels if label.status == "active")

    @property
    def stale(self) -> tuple[Label, ...]:
        return tuple(label for label in self.labels if label.status == "stale")

    @property
    def archived(self) -> tuple[Label, ...]:
        return tuple(label for label in self.labels if label.status == "archived")

    def label(self, ref: str) -> Label | None:
        """The label of the active automation added at ``ref``, if it is active."""
        return next((label for label in self.labels if label.ref == ref), None)

    @property
    def offer(self) -> Label | None:
        """What a full register offers first (decision 1): the archived automation idle the
        longest, else the stale one idle the longest, ties by ref; ``None`` when none is
        either. Pinned and held automations are always ``active``, so never offered."""
        for group in (self.archived, self.stale):
            if group:
                return min(group, key=lambda label: (-label.idle, parse_ref(label.ref)))
        return None


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _spell(moment: datetime) -> str:
    """A moment in the log's own spelling, so a label quotes the ``ts`` it read."""
    return moment.isoformat(timespec="microseconds")


def rot(
    register: Register,
    records: Iterable[Record],
    now: datetime,
    *,
    stale_after: timedelta = STALE_AFTER,
    archive_after: timedelta = ARCHIVE_AFTER,
) -> Rot:
    """Label every active automation in ``register`` as of ``now`` (rules 1 to 5).

    ``register`` is ``walle.automations.fold_automations`` over the same ``records``; an
    entry whose add is not among them is a ``ValueError``, because its clock cannot be read.
    Pure: the same register, records (in any order, with duplicates) and ``now`` give the
    same labels, and nothing is read, written or asked of a model.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must carry a timezone: a naive date is not an instant")
    if stale_after <= timedelta(0) or archive_after <= stale_after:
        raise ValueError(
            f"stale_after ({stale_after}) must be positive and less than archive_after "
            f"({archive_after}), or the labels have no order"
        )
    events, live = live_events(records)
    active = register.active
    for entry in active:
        added = events.get(entry.ref)
        if not (isinstance(added, Automation) and added.verb is AutomationVerb.ADD):
            raise ValueError(
                f"the register holds {entry.ref}, which is not an add in these records: "
                "fold the register from the records rot is given"
            )

    refused = {u.ref for u in register.unapplied}
    seen: dict[str, tuple[datetime, str]] = {e.ref: (_at(e.added), e.ref) for e in active}
    for ref in fold_order(events):
        event = events[ref]
        if isinstance(event, Automation):
            if event.verb not in _CLOCK_VERBS or ref in refused:
                continue
            target = event.target
        elif isinstance(event, Outcome):
            if not live[ref]:
                continue
            target = event.target
        else:
            continue
        moment = _at(event.ts)
        if target in seen and moment <= now and moment > seen[target][0]:
            seen[target] = (moment, ref)

    def label(entry: Entry) -> Label:
        last, by = seen[entry.ref]
        idle = max(now - last, timedelta(0))
        since = "its add" if by == entry.ref else by
        held_by = register.held_by(entry.ref)
        status: RotStatus
        if entry.pinned:
            status, reason = "active", f"pinned ({entry.pin_ref}): never labelled (D114)"
        elif held_by:
            status = "active"
            reason = f"named in depends_on by live {', '.join(held_by)}: never labelled (D114)"
        elif idle >= archive_after:
            status = "archived"
            reason = f"idle {idle.days} days since {since}, {archive_after.days} or more"
        elif idle >= stale_after:
            status = "stale"
            reason = f"idle {idle.days} days since {since}, {stale_after.days} or more"
        else:
            status, reason = "active", f"idle {idle.days} days since {since}"
        return Label(
            ref=entry.ref,
            name=entry.name,
            status=status,
            last_activity=_spell(last),
            idle=idle,
            reason=reason,
            pinned=entry.pinned,
            held_by=held_by,
        )

    return Rot(now=_spell(now), labels=tuple(label(entry) for entry in active))


# --- reading --------------------------------------------------------------------------------


def labels(
    root: Path,
    principal: str,
    *,
    now: datetime | None = None,
    stale_after: timedelta = STALE_AFTER,
    archive_after: timedelta = ARCHIVE_AFTER,
    environ: Mapping[str, str] | None = None,
) -> Rot:
    """The labels for the principal's log as it is now, as of ``now`` (now, when not given).

    Raises ``RotOffError`` before anything is read unless the owner has set ``WALLE_ROT=on``
    (rule 7), and ``FileNotFoundError`` for a principal with no log. Writes nothing.
    """
    if not enabled(environ):
        raise RotOffError(
            f"rot detection is off; the owner turns it on with {ROT_SWITCH}={SWITCH_ON}"
        )
    root = Path(root)
    log = log_dir(root, principal)
    if not log.is_dir():
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}")
    when = now if now is not None else _at(now_instant())
    records = _read(root, principal)
    return rot(
        fold_automations(records),
        records,
        when,
        stale_after=stale_after,
        archive_after=archive_after,
    )


def _read(root: Path, principal: str) -> list[Record]:
    """Every record in every stream of the principal's log, each read under its own
    ``LOCK_SH``. No index: the labels must not depend on a derived file."""
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]
