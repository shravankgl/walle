"""The mission fold: the DAG each mission stands at, from its event and its steps (D67).

A ``mission`` event is the plan: nodes, links, manifests. Nothing about it is ever edited.
What happened since is ``step`` events — a node started, completed or failed; the mission
paused or resumed; a node injected; a node handed to someone else — and the DAG as it stands
now is a fold over those, in ref order, in the same way ``walle.resolver`` folds assertions.
Hard constraint 3 applies in full: this module opens no file, reads no clock and calls no
model, and the same records in any order give the same ``Missions``.

The rules
---------
1. **Supersede undoes (D8).** A step is applied only while it is live. Undoing a pause is a
   tombstone on the pause; undoing a reassign is a tombstone on the reassign. A superseded
   mission is not folded at all, and its steps are reported as unapplied.
2. **Last live delta wins.** A node's status is the status of the last live ``status`` step
   that names it, in ref order, or ``pending`` if none does. The fold does not police
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
   runner may actually start: nothing while the mission is paused, and nothing at all unless
   the owner has turned missions on — outside the log, in the environment, where no event
   the agent appends can reach (D117: install is not enable). There is no runner in this
   build; this function is the seam one will have to come through. A pause holds what has
   not started; a status delta on a step already running is still recorded while paused,
   because the log records what happened, not what should have.

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
    AnyEvent,
    Mission,
    Node,
    Step,
    StepStatus,
    StepVerb,
    content_hash,
    find_cycle,
    parse_ref,
)
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

    injected_by: str | None = None
    """The step that added it, or ``None`` for a node the mission event declared."""


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
    """Mission order, then injected nodes in ref order of their inject steps."""

    links: tuple[tuple[str, str], ...]
    """``(source, target)`` pairs: the mission's, then each applied inject's, in order."""

    paused: bool
    applied: tuple[str, ...]
    """Refs of the live steps that were applied, in ref order."""

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


@dataclass(frozen=True)
class Missions:
    """The fold's answer: every live mission in ref order, and what could not be applied."""

    missions: tuple[MissionState, ...]
    unapplied: tuple[Unapplied, ...]

    def mission(self, ref: str) -> MissionState:
        """The state of the mission at ``ref``. ``KeyError`` if it is not a live mission."""
        for state in self.missions:
            if state.ref == ref:
                return state
        raise KeyError(f"{ref} is not a live mission")


def dispatchable(state: MissionState, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Rule 5: the ready nodes a runner may start now — none unless missions are on, and
    none while the mission is paused."""
    if not enabled(environ) or state.paused:
        return ()
    return state.ready


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
        injected_by=dict.fromkeys((n.id for n in mission.nodes), None),
    )


def fold_missions(records: Iterable[Record]) -> Missions:
    """Fold records from any number of streams into the DAG each live mission stands at.

    ``records`` is what ``walle.reader`` returns. The same record passed twice counts once,
    and the order they arrive in does not matter: steps are applied in ref order.
    """
    events, live = live_events(records)
    ordered = sorted(events, key=parse_ref)

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
