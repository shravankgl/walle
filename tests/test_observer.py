"""Task 11's verify criterion, and the cases around it (D56, D93, D101).

  completed turns      N completed turns give N observations, whatever streamed in between
  token deltas         fragments of a message still streaming give nothing
  pending              a turn waiting on a tool, or with no answer yet, gives nothing
  identity             a turn is found through the parentUuid graph, never by session id
  fields               observed_at is the transcript's time, ts the append time; version carried
  subagents            a subagent's file hangs under the call that started it, and adds no turn
  ignored types        only user and assistant entries are read; api-request never is
  explicit input       the projects folder is always named by the caller, never defaulted
  appending            each completed turn reaches the log once; a re-run appends nothing,
                       and two processes at once still append each turn once (D92)
  a turn's end         a turn runs to the person's next real prompt; later text is kept

The fixture under ``tests/fixtures/observer/`` is synthetic: made-up uuids, a made-up balcony
garden. Its shape follows D101 and claude-code-log's ``dev-docs/agents.md``, plus the counts
and entry types recorded on issue #9 from real files (no real content was used).
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from walle import observer
from walle.events import ACTOR_OBSERVER, Observation
from walle.observer import append_observations, observe, observe_projects
from walle.reader import read_stream
from walle.transcript import load, transcript_files
from walle.writer import StreamWriter

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


def test_the_next_prompt_ends_a_turn_even_when_its_last_reply_called_a_tool(
    tmp_path: Path,
) -> None:
    """Changed in review: a turn runs until the person's next real prompt. A call cut short
    by the person speaking again is not trailing, so it is not pending (D80)."""
    rows = [
        prompt(1, None, 0, "one"),
        reply(2, 1, 1, [tool_use("t1")], None),
        prompt(3, 2, 2, "two"),
        reply(4, 3, 3, [text("second")], "end_turn"),
    ]
    report = observe_rows(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(2)), (u(3), u(4))]
    assert report.pending == ()


# --- a turn runs to the next real prompt (review, medium) -----------------------------------


def test_a_message_split_into_entries_that_each_say_end_turn_is_read_whole(
    tmp_path: Path,
) -> None:
    """Shape 1: thinking and text are separate entries of one message, both ``end_turn``."""
    think = {"type": "thinking", "thinking": "hidden"}
    rows = [
        prompt(1, None, 0, "q"),
        reply(2, 1, 1, [think], "end_turn", mid="m"),
        reply(3, 2, 2, [text("the answer")], "end_turn", mid="m"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.closing == u(3)
    assert turn.observation.body == "user: q\nassistant: the answer"
    rows.append(prompt(4, 3, 3, "next"))
    first, *_ = observe_rows(tmp_path, rows).turns
    assert (first.closing, first.observation.body) == (u(3), "user: q\nassistant: the answer")


@pytest.mark.parametrize(
    "between",
    [
        {"isMeta": True, "message": {"role": "user", "content": "Stop hook feedback: go on"}},
        {"type": "attachment", "attachment": {"type": "file"}, "message": None},
    ],
    ids=["meta", "attachment"],
)
def test_text_after_a_final_reply_and_before_the_next_prompt_stays_in_the_turn(
    tmp_path: Path, between: dict[str, Any]
) -> None:
    """Shape 2: a final reply, a line that is not the person, then more reply."""
    middle = {**entry(3, 2, 2, "user", None), **between}
    if middle.get("message") is None:
        middle.pop("message")
    rows = [
        prompt(1, None, 0, "q"),
        reply(2, 1, 1, [text("cut off")], "max_tokens"),
        middle,
        reply(4, 3, 3, [text("and the rest")], "end_turn"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.closing == u(4)
    assert turn.observation.body == "user: q\nassistant: cut off\nassistant: and the rest"
    rows.append(prompt(5, 4, 4, "next"))
    first = observe_rows(tmp_path, rows).turns[0]
    assert first.closing == u(4)


def test_a_final_reply_followed_by_a_line_that_is_not_the_person_is_pending(
    tmp_path: Path,
) -> None:
    """More is coming: a meta line or a tool result after the reply is not the turn's end."""
    rows = [
        prompt(1, None, 0, "q"),
        reply(2, 1, 1, [text("cut off")], "end_turn"),
        prompt(3, 2, 2, "Stop hook feedback: go on", isMeta=True),
    ]
    report = observe_rows(tmp_path, rows)
    assert report.turns == () and report.pending == (u(1),)


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
    # One ``user:`` block, the second block unlabelled under it (issue #129, finding 2.4).
    assert turn.observation.body == "user: what is this?\n[image]\nassistant: A fern."


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


# --- appending (review, high 1; D92) ---------------------------------------------------------


def lines_of(writer: StreamWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in writer.path.read_bytes().splitlines()]


def test_completed_turns_are_appended_with_seqs_and_a_rerun_appends_nothing(
    tmp_path: Path,
) -> None:
    writer = StreamWriter(tmp_path / "root", "maya", "mbp")
    first = append_observations(observe_projects(PROJECTS, ts=TS), writer)
    assert [o.seq for o in first.appended] == [1, 2]
    assert first.already_logged == ()
    logged = read_stream(tmp_path / "root", "maya", "mbp").events
    assert all(isinstance(e, Observation) for e in logged)
    assert [e.body for e in logged if isinstance(e, Observation)] == [
        t.observation.body for t in first.report.turns
    ]
    before = writer.path.read_bytes()

    again = append_observations(observe_projects(PROJECTS), writer)  # a later run, a new ts
    assert again.appended == ()
    assert [t.prompt for t in again.already_logged] == [g(1), g(4)]
    assert writer.path.read_bytes() == before


def test_a_turn_that_completes_later_is_appended_by_the_later_run(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path / "root", "maya", "mbp")
    rows = [
        prompt(1, None, 0, "q1"),
        reply(2, 1, 1, [text("a1")], "end_turn"),
        prompt(3, 2, 2, "q2"),
        reply(4, 3, 3, [text("a2 so far")], None),
    ]
    path = write(tmp_path / "p" / "s.jsonl", rows)
    assert len(append_observations(observe(load([path]), ts=TS), writer).appended) == 1
    write(path, [*rows, reply(5, 4, 4, [text("done")], "end_turn")])
    later = append_observations(observe(load([path]), ts=TS), writer)
    assert [o.seq for o in later.appended] == [2]
    assert [r["body"].splitlines()[0] for r in lines_of(writer)] == ["user: q1", "user: q2"]


def test_what_is_logged_does_not_depend_on_the_session_id(tmp_path: Path) -> None:
    """Rewrite every session id between runs: nothing is appended again (D101)."""
    writer = StreamWriter(tmp_path / "root", "maya", "mbp")
    rows = conversation(3, 1)
    path = write(tmp_path / "p" / "s.jsonl", rows)
    assert len(append_observations(observe(load([path]), ts=TS), writer).appended) == 3
    write(path, [{**r, "sessionId": f"new-{i}"} for i, r in enumerate(rows)])
    assert append_observations(observe(load([path]), ts=TS), writer).appended == ()


def test_an_observation_from_another_source_does_not_count_as_logged(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path / "root", "maya", "mbp")
    (turn,) = observe_rows(tmp_path, conversation(1, 0)).turns
    writer.append(turn.observation.model_copy(update={"source": "mail"}))
    result = append_observations(observe_rows(tmp_path, conversation(1, 0)), writer)
    assert [o.seq for o in result.appended] == [2]


def test_with_nothing_completed_no_stream_file_is_created(tmp_path: Path) -> None:
    writer = StreamWriter(tmp_path / "root", "maya", "mbp")
    result = append_observations(observe_rows(tmp_path, conversation(0, 2)), writer)
    assert result.appended == () and not writer.path.exists()


# Each child observes the fixture into one stream. The fold over the log is slowed down so
# that, if it ran outside the writer's lock, both children would read an empty stream and
# both would append every turn. Under the lock the second child waits and appends nothing.
_CHILD = """
import sys, time
from pathlib import Path
import walle.observer as observer
from walle.writer import StreamWriter

projects, root = Path(sys.argv[1]), Path(sys.argv[2])
fold = observer._logged
def slow(contents):
    found = fold(contents)
    time.sleep(1.0)
    return found
observer._logged = slow
result = observer.append_observations(
    observer.observe_projects(projects), StreamWriter(root, "maya", "mbp")
)
print(len(result.appended))
"""


def test_two_processes_at_once_append_each_turn_once(tmp_path: Path) -> None:
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(PROJECTS), str(tmp_path / "root")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    counts = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err.decode()
        counts.append(int(out))
    assert sorted(counts) == [0, 2]
    records = lines_of(StreamWriter(tmp_path / "root", "maya", "mbp"))
    assert [r["seq"] for r in records] == [1, 2]
    assert len({r["body"] for r in records}) == 2


# --- audit #129: lost, repeated and mislabelled turns (D56, D80, D92) -----------------------


def write_raw(path: Path, rows: list[dict[str, Any] | str], *, tail: str = "") -> Path:
    """Rows as given, a ``str`` row verbatim (a corrupt line), then ``tail`` unterminated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    path.write_text("".join(line + "\n" for line in lines) + tail)
    return path


def observe_raw(tmp_path: Path, rows: list[dict[str, Any] | str]) -> observer.ObserveReport:
    return observe(load([write_raw(tmp_path / "p" / "s.jsonl", rows)]), ts=TS)


CORRUPT_TOOL_RESULT = '{"uuid": "' + u(3) + '", "parentUuid": "' + u(2) + '", "type": "user", CORRU'
TOOL_TURN = "user: run it\nassistant: [tool: Bash]\nassistant: Done!"


def test_a_corrupt_line_inside_a_turn_does_not_lose_the_turn(tmp_path: Path) -> None:
    """Finding 2.1: the tool result's line is torn. D80 skips it; the reply below it is still
    this turn's reply, not an orphan root nobody observes."""
    rows: list[dict[str, Any] | str] = [
        prompt(1, None, 0, "run it"),
        reply(2, 1, 1, [tool_use("t1")], "tool_use"),
        CORRUPT_TOOL_RESULT,
        reply(4, 3, 3, [text("Done!")], "end_turn"),
    ]
    report = observe_raw(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(4))]
    assert report.pending == ()
    assert report.turns[0].observation.body == TOOL_TURN
    assert [s.line for s in report.transcripts.skips] == [3]
    assert [(a.kind, a.uuid) for a in report.transcripts.anomalies] == [("orphan", u(4))]


@pytest.mark.parametrize(
    "between",
    [
        {"type": "system", "uuid": u(3), "parentUuid": u(2), "subtype": "turn_duration"},
        {**tool_result(3, 2, 2, "t1"), "timestamp": "not a time"},
    ],
    ids=["unthreaded", "skipped-but-parsed"],
)
def test_a_line_kept_aside_or_skipped_inside_a_turn_still_links_the_chain(
    tmp_path: Path, between: dict[str, Any]
) -> None:
    """Finding 2.1: the line is not an entry (no timestamp, or a bad one) but it says what
    its parent is. The chain runs through it."""
    rows: list[dict[str, Any] | str] = [
        prompt(1, None, 0, "run it"),
        reply(2, 1, 1, [tool_use("t1")], "tool_use"),
        between,
        reply(4, 3, 3, [text("Done!")], "end_turn"),
    ]
    report = observe_raw(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(4))]
    assert report.pending == ()
    assert report.turns[0].observation.body == TOOL_TURN
    assert report.transcripts.parent_of[u(4)] == u(2)
    assert [(a.kind, a.uuid) for a in report.transcripts.anomalies] == [("bridged", u(4))]


def test_a_turn_after_a_corrupt_line_is_still_its_own_turn(tmp_path: Path) -> None:
    """The repair attaches the chain, it does not merge turns: the next prompt below the
    gap still starts a new turn and closes the one above it."""
    rows: list[dict[str, Any] | str] = [
        prompt(1, None, 0, "run it"),
        reply(2, 1, 1, [tool_use("t1")], "tool_use"),
        CORRUPT_TOOL_RESULT,
        reply(4, 3, 3, [text("Done!")], "end_turn"),
        prompt(5, 4, 4, "thanks"),
        reply(6, 5, 5, [text("Any time.")], "end_turn"),
    ]
    report = observe_raw(tmp_path, rows)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(4)), (u(5), u(6))]
    assert [t.observation.body for t in report.turns] == [
        TOOL_TURN,
        "user: thanks\nassistant: Any time.",
    ]


def test_a_growing_prefix_of_one_message_is_read_once_at_its_full_length(
    tmp_path: Path,
) -> None:
    """Finding 2.2: entries of one message that each hold the text so far are snapshots of
    one block, not pieces of it. The body holds the last, and never the prefixes."""
    rows = [
        prompt(1, None, 0, "hi"),
        reply(2, 1, 1, [text("He")], None, mid="m"),
        reply(3, 2, 2, [text("Hello")], None, mid="m"),
        reply(4, 3, 3, [text("Hello, world!")], "end_turn", mid="m"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.observation.body == "user: hi\nassistant: Hello, world!"


def test_a_snapshot_before_a_tool_call_in_the_same_message_is_read_once(tmp_path: Path) -> None:
    rows = [
        prompt(1, None, 0, "list"),
        reply(2, 1, 1, [text("I will")], None, mid="m"),
        reply(3, 2, 2, [text("I will look.")], None, mid="m"),
        reply(4, 3, 3, [text("I will look."), tool_use("t1")], "tool_use", mid="m"),
        tool_result(5, 4, 4, "t1"),
        reply(6, 5, 5, [text("Two files.")], "end_turn", mid="m2"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.observation.body.splitlines() == [
        "user: list",
        "assistant: I will look.",
        "assistant: [tool: Bash]",
        "assistant: Two files.",
    ]


def test_a_prefix_in_another_message_is_not_a_snapshot(tmp_path: Path) -> None:
    """Only one message's own entries supersede each other: two replies that happen to start
    alike are two replies."""
    rows = [
        prompt(1, None, 0, "hi"),
        reply(2, 1, 1, [text("Yes")], "end_turn", mid="m1"),
        reply(3, 2, 2, [text("Yes, and no.")], "end_turn", mid="m2"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.observation.body == "user: hi\nassistant: Yes\nassistant: Yes, and no."


def _old(path: Path) -> None:
    """Make ``path`` look untouched for an hour: nothing is writing it."""
    import os

    stamp = path.stat().st_mtime - 3600
    os.utime(path, (stamp, stamp))


def test_a_final_reply_at_the_end_of_a_file_being_written_is_pending_until_the_tail_lands(
    tmp_path: Path,
) -> None:
    """Finding 2.3: the file's last line is torn and the file was just written. The reply
    above the tear says end_turn, but the tear may continue the turn, and an observation
    made now and a longer one made next run would be two events for one turn (D92)."""
    writer = StreamWriter(tmp_path / "root", "maya", "mbp")
    path = tmp_path / "p" / "s.jsonl"
    rows: list[dict[str, Any] | str] = [
        prompt(1, None, 0, "q"),
        reply(2, 1, 1, [text("a")], "end_turn"),
    ]
    more = reply(3, 2, 2, [text("and b")], "end_turn")
    write_raw(path, rows, tail=json.dumps(more)[:40])

    first = append_observations(observe(load([path]), ts=TS), writer)
    assert first.report.turns == () and first.report.pending == (u(1),)
    assert first.appended == () and not writer.path.exists()

    write_raw(path, [*rows, more])
    second = append_observations(observe(load([path]), ts=TS), writer)
    assert [(t.prompt, t.closing) for t in second.report.turns] == [(u(1), u(3))]
    assert [o.body for o in second.appended] == ["user: q\nassistant: a\nassistant: and b"]

    third = append_observations(observe(load([path])), writer)
    assert third.appended == () and len(lines_of(writer)) == 1


def test_a_torn_tail_nobody_is_writing_does_not_hold_a_finished_turn_back(tmp_path: Path) -> None:
    """Claude Code died mid-line an hour ago. The reply above the tear is the reply."""
    path = write_raw(
        tmp_path / "p" / "s.jsonl",
        [prompt(1, None, 0, "q"), reply(2, 1, 1, [text("a")], "end_turn")],
        tail='{"uuid": "x", "par',
    )
    _old(path)
    report = observe(load([path]), ts=TS)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(2))]
    assert report.pending == ()
    (skip,) = report.transcripts.skips
    assert "unterminated" in skip.reason and "may still be writing" not in skip.reason


def test_a_growing_file_holds_back_only_the_turn_its_tail_can_reach(tmp_path: Path) -> None:
    """A turn the person has already moved on from is closed by their next prompt (rule 1),
    tear or no tear. A turn in another file is not this file's business."""
    growing = write_raw(
        tmp_path / "p" / "s.jsonl",
        [
            prompt(1, None, 0, "one"),
            reply(2, 1, 1, [text("first")], "end_turn"),
            prompt(3, 2, 2, "two"),
            reply(4, 3, 3, [text("second")], "end_turn"),
            entry(5, 4, 4, "system", None, subtype="turn_duration"),
        ],
        tail='{"uuid": "x", "par',
    )
    other = write(
        tmp_path / "p" / "t.jsonl",
        [prompt(6, None, 10, "elsewhere"), reply(7, 6, 11, [text("here")], "end_turn")],
    )
    report = observe(load([growing, other]), ts=TS)
    assert [(t.prompt, t.closing) for t in report.turns] == [(u(1), u(2)), (u(6), u(7))]
    assert report.pending == (u(3),)  # the tear hangs off entry 5, inside this turn


def test_a_multi_line_prompt_with_a_second_block_is_one_user_block(tmp_path: Path) -> None:
    """Finding 2.4: the person's blocks are one ``user:`` block. A second label after an
    unlabelled line is what a forged turn looks like to ``hazards``, so the observer must
    never write one for the person's own second block."""
    from walle.hazards import flag

    blocks = [text("Line 1\nLine 2"), {"type": "image", "source": {"type": "base64", "data": "x"}}]
    rows = [
        entry(1, None, 0, "user", {"role": "user", "content": blocks}),
        reply(2, 1, 1, [text("Got it.")], "end_turn"),
    ]
    (turn,) = observe_rows(tmp_path, rows).turns
    assert turn.observation.body == "user: Line 1\nLine 2\n[image]\nassistant: Got it."
    assert flag(turn.observation.body) == ()
