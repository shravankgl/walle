"""`walle stats`: how many proposals were kept, edited and dropped (D49, PLAN Phase 1 gate).

Phase 1's gate is "proposal acceptance rate is respectable" (PLAN §3). This module is that
number, and it is a fold over the log like everything else that answers a question about
memory: the same records in any order give the same counts, the index only makes them quick
to read, and destroying it changes nothing (hard constraint 5). No model is called.

What became of a proposal is read the way the staging queue reads it (D49, D120, ``Proposal``):

``kept``     an ``assertion`` cites it in ``caused_by`` and says what it said.
``edited``   an ``assertion`` cites it and its body or summary differs.
``dropped``  a live ``supersede`` retires it and no assertion cites it.
``staged``   neither: it is still in the queue (``resolver.staged``).

These four cover every proposal exactly once. Three rules decide the edges, all of them the
ones the staging queue already uses, so `walle review` and `walle stats` cannot disagree:

- **An assertion decides.** A proposal kept on one device and dropped on another before
  their logs met is kept, as ``resolver.staged`` and ``staging._still_staged`` say.
- **A decision stays a decision.** A kept proposal whose assertion was later forgotten is
  still kept: the question is what review said, not what is believed today.
- **An undone drop is staged.** Superseding the tombstone puts the proposal back in the
  queue (hard constraint 2), so it is counted there.

If more than one assertion cites a proposal (two devices keeping it before they synced), the
first in merge order (`walle.merge`: by stamp across streams, by seq within one) decides keep
versus edit, the one `walle review` would name. An "edit" that changed nothing is a keep: the
log cannot tell them apart, and the text is what counts.

Counts only, never text. The report holds integers and nothing else, so the command cannot
print what a proposal says even by mistake.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from walle import index
from walle.events import Assertion, Proposal
from walle.merge import fold_order
from walle.reader import Record
from walle.resolver import retired_refs, staged


@dataclass(frozen=True)
class Counts:
    """What became of every proposal in the log. Integers only."""

    kept: int = 0
    edited: int = 0
    dropped: int = 0
    staged: int = 0

    @property
    def proposals(self) -> int:
        return self.kept + self.edited + self.dropped + self.staged

    @property
    def accepted(self) -> int:
        """Kept or edited: a human said yes to it."""
        return self.kept + self.edited

    @property
    def decided(self) -> int:
        """Every proposal a human has answered. The rate's denominator."""
        return self.accepted + self.dropped


@dataclass(frozen=True)
class Report:
    """One ``stats`` run: the counts, and how many log lines the reader could not read."""

    counts: Counts
    skipped_lines: int
    """Lines the reader skipped (D80). A skipped line may have been a proposal, so a report
    with any is incomplete, and the command says so."""


def count(records: Iterable[Record]) -> Counts:
    """Fold ``records`` into counts. Pure: no IO, no model, any order gives the same answer.

    Raises ``ResolutionError`` for a log the resolver cannot fold (two events under one ref,
    a supersede cycle), as ``resolve`` would.
    """
    records = tuple(records)
    still_staged = {item.ref for item in staged(records)}  # validates the log as it folds
    retired = retired_refs(records)
    events = {record.ref: record.event for record in records}

    first_citing: dict[str, Assertion] = {}
    for ref in fold_order(events):
        event = events[ref]
        if isinstance(event, Assertion):
            for cause in event.caused_by:
                first_citing.setdefault(cause, event)

    kept = edited = dropped = waiting = 0
    for ref, event in events.items():
        if not isinstance(event, Proposal):
            continue
        assertion = first_citing.get(ref)
        if assertion is not None:
            if assertion.body == event.body and assertion.summary == event.summary:
                kept += 1
            else:
                edited += 1
        elif ref in still_staged:
            waiting += 1
        elif ref in retired:
            dropped += 1
        else:  # pragma: no cover - the four cases are exhaustive; see the module docstring
            raise AssertionError(f"{ref} is neither decided nor staged")
    return Counts(kept=kept, edited=edited, dropped=dropped, staged=waiting)


def stats(root: Path, principal: str) -> Report:
    """Count the log as it is now: the index is brought up to date first, so a proposal
    appended a moment ago is counted. No log raises ``FileNotFoundError``: a misspelt
    principal must not read as a person with nothing decided."""
    built = index.update(Path(root), principal)
    return Report(counts=count(built.records), skipped_lines=len(built.skips))


def percent(accepted: int, decided: int) -> int:
    """``accepted / decided`` as a whole percentage, halves rounded up. ``decided`` > 0.

    Integer arithmetic, so 1 of 8 is 13 and not ``round``'s banker's 12.
    """
    return (200 * accepted + decided) // (2 * decided)
