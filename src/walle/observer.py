"""The transcript observer, part 2: one ``observation`` per completed turn (D51, D56, D93, D101).

Part 1 (``transcript.py``) reads Claude Code's files into the ``parentUuid`` graph. This part
finds the turns in that graph and makes one ``Observation`` for each turn that has finished
(``observe``), then appends to a stream each one the log does not already hold
(``append_observations``).

What a turn is
--------------
A turn starts at a **prompt**: a main-thread ``user`` entry where the person speaks. That
excludes a ``tool_result`` (the harness answering a call), a ``isMeta`` line (Claude Code's
own caveats), anything in a sidechain (a subagent's instructions come from the main agent,
not the person) and a line with no text.

The turn is every entry below its prompt down to, but not including, the next prompt. The
subagent's work hangs off the turn in a sidechain. It is work the turn did and it adds no turn
of its own.

When a turn is complete (D56, D80)
----------------------------------
**Only a completed turn is observed. A token is not.** A turn runs until the person's next
real prompt, so it is complete in one of two ways:

1. **The person spoke again.** A later prompt sits below the turn. The last main-thread
   ``assistant`` entry above that prompt closes the turn, whatever its ``stop_reason``: even
   a tool call cut short is over once the person has moved on. It is not trailing, so D80's
   "pending" does not apply.
2. **No later prompt yet, and the reply has ended.** The last main-thread ``user`` or
   ``assistant`` entry in the turn, in walk order, is an ``assistant`` entry whose
   ``stop_reason`` ends a reply: ``end_turn``, ``stop_sequence``, ``max_tokens`` or
   ``refusal``. Entries of other types (``attachment``, ``system``, …) after it are passed
   over. A ``user`` line after it that is not the person (a tool result, an ``isMeta`` hook
   message) means more is coming, so the turn is pending.

The closing entry is the *last* such entry, not the first: a message split into entries that
each say ``end_turn``, or a reply that a hook continues, is read to its end.

Rule 2 also waits for a file still being written (issue #129, finding 2.3). A transcript whose
last line is torn, and which changed within ``transcript.TAIL_GRACE`` seconds, is **growing**:
the torn line is Claude Code mid-write, and it hangs off the file's last entry. If that entry
is inside the turn (below the prompt, on the main thread), what the tail holds may continue
the turn, so the turn is pending until the tail lands or the file goes quiet. Observing it
now and again after the tail landed would be two observations of one turn: the content key
below would differ, and D92's fold would not catch it. A tear older than the grace is a
crash, not a write; the turn above it is final and is observed. Rule 1 is unaffected: once
the person has spoken again, the turn above their prompt cannot grow.

``tool_use`` and ``pause_turn`` mean more is coming. An unknown reason counts as more coming,
so a new reason in a later build delays an observation and never invents one. An assistant
entry still streaming, with ``stop_reason`` still ``null``, produces nothing, and neither do
any number of them in a row. They are D56's token deltas. The message they belong to is read
whole once the turn completes.

A turn not complete yet is listed in ``pending``. It is observed on a later run, when it is.

Appending, once per turn (D92)
------------------------------
``append_observations`` takes the stream's lock (``StreamWriter.locked``), folds the stream
into the set of transcript observations it already holds, and appends each observed turn not
in that set. What is "already logged" is that fold over the log, rebuilt every run, with no
second store and no cursor file (D92). D92's own example cursor, ``max(ts)``, does not fit
here: ``ts`` is when we read a turn, not when it happened, and turns from several files and
resumes complete out of transcript order, so a high-water mark would drop late completions.

A turn is matched by its content: ``(source, observed_at, body, source_version)``. Never by
session id (D101), and not by ``ts``, which differs every run. The fold and the appends happen
under one lock, so two runs at once cannot both append a turn:

1. Runs A and B have each read the transcripts and found the same completed turn T.
2. A takes the stream's ``LOCK_EX`` and folds the stream: T is not there.
3. B asks for the lock and blocks. It has read nothing of the stream.
4. A appends T, ``fsync``s, and closes. The lock goes.
5. B gets the lock and folds the stream: T is there, with A's ``seq``. B appends nothing.

If A dies between appends, the ones before are durable and the rest are not written. The
next run appends the rest and nothing twice. The limit is the content match. A turn that
changes after it was observed (more of it arrives, which rule 2 makes unlikely but cannot
rule out) gets a second, longer observation. Telling those apart needs the prompt's ``uuid``
on the event, which is a schema change (D88, D96).

What an observation holds
-------------------------
* ``body``: the person's block, then one line per reply block along the path from the
  prompt to the entry that closed the turn. The prompt is **one** ``user:`` block: its first
  line labelled, every further line of it (a second line of text, a second text block, an
  ``[image]`` marker) unlabelled below it. ``hazards.forged_turn`` reads a ``user:`` label
  after an unlabelled line as a forged turn, and the observer must never write that shape
  for the person's own words (issue #129, finding 2.4). ``assistant:`` lines are text
  replies and ``[tool: <name>]`` markers, one per block. Thinking, tool inputs and tool
  results are left out. A block that repeats, unchanged, in a later entry of the same
  message is read once, and so is a text block that a later entry of the same message holds
  a longer version of: entries that each carry the text so far are snapshots of one block,
  not pieces of it, and only the last is the reply (issue #129, finding 2.2). Two messages
  are never folded into each other, however alike they start.
* ``observed_at``: the closing entry's timestamp, the transcript's own time (D93).
* ``ts``: when the log learns it. That is now, once for the whole run, unless the caller
  gives one.
* ``source_version``: the Claude Code build that wrote the closing entry (D101, D96).

Nothing here keys on the session id. It changes every turn and on every resume (D101), so a
turn is found through the graph alone. A turn whose prompt is in one file and whose answer is
in a resume of it is one turn.

What is never read: only ``user`` and ``assistant`` entries contribute to a body. Every other
type, known or not, is passed over. ``api-request`` lines arrive already emptied by
``transcript.py``, and they would be passed over anyway.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from walle.events import ACTOR_OBSERVER, Observation, now_instant
from walle.reader import StreamContents
from walle.transcript import TranscriptEntry, Transcripts, load, transcript_files
from walle.writer import StreamWriter

SOURCE = "transcript"

_FINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens", "refusal"})


@dataclass(frozen=True)
class ObservedTurn:
    """One completed turn and the observation made from it."""

    prompt: str
    """The ``uuid`` of the entry where the person spoke."""
    closing: str
    """The ``uuid`` of the ``assistant`` entry that completed the turn."""
    observation: Observation


@dataclass(frozen=True)
class ObserveReport:
    """What one run of the observer found."""

    turns: tuple[ObservedTurn, ...]
    """Completed turns, in the graph's walk order."""
    pending: tuple[str, ...]
    """Prompts whose turn has not completed yet, in walk order."""
    transcripts: Transcripts
    """The graph it read, with its skips and repairs, so a caller can report them."""


@dataclass(frozen=True)
class AppendReport:
    """What one ``append_observations`` call wrote."""

    appended: tuple[Observation, ...]
    """The observations appended, in walk order, with their ``seq``."""
    already_logged: tuple[ObservedTurn, ...]
    """Completed turns whose observation the stream already held, so nothing was appended
    for them. That includes a turn identical in content to one appended earlier in the same
    call, which is the content match's limit, not a second append."""
    report: ObserveReport


type _Key = tuple[str, str, str, str | None]


def _key(observation: Observation) -> _Key:
    """What makes two transcript observations the same turn. Not ``ts``, not a session id."""
    return (
        observation.source,
        observation.observed_at,
        observation.body,
        observation.source_version,
    )


def _logged(contents: StreamContents) -> set[_Key]:
    """The fold: every transcript observation already in the stream (D92)."""
    return {
        _key(event)
        for event in contents.events
        if isinstance(event, Observation) and event.source == SOURCE
    }


def append_observations(report: ObserveReport, writer: StreamWriter) -> AppendReport:
    """Append each completed turn in ``report`` that the stream does not hold yet.

    The fold and every append happen under the stream's one lock (see *Appending, once per
    turn*). With nothing completed, the stream is not touched, nor created.

    A stream the reader refuses (``CorruptStreamError``, ``FutureSchemaError``) raises before
    anything is appended. An error part-way through raises too, and leaves the appends
    before it durable; running again appends the rest.
    """
    if not report.turns:
        return AppendReport(appended=(), already_logged=(), report=report)
    appended: list[Observation] = []
    already: list[ObservedTurn] = []
    with writer.locked() as stream:
        logged = _logged(stream.read())
        for turn in report.turns:
            key = _key(turn.observation)
            if key in logged:
                already.append(turn)
                continue
            appended.append(stream.append(turn.observation))
            logged.add(key)
    return AppendReport(appended=tuple(appended), already_logged=tuple(already), report=report)


def observe_projects(projects_dir: Path, *, ts: str | None = None) -> ObserveReport:
    """Observe every transcript under ``projects_dir``.

    ``projects_dir`` is required and has no default (issue #9, D40). walle reads only a
    folder the person names. It never goes looking for one.
    """
    return observe(load(transcript_files(projects_dir)), ts=ts)


def observe(transcripts: Transcripts, *, ts: str | None = None) -> ObserveReport:
    """One ``Observation`` for each completed turn in ``transcripts``. Writes nothing."""
    stamp = now_instant() if ts is None else ts
    turns: list[ObservedTurn] = []
    pending: list[str] = []
    open_ends = _open_ends(transcripts)
    for entry in transcripts.walk():
        if not _is_prompt(entry):
            continue
        closing = _closing(transcripts, entry.uuid, open_ends)
        if closing is None:
            pending.append(entry.uuid)
            continue
        path = _path(transcripts, entry.uuid, closing.uuid)
        observation = Observation(
            ts=stamp,
            actor=ACTOR_OBSERVER,
            source=SOURCE,
            observed_at=closing.timestamp.isoformat(timespec="microseconds"),
            body=_body(path),
            source_version=closing.version,
        )
        turns.append(ObservedTurn(prompt=entry.uuid, closing=closing.uuid, observation=observation))
    return ObserveReport(turns=tuple(turns), pending=tuple(pending), transcripts=transcripts)


# --- reading one entry --------------------------------------------------------------------


def _message(entry: TranscriptEntry) -> Mapping[str, Any]:
    message = entry.raw.get("message")
    return message if isinstance(message, dict) else {}


def _blocks(entry: TranscriptEntry) -> list[Mapping[str, Any]]:
    """The message's content as a list of blocks. A bare string is one text block."""
    content = _message(entry).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _text_of(block: Mapping[str, Any]) -> str | None:
    value = block.get("text")
    return value if isinstance(value, str) and value.strip() else None


def _is_prompt(entry: TranscriptEntry) -> bool:
    """The person speaking: a main-thread user line with text and no tool result in it."""
    if entry.type != "user" or entry.is_sidechain or entry.raw.get("isMeta") is True:
        return False
    blocks = _blocks(entry)
    if any(block.get("type") == "tool_result" for block in blocks):
        return False
    return any(block.get("type") == "text" and _text_of(block) for block in blocks)


# --- finding the end of a turn ------------------------------------------------------------


def _open_ends(transcripts: Transcripts) -> frozenset[str]:
    """The last entry of each growing file: where a line still being written hangs."""
    return frozenset(uuid for uuid in transcripts.growing.values() if uuid is not None)


def _closing(
    transcripts: Transcripts, prompt: str, open_ends: frozenset[str] = frozenset()
) -> TranscriptEntry | None:
    """The entry that completes the turn started at ``prompt``, or ``None`` if none does yet.

    Depth first, in the walk's own child order, so "first" and "last" mean what they mean in
    ``Transcripts.walk``. The search stops at the next prompt and never enters a sidechain.
    ``open_ends`` are the entries a line still being written may hang from (``_open_ends``):
    a turn that reaches one may still grow, so rule 2 does not close it.
    """
    entries = transcripts.entries
    next_prompts: list[str] = []
    last: TranscriptEntry | None = None  # the last main-thread user or assistant line
    may_grow = False
    stack = list(reversed(transcripts.children.get(prompt, ())))
    while stack:
        uuid = stack.pop()
        entry = entries[uuid]
        if entry.is_sidechain:
            continue
        if _is_prompt(entry):
            next_prompts.append(uuid)
            continue
        if uuid in open_ends:
            may_grow = True
        if entry.type in ("user", "assistant"):
            last = entry
        stack.extend(reversed(transcripts.children.get(uuid, ())))

    for later in next_prompts:  # rule 1: the person spoke again
        above = _last_assistant_above(transcripts, later, prompt)
        if above is not None:
            return above
    if (  # rule 2: the reply has ended, and nothing after it says more is coming
        last is not None
        and not may_grow
        and last.type == "assistant"
        and _message(last).get("stop_reason") in _FINAL_STOP_REASONS
    ):
        return last
    return None


def _last_assistant_above(
    transcripts: Transcripts, start: str, prompt: str
) -> TranscriptEntry | None:
    uuid = transcripts.parent_of.get(start)
    while uuid is not None and uuid != prompt:
        entry = transcripts.entries[uuid]
        if entry.type == "assistant" and not entry.is_sidechain:
            return entry
        uuid = transcripts.parent_of.get(uuid)
    return None


def _path(transcripts: Transcripts, prompt: str, closing: str) -> list[TranscriptEntry]:
    """The entries from ``prompt`` down to ``closing``, both included, in order."""
    path: list[TranscriptEntry] = []
    uuid: str | None = closing
    while uuid is not None:
        path.append(transcripts.entries[uuid])
        if uuid == prompt:
            break
        uuid = transcripts.parent_of.get(uuid)
    # ``_closing`` only returns entries below ``prompt``, so the climb always reaches it.
    assert path[-1].uuid == prompt
    path.reverse()
    return path


# --- the body -----------------------------------------------------------------------------


def _body(path: Iterable[TranscriptEntry]) -> str:
    read = [
        entry
        for entry in path
        if not entry.is_sidechain
        and (entry.type == "assistant" or (entry.type == "user" and _is_prompt(entry)))
    ]  # a tool result, a meta line: not the person, and not the reply
    snapshots = _text_snapshots(read)
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(read):
        if entry.type == "user":
            parts = [p for p in (_part("user", block) for block in _blocks(entry)) if p]
            if parts:
                lines.append("user: " + "\n".join(parts))
            continue
        message_id = _message(entry).get("id")
        for block in _blocks(entry):
            part = _part("assistant", block)
            if part is None:
                continue
            if isinstance(message_id, str):
                key = (message_id, json.dumps(block, sort_keys=True, default=str))
                if key in seen or _superseded(snapshots, message_id, index, block):
                    continue
                seen.add(key)
            lines.append(f"assistant: {part}")
    return "\n".join(lines)


def _text_snapshots(read: list[TranscriptEntry]) -> dict[str, list[tuple[int, str]]]:
    """Every reply text block on the path, by message id, with the index of its entry."""
    found: dict[str, list[tuple[int, str]]] = {}
    for index, entry in enumerate(read):
        message_id = _message(entry).get("id")
        if entry.type != "assistant" or not isinstance(message_id, str):
            continue
        for block in _blocks(entry):
            if block.get("type") == "text" and (value := _text_of(block)) is not None:
                found.setdefault(message_id, []).append((index, value))
    return found


def _superseded(
    snapshots: Mapping[str, list[tuple[int, str]]],
    message_id: str,
    index: int,
    block: Mapping[str, Any],
) -> bool:
    """Whether a later entry of the same message holds a longer version of this text block:
    then this one is the text so far, and the later one is the block."""
    if block.get("type") != "text" or (value := _text_of(block)) is None:
        return False
    return any(
        later > index and len(longer) > len(value) and longer.startswith(value)
        for later, longer in snapshots.get(message_id, ())
    )


def _part(role: str, block: Mapping[str, Any]) -> str | None:
    """What one block contributes to the body, without its label."""
    kind = block.get("type")
    if kind == "text":
        return _text_of(block)
    if kind == "image":
        return "[image]"
    if kind == "tool_use" and role == "assistant":
        name = block.get("name")
        return f"[tool: {name if isinstance(name, str) and name else 'unnamed'}]"
    return None  # thinking, tool input and output, and anything not yet known
