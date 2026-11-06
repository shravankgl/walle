"""Observing Codex CLI sessions: one ``observation`` per completed turn (D51, D56, D92, D101).

The Claude Code observer (``transcript.py`` and ``observer.py``) reads one harness. This module
reads a second, Codex CLI, into the same ``Observation`` events, so the proposer, the hazard
checks and the labels downstream never learn which harness a turn came from. It adds nothing
to those modules and changes nothing in them, nor in ``hook.py``.

**The format is unverified.** The owner has no real Codex sessions yet. Everything below is
read from Codex's public source at ``openai/codex`` commit ``388aee4b``
(https://github.com/openai/codex/tree/388aee4b26226778701a7fa749354c44f74b4394/codex-rs), and
none of it has been checked against a real file. Where the source and a real file disagree,
the real file wins and this module is wrong.

Dormant, and only what the owner names
--------------------------------------
Off unless ``WALLE_CODEX`` is exactly ``on`` (the owner's condition on issue #81, D117's
shape). While off, ``observe_sessions`` and ``append_observations`` raise ``CodexOffError``
before reading or writing anything. There is no default folder: the caller names the sessions
folder every time, and nothing here builds a path to the home directory (D40, issue #9's rule
for Claude Code, applied again).

The format, as the source states it
-----------------------------------
* **Where.** ``<codex home>/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl``
  (``SESSIONS_SUBDIR`` in ``codex-rs/rollout/src/lib.rs``; the name in
  ``codex-rs/rollout/src/rollout_file_name.rs``). Cold files may be compressed to
  ``.jsonl.zst`` (``codex-rs/rollout/src/compression.rs``). zstd is not in the standard
  library or the dependency allowlist, so those are counted, not read.
* **One line, one record.** ``{"timestamp": "...Z", "ordinal"?: n, "type": <kind>,
  "payload": {...}}``: ``RolloutLine`` in ``codex-rs/history/src/lib.rs`` flattens a
  ``RolloutItem``, which ``RolloutItemWire`` in ``codex-rs/history/src/rollout_payload.rs``
  tags by ``type`` in snake case. Its thirteen kinds are ``KNOWN_TYPES``; a line of any other
  kind is skipped as an unknown record type and counted.
* **The format marker.** There is no format version number. What decides what a file holds
  is ``session_meta.payload.history_mode`` (``ThreadHistoryMode`` in
  ``codex-rs/protocol/src/protocol.rs``: ``legacy``, the default when absent, or
  ``paginated``). ``persisted_rollout_item`` in ``codex-rs/rollout/src/policy.rs`` writes the
  ``user_message`` and ``agent_message`` events this module reads only in ``legacy`` mode;
  ``paginated`` files carry ``item_completed`` turn items instead. So a ``history_mode`` other
  than ``legacy`` is a changed format: the whole file is skipped and counted, never guessed at.
* **The person, the replies, the tools.** ``event_msg`` payloads are tagged by ``type``
  (``EventMsg``): ``user_message.message`` is what the person typed, with ``images`` and
  ``local_images`` beside it; ``agent_message.message`` is a reply. ``response_item`` payloads
  are Responses API items (``ResponseItem`` in ``codex-rs/protocol/src/models.rs``); only the
  *name* of a call is read: ``function_call``, ``custom_tool_call``, ``local_shell_call``,
  ``tool_search_call``, ``web_search_call``.
* **Turns.** ``task_started`` (alias ``turn_started``) opens a turn, ``task_complete`` (alias
  ``turn_complete``) ends it, ``turn_aborted`` ends it as interrupted, and
  ``thread_rolled_back`` removes the last ``num_turns`` turns. The rules below copy
  ``ThreadHistoryBuilder`` in ``codex-rs/app-server-protocol/src/protocol/thread_history.rs``,
  Codex's own reducer from rollout to turns.

What is never read
------------------
Each line is reduced, as it is parsed, to the few fields above: a kind, a time, a turn id, the
person's text, a reply's text, a tool's name. Everything else on the line is dropped there and
then, so it cannot reach a record, a skip or an observation: tool inputs (``arguments``,
``input``), tool outputs (``function_call_output``, ``custom_tool_call_output``, command
output), reasoning, ``response_item`` messages (which repeat the replies and also carry the
harness's injected ``<environment_context>`` and instructions as user-role text, which are not
the person), ``session_meta.base_instructions``, ``turn_context``, compactions and token
counts. This matches the ``api-request`` rule in ``transcript.py``: a request body never
reaches an observation.

What a turn is (``ThreadHistoryBuilder``, and where this module departs from it)
--------------------------------------------------------------------------------
1. ``task_started`` finishes the turn in progress and opens an explicit one.
2. ``user_message`` inside an explicit turn belongs to it (the person can add to a turn while
   it runs). Outside one, as in older files with no turn markers, it finishes the implicit
   turn in progress and opens a new implicit one.
   *Departure (issue #138):* a ``user_message`` that arrives after the turn has already been
   answered, a follow-up typed while the tools run, closes what the turn holds so far and
   opens a new segment of the same turn. Codex keeps both in one turn; this module writes
   two observations, the way the Claude Code observer treats a prompt that follows a reply
   (its rule 1). Written as one body, the follow-up would be a ``user:`` line after an
   ``assistant:`` line, which ``hazards.forged_turn`` rightly reads as a forged turn, and
   the whole turn would be withheld from the proposer.
3. A reply or tool call outside any turn opens an implicit one.
4. ``task_complete`` ends the turn it names and finishes it, or the turn in progress if none
   matches. *Departure (issue #138):* ``turn_aborted`` ends its turn and finishes it too.
   Codex leaves an aborted turn in progress, but an observation of it may already be in the
   log, and a late line joining the turn would make a second, longer one (D92's content key
   cannot tell the two apart). What Codex still writes under the aborted turn goes into a
   further segment of it, where nobody spoke, so it is never observed; a rollback still
   counts the two as the one turn they are to Codex.
   A ``user_message`` with no text and no image is nobody speaking: it is dropped as it is
   read, like an empty reply, so it neither opens, cuts nor splits a turn.
5. ``thread_rolled_back`` finishes the turn in progress and drops the last ``num_turns``,
   counted as Codex counts them: a turn split into segments here is one turn there.

When a turn is complete (D56)
-----------------------------
A turn is observed when the person spoke in it, something answered (a reply or a tool call),
and either it ended (rule 4) or a later turn cut it off (the person moved on: the same as
Claude Code's rule 1). The one turn still in progress at the end of a file, not ended, is
``pending`` and is observed on a later run. A turn that ended or was cut off with no answer
has nothing to observe and is not pending either.

An *implicit* turn at the end of a file has no marker that could ever end it: only the next
prompt cuts it, and for the last turn in the file there is none. So it is complete once the
file has gone quiet: not changed for ``TAIL_GRACE`` seconds (the Claude Code observer's
rule for a torn last line, task 129; the one constant, imported, so the two observers agree
on what quiet means). While the file is still changing it is pending, as before. An explicit
turn is never closed this way: its ``task_complete`` or ``turn_aborted`` is still to come, and
a quiet file is not that marker. (A Codex that died mid-turn leaves it pending; see the
log for task 138.)

What an observation holds
-------------------------
The same shape as the Claude Code observer's, so the same session through either harness
gives the same ``body`` and ``observed_at``:

* ``body``: one line per item in file order. The person's message is one block, ``user:
  <text>`` on its first line and its further lines unlabelled, then ``[image]`` on its own
  unlabelled line per attached image, as ``observer._body`` writes it (issue #129, finding
  2.4). ``hazards.forged_turn`` reads that as one opening block; ``user: [image]`` after a
  multi-line prompt would read as a forged turn. Then ``assistant: [tool: <name>]`` per call,
  the name folded onto that one line, and ``assistant: <text>`` per reply.
* ``observed_at``: the time of the turn's last reply or tool call, the transcript's own time
  (D93), in UTC. Codex writes UTC (``JsonlWriter``); an odd offset on a line is converted as
  it is read, so no line can carry a time the log's ``Instant`` would refuse later.
* ``source``: ``codex``. ``source_version``: ``session_meta.cli_version`` (D101, D96).
* ``ts``: when the log learns it.

No session id and no turn id is copied into an observation (D101). A turn id is used only to
find the turn a ``task_complete`` names, and to count a fork's copy of a turn once.

Forks, and a turn counted once
------------------------------
Codex can copy a thread's history into a new rollout when it forks (``InitialHistory::Forked``
in ``codex-rs/core/src/session/mod.rs``): the copied lines keep their payloads, so the same
``turn_id`` appears in both files, but each copied line is stamped as it is written
(``JsonlWriter::write_rollout_item`` in ``codex-rs/rollout/src/recorder.rs``), so the copy
carries fresh timestamps and the fork's own ``cli_version``. The copy is found by turn id,
never by content, and the earlier file's turn is kept (by first timestamp, then path).

A turn id means "the same turn" only within one thread and its forks (Codex makes them as
UUIDv7, but this module does not rely on that), so the match is scoped to a *lineage*: the
file's ``session_meta.id`` followed through ``forked_from_id`` to the oldest ancestor named.
Two unrelated sessions whose turn ids collide are two sessions (issue #138). The ids are kept
on the ``RolloutFile`` only, for this one purpose, and reach no observation.

When a fork's ancestor cannot be read in a run, because Codex compressed it to ``.jsonl.zst``
(``codex-rs/rollout/src/compression.rs`` compresses cold rollouts) or it is gone, the copy
cannot be matched by turn id, and its fresh timestamps match nothing by D92's key. Such a
fork's turns are marked ``orphan_fork``, and ``append_observations`` reads them as the copy
they probably are: in file order, each whose ``body`` is already a Codex observation in the
log is already logged; the first that is not ends the copied prefix, and everything after is
the fork's own. The cost, accepted: the fork's first new turn is dropped if its body is
exactly a turn already logged. Reading the compressed file would remove the guess; zstd is
not in the dependency allowlist, so that is a decision, not a fix.

Skipped and counted (D80)
-------------------------
A bad line never loses the rest of a file. Skipped and counted, by line, byte offset and
length, with a fixed reason and never a byte of the line: a line not UTF-8 or not JSON, a JSON
value that is not an object, a line with no usable ``timestamp``, ``type`` or ``payload``, an
unknown record type, and a known event missing the field this module reads. An unterminated
last line is pending: Codex may be writing it. Whole files are skipped and counted, with line
0, for a changed format, a compressed rollout, and a file that vanished between listing and
reading. Kinds of ``event_msg`` and ``response_item`` this module does not read (there are
dozens, and more each release) are passed over, not counted: an event we never read is not a
broken line.

Appending, once per turn (D92)
------------------------------
``append_observations`` takes the stream's lock, folds the Codex observations the stream
already holds, and appends each observed turn not among them, exactly as
``observer.append_observations`` does for Claude Code, keyed on the same content: ``(source,
observed_at, body, source_version)``. The fold counts only ``source == "codex"``, so neither
observer's fold hides the other's turns. No cursor file: the log is the cursor.

Two runners (Codex C appending to a rollout, this reader R)
-----------------------------------------------------------
1. C has written lines 1-9 and is part-way through line 10.
2. R reads the file in one ``read_bytes``: line 10 has no newline, so it is pending.
3. C finishes line 10. R's next read sees it whole.

R never writes a rollout, and C only appends (``JsonlWriter`` in
``codex-rs/rollout/src/recorder.rs`` writes a line then flushes). Two runs of R at once are
serialised by the stream's lock, as in ``observer.py``'s walkthrough.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from walle.events import ACTOR_OBSERVER, Observation, now_instant
from walle.reader import StreamContents
from walle.transcript import TAIL_GRACE
from walle.writer import StreamWriter

SOURCE = "codex"

SWITCH = "WALLE_CODEX"
"""The environment variable the owner sets to turn the Codex observer on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for missions: a stray ``1`` or ``true`` is not consent."""

OFF_MESSAGE = f"the Codex observer is off: the owner turns it on with {SWITCH}={SWITCH_ON}"

KNOWN_TYPES = frozenset(
    {
        "session_meta",
        "dynamic_tools_updated",
        "response_item",
        "inter_agent_communication",
        "inter_agent_communication_metadata",
        "compacted",
        "turn_context",
        "token_usage_record",
        "world_state",
        "retained_context",
        "security_risk_score",
        "event_msg",
        "realtime_item",
    }
)
"""``RolloutItemWire``'s tags at commit ``388aee4b``. Any other ``type`` is an unknown record."""

READ_HISTORY_MODE = "legacy"
"""The one ``history_mode`` whose files this module reads. Absent means this, as in Codex."""

_STARTED = frozenset({"task_started", "turn_started"})
_COMPLETE = frozenset({"task_complete", "turn_complete"})

_TOOL_CALLS = frozenset(
    {"function_call", "custom_tool_call", "local_shell_call", "tool_search_call", "web_search_call"}
)

_PATTERN = "rollout-*.jsonl"
_COMPRESSED = "rollout-*.jsonl.zst"


class CodexOffError(RuntimeError):
    """The owner has not turned the Codex observer on. Nothing was read or written."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the Codex observer on.

    Off unless ``WALLE_CODEX`` is exactly ``on``. Reads the process environment when no
    mapping is given; a mapping is the whole answer when one is. Nothing in the log or the
    walle root can turn it on.
    """
    env = os.environ if environ is None else environ
    return env.get(SWITCH) == SWITCH_ON


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise CodexOffError(OFF_MESSAGE)


# --- reading one rollout -----------------------------------------------------------------

type Kind = Literal["started", "prompt", "tool", "reply", "complete", "aborted", "rollback"]


@dataclass(frozen=True)
class RolloutRecord:
    """The part of one rollout line that can change an observation. Nothing else is kept."""

    path: Path
    line: int
    """1-based line number in ``path``."""
    at: datetime
    kind: Kind
    text: str | None = None
    """The person's words, a reply, or a tool's name. ``None`` for turn markers."""
    turn_id: str | None = None
    count: int = 0
    """Images attached to a prompt; turns dropped by a rollback."""


@dataclass(frozen=True)
class RolloutSkip:
    """A line, or a whole file, that was not read, and why. Never what it held."""

    path: Path
    line: int
    """1-based; 0 when the whole file was skipped."""
    offset: int
    """Byte offset of the line's first byte in the file."""
    length: int
    """The line's length in bytes, without its newline."""
    reason: str
    """A fixed sentence from this module, never built from the line's content."""


@dataclass(frozen=True)
class RolloutFile:
    """What one read of one rollout found, in file order."""

    path: Path
    records: tuple[RolloutRecord, ...]
    skips: tuple[RolloutSkip, ...]
    source_version: str | None = None
    """The first ``session_meta.cli_version``: the Codex build that wrote the file."""
    thread_id: str | None = None
    """The first ``session_meta.id``. Scopes a turn id to its lineage; copied nowhere else."""
    forked_from: str | None = None
    """The first ``session_meta.forked_from_id``: the thread this one's history was copied
    from, when it was. Copied nowhere else."""
    growing: bool = False
    """The file changed within ``TAIL_GRACE`` seconds of being read: Codex may be writing it,
    so an implicit turn open at its end may still grow."""


class _Malformed(Exception):
    """A line whose shape this module cannot read. The message is a fixed reason."""


def _file_skip(path: Path, reason: str) -> RolloutFile:
    return RolloutFile(path=path, records=(), skips=(RolloutSkip(path, 0, 0, 0, reason),))


def read_rollout(path: Path) -> RolloutFile:
    """Read one rollout file. Bad lines are skipped and reported, never raised (D80).

    Read once, whole, never written. An ``OSError`` reading it is raised: a file that cannot
    be read is not a file with nothing in it. A changed format gives one whole-file skip.
    """
    path = Path(path)
    lines = path.read_bytes().split(b"\n")  # bytes only: ``splitlines`` also splits on U+2028
    tail = lines.pop()  # b"" when the file ends with a newline
    records: list[RolloutRecord] = []
    skips: list[RolloutSkip] = []
    version: str | None = None
    thread_id: str | None = None
    forked_from: str | None = None
    seen_meta = False

    offset = 0
    for number, raw in enumerate(lines, start=1):
        start, offset = offset, offset + len(raw) + 1
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except UnicodeDecodeError:
            skips.append(RolloutSkip(path, number, start, len(raw), "not UTF-8"))
            continue
        except json.JSONDecodeError:
            skips.append(RolloutSkip(path, number, start, len(raw), "not JSON"))
            continue
        try:
            kind, at, payload = _envelope(parsed)
            if kind == "session_meta":
                if payload.get("history_mode", READ_HISTORY_MODE) != READ_HISTORY_MODE:
                    return _file_skip(
                        path, f"format not read: history_mode is not {READ_HISTORY_MODE}"
                    )
                if not seen_meta:
                    version = _string(payload.get("cli_version"))
                    thread_id = _string(payload.get("id"))
                    forked_from = _string(payload.get("forked_from_id"))
                    seen_meta = True
                continue
            record = _record(path, number, at, kind, payload)
        except _Malformed as exc:
            skips.append(RolloutSkip(path, number, start, len(raw), str(exc)))
            continue
        if record is not None:
            records.append(record)

    if tail.strip():
        reason = "unterminated last line: pending, Codex may still be writing it"
        skips.append(RolloutSkip(path, len(lines) + 1, offset, len(tail), reason))
    return RolloutFile(
        path,
        tuple(records),
        tuple(skips),
        version,
        thread_id=thread_id,
        forked_from=forked_from,
        growing=_changed_within(path, TAIL_GRACE),
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _changed_within(path: Path, seconds: float) -> bool:
    """Whether ``path`` was modified in the last ``seconds``. A file that cannot be stat'ed
    any more (gone since it was read) is not being written."""
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    return datetime.now(UTC).timestamp() - modified < seconds


def read_or_skip(path: Path) -> RolloutFile:
    """``read_rollout``, except that a file gone since it was listed is a counted skip.

    Codex archives and compresses rollouts on its own schedule, so one can go between
    ``rollout_files`` and here. Any other ``OSError`` is raised.
    """
    try:
        return read_rollout(path)
    except FileNotFoundError:
        return _file_skip(Path(path), "file vanished before it was read")


def _envelope(parsed: object) -> tuple[str, datetime, Mapping[str, Any]]:
    if not isinstance(parsed, dict):
        raise _Malformed(f"a JSON {_json_type(parsed)}, not an object")
    stamp = parsed.get("timestamp")
    if not isinstance(stamp, str):
        raise _Malformed("not a rollout line: no timestamp")
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        raise _Malformed("not a rollout line: timestamp is not an instant") from None
    if at.tzinfo is None or at.utcoffset() is None:
        raise _Malformed("not a rollout line: timestamp has no offset")
    try:  # UTC, so every time spells as the log's Instant does: Codex writes Z anyway
        at = at.astimezone(UTC)
    except OverflowError:
        raise _Malformed("not a rollout line: timestamp is not an instant") from None
    kind = parsed.get("type")
    if not isinstance(kind, str):
        raise _Malformed("not a rollout line: no type")
    if kind not in KNOWN_TYPES:
        raise _Malformed("unknown record type")
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        raise _Malformed("not a rollout line: payload is not an object")
    return kind, at, payload


def _json_type(value: object) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    return "null" if value is None else type(value).__name__


def _record(
    path: Path, number: int, at: datetime, kind: str, payload: Mapping[str, Any]
) -> RolloutRecord | None:
    """Reduce one line to a record, or ``None`` for a line that cannot change a turn."""
    if kind == "response_item":
        item = payload.get("type")
        if not isinstance(item, str):
            raise _Malformed("not a rollout line: response_item without a type")
        if item not in _TOOL_CALLS:
            return None  # messages, reasoning, outputs, and kinds not known yet
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            name = item.removesuffix("_call") if "name" not in payload else "unnamed"
        # One line: a name with a line break in it would put an unlabelled line in the body.
        return RolloutRecord(path, number, at, "tool", text=" ".join(name.split()))
    if kind != "event_msg":
        return None
    event = payload.get("type")
    if not isinstance(event, str):
        raise _Malformed("not a rollout line: event_msg without a type")
    if event == "user_message":
        message = payload.get("message")
        if not isinstance(message, str):
            raise _Malformed("not a rollout line: user_message without a message")
        images = sum(
            len(value)
            for key in ("images", "local_images")
            if isinstance(value := payload.get(key), list)
        )
        if not message.strip() and not images:
            return None  # nobody spoke: it must not open, cut or split a turn either
        return RolloutRecord(path, number, at, "prompt", text=message, count=images)
    if event == "agent_message":
        message = payload.get("message")
        if not isinstance(message, str):
            raise _Malformed("not a rollout line: agent_message without a message")
        if not message.strip():
            return None  # Codex's own reducer drops an empty reply; blank is empty here
        return RolloutRecord(path, number, at, "reply", text=message)
    if event in _STARTED or event in _COMPLETE:
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str):
            raise _Malformed(f"not a rollout line: {event} without a turn_id")
        return RolloutRecord(
            path, number, at, "started" if event in _STARTED else "complete", turn_id=turn_id
        )
    if event == "turn_aborted":
        turn_id = payload.get("turn_id")
        return RolloutRecord(
            path, number, at, "aborted", turn_id=turn_id if isinstance(turn_id, str) else None
        )
    if event == "thread_rolled_back":
        n = payload.get("num_turns")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise _Malformed("not a rollout line: thread_rolled_back without num_turns")
        return RolloutRecord(path, number, at, "rollback", count=n)
    return None


def rollout_files(sessions_dir: Path) -> tuple[list[Path], list[Path]]:
    """Every rollout under ``sessions_dir``, sorted: ``(plain, compressed)``.

    ``sessions_dir`` has no default, here or in any caller. A missing folder gives two empty
    lists: no sessions yet is not an error.
    """
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return [], []
    plain = sorted(p for p in sessions_dir.rglob(_PATTERN) if p.is_file())
    compressed = sorted(p for p in sessions_dir.rglob(_COMPRESSED) if p.is_file())
    return plain, compressed


# --- turns -------------------------------------------------------------------------------


@dataclass
class _Turn:
    """One turn while a file is being folded. Private and mutable."""

    turn_id: str | None
    explicit: bool
    line: int
    segment: int = 0
    """Which piece of its Codex turn this is: 0, or one more per follow-up (rule 2)."""
    lines: list[str] = field(default_factory=list)
    spoke: bool = False
    answered_at: datetime | None = None
    ended: bool = False
    cut: bool = False
    open: bool = False
    """Still in progress when the file ended."""


@dataclass(frozen=True)
class ObservedTurn:
    """One completed turn and the observation made from it."""

    turn_id: str | None
    """Codex's id for the turn, or ``None`` for an older file's implicit turn. Not copied
    into the observation (D101)."""
    path: Path
    line: int
    """Where the turn starts in ``path``."""
    observation: Observation
    orphan_fork: bool = False
    """From a fork none of whose ancestors could be read this run, so a copy of an ancestor's
    turn cannot be told from the fork's own by turn id. ``append_observations`` matches the
    copied prefix by body instead."""


@dataclass(frozen=True)
class PendingTurn:
    """A turn the person started that has not completed yet."""

    path: Path
    line: int


@dataclass(frozen=True)
class DuplicateTurn:
    """A copy of a turn, by Codex's turn id, already kept from an earlier file."""

    turn_id: str
    path: Path
    line: int
    kept_path: Path
    kept_line: int


@dataclass(frozen=True)
class CodexReport:
    """What one run of the Codex observer found."""

    turns: tuple[ObservedTurn, ...]
    pending: tuple[PendingTurn, ...]
    skips: tuple[RolloutSkip, ...]
    duplicates: tuple[DuplicateTurn, ...]

    @property
    def skipped_lines(self) -> int:
        return sum(1 for s in self.skips if s.line)

    @property
    def skipped_files(self) -> int:
        return sum(1 for s in self.skips if not s.line)


def _fold(file: RolloutFile) -> list[_Turn]:
    """``ThreadHistoryBuilder``'s rules over one file's records. The last turn may be open."""
    finished: list[_Turn] = []
    current: _Turn | None = None

    def finish(*, cut: bool) -> None:
        nonlocal current
        if current is not None:
            current.cut = cut
            finished.append(current)
            current = None

    def ensure(record: RolloutRecord) -> _Turn:
        nonlocal current
        if current is None:
            current = _Turn(turn_id=None, explicit=False, line=record.line)
        return current

    def abort() -> None:
        """Finish the aborted turn, and leave a further segment of it open for whatever Codex
        still writes under it: a late reply joins that segment, nobody spoke in it, and it is
        never observed, while a rollback still counts the whole as one turn."""
        nonlocal current
        assert current is not None
        tail = _Turn(current.turn_id, current.explicit, current.line, segment=current.segment + 1)
        finish(cut=False)
        current = tail

    for record in file.records:
        if record.kind == "started":
            finish(cut=True)
            current = _Turn(turn_id=record.turn_id, explicit=True, line=record.line)
        elif record.kind == "prompt":
            if current is not None and not current.explicit:
                finish(cut=True)
            elif current is not None and current.answered_at is not None:
                # A follow-up after the answer began: the next segment of the same turn.
                segment = _Turn(current.turn_id, True, record.line, segment=current.segment + 1)
                finish(cut=True)
                current = segment
            turn = ensure(record)
            block = [record.text] if record.text and record.text.strip() else []
            block.extend(["[image]"] * record.count)
            turn.spoke = True  # ``_record`` dropped the blank-and-imageless message already
            turn.lines.append("user: " + "\n".join(block))
        elif record.kind in ("tool", "reply"):
            turn = ensure(record)
            text = f"[tool: {record.text}]" if record.kind == "tool" else record.text
            turn.lines.append(f"assistant: {text}")
            turn.answered_at = record.at
        elif record.kind == "complete":
            if current is not None and current.turn_id == record.turn_id:
                current.ended = True
                finish(cut=False)
            elif (done := _named(finished, record.turn_id)) is not None:
                done.ended = True
            elif current is not None:
                current.ended = True
                finish(cut=False)
        elif record.kind == "aborted":
            if current is not None and record.turn_id in (None, current.turn_id):
                current.ended = True
                abort()
            elif (done := _named(finished, record.turn_id)) is not None:
                done.ended = True
            elif current is not None:
                current.ended = True
                abort()
        else:  # rollback
            finish(cut=True)
            _rollback(finished, record.count)
    if current is not None:
        current.open = True
        finished.append(current)
    return finished


def _named(turns: list[_Turn], turn_id: str | None) -> _Turn | None:
    if turn_id is None:
        return None
    return next((t for t in turns if t.turn_id == turn_id), None)


def _rollback(finished: list[_Turn], num_turns: int) -> None:
    """Drop the last ``num_turns`` of Codex's turns from ``finished``, in place.

    Codex counts turns; ``finished`` holds segments. Consecutive segments with the same turn id
    (a follow-up's split, an abort's tail) are one turn. An implicit turn has no id and is one
    turn by itself.
    """
    keep = len(finished)
    turns = 0
    previous: str | None = None
    for index in range(len(finished) - 1, -1, -1):
        turn_id = finished[index].turn_id
        if turn_id is None or turn_id != previous:
            turns += 1
            if turns > num_turns:
                break
        previous = turn_id
        keep = index
    del finished[keep:]


def _lineage(file: RolloutFile, by_id: Mapping[str, RolloutFile]) -> tuple[str, bool]:
    """The scope a turn id is unique within, and whether an ancestor could not be read.

    The scope is the oldest thread id the fork chain names, following ``forked_from`` through
    the files read this run; a file naming no thread at all is its own scope. The chain is
    unread at its end when it names a thread no readable file has: compressed, archived or
    gone. A loop in the ids (nothing Codex writes) ends the walk where it started.
    """
    root = file.thread_id
    seen = set() if root is None else {root}
    parent = file.forked_from
    while parent is not None and parent not in seen:
        seen.add(parent)
        root = parent
        ancestor = by_id.get(parent)
        if ancestor is None:
            return f"thread:{root}", True
        parent = ancestor.forked_from
    if root is None:
        return f"path:{file.path}", False
    return f"thread:{root}", False


def observe(files: Iterable[RolloutFile], *, ts: str | None = None) -> CodexReport:
    """One ``Observation`` for each completed turn in ``files``. Reads and writes nothing.

    The order ``files`` arrive in does not matter: they are taken by first timestamp, then
    path, so a fork's copy of a turn always loses to the file it was copied from.
    """
    stamp = now_instant() if ts is None else ts

    def file_key(f: RolloutFile) -> tuple[bool, datetime | None, str]:
        first = min((r.at for r in f.records), default=None)
        return (first is None, first, str(f.path))

    ordered = sorted(files, key=file_key)
    by_id: dict[str, RolloutFile] = {}
    for file in ordered:
        if file.thread_id is not None:
            by_id.setdefault(file.thread_id, file)
    turns: list[ObservedTurn] = []
    pending: list[PendingTurn] = []
    duplicates: list[DuplicateTurn] = []
    kept: dict[tuple[str, str, int], ObservedTurn] = {}
    for file in ordered:
        scope, orphan = _lineage(file, by_id)
        for turn in _fold(file):
            if not turn.spoke:
                continue  # nobody spoke: nothing to observe
            settled = turn.open and not turn.explicit and not file.growing
            complete = turn.answered_at is not None and (turn.ended or turn.cut or settled)
            if not complete:
                if not turn.ended and not turn.cut:
                    pending.append(PendingTurn(file.path, turn.line))
                continue
            assert turn.answered_at is not None
            key = None if turn.turn_id is None else (scope, turn.turn_id, turn.segment)
            if key is not None and (first := kept.get(key)) is not None:
                duplicates.append(
                    DuplicateTurn(key[1], file.path, turn.line, first.path, first.line)
                )
                continue
            observed = ObservedTurn(
                turn_id=turn.turn_id,
                path=file.path,
                line=turn.line,
                observation=Observation(
                    ts=stamp,
                    actor=ACTOR_OBSERVER,
                    source=SOURCE,
                    observed_at=turn.answered_at.isoformat(timespec="microseconds"),
                    body="\n".join(turn.lines),
                    source_version=file.source_version,
                ),
                orphan_fork=orphan,
            )
            if key is not None:
                kept[key] = observed
            turns.append(observed)
    return CodexReport(
        turns=tuple(turns),
        pending=tuple(pending),
        skips=tuple(skip for file in ordered for skip in file.skips),
        duplicates=tuple(duplicates),
    )


def observe_sessions(
    sessions_dir: Path,
    *,
    ts: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodexReport:
    """Observe every rollout under ``sessions_dir``, which the owner names every time.

    Raises ``CodexOffError`` before reading anything unless the owner has turned the Codex
    observer on. Compressed rollouts are counted as skipped files, not read.
    """
    _require_on(environ)
    plain, compressed = rollout_files(sessions_dir)
    files = [read_or_skip(path) for path in plain]
    files += [_file_skip(path, "compressed rollout: not read") for path in compressed]
    return observe(files, ts=ts)


# --- appending, once per turn (D92) ------------------------------------------------------


@dataclass(frozen=True)
class AppendReport:
    """What one ``append_observations`` call wrote."""

    appended: tuple[Observation, ...]
    already_logged: tuple[ObservedTurn, ...]
    report: CodexReport


type _Key = tuple[str, str, str, str | None]


def _key(observation: Observation) -> _Key:
    return (
        observation.source,
        observation.observed_at,
        observation.body,
        observation.source_version,
    )


def _logged(contents: StreamContents) -> set[_Key]:
    """The fold: every Codex observation already in the stream (D92)."""
    return {
        _key(event)
        for event in contents.events
        if isinstance(event, Observation) and event.source == SOURCE
    }


def append_observations(
    report: CodexReport,
    writer: StreamWriter,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppendReport:
    """Append each completed turn in ``report`` that the stream does not hold yet.

    Raises ``CodexOffError`` before touching the stream unless the owner has turned the
    Codex observer on. With nothing completed, the stream is not touched, nor created. The
    fold and every append happen under the stream's one lock.
    """
    _require_on(environ)
    if not report.turns:
        return AppendReport(appended=(), already_logged=(), report=report)
    appended: list[Observation] = []
    already: list[ObservedTurn] = []
    with writer.locked() as stream:
        logged = _logged(stream.read())
        bodies = {key[2] for key in logged}
        # Files still inside the prefix a fork copied from an ancestor nobody could read.
        copying = {turn.path for turn in report.turns if turn.orphan_fork}
        for turn in report.turns:
            key = _key(turn.observation)
            if key in logged:
                already.append(turn)
                # An exact key is a turn this fork itself was observed from on an earlier run
                # (a copy carries fresh timestamps), so the copied prefix is over.
                copying.discard(turn.path)
                continue
            if turn.path in copying and turn.observation.body in bodies:
                already.append(turn)  # the ancestor's turn, observed from the ancestor
                continue
            copying.discard(turn.path)  # the fork's own turns begin here
            appended.append(stream.append(turn.observation))
            logged.add(key)
            bodies.add(key[2])
    return AppendReport(appended=tuple(appended), already_logged=tuple(already), report=report)
