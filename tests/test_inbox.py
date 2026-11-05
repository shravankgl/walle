"""Issue #70's verify criteria, and the cases around them (D63, D78).

  one result from two    two answers given at once resolve the item once: two runners race
                         for it, one wins, both are told the same result, the log holds one
                         answer; two answers that never shared a lock still fold to one
  orphans resolve        an item whose session has died is resolved ``orphaned`` by the kernel
                         and never approved
  unattended is routing  unattended changes where the person is reached and nothing else: a
                         machine actor can never answer for them, attended or not
  survives a restart     the wait is in the log: a fresh process sees the item pending, with
                         the mission step it holds, and can resolve it
  off by default         nothing writes unless the owner has turned the inbox on, outside the log

Every item here is synthetic: a made-up household, no real data and no real tool.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from test_constraints import forbidden_in_closure
from walle import inbox
from walle.events import (
    ACTOR_CURATOR,
    ACTOR_KERNEL,
    ACTOR_MCP,
    ACTOR_OBSERVER,
    ACTOR_PROPOSER,
    ACTOR_USER,
    Answer,
    AnswerOutcome,
    Ask,
    AskKind,
    Event,
    Supersede,
    now_instant,
)
from walle.index import load_records, rebuild
from walle.paths import inbox_lock_path, log_dir
from walle.reader import Record, read_file, read_stream
from walle.writer import StreamWriter

P = "lena"
D = "mbp"
ON = {inbox.INBOX_SWITCH: inbox.SWITCH_ON}
TS = "2026-11-05T09:00:00.000000+00:00"
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
INBOX_SOURCE = PACKAGE / "inbox.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v1" / "inbox"
MACHINES = (ACTOR_KERNEL, ACTOR_OBSERVER, ACTOR_PROPOSER, ACTOR_CURATOR, ACTOR_MCP)


# --- builders -----------------------------------------------------------------------------


def _ask(root: Path, kind: AskKind = AskKind.APPROVAL, **extra: Any) -> inbox.Asked:
    fields: dict[str, Any] = {
        "session": "s-7f3a",
        "tool_call_id": "toolu_01Send",
        "body": "Send the note to the school?",
    }
    fields.update(extra)
    return inbox.ask(root, P, D, kind=kind, environ=ON, **fields)


def _records(root: Path) -> list[Record]:
    log = log_dir(root, P)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _answers(root: Path) -> list[Answer]:
    return [r.event for r in _records(root) if isinstance(r.event, Answer)]


def _write(root: Path, device: str, event: Event) -> str:
    """Append bypassing the inbox: a second device's log, or a hand-written line."""
    written = StreamWriter(root, P, device).append(event)
    return f"d-{device}:{written.seq}"


def _raw_ask(**extra: Any) -> Ask:
    fields: dict[str, Any] = {
        "ts": TS,
        "actor": ACTOR_KERNEL,
        "kind": AskKind.APPROVAL,
        "session": "s-7f3a",
        "tool_call_id": "toolu_01Send",
        "body": "Send the note to the school?",
    }
    fields.update(extra)
    return Ask(**fields)


def _raw_answer(ask: str, outcome: AnswerOutcome, **extra: Any) -> Answer:
    fields: dict[str, Any] = {"ts": TS, "actor": "lena", "ask": ask, "outcome": outcome}
    fields.update(extra)
    return Answer(**fields)


# --- verify: two simultaneous answers give one result ----------------------------------------


def test_two_runners_answering_at_once_give_one_result(tmp_path: Path) -> None:
    """Runners A and B answer the same approval at the same moment, A yes and B no. One of
    them wins, both are told the winner's outcome, and the log holds exactly one answer."""
    ref = _ask(tmp_path).item.ref
    barrier = threading.Barrier(2)
    results: dict[str, inbox.Resolution] = {}
    errors: list[BaseException] = []

    def runner(name: str, outcome: AnswerOutcome, device: str) -> None:
        try:
            barrier.wait()
            results[name] = inbox.answer(
                tmp_path, P, device, ref, outcome, actor="lena", surface=name, environ=ON
            )
        except BaseException as exc:  # reported below, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=runner, args=("app", AnswerOutcome.APPROVED, "mbp")),
        threading.Thread(target=runner, args=("slack", AnswerOutcome.DENIED, "phone")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    assert sorted(r.won for r in results.values()) == [False, True]
    outcomes = {r.item.outcome for r in results.values()}
    assert len(outcomes) == 1
    assert len(_answers(tmp_path)) == 1
    assert _answers(tmp_path)[0].outcome in outcomes


_HOLD_AND_ANSWER = """
import fcntl, os, sys, time
from pathlib import Path
from walle.events import Answer, AnswerOutcome, now_instant
from walle.writer import StreamWriter

root, ref, lock = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(1.0)
StreamWriter(root, "lena", "phone").append(
    Answer(ts=now_instant(), actor="lena", ask=ref, outcome=AnswerOutcome.APPROVED, surface="app")
)
os.close(fd)
"""


def test_a_second_responder_waits_and_is_told_the_first_answer(tmp_path: Path) -> None:
    """Runner A holds inbox.lock and approves. Runner B, denying the same item, waits for A,
    reads the log only then, and is told A's answer: B writes nothing.

    Had B checked "still pending?" before waiting, it would wake holding a stale answer and
    append a deny beside A's approve."""
    ref = _ask(tmp_path).item.ref
    lock = inbox_lock_path(tmp_path, P)
    env = {**os.environ, "PYTHONPATH": str(PACKAGE.parent)}
    runner_a = subprocess.Popen(
        [sys.executable, "-c", _HOLD_AND_ANSWER, str(tmp_path), ref, str(lock)],
        stdout=subprocess.PIPE,
        env=env,
    )
    try:
        assert runner_a.stdout is not None
        assert runner_a.stdout.readline().strip() == b"held"
        started = time.monotonic()
        result = inbox.answer(tmp_path, P, D, ref, AnswerOutcome.DENIED, actor="lena", environ=ON)
        waited = time.monotonic() - started
    finally:
        runner_a.wait(timeout=10)
    assert waited >= 0.5
    assert not result.won
    assert result.item.outcome is AnswerOutcome.APPROVED
    assert result.item.answer_ref == "d-phone:1"
    assert [a.outcome for a in _answers(tmp_path)] == [AnswerOutcome.APPROVED]


def test_two_answers_that_never_shared_a_lock_still_fold_to_one(tmp_path: Path) -> None:
    """Two devices that do not share a filesystem each answered before their logs met. The
    first in ref order wins on every machine, and the other is reported, not dropped."""
    ref = _write(tmp_path, "mbp", _raw_ask())
    yes = _write(tmp_path, "phone", _raw_answer(ref, AnswerOutcome.APPROVED))
    no = _write(tmp_path, "mbp", _raw_answer(ref, AnswerOutcome.DENIED))

    state = inbox.fold_inbox(_records(tmp_path))
    item = state.item(ref)

    assert (item.outcome, item.answer_ref) == (AnswerOutcome.DENIED, no)
    assert [(u.ref, u.reason) for u in state.unapplied] == [
        (yes, f"{ref} was already resolved by {no}: the first answer wins")
    ]
    assert inbox.fold_inbox(reversed(_records(tmp_path))) == state


def test_an_item_is_resolved_once_and_a_late_answer_writes_nothing(tmp_path: Path) -> None:
    ref = _ask(tmp_path, AskKind.NOTIFICATION).item.ref
    first = inbox.answer(tmp_path, P, D, ref, AnswerOutcome.ACKNOWLEDGED, environ=ON)
    again = inbox.answer(tmp_path, P, "phone", ref, AnswerOutcome.ACKNOWLEDGED, environ=ON)
    assert first.won and not again.won
    assert again.item == first.item
    assert len(_answers(tmp_path)) == 1


# --- idempotent by (session, tool_call_id) ----------------------------------------------------


def test_asking_again_with_the_same_call_is_the_same_item(tmp_path: Path) -> None:
    first = _ask(tmp_path)
    again = _ask(tmp_path, body="Send the note to the school? (asked again after a restart)")
    assert first.new and not again.new
    assert again.item == first.item
    assert len([r for r in _records(tmp_path) if isinstance(r.event, Ask)]) == 1


def test_the_same_call_asked_as_another_kind_is_refused(tmp_path: Path) -> None:
    """One call is one item: re-asking it as a question must not hand back the approval."""
    ref = _ask(tmp_path).item.ref
    with pytest.raises(ValueError, match=rf"already asked as {ref} \(approval, mission None"):
        _ask(tmp_path, AskKind.QUESTION)
    with pytest.raises(ValueError, match="one call is one item"):
        _ask(tmp_path, mission="d-mbp:1", node="send-note")
    assert len([r for r in _records(tmp_path) if isinstance(r.event, Ask)]) == 1


def test_another_tool_call_in_the_same_session_is_another_item(tmp_path: Path) -> None:
    a = _ask(tmp_path).item
    b = _ask(tmp_path, tool_call_id="toolu_02Day").item
    assert a.ref != b.ref
    assert [i.ref for i in inbox.pending(tmp_path, P)] == [a.ref, b.ref]


def test_a_repeated_ask_in_the_log_is_reported_and_its_first_stands(tmp_path: Path) -> None:
    first = _write(tmp_path, "mbp", _raw_ask())
    repeat = _write(tmp_path, "phone", _raw_ask(body="again"))
    state = inbox.fold_inbox(_records(tmp_path))
    assert [i.ref for i in state.items] == [first]
    assert [(u.ref, u.reason) for u in state.unapplied] == [
        (repeat, f"repeats session s-7f3a, tool call toolu_01Send: the item is {first}")
    ]


# --- verify: an orphaned item resolves when its session dies ----------------------------------


def test_an_orphaned_item_resolves_when_its_session_dies(tmp_path: Path) -> None:
    dead = _ask(tmp_path, session="s-dead").item
    alive = _ask(tmp_path, session="s-alive", tool_call_id="toolu_09").item

    orphaned = inbox.orphan(tmp_path, P, D, alive={"s-alive"}, environ=ON)

    assert [i.ref for i in orphaned] == [dead.ref]
    resolved = inbox.fold_inbox(_records(tmp_path)).item(dead.ref)
    assert resolved.outcome is AnswerOutcome.ORPHANED
    assert resolved.answer is not None and resolved.answer.actor == ACTOR_KERNEL
    assert [i.ref for i in inbox.pending(tmp_path, P)] == [alive.ref]


def test_an_orphan_sweep_touches_nothing_already_resolved(tmp_path: Path) -> None:
    ref = _ask(tmp_path, session="s-dead").item.ref
    inbox.answer(tmp_path, P, D, ref, AnswerOutcome.DENIED, environ=ON)
    assert inbox.orphan(tmp_path, P, D, alive=set(), environ=ON) == ()
    assert [a.outcome for a in _answers(tmp_path)] == [AnswerOutcome.DENIED]


def test_an_orphaned_approval_can_no_longer_be_approved(tmp_path: Path) -> None:
    ref = _ask(tmp_path, session="s-dead").item.ref
    inbox.orphan(tmp_path, P, D, alive=set(), environ=ON)
    late = inbox.answer(tmp_path, P, D, ref, AnswerOutcome.APPROVED, environ=ON)
    assert not late.won
    assert late.item.outcome is AnswerOutcome.ORPHANED


def test_only_the_kernel_orphans_an_item(tmp_path: Path) -> None:
    ref = _ask(tmp_path).item.ref
    with pytest.raises(inbox.NotAnswerableError, match="only the kernel"):
        inbox.answer(tmp_path, P, D, ref, AnswerOutcome.ORPHANED, actor="lena", environ=ON)
    assert _answers(tmp_path) == []


# --- verify: unattended mode never raises autonomy ---------------------------------------------


def test_unattended_moves_where_the_person_is_reached_and_nothing_else() -> None:
    assert inbox.route(unattended=False) == "inline"
    assert inbox.route(unattended=True) == "inbox"
    for kind in AskKind:
        attended = _raw_ask(kind=kind, unattended=False)
        unattended = _raw_ask(kind=kind, unattended=True)
        for outcome in AnswerOutcome:
            value = "x" if outcome is AnswerOutcome.ANSWERED else None
            for actor in (*MACHINES, ACTOR_USER, "lena"):
                given = _raw_answer("d-mbp:1", outcome, actor=actor, value=value)
                assert inbox.misfit(attended, given) == inbox.misfit(unattended, given)


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.parametrize("actor", MACHINES)
def test_no_machine_actor_can_approve_attended_or_not(
    tmp_path: Path, actor: str, unattended: bool
) -> None:
    """The run that asked cannot answer itself, and neither can any other part of walle."""
    ref = _ask(tmp_path, unattended=unattended).item.ref
    with pytest.raises(inbox.NotAnswerableError, match="a person"):
        inbox.answer(tmp_path, P, D, ref, AnswerOutcome.APPROVED, actor=actor, environ=ON)
    assert _answers(tmp_path) == []
    assert [i.ref for i in inbox.pending(tmp_path, P)] == [ref]


@pytest.mark.parametrize("actor", MACHINES)
def test_a_self_approval_written_into_the_log_is_reported_not_applied(
    tmp_path: Path, actor: str
) -> None:
    """Even a line that bypassed ``answer`` cannot resolve the item: the fold checks too."""
    ref = _write(tmp_path, "mbp", _raw_ask(unattended=True))
    bad = _write(tmp_path, "mbp", _raw_answer(ref, AnswerOutcome.APPROVED, actor=actor))
    state = inbox.fold_inbox(_records(tmp_path))
    assert state.item(ref).status == "pending"
    assert [u.ref for u in state.unapplied] == [bad]
    assert "a person" in state.unapplied[0].reason


# --- what fits -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "outcome", "fits"),
    [
        (AskKind.APPROVAL, AnswerOutcome.APPROVED, True),
        (AskKind.APPROVAL, AnswerOutcome.DENIED, True),
        (AskKind.APPROVAL, AnswerOutcome.ANSWERED, False),
        (AskKind.APPROVAL, AnswerOutcome.ACKNOWLEDGED, False),
        (AskKind.QUESTION, AnswerOutcome.ANSWERED, True),
        (AskKind.QUESTION, AnswerOutcome.APPROVED, False),
        (AskKind.NOTIFICATION, AnswerOutcome.ACKNOWLEDGED, True),
        (AskKind.NOTIFICATION, AnswerOutcome.APPROVED, False),
    ],
)
def test_an_outcome_must_fit_the_kind_of_item(
    tmp_path: Path, kind: AskKind, outcome: AnswerOutcome, fits: bool
) -> None:
    ref = _ask(tmp_path, kind).item.ref
    value = "thursday" if outcome is AnswerOutcome.ANSWERED else None
    if fits:
        assert inbox.answer(tmp_path, P, D, ref, outcome, value=value, environ=ON).won
    else:
        with pytest.raises(inbox.NotAnswerableError, match="does not fit"):
            inbox.answer(tmp_path, P, D, ref, outcome, value=value, environ=ON)
        assert _answers(tmp_path) == []


def test_a_question_with_choices_takes_only_one_of_them(tmp_path: Path) -> None:
    ref = _ask(
        tmp_path,
        AskKind.QUESTION,
        tool_call_id="toolu_02Day",
        body="Which day suits the plumber?",
        choices=["tuesday", "thursday"],
    ).item.ref
    with pytest.raises(inbox.NotAnswerableError, match="not one of"):
        inbox.answer(tmp_path, P, D, ref, AnswerOutcome.ANSWERED, value="friday", environ=ON)
    result = inbox.answer(tmp_path, P, D, ref, AnswerOutcome.ANSWERED, value="tuesday", environ=ON)
    assert result.won and result.item.answer is not None
    assert result.item.answer.value == "tuesday"


def test_answering_what_is_not_an_item_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    ref = _ask(tmp_path).item.ref
    with pytest.raises(inbox.NotAnswerableError, match="no inbox item d-mbp:9"):
        inbox.answer(tmp_path, P, D, "d-mbp:9", AnswerOutcome.APPROVED, environ=ON)
    withdrawn = _write(tmp_path, "mbp", Supersede(ts=TS, actor=ACTOR_KERNEL, target=ref))
    with pytest.raises(inbox.NotAnswerableError, match=f"withdrawn by {withdrawn}"):
        inbox.answer(tmp_path, P, D, ref, AnswerOutcome.APPROVED, environ=ON)
    assert _answers(tmp_path) == []


def test_answering_a_principal_with_no_log_is_an_error_and_creates_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        inbox.answer(tmp_path, "nobody", D, "d-mbp:1", AnswerOutcome.APPROVED, environ=ON)
    assert not (tmp_path / "principals").exists()


def test_the_event_shapes_are_held_to_their_kind() -> None:
    with pytest.raises(ValidationError, match="only a question"):
        _raw_ask(kind=AskKind.APPROVAL, choices=["yes"])
    with pytest.raises(ValidationError, match="appears twice"):
        _raw_ask(kind=AskKind.QUESTION, choices=["a", "a"])
    with pytest.raises(ValidationError, match="without its mission"):
        _raw_ask(node="send-note")
    with pytest.raises(ValidationError, match="not a session"):
        _raw_ask(session="s 7f3a")
    with pytest.raises(ValidationError, match="needs a value"):
        _raw_answer("d-mbp:1", AnswerOutcome.ANSWERED)
    with pytest.raises(ValidationError, match="carries no value"):
        _raw_answer("d-mbp:1", AnswerOutcome.APPROVED, value="yes")


# --- verify: survives a restart ---------------------------------------------------------------

_ASK_AND_EXIT = """
import sys
from pathlib import Path
from walle import inbox
from walle.events import AskKind

asked = inbox.ask(
    Path(sys.argv[1]), "lena", "mbp", kind=AskKind.APPROVAL, session="s-7f3a",
    tool_call_id="toolu_01Send", body="Send the note to the school?", unattended=True,
    mission="d-mbp:1", node="send-note", environ={"WALLE_INBOX": "on"},
)
print(asked.item.ref, flush=True)
"""


def test_a_pending_item_survives_the_process_that_asked(tmp_path: Path) -> None:
    """The wait is in the log, not in memory: a new process sees the item, knows which
    mission step it holds, and can answer it. Destroying the index changes nothing."""
    env = {**os.environ, "PYTHONPATH": str(PACKAGE.parent)}
    done = subprocess.run(
        [sys.executable, "-c", _ASK_AND_EXIT, str(tmp_path)],
        capture_output=True,
        env=env,
        check=True,
        timeout=30,
    )
    ref = done.stdout.decode().strip()

    rebuild(tmp_path, P)
    (tmp_path / "principals" / f"p-{P}" / "index.sqlite").unlink()
    (waiting,) = inbox.pending(tmp_path, P)
    assert (waiting.ref, waiting.ask.mission, waiting.ask.node) == (ref, "d-mbp:1", "send-note")

    again = _ask(tmp_path, unattended=True, mission="d-mbp:1", node="send-note")
    assert not again.new and again.item.ref == ref
    assert inbox.answer(tmp_path, P, D, ref, AnswerOutcome.APPROVED, environ=ON).won
    assert inbox.pending(tmp_path, P) == ()


# --- the dormant rule ---------------------------------------------------------------------


def test_the_inbox_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Switch unset: nothing asks, answers or orphans, and nothing is created on disk."""
    monkeypatch.delenv(inbox.INBOX_SWITCH, raising=False)
    assert not inbox.enabled()
    with pytest.raises(inbox.InboxOffError):
        inbox.ask(tmp_path, P, D, kind=AskKind.APPROVAL, session="s", tool_call_id="t", body="b")
    with pytest.raises(inbox.InboxOffError):
        inbox.answer(tmp_path, P, D, "d-mbp:1", AnswerOutcome.APPROVED)
    with pytest.raises(inbox.InboxOffError):
        inbox.orphan(tmp_path, P, D, alive=set())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "on\n"])
def test_only_the_one_spelling_turns_the_inbox_on(value: str) -> None:
    assert not inbox.enabled({inbox.INBOX_SWITCH: value})
    assert inbox.enabled({inbox.INBOX_SWITCH: "on"})


def test_reading_the_inbox_is_not_acting_and_works_while_off(tmp_path: Path) -> None:
    ref = _ask(tmp_path).item.ref
    assert [i.ref for i in inbox.pending(tmp_path, P)] == [ref]


def test_nothing_in_the_log_can_turn_the_inbox_on() -> None:
    """The switch is the environment only: no event field turns it on."""
    for model in (Ask, Answer):
        assert not any(f in model.model_fields for f in ("enabled", "on", "active"))
    tree = ast.parse(INBOX_SOURCE.read_text())
    reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "environ" and isinstance(n.value, ast.Name)
    ]
    assert all(n.value.id == "os" for n in reads)  # type: ignore[union-attr]


def test_the_cli_gains_no_inbox_command() -> None:
    from walle.cli import main

    assert not any("inbox" in name or "answer" in name for name in main.commands)


# --- pure fold, golden log --------------------------------------------------------------------


def test_the_inbox_reaches_no_model_and_no_network() -> None:
    assert forbidden_in_closure(PACKAGE, ("inbox",)) == {}


def test_the_fold_reads_no_clock_and_opens_no_file() -> None:
    """``fold_inbox`` and ``misfit`` are pure functions of their arguments (constraint 3)."""
    tree = ast.parse(INBOX_SOURCE.read_text())
    pure = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in {"fold_inbox", "misfit"}
    ]
    assert len(pure) == 2
    for fn in pure:
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert not names & {"open", "now_instant", "time", "datetime", "os"}
        assert not attrs & {"read_text", "now", "environ"}


def test_the_inbox_fold_over_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    """The fold the shipping build saw, pinned here because ``test_golden`` pins the belief
    hash, which no inbox event moves."""
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    principal = expected["principal"]
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):  # a read-only checkout must not fail the rebuild
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))

    report = rebuild(root, principal)
    from_index = inbox.fold_inbox(load_records(root, principal))
    from_log = inbox.fold_inbox(read_stream(root, principal, "mbp").records)

    assert inbox.fold_inbox(report.records) == from_index == from_log
    assert [(i.ref, i.outcome, i.answer_ref) for i in from_index.items] == [
        ("d-mbp:2", AnswerOutcome.APPROVED, "d-mbp:4"),
        ("d-mbp:3", AnswerOutcome.ANSWERED, "d-mbp:5"),
        ("d-mbp:6", AnswerOutcome.ORPHANED, "d-mbp:7"),
        ("d-mbp:9", None, None),
    ]
    assert [u.ref for u in from_index.unapplied] == ["d-mbp:8"]


def test_now_instant_is_what_the_inbox_stamps(tmp_path: Path) -> None:
    before = now_instant()
    asked = _ask(tmp_path).item.ask
    assert before <= asked.ts <= now_instant()
    assert asked.actor == ACTOR_KERNEL
