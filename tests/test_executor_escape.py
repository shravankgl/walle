"""Issue #72's verify criterion: escape attempts fail in a real container (D68).

These tests start real containers through the standard ``docker`` CLI, so they need a daemon.
Where none is reachable they **skip and say so**; agent VMs and most CI runners have none.
The owner runs them on his Mac, where OrbStack is the active ``docker`` context, with one
command, before this merges::

    WALLE_REQUIRE_DOCKER=1 uv run pytest tests/test_executor_escape.py -v

``WALLE_REQUIRE_DOCKER=1`` turns the skip into a failure: with it set, no daemon is a red
test, not a quiet pass. Without it, the skip reason says how to set it.

What is attempted, and what must hold, each in its own test:

  write outside the workspace      the root filesystem is read-only; ``/`` and ``/etc`` refuse
                                   a new file, and nothing lands on the host
  read beside the workspace        a sibling folder of the workspace, holding a synthetic
                                   secret, is not in the container at any path
  keep a capability                the effective capability set is empty; ``mount`` and
                                   ``chown`` on the read-only root fail
  gain a privilege                 ``NoNewPrivs`` is set on the process
  reach the network                the only interface is ``lo``
  outgrow the caps                 the cgroup reports the declared memory, CPU and PID limits,
                                   and a fork storm is cut off at the PID cap
  outlive the timeout              a command that sleeps past the timeout is killed and its
                                   container is gone afterwards
  survive the parent               a container left behind under the label, as a crashed
                                   parent would leave one, is found and removed by ``reap``
  the workspace itself works       a write inside the workspace reaches the host, so the
                                   refusals above are the boundary and not a broken harness

Everything here is synthetic: a temporary workspace, a made-up secret, a small public image.
No real data is mounted. The image is pulled on first use; the first run takes longer.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from walle.executor import (
    DEFAULT_IMAGE,
    EXECUTOR_SWITCH,
    LABEL,
    REQUIRE_DOCKER,
    SWITCH_ON,
    Job,
    Outcome,
    reap,
    run,
    unavailable,
)

DOCKER = ("docker",)
ON = {EXECUTOR_SWITCH: SWITCH_ON}
REASON = unavailable(DOCKER)
"""Why no daemon is reachable, or ``None`` when one is. Probed once, at import."""

COMMAND = f"{REQUIRE_DOCKER}=1 uv run pytest tests/test_executor_escape.py -v"


@pytest.fixture(autouse=True)
def _a_daemon_or_an_honest_answer() -> None:
    if REASON is None:
        return
    if os.environ.get(REQUIRE_DOCKER) == "1":
        pytest.fail(
            f"{REQUIRE_DOCKER}=1 is set but no docker daemon is reachable: {REASON}. "
            "This run proves nothing about the container boundary.",
            pytrace=False,
        )
    pytest.skip(
        f"no docker daemon reachable ({REASON}); the escape tests did not run. "
        f"On a machine with Docker: {COMMAND}  — {REQUIRE_DOCKER}=1 makes this a failure."
    )


@pytest.fixture(scope="module", autouse=True)
def _the_image_once() -> None:
    """Pull the image before the first job, so a slow first pull is not read as a timeout."""
    if REASON is not None:
        return
    present = subprocess.run(
        [*DOCKER, "image", "inspect", DEFAULT_IMAGE], capture_output=True, timeout=60
    )
    if present.returncode != 0:
        subprocess.run(
            [*DOCKER, "pull", DEFAULT_IMAGE], capture_output=True, check=True, timeout=600
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "inside.txt").write_text("synthetic workspace file\n")
    sibling = tmp_path / "beside"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("synthetic secret, never real\n")
    return ws


def _sh(workspace: Path, script: str, **kw: object) -> Outcome:
    job = Job(workspace, ("sh", "-c", script), **kw)  # type: ignore[arg-type]
    outcome = run(job, environ=ON, docker=DOCKER)
    assert isinstance(outcome, Outcome), outcome
    return outcome


def _containers(label: str) -> list[str]:
    done = subprocess.run(
        [*DOCKER, "ps", "--all", "--quiet", "--filter", f"label={label}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.split()


# --- the workspace itself works ---------------------------------------------------------------


def test_a_write_inside_the_workspace_reaches_the_host(workspace: Path) -> None:
    outcome = _sh(workspace, "cat inside.txt && echo made-inside > made.txt")
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.stdout == "synthetic workspace file\n"
    assert (workspace / "made.txt").read_text() == "made-inside\n"


# --- write outside the workspace ----------------------------------------------------------------


@pytest.mark.parametrize("target", ["/escaped", "/etc/escaped", "/usr/escaped", "/root/escaped"])
def test_a_write_outside_the_workspace_is_refused(workspace: Path, target: str) -> None:
    outcome = _sh(workspace, f"echo x > {target}")
    assert outcome.exit_code != 0
    assert "read-only" in outcome.stderr.lower() or "permission" in outcome.stderr.lower()
    assert not Path(target).exists()


def test_tmp_is_writable_but_nothing_in_it_can_run(workspace: Path) -> None:
    outcome = _sh(
        workspace, "printf '#!/bin/sh\\necho ran\\n' > /tmp/x && chmod +x /tmp/x && /tmp/x"
    )
    assert outcome.exit_code != 0
    assert "ran" not in outcome.stdout


# --- read beside the workspace -----------------------------------------------------------------


def test_a_sibling_of_the_workspace_is_not_in_the_container(
    workspace: Path, tmp_path: Path
) -> None:
    beside = tmp_path / "beside"
    outcome = _sh(
        workspace,
        f"cat {beside}/secret.txt; cat /workspace/../beside/secret.txt; "
        "grep -rl 'synthetic secret' /workspace /etc /tmp /root /home /mnt /opt /var /usr "
        "2>/dev/null; echo done",
    )
    assert outcome.exit_code == 0
    assert "synthetic secret" not in outcome.stdout
    assert outcome.stdout.strip().splitlines() == ["done"]


def test_only_one_mount_is_a_bind_from_the_host(workspace: Path) -> None:
    outcome = _sh(workspace, "cat /proc/self/mountinfo")
    assert outcome.exit_code == 0
    binds = [line for line in outcome.stdout.splitlines() if " /workspace " in f" {line} "]
    assert len(binds) == 1, outcome.stdout
    assert str(workspace.resolve()) not in "\n".join(
        line for line in outcome.stdout.splitlines() if line not in binds
    )


# --- keep a capability ------------------------------------------------------------------------


def test_the_effective_capability_set_is_empty(workspace: Path) -> None:
    outcome = _sh(workspace, "grep -E '^Cap(Eff|Prm|Bnd):' /proc/self/status")
    assert outcome.exit_code == 0, outcome.stderr
    values = {line.split()[0]: line.split()[1] for line in outcome.stdout.splitlines()}
    assert values == {
        "CapEff:": "0000000000000000",
        "CapPrm:": "0000000000000000",
        "CapBnd:": "0000000000000000",
    }


def test_mount_and_chown_fail_without_capabilities(workspace: Path) -> None:
    outcome = _sh(workspace, "mkdir -p /tmp/m && mount -t tmpfs none /tmp/m")
    assert outcome.exit_code != 0
    outcome = _sh(workspace, "touch /tmp/f && chown 1234 /tmp/f")
    assert outcome.exit_code != 0, "/tmp is writable, so only the missing CAP_CHOWN refuses this"


# --- gain a privilege -------------------------------------------------------------------------


def test_no_new_privileges_is_set(workspace: Path) -> None:
    outcome = _sh(workspace, "grep NoNewPrivs /proc/self/status")
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.stdout.split() == ["NoNewPrivs:", "1"]


# --- reach the network ------------------------------------------------------------------------


def test_the_only_interface_is_loopback(workspace: Path) -> None:
    outcome = _sh(workspace, "ls /sys/class/net")
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.stdout.split() == ["lo"]


def test_a_connection_out_fails(workspace: Path) -> None:
    outcome = _sh(workspace, "wget -q -T 3 -O - http://example.invalid/ ; nc -z -w 3 1.1.1.1 53")
    assert outcome.exit_code != 0


# --- outgrow the caps -------------------------------------------------------------------------


def _cgroup(workspace: Path, v2: str, v1: str) -> str:
    outcome = _sh(
        workspace,
        f"cat /sys/fs/cgroup/{v2} 2>/dev/null || cat /sys/fs/cgroup/{v1}",
        memory="64m",
        cpus="0.5",
        pids=20,
    )
    assert outcome.exit_code == 0, outcome.stderr
    return outcome.stdout.strip()


def test_the_memory_cap_is_the_one_declared(workspace: Path) -> None:
    assert _cgroup(workspace, "memory.max", "memory/memory.limit_in_bytes") == str(64 * 1024 * 1024)


def test_the_cpu_cap_is_the_one_declared(workspace: Path) -> None:
    text = _cgroup(workspace, "cpu.max", "cpu/cpu.cfs_quota_us")
    quota = text.split()[0]
    period = text.split()[1] if len(text.split()) > 1 else "100000"
    assert int(quota) / int(period) == pytest.approx(0.5)


def test_the_pid_cap_is_the_one_declared(workspace: Path) -> None:
    assert _cgroup(workspace, "pids.max", "pids/pids.max") == "20"


def test_a_fork_storm_is_cut_off_at_the_pid_cap(workspace: Path) -> None:
    """Sixty forks against a cap of twenty. The cgroup counts every fork it refused in
    ``pids.events``; the count is read after ``wait``, when the storm is over and a fork for
    ``cat`` is possible again."""
    outcome = _sh(
        workspace,
        "for i in $(seq 1 60); do sleep 3 & done 2>/dev/null; wait; "
        "cat /sys/fs/cgroup/pids.events 2>/dev/null || cat /sys/fs/cgroup/pids/pids.events",
        pids=20,
        timeout_s=60,
    )
    assert outcome.exit_code == 0, outcome.stderr
    refused = dict(line.split() for line in outcome.stdout.strip().splitlines())
    assert int(refused["max"]) > 0, outcome.stdout


# --- outlive the timeout ----------------------------------------------------------------------


def test_a_command_past_the_timeout_is_killed_and_its_container_is_gone(workspace: Path) -> None:
    started = time.monotonic()
    outcome = _sh(workspace, "sleep 60", timeout_s=3)
    assert time.monotonic() - started < 30
    assert outcome.timed_out is True and outcome.exit_code is None
    assert outcome.removed is True, outcome.stderr
    assert _containers(f"{LABEL}={outcome.token}") == []


# --- survive the parent -----------------------------------------------------------------------


@pytest.fixture
def orphan() -> Iterator[str]:
    """A container left running under the label, as a crashed parent would leave one. Made
    with the CLI directly, because the executor's own ``run`` would wait for it."""
    token = f"orphan{os.getpid()}"
    subprocess.run(
        [
            *DOCKER,
            "run",
            "--detach",
            "--rm",
            "--label",
            f"{LABEL}={token}",
            "--network",
            "none",
            DEFAULT_IMAGE,
            "sleep",
            "300",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    try:
        yield token
    finally:
        subprocess.run(
            [*DOCKER, "rm", "--force", *_containers(f"{LABEL}={token}")],
            capture_output=True,
            timeout=60,
        )


def test_a_container_left_behind_is_found_and_removed_by_reap(orphan: str) -> None:
    assert len(_containers(f"{LABEL}={orphan}")) == 1
    removed = reap(orphan, docker=DOCKER)
    assert len(removed) == 1
    assert _containers(f"{LABEL}={orphan}") == []
