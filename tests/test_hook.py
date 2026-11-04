"""Tests for the SessionStart hook (task 8; D54, D83, D90, D102, D115).

Every test that checks the hook's contract with Claude Code runs it the way the host does: a
fresh process, the payload on stdin, stdout read as the answer, the exit status read as success
or failure. Only the console script and the README's own install snippet are used to start it,
so what is measured is what a session pays. Fixtures are synthetic: a made-up person's dinners,
fern and basil.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

import walle.hook as hook
from walle import hot
from walle.events import ACTOR_USER, Assertion, Event
from walle.paths import hot_dir
from walle.writer import StreamWriter

ROOT = Path(__file__).parent.parent
P = "maya"
SCRIPT = Path(sys.executable).parent / "walle-hook"
PAYLOAD = {
    "session_id": "00000000-0000-0000-0000-000000000000",
    "transcript_path": "/tmp/synthetic/transcript.jsonl",
    "cwd": "/tmp/synthetic",
    "hook_event_name": "SessionStart",
    "source": "startup",
}


def _payload(**changes: object) -> bytes:
    return json.dumps({**PAYLOAD, **changes}).encode()


def _ts(minute: int) -> str:
    return f"2026-11-04T09:{minute // 60:02d}:{minute % 60:02d}.000000+00:00"


def _assertion(summary: str, *, body: str | None = None, minute: int = 0) -> Assertion:
    return Assertion(ts=_ts(minute), actor=ACTOR_USER, body=body or summary, summary=summary)


def _write(root: Path, *events: Event) -> None:
    writer = StreamWriter(root, P, "mbp")
    for event in events:
        writer.append(event)


def _rendered(root: Path) -> Path:
    """A rendered hot tier with three assertions and a rules file, as task 7 leaves it."""
    _write(
        root,
        _assertion("Eats dinner after eight", body="Eats dinner after eight on weekdays."),
        _assertion("Waters the fern on Sundays", body="Waters the fern on Sundays.", minute=1),
        _assertion("Prefers short replies", body="Prefers short replies, no preamble.", minute=2),
    )
    hot.render(root, P)
    folder = hot_dir(root, P)
    (folder / hot.RULES).write_text("Never book anything before 9am.\n", encoding="utf-8")
    return folder


def _run(
    root: Path,
    stdin: bytes | None = None,
    *extra: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], float]:
    """Run the installed console script as the host would; return the result and wall time."""
    argv = [str(SCRIPT), *(["--root", str(root), "--principal", P] if args is None else args)]
    start = time.monotonic()
    done = subprocess.run(
        [*argv, *extra],
        input=_payload() if stdin is None else stdin,
        capture_output=True,
        timeout=30,
        env=env,
    )
    return done, time.monotonic() - start


def _context(done: subprocess.CompletedProcess[bytes]) -> str:
    out = json.loads(done.stdout)
    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return out["hookSpecificOutput"]["additionalContext"]


def _silent(done: subprocess.CompletedProcess[bytes]) -> None:
    """What "emits nothing and exits cleanly" means to the host: no stdout, no stderr, 0."""
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")


def _slow_render(root: Path, seconds: float) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real render that holds ``render.lock`` for ``seconds`` before it folds.

    Returns once the child holds the lock. ``index.update`` runs inside the lock, so slowing
    it is what a big log or a slow disk does to a render.
    """
    ready = root / "slow-render-holds-the-lock"
    code = (
        "import pathlib, sys, time\n"
        "import walle.hot as hot\n"
        "root, ready, seconds = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), "
        "float(sys.argv[3])\n"
        "real = hot.index.update\n"
        "def slow(*a, **k):\n"
        "    ready.touch()\n"
        "    time.sleep(seconds)\n"
        "    return real(*a, **k)\n"
        "hot.index.update = slow\n"
        "hot.render(root, 'maya')\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(root), str(ready), str(seconds)])
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert child.poll() is None, "the slow render died before it took the lock"
        assert time.monotonic() < deadline, "the slow render never took the lock"
        time.sleep(0.01)
    return child, ready


def _snapshot(*folders: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    """Every path under ``folders``: mode, size, mtime_ns and bytes. A write changes one."""
    seen: dict[str, tuple[int, int, int, bytes | None]] = {}
    for folder in folders:
        for path in sorted([folder, *folder.rglob("*")]):
            st = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            seen[str(path)] = (st.st_mode, st.st_size, st.st_mtime_ns, data)
    return seen


def _snippet() -> dict[str, object]:
    """The install snippet from README.md: the only place a person copies it from."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Install the SessionStart hook", 1)[1]
    found = re.search(r"```json\n(.*?)\n```", section, re.DOTALL)
    assert found, "README.md has no json install snippet under its hook heading"
    return json.loads(found.group(1))


def _snippet_command(root: Path) -> str:
    entry = _snippet()["hooks"]["SessionStart"][0]["hooks"][0]  # type: ignore[index]
    command: str = entry["command"]
    return (
        command.replace("/path/to/walle/.venv/bin/walle-hook", shlex.quote(str(SCRIPT)))
        .replace("/path/to/walle-data", shlex.quote(str(root)))
        .replace("--principal maya", f"--principal {P}")
    )


# --- the verify criterion -----------------------------------------------------------------


def test_a_fake_payload_gets_the_hot_tier_as_json(tmp_path: Path) -> None:
    """Fake hook payload on stdin -> one JSON object carrying the hot tier, in D81's order."""
    folder = _rendered(tmp_path)

    done, _ = _run(tmp_path)

    assert (done.returncode, done.stderr) == (0, b"")
    context = _context(done)
    texts = [(folder / name).read_text(encoding="utf-8") for name in hot.ORDER]
    starts = [context.index(text) for text in texts]
    assert starts == sorted(starts), "the files must be injected in ORDER: rules above learned"
    for summary in ("Eats dinner after eight", "Waters the fern", "Never book anything"):
        assert summary in context
    assert done.stdout.count(b"\n") <= 1, "one JSON object and nothing else on stdout"


def test_no_rules_file_is_fine_and_the_rules_are_labelled_when_there_is_one(
    tmp_path: Path,
) -> None:
    """``rules.md`` is the person's and may not exist; when it does, the model is told whose."""
    folder = _rendered(tmp_path)
    with_rules = _context(_run(tmp_path)[0])
    (folder / hot.RULES).unlink()
    without = _context(_run(tmp_path)[0])

    assert f"{hot.RULES}, {P}'s own rules, written only by them" in with_rules
    assert "Never book anything" not in without
    assert (folder / hot.LEARNED).read_text(encoding="utf-8") in without


def test_the_measured_token_count_is_printed(tmp_path: Path) -> None:
    """``--measure`` prints the size of what was injected, on stderr, beside the same JSON."""
    _rendered(tmp_path)

    done, _ = _run(tmp_path, None, "--measure")

    context = _context(done)  # stdout is still exactly the hook's answer
    line = done.stderr.decode()
    print(line, end="")
    found = re.fullmatch(
        r"walle hook: injected ([\d,]+) characters, about ([\d,]+) tokens "
        r"\(characters / 4, an estimate\), in (\d+) ms after start-up\n",
        line,
    )
    assert found, line
    chars, tokens = (int(found.group(i).replace(",", "")) for i in (1, 2))
    assert chars == len(context)
    assert tokens == math.ceil(len(context) / hook.CHARS_PER_TOKEN)


def test_a_render_made_slow_emits_nothing_and_the_session_still_starts(tmp_path: Path) -> None:
    """D115: past the budget the hook gives up, says nothing and exits 0; the next session
    gets the render that was slow."""
    _rendered(tmp_path)
    _write(tmp_path, _assertion("Grows basil on the sill", minute=3))
    child, _ = _slow_render(tmp_path, hook.BUDGET + 3)
    try:
        done, took = _run(tmp_path)
        _silent(done)
        assert hook.BUDGET * 0.95 <= took < hook.BUDGET + 1.0, took
        assert child.poll() is None, "the render must still be running, or nothing was tested"
    finally:
        assert child.wait(timeout=30) == 0

    after, _ = _run(tmp_path)
    assert "Grows basil on the sill" in _context(after)


def test_a_short_render_is_waited_out_not_given_up_on(tmp_path: Path) -> None:
    """The hook waits for a render in progress so it never reads half of one, within budget."""
    _rendered(tmp_path)
    _write(tmp_path, _assertion("Grows basil on the sill", minute=3))
    child, _ = _slow_render(tmp_path, 0.4)
    try:
        done, took = _run(tmp_path)
    finally:
        assert child.wait(timeout=30) == 0

    assert "Grows basil on the sill" in _context(done)
    assert took < hook.BUDGET


def test_stdin_that_never_closes_still_ends_within_budget(tmp_path: Path) -> None:
    """A host (or a person at a terminal) that never closes stdin cannot hold the hook."""
    _rendered(tmp_path)
    start = time.monotonic()
    proc = subprocess.Popen(
        [str(SCRIPT), "--root", str(tmp_path), "--principal", P],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    try:
        status = proc.wait(timeout=hook.BUDGET + 5)
        took = time.monotonic() - start
        out, err = proc.stdout.read(), proc.stderr.read()
    finally:
        proc.stdin.close()
        proc.kill()
        proc.wait()
    assert (status, out, err) == (0, b"", b"")
    assert took < hook.BUDGET + 1.0, took


# --- every failure is silent and exits 0 ----------------------------------------------------


def _break_learned_symlink(folder: Path) -> None:
    target = folder.parent / "elsewhere.md"
    target.write_text("not the hot tier\n", encoding="utf-8")
    (folder / hot.LEARNED).unlink()
    (folder / hot.LEARNED).symlink_to(target)


def _break_learned_bytes(folder: Path) -> None:
    path = folder / hot.LEARNED
    path.chmod(0o644)
    path.write_bytes(b"\xff\xfe not utf-8 \x80\n")


def _break_rules_fifo(folder: Path) -> None:
    (folder / hot.RULES).unlink()
    os.mkfifo(folder / hot.RULES)


def _break_rules_huge(folder: Path) -> None:
    (folder / hot.RULES).write_bytes(b"x" * (hook.MAX_FILE_BYTES + 1))


def _break_missing_learned(folder: Path) -> None:
    (folder / hot.LEARNED).unlink()


def _break_lock_missing(folder: Path) -> None:
    (folder / hot.RENDER_LOCK).unlink()


def _break_learned_blank(folder: Path) -> None:
    (folder / hot.LEARNED).write_text(" \n\n", encoding="utf-8")


def _break_identity_empty(folder: Path) -> None:
    (folder / hot.IDENTITY).write_bytes(b"")


BROKEN_FOLDERS = {
    "learned.md is a symlink": _break_learned_symlink,
    "learned.md is not utf-8": _break_learned_bytes,
    "rules.md is a fifo": _break_rules_fifo,
    "rules.md is too big": _break_rules_huge,
    "learned.md is missing": _break_missing_learned,
    "render.lock is missing": _break_lock_missing,
    "learned.md is blank": _break_learned_blank,
    "identity.md is empty": _break_identity_empty,
}


@pytest.mark.parametrize("case", sorted(BROKEN_FOLDERS))
def test_a_broken_hot_tier_emits_nothing_and_exits_zero(tmp_path: Path, case: str) -> None:
    folder = _rendered(tmp_path)
    BROKEN_FOLDERS[case](folder)

    done, took = _run(tmp_path)

    _silent(done)
    assert took < hook.BUDGET, "a broken file must fail fast, not wait out the budget"


BAD_INPUTS: dict[str, tuple[bytes, list[str] | None]] = {
    "payload is not json": (b"{not json", None),
    "payload is not an object": (b"[1, 2]", None),
    "payload is for another event": (_payload(hook_event_name="UserPromptSubmit"), None),
    "payload has no event name": (json.dumps({"source": "startup"}).encode(), None),
    "payload is too big": (b" " * (hook.MAX_PAYLOAD_BYTES + 1) + _payload(), None),
    "principal is unsafe": (_payload(), ["--root", "{root}", "--principal", "../maya"]),
    "principal is missing": (_payload(), ["--root", "{root}"]),
    "an unknown option": (_payload(), ["--root", "{root}", "--principal", P, "--frobnicate"]),
    "help is asked for": (_payload(), ["--help"]),
    "principal never rendered": (_payload(), ["--root", "{root}", "--principal", "nobody"]),
}


@pytest.mark.parametrize("case", sorted(BAD_INPUTS))
def test_bad_input_emits_nothing_and_exits_zero(tmp_path: Path, case: str) -> None:
    _rendered(tmp_path)
    stdin, args = BAD_INPUTS[case]
    argv = None if args is None else [a.replace("{root}", str(tmp_path)) for a in args]

    done, took = _run(tmp_path, stdin, args=argv)

    _silent(done)
    assert took < hook.BUDGET


def test_measure_says_why_nothing_was_emitted(tmp_path: Path) -> None:
    """``--measure`` is for a person debugging the hook; without it, failure is silent."""
    folder = _rendered(tmp_path)
    _break_missing_learned(folder)

    done, _ = _run(tmp_path, None, "--measure")

    assert (done.returncode, done.stdout) == (0, b"")
    assert done.stderr.decode().startswith("walle hook: emitted nothing: ")
    assert hot.LEARNED in done.stderr.decode()


def test_measure_warns_past_the_host_cap(tmp_path: Path) -> None:
    """Claude Code swaps an ``additionalContext`` over 10,000 characters for a file path and a
    2,000-character preview. The hook still emits it, and ``--measure`` says so."""
    body = "The basil on the sill wants morning sun. " * 40
    _write(tmp_path, *(_assertion(f"Plant note {i}", body=body, minute=i) for i in range(8)))
    hot.render(tmp_path, P)

    done, _ = _run(tmp_path, None, "--measure")

    assert len(_context(done)) > hook.HOST_CAP
    assert "over Claude Code's 10,000-character cap" in done.stderr.decode()


# --- what the hook must never do ------------------------------------------------------------


def test_the_hook_writes_nothing_and_never_touches_claude_settings(tmp_path: Path) -> None:
    """It only reads the hot tier. Not the hot folder, not ~/.claude, nothing under HOME,
    whether it succeeds or fails."""
    root = tmp_path / "data"
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    folder = _rendered(root)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}

    before = _snapshot(root, home)
    ok, _ = _run(root, env=env)
    assert _context(ok)
    assert _snapshot(root, home) == before

    _break_missing_learned(folder)
    before = _snapshot(root, home)
    failed, _ = _run(root, None, "--measure", env=env)
    assert failed.stdout == b""
    assert _snapshot(root, home) == before


def test_importing_the_hook_stays_light() -> None:
    """Cold start is paid every session: no pydantic, click, sqlite3, or the renderer."""
    code = "import json, sys, walle.hook; print(json.dumps(sorted(sys.modules)))"
    loaded = set(
        json.loads(
            subprocess.run([sys.executable, "-c", code], capture_output=True, check=True).stdout
        )
    )
    heavy = {"pydantic", "click", "sqlite3", "walle.hot", "walle.index", "walle.events"}
    assert loaded & heavy == set()


def test_the_hook_reads_the_files_the_renderer_writes() -> None:
    """The hook keeps its own copy of these names so it need not import the renderer."""
    assert hook.ORDER == hot.ORDER
    assert hook.RULES == hot.RULES
    assert hook.RENDER_LOCK == hot.RENDER_LOCK


def test_the_install_snippet_is_documentation_with_an_outer_timeout() -> None:
    entry = _snippet()["hooks"]["SessionStart"][0]  # type: ignore[index]
    assert "matcher" not in entry, "no matcher: startup, resume, clear and compact all get it"
    (command,) = entry["hooks"]
    assert command["type"] == "command"
    assert command["command"].split()[0].endswith("/.venv/bin/walle-hook")
    assert command["timeout"] > hook.BUDGET, "the host's timeout is the guard behind ours"


def test_cold_start_through_the_install_snippet(tmp_path: Path) -> None:
    """Measured the way Claude Code runs it: the snippet's command through ``sh -c``, a fresh
    process each time, the payload on stdin."""
    _rendered(tmp_path)
    command = _snippet_command(tmp_path)
    times: list[float] = []
    context = ""
    for _ in range(7):
        start = time.monotonic()
        done = subprocess.run(
            ["/bin/sh", "-c", command], input=_payload(), capture_output=True, timeout=30
        )
        times.append(time.monotonic() - start)
        context = _context(done)
    median = statistics.median(times)
    print(
        f"\ncold start via the install snippet: median {median * 1000:.0f} ms, "
        f"worst {max(times) * 1000:.0f} ms over {len(times)} runs; injected {len(context):,} "
        f"characters, about {math.ceil(len(context) / hook.CHARS_PER_TOKEN):,} tokens"
    )
    assert median < hook.BUDGET


# --- issue 23: low findings from the review of PR 30 -----------------------------------


def test_a_blank_rules_file_is_skipped_and_the_rest_injected(tmp_path: Path) -> None:
    """``rules.md`` is optional, so blank is the same as absent. Only it may be blank."""
    folder = _rendered(tmp_path)
    (folder / hot.RULES).write_text("\n", encoding="utf-8")

    done, _ = _run(tmp_path)

    assert done.returncode == 0
    text = _context(done)
    assert "Waters the fern on Sundays" in text
    assert f"{hot.RULES}, {P}'s own rules" not in text


def test_measure_as_an_option_value_is_not_the_measure_flag(tmp_path: Path) -> None:
    """``--principal --measure`` is bad arguments, not a request to measure: stay silent."""
    _rendered(tmp_path)

    done, _ = _run(tmp_path, args=["--root", str(tmp_path), "--principal", "--measure"])

    _silent(done)


def test_measure_still_explains_bad_arguments(tmp_path: Path) -> None:
    """Asked for properly, ``--measure`` reports an argument error too."""
    _rendered(tmp_path)

    done, _ = _run(tmp_path, args=["--measure", "--root", str(tmp_path)])

    assert (done.returncode, done.stdout) == (0, b"")
    assert done.stderr.decode().startswith("walle hook: emitted nothing: bad arguments")
