"""Issue #67's verify criteria, and the cases around them (D8, D67, D117).

  same log, same DAG       folding the same records twice, in any order, gives one DAG and one hash
  a cycle is refused       a mission whose links loop never becomes an event; an inject that
                           would close a loop is reported and leaves the DAG as it was
  interventions are events pause, resume, inject and reassign are ``step`` events in the log,
                           folded like any other; a superseded one is undone
  ready is a pure rule     a node is ready when every predecessor is completed, from status alone
  off by default           nothing is dispatchable unless the owner has turned missions on,
                           outside the log, and the switch is not on in a fresh environment
  pure                     the module reaches no model, no network, no file and no clock

Every mission here is synthetic: a made-up weekend of chores, no real data and no real tool.
"""

from __future__ import annotations

import ast
import json
import random
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from test_constraints import MODEL_OR_NETWORK, forbidden_in_closure
from walle.events import (
    ACTOR_KERNEL,
    ACTOR_USER,
    AnyEvent,
    Link,
    Mission,
    Node,
    Step,
    StepStatus,
    StepVerb,
    Supersede,
    find_cycle,
    parse_event,
)
from walle.index import load_records, rebuild
from walle.missions import (
    MISSIONS_SWITCH,
    SWITCH_ON,
    Missions,
    MissionState,
    dag_hash,
    dispatchable,
    enabled,
    fold_missions,
)
from walle.paths import log_dir
from walle.reader import Record, read_file

TS = "2026-11-05T09:00:00.000000+00:00"
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
MISSIONS_SOURCE = PACKAGE / "missions.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v1" / "chores"


# --- builders -----------------------------------------------------------------------------


def _node(node_id: str, **extra: Any) -> Node:
    return Node(id=node_id, title=f"step {node_id}", **extra)


def _link(source: str, target: str) -> Link:
    return Link(source=source, target=target)


def _mission(nodes: list[Node], links: list[Link], *, title: str = "Saturday chores") -> Mission:
    return Mission(ts=TS, actor=ACTOR_USER, title=title, nodes=nodes, links=links)


def _diamond() -> Mission:
    """``a -> b``, ``a -> c``, ``b -> d``, ``c -> d``: the smallest DAG with a join."""
    return _mission(
        [_node("a"), _node("b"), _node("c"), _node("d")],
        [_link("a", "b"), _link("a", "c"), _link("b", "d"), _link("c", "d")],
    )


def _status(mission: str, node: str, status: StepStatus, **extra: Any) -> Step:
    return Step(
        ts=TS,
        actor=ACTOR_KERNEL,
        mission=mission,
        verb=StepVerb.STATUS,
        node=node,
        status=status,
        **extra,
    )


def _pause(mission: str) -> Step:
    return Step(ts=TS, actor=ACTOR_USER, mission=mission, verb=StepVerb.PAUSE)


def _resume(mission: str) -> Step:
    return Step(ts=TS, actor=ACTOR_USER, mission=mission, verb=StepVerb.RESUME)


def _inject(
    mission: str, node: Node, *, after: list[str] | None = None, before: list[str] | None = None
) -> Step:
    return Step(
        ts=TS,
        actor=ACTOR_USER,
        mission=mission,
        verb=StepVerb.INJECT,
        inject=node,
        after=after or [],
        before=before or [],
    )


def _reassign(mission: str, node: str, assignee: str) -> Step:
    return Step(
        ts=TS,
        actor=ACTOR_USER,
        mission=mission,
        verb=StepVerb.REASSIGN,
        node=node,
        assignee=assignee,
    )


def _supersede(target: str) -> Supersede:
    return Supersede(ts=TS, actor=ACTOR_USER, target=target, reason="undone")


def _stream(*events: AnyEvent, device: str = "mbp") -> list[Record]:
    """Records as ``reader`` would hand them over: seq from 1, refs ``d-<device>:<seq>``."""
    out: list[Record] = []
    for seq, event in enumerate(events, start=1):
        stored = event.model_copy(update={"seq": seq})
        out.append(Record(ref=f"d-{device}:{seq}", seq=seq, event=stored, line=seq))
    return out


def _one(missions: Missions, ref: str = "d-mbp:1") -> MissionState:
    return missions.mission(ref)


def _statuses(state: MissionState) -> dict[str, str]:
    return {n.id: n.status.value for n in state.nodes}


# --- same log, same DAG (verify) ----------------------------------------------------------


def test_folding_the_same_log_twice_gives_the_same_dag() -> None:
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.RUNNING),
        _status("d-mbp:1", "a", StepStatus.COMPLETED, cost_usd=0.02, tokens=1200),
        _inject("d-mbp:1", _node("e"), after=["a"], before=["d"]),
        _reassign("d-mbp:1", "c", "helper"),
        _pause("d-mbp:1"),
    )

    first, second = fold_missions(records), fold_missions(records)

    assert first == second
    assert dag_hash(_one(first)) == dag_hash(_one(second))
    assert fold_missions(records + records) == first


def test_input_order_does_not_change_the_dag() -> None:
    mbp = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
        _status("d-mbp:1", "b", StepStatus.COMPLETED),
        device="mbp",
    )
    phone = _stream(
        _inject("d-mbp:1", _node("e"), after=["b"], before=["d"]),
        _status("d-mbp:1", "c", StepStatus.FAILED),
        _status("d-mbp:1", "c", StepStatus.PENDING),
        _reassign("d-mbp:1", "d", "helper"),
        device="phone",
    )
    expected = fold_missions(mbp + phone)
    shuffled = mbp + phone
    for seed in range(20):
        random.Random(seed).shuffle(shuffled)
        folded = fold_missions(shuffled)
        assert folded == expected
        assert dag_hash(_one(folded)) == dag_hash(_one(expected))
    assert _one(expected).ready == ("c", "e")


def test_the_dag_hash_moves_when_the_dag_does() -> None:
    base = _stream(_diamond(), _status("d-mbp:1", "a", StepStatus.COMPLETED))
    before = dag_hash(_one(fold_missions(base)))
    after = dag_hash(_one(fold_missions(base + _stream(_pause("d-mbp:1"), device="phone"))))
    assert before != after


def test_the_dag_hash_covers_the_plan_as_written_too() -> None:
    """Two missions with the same nodes but different titles are two plans."""
    a = _one(fold_missions(_stream(_diamond())))
    b = _one(fold_missions(_stream(_mission(_diamond().nodes, _diamond().links, title="Sunday"))))
    assert len(dag_hash(a)) == 64 and set(dag_hash(a)) <= set("0123456789abcdef")
    assert dag_hash(a) != dag_hash(b)


# --- a cycle is refused (verify) ----------------------------------------------------------


def test_a_mission_with_a_cycle_is_refused_as_an_event() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        _mission([_node("a"), _node("b")], [_link("a", "b"), _link("b", "a")])


def test_a_self_link_is_a_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        _mission([_node("a")], [_link("a", "a")])


def test_a_long_cycle_is_named_in_order() -> None:
    nodes = [_node(n) for n in "abcd"]
    links = [_link("a", "b"), _link("b", "c"), _link("c", "d"), _link("d", "b")]
    with pytest.raises(ValidationError, match=r"b -> c -> d -> b"):
        _mission(nodes, links)
    assert find_cycle(["a", "b", "c", "d"], [(x.source, x.target) for x in links]) == [
        "b",
        "c",
        "d",
        "b",
    ]
    assert find_cycle(["a", "b"], [("a", "b")]) is None


def test_a_cycle_cannot_enter_the_log_by_parsing_either() -> None:
    """The validator runs on load too, so a hand-edited line is refused, not folded."""
    data = _diamond().model_dump(mode="json", exclude_none=True)
    data["links"].append({"source": "d", "target": "a"})
    with pytest.raises(ValidationError, match="cycle"):
        parse_event(data)


@pytest.mark.parametrize(
    ("nodes", "links", "message"),
    [
        (["a", "a"], [], "twice"),
        (["a"], [("a", "zz")], "unknown node"),
        (["a"], [("zz", "a")], "unknown node"),
        (["a", "b"], [("a", "b"), ("a", "b")], "twice"),
    ],
)
def test_a_malformed_dag_is_refused(
    nodes: list[str], links: list[tuple[str, str]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _mission([_node(n) for n in nodes], [_link(s, t) for s, t in links])


def test_an_inject_that_would_close_a_cycle_is_reported_and_leaves_the_dag_alone() -> None:
    records = _stream(
        _diamond(),
        # e after d and before a: d -> e -> a -> ... -> d
        _inject("d-mbp:1", _node("e"), after=["d"], before=["a"]),
    )

    missions = fold_missions(records)

    state = _one(missions)
    assert [n.id for n in state.nodes] == ["a", "b", "c", "d"]
    assert state.applied == ()
    assert [(u.ref, u.reason) for u in missions.unapplied] == [
        ("d-mbp:2", "injecting e would close a cycle: a -> b -> d -> e -> a")
    ]
    assert dag_hash(state) == dag_hash(_one(fold_missions(records[:1])))


# --- interventions are events (verify) ----------------------------------------------------


def test_pause_and_resume_are_events() -> None:
    records = _stream(_diamond(), _pause("d-mbp:1"))
    paused = _one(fold_missions(records))
    assert paused.paused is True
    assert paused.ready == ("a",), "the DAG rule does not change; dispatch does"
    assert paused.applied == ("d-mbp:2",)

    resumed = _one(fold_missions(records + _stream(_resume("d-mbp:1"), device="phone")))
    assert resumed.paused is False


def test_inject_is_an_event_that_adds_a_node_and_its_links() -> None:
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
        _inject("d-mbp:1", _node("e", reads=["calendar"]), after=["a"], before=["d"]),
    )

    state = _one(fold_missions(records))

    assert [n.id for n in state.nodes] == ["a", "b", "c", "d", "e"]
    assert ("a", "e") in state.links and ("e", "d") in state.links
    assert state.node("e").predecessors == ("a",)
    assert state.node("d").predecessors == ("b", "c", "e")
    assert state.node("e").node.reads == ["calendar"]
    assert state.node("e").injected_by == "d-mbp:3"
    assert state.ready == ("b", "c", "e")


def test_an_injected_node_can_be_a_root_or_a_leaf() -> None:
    records = _stream(_diamond(), _inject("d-mbp:1", _node("e")))
    state = _one(fold_missions(records))
    assert state.node("e").predecessors == ()
    assert state.ready == ("a", "e")


def test_reassign_is_an_event() -> None:
    records = _stream(
        _mission([_node("a", assignee="walle")], []),
        _reassign("d-mbp:1", "a", "helper"),
    )
    state = _one(fold_missions(records))
    assert state.node("a").assignee == "helper"
    assert state.node("a").node.assignee == "walle", "the mission event is never rewritten"
    assert state.applied == ("d-mbp:2",)


def test_a_superseded_intervention_is_undone() -> None:
    """D8: undoing a pause is a tombstone on the pause, never an edit."""
    records = _stream(
        _diamond(),
        _pause("d-mbp:1"),
        _reassign("d-mbp:1", "a", "helper"),
        _supersede("d-mbp:2"),
        _supersede("d-mbp:3"),
    )
    state = _one(fold_missions(records))
    assert state.paused is False
    assert state.node("a").assignee is None
    assert state.applied == ()


def test_a_superseded_status_delta_is_undone() -> None:
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
        _supersede("d-mbp:2"),
    )
    state = _one(fold_missions(records))
    assert _statuses(state)["a"] == "pending"
    assert state.ready == ("a",)


def test_a_superseded_mission_is_gone_and_its_steps_are_reported() -> None:
    records = _stream(
        _diamond(), _status("d-mbp:1", "a", StepStatus.COMPLETED), _supersede("d-mbp:1")
    )
    missions = fold_missions(records)
    assert missions.missions == ()
    assert [(u.ref, u.reason) for u in missions.unapplied] == [
        ("d-mbp:2", "its mission d-mbp:1 is superseded")
    ]


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        (
            _status("d-mbp:1", "zz", StepStatus.RUNNING),
            "names node 'zz', which d-mbp:1 has no node of",
        ),
        (_reassign("d-mbp:1", "zz", "helper"), "names node 'zz', which d-mbp:1 has no node of"),
        (_inject("d-mbp:1", _node("a")), "injects 'a', which d-mbp:1 already has"),
        (
            _inject("d-mbp:1", _node("e"), after=["zz"]),
            "names node 'zz', which d-mbp:1 has no node of",
        ),
        (
            _inject("d-mbp:1", _node("e"), before=["zz"]),
            "names node 'zz', which d-mbp:1 has no node of",
        ),
        (
            _status("d-mbp:9", "a", StepStatus.RUNNING),
            "targets d-mbp:9, which the fold was not given",
        ),
        (_status("d-mbp:2", "a", StepStatus.RUNNING), "targets d-mbp:2, which is not a mission"),
    ],
)
def test_a_step_the_fold_cannot_apply_is_reported_not_dropped(step: Step, reason: str) -> None:
    records = _stream(_diamond(), step)
    missions = fold_missions(records)
    assert [(u.ref, u.reason) for u in missions.unapplied] == [("d-mbp:2", reason)]
    assert _one(missions).applied == ()


def test_an_inject_naming_one_predecessor_twice_is_reported() -> None:
    records = _stream(_diamond(), _inject("d-mbp:1", _node("e"), after=["a", "a"]))
    missions = fold_missions(records)
    assert [u.reason for u in missions.unapplied] == ["injects 'e' with the same link twice"]
    assert [n.id for n in _one(missions).nodes] == ["a", "b", "c", "d"]


def test_a_status_delta_is_recorded_while_paused() -> None:
    """The kernel finishing a step it had already started is a fact, pause or not."""
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.RUNNING),
        _pause("d-mbp:1"),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
    )
    state = _one(fold_missions(records))
    assert _statuses(state)["a"] == "completed"
    assert state.paused is True
    assert dispatchable(state, {MISSIONS_SWITCH: SWITCH_ON}) == ()


def test_an_unapplied_step_does_not_hide_a_later_good_one() -> None:
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "zz", StepStatus.RUNNING),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
    )
    state = _one(fold_missions(records))
    assert state.applied == ("d-mbp:3",)
    assert state.ready == ("b", "c")


# --- the step event's shape ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verb": StepVerb.STATUS, "node": "a"},  # no status
        {"verb": StepVerb.STATUS, "status": StepStatus.RUNNING},  # no node
        {"verb": StepVerb.PAUSE, "node": "a"},  # pause is mission-wide
        {"verb": StepVerb.RESUME, "status": StepStatus.RUNNING},
        {"verb": StepVerb.INJECT},  # nothing to inject
        {"verb": StepVerb.INJECT, "inject": _node("e"), "node": "e"},  # the node is in inject
        {"verb": StepVerb.INJECT, "inject": _node("e"), "assignee": "x"},
        {"verb": StepVerb.REASSIGN, "node": "a"},  # no assignee
        {"verb": StepVerb.REASSIGN, "assignee": "x"},  # no node
        {"verb": StepVerb.REASSIGN, "node": "a", "assignee": "x", "after": ["b"]},
        {"verb": StepVerb.STATUS, "node": "a", "status": StepStatus.RUNNING, "inject": _node("e")},
        {"verb": StepVerb.PAUSE, "cost_usd": 0.1},  # spend belongs to a status delta
    ],
)
def test_a_step_carries_exactly_the_fields_its_verb_needs(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Step(ts=TS, actor=ACTOR_USER, mission="d-mbp:1", **kwargs)


def test_a_step_round_trips_with_its_spend() -> None:
    step = _status("d-mbp:1", "a", StepStatus.COMPLETED, cost_usd=0.5, tokens=30_000, note="ok")
    wire = json.dumps(step.model_dump(mode="json", exclude_none=True))
    assert parse_event(json.loads(wire)) == step


def test_spend_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        _status("d-mbp:1", "a", StepStatus.COMPLETED, cost_usd=-0.1)
    with pytest.raises(ValidationError):
        _status("d-mbp:1", "a", StepStatus.COMPLETED, tokens=-1)


def test_a_node_declares_its_manifest() -> None:
    """D67 with D75: reads, writes and egress, and a token budget for D22's abort."""
    node = Node(
        id="mail",
        title="Read this week's mail",
        reads=["principals/p-maya/log"],
        writes=[],
        egress=["gmail.googleapis.com"],
        token_budget=20_000,
    )
    assert node.model_dump(mode="json")["egress"] == ["gmail.googleapis.com"]
    with pytest.raises(ValidationError):
        Node(id="Bad Id", title="x")
    with pytest.raises(ValidationError):
        Node(id="x", title="x", token_budget=0)


# --- ready is a pure rule of node status --------------------------------------------------


def test_a_node_is_ready_when_every_predecessor_is_completed() -> None:
    mission = _diamond()
    state = _one(fold_missions(_stream(mission)))
    assert state.ready == ("a",)

    after_a = _one(fold_missions(_stream(mission, _status("d-mbp:1", "a", StepStatus.COMPLETED))))
    assert after_a.ready == ("b", "c")

    one_side = _one(
        fold_missions(
            _stream(
                mission,
                _status("d-mbp:1", "a", StepStatus.COMPLETED),
                _status("d-mbp:1", "b", StepStatus.COMPLETED),
            )
        )
    )
    assert one_side.ready == ("c",), "d waits for c; one predecessor is not every predecessor"


def test_running_and_failed_nodes_are_not_ready() -> None:
    mission = _diamond()
    running = _one(fold_missions(_stream(mission, _status("d-mbp:1", "a", StepStatus.RUNNING))))
    assert running.ready == ()
    assert running.running == ("a",)

    failed = _one(fold_missions(_stream(mission, _status("d-mbp:1", "a", StepStatus.FAILED))))
    assert failed.ready == ()
    assert failed.outcome == "failed"


def test_a_failed_node_set_back_to_pending_is_ready_again() -> None:
    """D91: on resume a step with no outcome is re-dispatched or failed, explicitly."""
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.FAILED),
        _status("d-mbp:1", "a", StepStatus.PENDING),
    )
    state = _one(fold_missions(records))
    assert state.ready == ("a",)
    assert state.outcome == "open"


def test_the_last_live_delta_in_ref_order_wins() -> None:
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
        _status("d-mbp:1", "a", StepStatus.RUNNING),
    )
    assert _statuses(_one(fold_missions(records)))["a"] == "running"


def test_a_mission_is_done_when_every_node_is_completed() -> None:
    records = _stream(
        _diamond(),
        *(_status("d-mbp:1", n, StepStatus.COMPLETED) for n in "abcd"),
    )
    state = _one(fold_missions(records))
    assert state.outcome == "done"
    assert state.ready == ()
    assert state.spent == (0.0, 0)


def test_spend_is_summed_over_live_status_deltas() -> None:
    records = _stream(
        _diamond(),
        _status("d-mbp:1", "a", StepStatus.RUNNING, cost_usd=0.1, tokens=100),
        _status("d-mbp:1", "a", StepStatus.COMPLETED, cost_usd=0.25, tokens=2000),
        _status("d-mbp:1", "b", StepStatus.FAILED, cost_usd=0.05),
        _supersede("d-mbp:4"),
    )
    state = _one(fold_missions(records))
    assert state.spent == (0.35, 2100)
    assert state.node("a").spent == (0.35, 2100)
    assert state.node("b").spent == (0.0, 0)


def test_two_missions_fold_apart() -> None:
    records = _stream(
        _diamond(),
        _mission([_node("x")], [], title="Sunday"),
        _status("d-mbp:1", "a", StepStatus.COMPLETED),
        _status("d-mbp:2", "x", StepStatus.RUNNING),
    )
    missions = fold_missions(records)
    assert [m.ref for m in missions.missions] == ["d-mbp:1", "d-mbp:2"]
    assert missions.mission("d-mbp:1").ready == ("b", "c")
    assert missions.mission("d-mbp:2").running == ("x",)
    with pytest.raises(KeyError):
        missions.mission("d-mbp:3")


# --- off by default (the owner's condition) -----------------------------------------------


def test_missions_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here runs unless the owner turns it on, outside the log (D117)."""
    monkeypatch.delenv(MISSIONS_SWITCH, raising=False)
    assert enabled() is False
    assert enabled({}) is False

    state = _one(fold_missions(_stream(_diamond())))
    assert state.ready == ("a",), "the fold still answers; only dispatch is withheld"
    assert dispatchable(state) == ()
    assert dispatchable(state, {}) == ()


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", "On", " on", "on "])
def test_only_the_one_spelling_turns_missions_on(value: str) -> None:
    assert enabled({MISSIONS_SWITCH: value}) is False


def test_the_owner_turns_missions_on_outside_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MISSIONS_SWITCH, SWITCH_ON)
    assert enabled() is True
    state = _one(fold_missions(_stream(_diamond())))
    assert dispatchable(state) == ("a",)
    assert dispatchable(state, {}) == (), "an explicit environment is the whole answer"


def test_a_paused_mission_dispatches_nothing_even_when_on() -> None:
    on = {MISSIONS_SWITCH: SWITCH_ON}
    records = _stream(_diamond(), _pause("d-mbp:1"))
    paused = _one(fold_missions(records))
    assert paused.ready == ("a",)
    assert dispatchable(paused, on) == ()
    resumed = _one(fold_missions(records + _stream(_resume("d-mbp:1"), device="phone")))
    assert dispatchable(resumed, on) == ("a",)


def test_nothing_in_the_log_can_turn_missions_on() -> None:
    """The switch is read from the environment only: no event field, no file under the root."""
    source = MISSIONS_SOURCE.read_text()
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "open" not in names and "Path" not in names
    assert "read_text" not in attrs and "exists" not in attrs
    assert not any(field in Mission.model_fields for field in ("enabled", "on", "active"))
    assert not any(field in Step.model_fields for field in ("enabled", "on", "active"))


def test_the_cli_gains_no_mission_command() -> None:
    """The command surface stays as it was: there is nothing a person can run that acts."""
    from walle.cli import main

    assert not any("mission" in name for name in main.commands)


# --- pure --------------------------------------------------------------------------------


def test_missions_reach_no_model_and_no_network() -> None:
    """Constraint 3, the same check ``test_constraints`` runs for the resolver, on this root."""
    assert forbidden_in_closure(PACKAGE, ("missions",)) == {}
    assert "walle.models" not in _runtime_imports(MISSIONS_SOURCE)


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in tree.body:  # top level only: imports under `if TYPE_CHECKING:` never run
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_mission_fold_does_no_io_and_reads_no_clock() -> None:
    imports = _runtime_imports(MISSIONS_SOURCE)
    allowed = {
        "__future__",
        "dataclasses",
        "enum",
        "hashlib",
        "json",
        "os",
        "typing",
        "walle.events",
        "walle.manifest",  # task 68: the gate's matching rules; held to the same purity test
        "walle.resolver",
    }
    assert imports <= allowed, imports - allowed
    assert not (imports & MODEL_OR_NETWORK)
    assert "datetime" not in imports and "time" not in imports


def test_the_resolver_still_reaches_no_model_through_missions() -> None:
    """missions imports resolver, never the other way round: the fold's closure is unchanged."""
    assert "walle.missions" not in _runtime_imports(PACKAGE / "resolver.py")
    assert "walle.missions" not in _runtime_imports(PACKAGE / "events.py")


# --- the rebuild test stays green (verify) ------------------------------------------------


def test_the_golden_log_holds_a_mission_and_every_verb() -> None:
    """``tests/fixtures/v1/chores`` is the frozen v1 shape of both new types (D88)."""
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    lines = [
        json.loads(line)
        for f in sorted(log_dir(GOLDEN, expected["principal"]).glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {d["type"] for d in lines} >= {"mission", "step"}
    assert {d["verb"] for d in lines if d["type"] == "step"} == {
        "status",
        "pause",
        "resume",
        "inject",
        "reassign",
    }


def test_the_mission_fold_over_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    """The DAG the shipping build saw, pinned: a rebuild that folds differently fails here.

    The golden check in ``test_golden`` pins the *belief* hash, which no mission moves, and
    its ``__expected__.json`` has a fixed set of keys, so the mission fold's pin lives here
    as ``DAG_HASH``. Moving it is a visible diff in a test file, which is the point.
    """
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    principal = expected["principal"]
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):  # a read-only checkout must not fail the rebuild
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))

    report = rebuild(root, principal)
    from_index = fold_missions(load_records(root, principal))
    from_log = fold_missions(
        r
        for f in sorted(log_dir(root, principal).glob("*.jsonl"))
        for r in read_file(f, f.stem).records
    )

    assert fold_missions(report.records) == from_index == from_log
    pinned = expected["sentinels"]
    state = from_index.mission("d-mbp:1")
    assert state.ready == ("water-plants",)
    assert state.paused is False
    assert [n.id for n in state.nodes] == pinned_nodes(pinned)
    assert dag_hash(state) == DAG_HASH


def pinned_nodes(sentinels: dict[str, Any]) -> list[str]:
    """The node ids the fixture declares, in mission order then inject order."""
    mission = sorted(k for k in sentinels if k.startswith("d-mbp:1.nodes.") and k.endswith(".id"))
    ids = [sentinels[k] for k in sorted(mission, key=lambda k: int(k.split(".")[2]))]
    return [*ids, sentinels["d-mbp:5.inject.id"]]


DAG_HASH = "e8570686c220488852c782f2946d886ec8c0b3fc0ee9ed823fd4be3beffcf854"
