"""The orders every fold shares: streams merged by time, and refs listed by D57 (issue #120).

A ref is ``stream:seq`` and ``seq`` counts within its stream alone (D57), so two devices'
logs merge by union with nothing to reconcile. A union has no order of its own, and the
folds that apply events one after another — the register's cap, a mission's last delta, the
inbox's first ask — need one. Sorting refs as ``(stream, seq)`` is one, but it puts every
event the laptop ever wrote before the first event the phone wrote, so a retire from the
laptop of an automation added on the phone found nothing to retire, and a pause from the
laptop was folded before the resume it came after. That is audit finding 5.1 and 5.2.

This module is the one answer, so that no fold sorts on its own and two folds cannot drift.
It has two functions because folds need two different things:

- ``fold_order`` is the order a fold **applies** events in. Every fold that applies events in
  sequence uses it: ``walle.automations``, ``walle.missions``, ``walle.inbox``,
  ``walle.survival``, ``walle.stats``.
- ``ref_order`` is the order a fold **lists** a set of refs in: D57's ``(stream, seq)``.
  ``walle.resolver`` applies nothing in sequence — a supersede and a contradiction name their
  refs — and lists its answer this way, because it is the one order a set of refs keeps when
  some of them are later forgotten (below).

The merge, as ``fold_order`` applies it
---------------------------------------
1. **A stream keeps its ``seq`` order.** Within one stream the order is ``seq``, whatever the
   stamps say. ``seq`` is what the writer assigned under the lock; it is the stream's order
   by construction (D57), and a clock is only what a device believed. One device's log
   merges to exactly its ref order, so nothing changes for a single machine, and the golden
   logs replay as they did.
2. **Streams interleave by instant.** Across streams, the event with the earlier ``ts`` goes
   first. Stamps are compared as moments, in UTC, not as strings: ``10:00+05:30`` is before
   ``05:00+00:00``.
3. **A tie falls back to D57.** Two events at the same instant on two streams are ordered
   ``(stream, seq)`` — the order D57 names, kept as the order of last resort so a tie is
   still one answer.
4. **A clock that ran backwards changes nothing it should not.** Rule 1 holds first: a
   stream's next event is eligible only once its previous one has been applied, and among
   the eligible heads the earliest instant goes first (rule 2, then rule 3). So a later
   ``seq`` with an earlier stamp waits behind its predecessor and is interleaved from there.
   It never overtakes its own stream, and it never moves another stream's events relative to
   each other. When every stream's stamps are non-decreasing this is exactly the sort by
   ``(instant, stream, seq)``; it differs only where a clock misbehaved, and there it is
   still a pure function of the log: the same records, in any order, give the same answer.

What the merge cannot promise, and ``ref_order`` can
---------------------------------------------------
Take events away and the merge of what is left need not be the old order with gaps. If the
phone stamped seq 1 at 10:10 and seq 2 at 09:50, seq 2 is applied after 10:10 while seq 1 is
there (rule 4), and at 09:50 once seq 1 is forgotten. No order can keep rule 1, rule 2 *and*
that stability when a clock ran backwards: rule 1 ties an event to its predecessor and rule
2 ties it to its stamp, and the two disagree. The folds that apply events accept that; they
run over whole streams. A listing of a set — the resolver's current and retired beliefs,
which ``walle.consolidation`` must keep identical after a snapshot forgets observations —
needs the stability, and ``(stream, seq)`` is the order that has it. Where every clock was
monotone the two orders agree on any one stream and differ only in how streams interleave.

What this module does not do: it does not read a clock, open a file, or know what an event
means. A device whose clock is ahead has its events applied later than they happened,
relative to other devices'; the fold that then cannot apply something reports it
(``unapplied``), as it always has — the order does not guess at causality.
"""

from __future__ import annotations

import heapq
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from walle.events import parse_ref

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class Stamped(Protocol):
    """Anything with the log's ``ts``: every event."""

    @property
    def ts(self) -> str: ...


def merge_order(stamps: Mapping[str, str]) -> tuple[str, ...]:
    """Every ref in ``stamps`` (``{ref: ts}``), in the order the rules above give.

    A pure function of its argument: the same refs and stamps give the same order whatever
    order the mapping holds them in. A malformed ref or stamp is a ``ValueError`` before
    anything is ordered.
    """
    queues: dict[str, list[tuple[int, datetime, str]]] = {}
    for ref, stamp in stamps.items():
        stream, seq = parse_ref(ref)
        queues.setdefault(stream, []).append((seq, _instant(stamp), ref))
    for queue in queues.values():
        queue.sort()  # rule 1: seq, within a stream
        queue.reverse()  # so the head is at the end, and pop is O(1)

    heads = [(queue[-1][1], stream, queue[-1][0]) for stream, queue in queues.items()]
    heapq.heapify(heads)  # rules 2 and 3: instant, then stream, then seq
    out: list[str] = []
    while heads:
        _, stream, _ = heapq.heappop(heads)
        queue = queues[stream]
        out.append(queue.pop()[2])
        if queue:  # rule 4: the stream's next event is eligible only now
            seq, instant, _ = queue[-1]
            heapq.heappush(heads, (instant, stream, seq))
    return tuple(out)


def fold_order(events: Mapping[str, Stamped]) -> tuple[str, ...]:
    """``merge_order`` over events by ref — the order a fold applies them in."""
    return merge_order({ref: event.ts for ref, event in events.items()})


def ref_order(refs: Iterable[str]) -> tuple[str, ...]:
    """The refs, once each, in D57's ``(stream, seq)``: the order a set of refs is listed in.
    A malformed ref is a ``ValueError``."""
    return tuple(sorted(set(refs), key=parse_ref))


def _instant(stamp: str) -> datetime:
    """The moment a stamp names. Refuses a naive one: it cannot be placed against the rest."""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError as error:
        raise ValueError(f"not an instant: {stamp!r}") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"not an instant: {stamp!r} carries no offset")
    return moment
