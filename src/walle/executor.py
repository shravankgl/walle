"""The container executor: Builder output in a container, off by default (D68, D99, D117).

D68 names the boundary: only the workspace mounted, capabilities dropped, no new privileges,
memory, CPU and PID caps, and a label so a crashed parent's child can still be reaped. D99
made it the stronger opt-in beside the OS sandbox, and the owner's condition on Phase 2
(D117, issue #72) makes it dormant: nothing here runs unless ``WALLE_CONTAINER_EXECUTOR`` is
exactly ``on`` in the environment — outside the log, outside the walle root, where no event
the agent appends and no file it writes can reach. There is no runner in this build that
calls it; this is the executor a runner will call, and the shape of the container it gets.

It drives the standard ``docker`` CLI and nothing else: no SDK, no socket, no runtime-specific
call. OrbStack, Docker Desktop and Docker Engine all answer to the same client. The three
things it does:

- ``plan(job, token)`` builds the exact ``docker run`` argv, with no process started. This is
  the boundary as a list of strings, and it is what the tests pin::

      run --rm --init --name walle-<token> --label walle.executor=<token>
          --mount type=bind,source=<workspace>,target=/workspace --workdir /workspace
          --user <uid>:<gid>
          --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m
          --network none --cap-drop ALL --security-opt no-new-privileges
          --memory <m> --memory-swap <m> --cpus <c> --pids-limit <p>
          <image> <command...>

  The root filesystem is read-only and ``/tmp`` is a private tmpfs nothing can execute from,
  so the workspace is the only place a write can land and the only place one is looked for.
  The process runs as the workspace's owner (``--user <uid>:<gid>``, from the folder's
  ``stat`` on the host), never as root. Two reasons. With every capability dropped, root has
  no ``CAP_DAC_OVERRIDE`` and cannot write a bind mount the host user owns — the first run in
  a real Linux container (CI) failed exactly there, where OrbStack's file sharing had hidden
  it on the Mac. And files the step leaves behind should belong to the person, not to root.
  A workspace owned by uid 0 is refused outright, because its owner *is* root and the
  container would run as root again.
  ``--memory-swap`` equal to ``--memory`` is how the CLI spells "no swap". Nothing from the
  host environment is passed in: ``docker run`` gives a container an empty environment
  unless told otherwise, and it is not told.
- ``run(job, ...)`` refuses unless the switch is on, then runs the plan with a timeout. A
  command that outlives it is killed with the client, and because killing the client does not
  stop the container, the container is then removed by name; the ``Outcome`` says whether
  that removal worked rather than assuming it did.
- ``reap(token)`` finds every container under the label — all of them, or one token's — and
  removes them. It needs no switch: cleaning up after a crash is not running Builder output.

Removal is judged by the listing, never by ``rm``'s exit status. A container started with
``--rm`` is removed by the daemon the moment it stops, so an ``rm --force`` on a running one
races the daemon's own removal and can exit 1 ("removal already in progress") over a
container that is in fact gone. Both ``run`` and ``reap`` therefore ask ``docker ps`` after
the ``rm``, for up to two seconds, and report what it says.

Every value that reaches the argv is checked first, so an image name or a cap that the CLI
would read as an option cannot be smuggled in by a plan: the image must look like an image
reference, the caps like the numbers the CLI documents, the token like a plain word, and the
workspace must be an existing absolute directory that is neither ``/`` nor the home folder,
resolved so a symlink cannot point the mount elsewhere.

What this module does not do: it does not decide *what* runs (that is the mission's manifest
and ``walle.missions.admit``), it sets no environment inside the container (not even
``HOME``), and it does not pull images ahead of time. The escape attempts that prove the
boundary are ``tests/test_executor_escape.py`` and need a daemon; the unit tests drive a fake
client.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

EXECUTOR_SWITCH = "WALLE_CONTAINER_EXECUTOR"
"""The environment variable the owner sets to turn the container executor on. Nothing else
does."""

SWITCH_ON = "on"
"""The one value that means on — the same rule as ``walle.missions``: one spelling, so a stray
value in a shell profile cannot be mistaken for consent."""

REQUIRE_DOCKER = "WALLE_REQUIRE_DOCKER"
"""Set to ``1`` where a daemon *must* be present (the owner's Mac): the escape tests then fail
instead of skipping when none is reachable, so a skip cannot pass as a run."""

LABEL = "walle.executor"
"""The label every container carries, with the job's token as its value, so ``reap`` can find
what a crashed parent left behind."""

MOUNT_POINT = "/workspace"
DEFAULT_IMAGE = "alpine:3.20"
"""Small, public, pinned to a tag. A job names its own image when it needs more."""

TMPFS = "/tmp:rw,noexec,nosuid,size=64m"
CLIENT_TIMEOUT_S = 30.0
"""How long the client itself may take for ``info``, ``ps`` and ``rm`` — not the job."""

GONE_TRIES = 10
GONE_PAUSE_S = 0.2
"""How long a removal is given to show in the listing: ``--rm`` and ``rm --force`` race each
other inside the daemon, so the removal's exit status is not the answer and the listing is."""

CLIENT_FAILED = 125
"""The exit status ``docker run`` reserves for its own failure — no daemon, no such image, a
bad option — as distinct from the command's. A command that chooses to exit 125 itself is
read as the client failing; the refusal carries the stderr either way, so a reader can tell."""

_IMAGE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?(?:@sha256:[0-9a-f]{64})?$"
)
_MEMORY = re.compile(r"^[1-9][0-9]*[bkmgBKMG]?$")
_CPUS = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_TOKEN = re.compile(r"^[a-z0-9]{1,64}$")


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the container executor on (D117).

    Off unless ``WALLE_CONTAINER_EXECUTOR`` is exactly ``on``. Reads the process environment
    when no mapping is given; a mapping is the whole answer when one is.
    """
    env = os.environ if environ is None else environ
    return env.get(EXECUTOR_SWITCH) == SWITCH_ON


@dataclass(frozen=True)
class Job:
    """One command to run in a container: where, what, and how much it may use.

    Checked at construction, so a bad value is refused before anything is planned. The
    workspace itself is checked when the plan is built (``check_workspace``), because that
    takes a look at the filesystem and this does not.
    """

    workspace: Path
    """The one folder the container sees, bind-mounted at ``/workspace``, read and write."""

    command: tuple[str, ...]
    """The argv run inside the container, after the image. Never a shell string of ours."""

    image: str = DEFAULT_IMAGE
    memory: str = "512m"
    """As ``docker run --memory`` reads it: digits and one of ``b k m g``."""

    cpus: str = "1"
    """As ``docker run --cpus`` reads it: a positive decimal."""

    pids: int = 64
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if not self.command or not all(isinstance(part, str) for part in self.command):
            raise ValueError("command must be a non-empty tuple of strings")
        if not _IMAGE.fullmatch(self.image):
            raise ValueError(f"image {self.image!r} is not an image reference")
        if not _MEMORY.fullmatch(self.memory):
            raise ValueError(f"memory {self.memory!r} is not a cap docker reads (e.g. 512m)")
        if not _CPUS.fullmatch(self.cpus) or float(self.cpus) <= 0:
            raise ValueError(f"cpus {self.cpus!r} is not a positive decimal")
        if self.pids < 1:
            raise ValueError(f"pids must be at least 1, not {self.pids}")
        if not isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be a positive finite number, not {self.timeout_s}")


@dataclass(frozen=True)
class Refusal:
    """Why ``run`` did not start anything."""

    reason: str


@dataclass(frozen=True)
class Outcome:
    """What happened to a job that was started."""

    token: str
    argv: tuple[str, ...]
    """The whole client command, so a log can say exactly what ran."""

    exit_code: int | None
    """The command's exit status, or ``None`` when it was killed at the timeout."""

    stdout: str
    stderr: str
    timed_out: bool = False
    removed: bool | None = None
    """After a timeout: whether the container was removed. ``None`` when there was no timeout
    and ``--rm`` did the removing. ``False`` is a container not confirmed removed — still
    running, or never made because the client died pulling the image; the daemon's words are
    appended to ``stderr`` and ``reap`` is the next move."""


class DockerError(RuntimeError):
    """The client ran and the daemon said no. ``argv`` is the docker argv (after the client),
    ``exit_code`` and ``stderr`` the client's."""

    def __init__(self, argv: Sequence[str], exit_code: int, stderr: str) -> None:
        super().__init__(f"docker {' '.join(argv[:2])} failed ({exit_code}): {stderr.strip()}")
        self.argv = tuple(argv)
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True)
class _Client:
    """The docker client as an argv prefix: ``("docker",)``, or a fake for the tests."""

    argv: tuple[str, ...] = field(default=("docker",))

    def run(self, *args: str, timeout: float) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # no shell: every element is checked or a literal
            [*self.argv, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )


def check_workspace(path: Path) -> Path:
    """The workspace as it will be mounted: absolute, resolved, an existing directory that is
    neither the filesystem root nor the home folder, and spelled so the mount syntax reads it.

    Raises ``ValueError`` naming the rule otherwise. Resolving first means a symlink at
    ``path`` mounts what it points at, by that name, and the plan says so.
    """
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"workspace must be an absolute path, not {str(path)!r}")
    resolved = path.resolve(strict=False)
    if not resolved.is_dir():
        raise ValueError(f"workspace {str(resolved)!r} is not an existing directory")
    if resolved == Path(resolved.anchor):
        raise ValueError("workspace may not be the filesystem root")
    if resolved == Path.home().resolve():
        raise ValueError("workspace may not be the home folder")
    if "," in str(resolved) or "\n" in str(resolved):
        raise ValueError(
            f"workspace path {str(resolved)!r} holds a comma or newline; "
            "the mount syntax cannot carry it"
        )
    return resolved


def owner_user(workspace: Path) -> str:
    """``uid:gid`` of the workspace's owner, as ``--user`` takes it: who the container runs
    as. Raises ``ValueError`` for a workspace owned by uid 0, since that would put root back
    in the container. One place, so a test can pin the plan against it and a runner can say
    who ran."""
    uid, gid = _owner_ids(workspace)
    if uid == 0:
        raise ValueError(
            f"workspace {str(workspace)!r} is owned by root; the container would run as root"
        )
    return f"{uid}:{gid}"


def _owner_ids(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_uid, info.st_gid


def _check_token(token: str) -> str:
    if not _TOKEN.fullmatch(token):
        raise ValueError(f"token {token!r} must be 1 to 64 lowercase letters and digits")
    return token


def plan(job: Job, *, token: str) -> tuple[str, ...]:
    """The ``docker run`` argv for ``job``, after the client: the boundary as a list of strings.

    Starts no process, but does ``stat`` the workspace for its owner. ``token`` names and
    labels the container; ``run`` makes one when the caller has none. Raises ``ValueError``
    for a workspace or token that fails its check, and for a root-owned workspace.
    """
    workspace = check_workspace(job.workspace)
    token = _check_token(token)
    return (
        "run",
        "--rm",
        "--init",
        "--name",
        f"walle-{token}",
        "--label",
        f"{LABEL}={token}",
        "--mount",
        f"type=bind,source={workspace},target={MOUNT_POINT}",
        "--workdir",
        MOUNT_POINT,
        "--user",
        owner_user(workspace),
        "--read-only",
        "--tmpfs",
        TMPFS,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        job.memory,
        "--memory-swap",
        job.memory,
        "--cpus",
        job.cpus,
        "--pids-limit",
        str(job.pids),
        job.image,
        *job.command,
    )


def run(
    job: Job,
    *,
    environ: Mapping[str, str] | None = None,
    docker: Sequence[str] = ("docker",),
    token: str | None = None,
) -> Refusal | Outcome:
    """Run ``job`` in a container, or refuse.

    Refuses, before any process starts, unless the owner has turned the executor on
    (``enabled``), and when the client named by ``docker`` cannot be found; refuses after
    the client ran when it failed on its own account (``CLIENT_FAILED``: no daemon, no such
    image), because then the command never started. Otherwise runs the plan with the job's
    timeout, which covers an image pull on first use. On a timeout the client is killed, the
    container is removed by name, and the outcome records whether that worked. The client's
    own stdout and stderr are the command's, decoded with replacement so a stray byte cannot
    raise.
    """
    if not enabled(environ):
        return Refusal("container executor is off")
    client = _Client(tuple(docker))
    if shutil.which(client.argv[0]) is None and not Path(client.argv[0]).is_file():
        return Refusal(f"docker client not found: {client.argv[0]!r}")
    token = _check_token(token) if token is not None else secrets.token_hex(8)
    argv = plan(job, token=token)
    full = (*client.argv, *argv)
    try:
        done = client.run(*argv, timeout=job.timeout_s)
    except subprocess.TimeoutExpired as expired:
        stdout = _text(expired.stdout)
        stderr = _text(expired.stderr)
        removed, note = _remove(client, token)
        if note:
            stderr = f"{stderr}{note}"
        return Outcome(token, full, None, stdout, stderr, timed_out=True, removed=removed)
    if done.returncode == CLIENT_FAILED:
        return Refusal(f"docker could not start the container: {_text(done.stderr).strip()}")
    return Outcome(token, full, done.returncode, _text(done.stdout), _text(done.stderr))


def _remove(client: _Client, token: str) -> tuple[bool, str]:
    """``docker rm --force`` the container, then ask the listing whether it is gone. Returns
    whether it is and, when not, the daemon's words — the removal's and the listing's."""
    name = f"walle-{token}"
    removal = ("rm", "--force", name)
    try:
        done = client.run(*removal, timeout=CLIENT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"docker rm --force {name} did not finish in {CLIENT_TIMEOUT_S:g}s\n"
    words = _text(done.stderr) if done.returncode != 0 else ""
    gone, listed = _gone(client, f"label={LABEL}={token}")
    if gone:
        return True, ""
    return False, f"{words}{listed}"


def _gone(client: _Client, selector: str) -> tuple[bool, str]:
    """Whether ``docker ps --all`` lists nothing for ``selector``, asked up to ``GONE_TRIES``
    times a short pause apart. ``(False, words)`` when something is still listed at the end,
    or when the listing itself failed — the daemon's words either way."""
    listing = ("ps", "--all", "--quiet", "--filter", selector)
    ids: list[str] = []
    for attempt in range(GONE_TRIES):
        try:
            done = client.run(*listing, timeout=CLIENT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"docker ps did not answer in {CLIENT_TIMEOUT_S:g}s\n"
        if done.returncode != 0:
            return False, _text(done.stderr) or f"docker ps exited {done.returncode}\n"
        ids = _text(done.stdout).split()
        if not ids:
            return True, ""
        if attempt + 1 < GONE_TRIES:
            time.sleep(GONE_PAUSE_S)
    return False, f"still listed under {selector}: {', '.join(ids)}\n"


def reap(token: str | None = None, *, docker: Sequence[str] = ("docker",)) -> tuple[str, ...]:
    """Remove every container under the label — one token's, or all — and return their ids.

    For the parent that crashed with a child still running, and for the ``Outcome`` whose
    ``removed`` was ``False``. Needs no switch: it consults no environment. Judged by the
    listing after the removal, not by ``rm``'s status (see the module docstring); raises
    ``DockerError`` when the listing fails or still shows something at the end, so a reap that
    did nothing is never reported as one that did.
    """
    client = _Client(tuple(docker))
    selector = f"label={LABEL}" if token is None else f"label={LABEL}={_check_token(token)}"
    listing = ("ps", "--all", "--quiet", "--filter", selector)
    done = client.run(*listing, timeout=CLIENT_TIMEOUT_S)
    if done.returncode != 0:
        raise DockerError(listing, done.returncode, _text(done.stderr))
    ids = tuple(_text(done.stdout).split())
    if not ids:
        return ()
    removal = ("rm", "--force", *ids)
    done = client.run(*removal, timeout=CLIENT_TIMEOUT_S)
    words = _text(done.stderr) if done.returncode != 0 else ""
    gone, listed = _gone(client, selector)
    if not gone:
        raise DockerError(removal, done.returncode, f"{words}{listed}")
    return ids


def unavailable(docker: Sequence[str] = ("docker",)) -> str | None:
    """Why no daemon is reachable through ``docker``, or ``None`` when one answers ``info``.

    For the escape tests' skip-or-fail, and for a runner deciding whether this executor can
    be offered at all. Quotes the client's own words so the reason is the real one.
    """
    client = _Client(tuple(docker))
    if shutil.which(client.argv[0]) is None and not Path(client.argv[0]).is_file():
        return f"docker client not found: {client.argv[0]!r}"
    try:
        done = client.run("info", "--format", "{{.ServerVersion}}", timeout=CLIENT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"docker info did not answer in {CLIENT_TIMEOUT_S:g}s"
    except OSError as error:
        return f"docker client could not be started: {error}"
    if done.returncode != 0:
        words = _text(done.stderr).strip() or _text(done.stdout).strip()
        return words or f"docker info exited {done.returncode}"
    return None


def _text(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")
