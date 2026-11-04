"""The connector base: one interface for observation sources, with the cursor folded from
the log (D92, D93).

A connector reads a source outside walle — a calendar, a mailbox — and appends one
``observation`` per item. Every connector has the same two problems, and they are solved
here once so that a provider module holds only what is specific to its provider:

* **Where did I get to last time?** D92's answer: the cursor already exists in the log. The
  last observed moment per source is ``max(observed_at)`` over the ``observation`` events
  with that ``source``, a fold, rebuilt every run, with no side store, no cursor file and no
  row in the index. Delete the index and nothing is lost, because nothing was there.
* **How do I not append the same item twice?** A cursor alone does not answer that: a source
  asked for "everything since 10:00" may well hand back the 10:00 item again, and two runs
  at once both start from the same cursor. So the base also folds the *content* of what the
  stream already holds and appends only what is missing, under the stream's lock. The cursor
  is advice to the source about where to start; the content fold is what keeps the log
  free of duplicates.

What this file does not hold, on purpose: any provider. No vendor client, no sign-in flow, no
network. #57 (calendar) and #58 (mail) each add a module that implements ``Source`` against a
fake of its API, and nothing in this file needs to change for them. A test holds that line.

The two clocks (D93)
--------------------
D92 was written in Session 5, when an event had one timestamp. D93 split it in two: ``ts``
is when the log learned something, ``observed_at`` is the source's own time. "The last
timestamp of the retrieved dataset" — D92's own words for what the cursor is — is the
source's time, so the cursor is ``max(observed_at)``. ``max(ts)`` would be the time of our
last *read*, which tells a source nothing about what to fetch. Both are reported on the
``Cursor`` so a source that wants either has it; ``observed_at`` is the one that means
"since".

Why the cursor is advice and not a filter
-----------------------------------------
An item older than the cursor is not refused. A calendar event created today for last week,
a mail delivered late, a source whose own ordering is loose: all arrive "before" the cursor
and are still new. The observer learned this the hard way with transcript turns (its module
docstring says why a high-water mark drops late completions), and the same holds for every
source that is not strictly append-ordered on its own clock. What stops a duplicate is the
content match, not the time.

What makes two items the same: ``(source, observed_at, body, source_version)``. Never
``ts``, which differs every run. An item whose text changes after it was observed gets a
second observation, which is right: the thing observed changed. Telling the two apart needs
a stable item id on the event, and that is a schema change (D88, D96), not a base concern.

Order of operations, and why
----------------------------
1. Fold the cursor from the stream (a short ``LOCK_SH`` read; a missing stream is an empty
   cursor).
2. Ask the source: ``pull(cursor)``. Materialise everything it yields and build every
   ``Observation`` — validation included — before anything else. A source that raises half
   way, or hands back an item the schema refuses, appends nothing: not the items before the
   failure either. #57 and #58 both verify "a failed sync appends nothing partial"; this is
   where that property comes from.
3. Take the stream's lock. Fold the content keys, append each item not present, release.

The pull is outside the lock. A source is network IO measured in seconds, and the lock is
shared with the hook, the CLI and the observer: holding it across a page of API calls would
stall every one of them for the length of a sync.

Two runners, step by step
-------------------------
A and B both run "sync the calendar" at the same time. The stream holds nothing from it.

1. A and B each fold the cursor: both see "nothing yet". Both ask the source for everything.
   Both get the same three items. Neither has taken the stream's lock.
2. A takes ``LOCK_EX`` and folds the stream: none of the three is there.
3. B asks for the lock and blocks. B has read nothing of the stream under the lock yet; the
   cursor it folded in step 1 was never a claim about what B would write.
4. A appends the three, each ``fsync``ed, and closes. The lock goes.
5. B wakes and folds the stream: all three are there, with A's seqs. B appends nothing and
   reports them as already logged.

Had B folded before blocking on the lock and acted on that, it would have appended three
more: six events, two refs for each thing that happened. The fold that decides what to
append happens only under the lock.

If A dies in step 4 after the first item, the first is durable and the rest were never
acknowledged. B, or the next run, appends the other two and nothing twice. The log can be
partial after a crash — it is append-only, so there is no transaction to roll back — and the
next run finishes the job. That is a different thing from step 2's guarantee, which is about
the source failing, not the writer.

Now A syncs while B is the observer on the same stream. The lock is one lock per stream, so
A's appends and B's are serialised in whichever order they arrive; each folds its own source
under the lock and neither sees the other's items as its own (the key carries ``source``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from walle.events import ACTOR_OBSERVER, AnyEvent, Observation, now_instant
from walle.reader import StreamContents, read_stream
from walle.writer import StreamWriter


@dataclass(frozen=True)
class Item:
    """One thing a source observed, before it is an event.

    Deliberately smaller than ``Observation``: a source says *what* and *when in its own
    time*, and the base supplies ``ts``, ``actor`` and ``source``. A source cannot set
    ``caused_by`` or ``actor``, so it cannot forge provenance.
    """

    observed_at: str
    """The source's own time for the item, as an RFC 3339 instant (D93). Validated when
    the observation is built, before anything is appended."""
    body: str
    """The item's text. Untrusted: it came from outside. The envelope that says so is applied
    where text is recalled (D55), not here, and nothing in the base reads it."""
    source_version: str | None = None
    """The version of what produced it, when the source has one (D101)."""


@dataclass(frozen=True)
class Cursor:
    """Where a source got to, folded from the log (D92). Nothing stores one."""

    source: str
    observed_at: str | None
    """The latest ``observed_at`` among this source's observations: the "since" a source
    wants. ``None`` when the log holds nothing from it."""
    ts: str | None
    """The latest ``ts`` among them: when the log last learned something from this source."""
    events: int
    """How many observations from this source the stream holds."""


@runtime_checkable
class Source(Protocol):
    """What a connector provides. Everything else is the base's job."""

    @property
    def name(self) -> str:
        """The ``source`` written on each observation: ``calendar``, ``mail``. One source,
        one name, forever — the cursor is folded by it."""
        ...

    def pull(self, cursor: Cursor) -> Iterable[Item]:
        """Everything observed since ``cursor``, in the source's own order.

        "Since" is advice: hand back the boundary item again, or an older one that arrived
        late, and the base appends each once. Raise on any failure, and nothing is appended.
        """
        ...


@dataclass(frozen=True)
class SyncReport:
    """What one ``sync`` did."""

    before: Cursor
    """The cursor the source was asked with."""
    appended: tuple[Observation, ...]
    """What was appended, in the source's order, with ``seq`` set."""
    already_logged: tuple[Item, ...]
    """Items the stream already held, including an item repeated within one pull."""
    after: Cursor
    """The fold over what the stream held under the lock plus what this run appended: the
    cursor at the moment the lock went. Another writer may have appended since."""


type _Key = tuple[str, str, str, str | None]


def _key(event: Observation) -> _Key:
    """What makes two observations of one source the same item. Not ``ts``."""
    return (event.source, event.observed_at, event.body, event.source_version)


def _instant(value: str) -> datetime:
    # Both spellings an ``Instant`` can have carry an offset, so these compare as moments.
    return datetime.fromisoformat(value)


def cursor_of(contents: StreamContents, source: str) -> Cursor:
    """The fold: this source's cursor over one stream's contents. Pure, and reads no clock."""
    return _fold(contents.events, source)


def _fold(events: Sequence[AnyEvent], source: str) -> Cursor:
    # The maxima are kept as the strings the log holds, compared as the moments they name.
    # Two spellings of one moment (``+05:30`` beside ``+00:00``) keep the first seen; the
    # fold is still deterministic, because the log has one order.
    observed: tuple[datetime, str] | None = None
    learned: tuple[datetime, str] | None = None
    count = 0
    for event in events:
        if not isinstance(event, Observation) or event.source != source:
            continue
        count += 1
        observed = _later(observed, event.observed_at)
        learned = _later(learned, event.ts)
    return Cursor(
        source=source,
        observed_at=None if observed is None else observed[1],
        ts=None if learned is None else learned[1],
        events=count,
    )


def _later(best: tuple[datetime, str] | None, value: str) -> tuple[datetime, str]:
    moment = _instant(value)
    return (moment, value) if best is None or moment > best[0] else best


def fold_cursor(root: Path, principal: str, device: str, source: str) -> Cursor:
    """Read the stream and fold the cursor. A stream that does not exist yet is empty.

    The reader refuses a missing stream so that a misspelt principal does not read as someone
    with no history. A sync is a write, not a read: before the first one there is no stream,
    and ``StreamWriter`` will create it under this root either way. So a missing file is
    "nothing yet" here, and only that error is caught.
    """
    try:
        contents = read_stream(root, principal, device)
    except FileNotFoundError:
        return Cursor(source=source, observed_at=None, ts=None, events=0)
    return cursor_of(contents, source)


def _logged(contents: StreamContents, source: str) -> set[_Key]:
    """Every item from ``source`` the stream already holds, by content (D92)."""
    return {
        _key(event)
        for event in contents.events
        if isinstance(event, Observation) and event.source == source
    }


def sync(source: Source, writer: StreamWriter, *, ts: str | None = None) -> SyncReport:
    """Pull from ``source`` and append each item the stream does not hold yet.

    ``ts`` is when the log learns the batch: now, once for the run, unless given. See the
    module docstring for the order of operations and the two-runner walkthrough.

    Raises whatever the source raises, and ``pydantic.ValidationError`` for an item the
    schema refuses; in both cases nothing has been appended. A stream the reader refuses
    (``CorruptStreamError``, ``FutureSchemaError``) raises before anything is appended too.
    """
    name = source.name
    before = fold_cursor(writer.root, writer.principal, writer.device, name)
    stamp = now_instant() if ts is None else ts
    # Everything the source has to say, validated, before the lock and before any append.
    pulled = [
        (
            item,
            Observation(
                ts=stamp,
                actor=ACTOR_OBSERVER,
                source=name,
                observed_at=item.observed_at,
                body=item.body,
                source_version=item.source_version,
            ),
        )
        for item in source.pull(before)
    ]
    if not pulled:
        return SyncReport(before=before, appended=(), already_logged=(), after=before)

    appended: list[Observation] = []
    already: list[Item] = []
    with writer.locked() as stream:
        contents = stream.read()
        logged = _logged(contents, name)
        for item, observation in pulled:
            key = _key(observation)
            if key in logged:
                already.append(item)
                continue
            appended.append(stream.append(observation))
            logged.add(key)
    # The same fold the next run will do, over what the stream held plus what joined it.
    after = _fold([*contents.events, *appended], name)
    return SyncReport(
        before=before, appended=tuple(appended), already_logged=tuple(already), after=after
    )
