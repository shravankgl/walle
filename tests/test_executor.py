"""Issue #72's container executor, without a container (D68, D99, D117).

  the plan is the boundary   the ``docker run`` the executor builds mounts the workspace and
                             nothing else, runs as the workspace's owner, drops every
                             capability, forbids new privileges, caps memory, CPU and PIDs,
                             closes the network, and labels the container so a crashed
                             parent's child can be found
  the client is driven       with a fake ``docker`` standing in for the real one: the argv the
                             fake records is the plan exactly, the outcome carries exit code and
                             output, a hang is killed and the container removed by name, and the
                             outcome says when removal failed rather than claiming it worked,
                             judged by the listing because rm's exit status races --rm
  reapable                   ``reap`` finds containers by label and removes them, and says
                             when the daemon would not
  off by default             ``run`` refuses before any process starts unless the owner has set
                             ``WALLE_CONTAINER_EXECUTOR=on`` outside the log; no command gained;
                             nothing the agent writes can open it
  the escape tests tell the  ``tests/test_executor_escape.py`` skips with a reason when no
  truth                      daemon is reachable, and fails instead when ``WALLE_REQUIRE_DOCKER=1``
  pure of models             the module reaches no model and no network library

The real escape attempts are in ``tests/test_executor_escape.py`` and need a daemon. Every
path, image name and command here is synthetic.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from test_constraints import MODEL_OR_NETWORK, forbidden_in_closure
from walle.executor import (
    DEFAULT_IMAGE,
    EXECUTOR_SWITCH,
    GONE_TRIES,
    LABEL,
    MOUNT_POINT,
    REQUIRE_DOCKER,
    SWITCH_ON,
    DockerError,
    Job,
    Outcome,
    Refusal,
    check_workspace,
    enabled,
    plan,
    reap,
    run,
    unavailable,
)

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "src" / "walle"
EXECUTOR_SOURCE = PACKAGE / "executor.py"
FAKE = Path(__file__).parent / "fake_docker.py"
ESCAPE_TESTS = Path(__file__).parent / "test_executor_escape.py"

ON = {EXECUTOR_SWITCH: SWITCH_ON}
TOKEN = "0123abcd4567"


def _fake(tmp_path: Path, mode: str = "normal") -> tuple[tuple[str, ...], Path]:
    record = tmp_path / "record.jsonl"
    return (sys.executable, str(FAKE), "--record", str(record), "--mode", mode), record


def _calls(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _job(tmp_path: Path, **kw: object) -> Job:
    base: dict[str, object] = {"workspace": _workspace(tmp_path), "command": ("sh", "-c", "true")}
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def _flag(argv: Sequence[str], name: str) -> list[str]:
    """Every value given to ``name`` in ``argv``, as ``--name value``."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == name]


# --- the plan is the boundary --------------------------------------------------------------


def test_the_plan_is_a_docker_run_of_the_image_and_the_command(tmp_path: Path) -> None:
    job = _job(tmp_path, command=("sh", "-c", "echo hi"))
    argv = plan(job, token=TOKEN)
    assert argv[0] == "run"
    assert argv[-4:] == (DEFAULT_IMAGE, "sh", "-c", "echo hi")
    assert "--rm" in argv and "--init" in argv


def test_only_the_workspace_is_mounted(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    argv = plan(Job(ws, ("true",)), token=TOKEN)
    mounts = _flag(argv, "--mount")
    assert mounts == [f"type=bind,source={ws.resolve()},target={MOUNT_POINT}"]
    assert "-v" not in argv and "--volume" not in argv and "--volumes-from" not in argv
    assert _flag(argv, "--workdir") == [MOUNT_POINT]


def test_the_process_runs_as_the_workspace_owner_not_as_root(tmp_path: Path) -> None:
    """With every capability dropped, root has no ``CAP_DAC_OVERRIDE`` and cannot write a
    folder it does not own; the owner can, and what the step leaves behind is the owner's."""
    ws = _workspace(tmp_path)
    owner = ws.stat()
    argv = plan(Job(ws, ("true",)), token=TOKEN)
    assert _flag(argv, "--user") == [f"{owner.st_uid}:{owner.st_gid}"]
    assert "--privileged" not in argv


def test_the_root_filesystem_is_read_only_with_a_private_tmp(tmp_path: Path) -> None:
    argv = plan(_job(tmp_path), token=TOKEN)
    assert "--read-only" in argv
    (tmpfs,) = _flag(argv, "--tmpfs")
    assert tmpfs.startswith("/tmp:") and "noexec" in tmpfs and "nosuid" in tmpfs


def test_every_capability_is_dropped_and_none_may_be_gained(tmp_path: Path) -> None:
    argv = plan(_job(tmp_path), token=TOKEN)
    assert _flag(argv, "--cap-drop") == ["ALL"]
    assert "--cap-add" not in argv and "--privileged" not in argv
    assert "no-new-privileges" in _flag(argv, "--security-opt")


def test_memory_cpu_and_pids_are_capped_as_declared(tmp_path: Path) -> None:
    argv = plan(_job(tmp_path, memory="256m", cpus="0.5", pids=32), token=TOKEN)
    assert _flag(argv, "--memory") == ["256m"]
    assert _flag(argv, "--memory-swap") == ["256m"], "swap equal to memory means no swap"
    assert _flag(argv, "--cpus") == ["0.5"]
    assert _flag(argv, "--pids-limit") == ["32"]


def test_the_network_is_closed(tmp_path: Path) -> None:
    argv = plan(_job(tmp_path), token=TOKEN)
    assert _flag(argv, "--network") == ["none"]
    assert "-p" not in argv and "--publish" not in argv


def test_the_container_is_named_and_labelled_by_its_token(tmp_path: Path) -> None:
    argv = plan(_job(tmp_path), token=TOKEN)
    assert _flag(argv, "--name") == [f"walle-{TOKEN}"]
    assert _flag(argv, "--label") == [f"{LABEL}={TOKEN}"]


def test_the_host_environment_does_not_reach_the_container(tmp_path: Path) -> None:
    argv = plan(_job(tmp_path), token=TOKEN)
    assert "--env" not in argv and "-e" not in argv and "--env-file" not in argv


def test_the_plan_is_deterministic_for_one_token(tmp_path: Path) -> None:
    job = _job(tmp_path)
    assert plan(job, token=TOKEN) == plan(job, token=TOKEN)
    assert plan(job, token=TOKEN) != plan(job, token="ffff00001111")


@pytest.mark.parametrize("token", ["", "UPPER", "has space", "a/b", "-dash", "x" * 65])
def test_a_token_that_is_not_a_plain_lowercase_word_is_refused(tmp_path: Path, token: str) -> None:
    with pytest.raises(ValueError, match="token"):
        plan(_job(tmp_path), token=token)


# --- what the job may say ------------------------------------------------------------------


@pytest.mark.parametrize("image", ["", "-rm", "alpine:3.20 extra", "Alpine", "a:b:c", "x\n"])
def test_an_image_name_that_could_be_read_as_an_option_is_refused(
    tmp_path: Path, image: str
) -> None:
    with pytest.raises(ValueError, match="image"):
        _job(tmp_path, image=image)


@pytest.mark.parametrize(
    "image",
    ["alpine:3.20", "python:3.12-slim", "ghcr.io/owner/tool:1.0", "a@sha256:" + "0" * 64],
)
def test_an_ordinary_image_reference_is_accepted(tmp_path: Path, image: str) -> None:
    assert _job(tmp_path, image=image).image == image


def test_an_empty_command_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="command"):
        _job(tmp_path, command=())


@pytest.mark.parametrize("memory", ["", "0", "-1m", "1.5g", "512 m", "lots", "1t"])
def test_a_memory_cap_docker_would_not_read_as_one_is_refused(tmp_path: Path, memory: str) -> None:
    with pytest.raises(ValueError, match="memory"):
        _job(tmp_path, memory=memory)


@pytest.mark.parametrize("cpus", ["", "0", "-1", "two", "1e3", " 1"])
def test_a_cpu_cap_that_is_not_a_positive_decimal_is_refused(tmp_path: Path, cpus: str) -> None:
    with pytest.raises(ValueError, match="cpus"):
        _job(tmp_path, cpus=cpus)


@pytest.mark.parametrize("pids", [0, -1])
def test_a_pid_cap_below_one_is_refused(tmp_path: Path, pids: int) -> None:
    with pytest.raises(ValueError, match="pids"):
        _job(tmp_path, pids=pids)


@pytest.mark.parametrize("timeout_s", [0, -1.0, float("inf"), float("nan")])
def test_a_timeout_that_is_not_a_positive_finite_number_is_refused(
    tmp_path: Path, timeout_s: float
) -> None:
    with pytest.raises(ValueError, match="timeout"):
        _job(tmp_path, timeout_s=timeout_s)


def test_the_workspace_must_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workspace"):
        check_workspace(tmp_path / "missing")
    (tmp_path / "file").write_text("x")
    with pytest.raises(ValueError, match="workspace"):
        check_workspace(tmp_path / "file")


def test_a_relative_workspace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ws").mkdir()
    with pytest.raises(ValueError, match="absolute"):
        check_workspace(Path("ws"))


def test_the_filesystem_root_and_the_home_folder_are_never_the_workspace() -> None:
    with pytest.raises(ValueError, match="workspace"):
        check_workspace(Path("/"))
    with pytest.raises(ValueError, match="workspace"):
        check_workspace(Path.home())


def test_a_workspace_whose_path_would_break_the_mount_syntax_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "a,b"
    ws.mkdir()
    with pytest.raises(ValueError, match="comma"):
        check_workspace(ws)


def test_the_workspace_is_resolved_so_a_symlink_cannot_point_the_mount_elsewhere(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert check_workspace(link) == real.resolve()
    argv = plan(Job(link, ("true",)), token=TOKEN)
    assert f"source={real.resolve()}," in _flag(argv, "--mount")[0]


# --- the client is driven --------------------------------------------------------------------


def test_the_fake_is_handed_the_plan_exactly_and_the_outcome_carries_its_output(
    tmp_path: Path,
) -> None:
    docker, record = _fake(tmp_path)
    job = _job(tmp_path)
    outcome = run(job, environ=ON, docker=docker, token=TOKEN)
    assert isinstance(outcome, Outcome)
    assert outcome.exit_code == 0 and outcome.stdout == "ran\n" and outcome.stderr == ""
    assert outcome.timed_out is False and outcome.removed is None
    assert _calls(record) == [list(plan(job, token=TOKEN))]
    assert outcome.argv == (*docker, *plan(job, token=TOKEN))


def test_a_failing_command_reports_its_exit_code_and_stderr(tmp_path: Path) -> None:
    docker, _ = _fake(tmp_path, "fail-run")
    outcome = run(_job(tmp_path), environ=ON, docker=docker, token=TOKEN)
    assert isinstance(outcome, Outcome)
    assert outcome.exit_code == 7 and outcome.stderr == "boom\n" and outcome.timed_out is False


def test_a_hang_is_killed_at_the_timeout_and_the_container_removed_by_name(
    tmp_path: Path,
) -> None:
    docker, record = _fake(tmp_path, "hang")
    outcome = run(_job(tmp_path, timeout_s=3), environ=ON, docker=docker, token=TOKEN)
    assert isinstance(outcome, Outcome)
    assert outcome.timed_out is True and outcome.exit_code is None
    assert outcome.removed is True
    calls = _calls(record)
    assert calls[0][0] == "run"
    assert calls[1] == ["rm", "--force", f"walle-{TOKEN}"]
    assert calls[2] == ["ps", "--all", "--quiet", "--filter", f"label={LABEL}={TOKEN}"]


def test_removal_is_judged_by_the_listing_not_by_rm_exit_status(tmp_path: Path) -> None:
    """``--rm`` and ``rm --force`` race inside the daemon: the ``rm`` can exit 1 with "removal
    already in progress" over a container that is gone. The listing is the answer."""
    docker, _ = _fake(tmp_path, "hang-racy")
    outcome = run(_job(tmp_path, timeout_s=3), environ=ON, docker=docker, token=TOKEN)
    assert isinstance(outcome, Outcome)
    assert outcome.timed_out is True and outcome.removed is True
    assert "already in progress" not in outcome.stderr


def test_when_the_container_cannot_be_removed_the_outcome_says_so(tmp_path: Path) -> None:
    """A status message that lies — "cleaned up" over a container still running — is what
    the stranger pass hunts. ``removed`` is ``False`` and the daemon's words are kept, both
    the removal's and the listing's."""
    docker, record = _fake(tmp_path, "hang-stuck")
    outcome = run(_job(tmp_path, timeout_s=3), environ=ON, docker=docker, token=TOKEN)
    assert isinstance(outcome, Outcome)
    assert outcome.timed_out is True and outcome.removed is False
    assert "cannot remove" in outcome.stderr and "still listed" in outcome.stderr
    assert "deadbeef0000" in outcome.stderr
    assert [c[0] for c in _calls(record)].count("ps") == GONE_TRIES


def test_a_token_is_made_when_none_is_given_and_names_the_container(tmp_path: Path) -> None:
    docker, record = _fake(tmp_path)
    outcome = run(_job(tmp_path), environ=ON, docker=docker)
    assert isinstance(outcome, Outcome)
    (call,) = _calls(record)
    (name,) = _flag(call, "--name")
    assert name == f"walle-{outcome.token}" and len(outcome.token) >= 12
    assert plan(_job(tmp_path), token=outcome.token)  # a made token passes the same check


def test_a_missing_docker_client_is_a_refusal_not_a_crash(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-docker")
    result = run(_job(tmp_path), environ=ON, docker=(missing,), token=TOKEN)
    assert isinstance(result, Refusal)
    assert "not found" in result.reason and missing in result.reason


def test_a_client_that_fails_on_its_own_account_is_a_refusal_not_an_exit_code(
    tmp_path: Path,
) -> None:
    """``docker run`` exits 125 when *it* failed — no daemon, no such image — and the command
    never started. Reporting that as the command's exit status would be a lie."""
    docker, _ = _fake(tmp_path, "no-daemon")
    result = run(_job(tmp_path), environ=ON, docker=docker, token=TOKEN)
    assert isinstance(result, Refusal)
    assert "could not start" in result.reason and "Cannot connect" in result.reason


def test_the_executor_never_goes_through_a_shell(tmp_path: Path) -> None:
    docker, record = _fake(tmp_path)
    job = _job(tmp_path, command=("sh", "-c", "echo $HOME; rm -rf / # not run by us"))
    run(job, environ=ON, docker=docker, token=TOKEN)
    (call,) = _calls(record)
    assert call[-1] == "echo $HOME; rm -rf / # not run by us"  # one argv element, unexpanded
    source = EXECUTOR_SOURCE.read_text()
    assert "shell=True" not in source and "os.system" not in source


# --- reapable --------------------------------------------------------------------------------


def test_reap_lists_by_label_removes_what_it_finds_and_checks_the_listing(
    tmp_path: Path,
) -> None:
    docker, record = _fake(tmp_path, "stale")
    assert reap(docker=docker) == ("0123456789ab", "fedcba987654")
    calls = _calls(record)
    assert calls[0] == ["ps", "--all", "--quiet", "--filter", f"label={LABEL}"]
    assert calls[1] == ["rm", "--force", "0123456789ab", "fedcba987654"]
    assert calls[2] == calls[0]
    assert len(calls) == 3


def test_reap_trusts_the_listing_when_rm_loses_the_race_with_autoremove(tmp_path: Path) -> None:
    docker, _ = _fake(tmp_path, "stale-racy")
    assert reap(docker=docker) == ("0123456789ab", "fedcba987654")


def test_reap_can_be_narrowed_to_one_token(tmp_path: Path) -> None:
    docker, record = _fake(tmp_path, "stale")
    reap(TOKEN, docker=docker)
    assert _calls(record)[0] == ["ps", "--all", "--quiet", "--filter", f"label={LABEL}={TOKEN}"]


def test_reap_removes_nothing_when_nothing_is_found(tmp_path: Path) -> None:
    docker, record = _fake(tmp_path)
    assert reap(docker=docker) == ()
    assert [c[0] for c in _calls(record)] == ["ps"]


def test_reap_raises_when_the_containers_are_still_listed_after_rm(tmp_path: Path) -> None:
    docker, _ = _fake(tmp_path, "stale-stuck")
    with pytest.raises(DockerError, match="cannot remove") as info:
        reap(docker=docker)
    assert info.value.exit_code == 1 and info.value.argv[0] == "rm"
    assert "still listed" in info.value.stderr and "0123456789ab" in info.value.stderr


def test_reap_raises_when_there_is_no_daemon(tmp_path: Path) -> None:
    docker, _ = _fake(tmp_path, "no-daemon")
    with pytest.raises(DockerError, match="Cannot connect"):
        reap(docker=docker)


def test_reap_runs_whether_or_not_the_executor_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleaning up after a crash is not running Builder output: it needs no switch."""
    monkeypatch.delenv(EXECUTOR_SWITCH, raising=False)
    docker, _ = _fake(tmp_path, "stale")
    assert reap(docker=docker) == ("0123456789ab", "fedcba987654")


# --- is there a daemon ------------------------------------------------------------------------


def test_unavailable_is_none_when_the_daemon_answers(tmp_path: Path) -> None:
    docker, record = _fake(tmp_path)
    assert unavailable(docker) is None
    assert _calls(record) == [["info", "--format", "{{.ServerVersion}}"]]


def test_unavailable_quotes_the_client_when_the_daemon_does_not_answer(tmp_path: Path) -> None:
    docker, _ = _fake(tmp_path, "no-daemon")
    reason = unavailable(docker)
    assert reason is not None and "Cannot connect to the Docker daemon" in reason


def test_unavailable_names_a_missing_client(tmp_path: Path) -> None:
    reason = unavailable((str(tmp_path / "nope"),))
    assert reason is not None and "not found" in reason


# --- off by default (the owner's condition) --------------------------------------------------


def test_the_executor_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing runs unless the owner has set the switch outside the log (D117). The fake
    records nothing: the refusal lands before any process starts."""
    monkeypatch.delenv(EXECUTOR_SWITCH, raising=False)
    docker, record = _fake(tmp_path)
    assert enabled() is False and enabled({}) is False
    assert run(_job(tmp_path), docker=docker, token=TOKEN) == Refusal("container executor is off")
    assert run(_job(tmp_path), environ={}, docker=docker, token=TOKEN) == Refusal(
        "container executor is off"
    )
    assert _calls(record) == []


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "container"])
def test_only_the_one_spelling_turns_the_executor_on(value: str, tmp_path: Path) -> None:
    docker, record = _fake(tmp_path)
    assert enabled({EXECUTOR_SWITCH: value}) is False
    result = run(_job(tmp_path), environ={EXECUTOR_SWITCH: value}, docker=docker, token=TOKEN)
    assert result == Refusal("container executor is off")
    assert _calls(record) == []


def test_the_one_spelling_does_turn_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    assert enabled(ON) is True
    monkeypatch.setenv(EXECUTOR_SWITCH, SWITCH_ON)
    assert enabled() is True


def test_nothing_the_agent_writes_can_open_the_executor() -> None:
    """The switch is read from the environment and nowhere else: the module opens no file,
    reads no config and takes no ``enabled`` argument a caller could pass as ``True``."""
    tree = ast.parse(EXECUTOR_SOURCE.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "open" not in names
    assert not attrs & {"read_text", "read_bytes", "loads", "load", "open"}
    assert not any(f in Job.__dataclass_fields__ for f in ("enabled", "on", "unchecked", "force"))
    import inspect

    assert "enabled" not in inspect.signature(run).parameters


def test_the_cli_gains_no_executor_command() -> None:
    from walle.cli import main

    assert not any(
        word in name
        for name in main.commands
        for word in ("exec", "run", "container", "docker", "sandbox", "reap")
    )


def test_the_default_image_is_pinned_to_a_tag_and_is_not_latest() -> None:
    assert ":" in DEFAULT_IMAGE and not DEFAULT_IMAGE.endswith(":latest")


# --- the escape tests tell the truth -----------------------------------------------------------


def _no_daemon_on_path(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``docker`` is a client that cannot reach a daemon."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/bin/sh\necho 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock' >&2\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.pop(REQUIRE_DOCKER, None)
    return env


def _pytest_escape(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", str(ESCAPE_TESTS)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_escape_tests_skip_with_the_reason_when_no_daemon_is_reachable(tmp_path: Path) -> None:
    result = _pytest_escape(_no_daemon_on_path(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" not in result.stdout.splitlines()[-1]
    assert "skipped" in result.stdout
    assert "Cannot connect to the Docker daemon" in result.stdout
    assert f"{REQUIRE_DOCKER}=1" in result.stdout, "the skip must say how to make it a failure"


def test_the_escape_tests_fail_rather_than_skip_when_docker_is_required(tmp_path: Path) -> None:
    """A skip on the owner's Mac would look like a pass. With ``WALLE_REQUIRE_DOCKER=1`` set,
    no daemon is a failure that says so."""
    env = _no_daemon_on_path(tmp_path)
    env[REQUIRE_DOCKER] = "1"
    result = _pytest_escape(env)
    assert result.returncode != 0
    assert "skipped" not in result.stdout.splitlines()[-1]
    assert f"{REQUIRE_DOCKER}=1" in result.stdout and "no docker daemon" in result.stdout.lower()


def test_the_escape_tests_document_the_one_command_for_the_mac() -> None:
    text = ESCAPE_TESTS.read_text()
    assert f"{REQUIRE_DOCKER}=1 uv run pytest tests/test_executor_escape.py -v" in text


# --- pure of models ----------------------------------------------------------------------------


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_executor_reaches_no_model_and_no_network_library() -> None:
    assert forbidden_in_closure(PACKAGE, ("executor",)) == {}
    imports = _runtime_imports(EXECUTOR_SOURCE)
    assert not (imports & MODEL_OR_NETWORK)
    assert not any(name.startswith("walle.") for name in imports), imports


def test_the_resolution_path_does_not_import_the_executor() -> None:
    for name in (
        "events",
        "paths",
        "writer",
        "reader",
        "resolver",
        "index",
        "missions",
        "manifest",
    ):
        assert "walle.executor" not in _runtime_imports(PACKAGE / f"{name}.py"), name
