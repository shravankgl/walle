"""Issue #121: the Builder runs a step through the whole chain, or not at all (D32, D67, D68, D72).

The audit found two seams between the Builder and the modules that govern what a step may do:

  2.1  ``build`` accepted a step whose ``writes`` named a floor path, and ``run_step`` handed a
       step to the executor without ``missions.admit``: no floor, no missions switch, no step
       order, and no look at the register of active automations.
  2.2  ``_kept`` never checked that the proposal an assertion was kept from is still live, and
       ``workflow`` checked only that the approval ref was live, not that it was an approval.

Every test here was written before the fix and failed against main at d6a6c23. The ones under
*the seams* drive the real modules together — ``builder``, ``floors``, ``missions``,
``manifest``, ``automations``, ``executor`` — so the two sides of each seam cannot drift apart
again. Nothing is written to the log on a refusal; a refusal never starts the docker client.

Every principal, path, command and sentence here is synthetic.
"""

from __future__ import annotations

import ast
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from test_builder import (
    ALL_ON,
    APPROVAL,
    FAKE_DOCKER,
    ON,
    TOKEN,
    D,
    P,
    Scripted,
    _approval,
    _calls,
    _kept,
    _lines,
    _records,
    _reply,
    _staged_ref,
    _step,
    _workspace,
)
from walle import automations, builder, executor, missions, staging
from walle.builder import (
    ACTOR_BUILDER,
    WORKFLOW_HEADER,
    Draft,
    NotApprovedError,
    Refusal,
    Workflow,
    WorkflowStep,
    approved,
    build,
    parse_workflow,
    run_step,
    workflow,
)
from walle.events import (
    Assertion,
    Mission,
    Observation,
    Proposal,
    Step,
    StepStatus,
    StepVerb,
    Supersede,
    now_instant,
)
from walle.manifest import Touch
from walle.reader import Record
from walle.writer import StreamWriter

AUTOMATIONS_ON = {automations.AUTOMATIONS_SWITCH: automations.SWITCH_ON}
MISSIONS_ON = {missions.MISSIONS_SWITCH: missions.SWITCH_ON}
TS = "2026-11-05T10:00:00+00:00"

FLOOR_WRITES = [
    "kernel/init.py",
    "shared/skills/x.md",
    "principals/p-maya/hot/rules.md",
    "principals/p-maya/grants/config.jsonl",
    "principals/p-maya/keys/secret",
    "principals/p-maya",  # above a floor: a delete or rename here takes the floor with it
    "KERNEL/Init.py",  # the floor folds case, as the filesystems walle ships on do
]

SECOND = _step(id="tidy", title="Tidy up", command=["true"], reads=["plans"], writes=[])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


_RUNS = itertools.count()


def _fake(tmp_path: Path, mode: str = "normal") -> tuple[tuple[str, ...], Path]:
    """A fresh record file per call, so each ``_run`` sees only its own docker calls."""
    record = tmp_path / f"docker-{mode}-{next(_RUNS)}.jsonl"
    return (sys.executable, str(FAKE_DOCKER), "--record", str(record), "--mode", mode), record


def _enrol(root: Path, kept: str, name: str = "meal-plan") -> automations.Entry:
    """A person adds the kept workflow to the register of active automations (D6)."""
    return automations.add(
        root, P, D, name=name, title="Sunday meal plan", workflow=kept, environ=AUTOMATIONS_ON
    )


def _two_steps(root: Path, *, enrol: bool = True) -> str:
    kept = staging.keep(root, P, D, _staged_ref(root, _reply(_step(), SECOND)))
    if enrol:
        _enrol(root, kept)
    return kept


def _run(
    root: Path,
    tmp_path: Path,
    ref: str,
    node: str,
    *,
    environ: dict[str, str] | None = None,
    mode: str = "normal",
) -> tuple[Refusal | executor.Refusal | executor.Outcome, list[list[str]]]:
    docker, record = _fake(tmp_path, mode)
    result = run_step(
        root,
        P,
        D,
        ref,
        node,
        _workspace(tmp_path),
        environ=ALL_ON if environ is None else environ,
        docker=docker,
        token=TOKEN,
    )
    return result, _calls(record)


def _events(root: Path, kind: type) -> list[Record]:
    return [r for r in _records(root) if isinstance(r.event, kind)]


def _statuses(root: Path) -> list[tuple[str, str, str]]:
    """``(mission, node, status)`` of every step event in the log, in log order."""
    out = []
    for r in _events(root, Step):
        assert isinstance(r.event, Step) and r.event.node and r.event.status
        out.append((r.event.mission, r.event.node, r.event.status.value))
    return out


def _edited_to_write(root: Path, ref: str, path: str) -> str:
    """The staged workflow at ``ref``, kept by a person with its one step's ``writes`` edited
    to ``path`` — a floor path reaches a kept body only this way, once ``build`` refuses it."""
    item = next(s for s in staging.pending(root, P) if s.ref == ref)
    data = json.loads(item.proposal.body.split("\n", 1)[1])
    data["steps"][0]["writes"] = [path]
    return staging.edit(root, P, D, ref, body=WORKFLOW_HEADER + "\n" + json.dumps(data, indent=2))


# --- 2.1: a floored write is refused where the workflow is read, in every mode ---------------


@pytest.mark.parametrize("path", FLOOR_WRITES)
def test_build_refuses_a_step_that_writes_a_floor_path(root: Path, path: str) -> None:
    """The reproduction: ``build`` turned a step writing ``kernel/init.py`` into a ``Draft``
    while ``missions.admit`` would refuse the very same step. Now the Builder refuses it the
    way the floor does (D72), without quoting the model's path, and writes nothing."""
    approval = _approval(root)
    before = _lines(root)
    model = Scripted(_reply(_step(writes=["plans/meals.md", path])))
    result = build(approval, model, environ=ON)
    assert isinstance(result, Refusal), result
    assert "floored" in result.reason and "D72" in result.reason
    assert path not in result.reason and model.calls == 1
    assert _lines(root) == before


def test_the_reproduction_from_the_audit_is_refused(root: Path) -> None:
    """Issue #121, 2.1, as written: hand-built records, a model that writes the floor."""
    reply = json.dumps(
        {
            "title": "Floor write",
            "steps": [
                {
                    "id": "s1",
                    "title": "Write floor",
                    "command": ["sh", "-c", "echo hi"],
                    "reads": [],
                    "writes": ["kernel/init.py", "principals/p-maya/hot/rules.md"],
                }
            ],
        }
    )
    p1 = Proposal(seq=1, ts=TS, actor="user", body="plan", summary="plan")
    a1 = Assertion(seq=2, ts=TS, actor="user", caused_by=["d-mbp:1"], body="plan", summary="plan")
    records = [Record("d-mbp:1", 1, p1, 1), Record("d-mbp:2", 2, a1, 2)]
    result = build(approved(records, "d-mbp:2"), Scripted(reply), environ=ON)
    assert isinstance(result, Refusal) and "floored" in result.reason


def test_a_floor_path_in_reads_is_not_a_write(root: Path) -> None:
    """The floor is under writes (D72). Declaring a read of one is the manifest's business."""
    result = build(_approval(root), Scripted(_reply(_step(reads=["kernel/init.py"]))), environ=ON)
    assert isinstance(result, Draft)


@pytest.mark.parametrize("path", FLOOR_WRITES[:4])
def test_a_kept_workflow_edited_to_write_a_floor_path_is_refused_at_every_later_stage(
    root: Path, tmp_path: Path, path: str
) -> None:
    """A person edits a staged workflow so that its step writes a floor path. Reading it back
    refuses; ``run_step`` refuses before the client is looked for; nothing is written."""
    kept = _edited_to_write(root, _staged_ref(root), path)
    _enrol(root, kept)
    last = _events(root, Assertion)[-1].event
    assert isinstance(last, Assertion) and isinstance(parse_workflow(last.body), str)
    with pytest.raises(NotApprovedError, match="floored"):
        workflow(_records(root), kept)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "floored" in result.reason
    assert calls == [] and _lines(root) == before


# --- 2.1: run_step goes through missions.admit, and only through it ---------------------------


def test_run_step_asks_missions_admit_about_the_step_it_is_about_to_run(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one door: before the executor, ``missions.admit`` is asked about this node with
    exactly what the step declares, on the mission as the log has it, in the caller's
    environment. What ``admit`` refuses, ``run_step`` refuses, in ``admit``'s words."""
    kept = _two_steps(root)
    asked: list[tuple[str, Touch, Any]] = []
    real = missions.admit

    def spy(state: missions.MissionState, node: str, touch: Touch, environ: Any) -> Any:
        asked.append((node, touch, environ))
        assert state.mission.caused_by == [kept] and state.mission.actor == ACTOR_BUILDER
        return real(state, node, touch, environ)

    monkeypatch.setattr(missions, "admit", spy)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome) and len(calls) == 1
    assert asked == [("write-plan", Touch(reads=(), writes=("plans/meals.md",)), ALL_ON)]

    monkeypatch.setattr(missions, "admit", lambda *a, **k: missions.Refusal("tidy", "no"))
    result, calls = _run(root, tmp_path, kept, "tidy")
    assert result == Refusal("no") and calls == []


def test_with_missions_off_a_kept_step_does_not_run_and_nothing_is_written(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dormant rule, at this seam: the Builder and the executor on, missions at their
    default, and the step is refused by the missions switch with no event and no process."""
    monkeypatch.delenv(missions.MISSIONS_SWITCH, raising=False)
    kept = _two_steps(root)
    before = _lines(root)
    environ = {k: v for k, v in ALL_ON.items() if k != missions.MISSIONS_SWITCH}
    result, calls = _run(root, tmp_path, kept, "write-plan", environ=environ)
    assert result == Refusal("missions are off")
    assert calls == [] and _lines(root) == before
    result, calls = _run(
        root, tmp_path, kept, "write-plan", environ=ALL_ON | {"WALLE_MISSIONS": "1"}
    )
    assert result == Refusal("missions are off") and calls == [] and _lines(root) == before


def test_with_the_executor_off_nothing_is_written_either(root: Path, tmp_path: Path) -> None:
    """Admitted by the mission, refused by the executor's own switch: the log gains no mission
    and no step, because nothing ran and nothing was about to."""
    kept = _two_steps(root)
    before = _lines(root)
    environ = {k: v for k, v in ALL_ON.items() if k != executor.EXECUTOR_SWITCH}
    result, calls = _run(root, tmp_path, kept, "write-plan", environ=environ)
    assert result == executor.Refusal("container executor is off")
    assert calls == [] and _lines(root) == before


def test_step_two_cannot_run_before_step_one(root: Path, tmp_path: Path) -> None:
    """The reproduction: step 2 ran with step 1 never run. Now the mission's order holds (D67):
    step 2 is refused as not ready, nothing is written; step 1 runs and is recorded; then
    step 2 runs; and a finished step is not run again while its mission is open."""
    kept = _two_steps(root)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "tidy")
    assert result == Refusal("not ready: waiting on write-plan")
    assert calls == [] and _lines(root) == before

    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome) and result.exit_code == 0 and len(calls) == 1
    (mission,) = _events(root, Mission)
    assert isinstance(mission.event, Mission)
    assert mission.event.actor == ACTOR_BUILDER and mission.event.caused_by == [kept]
    assert _statuses(root) == [
        (mission.ref, "write-plan", "running"),
        (mission.ref, "write-plan", "completed"),
    ]

    again, calls = _run(root, tmp_path, kept, "write-plan")
    assert again == Refusal("already completed") and calls == []

    result, calls = _run(root, tmp_path, kept, "tidy")
    assert isinstance(result, executor.Outcome) and len(calls) == 1
    assert calls[0][-1:] == ["true"]
    assert _statuses(root)[2:] == [
        (mission.ref, "tidy", "running"),
        (mission.ref, "tidy", "completed"),
    ]
    assert missions.fold_missions(_records(root)).mission(mission.ref).outcome == "done"


def test_a_done_mission_is_over_and_the_next_run_starts_a_new_one(
    root: Path, tmp_path: Path
) -> None:
    """Every Sunday is a new mission: once every step is completed the plan is spent, and the
    next ``run_step`` of step 1 appends a fresh mission rather than re-running the old one."""
    kept = _two_steps(root)
    for node in ("write-plan", "tidy"):
        result, _ = _run(root, tmp_path, kept, node)
        assert isinstance(result, executor.Outcome)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome) and len(calls) == 1
    first, second = _events(root, Mission)
    assert _statuses(root)[-2:] == [
        (second.ref, "write-plan", "running"),
        (second.ref, "write-plan", "completed"),
    ]
    folded = missions.fold_missions(_records(root))
    assert folded.mission(first.ref).outcome == "done"
    assert folded.mission(second.ref).outcome == "open"


def test_a_failed_step_is_recorded_and_holds_its_successor(root: Path, tmp_path: Path) -> None:
    """The command exits 7: the step is ``failed`` with the status and never the output, step
    2 stays held behind it, and nothing further is written on the refusal."""
    kept = _two_steps(root)
    result, calls = _run(root, tmp_path, kept, "write-plan", mode="fail-run")
    assert isinstance(result, executor.Outcome) and result.exit_code == 7 and len(calls) == 1
    (mission,) = _events(root, Mission)
    assert _statuses(root) == [
        (mission.ref, "write-plan", "running"),
        (mission.ref, "write-plan", "failed"),
    ]
    note = _events(root, Step)[-1].event
    assert isinstance(note, Step) and note.note and "7" in note.note
    assert "boom" not in (note.note or "")  # the container's words never reach the log
    before = _lines(root)
    held, calls = _run(root, tmp_path, kept, "tidy")
    assert held == Refusal("not ready: waiting on write-plan") and calls == []
    assert _lines(root) == before
    # A failed mission is spent, like a done one: the retry is a new mission (the plan is
    # never edited), and the failed one stays in the log as it ended.
    again, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(again, executor.Outcome) and len(calls) == 1
    first, second = _events(root, Mission)
    folded = missions.fold_missions(_records(root))
    assert folded.mission(first.ref).outcome == "failed"
    assert folded.mission(second.ref).outcome == "open"
    assert _statuses(root)[-2:] == [
        (second.ref, "write-plan", "running"),
        (second.ref, "write-plan", "completed"),
    ]


def test_a_client_that_cannot_start_the_container_leaves_the_step_failed(
    root: Path, tmp_path: Path
) -> None:
    """The step was about to run — ``running`` is in the log — and then the client could not
    start it. The log says ``failed`` with the executor's reason, never ``running`` forever."""
    kept = _two_steps(root)
    result, _ = _run(root, tmp_path, kept, "write-plan", mode="no-daemon")
    assert isinstance(result, executor.Refusal)
    (mission,) = _events(root, Mission)
    assert _statuses(root) == [
        (mission.ref, "write-plan", "running"),
        (mission.ref, "write-plan", "failed"),
    ]


def test_a_step_already_running_is_not_started_twice(root: Path, tmp_path: Path) -> None:
    """Writer A is mid-step: the log holds its mission and a ``running`` status. Writer B asks
    for the same step and is refused — ``admit`` lets a running node through because it is
    also the check at each tool call, but a runner starting a step is not a tool call."""
    kept = _two_steps(root)
    flow = workflow(_records(root), kept)
    writer = StreamWriter(root, P, D)
    mission = writer.append(flow.mission(now_instant()))
    assert mission.seq is not None
    mref = f"{writer.stream}:{mission.seq}"
    writer.append(
        Step(
            ts=now_instant(),
            actor=ACTOR_BUILDER,
            mission=mref,
            verb=StepVerb.STATUS,
            node="write-plan",
            status=StepStatus.RUNNING,
        )
    )
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "already running" in result.reason
    assert calls == [] and _lines(root) == before
    held, calls = _run(root, tmp_path, kept, "tidy")
    assert held == Refusal("not ready: waiting on write-plan") and calls == []


def test_a_paused_mission_holds_every_step(root: Path, tmp_path: Path) -> None:
    kept = _two_steps(root)
    result, _ = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome)
    (mission,) = _events(root, Mission)
    StreamWriter(root, P, D).append(
        Step(ts=now_instant(), actor="user", mission=mission.ref, verb=StepVerb.PAUSE)
    )
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "tidy")
    assert result == Refusal("mission is paused") and calls == [] and _lines(root) == before


def test_a_mission_in_the_log_that_is_not_the_workflow_is_refused(
    root: Path, tmp_path: Path
) -> None:
    """A live Builder mission names the kept workflow but declares another plan — a wider
    manifest, say. The runner trusts neither: it refuses and writes nothing."""
    kept = _two_steps(root)
    flow = workflow(_records(root), kept)
    wide = flow.mission(now_instant()).model_copy(deep=True)
    wide.nodes[0].writes.append("plans")
    StreamWriter(root, P, D).append(wide)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "does not match" in result.reason
    assert calls == [] and _lines(root) == before


# --- 2.1: the register of active automations -------------------------------------------------


def test_a_workflow_not_in_the_register_does_not_run(root: Path, tmp_path: Path) -> None:
    """The reproduction: a kept workflow nobody added under the WIP cap still ran. A kept
    workflow that runs is an automation (D6, hard constraint 9), so the register must hold it,
    active, bound to this workflow — an active automation bound to another one does not count."""
    kept = _two_steps(root, enrol=False)
    other = staging.keep(root, P, D, _staged_ref(root))
    _enrol(root, other, name="other-plan")
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "register" in result.reason and "D6" in result.reason
    assert calls == [] and _lines(root) == before
    _enrol(root, kept)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome) and len(calls) == 1


def test_a_retired_automation_does_not_run(root: Path, tmp_path: Path) -> None:
    kept = _two_steps(root)
    automations.retire(root, P, D, "meal-plan", environ=AUTOMATIONS_ON)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "retired" in result.reason
    assert calls == [] and _lines(root) == before


def test_an_add_bound_to_a_workflow_names_it_in_caused_by_and_the_fold_carries_it(
    root: Path,
) -> None:
    kept = _two_steps(root, enrol=False)
    entry = _enrol(root, kept)
    assert entry.caused_by == (kept,)
    register = automations.fold_automations(_records(root))
    assert register.entry(entry.ref) == entry
    added = next(r.event for r in _records(root) if r.ref == entry.ref)
    assert added.caused_by == [kept]


def test_an_add_that_swaps_keeps_both_the_workflow_and_the_retire_in_caused_by(
    root: Path,
) -> None:
    kept = _two_steps(root, enrol=False)
    for i in range(automations.WIP_CAP):
        automations.add(root, P, D, name=f"a{i}", title=f"a{i}", environ=AUTOMATIONS_ON)
    entry = automations.add(
        root, P, D, name="meal-plan", title="t", workflow=kept, retire="a0", environ=AUTOMATIONS_ON
    )
    assert entry.caused_by[0] == kept and len(entry.caused_by) == 2
    assert automations.fold_automations(_records(root)).entry(entry.ref) == entry


# --- 2.2: liveness of the proposal and of the approval ---------------------------------------


def test_an_assertion_whose_proposal_was_superseded_is_not_kept(root: Path) -> None:
    """2.2 (1): the proposal a person kept is then tombstoned. The keep stands on nothing."""
    ref = _kept(root)
    proposal = _events(root, Proposal)[0].ref
    StreamWriter(root, P, D).append(Supersede(ts=now_instant(), actor="user", target=proposal))
    with pytest.raises(NotApprovedError, match="superseded"):
        approved(_records(root), ref)


def test_a_workflow_whose_own_proposal_was_superseded_does_not_run(
    root: Path, tmp_path: Path
) -> None:
    kept = _two_steps(root)
    flow = workflow(_records(root), kept)
    StreamWriter(root, P, D).append(Supersede(ts=now_instant(), actor="user", target=flow.proposal))
    with pytest.raises(NotApprovedError, match="superseded"):
        workflow(_records(root), kept)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "superseded" in result.reason
    assert calls == [] and _lines(root) == before


def test_the_reproduction_for_2_2_is_refused(root: Path) -> None:
    """Issue #121, 2.2, as written: the Builder's proposal cites an observation as its source,
    a person keeps it, and the source is tombstoned. ``approved`` refuses the source, and now
    so does ``workflow``: what it was built from is not an approval."""
    obs = Observation(
        seq=1, ts=TS, actor="observer", source="transcript", observed_at=TS, body="user: hi"
    )
    body = builder.render(
        "d-mbp:1", "Title", [WorkflowStep("s1", "Step 1", ("echo", "hi"), (), ())]
    )
    wf_prop = Proposal(
        seq=2, ts=TS, actor="builder", caused_by=["d-mbp:1"], body=body, summary="workflow: Title"
    )
    wf_ass = Assertion(
        seq=3, ts=TS, actor="user", caused_by=["d-mbp:2"], body=body, summary="workflow: Title"
    )
    drop = Supersede(seq=4, ts=TS, actor="user", target="d-mbp:2", reason="dropped in review")
    records = [
        Record("d-mbp:1", 1, obs, 1),
        Record("d-mbp:2", 2, wf_prop, 2),
        Record("d-mbp:3", 3, wf_ass, 3),
    ]
    with pytest.raises(NotApprovedError):
        approved(records, "d-mbp:1")
    with pytest.raises(NotApprovedError, match="not an assertion"):
        workflow(records, "d-mbp:3")
    with pytest.raises(NotApprovedError, match="superseded"):
        workflow([*records, Record("d-mbp:4", 4, drop, 4)], "d-mbp:3")


def test_a_workflow_built_from_something_that_is_not_an_approval_does_not_run(
    root: Path, tmp_path: Path
) -> None:
    """The same, through the log: the source is an assertion nobody kept from a proposal."""
    writer = StreamWriter(root, P, D)
    writer.append(Assertion(ts=now_instant(), actor="user", body=APPROVAL, summary="plan"))
    body = builder.render("d-mbp:1", "Title", [WorkflowStep("s1", "Step 1", ("true",), (), ())])
    writer.append(
        Proposal(
            ts=now_instant(),
            actor=ACTOR_BUILDER,
            caused_by=["d-mbp:1"],
            body=body,
            summary="workflow: Title",
        )
    )
    kept = staging.keep(root, P, D, "d-mbp:2")
    _enrol(root, kept)
    with pytest.raises(NotApprovedError, match="from a proposal"):
        workflow(_records(root), kept)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "s1")
    assert isinstance(result, Refusal) and "from a proposal" in result.reason
    assert calls == [] and _lines(root) == before


# --- the seams: the real modules, together -----------------------------------------------------


@pytest.mark.parametrize("environ", [MISSIONS_ON, {}], ids=["on", "off"])
def test_the_builder_and_the_mission_gate_refuse_the_same_floored_workflow(
    environ: dict[str, str],
) -> None:
    """The seam the audit found: a workflow the Builder would stage became a mission
    ``missions.admit`` refused. Now both sides say no to the same step, the mission gate in
    every mode (rule 8) and the Builder wherever it reads a workflow, so they cannot drift."""
    step = WorkflowStep("s1", "Write floor", ("sh", "-c", "echo hi"), (), ("kernel/init.py",))
    flow = Workflow("d-mbp:3", "d-mbp:2", "d-mbp:1", "Floor write", (step,))
    mission = flow.mission(TS)
    state = missions.fold_missions([Record("d-mbp:4", 4, mission, 4)]).mission("d-mbp:4")
    touch = Touch(reads=step.reads, writes=step.writes)
    refused = missions.admit(state, "s1", touch, environ)
    assert refused is not None and "floored" in refused.reason and "D72" in refused.reason
    read = parse_workflow(builder.render(flow.source, flow.title, flow.steps))
    assert isinstance(read, str) and "floored" in read
    assert isinstance(
        builder._parse_reply(
            json.dumps(
                {
                    "title": "t",
                    "steps": [
                        {
                            "id": "s1",
                            "title": "t",
                            "command": ["true"],
                            "reads": [],
                            "writes": ["kernel/init.py"],
                        }
                    ],
                }
            )
        ),
        str,
    )


def test_what_a_step_declares_is_what_the_mission_admits(root: Path, tmp_path: Path) -> None:
    """``run_step`` hands ``admit`` the step's own ``reads`` and ``writes`` as the touch, so the
    manifest ``missions`` checks is the one the Builder staged and the person kept — the same
    node, through ``Workflow.mission`` — and a step that declares nothing touches nothing."""
    kept = _two_steps(root)
    flow = workflow(_records(root), kept)
    state = missions.fold_missions([Record("d-mbp:9", 9, flow.mission(TS), 9)]).mission("d-mbp:9")
    for step in flow.steps:
        touch = Touch(reads=step.reads, writes=step.writes)
        assert state.node(step.id).node.reads == list(step.reads)
        assert state.node(step.id).node.writes == list(step.writes)
        assert missions.admit(state, step.id, touch, MISSIONS_ON) in (
            None,
            missions.Refusal(step.id, "not ready: waiting on write-plan"),
        )
    result, _ = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome)


def test_the_builder_has_exactly_one_door_to_the_executor_and_it_is_behind_admit() -> None:
    """Source-level: ``run_step`` calls ``missions.admit`` and ``executor.run`` once each, and
    the admit comes first. No other function in the module names either."""
    source = (Path(__file__).parent.parent / "src" / "walle" / "builder.py").read_text()
    tree = ast.parse(source)
    where: dict[str, list[tuple[str, int]]] = {"admit": [], "run": []}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (node.func.value.id, node.func.attr)
                in {("missions", "admit"), ("executor", "run")}
            ):
                where[node.func.attr].append((fn.name, node.lineno))
    assert [f for f, _ in where["admit"]] == ["run_step"]
    assert [f for f, _ in where["run"]] == ["run_step"]
    assert where["admit"][0][1] < where["run"][0][1]
