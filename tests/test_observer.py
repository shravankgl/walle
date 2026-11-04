"""Task 11's verify criterion, and the cases around it (D56, D93, D101).

  completed turns      N completed turns give N observations, whatever streamed in between
  token deltas         fragments of a message still streaming give nothing
  pending              a turn waiting on a tool, or with no answer yet, gives nothing
  identity             a turn is found through the parentUuid graph, never by session id
  fields               observed_at is the transcript's time, ts the append time; version carried
  subagents            a subagent's file hangs under the call that started it, and adds no turn
  ignored types        only user and assistant entries are read; api-request never is
  explicit input       the projects folder is always named by the caller, never defaulted

The fixture under ``tests/fixtures/observer/`` is synthetic: made-up uuids, a made-up balcony
garden. Its shape follows D101 and claude-code-log's ``dev-docs/agents.md``, plus the counts
and entry types recorded on issue #9 from real files (no real content was used).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from walle import observer
from walle.events import ACTOR_OBSERVER, Observation
from walle.observer import observe, observe_projects
from walle.transcript import load, transcript_files

PROJECTS = Path(__file__).parent / "fixtures" / "observer"
PROJECT = PROJECTS / "-home-example-garden"
TRUNK = PROJECT / "b1000000-0000-4000-8000-00000000000b.jsonl"
AGENT_FILE = (
    PROJECT / "b1000000-0000-4000-8000-00000000000b" / "subagents" / "agent-a7c0ffee00000001.jsonl"
)
CANARY = "CANARY-RAW-REQUEST-PAYLOAD"
TS = "2026-11-04T08:00:00.000000+00:00"


def g(n: int) -> str:
    """A uuid in the garden fixture's trunk."""
    return f"0b000000-0000-4000-8000-0000000000{n:02d}"


def u(n: int) -> str:
    return f"00000000-0000-4000-8000-0000{n:08d}"


def entry(
    n: int,
    parent: int | None,
    second: int,
    typ: str,
    message: dict[str, Any] | None,
    *,
    session: str = "s",
    version: str = "2.0.0",
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "parentUuid": None if parent is None else u(parent),
        "isSidechain": False,
        "cwd": "/home/example/x",
        "sessionId": session,
        "version": version,
        "type": typ,
        "uuid": u(n),
        "timestamp": f"2026-10-01T10:{second // 60:02d}:{second % 60:02d}Z",
    }
    if message is not None:
        row["message"] = message
    row.update(extra)
    return row


def prompt(n: int, parent: int | None, second: int, text: str, **kw: Any) -> dict[str, Any]:
    return entry(n, parent, second, "user", {"role": "user", "content": text}, **kw)


def reply(
    n: int,
    parent: int,
    second: int,
    blocks: list[dict[str, Any]],
    stop: str | None,
    *,
    mid: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    message = {
        "id": mid or f"msg_{n}",
        "role": "assistant",
        "content": blocks,
        "stop_reason": stop,
    }
    return entry(n, parent, second, "assistant", message, **kw)


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str = "Bash") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": "ls"}}


def tool_result(n: int, parent: int, second: int, tool_id: str, **kw: Any) -> dict[str, Any]:
    content = [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]
    return entry(n, parent, second, "user", {"role": "user", "content": content}, **kw)


def write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def observe_rows(tmp_path: Path, rows: list[dict[str, Any]]) -> observer.ObserveReport:
    return observe(load([write(tmp_path / "p" / "s.jsonl", rows)]), ts=TS)


def conversation(completed: int, deltas: int) -> list[dict[str, Any]]:
    """``completed`` finished turns, each answer streamed as ``deltas`` fragments first, and a
    last turn still streaming. A fragment is an assistant entry with no stop_reason yet."""
    rows: list[dict[str, Any]] = []
    n = 0
    parent: int | None = None

    def add(row: dict[str, Any]) -> int:
        nonlocal n
        rows.append(row)
        return n

    for turn in range(completed + 1):
        n += 1
        parent = add(prompt(n, parent, n, f"question {turn}", session=f"s{turn}"))
        for piece in range(deltas):
            n += 1
            parent = add(reply(n, parent, n, [text(f"part {piece}")], None, mid=f"m{turn}"))
        if turn < completed:
            n += 1
            parent = add(reply(n, parent, n, [text("done")], "end_turn", mid=f"m{turn}"))
    return rows


# --- the verify criterion ----------------------------------------------------------------


@pytest.mark.parametrize("completed", [0, 1, 3, 7])
@pytest.mark.parametrize("deltas", [0, 1, 5])
def test_n_completed_turns_give_n_observations_and_token_deltas_give_nothing(
    tmp_path: Path, completed: int, deltas: int
) -> None:
    report = observe_rows(tmp_path, conversation(completed, deltas))
    assert len(report.turns) == completed
    assert all(isinstance(turn.observation, Observation) for turn in report.turns)
    assert len(report.pending) == 1  # the last turn, still streaming
    bodies = [turn.observation.body for turn in report.turns]
    pieces = "".join(f"assistant: part {p}\n" for p in range(deltas))
    assert bodies == [f"user: question {i}\n{pieces}assistant: done" for i in range(completed)]


def test_the_fixture_has_two_completed_turns_and_one_still_streaming() -> None:
    report = observe_projects(PROJECTS, ts=TS)
    assert [(t.prompt, t.closing) for t in report.turns] == [(g(1), g(3)), (g(4), g(8))]
    assert report.pending == (g(9),)
    assert [t.observation.body for t in report.turns] == [
        "user: Which herbs grow well on a north-facing balcony?\n"
        "assistant: Mint, parsley and chives cope with shade.",
        "user: Plan a watering schedule for them.\n"
        "assistant: [tool: Task]\n"
        "assistant: Water every second morning; mint daily in July.",
    ]


def test_fragments_alone_never_become_an_observation(tmp_path: Path) -> None:
    rows = [
        prompt(1, None, 0, "hello"),
        reply(2, 1, 1, [text("Hel")], None, mid="m"),
        reply(3, 2, 2, [text("lo th")], None, mid="m"),
        reply(4, 3, 3, [text("ere")], None, mid="m"),
    ]
    assert observe_rows(tmp_path, rows).turns == ()
    rows.append(reply(5, 4, 4, [text(".")], "end_turn", mid="m"))
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.closing == u(5)
    assert turn.observation.body.splitlines() == [
        "user: hello",
        "assistant: Hel",
        "assistant: lo th",
        "assistant: ere",
        "assistant: .",
    ]


def test_a_block_repeated_in_a_later_fragment_of_one_message_is_read_once(
    tmp_path: Path,
) -> None:
    rows = [
        prompt(1, None, 0, "hi"),
        reply(2, 1, 1, [text("Hi.")], None, mid="m"),
        reply(3, 2, 2, [text("Hi."), text("Bye.")], "end_turn", mid="m"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.observation.body == "user: hi\nassistant: Hi.\nassistant: Bye."


# --- pending -----------------------------------------------------------------------------


def test_a_turn_waiting_on_a_tool_result_is_pending_not_observed(tmp_path: Path) -> None:
    """D80: trailing calls without results are pending, not corrupt."""
    rows = [prompt(1, None, 0, "list files"), reply(2, 1, 1, [tool_use("t1")], "tool_use")]
    report = observe_rows(tmp_path, rows)
    assert report.turns == () and report.pending == (u(1),)
    rows += [tool_result(3, 2, 2, "t1"), reply(4, 3, 3, [text("Two files.")], "end_turn")]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.closing == u(4)
    assert (
        turn.observation.body == "user: list files\nassistant: [tool: Bash]\nassistant: Two files."
    )


def test_a_prompt_with_no_answer_is_pending(tmp_path: Path) -> None:
    report = observe_rows(tmp_path, [prompt(1, None, 0, "anyone there?")])
    assert report.turns == () and report.pending == (u(1),)


def test_with_no_stop_reason_the_next_prompt_closes_the_turn_before_it(tmp_path: Path) -> None:
    """A build that never writes a stop_reason still has turns: the person spoke again."""
    rows = [
        prompt(1, None, 0, "one"),
        reply(2, 1, 1, [text("first")], None),
        prompt(3, 2, 2, "two"),
        reply(4, 3, 3, [text("second")], None),
    ]
    report = observe_rows(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(2))]
    assert report.pending == (u(3),)


def test_a_next_prompt_does_not_close_a_turn_whose_last_reply_calls_a_tool(
    tmp_path: Path,
) -> None:
    rows = [
        prompt(1, None, 0, "one"),
        reply(2, 1, 1, [tool_use("t1")], None),
        prompt(3, 2, 2, "two"),
        reply(4, 3, 3, [text("second")], "end_turn"),
    ]
    report = observe_rows(tmp_path, rows)
    assert [t.prompt for t in report.turns] == [u(3)]
    assert report.pending == (u(1),)


@pytest.mark.parametrize("stop", ["tool_use", "pause_turn", "a_reason_from_the_future"])
def test_only_a_known_final_stop_reason_completes_a_turn(tmp_path: Path, stop: str) -> None:
    rows = [prompt(1, None, 0, "q"), reply(2, 1, 1, [text("a")], stop)]
    assert observe_rows(tmp_path, rows).turns == ()


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence", "max_tokens", "refusal"])
def test_a_final_stop_reason_completes_a_turn(tmp_path: Path, stop: str) -> None:
    rows = [prompt(1, None, 0, "q"), reply(2, 1, 1, [text("a")], stop)]
    assert len(observe_rows(tmp_path, rows).turns) == 1


# --- identity: the graph, never the session id (D101) --------------------------------------


def test_a_session_id_that_changes_on_every_entry_does_not_split_a_turn(tmp_path: Path) -> None:
    rows = [
        prompt(1, None, 0, "q", session="a"),
        reply(2, 1, 1, [tool_use("t1")], "tool_use", session="b"),
        tool_result(3, 2, 2, "t1", session="c"),
        reply(4, 3, 3, [text("a")], "end_turn", session="d"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert (turn.prompt, turn.closing) == (u(1), u(4))


def test_two_turns_sharing_one_session_id_are_two_observations(tmp_path: Path) -> None:
    rows = [
        prompt(1, None, 0, "q1", session="same"),
        reply(2, 1, 1, [text("a1")], "end_turn", session="same"),
        prompt(3, 2, 2, "q2", session="same"),
        reply(4, 3, 3, [text("a2")], "end_turn", session="same"),
    ]
    assert [t.prompt for t in observe_rows(tmp_path, rows).turns] == [u(1), u(3)]


def test_a_turn_split_across_a_resume_is_one_observation(tmp_path: Path) -> None:
    """The prompt is in the first file; the answer is in the resume, under a new session id."""
    first = write(tmp_path / "p" / "b-first.jsonl", [prompt(1, None, 0, "q", session="one")])
    resumed = write(
        tmp_path / "p" / "a-resumed.jsonl",
        [
            prompt(1, None, 0, "q", session="two"),  # a replayed copy
            reply(2, 1, 60, [text("a")], "end_turn", session="two"),
        ],
    )
    (turn,) = observe(load([first, resumed]), ts=TS).turns
    assert (turn.prompt, turn.closing) == (u(1), u(2))


def test_a_rewound_prompt_is_its_own_turn(tmp_path: Path) -> None:
    """Editing a prompt forks at its parent: two prompts, two answers, two turns."""
    rows = [
        prompt(1, None, 0, "q1"),
        reply(2, 1, 1, [text("a1")], "end_turn"),
        prompt(3, 2, 2, "first try"),
        reply(4, 3, 3, [text("a")], "end_turn"),
        prompt(5, 2, 4, "second try"),
        reply(6, 5, 5, [text("b")], "end_turn"),
    ]
    assert [t.prompt for t in observe_rows(tmp_path, rows).turns] == [u(1), u(3), u(5)]


def test_what_counts_as_a_prompt(tmp_path: Path) -> None:
    """A tool result, a meta line and a sidechain line are not the person speaking."""
    rows = [
        prompt(1, None, 0, "q"),
        reply(2, 1, 1, [tool_use("t1")], "tool_use"),
        tool_result(3, 2, 2, "t1"),
        prompt(4, 3, 3, "Caveat: generated by a local command", isMeta=True),
        prompt(5, 4, 4, "a subagent's instructions", isSidechain=True),
        reply(6, 4, 5, [text("a")], "end_turn"),
    ]
    report = observe_rows(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(6))]
    assert report.pending == ()


def test_prompt_content_as_blocks_reads_text_and_marks_images(tmp_path: Path) -> None:
    blocks = [text("what is this?"), {"type": "image", "source": {"type": "base64", "data": "x"}}]
    rows = [
        entry(1, None, 0, "user", {"role": "user", "content": blocks}),
        reply(2, 1, 1, [{"type": "thinking", "thinking": "hidden"}, text("A fern.")], "end_turn"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.observation.body == "user: what is this?\nuser: [image]\nassistant: A fern."


# --- the fields on each observation (D93, D101) ---------------------------------------------


def test_each_observation_carries_observed_at_ts_and_version(tmp_path: Path) -> None:
    rows = [
        prompt(1, None, 0, "q", version="2.0.9"),
        reply(2, 1, 75, [text("a")], "end_turn", version="2.1.0"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    obs = turn.observation
    assert obs.observed_at == "2026-10-01T10:01:15.000000+00:00"  # the closing entry's time
    assert obs.ts == TS  # when the log learns it, not when it happened
    assert obs.source_version == "2.1.0"  # the build that wrote the closing entry
    assert obs.source == "transcript"
    assert obs.actor == ACTOR_OBSERVER
    assert obs.seq is None  # the writer assigns it
    assert obs.caused_by == []


def test_ts_defaults_to_now_once_for_the_whole_run(tmp_path: Path) -> None:
    report = observe(load([write(tmp_path / "p" / "s.jsonl", conversation(3, 0))]))
    stamps = {t.observation.ts for t in report.turns}
    assert len(stamps) == 1
    Observation.model_validate({**report.turns[0].observation.model_dump(), "ts": stamps.pop()})


def test_observations_come_in_walk_order(tmp_path: Path) -> None:
    rows = conversation(4, 1)
    rows.reverse()  # the order on disk says nothing
    report = observe_rows(tmp_path, rows)
    assert [t.observation.body.splitlines()[0] for t in report.turns] == [
        f"user: question {i}" for i in range(4)
    ]


# --- subagents --------------------------------------------------------------------------


def test_a_subagent_file_is_read_and_hangs_under_the_call_that_started_it() -> None:
    files = transcript_files(PROJECTS)
    assert AGENT_FILE in files
    transcripts = load(files)
    agent_root = "0c000000-0000-4000-8000-000000000001"
    assert transcripts.parent_of[agent_root] == g(7)  # the Task tool_result
    walked = [entry.uuid for entry in transcripts.walk()]
    assert walked.index(g(7)) < walked.index(agent_root) < walked.index(g(8))
    assert not [a for a in transcripts.anomalies if a.uuid == agent_root]


def test_a_subagent_adds_no_turn_of_its_own() -> None:
    """Its prompt is the main agent's instructions, not the person; it is work the turn did."""
    with_agent = observe_projects(PROJECTS, ts=TS)
    without = observe(load([TRUNK]), ts=TS)
    assert [t.observation for t in with_agent.turns] == [t.observation for t in without.turns]


# --- ignored entry types (issue #9) --------------------------------------------------------


def test_no_raw_request_payload_reaches_an_observation_or_the_loaded_lines() -> None:
    report = observe_projects(PROJECTS, ts=TS)
    for turn in report.turns:
        assert CANARY not in turn.observation.model_dump_json()
    transcripts = report.transcripts
    held = [dict(e.raw) for e in transcripts.entries.values()]
    held += [dict(item.raw) for item in transcripts.unthreaded]
    assert CANARY not in json.dumps(held)
    assert TRUNK.read_text().count(CANARY) == 2  # the fixture does carry it


def test_unknown_and_undocumented_types_are_ignored_not_skipped(tmp_path: Path) -> None:
    rows = [
        prompt(1, None, 0, "q"),
        entry(2, 1, 1, "attachment", None, attachment={"type": "file"}),
        entry(3, 2, 2, "a-type-from-the-future", {"role": "user", "content": "not a prompt"}),
        reply(4, 3, 3, [text("a")], "end_turn"),
        {"type": "another-future-type", "uuid": u(5), "parentUuid": u(4)},  # no timestamp
        {"type": "mode", "mode": "normal"},
    ]
    report = observe_rows(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(4))]
    assert report.transcripts.skips == ()


def test_the_fixture_reads_with_no_skips_and_no_anomalies() -> None:
    report = observe_projects(PROJECTS, ts=TS)
    assert report.transcripts.skips == ()
    assert report.transcripts.anomalies == ()


# --- explicit input (D40) ----------------------------------------------------------------


def test_the_projects_folder_is_required_and_has_no_default() -> None:
    for fn in (observe_projects, transcript_files):
        param = inspect.signature(fn).parameters["projects_dir"]
        assert param.default is inspect.Parameter.empty


def test_no_observer_module_names_a_home_folder() -> None:
    """walle only reads a folder the person names (D40): no ``~``, no home lookup."""
    import walle.transcript

    for module in (observer, walle.transcript):
        source = inspect.getsource(module)
        for needle in ("expanduser", "Path.home", "os.environ", "getenv", 'Path("~'):
            assert needle not in source, f"{module.__name__} uses {needle}"
