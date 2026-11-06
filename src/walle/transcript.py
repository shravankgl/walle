"""Reading Claude Code's transcripts into the ``parentUuid`` graph, and walking it (D51, D101).

Phase 0 observes the harness transcript itself (D51):
``~/.claude/projects/<project>/<session>.jsonl``. This module is part 1 of the observer. It
reads those files, builds the graph and returns the entries in graph order. Part 2 (task 11)
turns completed turns into ``observation`` events; this part writes nothing.

The format is Claude Code's, not ours, and nobody promises it stays put (PLAN §6's risk). What
D101 records from claude-code-log and claude-code-transcripts:

* Each conversational line has ``uuid``, ``parentUuid``, ``isSidechain``, ``sessionId``, ``cwd``,
  ``version`` and ``timestamp``, plus ``type`` (``user``, ``assistant``, ``system``, …).
* Some lines have no ``uuid`` at all (``summary``, ``queue-operation``). They are not part of
  the graph. They are kept aside, in ``Transcripts.unthreaded``, not skipped.

What real files showed (issue #9, task 11; counts and structure only, read on the owner's
machine):

* **Subagents have their own files**, ``<project>/<session>/subagents/agent-<id>.jsonl``.
  Every entry there has ``isSidechain: true``, the trunk's ``sessionId`` and the agent's own
  ``agentId``, and the first has ``parentUuid: null`` (claude-code-log ``dev-docs/agents.md``).
  ``load`` hangs that root under the trunk entry whose ``toolUseResult.agentId`` names the
  agent: the result of the call that started it. That repair is reported only when it fails
  (``unanchored-agent``).
* **More entry types exist** than D101 lists (``attachment``, ``mode``, ``ai-title``,
  ``api-request`` and others). Only ``user`` and ``assistant`` matter to the observer. A line
  of any other type that does not fit the entry shape is kept aside in ``unthreaded``, not
  reported as corrupt: a type we have never seen is not a broken file.
* **``api-request`` and ``api-request-blob`` may hold raw request payloads.** Their ``raw`` is
  replaced by ``{}`` as they are read, so nothing downstream can copy one anywhere. And no
  skipped line of any type is copied into its ``LineSkip``: a skip says where the line is
  (line, byte offset, length) and why, never what it held.

Order (D101)
------------
**Order is the graph, never the clock.** A parent always comes before its children, and a
branch is walked whole before its next sibling starts. Timestamps are used only to put
siblings (and roots) in order among themselves. So clock skew, a background subagent running
alongside the main thread, and a resume from another file cannot reorder a chain.

At each entry the children are taken sidechain first, then by timestamp, then by where they
were read. A sidechain hanging off an entry is a subagent that entry started: its work belongs
under the call that started it, ahead of the main thread's continuation.

**The session id is not an identity.** It changes on every turn and on every resume (D101).
It is carried on each entry and nothing here keys on it.

Duplicates: a resume can replay entries from the session it continues into its own file, with
the same ``uuid`` and a new ``sessionId``. One instance is kept, from the file whose earliest
timestamp is earliest (then by path, then by line), so the answer does not depend on the order
files are passed in. Every dropped copy is reported in ``Transcripts.duplicates``, and a copy
that disagrees about its parent says so.

Bad lines (D80)
---------------
A bad line never loses the rest of a file. Each is skipped, returned in ``skips`` and logged
as a warning:

* An **unterminated final line** is pending, even if it parses. Claude Code may be writing it
  right now (there is no lock to take on a file we do not own), and the next read sees it whole.
  A file whose last line is torn and whose ``mtime`` is within ``TAIL_GRACE`` seconds is
  **growing** (``TranscriptFile.growing``, ``Transcripts.growing``): the observer holds back a
  turn the tail could still extend (issue #129, finding 2.3). Older than that, nothing is
  writing it: Claude Code writes a line in one go, so a tear left alone for minutes is a
  crash, and the entries above it are final. The skip's reason says which.
* A line that is not UTF-8 or not JSON, a JSON value that is not an object, and an object
  with a ``uuid`` that does not validate as an entry.

This is looser than ``reader.py``, which halts on a complete line it cannot read. That
strictness protects our own log, where a skipped record would silently lose a ref. Here the
file is someone else's, and keeping the recoverable history is the rule D80 sets for the
transcript observer.

**A lost line must not cut the chain** (issue #129, finding 2.1). The entry after a skipped or
unthreaded line names it as its parent, and without repair that entry would be an orphan
root: every turn running through the gap would be lost, since the observer only looks below a
prompt. Two repairs, both reported:

* ``bridged``: the lost line parsed and carried a ``uuid`` and a ``parentUuid`` (a line kept
  aside for want of a timestamp, a user line with a bad one, a payload line). Its link is
  kept, in ``TranscriptFile.links``, and the entry below it is hung from the lost line's own
  parent, through as many lost lines in a row as it takes. Nothing of the lost line but the
  two identifiers is kept; a payload line's link is read before its content is dropped.
* ``orphan``, attached: the lost line did not parse, so its parent is unknown. If a line
  between the orphan and the entry before it in the same file was not loaded, the orphan is
  hung from that entry before it: Claude Code appends, so the line before a torn line is
  almost always its parent. With no lost line between them the parent went missing some other
  way (a deleted file), and the orphan is a root as before. The limit of the guess: if the
  lost line was the person's own prompt, the reply below it joins the turn above. The lost
  prompt was unreadable either way; the anomaly names the gap.

Graph repairs, also reported (``Transcripts.anomalies``): a parent that was never loaded
(``orphan``) makes its child a root, or hangs it from the entry before a lost line; a link
through lost lines (``bridged``) is followed; an entry that is its own parent
(``self-parent``) becomes a root; a ring of entries with no way in (``cycle``) is broken at
its earliest entry. Repairs are a view: the files are only ever read.

Two runners (Claude Code C appending to a session file, this reader R)
----------------------------------------------------------------------
1. C has written lines 1-9 and is part-way through line 10.
2. R reads the file in one ``read``: lines 1-9 end in a newline, line 10 does not.
3. R returns 1-9 and reports line 10 as an unterminated line, pending.
4. C finishes line 10 and starts 11. R's next read returns 1-10.

Nothing is locked and nothing needs to be: R never writes, and C only appends. If C dies in
step 1, line 10 stays unterminated and is reported on every read, which is true. If C ever
rewrote a file instead of appending, R could see a mix of old and new bytes; that is one of
the format assumptions in ``devlog/task-10.md``.
"""

from __future__ import annotations

import json
import logging
import time
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class _EntryFields(BaseModel):
    """The fields D101 names, validated. Every other field stays in ``raw``, unvalidated.

    ``extra="ignore"`` rather than the ``extra="forbid"`` our own events use (D88): this is a
    schema we do not control, and a new field in a Claude Code release must not make every
    line of a transcript unreadable.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    uuid: str = Field(min_length=1)
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    type: str = Field(min_length=1)
    timestamp: AwareDatetime
    is_sidechain: bool = Field(default=False, alias="isSidechain", strict=True)
    session_id: str | None = Field(default=None, alias="sessionId")
    cwd: str | None = None
    version: str | None = None
    agent_id: str | None = Field(default=None, alias="agentId")


@dataclass(frozen=True)
class TranscriptEntry:
    """One line of a transcript that is part of the graph, and where it was read from."""

    uuid: str
    parent_uuid: str | None
    """As written in the file. ``Transcripts.parent_of`` gives the parent after repairs."""
    type: str
    timestamp: datetime
    is_sidechain: bool
    session_id: str | None
    """Carried, never keyed on (D101)."""
    cwd: str | None
    version: str | None
    """The Claude Code build that wrote the line: D96's era marker for a schema we do not own."""
    agent_id: str | None
    path: Path
    line: int
    """1-based line number in ``path``."""
    raw: Mapping[str, Any] = field(compare=False, repr=False)
    """The whole parsed line. The message content is in here, for part 2."""


@dataclass(frozen=True)
class Unthreaded:
    """A JSON object that is not part of the graph: no ``uuid`` (``summary``, ``mode``, …), or
    a type other than ``user`` or ``assistant`` whose fields do not fit an entry."""

    path: Path
    line: int
    raw: Mapping[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class LineSkip:
    """One line that was not read, and why."""

    path: Path
    line: int
    """1-based; 0 when the whole file was skipped."""
    offset: int
    """Byte offset of the line's first byte in the file."""
    length: int
    """The line's length in bytes, without its newline. ``path``, ``offset`` and ``length``
    find the line; nothing of it is copied here.

    There is deliberately no preview. A line that cannot be parsed cannot be trusted to say
    what it is: an ``api-request`` line cut off before its ``"type"`` arrives looks like any
    other fragment, and its first bytes may be a raw request (issue #9)."""
    reason: str
    """Why it was skipped. Built from the parser's own messages and field names, never from
    the line's content."""


@dataclass(frozen=True)
class Duplicate:
    """A copy of an entry whose ``uuid`` was already kept from another place."""

    uuid: str
    path: Path
    line: int
    kept_path: Path
    kept_line: int
    conflict: str | None
    """``None`` when the copy agrees with the kept entry about its parent, else what differs."""


@dataclass(frozen=True)
class Anomaly:
    """A repair made to the graph so it can be walked. The files are not touched."""

    kind: str
    """``orphan``, ``bridged``, ``self-parent``, ``cycle`` or ``unanchored-agent``."""
    uuid: str
    detail: str


@dataclass(frozen=True)
class TranscriptFile:
    """Everything one read of one transcript file found, in file order."""

    path: Path
    entries: tuple[TranscriptEntry, ...]
    unthreaded: tuple[Unthreaded, ...]
    skips: tuple[LineSkip, ...]
    links: Mapping[str, str | None] = field(default_factory=dict)
    """``uuid`` to ``parentUuid`` of each line that parsed as an object with both but was not
    loaded as an entry. The chain is followed through them (``load``). Identifiers only."""
    growing: bool = False
    """The last line is unterminated and the file changed within ``TAIL_GRACE`` seconds:
    Claude Code may be writing it, and the chain may grow from the file's last entry."""


# Lines that may carry a raw API request (issue #9). Their content is dropped on read.
_PAYLOAD_TYPES = frozenset({"api-request", "api-request-blob"})

TAIL_GRACE = 300.0
"""Seconds since a file last changed within which its torn last line counts as being written.
Claude Code writes a line in one go, so a tear this old is a crash, not a write in progress."""

_LINK_LENGTH = 128
"""The most a ``uuid`` or ``parentUuid`` may be to be kept as a link: a uuid is 36 characters,
and a longer string in that field is not an identifier and is not copied out of the line."""

# The types an entry must validate as. Any other type that does not fit is not corrupt.
_CONVERSATIONAL = frozenset({"user", "assistant"})

# Where a subagent's own transcript lives, relative to the projects folder.
_PATTERNS = ("*/*.jsonl", "*/*/subagents/*.jsonl")


def transcript_files(projects_dir: Path) -> list[Path]:
    """Every session file and every subagent file under ``projects_dir``, sorted.

    That is ``<projects_dir>/<project>/<session>.jsonl`` and
    ``<projects_dir>/<project>/<session>/subagents/<agent>.jsonl``, regular files only.

    ``projects_dir`` has no default, here or in any caller: walle reads only a folder the
    person names (issue #9, D40). A missing directory gives ``[]``: no transcripts yet is not
    an error.
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    found = {path for pattern in _PATTERNS for path in projects_dir.glob(pattern)}
    return sorted(path for path in found if path.is_file())


def read_transcript(path: Path) -> TranscriptFile:
    """Read one transcript file. Bad lines are skipped and reported, never raised (D80).

    The file is read once, whole, and never written. An ``OSError`` opening or reading it is
    raised: a file that cannot be read is not the same as a file with nothing in it.
    """
    path = Path(path)
    data = path.read_bytes()
    lines = data.split(b"\n")  # bytes only: ``splitlines`` would also split on U+2028
    tail = lines.pop()  # b"" when the file ends with a newline
    entries: list[TranscriptEntry] = []
    unthreaded: list[Unthreaded] = []
    skips: list[LineSkip] = []
    links: dict[str, str | None] = {}

    def skip(number: int, start: int, raw: bytes, reason: str) -> None:
        skips.append(LineSkip(path=path, line=number, offset=start, length=len(raw), reason=reason))
        logger.warning("%s:%d: skipped (%d bytes) — %s", path, number, len(raw), reason)

    def link(parsed: Mapping[str, Any]) -> None:
        """Keep the line's place in the chain, if it says where that is. Nothing else of it."""
        uuid, parent = parsed.get("uuid"), parsed.get("parentUuid")
        if not (isinstance(uuid, str) and 0 < len(uuid) <= _LINK_LENGTH):
            return
        if parent is not None and not (isinstance(parent, str) and len(parent) <= _LINK_LENGTH):
            return
        links.setdefault(uuid, parent)

    offset = 0
    for number, raw in enumerate(lines, start=1):
        start, offset = offset, offset + len(raw) + 1
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except UnicodeDecodeError:
            skip(number, start, raw, "not UTF-8")
            continue
        except json.JSONDecodeError as exc:
            skip(number, start, raw, f"not JSON ({exc.msg})")
            continue
        if not isinstance(parsed, dict):
            skip(number, start, raw, f"a JSON {type(parsed).__name__}, not an object")
            continue
        kind = parsed.get("type")
        kept: dict[str, Any] = {} if kind in _PAYLOAD_TYPES else parsed
        if "uuid" not in parsed:
            unthreaded.append(Unthreaded(path=path, line=number, raw=kept))
            continue
        try:
            fields = _EntryFields.model_validate(parsed)
        except ValidationError as exc:
            link(parsed)
            if isinstance(kind, str) and kind not in _CONVERSATIONAL:
                # Not ours to judge: a type we do not read, in a schema we do not own.
                logger.debug("%s:%d: %r line kept aside, not an entry", path, number, kind)
                unthreaded.append(Unthreaded(path=path, line=number, raw=kept))
                continue
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc']) or 'line'}: {error['msg']}"
                for error in exc.errors()
            )
            skip(number, start, raw, f"not a transcript entry ({problems})")
            continue
        entries.append(
            TranscriptEntry(
                **fields.model_dump(),
                path=path,
                line=number,
                raw=kept,
            )
        )

    growing = False
    if tail.strip():
        growing = _changed_within(path, TAIL_GRACE)
        reason = (
            "unterminated last line: pending, Claude Code may still be writing it"
            if growing
            else "unterminated last line: the file has not changed for over "
            f"{TAIL_GRACE:.0f}s, so nothing is writing it"
        )
        skip(len(lines) + 1, offset, tail, reason)
    return TranscriptFile(
        path=path,
        entries=tuple(entries),
        unthreaded=tuple(unthreaded),
        skips=tuple(skips),
        links=links,
        growing=growing,
    )


def _changed_within(path: Path, seconds: float) -> bool:
    """Whether ``path`` was modified in the last ``seconds``. A file that cannot be stat'ed
    any more (deleted since it was read) is not being written."""
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    return time.time() - modified < seconds


@dataclass(frozen=True)
class Transcripts:
    """The graph over every file loaded together, with what was set aside and why."""

    entries: Mapping[str, TranscriptEntry]
    """By ``uuid``, one instance each."""
    parent_of: Mapping[str, str | None]
    """Each entry's parent after repairs: ``None`` for a root."""
    children: Mapping[str, tuple[str, ...]]
    """Each entry's children, already in walk order. Leaves are absent."""
    roots: tuple[str, ...]
    """Entries with no parent after repairs, in walk order."""
    unthreaded: tuple[Unthreaded, ...]
    skips: tuple[LineSkip, ...]
    duplicates: tuple[Duplicate, ...]
    anomalies: tuple[Anomaly, ...]
    growing: Mapping[Path, str | None] = field(default_factory=dict)
    """Each file whose torn last line may still be being written (``TranscriptFile.growing``),
    with the ``uuid`` of its last entry in file order: where the line being written hangs.
    ``None`` for a file with no entry at all yet. Taken from the file's own order, not from
    whichever copy of a replayed entry was kept."""

    def walk(self) -> tuple[TranscriptEntry, ...]:
        """Every entry once, depth first: parents before children, each branch whole."""
        order: list[TranscriptEntry] = []
        stack = list(reversed(self.roots))
        while stack:  # iterative: a long session is a chain thousands of entries deep
            uuid = stack.pop()
            order.append(self.entries[uuid])
            stack.extend(reversed(self.children.get(uuid, ())))
        return tuple(order)


def load(paths: Iterable[Path]) -> Transcripts:
    """Read every file in ``paths`` and build one graph over all of them.

    Load them together: a resume puts the child in one file and its parent in another. The
    result does not depend on the order of ``paths``.

    A file that no longer exists is reported in ``skips`` with ``line`` 0 and the rest still
    load: Claude Code deletes old transcripts on its own schedule, so one can go between
    ``transcript_files`` and here. Any other ``OSError`` is raised, since a file that exists
    and cannot be read would otherwise look like a file with nothing in it.
    """
    files: list[TranscriptFile] = []
    vanished: list[LineSkip] = []
    for path in sorted({Path(p) for p in paths}):
        try:
            files.append(read_transcript(path))
        except FileNotFoundError:
            reason = "file vanished before it was read"
            vanished.append(LineSkip(path=path, line=0, offset=0, length=0, reason=reason))
            logger.warning("%s: skipped — %s", path, reason)

    def file_key(transcript: TranscriptFile) -> tuple[int, datetime | None, str]:
        first = min((entry.timestamp for entry in transcript.entries), default=None)
        return (first is None, first, str(transcript.path))

    files.sort(key=file_key)

    entries: dict[str, TranscriptEntry] = {}
    duplicates: list[Duplicate] = []
    for transcript in files:
        for entry in transcript.entries:
            kept = entries.get(entry.uuid)
            if kept is None:
                entries[entry.uuid] = entry
                continue
            conflict = None
            if kept.parent_uuid != entry.parent_uuid:
                conflict = f"parentUuid {entry.parent_uuid!r}, kept {kept.parent_uuid!r}"
            duplicates.append(
                Duplicate(
                    uuid=entry.uuid,
                    path=entry.path,
                    line=entry.line,
                    kept_path=kept.path,
                    kept_line=kept.line,
                    conflict=conflict,
                )
            )
            if conflict:
                logger.warning(
                    "%s:%d: duplicate uuid %s disagrees with %s:%d — %s",
                    entry.path,
                    entry.line,
                    entry.uuid,
                    kept.path,
                    kept.line,
                    conflict,
                )

    anomalies: list[Anomaly] = []

    def repair(kind: str, entry: TranscriptEntry, detail: str) -> None:
        anomalies.append(Anomaly(kind=kind, uuid=entry.uuid, detail=detail))
        logger.warning("%s:%d: %s %s — %s", entry.path, entry.line, kind, entry.uuid, detail)

    def position(uuid: str) -> tuple[bool, datetime, str, int]:
        entry = entries[uuid]
        return (not entry.is_sidechain, entry.timestamp, str(entry.path), entry.line)

    # Where each subagent started: the trunk entry carrying the spawn's structured result,
    # ``toolUseResult.agentId``. The earliest wins if two claim one agent. Sidechain entries
    # are never anchors: that rules out an agent anchoring inside itself, and a nested
    # spawn's result (claude-code-log says nested results carry no ``toolUseResult``
    # anyway) beating the trunk's.
    anchors: dict[str, str] = {}
    for uuid in sorted(entries, key=lambda u: position(u)[1:]):
        result = entries[uuid].raw.get("toolUseResult")
        spawned = result.get("agentId") if isinstance(result, dict) else None
        if isinstance(spawned, str) and spawned and not entries[uuid].is_sidechain:
            anchors.setdefault(spawned, uuid)

    # The chain through lines that were not loaded (finding 2.1). ``links`` is where each
    # lost-but-parsed line said it hangs; ``before_gap`` is, for each entry that follows a
    # skipped line in its own file, the entry before that gap. Only a skipped line counts as
    # a gap: a line kept aside without a uuid cannot be anyone's parent, and one with a uuid
    # is in ``links`` already.
    links: dict[str, str | None] = {}
    before_gap: dict[tuple[Path, int], str] = {}
    for transcript in files:
        for uuid, parent in transcript.links.items():
            links.setdefault(uuid, parent)
        skipped = sorted(skip.line for skip in transcript.skips)
        for previous, entry in zip(transcript.entries, transcript.entries[1:], strict=False):
            at = bisect_right(skipped, previous.line)
            if at < len(skipped) and skipped[at] < entry.line:
                before_gap[entry.path, entry.line] = previous.uuid

    def bridge(start: str) -> tuple[str | None, int] | None:
        """Follow ``links`` from the missing parent ``start`` to a loaded entry or a root.
        ``None`` when the trail ends in a line nothing is known about, or loops."""
        hop: str | None = start
        crossed: list[str] = []
        while hop is not None and hop not in entries:
            if hop not in links or hop in crossed:
                return None
            crossed.append(hop)
            hop = links[hop]
        return hop, len(crossed)

    parent_of: dict[str, str | None] = {}
    for uuid, entry in entries.items():
        parent = entry.parent_uuid
        if parent == uuid:
            repair("self-parent", entry, "names itself as parent; walked as a root")
            parent = None
        elif parent is not None and parent not in entries:
            bridged = bridge(parent)
            attach = before_gap.get((entry.path, entry.line))
            if bridged is not None and bridged[0] != uuid:
                end, crossed = bridged
                where = "a root" if end is None else f"entry {end}"
                repair(
                    "bridged",
                    entry,
                    f"parent {parent} was not loaded as an entry; its own parent chain "
                    f"leads through {crossed} lost line(s) to {where}",
                )
                parent = end
            elif attach is not None:
                repair(
                    "orphan",
                    entry,
                    f"parent {parent} was not loaded, and a line before this one was lost; "
                    f"hung from the entry before the gap, {attach}",
                )
                parent = attach
            else:
                repair("orphan", entry, f"parent {parent} was not loaded; walked as a root")
                parent = None
        elif parent is None and entry.agent_id and entry.path.parent.name == "subagents":
            parent = anchors.get(entry.agent_id)
            if parent is None:
                repair(
                    "unanchored-agent",
                    entry,
                    f"no loaded entry started agent {entry.agent_id}; walked as a root",
                )
        parent_of[uuid] = parent

    def build() -> tuple[dict[str, list[str]], list[str]]:
        children: dict[str, list[str]] = {}
        roots: list[str] = []
        for uuid, parent in parent_of.items():
            (roots if parent is None else children.setdefault(parent, [])).append(uuid)
        for kids in children.values():
            kids.sort(key=position)
        roots.sort(key=lambda uuid: position(uuid)[1:])  # a root's sidechain flag says nothing
        return children, roots

    def reachable(children: Mapping[str, list[str]], roots: list[str]) -> set[str]:
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            uuid = stack.pop()
            seen.add(uuid)
            stack.extend(children.get(uuid, ()))
        return seen

    # Every entry in a ring has a parent, so none is a root and a walk from the roots never
    # reaches it, nor anything hanging off it. Climb from an unreached entry until a uuid
    # repeats: that one is on the ring. Cut the ring at its earliest entry and look again.
    children, roots = build()
    seen = reachable(children, roots)
    while len(seen) < len(entries):
        uuid = min((u for u in entries if u not in seen), key=lambda u: position(u)[1:])
        climbed: set[str] = set()
        while uuid not in climbed:
            climbed.add(uuid)
            parent = parent_of[uuid]
            assert parent is not None  # an unreached entry is never a root
            uuid = parent
        ring = [uuid]
        while (parent := parent_of[ring[-1]]) != uuid:
            assert parent is not None
            ring.append(parent)
        stuck = min(ring, key=lambda u: position(u)[1:])
        repair("cycle", entries[stuck], f"parent chain loops back; parent {parent_of[stuck]} cut")
        parent_of[stuck] = None
        children, roots = build()
        seen = reachable(children, roots)

    skips = (*vanished, *(skip for transcript in files for skip in transcript.skips))
    unthreaded = tuple(item for transcript in files for item in transcript.unthreaded)
    return Transcripts(
        entries=entries,
        parent_of=parent_of,
        children={uuid: tuple(kids) for uuid, kids in children.items()},
        roots=tuple(roots),
        unthreaded=unthreaded,
        skips=skips,
        duplicates=tuple(duplicates),
        anomalies=tuple(anomalies),
        growing={
            transcript.path: transcript.entries[-1].uuid if transcript.entries else None
            for transcript in files
            if transcript.growing
        },
    )
