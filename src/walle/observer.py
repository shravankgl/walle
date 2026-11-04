"""The transcript observer, part 2: one ``observation`` per completed turn (D51, D56, D93, D101).

Part 1 (``transcript.py``) reads Claude Code's files into the ``parentUuid`` graph. This part
finds the turns in that graph and makes one ``Observation`` for each turn that has finished.
It returns the events and writes nothing: they come back unsequenced (``seq`` is ``None``),
for the stream writer to append.

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
**Only a completed turn is observed. A token is not.** A turn is complete at the first main-
thread ``assistant`` entry, in walk order, whose ``stop_reason`` is one that ends a reply:
``end_turn``, ``stop_sequence``, ``max_tokens`` or ``refusal``. ``tool_use`` and
``pause_turn`` mean more is coming. An unknown reason counts as more coming, so a new reason
in a later build delays an observation and never invents one.

So an assistant entry still streaming, with ``stop_reason`` still ``null``, produces nothing.
Neither do any number of them in a row. They are D56's token deltas. The message they belong
to is read whole once the turn completes.

A build that never writes a ``stop_reason`` would then never complete a turn. So there is one
more way to complete one: the person spoke again. If a later prompt sits directly below the
turn, the last ``assistant`` entry above that prompt closes the turn, as long as it is not
calling a tool. A call with no result is pending, not finished (D80).

A turn not complete yet is listed in ``pending``. It is observed on a later run, when it is.

What an observation holds
-------------------------
* ``body``: one line per content block along the path from the prompt to the entry that
  closed the turn. ``user:`` lines are the prompt's text. ``assistant:`` lines are text
  replies and ``[tool: <name>]`` markers. Thinking, tool inputs and tool results are left
  out. A block that repeats, unchanged, in a later entry of the same message is read once.
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
from walle.transcript import TranscriptEntry, Transcripts, load, transcript_files

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
    for entry in transcripts.walk():
        if not _is_prompt(entry):
            continue
        closing = _closing(transcripts, entry.uuid)
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


def _calls_a_tool(entry: TranscriptEntry) -> bool:
    return any(block.get("type") == "tool_use" for block in _blocks(entry))


# --- finding the end of a turn ------------------------------------------------------------


def _closing(transcripts: Transcripts, prompt: str) -> TranscriptEntry | None:
    """The entry that completes the turn started at ``prompt``, or ``None`` if none does yet.

    Depth first, in the walk's own child order, so "first" means the same thing it means in
    ``Transcripts.walk``. The search stops at the next prompt and never enters a sidechain.
    """
    entries = transcripts.entries
    next_prompts: list[str] = []
    stack = list(reversed(transcripts.children.get(prompt, ())))
    while stack:
        uuid = stack.pop()
        entry = entries[uuid]
        if entry.is_sidechain:
            continue
        if _is_prompt(entry):
            next_prompts.append(uuid)
            continue
        if entry.type == "assistant" and _message(entry).get("stop_reason") in _FINAL_STOP_REASONS:
            return entry
        stack.extend(reversed(transcripts.children.get(uuid, ())))

    for later in next_prompts:  # the person spoke again: the reply before it was finished
        last = _last_assistant_above(transcripts, later, prompt)
        if last is not None and not _calls_a_tool(last):
            stop = _message(last).get("stop_reason")
            if stop is None or stop in _FINAL_STOP_REASONS:
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
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in path:
        if entry.is_sidechain or entry.type not in ("user", "assistant"):
            continue
        if entry.type == "user" and not _is_prompt(entry):
            continue  # a tool result, a meta line: not the person, and not the reply
        message_id = _message(entry).get("id")
        for block in _blocks(entry):
            line = _line(entry.type, block)
            if line is None:
                continue
            if isinstance(message_id, str):
                key = (message_id, json.dumps(block, sort_keys=True, default=str))
                if key in seen:
                    continue
                seen.add(key)
            lines.append(line)
    return "\n".join(lines)


def _line(role: str, block: Mapping[str, Any]) -> str | None:
    kind = block.get("type")
    if kind == "text":
        value = _text_of(block)
        return None if value is None else f"{role}: {value}"
    if kind == "image":
        return f"{role}: [image]"
    if kind == "tool_use" and role == "assistant":
        name = block.get("name")
        return f"{role}: [tool: {name if isinstance(name, str) and name else 'unnamed'}]"
    return None  # thinking, tool input and output, and anything not yet known
