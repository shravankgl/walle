"""``walle why <ref>``: where a belief came from, and what became of it (D59, D93).

Provenance is position in the log: every event names the events it came from in
``caused_by`` (D57), so the log is a DAG and ``why`` walks it backwards. The walk is
``index.ancestors``, a ``WITH RECURSIVE`` query over the index's ``edges`` table (D59). No new
artifact and no new dependency, and like the rest of the index it is rebuilt from the log.

Each step also carries a **status**, from the resolver fold over the same records:

* an assertion is ``current``, or retired: ``forgotten by <supersede>`` or ``edited by
  <supersede>, replaced by <assertion>``, with the day the log learned it and, when the
  supersede states one, the world time it stopped being true (D93);
* a proposal is ``staged``, ``kept as <assertion>`` or ``dropped by <supersede>``.

A forgotten assertion is still explained, because forgetting is a ``supersede`` and the
assertion never leaves the log (hard constraint 2). The fold hides it from ``current``; it does
not hide it from ``why``.

Which state the answer describes
--------------------------------
``explain`` brings the index up to date (``index.update``), runs the query (``index.ancestors``),
then reads every row (``index.load_records``) for the statuses. Each takes ``index.lock`` on
its own, so a writer can append between them. That cannot make the answer wrong. ``caused_by``
never changes once written, so the chain is fixed the moment ``ref`` exists. The rows read last
are a superset of what the query saw, because the index only grows, so every step has its
event. A status may be newer than the chain by an append or two, and newer is the right way
round: it says what is true now.

Nothing here writes, and nothing here calls a model (hard constraint 3).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from walle import index
from walle.events import AnyEvent, Assertion, Proposal, Supersede, parse_ref
from walle.reader import Record
from walle.resolver import Belief, Retired, resolve, retired_by, staged


@dataclass(frozen=True)
class Step:
    """One event on the chain."""

    ref: str
    depth: int
    """0 for the ref asked about, 1 for what it cites, and so on: the shortest distance."""

    event: AnyEvent
    status: str | None
    """What became of it, for an assertion or a proposal. ``None`` for other types."""


@dataclass(frozen=True)
class Explanation:
    ref: str
    steps: tuple[Step, ...]
    """The ref itself first, then its ancestors by depth, then ref."""


class Ledger:
    """The fold over a set of records, kept for asking "what became of this ref?".

    ``context`` uses it too, to say why a fact it was asked to change is no longer current.
    """

    def __init__(self, records: Iterable[Record]) -> None:
        self.records = tuple(records)
        self.events: dict[str, AnyEvent] = {r.ref: r.event for r in self.records}
        resolution = resolve(self.records)
        self.current: dict[str, Belief] = {b.ref: b for b in resolution.current}
        self.retired: dict[str, Retired] = {r.ref: r for r in resolution.retired}
        self._staged = {s.ref for s in staged(self.records)}

    def status(self, ref: str) -> str | None:
        """A plain account of where ``ref`` stands, or ``None`` if there is nothing to say."""
        event = self.events.get(ref)
        if isinstance(event, Assertion):
            return self._assertion(ref)
        if isinstance(event, Proposal):
            return self._proposal(ref)
        return None

    def _assertion(self, ref: str) -> str | None:
        belief = self.current.get(ref)
        if belief is not None:
            if not belief.closed_by:
                return "current"
            took_over = ", ".join(belief.closed_by)
            return f"current, true until {belief.window.valid_to} ({took_over} took over)"
        retired = self.retired.get(ref)
        if retired is None:  # pragma: no cover - every assertion is current or retired
            return None
        parts = [self._retirement(s) for s in retired.by]
        if retired.valid_to is not None:
            parts.append(f"true until {retired.valid_to}")
        return "; ".join(parts)

    def _retirement(self, supersede_ref: str) -> str:
        supersede = self.events[supersede_ref]
        assert isinstance(supersede, Supersede)
        replaced = [c for c in supersede.caused_by if isinstance(self.events.get(c), Assertion)]
        text = (
            f"edited by {supersede_ref}, replaced by {', '.join(replaced)}"
            if replaced
            else f"forgotten by {supersede_ref}"
        )
        text += f" on {supersede.ts[:10]}"
        if supersede.reason:
            text += f" ({supersede.reason})"
        return text

    def _proposal(self, ref: str) -> str:
        if ref in self._staged:
            return "staged"
        kept = sorted(
            (r for r, e in self.events.items() if isinstance(e, Assertion) and ref in e.caused_by),
            key=parse_ref,
        )
        if kept:
            return f"kept as {', '.join(kept)}"
        by = retired_by(self.records, ref)
        return f"dropped by {', '.join(by)}" if by else "dropped"


def explain(root: Path, principal: str, ref: str) -> Explanation:
    """The full ``caused_by`` chain behind ``ref``, each step with its status.

    ``ValueError`` for a malformed ref, ``KeyError`` for a ref no event has, and whatever
    ``index.update`` raises for a log that cannot be indexed.
    """
    parse_ref(ref)  # malformed: ValueError, before any file is touched
    root = Path(root)
    index.update(root, principal)
    try:
        chain = index.ancestors(root, principal, ref)
    except KeyError:
        raise KeyError(f"no event {ref} in {principal}'s log") from None
    ledger = Ledger(index.load_records(root, principal))
    return Explanation(
        ref=ref,
        steps=tuple(Step(r, depth, event, ledger.status(r)) for r, depth, event in chain),
    )
