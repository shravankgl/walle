"""Consolidation as a fold: what a snapshot hides, what it may forget, and when one is due.

A ``snapshot`` is the Curator's digest of part of the log, written into the log as an event
(hard constraint 4, D29). Its ``body`` comes from a model and cannot be replayed, so nothing
here reads it. What this module does read is ``forgets``: the refs the digest stands in for
(D89). Everything here is a pure function of the records it is given. It opens no file, reads
no clock and calls no model, so the gate and the view can be replayed like the resolver
(hard constraint 3, D95).

The view (D89)
--------------
``view`` is the log with what live snapshots forget taken out: the cheaper input meant for
the Curator, the proposer and later the hot tier, in place of every observation ever made.
Nothing reads it yet; this task builds it and proves it safe to read.
Its one promise is that **folding the view gives exactly what folding the whole log gives**
(``resolve`` and ``staged``). That is the task's verify criterion, and it is kept by what the
view is allowed to hide, not by trusting whoever wrote the snapshot:

1. **Only observations and snapshots.** The fold never reads either for belief, so taking one
   out cannot change a belief. An assertion, proposal, supersede or contradiction that a
   snapshot names stays in the view and is reported in ``held``. A snapshot that wants to
   retire a belief needs a ``supersede``, which is a decision with a reviewer, not a digest.
2. **Only what nothing names.** A ``supersede`` target and a ``contradiction``'s
   ``caused_by`` are looked up by the fold: a missing one turns into an ``unapplied`` entry.
   So a forgotten ref that any supersede or contradiction names, live or not, is held. This is
   decided every fold, so a contradiction written *after* the snapshot pins what it names,
   and the view never depends on which of two writers got there first.
3. **Only live snapshots.** A supersede targeting a snapshot undoes it, and what it forgot
   comes back, the way superseding a supersede brings its target back (D8).
4. **Forgetting is not superseding.** A later snapshot may forget an earlier one ("summaries
   get summarized", D89). The earlier one's ``forgets`` still apply, because its digest still
   covers them. Only a supersede undoes a snapshot.

An assertion's own ``caused_by`` does not pin anything. ``resolve`` returns the assertion as
logged, ``caused_by`` and all, and never looks the causes up. ``walle why`` reads the index,
which holds the whole log, so it still explains a belief whose evidence has been consolidated.

The gate (D95)
--------------
A run is due when all three hold: the profile has been **idle** for ``Policy.idle`` (no
event other than a snapshot newer than that), at least ``Policy.new`` **observations** have
``ts`` later than the last snapshot, and at least ``Policy.gap`` has passed **since** that
snapshot. Starting values are D95's: 30 minutes, 20, 12 hours. ``now`` is an argument; the
gate never asks the clock. D89's *hard* trigger, a hot tier that no longer fits, is not this
gate and is not built here.

Times are ``ts``, the log's own clock (D93), compared as instants. Across two devices that is
two clocks, and a skewed one moves a count by the events near the boundary. That is accepted:
the gate decides when a digest is worth writing, never what any belief is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from walle.events import (
    AnyEvent,
    Contradiction,
    Observation,
    Snapshot,
    Supersede,
    parse_ref,
)

# The resolver's own dedupe and liveness, so the view can never disagree with the fold about
# which snapshot is live or which ref names what. They are private there. Promoting them is a
# change to `resolver.py`, which other tasks are editing in parallel (devlog/task-22.md).
from walle.resolver import (
    _by_ref,
    _liveness,
)

if TYPE_CHECKING:  # annotations only: this module takes records, it never reads them
    from collections.abc import Iterable, Sequence

    from walle.reader import Record

__all__ = [
    "MIN_GAP",
    "MIN_IDLE",
    "MIN_NEW",
    "Gate",
    "Held",
    "Policy",
    "SnapshotError",
    "View",
    "check_forgets",
    "forgettable",
    "gate",
    "plan_forgets",
    "view",
]

MIN_IDLE = timedelta(minutes=30)
MIN_NEW = 20
MIN_GAP = timedelta(hours=12)

FORGETTABLE_TYPES = ("observation", "snapshot")


class SnapshotError(Exception):
    """A snapshot that may not be written as asked: it forgets nothing, names a ref twice,
    names one the log does not have, or names one the view would have to hold."""


# --- the view ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """A ref a live snapshot forgets that the view keeps anyway, and why."""

    ref: str
    by: str
    """The snapshot that names it."""

    reason: str


@dataclass(frozen=True)
class View:
    """The log with what live snapshots forget taken out (D89)."""

    records: tuple[Record, ...]
    """What is left, in the order given."""

    forgotten: tuple[str, ...]
    """The refs taken out, in ref order."""

    held: tuple[Held, ...]
    """Refs a live snapshot forgets that stayed in, in (snapshot, ref) order."""


def _key(ref: str) -> tuple[str, int]:
    return parse_ref(ref)


def _pins(events: dict[str, AnyEvent]) -> dict[str, list[str]]:
    """``{ref: [events that look it up]}``: supersede targets and contradiction sides."""
    pins: dict[str, list[str]] = {}
    for ref in sorted(events, key=_key):
        event = events[ref]
        if isinstance(event, Supersede):
            pins.setdefault(event.target, []).append(ref)
        elif isinstance(event, Contradiction):
            for side in dict.fromkeys(event.caused_by):
                pins.setdefault(side, []).append(ref)
    return pins


def _why_not(ref: str, events: dict[str, AnyEvent], pins: dict[str, list[str]]) -> str | None:
    """Why ``ref`` may not be forgotten, or ``None`` when it may."""
    event = events.get(ref)
    if event is None:
        return "not in the log"
    if event.type not in FORGETTABLE_TYPES:
        article = "an" if event.type[0] in "aeiou" else "a"
        return f"{article} {event.type} event: only observations and snapshots can be forgotten"
    if ref in pins:
        return f"named by {', '.join(pins[ref])}"
    return None


def _live_snapshots(events: dict[str, AnyEvent]) -> list[tuple[str, Snapshot]]:
    live, _ = _liveness(events)
    return [
        (ref, event)
        for ref in sorted(events, key=_key)
        if isinstance(event := events[ref], Snapshot) and live[ref]
    ]


def forgettable(records: Iterable[Record]) -> frozenset[str]:
    """Every ref a snapshot could forget now: observations and snapshots nothing names."""
    events = _by_ref(records)
    pins = _pins(events)
    return frozenset(ref for ref in events if _why_not(ref, events, pins) is None)


def view(records: Iterable[Record]) -> View:
    """The records without what live snapshots forget, keeping the fold identical.

    Raises ``resolver.ResolutionError`` when one ref names two different events, checked over every
    record before anything is hidden, so a hidden ref cannot hide a corrupt log.
    """
    given = tuple(records)
    events = _by_ref(given)
    pins = _pins(events)
    hidden: set[str] = set()
    held: list[Held] = []
    for by, snapshot in _live_snapshots(events):
        for ref in dict.fromkeys(snapshot.forgets):
            reason = _why_not(ref, events, pins)
            if reason is None:
                hidden.add(ref)
            else:
                held.append(Held(ref, by, reason))
    return View(
        records=tuple(r for r in given if r.ref not in hidden),
        forgotten=tuple(sorted(hidden, key=_key)),
        held=tuple(held),
    )


# --- writing one -------------------------------------------------------------------------


def check_forgets(records: Iterable[Record], forgets: Sequence[str]) -> None:
    """Refuse a ``forgets`` list the view would not apply in full, naming every problem.

    Stricter than the view, because it runs before a write and the view runs on every read:
    the view holds back what it cannot hide, and this refuses to write a snapshot that would
    need holding back, so ``held`` stays a report of later events and never of a careless
    digest. It also refuses a ref a live snapshot already forgets. Forget the old snapshot
    instead (D89).
    """
    if not forgets:
        raise SnapshotError("a snapshot that forgets nothing stands in for nothing")
    events = _by_ref(records)
    pins = _pins(events)
    already = {ref: by for by, snapshot in _live_snapshots(events) for ref in snapshot.forgets}
    problems: list[str] = []
    seen: set[str] = set()
    for ref in forgets:
        try:
            parse_ref(ref)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if ref in seen:
            problems.append(f"{ref} is named twice")
            continue
        seen.add(ref)
        reason = _why_not(ref, events, pins)
        if reason is not None:
            problems.append(f"{ref} is {reason}")
        elif ref in already:
            problems.append(f"{ref} is already forgotten by {already[ref]}")
    if problems:
        raise SnapshotError("; ".join(problems))


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def plan_forgets(records: Iterable[Record]) -> tuple[str, ...]:
    """The older half of the observations still in the view that nothing names (D89).

    "Older" is ``observed_at``, the source's own time (D93), then ref, so the plan is the
    same whatever order the records arrive in. An odd count keeps the extra one verbatim.
    Returned in ref order, which is how ``write_snapshot`` records them.
    """
    seen = view(records)
    events = _by_ref(seen.records)
    pins = _pins(events)
    candidates = [
        (ref, event)
        for ref, event in events.items()
        if isinstance(event, Observation) and _why_not(ref, events, pins) is None
    ]
    candidates.sort(key=lambda item: (_instant(item[1].observed_at), _key(item[0])))
    older = candidates[: len(candidates) // 2]
    return tuple(sorted((ref for ref, _ in older), key=_key))


# --- the gate (D95) ----------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """D95's three thresholds. A run is due only when all three hold."""

    idle: timedelta = MIN_IDLE
    """How long the profile must have been quiet: never during a live session."""

    new: int = MIN_NEW
    """How many observations must have arrived since the last run."""

    gap: timedelta = MIN_GAP
    """How long since the last run, so a busy profile is not consolidated every half hour."""

    def __post_init__(self) -> None:
        if self.idle < timedelta(0):
            raise ValueError(f"idle must not be negative, got {self.idle}")
        if self.new < 1:
            raise ValueError(f"new must be at least 1, got {self.new}: a run needs something new")
        if self.gap < timedelta(0):
            raise ValueError(f"gap must not be negative, got {self.gap}")


@dataclass(frozen=True)
class Gate:
    """The gate's answer, with the numbers it was decided on."""

    due: bool
    reasons: tuple[str, ...]
    """One line per condition that does not hold. Empty exactly when ``due``."""

    idle_for: timedelta | None
    """``now`` minus the newest non-snapshot event. ``None`` for a log with none."""

    new_observations: int
    last_run: str | None
    """The ref of the newest snapshot, live or not: an undone run still ran."""

    since_last_run: timedelta | None


def _minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)


def _hours(span: timedelta) -> str:
    # Floored, like `_minutes`: rounding 11 h 59 m 59 s up would print "12.00 h, needs
    # 12.00 h", a refusal whose own numbers say it should have passed.
    return f"{math.floor(span.total_seconds() / 36) / 100:.2f}"


def gate(records: Iterable[Record], now: datetime, policy: Policy | None = None) -> Gate:
    """Whether a consolidation run is due at ``now`` (D95). Pure: ``now`` is the caller's.

    ``now`` must carry a time zone, because every ``ts`` does and comparing the two would
    otherwise mean guessing one.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now must carry a time zone, got {now!r}")
    policy = policy or Policy()
    events = _by_ref(records)

    runs = sorted(
        ((_instant(e.ts), _key(ref), ref) for ref, e in events.items() if isinstance(e, Snapshot)),
    )
    last_at, last_run = (runs[-1][0], runs[-1][2]) if runs else (None, None)

    activity = [_instant(e.ts) for e in events.values() if not isinstance(e, Snapshot)]
    idle_for = now - max(activity) if activity else None
    new = sum(
        1
        for e in events.values()
        if isinstance(e, Observation) and (last_at is None or _instant(e.ts) > last_at)
    )
    since = now - last_at if last_at is not None else None

    reasons: list[str] = []
    if idle_for is not None and idle_for < policy.idle:
        reasons.append(f"idle {_minutes(idle_for)} min, needs {_minutes(policy.idle)} min")
    if new < policy.new:
        reasons.append(f"{new} new observations, needs {policy.new}")
    if since is not None and since < policy.gap:
        reasons.append(
            f"{_hours(since)} h since the last run ({last_run}), needs {_hours(policy.gap)} h"
        )
    return Gate(
        due=not reasons,
        reasons=tuple(reasons),
        idle_for=idle_for,
        new_observations=new,
        last_run=last_run,
        since_last_run=since,
    )
