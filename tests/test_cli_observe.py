"""Issue #40's verify criterion for `walle observe`, and the cases around it (D40, D56, D92, D101).

  first and second run   a fixture folder appends N observations, then 0 (D92)
  explicit input         a missing --projects-dir is refused; it has no default (D40)
  counts only            the output is count lines; no transcript text, on stdout or stderr
  two at once            two concurrent runs append each turn once
  real shapes            a ~180,000-character turn and 834 duplicate entries from resumed
                         sessions each give one observation per turn (D101)

Every transcript here is synthetic. The ones built in this file use made-up uuids, made-up
text and marker strings (``SECRET-…``) that must never reach the output. No test depends on
running as root or as any particular user: nothing changes a file's permissions.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

from walle.cli import main
from walle.paths import stream_path

PROJECTS = Path(__file__).parent / "fixtures" / "observer"
SECRET = "SECRET-TRANSCRIPT-TEXT"

# What a line of `walle observe` output may be: a heading naming the principal and stream, or
# one count. Anything else (a path, a uuid, a word of a transcript) fails the counts-only test.
_OUTPUT_LINE = re.compile(
    r"(?:[a-z0-9][a-z0-9-]*: observed transcripts into d-[a-z0-9][a-z0-9-]*"
    r"|  (?:turns seen|appended|already logged|pending|skipped lines|duplicate entries): \d+)"
)


_RUN = "from walle.cli import main; main()"


def observe(root: Path, projects: Path, *extra: str, device: str = "mbp") -> Result:
    args = ["observe", "--root", str(root), "--principal", "maya", "--device", device]
    return CliRunner().invoke(main, [*args, "--projects-dir", str(projects), *extra])


def counts(result: Result) -> dict[str, int]:
    found: dict[str, int] = {}
    for line in result.stdout.splitlines():
        name, _, value = line.strip().rpartition(": ")
        if value.isdigit():
            found[name] = int(value)
    return found


def stream_lines(root: Path) -> list[dict[str, Any]]:
    path = stream_path(root, "maya", "mbp")
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- building synthetic transcripts ------------------------------------------------------


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def row(
    n: int, parent: int | None, typ: str, message: dict[str, Any], *, session: str = "s1"
) -> dict[str, Any]:
    return {
        "parentUuid": None if parent is None else uid(parent),
        "isSidechain": False,
        "cwd": f"/home/example/{SECRET}-cwd",
        "sessionId": session,
        "version": "2.1.0",
        "type": typ,
        "uuid": uid(n),
        "timestamp": f"2026-10-01T{n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z",
        "message": message,
    }


def ask(n: int, parent: int | None, words: str, **kw: Any) -> dict[str, Any]:
    return row(n, parent, "user", {"role": "user", "content": words}, **kw)


def answer(n: int, parent: int, words: str, stop: str | None = "end_turn", **kw: Any):
    message = {
        "id": f"msg_{n}",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": f"{SECRET} thinking {n}"},
            {"type": "text", "text": words},
            {"type": "tool_use", "id": f"t{n}", "name": f"{SECRET}Tool", "input": {"x": SECRET}},
        ],
        "stop_reason": stop,
    }
    return row(n, parent, "assistant", message, **kw)


def turns(count: int, *, start: int = 1, parent: int | None = None, **kw: Any):
    """``count`` completed turns in a chain, two entries each, with secret text in both."""
    rows: list[dict[str, Any]] = []
    n = start
    for turn in range(count):
        rows.append(ask(n, parent, f"{SECRET} question {n} turn {turn}", **kw))
        rows.append(answer(n + 1, n, f"{SECRET} answer {n + 1}", **kw))
        parent, n = n + 1, n + 2
    return rows


def write(path: Path, rows: list[dict[str, Any]], *, raw: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + raw)
    return path


# --- the verify criterion ----------------------------------------------------------------


def test_the_fixture_folder_appends_its_turns_on_the_first_run_and_0_on_the_second(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    first = observe(root, PROJECTS)
    assert first.exit_code == 0, first.output
    # The garden fixture: two completed turns and one still streaming (task 11).
    assert counts(first) == {
        "turns seen": 3,
        "appended": 2,
        "already logged": 0,
        "pending": 1,
        "skipped lines": 0,
        "duplicate entries": 0,
    }
    records = stream_lines(root)
    assert [(r["type"], r["seq"], r["source"]) for r in records] == [
        ("observation", 1, "transcript"),
        ("observation", 2, "transcript"),
    ]
    before = stream_path(root, "maya", "mbp").read_bytes()

    second = observe(root, PROJECTS)
    assert second.exit_code == 0, second.output
    assert counts(second)["appended"] == 0
    assert counts(second)["already logged"] == 2
    assert counts(second)["pending"] == 1
    assert stream_path(root, "maya", "mbp").read_bytes() == before


@pytest.mark.parametrize("n", [0, 1, 4])
def test_n_completed_turns_append_n_then_0(tmp_path: Path, n: int) -> None:
    projects = tmp_path / "projects"
    write(projects / "-home-example-x" / "s1.jsonl", turns(n))
    root = tmp_path / "root"
    first, second = observe(root, projects), observe(root, projects)
    assert first.exit_code == 0 and second.exit_code == 0, first.output + second.output
    assert counts(first)["appended"] == n
    assert counts(second)["appended"] == 0
    assert counts(second)["already logged"] == n
    if n == 0:  # nothing to append creates no stream (append_observations)
        assert not stream_path(root, "maya", "mbp").exists()
    else:
        assert [r["seq"] for r in stream_lines(root)] == list(range(1, n + 1))


def test_a_missing_projects_dir_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    args = ["observe", "--root", str(root), "--principal", "maya", "--device", "mbp"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2
    assert "--projects-dir" in result.output
    assert not root.exists()


def test_the_projects_dir_option_has_no_default() -> None:
    command = main.commands["observe"]
    option = next(p for p in command.params if p.name == "projects_dir")
    assert option.required
    # Click 8.3 marks "no default" with a sentinel rather than None; a string or a Path here
    # would be a default folder walle reads without being told to.
    assert not isinstance(option.get_default(click.Context(command)), str | Path)
    assert option.envvar is None  # not from the environment either: the person names it


def test_a_projects_dir_that_does_not_exist_is_refused_not_read_as_empty(tmp_path: Path) -> None:
    result = observe(tmp_path / "root", tmp_path / "no-such-folder")
    assert result.exit_code == 2
    assert "--projects-dir" in result.output and "does not exist" in result.output
    assert not (tmp_path / "root").exists()


def test_the_output_is_counts_and_holds_no_transcript_text(tmp_path: Path) -> None:
    """Everything that could carry text is in here: prompts, replies, thinking, tool names and
    inputs, the cwd, the folder name, a line that is skipped, an orphan, a duplicate that
    disagrees about its parent (each of which transcript.py logs a warning for)."""
    projects = tmp_path / "projects"
    project = projects / f"-home-example-{SECRET}"
    rows = turns(3)
    rows.append(ask(50, None, f"{SECRET} pending question"))  # no answer yet: pending
    orphan = answer(60, 59, f"{SECRET} orphan")  # parent never loaded
    write(project / "s1.jsonl", [*rows, orphan], raw=f'{{"type": "user", "{SECRET}": \n')
    clash = dict(rows[1], parentUuid=uid(99))  # a copy that disagrees about its parent
    write(project / "s2.jsonl", [clash])

    # A real process, not CliRunner: under pytest the root logger has handlers, so a
    # warning that would reach a person's terminal through logging's last resort is hidden.
    args = ["observe", "--root", str(tmp_path / "root"), "--principal", "maya"]
    args += ["--device", "mbp", "--projects-dir", str(projects)]
    result = subprocess.run(
        [sys.executable, "-c", _RUN, *args], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    assert result.stderr == ""
    for line in result.stdout.splitlines():
        assert _OUTPUT_LINE.fullmatch(line), line
    found = {
        name: int(value)
        for name, _, value in (x.strip().rpartition(": ") for x in result.stdout.splitlines())
        if value.isdigit()
    }
    assert found["skipped lines"] == 1
    assert found["duplicate entries"] == 1
    assert found["pending"] == 1
    # The text is in the log, where it belongs: only the output is counts.
    assert any(SECRET in r["body"] for r in stream_lines(tmp_path / "root"))


def test_a_refused_stream_is_reported_without_its_contents(tmp_path: Path) -> None:
    """The stream holds transcript text. A line the reader refuses names its file and line,
    and none of what the line held: pydantic's own message would quote it."""
    projects = tmp_path / "projects"
    write(projects / "-home-example-x" / "s1.jsonl", turns(1))
    root = tmp_path / "root"
    assert observe(root, projects).exit_code == 0
    path = stream_path(root, "maya", "mbp")
    record = json.loads(path.read_text())
    record["note"] = f"{SECRET} in an unknown field"
    path.write_text(json.dumps(record) + "\n")

    result = observe(root, projects)
    assert result.exit_code == 1
    assert SECRET not in result.output
    assert f"{path}:1" in result.output
    assert "note" in result.output  # which field is wrong is said; what it held is not


def test_a_bad_principal_or_device_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    result = observe(tmp_path / "root", PROJECTS, device="../elsewhere")
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert not (tmp_path / "root").exists()


# --- two runs at once --------------------------------------------------------------------

# Each child runs `walle observe` on the fixture into one stream. The fold over the log is
# slowed down so that, if it ran outside the stream's lock, both children would read an empty
# stream and both would append every turn. Under the lock, the second waits and appends 0.
_CHILD = """
import sys, time
import walle.observer as observer
from walle.cli import main

fold = observer._logged
def slow(contents):
    found = fold(contents)
    time.sleep(1.0)
    return found
observer._logged = slow
main(sys.argv[1:])
"""


def test_two_concurrent_runs_append_each_turn_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    args = ["observe", "--root", str(root), "--principal", "maya", "--device", "mbp"]
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, *args, "--projects-dir", str(PROJECTS)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    appended = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        line = next(x for x in out.splitlines() if x.strip().startswith("appended:"))
        appended.append(int(line.rpartition(": ")[2]))
    assert sorted(appended) == [0, 2]
    records = stream_lines(root)
    assert [r["seq"] for r in records] == [1, 2]
    assert len({r["body"] for r in records}) == 2


# --- what real folders hold --------------------------------------------------------------


def test_a_turn_of_180000_characters_is_appended_once(tmp_path: Path) -> None:
    """One real body is about 180,000 characters: past the writer's 64 KiB tail window, so
    the next append has to find the last line by widening its read (writer._tail)."""
    long_text = ("lorem ipsum " * 15_000)[:180_000]
    rows = [ask(1, None, "a short question"), answer(2, 1, long_text)]
    rows += turns(1, start=3, parent=2)  # a normal turn after it, appended after the long one
    projects = tmp_path / "projects"
    write(projects / "-home-example-x" / "s1.jsonl", rows)
    root = tmp_path / "root"

    first = observe(root, projects)
    assert first.exit_code == 0, first.output
    assert counts(first)["appended"] == 2
    records = stream_lines(root)
    assert [r["seq"] for r in records] == [1, 2]
    assert len(records[0]["body"]) > 180_000
    assert "lorem" not in first.output

    second = observe(root, projects)
    assert counts(second)["appended"] == 0 and counts(second)["already logged"] == 2


def test_834_duplicate_entries_from_resumed_sessions_give_one_observation_per_turn(
    tmp_path: Path,
) -> None:
    """A resume replays the session it continues into its own file: same uuids, new
    sessionId (D101). 417 turns of two entries, replayed whole, is 834 duplicate entries."""
    projects = tmp_path / "projects" / "-home-example-x"
    original = turns(417)
    write(projects / "s1.jsonl", original)
    replay = [dict(r, sessionId="s2") for r in original]
    resumed = turns(2, start=1000, parent=834, session="s2")  # the resume's own new turns
    write(projects / "s2.jsonl", [*replay, *resumed])
    root = tmp_path / "root"

    first = observe(root, tmp_path / "projects")
    assert first.exit_code == 0, first.output
    assert counts(first)["duplicate entries"] == 834
    assert counts(first)["appended"] == 419
    records = stream_lines(root)
    assert len({r["body"] for r in records}) == len(records) == 419

    # Claude Code resumes again, replaying both files into a third.
    write(projects / "s3.jsonl", [dict(r, sessionId="s3") for r in [*original, *resumed]])
    second = observe(root, tmp_path / "projects")
    assert counts(second)["appended"] == 0
    assert counts(second)["already logged"] == 419
    assert counts(second)["duplicate entries"] == 834 + 838


def test_an_error_message_never_quotes_what_pydantic_was_given() -> None:
    """``_without_content`` on a ValidationError, and on one wrapped without the reader's
    " — " separator: the field and error code are kept, the value is not."""
    from pydantic import BaseModel, ConfigDict, ValidationError

    from walle.cli import _without_content

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")

    with pytest.raises(ValidationError) as caught:
        Strict.model_validate({"field": SECRET})
    wrapped = RuntimeError(f"wrapped {caught.value}")
    wrapped.__cause__ = caught.value
    for exc in (caught.value, wrapped):
        message = _without_content(exc)
        assert SECRET not in message
        assert "field: extra_forbidden" in message
