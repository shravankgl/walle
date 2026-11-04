"""Task 10's verify criterion, and the cases around it (D51, D101, D80).

  DAG order            a resume and a sidechain walk in parentUuid order, not timestamp order
  load order           the files' order on disk or in the call changes nothing
  resume               a replayed prefix is one entry, kept from the earlier file, and reported
  sidechain            a subagent's entries stay together under the entry that spawned them
  version              every walked entry carries the Claude Code build that wrote it
  unthreaded           summary and queue-operation lines have no uuid: kept aside, not walked
  shapes               clock skew, a rewind, a dangling parent, a cycle
  bad lines            skipped and reported; one bad line never loses the rest of the file
  discovery            ``<projects>/*/*.jsonl`` and ``<project>/<session>/subagents/*.jsonl``
  subagent files       a subagent's root hangs under the call that spawned it (task 11)
  ignored lines        undocumented types are not corrupt; api-request is never kept

The fixture under ``tests/fixtures/transcripts/`` is synthetic: made-up uuids, a made-up walking
trip, a made-up museum. It follows the shape D101 records from claude-code-log and
claude-code-transcripts; ``devlog/task-10.md`` lists what still needs checking against real files.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from walle.transcript import (
    LineSkip,
    TranscriptEntry,
    load,
    read_transcript,
    transcript_files,
)

PROJECTS = Path(__file__).parent / "fixtures" / "transcripts"
PROJECT = PROJECTS / "-home-example-trip"
ORIGINAL = PROJECT / "9f000000-0000-4000-8000-000000000001.jsonl"
RESUMED = PROJECT / "1a000000-0000-4000-8000-000000000002.jsonl"
S1 = "9f000000-0000-4000-8000-000000000001"
S2 = "1a000000-0000-4000-8000-000000000002"


def u(n: int) -> str:
    return f"00000000-0000-4000-8000-0000000000{n:02d}"


def short(entries: tuple[TranscriptEntry, ...]) -> list[int]:
    return [int(entry.uuid[-2:]) for entry in entries]


def row(
    n: int,
    parent: int | None,
    ts: str,
    *,
    typ: str = "user",
    session: str = "s",
    sidechain: bool = False,
    version: str = "2.0.0",
) -> dict[str, Any]:
    return {
        "parentUuid": None if parent is None else u(parent),
        "isSidechain": sidechain,
        "cwd": "/home/example/x",
        "sessionId": session,
        "version": version,
        "type": typ,
        "message": {"role": "user" if typ == "user" else "assistant", "content": "x"},
        "uuid": u(n),
        "timestamp": f"2026-10-01T{ts}Z",
    }


def write(path: Path, rows: list[dict[str, Any]], *, extra: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows) + extra)
    return path


# --- the verify criterion ----------------------------------------------------------------

# The resumed session (S2) continues from entry 4 of the original (S1). Entry 6 starts a
# background subagent (7, 9, 11, 12, all ``isSidechain``) whose timestamps interleave with
# the main thread's 8, 10 and 13.
DAG_ORDER = [1, 2, 3, 4, 5, 6, 7, 9, 11, 12, 8, 10, 13, 14]


def test_a_resume_and_a_sidechain_walk_in_dag_order_not_timestamp_order() -> None:
    transcripts = load(transcript_files(PROJECTS))
    walked = transcripts.walk()

    assert short(walked) == DAG_ORDER
    by_time = sorted(walked, key=lambda entry: entry.timestamp)
    assert short(tuple(by_time)) != DAG_ORDER  # the fixture really does disagree
    assert short(tuple(by_time)) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]

    # Every parent comes before its children, and nothing is walked twice.
    position = {entry.uuid: i for i, entry in enumerate(walked)}
    assert len(position) == len(walked)
    for entry in walked:
        if entry.parent_uuid is not None:
            assert position[entry.parent_uuid] < position[entry.uuid]


def test_the_order_files_are_given_in_changes_nothing() -> None:
    forwards = load([ORIGINAL, RESUMED])
    backwards = load([RESUMED, ORIGINAL])
    assert forwards.walk() == backwards.walk()
    assert forwards.duplicates == backwards.duplicates


# --- resume ------------------------------------------------------------------------------


def test_a_replayed_prefix_is_one_entry_kept_from_the_earlier_file() -> None:
    transcripts = load([RESUMED, ORIGINAL])
    walked = transcripts.walk()

    assert [entry.uuid for entry in walked].count(u(4)) == 1
    kept = transcripts.entries[u(4)]
    assert (kept.path, kept.line, kept.session_id) == (ORIGINAL, 4, S1)
    assert len(transcripts.duplicates) == 1
    dropped = transcripts.duplicates[0]
    assert (dropped.uuid, dropped.path, dropped.line) == (u(4), RESUMED, 1)


def test_the_session_id_changes_across_a_resume_and_the_chain_holds() -> None:
    """D101: nothing keys on the session id; the parentUuid link carries the resume."""
    transcripts = load([ORIGINAL, RESUMED])
    resumed = transcripts.entries[u(5)]
    assert resumed.parent_uuid == u(4)
    assert resumed.session_id == S2 != transcripts.entries[u(4)].session_id
    assert transcripts.roots == (u(1),)


# --- sidechain ---------------------------------------------------------------------------


def test_a_sidechain_stays_together_right_after_the_entry_that_spawned_it() -> None:
    walked = load(transcript_files(PROJECTS)).walk()
    flags = [(int(entry.uuid[-2:]), entry.is_sidechain) for entry in walked]
    start = flags.index((7, True))
    assert flags[start - 1] == (6, False)
    assert flags[start : start + 4] == [(7, True), (9, True), (11, True), (12, True)]
    assert not any(side for _, side in flags[start + 4 :])
    assert walked[start].agent_id == "agent-0001"


# --- version and the rest of the entry ---------------------------------------------------


def test_every_walked_entry_carries_its_version() -> None:
    walked = load(transcript_files(PROJECTS)).walk()
    versions = {int(entry.uuid[-2:]): entry.version for entry in walked}
    assert versions[1] == versions[4] == "2.0.1"
    assert versions[5] == versions[14] == "2.0.2"
    assert all(entry.cwd == "/home/example/trip" for entry in walked)


def test_the_raw_line_is_kept_for_the_next_part() -> None:
    entry = load([ORIGINAL]).entries[u(2)]
    assert entry.type == "assistant"
    assert entry.raw["message"]["content"][0]["text"].startswith("Day one")


def test_lines_without_a_uuid_are_kept_aside_not_walked_and_not_skipped() -> None:
    transcripts = load([ORIGINAL, RESUMED])
    kinds = sorted(item.raw["type"] for item in transcripts.unthreaded)
    assert kinds == ["queue-operation", "summary"]
    assert transcripts.skips == ()
    assert all(entry.type in {"user", "assistant"} for entry in transcripts.walk())


# --- shapes timestamps cannot express ----------------------------------------------------


def test_a_child_stamped_before_its_parent_still_walks_after_it(tmp_path: Path) -> None:
    path = write(
        tmp_path / "p" / "s.jsonl",
        [row(1, None, "10:00:00"), row(2, 1, "10:00:09"), row(3, 2, "10:00:03")],
    )
    assert short(load([path]).walk()) == [1, 2, 3]


def test_a_rewind_walks_each_branch_whole_earliest_branch_first(tmp_path: Path) -> None:
    # 2 has two children: the first attempt (3, 4) and a rewound prompt (5, 6) typed later.
    # The first branch's tail (4) is later than the second branch's head (5).
    path = write(
        tmp_path / "p" / "s.jsonl",
        [
            row(1, None, "10:00:00"),
            row(2, 1, "10:00:05", typ="assistant"),
            row(5, 2, "10:02:00"),
            row(3, 2, "10:01:00"),
            row(6, 5, "10:02:05", typ="assistant"),
            row(4, 3, "10:09:00", typ="assistant"),
        ],
    )
    assert short(load([path]).walk()) == [1, 2, 3, 4, 5, 6]


def test_a_dangling_parent_becomes_a_root_and_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write(tmp_path / "p" / "s.jsonl", [row(2, 1, "10:00:05"), row(3, 2, "10:00:09")])
    with caplog.at_level(logging.WARNING):
        transcripts = load([path])
    assert short(transcripts.walk()) == [2, 3]
    assert transcripts.roots == (u(2),)
    assert [(a.kind, a.uuid) for a in transcripts.anomalies] == [("orphan", u(2))]
    assert u(1) in caplog.text


def test_a_cycle_is_broken_every_entry_walked_once_and_reported(tmp_path: Path) -> None:
    # 1 → 2 → 3 → 1: no root at all. 4 hangs off the cycle; 5 is its own parent.
    path = write(
        tmp_path / "p" / "s.jsonl",
        [
            row(1, 3, "10:00:00"),
            row(2, 1, "10:00:01"),
            row(3, 2, "10:00:02"),
            row(4, 2, "10:00:03"),
            row(5, 5, "10:00:04"),
        ],
    )
    transcripts = load([path])
    walked = short(transcripts.walk())
    assert sorted(walked) == [1, 2, 3, 4, 5]
    assert walked == [1, 2, 3, 4, 5]
    kinds = sorted((a.kind, a.uuid) for a in transcripts.anomalies)
    assert kinds == [("cycle", u(1)), ("self-parent", u(5))]


def test_a_cycle_is_cut_on_the_ring_not_on_an_entry_hanging_off_it(tmp_path: Path) -> None:
    # 2 ↔ 3 is the ring. 1 hangs off 2 and is the earliest entry nobody can reach; cutting
    # 1 instead of the ring would report a cycle that is not there and leave 2, 3 stuck.
    path = write(
        tmp_path / "p" / "s.jsonl",
        [row(1, 2, "09:00:00"), row(2, 3, "10:00:00"), row(3, 2, "10:00:01")],
    )
    transcripts = load([path])
    assert [(a.kind, a.uuid) for a in transcripts.anomalies] == [("cycle", u(2))]
    assert transcripts.parent_of[u(1)] == u(2)
    assert short(transcripts.walk()) == [2, 1, 3]


def test_a_duplicate_that_disagrees_about_its_parent_is_reported_with_both(
    tmp_path: Path,
) -> None:
    first = write(tmp_path / "p" / "a.jsonl", [row(1, None, "10:00:00"), row(2, 1, "10:00:01")])
    second = write(
        tmp_path / "p" / "b.jsonl", [row(9, None, "10:00:00"), row(2, 9, "10:00:01", session="t")]
    )
    transcripts = load([second, first])
    assert transcripts.entries[u(2)].path == first  # same start time: the path breaks the tie
    (dropped,) = transcripts.duplicates
    assert dropped.path == second
    assert dropped.conflict is not None and u(9) in dropped.conflict


# --- bad lines (D80) ---------------------------------------------------------------------


def test_bad_lines_are_skipped_and_reported_and_the_rest_survives(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "p" / "s.jsonl"
    path.parent.mkdir()
    lines = [
        json.dumps(row(1, None, "10:00:00")).encode(),
        b'{"uuid": "00000000-0000-4000-8000-00000000',
        b"[1, 2, 3]",
        json.dumps({**row(7, 1, "10:00:02"), "uuid": 7}).encode(),
        json.dumps({**row(8, 1, "10:00:03"), "timestamp": "yesterday"}).encode(),
        json.dumps({**row(9, 1, "10:00:04"), "isSidechain": "yes"}).encode(),
        b'{"type": "user", "uuid": "\xff\xfe"}',
        json.dumps(row(2, 1, "10:00:05")).encode(),
        b"",
    ]
    path.write_bytes(b"\n".join(lines) + b"\n")
    with caplog.at_level(logging.WARNING):
        transcript = read_transcript(path)
    assert short(tuple(transcript.entries)) == [1, 2]
    assert [skip.line for skip in transcript.skips] == [2, 3, 4, 5, 6, 7]
    assert all(f"s.jsonl:{n}" in caplog.text for n in (2, 3, 4, 5, 6, 7))


def test_an_unterminated_last_line_is_pending_even_when_it_parses(tmp_path: Path) -> None:
    """Claude Code may still be writing it; the next read sees it whole."""
    tail = json.dumps(row(2, 1, "10:00:05")).encode()
    path = write(tmp_path / "p" / "s.jsonl", [row(1, None, "10:00:00")], extra=tail)
    transcript = read_transcript(path)
    assert short(tuple(transcript.entries)) == [1]
    (skip,) = transcript.skips
    assert skip.line == 2 and "unterminated" in skip.reason


def test_load_carries_every_files_skips(tmp_path: Path) -> None:
    a = write(tmp_path / "p" / "a.jsonl", [row(1, None, "10:00:00")], extra=b"not json\n")
    b = write(tmp_path / "p" / "b.jsonl", [row(2, 1, "10:00:01")], extra=b"{")
    transcripts = load([a, b])
    assert sorted((skip.path.name, skip.line) for skip in transcripts.skips) == [
        ("a.jsonl", 2),
        ("b.jsonl", 2),
    ]
    assert short(transcripts.walk()) == [1, 2]


def test_a_file_that_vanished_before_load_is_reported_and_the_rest_loads(
    tmp_path: Path,
) -> None:
    """Claude Code deletes old transcripts itself; one gone between listing and reading."""
    kept = write(tmp_path / "p" / "a.jsonl", [row(1, None, "10:00:00")])
    gone = tmp_path / "p" / "b.jsonl"
    transcripts = load([kept, gone])
    assert short(transcripts.walk()) == [1]
    (skip,) = transcripts.skips
    assert (skip.path, skip.line) == (gone, 0)
    assert "vanished" in skip.reason
    with pytest.raises(FileNotFoundError):
        read_transcript(gone)  # one file asked for by name: missing is an error


# --- discovery ---------------------------------------------------------------------------


def test_transcript_files_finds_sessions_and_their_subagents_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Changed in task 11: subagent files were left out (issue #9: that is where they live)."""
    write(tmp_path / "b-proj" / "s2.jsonl", [])
    write(tmp_path / "a-proj" / "s1.jsonl", [])
    write(tmp_path / "a-proj" / "s1" / "subagents" / "agent-1.jsonl", [])
    (tmp_path / "a-proj" / "s1" / "subagents" / "agent-1.meta.json").write_text("{}")
    write(tmp_path / "a-proj" / "s1" / "other" / "x.jsonl", [])
    write(tmp_path / "a-proj" / "s1" / "subagents" / "deeper" / "agent-2.jsonl", [])
    write(tmp_path / "top.jsonl", [])
    (tmp_path / "a-proj" / "notes.txt").write_text("x")
    (tmp_path / "a-proj" / "dir.jsonl").mkdir()
    (tmp_path / "a-proj" / "s1" / "subagents" / "dir.jsonl").mkdir()
    found = transcript_files(tmp_path)
    assert found == [
        tmp_path / "a-proj" / "s1" / "subagents" / "agent-1.jsonl",
        tmp_path / "a-proj" / "s1.jsonl",
        tmp_path / "b-proj" / "s2.jsonl",
    ]


# --- subagent files (task 11) --------------------------------------------------------------


def agent_row(n: int, parent: int | None, ts: str, agent: str) -> dict[str, Any]:
    return {**row(n, parent, ts, sidechain=True), "agentId": agent}


def spawn_result(n: int, parent: int, ts: str, agent: str) -> dict[str, Any]:
    return {**row(n, parent, ts), "toolUseResult": {"agentId": agent}}


def test_a_subagent_root_hangs_under_the_tool_result_that_names_its_agent(
    tmp_path: Path,
) -> None:
    trunk = write(
        tmp_path / "p" / "s.jsonl",
        [
            row(1, None, "10:00:00"),
            row(2, 1, "10:00:01", typ="assistant"),
            spawn_result(3, 2, "10:00:09", "ag1"),
            row(4, 3, "10:00:10", typ="assistant"),
        ],
    )
    agent = write(
        tmp_path / "p" / "s" / "subagents" / "agent-ag1.jsonl",
        [agent_row(20, None, "10:00:02", "ag1"), agent_row(21, 20, "10:00:05", "ag1")],
    )
    transcripts = load([trunk, agent])
    assert transcripts.parent_of[u(20)] == u(3)
    assert transcripts.roots == (u(1),)
    assert short(transcripts.walk()) == [1, 2, 3, 20, 21, 4]
    assert transcripts.anomalies == ()
    assert transcripts.entries[u(20)].parent_uuid is None  # as written; the graft is a view


def test_a_subagent_nobody_names_stays_a_root_and_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trunk = write(tmp_path / "p" / "s.jsonl", [row(1, None, "10:00:00")])
    agent = write(
        tmp_path / "p" / "s" / "subagents" / "agent-ag9.jsonl",
        [agent_row(20, None, "10:00:02", "ag9")],
    )
    with caplog.at_level(logging.WARNING):
        transcripts = load([trunk, agent])
    assert transcripts.roots == (u(1), u(20))
    (anomaly,) = transcripts.anomalies
    assert (anomaly.kind, anomaly.uuid) == ("unanchored-agent", u(20))
    assert "ag9" in caplog.text


def test_an_agent_is_never_anchored_inside_itself(tmp_path: Path) -> None:
    """A tool_result inside agent ag1's own file that names ag1 is not where ag1 started."""
    agent = write(
        tmp_path / "p" / "s" / "subagents" / "agent-ag1.jsonl",
        [
            agent_row(20, None, "10:00:02", "ag1"),
            {**agent_row(21, 20, "10:00:03", "ag1"), "toolUseResult": {"agentId": "ag1"}},
        ],
    )
    transcripts = load([agent])
    assert transcripts.roots == (u(20),)
    assert [a.kind for a in transcripts.anomalies] == ["unanchored-agent"]


def test_a_null_parent_outside_a_subagents_folder_is_not_grafted(tmp_path: Path) -> None:
    trunk = write(
        tmp_path / "p" / "s.jsonl",
        [row(1, None, "10:00:00"), spawn_result(2, 1, "10:00:01", "ag1")],
    )
    other = write(tmp_path / "p" / "t.jsonl", [agent_row(20, None, "10:00:02", "ag1")])
    assert load([trunk, other]).roots == (u(1), u(20))


# --- ignored lines (issue #9) --------------------------------------------------------------


def test_an_undocumented_type_that_does_not_fit_is_ignored_not_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rows = [
        row(1, None, "10:00:00"),
        {"type": "atis-latch", "uuid": u(2), "parentUuid": u(1)},  # no timestamp
        {"type": "ai-title", "aiTitle": "t"},
        row(3, 1, "10:00:02", typ="a-type-from-the-future"),
    ]
    path = write(tmp_path / "p" / "s.jsonl", rows)
    with caplog.at_level(logging.WARNING):
        transcript = read_transcript(path)
    assert transcript.skips == ()
    assert short(transcript.entries) == [1, 3]
    assert sorted(item.raw["type"] for item in transcript.unthreaded) == ["ai-title", "atis-latch"]
    assert caplog.text == ""


def test_a_user_or_assistant_line_that_does_not_fit_is_still_skipped(tmp_path: Path) -> None:
    rows = [
        row(1, None, "10:00:00"),
        {**row(2, 1, "10:00:01", typ="assistant"), "timestamp": "soon"},
    ]
    transcript = read_transcript(write(tmp_path / "p" / "s.jsonl", rows))
    assert [skip.line for skip in transcript.skips] == [2]


@pytest.mark.parametrize("typ", ["api-request", "api-request-blob"])
def test_a_raw_request_line_is_never_kept(tmp_path: Path, typ: str) -> None:
    secret = "CANARY-RAW-REQUEST-PAYLOAD"
    rows = [
        row(1, None, "10:00:00"),
        {**row(2, 1, "10:00:01", typ=typ), "request": {"body": secret}},
        {"type": typ, "blob": secret},
    ]
    transcript = read_transcript(write(tmp_path / "p" / "s.jsonl", rows))
    assert short(transcript.entries) == [1, 2]  # still in the graph, so its children hold
    kept = [dict(e.raw) for e in transcript.entries] + [dict(i.raw) for i in transcript.unthreaded]
    assert secret not in json.dumps(kept)
    assert transcript.entries[1].raw == {}
    assert len(transcript.unthreaded) == 1


def test_transcript_files_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    """No Claude Code installed yet is not an error; there is simply nothing to observe."""
    assert transcript_files(tmp_path / "absent") == []


def test_a_line_with_a_uuid_and_no_type_is_still_skipped(tmp_path: Path) -> None:
    """An unknown type is ignored; a missing one is a broken line."""
    rows = [row(1, None, "10:00:00"), {"uuid": u(2), "parentUuid": u(1)}]
    transcript = read_transcript(write(tmp_path / "p" / "s.jsonl", rows))
    assert [skip.line for skip in transcript.skips] == [2]


def test_a_skip_carries_where_the_line_is_and_none_of_its_bytes(tmp_path: Path) -> None:
    """Changed in review: no preview of transcript content, for any line (finding high 2)."""
    good = json.dumps(row(1, None, "10:00:00")).encode()
    bad = b'{"uuid": "broken'
    tail = b'{"type": "user"'
    path = tmp_path / "p" / "s.jsonl"
    path.parent.mkdir()
    data = good + b"\n" + bad + b"\n" + tail
    path.write_bytes(data)
    skips = read_transcript(path).skips
    assert {f.name for f in dataclasses.fields(LineSkip)} == {
        "path",
        "line",
        "offset",
        "length",
        "reason",
    }
    assert [(s.line, s.offset, s.length) for s in skips] == [
        (2, len(good) + 1, len(bad)),
        (3, len(good) + len(bad) + 2, len(tail)),
    ]
    for skip in skips:
        assert data[skip.offset : skip.offset + skip.length] in (bad, tail)


PAYLOAD = "CANARY-RAW-REQUEST-PAYLOAD"


def every_string(value: object) -> list[str]:
    """Every string reachable from ``value``: dataclass fields (``raw`` too), maps, lists."""
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, Path):
            found.append(str(item))
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, f.name) for f in dataclasses.fields(item))
        elif isinstance(item, dict):
            stack.extend([*item.keys(), *item.values()])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        elif isinstance(item, (list, tuple)):
            stack.extend(item)  # pyright: ignore[reportUnknownArgumentType]
    return found


@pytest.mark.parametrize("where", ["mid-file", "last line"])
def test_a_truncated_blob_line_with_its_type_last_leaves_no_payload_byte_anywhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, where: str
) -> None:
    """``type`` comes last and never arrives: nothing in the line says what it is."""
    cut = ('{"sessionId":"s","blob":"' + PAYLOAD + "-and-more").encode()
    path = tmp_path / "p" / "s" / "subagents" / "agent-x.jsonl"
    path.parent.mkdir(parents=True)
    first = json.dumps(row(1, None, "10:00:00")).encode() + b"\n"
    after = json.dumps(row(2, 1, "10:00:01")).encode() + b"\n"
    path.write_bytes(first + cut + b"\n" + after if where == "mid-file" else first + cut)
    with caplog.at_level(logging.DEBUG):
        transcript = read_transcript(path)
        transcripts = load([path])
    (skip,) = transcript.skips
    assert skip.length == len(cut)
    for text in every_string(transcript) + every_string(transcripts) + [caplog.text]:
        assert PAYLOAD not in text
        assert "and-more" not in text


# --- the subagent anchor is a trunk entry (review, low) --------------------------------------


def test_a_sidechain_entry_naming_an_agent_is_never_its_anchor(tmp_path: Path) -> None:
    trunk = write(
        tmp_path / "p" / "s.jsonl",
        [row(1, None, "10:00:00"), spawn_result(2, 1, "10:00:30", "ag2")],
    )
    first = write(
        tmp_path / "p" / "s" / "subagents" / "agent-ag1.jsonl",
        [
            agent_row(10, None, "10:00:01", "ag1"),
            {**agent_row(11, 10, "10:00:02", "ag1"), "toolUseResult": {"agentId": "ag2"}},
        ],
    )
    second = write(
        tmp_path / "p" / "s" / "subagents" / "agent-ag2.jsonl",
        [agent_row(20, None, "10:00:03", "ag2")],
    )
    transcripts = load([trunk, first, second])
    assert transcripts.parent_of[u(20)] == u(2)  # not 11, though 11 is earlier
