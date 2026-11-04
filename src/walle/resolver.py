"""The resolver fold: which assertions are current, and over what world time (D8, D58, D93).

Everything else in walle that wants "what do we believe" asks this module, and this module
answers from its argument alone. It opens no file, reads no clock and calls no model: the
caller reads the log through ``walle.reader`` and passes the records in. Hard constraint 3 —
resolution is a pure function of the log — is the reason for that split, not a style choice.
If picking the live fact ever needed judgment, replay and the rebuild test would both stop
meaning anything.

The rules
---------
1. **Supersede hides (D8).** An event is *live* unless a live ``supersede`` targets it. An
   assertion that is not live is not current; it is returned under ``retired`` with the
   supersedes that retired it and the ``valid_to`` they carry, because a retraction has a world
   time as well as a log time (D93). Liveness is recursive, so superseding a supersede brings
   its target back, which is how a retraction is undone without editing anything.
2. **Contradiction needs a candidate (D58).** Whether two assertions are about the same thing
   is judgment, so the fold never infers it from text. The Curator records it as a
   ``contradiction`` event whose ``caused_by`` names both sides; the fold applies the rule
   below to each live one. Graphiti's rule is "pure once candidates are chosen" (D93) and this
   is that split.
3. **Overlap only (D93).** Validity windows are half-open, ``[valid_from, valid_to)``, with a
   null start meaning "no known start" and a null end meaning "still true". Two sides whose
   windows do not overlap never conflict: someone can have lived in Pune and then Bangalore.
   A window whose start is at or after its end holds no moment, so it overlaps nothing.
4. **Later-valid closes earlier (D93).** When the windows overlap and one side starts strictly
   later, the earlier side's window closes at the later side's ``valid_from``. The closing is
   computed here, every fold; the event in the log is never rewritten. An assertion closed by
   several later ones ends at the earliest of them.
5. **Otherwise it stays open.** Overlapping with no strictly later start — the same start,
   both null, or a start that cannot be ordered — is a real conflict. Both sides stay current
   and the contradiction is returned under ``conflicts``; D58 presents both rather than picking.

The staging queue (D49, D60)
---------------------------
``staged`` is the same kind of fold for ``proposal`` events: a proposal is staged while it
is live (rule 1, so a drop is a ``supersede`` and undoing a drop is superseding that) and no
``assertion`` names it in ``caused_by``. Any assertion counts, live or not: forgetting a kept
belief later must not put the proposal back in the review batch. ``resolve`` never returns a
proposal, so nothing staged can reach the hot tier.

Each contradiction is judged on the windows as written, never on windows another
contradiction has already closed. That is what makes the answer independent of input order.

Anything the fold cannot apply is **reported, not dropped**: a live supersede whose target was
not passed in, or a live contradiction that does not name exactly two assertions it was given,
lands in ``unapplied`` with a reason. A supersede cycle, or one ref naming two different
events, raises ``ResolutionError``: no answer would be true.

World time and precision
------------------------
``valid_from`` and ``valid_to`` are dates or instants (D103 keeps a date a date). Two instants
are compared in UTC. Two dates compare as dates. A date carries no time zone, so the day it
names runs, in UTC, somewhere between its midnight at UTC+14 and its end at UTC-12. An instant
outside that span is ordered against the date; inside it the order cannot be known without
inventing a time zone for the date, so that comparison is *unknown*. It depends only on the
instant's moment, so two spellings of one moment always agree. Unknown never closes a window,
and it never counts as disjoint. It is reported as an open conflict, because guessing would
look precise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from walle.events import (
    AnyEvent,
    Assertion,
    Contradiction,
    Proposal,
    Supersede,
    WorldTime,
    parse_ref,
)

if TYPE_CHECKING:  # annotations only: the fold takes records, it never reads them itself
    from collections.abc import Iterable

    from walle.reader import Record


_WORLD_TIME: TypeAdapter[str] = TypeAdapter(WorldTime)

# The widest a calendar day can be once a time zone is put on it: from its midnight in the
# earliest zone (UTC+14) to the end of the day in the latest (UTC-12), relative to UTC midnight.
_DAY_STARTS_FROM = timedelta(hours=-14)
_DAY_ENDS_BY = timedelta(days=1, hours=12)


class ResolutionError(Exception):
    """The records cannot be folded into any true answer: a supersede cycle, or one ref
    naming two different events. Fatal, like ``CorruptStreamError``, and named by ref."""


@dataclass(frozen=True)
class Window:
    """A world-time window, ``[valid_from, valid_to)`` (D93). ``None`` is unbounded."""

    valid_from: str | None = None
    valid_to: str | None = None

    def __post_init__(self) -> None:
        # The comparisons below trust the log's one spelling (``events._check_world_time``).
        # ``fromisoformat`` alone would also take ``20260301`` and order it, so a window
        # built by hand is held to the same rule as one read from an assertion.
        for value in (self.valid_from, self.valid_to):
            if value is not None and _WORLD_TIME.validate_python(value) != value:
                raise ValueError(f"not in the log's spelling: {value!r} — write Z as +00:00")


@dataclass(frozen=True)
class Belief:
    """A current assertion and the window it holds over after the fold."""

    ref: str
    assertion: Assertion
    """The event exactly as logged. Its own ``valid_to`` is never changed."""

    window: Window
    """Effective world time: as written, or closed by a later-valid assertion (rule 4)."""

    closed_by: tuple[str, ...] = ()
    """Refs of the later-valid assertions that closed this window, in ref order."""


@dataclass(frozen=True)
class Retired:
    """An assertion a live supersede hides (rule 1)."""

    ref: str
    assertion: Assertion
    by: tuple[str, ...]
    """The live supersedes that target it, in ref order."""

    valid_to: str | None
    """When it stopped being true, from those supersedes: the earliest any of them states."""


@dataclass(frozen=True)
class Conflict:
    """A live contradiction whose sides overlap with no later start (rule 5)."""

    ref: str
    """The contradiction event's own ref."""

    between: tuple[str, str]
    """The two assertions, in ref order. Both are current."""


@dataclass(frozen=True)
class Unapplied:
    """A live supersede or contradiction the fold could not apply, and why."""

    ref: str
    reason: str


@dataclass(frozen=True)
class Resolution:
    """The fold's answer. Every tuple is in ref order, ``(stream, seq)``."""

    current: tuple[Belief, ...]
    retired: tuple[Retired, ...]
    conflicts: tuple[Conflict, ...]
    unapplied: tuple[Unapplied, ...]

    def belief(self, ref: str) -> Belief:
        """The current belief for ``ref``. ``KeyError`` if it is not current."""
        for belief in self.current:
            if belief.ref == ref:
                return belief
        raise KeyError(f"{ref} is not a current assertion")


@dataclass(frozen=True)
class Staged:
    """A proposal still waiting for a keep, an edit or a drop."""

    ref: str
    proposal: Proposal


# --- world time --------------------------------------------------------------------------


def _compare(a: str, b: str) -> int | None:
    """-1, 0 or 1 for two world times, or ``None`` when the order cannot be known."""
    a_instant, b_instant = "T" in a, "T" in b
    if a_instant and b_instant:
        left, right = datetime.fromisoformat(a), datetime.fromisoformat(b)
    elif not a_instant and not b_instant:
        left, right = date.fromisoformat(a), date.fromisoformat(b)
    elif a_instant:
        return _against_day(a, b)
    else:
        order = _against_day(b, a)
        return None if order is None else -order
    return (left > right) - (left < right)


def _against_day(instant: str, day: str) -> int | None:
    """-1 if ``instant`` is before ``day`` in every time zone, 1 if after it in every one."""
    moment = datetime.fromisoformat(instant)
    midnight = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    if moment < midnight + _DAY_STARTS_FROM:
        return -1
    if moment >= midnight + _DAY_ENDS_BY:
        return 1
    return None


def _empty(window: Window) -> bool:
    """Whether ``[valid_from, valid_to)`` is known to hold no moment: start at or after end."""
    if window.valid_from is None or window.valid_to is None:
        return False
    order = _compare(window.valid_from, window.valid_to)
    return order is not None and order >= 0


def _starts_before_end(start: str | None, end: str | None) -> bool | None:
    """Whether a window starting at ``start`` begins before one ending at ``end`` does."""
    if start is None or end is None:
        return True  # an open start is before everything; an open end is after everything
    order = _compare(start, end)
    return None if order is None else order < 0


def windows_overlap(a: Window, b: Window) -> bool | None:
    """Whether two half-open windows share any moment. ``None`` when that cannot be known."""
    if _empty(a) or _empty(b):
        return False
    first = _starts_before_end(a.valid_from, b.valid_to)
    second = _starts_before_end(b.valid_from, a.valid_to)
    if first is False or second is False:
        return False
    if first is None or second is None:
        return None
    return True


def starts_later(a: Window, b: Window) -> bool | None:
    """Whether ``a`` starts strictly later than ``b``. ``None`` when that cannot be known."""
    if a.valid_from is None:
        return False  # nothing starts before an open start
    if b.valid_from is None:
        return True
    order = _compare(a.valid_from, b.valid_from)
    return None if order is None else order > 0


def _earliest(times: list[str]) -> str:
    """The earliest of ``times``, which are in a fixed order. When two cannot be ordered the
    one met first is kept, so the answer is still deterministic."""
    best = times[0]
    for candidate in times[1:]:
        if _compare(candidate, best) == -1:
            best = candidate
    return best


# --- the fold ----------------------------------------------------------------------------


def _ref_key(ref: str) -> tuple[str, int]:
    return parse_ref(ref)


def _by_ref(records: Iterable[Record]) -> dict[str, AnyEvent]:
    events: dict[str, AnyEvent] = {}
    for record in records:
        known = events.get(record.ref)
        if known is None:
            events[record.ref] = record.event
        elif known != record.event:
            raise ResolutionError(
                f"{record.ref} names two different events — the same stream read from two "
                "copies that disagree, or a corrupt log"
            )
    return events


def _liveness(events: dict[str, AnyEvent]) -> tuple[dict[str, bool], dict[str, list[str]]]:
    """Rule 1: ``live[ref]`` for every event, and ``targeted_by[ref]`` for every target.

    Iterative rather than recursive, so a long chain of supersedes cannot hit Python's
    recursion limit. A cycle has no consistent answer and raises.
    """
    targeted_by: dict[str, list[str]] = {}
    for ref in sorted(events, key=_ref_key):
        event = events[ref]
        if isinstance(event, Supersede):
            targeted_by.setdefault(event.target, []).append(ref)

    live: dict[str, bool] = {}
    for start in sorted(events, key=_ref_key):
        stack, on_path = [start], {start}
        while stack:
            ref = stack[-1]
            pending = next((s for s in targeted_by.get(ref, ()) if s not in live), None)
            if pending is not None:
                if pending in on_path:
                    cycle = [*stack[stack.index(pending) :], pending]
                    raise ResolutionError(f"supersede cycle: {' -> '.join(cycle)}")
                stack.append(pending)
                on_path.add(pending)
                continue
            live[ref] = not any(live[s] for s in targeted_by.get(ref, ()))
            stack.pop()
            on_path.discard(ref)
    return live, targeted_by


def _window(assertion: Assertion) -> Window:
    return Window(assertion.valid_from, assertion.valid_to)


def resolve(records: Iterable[Record]) -> Resolution:
    """Fold records from any number of streams into the current assertions.

    ``records`` is what ``walle.reader`` returns — one ``StreamContents``, or several chained.
    The same record passed twice counts once. The order they arrive in does not matter.
    """
    events = _by_ref(records)
    live, targeted_by = _liveness(events)
    ordered = sorted(events, key=_ref_key)
    unapplied: list[Unapplied] = []

    for ref in ordered:
        event = events[ref]
        if isinstance(event, Supersede) and live[ref] and event.target not in events:
            unapplied.append(
                Unapplied(ref, f"targets {event.target}, which the fold was not given")
            )

    # Rules 2-5, on live contradictions whose sides are both live.
    closers: dict[str, set[str]] = {}  # a set: two contradictions about one pair close once
    conflicts: list[Conflict] = []
    for ref in ordered:
        event = events[ref]
        if not isinstance(event, Contradiction) or not live[ref]:
            continue
        missing = [r for r in event.caused_by if r not in events]
        if missing:
            unapplied.append(
                Unapplied(ref, f"names {', '.join(missing)}, which the fold was not given")
            )
            continue
        sides = sorted(
            {r for r in event.caused_by if isinstance(events[r], Assertion)}, key=_ref_key
        )
        if len(sides) != 2:
            noun = "assertion" if len(sides) == 1 else "assertions"
            unapplied.append(Unapplied(ref, f"names {len(sides)} {noun}, not 2"))
            continue
        a_ref, b_ref = sides
        if not (live[a_ref] and live[b_ref]):
            continue  # a superseded side: the supersede already resolved it (D58)
        a, b = events[a_ref], events[b_ref]
        assert isinstance(a, Assertion) and isinstance(b, Assertion)
        a_window, b_window = _window(a), _window(b)
        overlap = windows_overlap(a_window, b_window)
        if overlap is False:
            continue  # rule 3
        if overlap is True and starts_later(b_window, a_window) is True:
            closers.setdefault(a_ref, set()).add(b_ref)
        elif overlap is True and starts_later(a_window, b_window) is True:
            closers.setdefault(b_ref, set()).add(a_ref)
        else:
            conflicts.append(Conflict(ref, (a_ref, b_ref)))  # rule 5

    current: list[Belief] = []
    retired: list[Retired] = []
    for ref in ordered:
        event = events[ref]
        if not isinstance(event, Assertion):
            continue
        if not live[ref]:
            by = tuple(s for s in targeted_by[ref] if live[s])
            stated = [v for s in by if (v := _supersede_valid_to(events[s])) is not None]
            retired.append(Retired(ref, event, by, _earliest(stated) if stated else None))
            continue
        window = _window(event)
        closed_by = tuple(sorted(closers.get(ref, ()), key=_ref_key))
        if closed_by:
            starts = [_start_of(events[c]) for c in closed_by]
            window = Window(event.valid_from, _earliest(starts))
        current.append(Belief(ref, event, window, closed_by))

    return Resolution(
        current=tuple(current),
        retired=tuple(retired),
        conflicts=tuple(conflicts),
        unapplied=tuple(unapplied),
    )


def staged(records: Iterable[Record]) -> tuple[Staged, ...]:
    """The staging queue: every live proposal no assertion has decided, in ref order.

    Pure, like ``resolve``: the same records in any order give the same answer.
    """
    events = _by_ref(records)
    live, _ = _liveness(events)
    decided = {
        cause
        for event in events.values()
        if isinstance(event, Assertion)
        for cause in event.caused_by
    }
    return tuple(
        Staged(ref, event)
        for ref in sorted(events, key=_ref_key)
        if isinstance(event := events[ref], Proposal) and live[ref] and ref not in decided
    )


def retired_by(records: Iterable[Record], ref: str) -> tuple[str, ...]:
    """The live supersedes that target ``ref``, in ref order. Empty when it is live.

    For any event, not only assertions: it is how a review says which tombstone dropped a
    proposal.
    """
    events = _by_ref(records)
    live, targeted_by = _liveness(events)
    return tuple(s for s in targeted_by.get(ref, ()) if live[s])


def _supersede_valid_to(event: AnyEvent) -> str | None:
    assert isinstance(event, Supersede)
    return event.valid_to


def _start_of(event: AnyEvent) -> str:
    # A closer always starts strictly later than what it closes, so it has a start.
    assert isinstance(event, Assertion) and event.valid_from is not None
    return event.valid_from
