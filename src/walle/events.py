"""Event schema — the shape of everything in the log.

The log is the only source of truth (D10). Every other artifact in walle — the SQLite
index, the rendered hot tier, the provenance graph — is a fold over these events and can
be destroyed and rebuilt from them. So this module is the most expensive thing in the
project to get wrong: an event written today must still load in five years (D88), and a
field that is absent today cannot be added to history later.

Nothing here does IO and nothing here calls a model. The resolver imports this module, and
resolution must be a pure function of the log (hard constraint 3).

Decisions implemented here
--------------------------
D57/D85  A provenance reference is ``d-<device>:<seq>`` — the stream, then the position in
         it. ``seq`` is per-stream, so a bare number would be ambiguous the moment a second
         device exists.
D58      ``contradiction`` is an event: two assertions that cannot both hold, naming both.
D66      ``canonical_json`` and ``content_hash`` — sorted keys, compact, deterministic, so
         the rebuild test can compare a hash instead of diffing structures.
D88/D96  Every event carries ``v``, its own type's schema version, and loads through a
         chain of upcasters. Handlers are permanent: they are what lets a five-year-old
         event still load.
D89      ``snapshot`` names the refs it stands in for, rather than implying "everything
         before N" — with per-device streams there is no total order for that to mean.
D93      Two clocks. ``ts`` is when the log learned something; ``valid_from`` / ``valid_to``
         are when it was true in the world. ``observed_at`` is the source's own time.
D94      An assertion carries how it was reached (``level``) and, rarely, who it is about.
D49/D60  ``proposal`` is an item in the staging queue: a belief someone would like kept,
         waiting for a human yes. It is never folded into what walle believes.
D111     An event whose ``v`` is newer than this build halts loading by name. Reading it
         "best effort" would mean silently misinterpreting a field we do not know about.
D67/D75  ``mission`` is a DAG of steps in ``node_link_data`` shape, each node carrying its
         manifest (``reads``, ``writes``, ``egress``) and a ``token_budget``; ``step`` is a
         delta against one mission: a node's status, or one of the intervention verbs. The
         resolved DAG is a fold in ``walle.missions``, never a mutation of the mission event.
D78      ``ask`` is an inbox item — an approval, a question or a notification — keyed by
         ``(session, tool_call_id)``; ``answer`` resolves one. Resolved once, first responder
         wins: which answer that is, is a fold in ``walle.inbox``, never an edit of the ask.
D6/D114  ``automation`` is one change to the register of active automations: ``add``,
         ``retire``, ``pin`` or ``unpin``. Which automations are active, and whether the WIP
         cap allows another, is a fold in ``walle.automations``, never an edit of an add.
D6/D20   ``outcome`` is one observed run of an automation, ``ok`` or ``failed``, against the
         ref of its add. How many shipped automations still run at day 30 is a fold in
         ``walle.survival`` over the log and an explicit ``as_of``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

# --- references -------------------------------------------------------------------------

# A stream is one (profile, device) pair, one file: `principals/p-maya/log/d-mbp.jsonl`.
#
# Lowercase only, and no dots. Two reasons, both learned from other people's bugs:
# macOS and Windows filesystems are case-insensitive, so `d-MBP` and `d-mbp` would be one
# file under two stream ids; and a stream id becomes a path segment, so anything that could
# read as a traversal is refused here rather than sanitized later (D85).
# Anchors are deliberately absent and every use below is `fullmatch`, never `match`.
# Python's `$` matches at the end of a string *or just before a trailing newline*, so
# `^...$` with `.match` accepted "d-mbp:1\n" as a ref and "maya\n" as a name. A ref with a
# trailing newline prints identically to one without and compares unequal — the "one ref
# naming two events" failure this project calls its worst — and a name with one became a
# directory. Found by an independent reviewer after two author-blind passes missed it.
_REF_RE = re.compile(r"d-[a-z0-9][a-z0-9_-]{0,62}:[1-9][0-9]{0,18}")
_STREAM_RE = re.compile(r"d-[a-z0-9][a-z0-9_-]{0,62}")


def _check_ref(value: str) -> str:
    if not _REF_RE.fullmatch(value):
        raise ValueError(
            f"not a provenance ref: {value!r} — expected 'd-<device>:<seq>', "
            "lowercase device, seq >= 1"
        )
    return value


def _check_stream(value: str) -> str:
    if not _STREAM_RE.fullmatch(value):
        raise ValueError(f"not a stream id: {value!r} — expected 'd-<device>', lowercase")
    return value


type Ref = Annotated[str, AfterValidator(_check_ref)]
"""A reference to one event: ``d-mbp:44881``. A plain string, because that is what it is
in the file — the canonical form is JSONL a person can read and grep."""

type StreamId = Annotated[str, AfterValidator(_check_stream)]


def parse_ref(ref: str) -> tuple[str, int]:
    """Split a ref into ``(stream, seq)``. Raises ``ValueError`` on a malformed one."""
    _check_ref(ref)
    stream, _, seq = ref.rpartition(":")
    return stream, int(seq)


def make_ref(stream: str, seq: int) -> Ref:
    """Build a ref, validating both halves."""
    _check_stream(stream)
    if seq < 1:
        raise ValueError(f"seq must be >= 1, got {seq}")
    return _check_ref(f"{stream}:{seq}")


# --- times ------------------------------------------------------------------------------


# One instant, one spelling. `datetime.fromisoformat` is far more permissive than RFC 3339:
# it takes `20261010T143702+0000` and `2026-10-10 14:37:02+00:00` as readily as the extended
# form, so parsing alone would let the same moment into the log under three different strings
# — three different hashes, and three things to grep for in a file whose whole claim is that
# you can read it with `cat` (D10). The shape is gated before it is parsed.
_INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _check_instant(value: str) -> str:
    """An RFC 3339 instant in extended form, with an explicit offset.

    Stored as a string, not a ``datetime``: the JSONL is canonical, and a string round-trips
    byte-identically, which the content hash depends on. A naive timestamp is refused —
    "14:37" means nothing across a timezone change, and this log is meant to outlive several.

    ``Z`` is normalized to ``+00:00``. They denote the same offset by definition, so letting
    both through would be the same canonicality hole in miniature.
    """
    if not _INSTANT_RE.fullmatch(value):
        raise ValueError(
            f"not an RFC 3339 instant: {value!r} — expected "
            "'YYYY-MM-DDTHH:MM:SS[.ffffff](Z|±HH:MM)'"
        )
    try:
        datetime.fromisoformat(value)  # the shape is right; is it a real moment?
    except ValueError as exc:
        raise ValueError(f"not a real instant: {value!r}") from exc
    return f"{value[:-1]}+00:00" if value.endswith("Z") else value


def _check_world_time(value: str) -> str:
    """World time (D93) — a plain date or an instant.

    Dates are allowed because world time usually arrives as one: "I moved in March" resolves
    to a day, not a moment, and widening it to midnight UTC would invent precision the source
    never had (D103).

    Only ``YYYY-MM-DD``. ISO 8601's basic (``20260301``) and week (``2026-W09-1``) forms name
    the same day in other spellings, and ``date.fromisoformat`` accepts both.
    """
    if _DATE_RE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"not a real date: {value!r}") from exc
        return value
    if _INSTANT_RE.fullmatch(value):
        return _check_instant(value)
    raise ValueError(f"not a world time: {value!r} — expected 'YYYY-MM-DD' or an RFC 3339 instant")


type Instant = Annotated[str, AfterValidator(_check_instant)]
type WorldTime = Annotated[str, AfterValidator(_check_world_time)]


def now_instant() -> str:
    """The current moment, in the one spelling the log accepts.

    Lives here, beside the type it produces, so that three modules do not each invent their
    own format and discover the difference in a hash months later. The resolver never calls
    it — a fold that reads the clock is not a pure function of the log (hard constraint 3).
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


# --- enums ------------------------------------------------------------------------------


class StepStatus(StrEnum):
    """Where one node of a mission stands (D67).

    ``pending`` is the state every node starts in and the one a human or the kernel sets a
    node back to when it is re-dispatched after a crash (D91). ``completed`` is the only
    state that satisfies a successor: a step is ready when every predecessor is completed,
    not failed and not merely started. A closed set, like ``Level``: adding a member is a
    version bump with an upcaster (D88).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepVerb(StrEnum):
    """What a ``step`` event does to its mission (D67).

    ``status`` is the node-status delta the kernel writes as work happens. The other four
    are the intervention verbs for a mission that runs forty minutes with nobody watching:
    ``pause`` and ``resume`` hold and release the whole mission, ``inject`` adds a node with
    its links, ``reassign`` hands a node to someone else. Each is an event, so each is
    undone by a ``supersede`` of it and never by an edit (D8).
    """

    STATUS = "status"
    PAUSE = "pause"
    RESUME = "resume"
    INJECT = "inject"
    REASSIGN = "reassign"


class AskKind(StrEnum):
    """What an inbox item asks of a person (D78).

    ``approval`` is a yes or no the run waits on; ``question`` wants words, or one of the
    item's ``choices``; ``notification`` only wants to be seen. A closed set, like ``Level``.
    """

    APPROVAL = "approval"
    QUESTION = "question"
    NOTIFICATION = "notification"


class AnswerOutcome(StrEnum):
    """How an inbox item was resolved (D78).

    Which outcomes fit which kind of item is ``walle.inbox.FITS``: ``approved`` and ``denied``
    an approval, ``answered`` a question, ``acknowledged`` a notification. ``orphaned`` fits
    every kind: the session that asked died, and an orphaned approval can never be
    meaningfully answered. Only the kernel writes it, and it is never a yes.
    """

    APPROVED = "approved"
    DENIED = "denied"
    ANSWERED = "answered"
    ACKNOWLEDGED = "acknowledged"
    ORPHANED = "orphaned"


class AutomationVerb(StrEnum):
    """What an ``automation`` event does to the register (D6, D114).

    ``add`` puts an automation in the register as active; ``retire`` takes one out, and it
    stays in the log as retired (D8); ``pin`` and ``unpin`` set and clear the human mark that
    no automatic transition may cross. A closed set, like ``StepVerb``: adding a member is a
    version bump with an upcaster (D88).
    """

    ADD = "add"
    RETIRE = "retire"
    PIN = "pin"
    UNPIN = "unpin"


class OutcomeResult(StrEnum):
    """How one run of an automation went (D19, D20).

    ``ok`` means it ran and did its job; ``failed`` means it ran and did not. There is no
    ``stopped``: an automation that stopped produces no outcome at all, and that silence is
    what ``walle.survival`` counts against it. A closed set, like ``StepVerb``: adding a member
    is a version bump with an upcaster (D88).
    """

    OK = "ok"
    FAILED = "failed"


class Level(StrEnum):
    """How an assertion was reached (D94).

    Load-bearing for D30: a contradiction between something *stated* and something
    *observed* is handled differently from two conflicting statements.

    Adding a member is a **breaking change for existing readers** — an older build meets a
    value it cannot validate — so it needs a version bump and an upcaster like any other
    shape change (D88). That is the cost of a closed set, and it is worth paying here: these
    three drive behaviour, unlike ``actor``, which is left an open string for that reason.
    """

    EXPLICIT = "explicit"
    DEDUCTIVE = "deductive"
    INDUCTIVE = "inductive"


# Known actors. Deliberately *not* an enum on the model: an event written by a later
# version with an actor this build has not heard of must still load (D88). The version gate
# is `v`, not a membership test on a string.
ACTOR_USER = "user"
ACTOR_OBSERVER = "observer"
ACTOR_PROPOSER = "proposer"
ACTOR_CURATOR = "curator"
ACTOR_KERNEL = "kernel"
ACTOR_MCP = "mcp"
"""The MCP server's ``remember``: the host model's words, not the person's (D97)."""
ACTOR_BUILDER = "builder"
"""The Builder: who a staged workflow comes from (D117). A machine, like the proposer."""

MACHINE_ACTORS = frozenset(
    {ACTOR_OBSERVER, ACTOR_PROPOSER, ACTOR_CURATOR, ACTOR_KERNEL, ACTOR_MCP, ACTOR_BUILDER}
)
"""Every actor that is a part of walle rather than a person, in one place (issue #119). No
machine actor resolves an inbox item (``walle.inbox`` rule 4) or changes the register of
automations (``walle.automations`` rule 4, D72). A new ``ACTOR_*`` is added here or it is
not a machine, and ``tests/test_audit_approvals.py`` checks that every one defined in this
package is defined in this module."""


# --- errors -----------------------------------------------------------------------------


class FutureSchemaError(Exception):
    """An event written by a newer build of walle (D111).

    Not recoverable by skipping. A newer version may have renamed or repurposed a field this
    build thinks it understands, so reading the event at all risks acting on a
    misinterpretation. The caller halts and says which version it met and which it knows.

    Deliberately **not** a ``ValueError``: pydantic catches ``ValueError`` inside a validator
    and folds it into a ``ValidationError``, where — under a discriminated union — it would
    arrive as one entry in a list of per-member failures, indistinguishable from a typo in a
    field name. D111 asks for a halt that names the version, so this has to propagate.
    """

    def __init__(self, *, event_type: str, found: int, known: int) -> None:
        super().__init__(
            f"{event_type} event is schema v{found}, this build knows v{known}. "
            "Upgrade walle; refusing to read it rather than guess at its fields."
        )
        self.event_type = event_type
        self.found = found
        self.known = known


# --- base -------------------------------------------------------------------------------


class Event(BaseModel):
    """Fields every event has.

    ``extra="forbid"`` so an unknown field is a loud failure rather than silent data loss,
    and ``frozen=True`` because an event is a record of something that happened: nothing in
    the system has any business editing one. A correction is a new event (D8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    SCHEMA_VERSION: ClassVar[int] = 1
    """This build's schema version for this event type. Bumped when the shape changes
    incompatibly, together with an upcaster that migrates the previous version."""

    UPCASTS: ClassVar[dict[int, Any]] = {}
    """``{from_version: fn(dict) -> dict}``, applied in sequence on load.

    These are **permanent** (D88). An upcaster is deleted only when every log that could
    contain that version is gone, which for a lifetime log is never.
    """

    v: int = 1
    """Schema version of this event's type (D96)."""

    seq: int | None = Field(default=None, ge=1)
    """Position in its stream. ``None`` until the writer appends it (D57) — the stream
    assigns it, because only the stream knows what comes next."""

    ts: Instant
    """When the log learned this. Never when it happened — that is ``observed_at`` on an
    observation and ``valid_from`` on an assertion (D93)."""

    actor: str = Field(min_length=1, max_length=64)
    """Who caused this event: a principal, or one of the ``ACTOR_*`` constants."""

    caused_by: list[Ref] = Field(default_factory=list)
    """The events this one came from. Provenance is position in the log, not a maintained
    citation field — ``walle why`` walks these (D59)."""

    @model_validator(mode="before")
    @classmethod
    def _upcast(cls, data: Any) -> Any:
        """Run the upcast chain before validation, and refuse anything from the future."""
        if not isinstance(data, dict):
            return data
        found = data.get("v", 1)
        if not isinstance(found, int):
            raise ValueError(f"v must be an integer, got {found!r}")
        if found > cls.SCHEMA_VERSION:
            raise FutureSchemaError(
                event_type=data.get("type", cls.__name__),
                found=found,
                known=cls.SCHEMA_VERSION,
            )
        while found < cls.SCHEMA_VERSION:
            upcast = cls.UPCASTS.get(found)
            if upcast is None:
                raise ValueError(
                    f"no upcaster from v{found} to v{found + 1} for "
                    f"{data.get('type', cls.__name__)} — history cannot be read without one"
                )
            data = upcast(dict(data))
            found += 1
            data["v"] = found
        return data


# --- the five types task 2 covers, task 9's proposal, Phase 2's mission, step, ask, answer -
#
# §5.2's taxonomy also has utterance, capability, trust, feedback and outcome. They belong
# to phases that do not exist yet; adding them now would be guessing at their fields with
# nothing to check the guess against. `mission` and `step` arrived with Phase 2 (R8, D67),
# `ask` and `answer` with its inbox (D78), `automation` with Phase 3's register (D6),
# `outcome` with its survival report (D20), and `utterance` with its conversation rules (D61).


class Observation(Event):
    """Something happened in the user's world. Appended freely, TTL'd, never injected on
    its own (D49)."""

    type: Literal["observation"] = "observation"

    source: str = Field(min_length=1, max_length=128)
    """Where it came from: ``transcript``, later ``mail``, ``calendar``."""

    observed_at: Instant
    """The source's own time, distinct from ``ts`` (D93). A transcript read today may hold
    a turn from last week; ordering observations by ``ts`` would put them in the order we
    happened to read them."""

    body: str
    """The observation itself."""

    source_version: str | None = None
    """The version of the thing that produced it — for the transcript observer, the Claude
    Code build that wrote the entry. An era marker for a schema we do not control (D101)."""


class Assertion(Event):
    """A durable belief about the subject. Injected into every future context, so it needs
    a human yes before it is written (D49)."""

    type: Literal["assertion"] = "assertion"

    body: str
    """The fact, with the why."""

    summary: str = Field(min_length=1, max_length=120)
    """One line, for index mode. Written at proposal time, not derived later: the index
    needs it, and backfilling it across history is the migration D88 exists to avoid (D81)."""

    level: Level = Level.EXPLICIT
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    """Starts at 0.3, reinforced asymptotically (D60). The arithmetic lives with the belief
    update, not here — this records the result."""

    evidence_count: int = Field(default=1, ge=1)

    valid_from: WorldTime | None = None
    valid_to: WorldTime | None = None
    """When the fact was true in the world (D93). Both null means "true as far as we know,
    assume no start" — null is not ignorance to be filled in by guessing (D103)."""

    about: str | None = Field(default=None, max_length=64)
    """Set only when this assertion is about a *different* principal, and only inside a
    workflow whose manifest declares the cross-folder read (D94). The folder says who the
    observer is; this says who is observed."""


class Proposal(Event):
    """A belief waiting in the staging queue for a human yes (D49, D60).

    The staging queue *is* D49's review batch, and it lives in the log rather than in a file
    beside it: a drop has to leave a tombstone (hard constraint 2), and a tombstone needs an
    event to point at. A ``supersede`` targeting a proposal is a drop. An ``assertion`` whose
    ``caused_by`` names it is a keep or an edit. Anything else is still staged.

    Its own type, not an ``assertion`` with a flag, so that nothing which folds assertions
    can pick one up by forgetting to check the flag. The resolver never puts a proposal in
    ``current``, so the hot tier cannot carry one (D86: ``remember`` only stages).

    The fields are the ones a keep copies onto the assertion. ``confidence`` and
    ``evidence_count`` are absent on purpose: they describe a belief, and this is not one yet.
    """

    type: Literal["proposal"] = "proposal"

    body: str = Field(min_length=1)
    """What would be remembered, with the why."""

    summary: str = Field(min_length=1, max_length=120)
    """One line, written when the item is proposed, because the assertion it becomes needs
    one and deriving it later is the migration D81 warns about."""

    level: Level = Level.EXPLICIT

    valid_from: WorldTime | None = None
    valid_to: WorldTime | None = None
    """World time, when the text carries a marker that resolves without guessing (D103).
    Editable in review whenever set."""


class Supersede(Event):
    """Retract or replace an earlier event. The target is never deleted — it stays in the
    log, with this event shadowing it (D8)."""

    type: Literal["supersede"] = "supersede"

    target: Ref
    reason: str | None = None

    valid_to: WorldTime | None = None
    """When the superseded fact stopped being true, if known. A retraction has both a
    world time and a log time (D93): learned today, true until March."""


class Snapshot(Event):
    """A digest the Curator produced, written into the log as a fact rather than treated as
    a rebuildable artifact — an LLM-generated summary is not reproducible by replay (D29)."""

    type: Literal["snapshot"] = "snapshot"

    forgets: list[Ref] = Field(default_factory=list)
    """The refs this digest stands in for. Explicit, not "everything before N": with one
    stream per device there is no total order for that to mean (D89)."""

    body: str
    model: str | None = None
    """Which model produced it — the snapshot's own provenance."""


class Contradiction(Event):
    """Two assertions that cannot both hold (D58).

    Recorded, not resolved. Resolution is a later ``supersede``, or a human saying which was
    right; both outcomes are events of their own. ``caused_by`` names both sides, so this
    type adds no field for them.
    """

    type: Literal["contradiction"] = "contradiction"

    note: str | None = None


# --- missions (D67) ----------------------------------------------------------------------

# A node id is a path-safe token, like a stream id: lowercase, no dots, no spaces. It is
# quoted in refs' company ("d-mbp:3 names node 'water-plants'") and may one day name a
# per-step working directory, so it is held to the same rule now rather than sanitized then.
_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _check_node_id(value: str) -> str:
    if not _NODE_ID_RE.fullmatch(value):
        raise ValueError(f"not a node id: {value!r} — lowercase letters, digits, '_' and '-'")
    return value


type NodeId = Annotated[str, AfterValidator(_check_node_id)]


def find_cycle(ids: Iterable[str], links: Iterable[tuple[str, str]]) -> list[str] | None:
    """The first cycle in a directed graph, as a path that returns to its start, or ``None``.

    Depth-first from each node in the order given, so the answer is deterministic for one
    input and names the loop a person can read: ``['b', 'c', 'd', 'b']``. Iterative, so a
    long chain cannot hit the recursion limit. A link naming a node outside ``ids`` is the
    caller's error to report; here it is simply followed.
    """
    successors: dict[str, list[str]] = {}
    for source, target in links:
        successors.setdefault(source, []).append(target)
    done: set[str] = set()
    for start in ids:
        if start in done:
            continue
        path, on_path = [start], {start}
        cursors = [iter(successors.get(start, ()))]
        while cursors:
            nxt = next(cursors[-1], None)
            if nxt is None:  # every successor of path[-1] is explored
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
                cursors.pop()
            elif nxt in on_path:
                return [*path[path.index(nxt) :], nxt]
            elif nxt not in done:
                path.append(nxt)
                on_path.add(nxt)
                cursors.append(iter(successors.get(nxt, ())))
    return None


class Node(BaseModel):
    """One step of a mission: what it is, who runs it, and its manifest (D67, D75).

    The manifest is the permission grant (§7 rule 2): ``reads`` and ``writes`` are the paths
    the step may touch and ``egress`` the hosts it may send to, checked before dispatch and
    not after (``walle.manifest``). A later version that declares more lapses whatever grant
    the mission had (D117). ``token_budget`` and ``cost_budget_usd`` are D22's threshold
    abort, per step: the fold sums what the node's live status steps report, and a node
    that has spent more than either is a breach that holds its whole mission. ``None`` is
    no budget. ``cost_budget_usd`` is optional and was added after the v1 golden log shipped;
    a line without it loads and hashes unchanged, so it is not a schema bump (D96).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NodeId
    title: str = Field(min_length=1, max_length=200)
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    egress: list[str] = Field(default_factory=list)
    token_budget: int | None = Field(default=None, ge=1)
    cost_budget_usd: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    assignee: str | None = Field(default=None, min_length=1, max_length=64)
    """Who runs it: an actor. ``None`` means whoever runs the mission. Changed by a
    ``reassign`` step, never by rewriting this."""


class Link(BaseModel):
    """``source`` must be completed before ``target`` is ready. ``node_link_data``'s edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: NodeId
    target: NodeId


class Mission(Event):
    """A DAG of steps (D67), in the shape of networkx's ``node_link_data``: ``nodes`` and
    ``links``. The event is the plan as proposed; everything that happens to it afterwards
    is a ``step``, and the DAG as it stands now is ``walle.missions.fold_missions``.

    A mission whose links loop is refused here, at construction and on load, because no
    fold could give it a true answer: a step that waits on itself is never ready and never
    wrong. Duplicate node ids and links to nodes the mission does not have are refused for
    the same reason the log has one spelling for an instant — two shapes for one plan would
    be two hashes.
    """

    type: Literal["mission"] = "mission"

    title: str = Field(min_length=1, max_length=200)
    nodes: list[Node] = Field(min_length=1)
    links: list[Link] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_dag(self) -> Mission:
        ids = [node.id for node in self.nodes]
        seen: set[str] = set()
        for node_id in ids:
            if node_id in seen:
                raise ValueError(f"node {node_id!r} appears twice")
            seen.add(node_id)
        edges: set[tuple[str, str]] = set()
        for link in self.links:
            for end in (link.source, link.target):
                if end not in seen:
                    raise ValueError(
                        f"link {link.source} -> {link.target} names unknown node {end!r}"
                    )
            if (link.source, link.target) in edges:
                raise ValueError(f"link {link.source} -> {link.target} appears twice")
            edges.add((link.source, link.target))
        cycle = find_cycle(ids, [(link.source, link.target) for link in self.links])
        if cycle is not None:
            raise ValueError(f"not a DAG, cycle: {' -> '.join(cycle)}")
        return self


class Step(Event):
    """A delta against one mission (D67): a node's new status, or an intervention.

    Which fields a step carries is decided by its ``verb``, and the validator holds each
    verb to exactly its own fields, so that a pause cannot smuggle a status and a status
    cannot smuggle a node. ``mission`` is a ref, like ``supersede.target``: a step whose
    mission the fold was not given is reported, not dropped.

    ========  =======================================================================
    verb      fields
    ========  =======================================================================
    status    ``node``, ``status``; optionally ``cost_usd`` and ``tokens``, what this
              step spent so far, summed by the fold for D22's abort
    pause     none: the whole mission is held
    resume    none
    inject    ``inject`` (the new node), ``after`` (its predecessors) and ``before``
              (its successors), both lists of existing node ids and both optional
    reassign  ``node``, ``assignee``
    ========  =======================================================================

    ``note`` is free text for any verb: why it was paused, who asked for the reassign.
    """

    type: Literal["step"] = "step"

    mission: Ref
    verb: StepVerb

    node: NodeId | None = None
    status: StepStatus | None = None
    cost_usd: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    tokens: int | None = Field(default=None, ge=0)
    inject: Node | None = None
    after: list[NodeId] = Field(default_factory=list)
    before: list[NodeId] = Field(default_factory=list)
    assignee: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = None

    @model_validator(mode="after")
    def _fields_fit_the_verb(self) -> Step:
        required, allowed = _STEP_FIELDS[self.verb]
        present = {
            name
            for name in ("node", "status", "cost_usd", "tokens", "inject", "assignee")
            if getattr(self, name) is not None
        } | {name for name in ("after", "before") if getattr(self, name)}
        if missing := required - present:
            raise ValueError(f"a {self.verb.value} step needs {sorted(missing)}")
        if extra := present - allowed:
            raise ValueError(f"a {self.verb.value} step does not carry {sorted(extra)}")
        return self


_STEP_FIELDS: dict[StepVerb, tuple[frozenset[str], frozenset[str]]] = {
    StepVerb.STATUS: (
        frozenset({"node", "status"}),
        frozenset({"node", "status", "cost_usd", "tokens"}),
    ),
    StepVerb.PAUSE: (frozenset(), frozenset()),
    StepVerb.RESUME: (frozenset(), frozenset()),
    StepVerb.INJECT: (frozenset({"inject"}), frozenset({"inject", "after", "before"})),
    StepVerb.REASSIGN: (frozenset({"node", "assignee"}), frozenset({"node", "assignee"})),
}
"""Per verb: the fields it must carry, and the fields it may."""


# --- the inbox (D78) --------------------------------------------------------------------

# A session id or a tool-call id comes from the harness, not from walle: a Claude Code session
# is a lowercase UUID, a tool call is `toolu_01AbC...`. So mixed case is allowed, unlike a
# stream id, but whitespace and `/` are not: either would let one id print as two, and the
# pair is the idempotency key.
_CALL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _check_call_id(value: str) -> str:
    if not _CALL_ID_RE.fullmatch(value):
        raise ValueError(
            f"not a session or tool-call id: {value!r} — letters, digits, '_', '.', ':' and '-'"
        )
    return value


type CallId = Annotated[str, AfterValidator(_check_call_id)]


class Ask(Event):
    """An item in the inbox: something a run needs a person for (D78).

    ``(session, tool_call_id)`` is the item's identity. Asking again with the same pair is the
    same item, so a run that restarts and re-issues its call does not ask twice. ``body`` is
    what the person reads. ``choices``, for a question only, are the answers it accepts.

    ``unattended`` records how the person is reached: inline, or through the inbox because
    nobody is watching. It is routing, never permission — the same outcomes fit the same
    item either way, and nothing about it lets a run answer itself.

    There is no recipient field on purpose (D63): the run decides *whether* to ask, and
    configuration decides *where* it goes. ``mission`` and ``node`` say which mission step is
    waiting, so that a restart can rebuild the wait from the log.
    """

    type: Literal["ask"] = "ask"

    kind: AskKind
    session: CallId
    tool_call_id: CallId
    body: str = Field(min_length=1)
    choices: list[str] = Field(default_factory=list)
    unattended: bool = False
    mission: Ref | None = None
    node: NodeId | None = None

    @model_validator(mode="after")
    def _fields_fit_the_kind(self) -> Ask:
        if self.choices and self.kind is not AskKind.QUESTION:
            raise ValueError(f"a {self.kind.value} has no choices; only a question does")
        if any(not choice.strip() for choice in self.choices):
            raise ValueError("a choice is empty")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("a choice appears twice")
        if self.node is not None and self.mission is None:
            raise ValueError("a node is named without its mission")
        return self


class Answer(Event):
    """The resolution of one inbox item (D78). ``ask`` is a ref, like ``supersede.target``.

    ``value`` is the words of an ``answered`` question and is carried by nothing else.
    ``surface`` is where it was answered — ``app``, ``slack``, ``cli`` — for the record only:
    any surface may answer, and the first answer in the log wins.

    Whether this answer fits its item, and whether its ``actor`` may give it, is decided by
    the fold, which has the ask in hand; an answer that does not fit is reported, not
    applied.
    """

    type: Literal["answer"] = "answer"

    ask: Ref
    outcome: AnswerOutcome
    value: str | None = Field(default=None, min_length=1)
    surface: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _value_fits_the_outcome(self) -> Answer:
        if self.outcome is AnswerOutcome.ANSWERED and self.value is None:
            raise ValueError("an answered question needs a value")
        if self.outcome is not AnswerOutcome.ANSWERED and self.value is not None:
            raise ValueError(f"an {self.outcome.value} answer carries no value")
        return self


# --- the automation register (D6, D114) ---------------------------------------------------


class Automation(Event):
    """One change to the register of active automations (D6, D114).

    An ``add`` names the automation (``name``, a path-safe token like a node id) and says
    what it is (``title``); its ref is the automation's identity from then on, so a name
    retired and added again is a new automation with a new ref. ``retire``, ``pin`` and
    ``unpin`` carry ``target``, the ref of the add they act on, like ``supersede.target``.
    The validator holds each verb to exactly its own fields. ``note`` is free text for any
    verb: why it was retired, why it matters enough to pin.

    There is no ``pinned`` or ``active`` field: both are what the fold says after reading
    every event, never something an add declares about itself. Whether this event may change
    the register at all (a person wrote it, the cap allows it, its target is not pinned) is
    decided by the fold, which has the register in hand; one that may not is reported, not
    applied.
    """

    type: Literal["automation"] = "automation"

    verb: AutomationVerb
    name: NodeId | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    target: Ref | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _fields_fit_the_verb(self) -> Automation:
        if self.verb is AutomationVerb.ADD:
            required, allowed = {"name", "title"}, {"name", "title"}
        else:
            required, allowed = {"target"}, {"target"}
        present = {n for n in ("name", "title", "target") if getattr(self, n) is not None}
        if missing := required - present:
            raise ValueError(f"an automation {self.verb.value} needs {sorted(missing)}")
        if extra := present - allowed:
            raise ValueError(f"an automation {self.verb.value} does not carry {sorted(extra)}")
        return self


# --- utterances (D61) ---------------------------------------------------------------------


_SPEAKER_RE = re.compile(r"[A-Za-z0-9+][A-Za-z0-9_.:@+-]{0,127}")


def check_speaker(value: str) -> str:
    """A speaker is the platform's id for a person, never a display name (D61): one token,
    no whitespace and no angle brackets, so it cannot print as a sentence or a tag."""
    if not _SPEAKER_RE.fullmatch(value):
        raise ValueError(
            f"not a speaker id: {value!r} — letters, digits, '_', '.', ':', '@', '+' and '-', "
            "as the platform gives it; never a display name"
        )
    return value


type SpeakerId = Annotated[str, AfterValidator(check_speaker)]

type RoleName = Literal["operator", "stakeholder", "admin"]
"""D27's three roles, by name. ``walle.profiles.Role`` is the enum; the event carries the
name so that the events module imports nothing that reads a folder."""


class Utterance(Event):
    """One completed turn in a conversation (§5.1, §5.2; D61).

    ``conversation`` is the platform's id for the conversation. ``speaker`` is the identity
    the kernel **verified** for the message — the principal's name when the platform's id
    resolves to one, the platform's id otherwise — never a display name and never anything
    the message says about itself. ``role`` is the role the kernel attached to that speaker
    for this message **in this folder**: the principal's own role, or ``None`` for anyone who
    is not the principal — a role is a fact about one profile, and another principal's is
    not kept here (§7, #122) — and ``None`` when it could attach none: an unknown speaker is
    the lowest role. ``body`` is the words, kept as data.

    The context ratchet is a fold over these: the lowest role that has ever spoken in a
    conversation, from the log, so that a restart cannot reset it (D61). There is no
    ``floor`` field, because a floor is what the fold says, never what one turn declares.
    """

    type: Literal["utterance"] = "utterance"

    conversation: CallId
    speaker: SpeakerId
    role: RoleName | None = None
    body: str


# --- outcomes (D19, D20) ------------------------------------------------------------------


class Outcome(Event):
    """One observed run of an automation: ``target`` is the ref of its add, ``result`` how it
    went, ``note`` free text (what it sent, why it failed).

    §5.2 says an outcome is observed against a ``capability`` or a ``mission``. Which kinds of
    target count is the reader's call, not the event's: ``walle.survival`` counts outcomes
    against an automation's add and reports any other. ``ts`` is when the run was recorded,
    which for the runner that writes it is when it ran.

    There is no ``survived`` or ``active`` field: whether an automation survived is what the
    fold says over every event up to a date, never something one run declares.
    """

    type: Literal["outcome"] = "outcome"

    target: Ref
    result: OutcomeResult
    note: str | None = None


class Digest(Event):
    """One run of a principal's digest (D31): ``slot`` is the start of the schedule slot it
    ran for, ``body`` what it said, ``model`` what composed it.

    ``body`` is ``None`` when the run had nothing to say. Silence is a result (D63): it is
    recorded, so the slot is spent, and nothing is sent. A body that is only whitespace is
    refused, so silence has one spelling. When there is a body it is written here because a
    composed digest is not reproducible by replay (hard constraint 4), the same reason a
    ``snapshot`` carries its body.

    There is no recipient field on purpose (D63): the run decides *whether* there is
    something to say, and configuration decides *where* it goes. Nothing appended to the log
    can steer a later run's delivery. ``ts`` is when the run happened.
    """

    type: Literal["digest"] = "digest"

    slot: Instant
    body: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def _silence_has_one_spelling(self) -> Digest:
        if self.body is not None and not self.body.strip():
            raise ValueError("a digest with nothing to say has no body, not a blank one")
        return self


type AnyEvent = Annotated[
    Observation
    | Assertion
    | Proposal
    | Supersede
    | Snapshot
    | Contradiction
    | Mission
    | Step
    | Ask
    | Answer
    | Utterance
    | Automation
    | Outcome
    | Digest,
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)

EVENT_TYPES: dict[str, type[Event]] = {
    "observation": Observation,
    "assertion": Assertion,
    "proposal": Proposal,
    "supersede": Supersede,
    "snapshot": Snapshot,
    "contradiction": Contradiction,
    "mission": Mission,
    "step": Step,
    "ask": Ask,
    "answer": Answer,
    "utterance": Utterance,
    "automation": Automation,
    "outcome": Outcome,
    "digest": Digest,
}


def parse_event(data: dict[str, Any]) -> AnyEvent:
    """Load one event from its JSON form, dispatching on ``type``.

    Raises ``FutureSchemaError`` for an event from a newer build (D111), and
    ``pydantic.ValidationError`` for anything malformed — including an unknown field, which
    is a failure and not something to drop.
    """
    return _ADAPTER.validate_python(data)


# --- canonical form ----------------------------------------------------------------------


def canonical_json(event: Event) -> str:
    """The form an event is hashed in (D66): sorted keys, no spaces, ASCII-escaped, and
    ``seq`` excluded.

    This is **not** the form written to the file. The file is readable JSONL with ``seq``
    in it; this is a byte-stable projection for comparison, and the two are deliberately
    different functions.

    ``seq`` is excluded because the hash identifies an event's *content*, not its position.
    That is what makes it useful: the same content appended twice, at different positions,
    has one hash, so the rebuild test can spot a duplicate instead of seeing two rows.
    """
    data = event.model_dump(mode="json", exclude_none=True)
    data.pop("seq", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(event: Event) -> str:
    """SHA-256 of the canonical form, hex. Stable across field order and across machines."""
    return hashlib.sha256(canonical_json(event).encode("ascii")).hexdigest()
