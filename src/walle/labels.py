"""Information-flow labels, computed from the provenance closure (D84, D97).

D84 puts FIDES's labels on every ``recall`` result as ``_meta.ifc``: an **integrity** label
(trusted / untrusted) and a **confidentiality** label (public / private / user-identity),
most restrictive wins on combination. D97 says how integrity is decided, and it is not a
judgment: it is computed CaMeL's way, from where the content came from.

The closure
-----------
An event's provenance closure is the event and everything its ``caused_by`` reaches (D59).
That is CaMeL's ``sources`` set. ``closure`` walks it over a ``{ref: event}`` map, once per
ref, so a cycle a hand-edited log could hold still ends.

Integrity
---------
An event is ``untrusted`` iff **any event in its closure is not the principal's own words**.
The principal's own words are events whose ``actor`` is ``user`` (the person at the CLI) or
the principal's own name. Everything else is text from a non-principal:

* an ``observation`` from the transcript holds the assistant's replies and tool output;
* a ``proposal`` written by the MCP server's ``remember`` is the host model's words;
* the proposer and the curator are models; another principal is not this one;
* an actor this build has never heard of is not the principal either. Fail closed.

Two things never count as the principal's own, whatever the actor says. An **observation**
is the transcript: the assistant's replies and tool output are in its body however the row
is attributed. A **reserved actor** (``RESERVED_ACTORS``: the observer, the proposer, the
curator, the kernel and the MCP server) is never a person, so a principal who shares such a
name does not make that actor's words their own. ``mcp_server`` refuses such a name at
start-up; the label does not rely on that.

A human keep on top of any of those does not launder it: the assertion's closure still holds
the proposal. A ref the map does not hold (a dangling ``caused_by``, which a built index
refuses, D111) cannot be checked, so it makes the closure untrusted rather than raising.

Never assigned: no event has a field for a label, and nothing here reads one. Change the log
and the label changes with it; it is recomputed on every call and stored nowhere.

Confidentiality
---------------
Every event lives in one principal's folder (D32, hard constraint 6), so its readers are that
profile and its label is ``private``. Combined over the closure, most restrictive wins, which
is still ``private``. ``user-identity`` is D18's pseudonymization boundary and nothing marks it
yet; when something does, it joins ``_RANK`` and the fold below picks it up.

Several refs at once
--------------------
A ``why`` step quotes more than one event: the step's own text, and a status built from the
supersede that retired it or the assertion that kept it. ``ifc_all`` labels such a block over
every ref it quotes, most restrictive wins, so no text sits under a label that did not look
at it.

Pure functions over events: no IO, no clock, no model (hard constraint 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from walle.events import (
    ACTOR_CURATOR,
    ACTOR_KERNEL,
    ACTOR_MCP,
    ACTOR_OBSERVER,
    ACTOR_PROPOSER,
    ACTOR_USER,
    AnyEvent,
    Observation,
)

TRUSTED = "trusted"
UNTRUSTED = "untrusted"

PUBLIC = "public"
PRIVATE = "private"
USER_IDENTITY = "user-identity"

_RANK = {PUBLIC: 0, PRIVATE: 1, USER_IDENTITY: 2}
"""Confidentiality, least to most restrictive. The combination is the maximum."""

RESERVED_ACTORS = frozenset(
    {ACTOR_OBSERVER, ACTOR_PROPOSER, ACTOR_CURATOR, ACTOR_KERNEL, ACTOR_MCP}
)
"""Actors that are never a person. Their words are never the principal's own, even for a
principal of the same name."""


def closure(events: Mapping[str, AnyEvent], ref: str) -> frozenset[str]:
    """``ref`` and every ref its ``caused_by`` reaches, including refs ``events`` lacks.

    ``KeyError`` if ``ref`` itself is not in ``events``.
    """
    if ref not in events:
        raise KeyError(f"no event {ref}")
    seen: set[str] = set()
    pending = [ref]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        event = events.get(current)
        if event is not None:
            pending.extend(event.caused_by)
    return frozenset(seen)


def is_principals_own(event: AnyEvent, principal: str) -> bool:
    """Whether ``event`` is the principal's own words: written by ``user`` or by them.

    Never for an observation, whose body is the transcript, and never for a reserved actor.
    """
    if isinstance(event, Observation) or event.actor in RESERVED_ACTORS:
        return False
    return event.actor in (ACTOR_USER, principal)


def integrity(events: Mapping[str, AnyEvent], ref: str, principal: str) -> str:
    """``trusted`` iff every event in ``ref``'s closure is the principal's own words."""
    for member in closure(events, ref):
        event = events.get(member)
        if event is None or not is_principals_own(event, principal):
            return UNTRUSTED
    return TRUSTED


def confidentiality(events: Mapping[str, AnyEvent], ref: str) -> str:
    """The most restrictive confidentiality over ``ref``'s closure.

    Every event in one principal's log is ``private`` (its readers are that profile, D32).
    """
    found = [PRIVATE for _ in closure(events, ref)]
    return max(found, key=_RANK.__getitem__)


def ifc(events: Mapping[str, AnyEvent], ref: str, principal: str) -> dict[str, str]:
    """``_meta.ifc`` for ``ref``, in FIDES's shape (D84)."""
    return {
        "integrity": integrity(events, ref, principal),
        "confidentiality": confidentiality(events, ref),
    }


def ifc_all(events: Mapping[str, AnyEvent], refs: Iterable[str], principal: str) -> dict[str, str]:
    """``_meta.ifc`` for text that quotes every ref in ``refs``: most restrictive wins.

    ``ValueError`` for no refs at all: a label that covers nothing is not a label.
    """
    found = [ifc(events, ref, principal) for ref in refs]
    if not found:
        raise ValueError("ifc_all needs at least one ref to label")
    return {
        "integrity": TRUSTED if all(f["integrity"] == TRUSTED for f in found) else UNTRUSTED,
        "confidentiality": max((f["confidentiality"] for f in found), key=_RANK.__getitem__),
    }
