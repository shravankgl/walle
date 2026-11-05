"""Issue #68's verify criteria, and the cases around them (D22, D32, D67, D117).

  refused before it runs    a step about to touch a path or host its node did not declare is
                            refused by the gate, with every undeclared item named, and nothing
                            runs; a declared touch on a ready node is admitted
  a breach aborts           a node whose live status steps have spent more tokens or dollars
                            than its budget is a breach; the whole mission dispatches nothing
                            more, the reason says which node, how much and of what, and the
                            step that records the abort carries that reason
  off by default            the gate refuses everything, declared or not, unless the owner has
                            turned missions on outside the log; no command gained
  the rebuild stays green   a frozen synthetic log with budgets folds to the breach it shipped
                            with, from the rebuild, the index and the raw lines alike
  pure                      the manifest module reaches no model, no network, no file, no clock

Every path, host, budget and mission here is synthetic: a made-up Sunday, no real data.
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
    content_hash,
    parse_event,
)
from walle.index import load_records, rebuild
from walle.manifest import Touch, covers_host, covers_path, undeclared
from walle.missions import (
    MISSIONS_SWITCH,
    SWITCH_ON,
    Breach,
    MissionState,
    Refusal,
    abort_step,
    admit,
    dag_hash,
    dispatchable,
    fold_missions,
)
from walle.paths import log_dir
from walle.reader import Record, read_file

TS = "2026-11-08T09:00:00.000000+00:00"
ON = {MISSIONS_SWITCH: SWITCH_ON}
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
MANIFEST_SOURCE = PACKAGE / "manifest.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v1" / "budget"


# --- builders -----------------------------------------------------------------------------


def _node(node_id: str, **extra: Any) -> Node:
    return Node(id=node_id, title=f"step {node_id}", **extra)


def _mission(nodes: list[Node], links: list[Link] | None = None) -> Mission:
    return Mission(
        ts=TS, actor=ACTOR_USER, title="Sunday meal plan", nodes=nodes, links=links or []
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


def _supersede(target: str) -> Supersede:
    return Supersede(ts=TS, actor=ACTOR_USER, target=target, reason="undone")


def _stream(*events: AnyEvent, device: str = "tab") -> list[Record]:
    out: list[Record] = []
    for seq, event in enumerate(events, start=1):
        stored = event.model_copy(update={"seq": seq})
        out.append(Record(ref=f"d-{device}:{seq}", seq=seq, event=stored, line=seq))
    return out


def _one(records: list[Record], ref: str = "d-tab:1") -> MissionState:
    return fold_missions(records).mission(ref)


PLAN = _node(
    "pick-recipes",
    reads=["principals/p-ira/notes", "shared/recipes"],
    writes=["principals/p-ira/plans"],
    egress=["grocer.example"],
    token_budget=2000,
    cost_budget_usd=0.10,
)
ORDER = _node("order-groceries", egress=["grocer.example"], token_budget=3000)


def _meal_plan() -> Mission:
    """``pick-recipes -> order-groceries``: the first declares a manifest and both budgets."""
    return _mission([PLAN, ORDER], [Link(source="pick-recipes", target="order-groceries")])


# --- the manifest: what a declaration covers ---------------------------------------------


@pytest.mark.parametrize(
    ("declared", "touched"),
    [
        ("principals/p-ira/notes", "principals/p-ira/notes"),
        ("principals/p-ira/notes", "principals/p-ira/notes/sunday.md"),
        ("principals/p-ira/notes/", "principals/p-ira/notes/sunday.md"),
        ("principals/p-ira/notes", "principals/p-ira/notes/a/../b.md"),
        ("principals/p-ira/notes", "./principals/p-ira/notes//sunday.md"),
        ("/tmp/walle", "/tmp/walle/out.txt"),
    ],
)
def test_a_declared_path_covers_itself_and_what_is_under_it(declared: str, touched: str) -> None:
    assert covers_path(declared, touched) is True


@pytest.mark.parametrize(
    ("declared", "touched"),
    [
        ("principals/p-ira/notes", "principals/p-ira/notes-old"),
        ("principals/p-ira/notes", "principals/p-ira"),
        ("principals/p-ira/notes", "principals/p-ira/notes/../keys"),
        ("principals/p-ira/notes", "principals/p-ira/notes/../../p-sam/log"),
        ("principals/p-ira/notes", "../principals/p-ira/notes"),
        ("principals/p-ira/notes", "/principals/p-ira/notes"),
        ("/principals/p-ira/notes", "principals/p-ira/notes"),
        ("principals/p-ira/notes", ""),
        ("principals/p-ira/notes", "."),
        ("principals/p-ira/notes", "Principals/p-ira/notes"),
        ("", "principals/p-ira/notes"),
    ],
)
def test_a_declared_path_covers_nothing_beside_or_above_it(declared: str, touched: str) -> None:
    """A shared prefix is not containment, ``..`` is resolved before comparing, and a path
    that climbs out of what was declared is never inside it (D85's spirit: refused, not
    normalized into a grant)."""
    assert covers_path(declared, touched) is False


@pytest.mark.parametrize(
    ("declared", "touched", "covered"),
    [
        ("grocer.example", "grocer.example", True),
        ("grocer.example", "GROCER.example", True),
        ("grocer.example", "grocer.example.", True),
        ("grocer.example", "api.grocer.example", False),
        ("api.grocer.example", "grocer.example", False),
        ("grocer.example", "grocer.example:443", False),
        ("grocer.example", "grocer.example/cart", False),
        ("grocer.example", "", False),
        ("*.grocer.example", "api.grocer.example", False),
    ],
)
def test_a_declared_host_covers_that_host_and_no_other(
    declared: str, touched: str, covered: bool
) -> None:
    """One host, not its subdomains and no wildcard: a grant names what it grants (D75)."""
    assert covers_host(declared, touched) is covered


def test_undeclared_names_every_item_and_which_list_it_is_missing_from() -> None:
    touch = Touch(
        reads=("principals/p-ira/notes/sunday.md", "principals/p-ira/keys"),
        writes=("principals/p-ira/plans/week.md", "principals/p-ira/notes"),
        egress=("grocer.example", "tracker.example"),
    )
    assert undeclared(PLAN, touch) == (
        "read 'principals/p-ira/keys' is not in reads",
        "write 'principals/p-ira/notes' is not in writes",
        "egress to 'tracker.example' is not in egress",
    )


def test_reads_and_writes_are_separate_grants() -> None:
    """A path in ``writes`` does not grant reading it, nor the other way round."""
    assert undeclared(PLAN, Touch(writes=("principals/p-ira/notes/x",))) == (
        "write 'principals/p-ira/notes/x' is not in writes",
    )
    assert undeclared(PLAN, Touch(reads=("principals/p-ira/plans/x",))) == (
        "read 'principals/p-ira/plans/x' is not in reads",
    )


def test_a_node_that_declares_nothing_may_touch_nothing() -> None:
    bare = _node("think")
    assert undeclared(bare, Touch()) == ()
    assert undeclared(bare, Touch(reads=("shared/recipes",))) == (
        "read 'shared/recipes' is not in reads",
    )


# --- refused before it runs (verify) -----------------------------------------------------


def test_a_step_touching_anything_undeclared_is_refused_before_it_runs() -> None:
    """The issue's first criterion. A runner that starts a step only when ``admit`` returns
    nothing never starts this one, and the refusal says what was undeclared."""
    state = _one(_stream(_meal_plan()))
    ran: list[str] = []

    def runner(node: str, touch: Touch) -> Refusal | None:
        refusal = admit(state, node, touch, ON)
        if refusal is None:
            ran.append(node)
        return refusal

    refusal = runner(
        "pick-recipes",
        Touch(reads=("principals/p-ira/notes", "principals/p-sam/log"), egress=("grocer.example",)),
    )

    assert ran == []
    assert refusal == Refusal(
        node="pick-recipes", reason="read 'principals/p-sam/log' is not in reads"
    )


def test_a_declared_touch_on_a_ready_node_is_admitted() -> None:
    state = _one(_stream(_meal_plan()))
    touch = Touch(
        reads=("principals/p-ira/notes/sunday.md", "shared/recipes/soup.md"),
        writes=("principals/p-ira/plans/week.md",),
        egress=("grocer.example",),
    )
    assert admit(state, "pick-recipes", touch, ON) is None
    assert admit(state, "pick-recipes", Touch(), ON) is None, "touching nothing is always fine"


def test_every_undeclared_item_is_in_the_refusal() -> None:
    state = _one(_stream(_meal_plan()))
    touch = Touch(writes=("principals/p-ira/keys",), egress=("tracker.example",))
    refusal = admit(state, "pick-recipes", touch, ON)
    assert refusal is not None
    assert refusal.reason == (
        "write 'principals/p-ira/keys' is not in writes; "
        "egress to 'tracker.example' is not in egress"
    )


def test_a_node_that_is_not_ready_is_refused_even_with_a_declared_touch() -> None:
    state = _one(_stream(_meal_plan()))
    refusal = admit(state, "order-groceries", Touch(egress=("grocer.example",)), ON)
    assert refusal == Refusal(node="order-groceries", reason="not ready: waiting on pick-recipes")


def test_a_running_node_may_keep_touching_what_it_declared() -> None:
    """The gate is also the check at each tool call while a step runs, so ``running`` passes
    and the manifest still bites."""
    records = _stream(_meal_plan(), _status("d-tab:1", "pick-recipes", StepStatus.RUNNING))
    state = _one(records)
    assert admit(state, "pick-recipes", Touch(reads=("shared/recipes",)), ON) is None
    refusal = admit(state, "pick-recipes", Touch(writes=("shared/recipes",)), ON)
    assert refusal == Refusal(node="pick-recipes", reason="write 'shared/recipes' is not in writes")


@pytest.mark.parametrize("status", [StepStatus.COMPLETED, StepStatus.FAILED])
def test_a_finished_node_is_refused(status: StepStatus) -> None:
    records = _stream(_meal_plan(), _status("d-tab:1", "pick-recipes", status))
    refusal = admit(_one(records), "pick-recipes", Touch(), ON)
    assert refusal == Refusal(node="pick-recipes", reason=f"already {status.value}")


def test_a_node_the_mission_lacks_is_refused_not_raised() -> None:
    refusal = admit(_one(_stream(_meal_plan())), "bake-bread", Touch(), ON)
    assert refusal == Refusal(node="bake-bread", reason="d-tab:1 has no node 'bake-bread'")


def test_a_paused_mission_refuses_every_touch() -> None:
    records = _stream(_meal_plan(), _pause("d-tab:1"))
    refusal = admit(_one(records), "pick-recipes", Touch(), ON)
    assert refusal == Refusal(node="pick-recipes", reason="mission is paused")


def test_the_gate_checks_the_manifest_last() -> None:
    """A refusal names the first reason that holds, in a fixed order: off, paused, breached,
    the node's state, then the manifest. So a reader of one refusal knows the earlier
    reasons did not apply."""
    records = _stream(_meal_plan(), _pause("d-tab:1"))
    bad = Touch(reads=("principals/p-sam/log",))
    assert admit(_one(records), "pick-recipes", bad, {}) == Refusal(
        node="pick-recipes", reason="missions are off"
    )
    assert admit(_one(records), "pick-recipes", bad, ON) == Refusal(
        node="pick-recipes", reason="mission is paused"
    )
    assert admit(_one(records[:1]), "order-groceries", bad, ON) == Refusal(
        node="order-groceries", reason="not ready: waiting on pick-recipes"
    )
    assert admit(_one(records[:1]), "pick-recipes", bad, ON) == Refusal(
        node="pick-recipes", reason="read 'principals/p-sam/log' is not in reads"
    )


# --- a breach aborts the mission and says why (verify) -----------------------------------


def _spend(*amounts: tuple[int, float]) -> list[Record]:
    """The meal plan with ``pick-recipes`` running and reporting each ``(tokens, cost)``."""
    steps = [
        _status("d-tab:1", "pick-recipes", StepStatus.RUNNING, tokens=t, cost_usd=c)
        for t, c in amounts
    ]
    return _stream(_meal_plan(), *steps)


def test_a_token_budget_breach_aborts_the_mission_and_says_why() -> None:
    """The issue's second criterion, over tokens."""
    state = _one(_spend((800, 0.02), (900, 0.02), (600, 0.02)))
    assert state.spent == (0.06, 2300)
    breach = state.breach
    assert breach == Breach(node="pick-recipes", kind="tokens", spent=2300, budget=2000)
    assert breach is not None
    assert breach.reason == "node 'pick-recipes' spent 2300 tokens of its 2000 token budget"
    assert state.aborted is True
    assert dispatchable(state, ON) == ()
    assert admit(state, "pick-recipes", Touch(), ON) == Refusal(
        node="pick-recipes",
        reason="aborted: node 'pick-recipes' spent 2300 tokens of its 2000 token budget",
    )


def test_a_cost_budget_breach_aborts_the_mission_and_says_why() -> None:
    """The issue's second criterion, over dollars."""
    state = _one(_spend((500, 0.04), (500, 0.04), (500, 0.03)))
    assert state.spent == (0.11, 1500)
    breach = state.breach
    assert breach == Breach(node="pick-recipes", kind="cost_usd", spent=0.11, budget=0.1)
    assert breach is not None
    assert breach.reason == "node 'pick-recipes' spent $0.11 of its $0.10 cost budget"
    assert dispatchable(state, ON) == ()


@pytest.mark.parametrize(
    ("spent", "budget", "words"),
    [
        (0.11, 0.1, "$0.11 of its $0.10"),
        (0.0061, 0.005, "$0.0061 of its $0.005"),
        (1.5, 1.0, "$1.50 of its $1.00"),
        (12.345678, 12.0, "$12.345678 of its $12.00"),
    ],
)
def test_the_cost_reason_shows_the_figures_and_never_rounds_them_into_equality(
    spent: float, budget: float, words: str
) -> None:
    assert words in Breach(node="a", kind="cost_usd", spent=spent, budget=budget).reason


def test_spending_exactly_the_budget_is_not_a_breach() -> None:
    state = _one(_spend((1000, 0.05), (1000, 0.05)))
    assert state.spent == (0.1, 2000)
    assert state.breach is None
    assert state.aborted is False


def test_a_node_without_a_budget_never_breaches() -> None:
    free = _mission([_node("roam")])
    spend = _status("d-tab:1", "roam", StepStatus.RUNNING, tokens=10**9, cost_usd=1e6)
    records = _stream(free, spend)
    assert _one(records).breach is None


def test_a_breach_on_one_branch_holds_the_whole_mission() -> None:
    """Abort means the mission, not the node: a ready node on another branch is held too."""
    mission = _mission(
        [PLAN, _node("water-plants"), ORDER],
        [Link(source="pick-recipes", target="order-groceries")],
    )
    records = _stream(
        mission,
        _status("d-tab:1", "pick-recipes", StepStatus.RUNNING, tokens=2500),
    )
    state = _one(records)
    assert state.ready == ("water-plants",)
    assert dispatchable(state, ON) == ()
    assert admit(state, "water-plants", Touch(), ON) == Refusal(
        node="water-plants",
        reason="aborted: node 'pick-recipes' spent 2500 tokens of its 2000 token budget",
    )


def test_the_first_breach_in_node_order_is_the_reason_and_all_are_listed() -> None:
    mission = _mission([PLAN, ORDER])
    records = _stream(
        mission,
        _status("d-tab:1", "order-groceries", StepStatus.RUNNING, tokens=4000),
        _status("d-tab:1", "pick-recipes", StepStatus.RUNNING, tokens=100, cost_usd=0.5),
    )
    state = _one(records)
    assert state.breaches == (
        Breach(node="pick-recipes", kind="cost_usd", spent=0.5, budget=0.1),
        Breach(node="order-groceries", kind="tokens", spent=4000, budget=3000),
    )
    assert state.breach == state.breaches[0]


def test_a_superseded_status_step_no_longer_counts_toward_the_budget() -> None:
    """D8: the spend is a fold over live steps, so undoing a report undoes its spend."""
    records = _spend((1500, 0.01), (1500, 0.01))
    assert _one(records).breach is not None
    undone = records + _stream(_supersede("d-tab:3"), device="phone")
    state = _one(undone)
    assert state.spent == (0.01, 1500)
    assert state.breach is None


def test_the_breach_is_a_pure_function_of_the_log() -> None:
    records = _spend((800, 0.02), (900, 0.02), (600, 0.02))
    expected = _one(records)
    shuffled = list(records)
    for seed in range(10):
        random.Random(seed).shuffle(shuffled)
        state = _one(shuffled)
        assert state.breach == expected.breach
        assert dag_hash(state) == dag_hash(expected)


def test_the_abort_step_records_the_reason_in_the_log() -> None:
    """What a runner appends on a breach: the node failed, with the reason as its note. Folded
    back in, the mission reads as failed and stays aborted."""
    records = _spend((2500, 0.02))
    state = _one(records)
    breach = state.breach
    assert breach is not None

    step = abort_step(state, breach, ts=TS)

    assert step.verb is StepVerb.STATUS
    assert step.status is StepStatus.FAILED
    assert step.node == "pick-recipes"
    assert step.mission == "d-tab:1"
    assert step.actor == ACTOR_KERNEL
    assert step.note == breach.reason
    assert step.tokens is None and step.cost_usd is None, "the abort itself spends nothing"
    assert step.caused_by == ["d-tab:2"], "the status steps whose spend crossed the line"

    spend = _status("d-tab:1", "pick-recipes", StepStatus.RUNNING, tokens=2500, cost_usd=0.02)
    after = _one(_stream(_meal_plan(), spend, step))  # appended after the spend, as a kernel would
    assert after.outcome == "failed"
    assert after.breach == breach
    assert after.spent == state.spent
    assert dispatchable(after, ON) == ()


def test_the_abort_step_round_trips_as_an_event() -> None:
    state = _one(_spend((2500, 0.02)))
    assert state.breach is not None
    step = abort_step(state, state.breach, ts=TS)
    line = json.loads(step.model_dump_json(exclude_none=True))
    assert parse_event(line) == step


# --- the node's budgets as events ---------------------------------------------------------


def test_a_node_declares_both_budgets_and_neither_is_required() -> None:
    both = _node("a", token_budget=100, cost_budget_usd=0.5)
    assert (both.token_budget, both.cost_budget_usd) == (100, 0.5)
    neither = _node("b")
    assert (neither.token_budget, neither.cost_budget_usd) == (None, None)


@pytest.mark.parametrize("value", [0, 0.0, -0.01, "free"])
def test_a_cost_budget_must_be_a_positive_number(value: Any) -> None:
    with pytest.raises(ValidationError):
        _node("a", cost_budget_usd=value)


def test_a_mission_without_cost_budgets_hashes_as_it_did_before() -> None:
    """An optional field is not a schema bump (D96): a v1 line without it loads unchanged and
    hashes the same, so every golden log before this task is untouched."""
    before = {
        "actor": "user",
        "caused_by": [],
        "links": [],
        "nodes": [
            {
                "egress": [],
                "id": "fetch-mail",
                "reads": ["principals/p-sam/log"],
                "title": "Read this week's mail",
                "token_budget": 20000,
                "writes": [],
            }
        ],
        "title": "Saturday chores",
        "ts": "2026-11-07T08:00:00.000000+05:30",
        "type": "mission",
        "v": 1,
    }
    event = parse_event(dict(before))
    assert isinstance(event, Mission)
    assert event.nodes[0].cost_budget_usd is None
    assert event.model_dump(mode="json", exclude_none=True) == before
    assert content_hash(event) == content_hash(parse_event(dict(before)))
    assert "cost_budget_usd" not in json.loads(event.model_dump_json(exclude_none=True))["nodes"][0]


# --- off by default (the owner's condition) -----------------------------------------------


def test_the_gate_refuses_everything_while_missions_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing here admits a step unless the owner has turned missions on outside the log
    (D117). A fully declared touch on a ready node is refused, and says so."""
    monkeypatch.delenv(MISSIONS_SWITCH, raising=False)
    state = _one(_stream(_meal_plan()))
    touch = Touch(reads=("principals/p-ira/notes",), egress=("grocer.example",))

    assert admit(state, "pick-recipes", touch) == Refusal(
        node="pick-recipes", reason="missions are off"
    )
    assert admit(state, "pick-recipes", touch, {}) == Refusal(
        node="pick-recipes", reason="missions are off"
    )
    assert admit(state, "pick-recipes", Touch()) is not None
    assert dispatchable(state) == ()


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on"])
def test_only_the_one_spelling_opens_the_gate(value: str) -> None:
    state = _one(_stream(_meal_plan()))
    assert admit(state, "pick-recipes", Touch(), {MISSIONS_SWITCH: value}) == Refusal(
        node="pick-recipes", reason="missions are off"
    )


def test_the_breach_is_reported_whether_missions_are_on_or_off() -> None:
    """Reading the budget is not acting on it: the fold answers either way, like ``ready``."""
    state = _one(_spend((2500, 0.0)))
    assert state.breach is not None
    assert dispatchable(state, {}) == ()
    assert dispatchable(state, ON) == ()


def test_nothing_in_the_log_can_open_the_gate() -> None:
    tree = ast.parse(MANIFEST_SOURCE.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "open" not in names and "Path" not in names
    assert "read_text" not in attrs and "exists" not in attrs and "environ" not in attrs
    assert not any(f in Node.model_fields for f in ("enabled", "on", "active", "unchecked"))


def test_the_cli_gains_no_manifest_or_budget_command() -> None:
    from walle.cli import main

    assert not any(
        word in name for name in main.commands for word in ("mission", "manifest", "budget", "step")
    )


# --- pure --------------------------------------------------------------------------------


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_manifest_module_reaches_no_model_no_network_no_file_and_no_clock() -> None:
    assert forbidden_in_closure(PACKAGE, ("manifest",)) == {}
    imports = _runtime_imports(MANIFEST_SOURCE)
    allowed = {"__future__", "dataclasses", "posixpath", "typing", "walle.events"}
    assert imports <= allowed, imports - allowed
    assert not (imports & MODEL_OR_NETWORK)
    assert "os" not in imports and "datetime" not in imports and "time" not in imports


def test_the_resolver_and_events_still_do_not_import_the_gate() -> None:
    assert "walle.manifest" not in _runtime_imports(PACKAGE / "resolver.py")
    assert "walle.manifest" not in _runtime_imports(PACKAGE / "events.py")
    assert "walle.missions" not in _runtime_imports(MANIFEST_SOURCE)


# --- the rebuild test stays green (verify) ------------------------------------------------


def test_the_golden_log_declares_both_budgets_and_records_a_breach() -> None:
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    lines = [
        json.loads(line)
        for f in sorted(log_dir(GOLDEN, expected["principal"]).glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mission = next(d for d in lines if d["type"] == "mission")
    assert any("cost_budget_usd" in n and "token_budget" in n for n in mission["nodes"])
    failed = [d for d in lines if d["type"] == "step" and d.get("status") == "failed"]
    assert len(failed) == 1 and "budget" in failed[0]["note"]


def test_the_breach_in_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    """The fixture is a kernel's Sunday as it would be written: spend reported per step, the
    budget crossed, the abort step with the fold's own reason. Three loads, one answer."""
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    principal = expected["principal"]
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))

    report = rebuild(root, principal)
    from_index = fold_missions(load_records(root, principal))
    from_log = fold_missions(
        r
        for f in sorted(log_dir(root, principal).glob("*.jsonl"))
        for r in read_file(f, f.stem).records
    )

    assert fold_missions(report.records) == from_index == from_log
    state = from_index.mission("d-tab:1")
    breach = state.breach
    assert breach == Breach(node="pick-recipes", kind="tokens", spent=2300, budget=2000)
    assert breach is not None
    assert state.outcome == "failed"
    assert state.node("pick-recipes").spent == (0.06, 2300)
    assert dispatchable(state, ON) == ()
    recorded = next(
        r.event
        for r in report.records
        if isinstance(r.event, Step) and r.event.status is StepStatus.FAILED
    )
    assert recorded.note == breach.reason
    assert dag_hash(state) == DAG_HASH


DAG_HASH = "e73976549c43de317e3bd216877b6667bebab259bc59d0bfd7b54741d7e1e9be"
