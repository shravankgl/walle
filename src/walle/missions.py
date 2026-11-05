"""The mission fold: the DAG each mission stands at, from its event and its steps (D67).

A ``mission`` event is the plan: nodes, links, manifests. Nothing about it is ever edited.
What happened since is ``step`` events — a node started, completed or failed; the mission
paused or resumed; a node injected; a node handed to someone else — and the DAG as it stands
now is a fold over those, in ``walle.merge``'s order (each stream in its own seq order,
streams interleaved by time: issue #120), in the same way ``walle.resolver`` folds
assertions. Hard constraint 3 applies in full: this module opens no file, reads no clock and
calls no model, and the same records in any order give the same ``Missions``.

The rules
---------
1. **Supersede undoes (D8).** A step is applied only while it is live. Undoing a pause is a
   tombstone on the pause; undoing a reassign is a tombstone on the reassign. A superseded
   mission is not folded at all, and its steps are reported as unapplied.
2. **Last live delta wins.** A node's status is the status of the last live ``status`` step
   that names it, in merge order, or ``pending`` if none does. The fold does not police
   transitions: it records what the kernel and the person said, and D91's recovery reads
   ``running`` nodes as the steps whose outcome is unknown.
3. **Ready is every predecessor completed (D67).** A node is ready when its status is
   ``pending`` and every node that links into it is ``completed``. Failed and running
   predecessors hold their successors back; so does an injected one until it completes.
   This is a function of node status alone — pausing does not change it (rule 5 does).
4. **An inject that would break the DAG is reported, not applied.** A node id the mission
   already has, a link to a node it lacks, or a loop closed by the new links, leaves the
   DAG exactly as it was and lands in ``unapplied`` with the reason. The mission event's own
   validator already refuses a cyclic plan, so the only way a cycle can be asked for is
   here, and the answer is no.
5. **Dispatch is not the fold.** ``ready`` is what the DAG says. ``dispatchable`` is what a
   runner may actually start: nothing while the mission is paused, nothing once it is
   aborted (rule 6), and nothing at all unless the owner has turned missions on — outside
   the log, in the environment, where no event the agent appends can reach (D117: install
   is not enable). There is no runner in this build; ``dispatchable`` and ``admit`` are the
   seam one will have to come through. A pause holds what has not started; a status delta
   on a step already running is still recorded while paused, because the log records what
   happened, not what should have.
6. **A budget breach aborts the mission (D22).** Each node may declare a ``token_budget`` and
   a ``cost_budget_usd``; the fold sums what its live status steps report. The moment a
   node has spent more than either, the mission is aborted: ``breach`` says which node,
   how much and of what, ``dispatchable`` is empty and ``admit`` refuses every node with
   that reason, on every branch, on or off. This is read from the log, not from a flag a
   runner remembers to set, so a runner that crashed mid-step and comes back finds the
   same abort. ``abort_step`` is the event it appends to say so: the node ``failed``, the
   reason as its note. Undoing the spend is a tombstone on a status step (rule 1); a bigger
   budget is a new mission, because the plan is never edited.
7. **The manifest is checked before the touch (D32 rule 2, D67).** ``admit`` takes what a
   step is about to read, write and send to, and refuses anything the node did not declare,
   naming every undeclared item — before the step starts and again at each tool call while
   it runs. The matching rules are ``walle.manifest``'s.
8. **The floor is under the manifest (D72).** A write to a file that governs permission —
   ``kernel/``, ``shared/``, a principal's ``grants/``, ``keys/`` or ``hot/``, anything under
   one or any parent of one — is refused whether or not the node declared it, and before
   every other reason, the switch included: the same refusal in every mode. ``admit`` asks
   ``walle.floors.hit`` by name, and a path ``hit`` cannot answer about by name — absolute,
   or climbing out through ``..`` — is its own refusal, not a pass and not "missions are
   off" (issue #118 finding 1.1; ``walle.reviewer.review`` asks the same function and gives
   the same sentence). A runner with a root asks ``walle.floors.floored`` as well, which
   follows links. Nothing in the log, the environment or a call lifts it.

Anything the fold cannot apply is **reported, not dropped**, as the resolver does: a step
whose mission the fold was not given, whose ``mission`` ref is not a mission, or whose node
the mission has no node of, each lands in ``unapplied`` with a reason and the fold goes on.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from walle.events import (
    ACTOR_KERNEL,
    AnyEvent,
    Mission,
    Node,
    Step,
    StepStatus,
    StepVerb,
    content_hash,
    find_cycle,
)
from walle.floors import Kind, hit
from walle.manifest import Touch, undeclared
from walle.merge import fold_order
from walle.resolver import live_events

if TYPE_CHECKING:  # annotations only: the fold takes records, it never reads them itself
    from collections.abc import Iterable, Mapping

    from walle.reader import Record


MISSIONS_SWITCH = "WALLE_MISSIONS"
"""The environment variable the owner sets to turn missions on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on. Not ``1``, ``true`` or ``yes``: one spelling, like an instant,
so that a stray value in a shell profile cannot be mistaken for consent."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned missions on (D117).

    Off unless ``WALLE_MISSIONS`` is exactly ``on``. Reads the process environment when no
    mapping is given; a mapping is the whole answer when one is. The switch lives outside the
    log and outside the walle root on purpose: an event the agent appends, or a file it
    writes, cannot turn this on.
    """
    env = os.environ if environ is None else environ
    return env.get(MISSIONS_SWITCH) == SWITCH_ON


type Outcome = Literal["open", "done", "failed"]


@dataclass(frozen=True)
class NodeState:
    """One node of a mission, as the fold leaves it."""

    id: str
    node: Node
    """The node as declared, in the mission event or in the inject step. Never changed."""

    status: StepStatus
    predecessors: tuple[str, ...]
    """Node ids that must be completed first, in node order."""

    assignee: str | None
    """Who runs it now: the last live reassign, else what the node declared."""

    spent: tuple[float, int]
    """``(cost_usd, tokens)`` summed over this node's live status deltas (D22)."""

    spent_from: tuple[str, ...] = ()
    """Refs of the live status steps that reported spend, in merge order: what an abort step
    cites, and what a later exporter reads the per-step figures from."""

    injected_by: str | None = None

    @property
    def breaches(self) -> tuple[Breach, ...]:
        """Rule 6, for this node: tokens first, then cost, each only if a budget is declared
        and the spend is strictly over it."""
        cost, tokens = self.spent
        out: list[Breach] = []
        if self.node.token_budget is not None and tokens > self.node.token_budget:
            out.append(Breach(self.id, "tokens", tokens, self.node.token_budget))
        if self.node.cost_budget_usd is not None and cost > self.node.cost_budget_usd:
            out.append(Breach(self.id, "cost_usd", cost, self.node.cost_budget_usd))
        return tuple(out)

    """The step that added it, or ``None`` for a node the mission event declared."""


type BreachKind = Literal["tokens", "cost_usd"]


@dataclass(frozen=True)
class Breach:
    """A node that has spent more than one of its budgets (D22)."""

    node: str
    kind: BreachKind
    spent: int | float
    budget: int | float

    @property
    def reason(self) -> str:
        """The sentence the abort step carries and ``admit`` refuses with. Dollars are shown
        to the cent at least and to the millionth at most, never rounded into equality: a
        breach of $0.0061 over a $0.005 budget says so, not "$0.01 of its $0.01"."""
        if self.kind == "tokens":
            return f"node {self.node!r} spent {self.spent} tokens of its {self.budget} token budget"
        spent, budget = _dollars(self.spent), _dollars(self.budget)
        return f"node {self.node!r} spent ${spent} of its ${budget} cost budget"


def _dollars(amount: int | float) -> str:
    text = f"{amount:.6f}".rstrip("0")
    whole, _, cents = text.partition(".")
    return f"{whole}.{cents.ljust(2, '0')}"


NOTHING = Touch()
"""A step that touches nothing, which every manifest covers: ``admit``'s default."""


@dataclass(frozen=True)
class Refusal:
    """Why ``admit`` would not let a step start or go on."""

    node: str
    reason: str


@dataclass(frozen=True)
class Unapplied:
    """A live step the fold could not apply, and why."""

    ref: str
    reason: str


@dataclass(frozen=True)
class MissionState:
    """The DAG one mission stands at. Every tuple is in a fixed order, so two folds of the
    same log compare equal."""

    ref: str
    mission: Mission
    nodes: tuple[NodeState, ...]
    """Mission order, then injected nodes in merge order of their inject steps."""

    links: tuple[tuple[str, str], ...]
    """``(source, target)`` pairs: the mission's, then each applied inject's, in order."""

    paused: bool
    applied: tuple[str, ...]
    """Refs of the live steps that were applied, in merge order."""

    def node(self, node_id: str) -> NodeState:
        for state in self.nodes:
            if state.id == node_id:
                return state
        raise KeyError(f"{self.ref} has no node {node_id!r}")

    @property
    def ready(self) -> tuple[str, ...]:
        """Rule 3: pending nodes whose every predecessor is completed, in node order."""
        status = {n.id: n.status for n in self.nodes}
        return tuple(
            n.id
            for n in self.nodes
            if n.status is StepStatus.PENDING
            and all(status[p] is StepStatus.COMPLETED for p in n.predecessors)
        )

    @property
    def running(self) -> tuple[str, ...]:
        """Nodes started and not finished: D91's unmatched steps, on resume."""
        return tuple(n.id for n in self.nodes if n.status is StepStatus.RUNNING)

    @property
    def outcome(self) -> Outcome:
        """``done`` when every node is completed, ``failed`` while any node is, else ``open``."""
        statuses = {n.status for n in self.nodes}
        if statuses == {StepStatus.COMPLETED}:
            return "done"
        if StepStatus.FAILED in statuses:
            return "failed"
        return "open"

    @property
    def spent(self) -> tuple[float, int]:
        """``(cost_usd, tokens)`` over the whole mission, for D22's threshold."""
        cost = sum(n.spent[0] for n in self.nodes)
        return (round(cost, 6), sum(n.spent[1] for n in self.nodes))

    @property
    def breaches(self) -> tuple[Breach, ...]:
        """Rule 6: every budget breach, in node order."""
        return tuple(b for n in self.nodes for b in n.breaches)

    @property
    def breach(self) -> Breach | None:
        """The first breach in node order — the reason the mission is aborted — or ``None``."""
        breaches = self.breaches
        return breaches[0] if breaches else None

    @property
    def aborted(self) -> bool:
        return bool(self.breaches)


@dataclass(frozen=True)
class Missions:
    """The fold's answer: every live mission in merge order, and what could not be applied."""

    missions: tuple[MissionState, ...]
    unapplied: tuple[Unapplied, ...]

    def mission(self, ref: str) -> MissionState:
        """The state of the mission at ``ref``. ``KeyError`` if it is not a live mission."""
        for state in self.missions:
            if state.ref == ref:
                return state
        raise KeyError(f"{ref} is not a live mission")


def _held(state: MissionState, environ: Mapping[str, str] | None) -> str | None:
    """Why nothing in this mission may run now, or ``None``: off, paused, aborted, in that
    order, so one refusal tells the reader the earlier reasons did not apply."""
    if not enabled(environ):
        return "missions are off"
    if state.paused:
        return "mission is paused"
    if (breach := state.breach) is not None:
        return f"aborted: {breach.reason}"
    return None


def dispatchable(state: MissionState, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Rule 5: the ready nodes a runner may start now — none unless missions are on, none
    while the mission is paused, and none once a budget is breached (rule 6)."""
    if _held(state, environ) is not None:
        return ()
    return state.ready


def admit(
    state: MissionState,
    node_id: str,
    touch: Touch = NOTHING,
    environ: Mapping[str, str] | None = None,
) -> Refusal | None:
    """The gate before a step touches anything (rules 5 to 8). ``None`` means go.

    Refuses, in this order and with the first reason that holds: a write in ``touch`` is
    floored or cannot be answered about by name (rule 8, in any mode, declared or not);
    missions are off; the mission is paused;
    the mission is aborted on a breach; the node is unknown, finished, or not ready;
    something in ``touch`` is not in the node's manifest, every such item named. A
    ``running`` node is admitted, because this is also the check at each tool call.
    """
    for path in touch.writes:
        if (floor := hit(path)) is not None:
            return Refusal(node_id, floor.reason(Kind.WRITE, path))
    if (held := _held(state, environ)) is not None:
        return Refusal(node_id, held)
    try:
        node = state.node(node_id)
    except KeyError:
        return Refusal(node_id, f"{state.ref} has no node {node_id!r}")
    if node.status in (StepStatus.COMPLETED, StepStatus.FAILED):
        return Refusal(node_id, f"already {node.status.value}")
    if node.status is StepStatus.PENDING and node_id not in state.ready:
        status = {n.id: n.status for n in state.nodes}
        waiting = [p for p in node.predecessors if status[p] is not StepStatus.COMPLETED]
        return Refusal(node_id, f"not ready: waiting on {', '.join(waiting)}")
    if missing := undeclared(node.node, touch):
        return Refusal(node_id, "; ".join(missing))
    return None


def abort_step(state: MissionState, breach: Breach, *, ts: str, actor: str = ACTOR_KERNEL) -> Step:
    """The event a runner appends when it stops on a breach: the node ``failed``, the reason
    as its note, caused by the status steps whose spend crossed the line. ``ts`` is passed
    in — this module reads no clock — and the step carries no spend of its own."""
    return Step(
        ts=ts,
        actor=actor,
        caused_by=list(state.node(breach.node).spent_from),
        mission=state.ref,
        verb=StepVerb.STATUS,
        node=breach.node,
        status=StepStatus.FAILED,
        note=breach.reason,
    )


def dag_hash(state: MissionState) -> str:
    """SHA-256 of the resolved DAG in canonical JSON: the mission event's content hash, the
    nodes with status, assignee, spend and predecessors, the links, ``paused``, and the
    applied steps. Two folds of one log hash the same, and any step that changes the DAG
    moves it (D66)."""
    data = {
        "ref": state.ref,
        "mission": content_hash(state.mission),
        "nodes": [
            {
                "id": n.id,
                "status": n.status.value,
                "assignee": n.assignee,
                "predecessors": list(n.predecessors),
                "spent": [n.spent[0], n.spent[1]],
                "injected_by": n.injected_by,
                "node": n.node.model_dump(mode="json", exclude_none=True),
            }
            for n in state.nodes
        ],
        "links": [list(link) for link in state.links],
        "paused": state.paused,
        "applied": list(state.applied),
    }
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


# --- the fold ----------------------------------------------------------------------------


@dataclass
class _Building:
    """One mission while its steps are being applied. Private and mutable: the fold's own
    scratch, never handed out."""

    ref: str
    mission: Mission
    nodes: dict[str, Node]
    links: list[tuple[str, str]]
    status: dict[str, StepStatus]
    assignee: dict[str, str | None]
    spent: dict[str, tuple[float, int]]
    spent_from: dict[str, list[str]]
    injected_by: dict[str, str | None]
    paused: bool = False

    def apply(self, ref: str, step: Step) -> str | None:
        """Apply one live step. Returns a reason when it cannot be applied, else ``None``."""
        if step.verb is StepVerb.PAUSE:
            self.paused = True
        elif step.verb is StepVerb.RESUME:
            self.paused = False
        elif step.verb is StepVerb.INJECT:
            return self._inject(ref, step)
        else:
            node = step.node
            assert node is not None  # the event's validator requires it for these verbs
            if node not in self.nodes:
                return f"names node {node!r}, which {self.ref} has no node of"
            if step.verb is StepVerb.STATUS:
                assert step.status is not None
                self.status[node] = step.status
                cost, tokens = self.spent[node]
                self.spent[node] = (
                    round(cost + (step.cost_usd or 0.0), 6),
                    tokens + (step.tokens or 0),
                )
                if step.cost_usd is not None or step.tokens is not None:
                    self.spent_from[node].append(ref)
            else:
                self.assignee[node] = step.assignee
        return None

    def _inject(self, ref: str, step: Step) -> str | None:
        node = step.inject
        assert node is not None
        if node.id in self.nodes:
            return f"injects {node.id!r}, which {self.ref} already has"
        for end in (*step.after, *step.before):
            if end not in self.nodes:
                return f"names node {end!r}, which {self.ref} has no node of"
        new_links = [(p, node.id) for p in step.after] + [(node.id, s) for s in step.before]
        if len(set(new_links)) != len(new_links):
            return f"injects {node.id!r} with the same link twice"
        cycle = find_cycle([*self.nodes, node.id], [*self.links, *new_links])
        if cycle is not None:
            return f"injecting {node.id} would close a cycle: {' -> '.join(cycle)}"
        self.nodes[node.id] = node
        self.links.extend(new_links)
        self.status[node.id] = StepStatus.PENDING
        self.assignee[node.id] = node.assignee
        self.spent[node.id] = (0.0, 0)
        self.spent_from[node.id] = []
        self.injected_by[node.id] = ref
        return None

    def freeze(self, applied: list[str]) -> MissionState:
        predecessors = {node_id: [] for node_id in self.nodes}
        for source, target in self.links:
            predecessors[target].append(source)
        order = {node_id: i for i, node_id in enumerate(self.nodes)}
        return MissionState(
            ref=self.ref,
            mission=self.mission,
            nodes=tuple(
                NodeState(
                    id=node_id,
                    node=node,
                    status=self.status[node_id],
                    predecessors=tuple(sorted(predecessors[node_id], key=order.__getitem__)),
                    assignee=self.assignee[node_id],
                    spent=self.spent[node_id],
                    spent_from=tuple(self.spent_from[node_id]),
                    injected_by=self.injected_by[node_id],
                )
                for node_id, node in self.nodes.items()
            ),
            links=tuple(self.links),
            paused=self.paused,
            applied=tuple(applied),
        )


def _start(ref: str, mission: Mission) -> _Building:
    return _Building(
        ref=ref,
        mission=mission,
        nodes={node.id: node for node in mission.nodes},
        links=[(link.source, link.target) for link in mission.links],
        status=dict.fromkeys((n.id for n in mission.nodes), StepStatus.PENDING),
        assignee={node.id: node.assignee for node in mission.nodes},
        spent=dict.fromkeys((n.id for n in mission.nodes), (0.0, 0)),
        spent_from={node.id: [] for node in mission.nodes},
        injected_by=dict.fromkeys((n.id for n in mission.nodes), None),
    )


def fold_missions(records: Iterable[Record]) -> Missions:
    """Fold records from any number of streams into the DAG each live mission stands at.

    ``records`` is what ``walle.reader`` returns. The same record passed twice counts once,
    and the order they arrive in does not matter: steps are applied in merge order.
    """
    events, live = live_events(records)
    ordered = fold_order(events)

    building: dict[str, _Building] = {}
    applied: dict[str, list[str]] = {}
    unapplied: list[Unapplied] = []
    for ref in ordered:
        event: AnyEvent = events[ref]
        if isinstance(event, Mission) and live[ref]:
            building[ref] = _start(ref, event)
            applied[ref] = []

    for ref in ordered:
        event = events[ref]
        if not isinstance(event, Step) or not live[ref]:
            continue
        target = events.get(event.mission)
        if target is None:
            unapplied.append(
                Unapplied(ref, f"targets {event.mission}, which the fold was not given")
            )
        elif not isinstance(target, Mission):
            unapplied.append(Unapplied(ref, f"targets {event.mission}, which is not a mission"))
        elif not live[event.mission]:
            unapplied.append(Unapplied(ref, f"its mission {event.mission} is superseded"))
        elif (reason := building[event.mission].apply(ref, event)) is not None:
            unapplied.append(Unapplied(ref, reason))
        else:
            applied[event.mission].append(ref)

    return Missions(
        missions=tuple(b.freeze(applied[ref]) for ref, b in building.items()),
        unapplied=tuple(unapplied),
    )
