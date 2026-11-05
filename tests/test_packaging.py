"""Task 25-26 verify: on a fresh machine, one command installs walle and one config line runs it.

What this proves, and what it cannot
------------------------------------
The README names two lines, ``INSTALL`` and ``CONFIG`` below, and this test holds the README to
them word for word. It then does what they say on a machine as fresh as a test can make one:
an empty ``HOME``, a tool folder and a bin folder of its own, ``PATH`` cut to the system's own
folders, and a data root that does not exist yet. The wheel is built from this checkout, because
the README's source (``main`` as a GitHub archive) would test the code before this change rather
than after it.

It cannot prove the parts that belong to a real fresh Mac: that ``uv`` installs there, that
GitHub serves the source, and that Claude Code starts the server from its own ``PATH``. Those
are left to the owner and listed in ``devlog/task-25.md``.

Dependencies are resolved from the package index (or uv's cache) exactly as a user's install
resolves them, not from ``uv.lock``. So this test needs the index or a warm cache, and it fails,
rather than skipping, when it has neither: a skipped verify criterion reads as a pass.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
SOURCE = "https://github.com/shravankgl/walle/archive/refs/heads/main.zip"
# --python 3.12: without it uv builds the tool on whatever Python it finds first, and a fresh Mac
# finds /usr/bin/python3, which is 3.9, so the install fails. With it, uv downloads 3.12 itself.
# An archive rather than a git+ URL: a git+ URL needs git, which a fresh Mac does not have.
INSTALL = f"uv tool install --python 3.12 {SOURCE}"
CONFIG = "claude mcp add walle -- walle-mcp --root ~/walle-data --principal me --device mac"


def _uv() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        pytest.fail("uv is not on PATH; run the tests with `uv run pytest`")
    return found


def _uv_dir(uv: str, which: str) -> str:
    args = [uv, which, "dir"]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, env=env, cwd=cwd, capture_output=True, text=True, timeout=600, check=False
    )
    assert result.returncode == 0, (
        f"{shlex.join(args)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    return result


def _readme_lines() -> list[str]:
    return [line.strip() for line in README.read_text(encoding="utf-8").splitlines()]


def test_readme_names_the_one_install_command_and_the_one_config_line() -> None:
    lines = _readme_lines()
    assert INSTALL in lines, f"README must show the install command on a line of its own: {INSTALL}"
    assert CONFIG in lines, f"README must show the config line on a line of its own: {CONFIG}"


async def _drive(command: str, args: list[str], env: dict[str, str]) -> tuple[set[str], str]:
    async with Client(StdioTransport(command=command, args=args, env=env)) as client:
        tools = {tool.name for tool in await client.list_tools()}
        staged = await client.call_tool(
            "remember",
            {"text": "The bins go out on Tuesday night.", "summary": "bins out Tuesday night"},
        )
    first = staged.content[0]
    return tools, getattr(first, "text", "")


def test_fresh_machine_one_command_installs_and_one_config_line_runs(tmp_path: Path) -> None:
    uv = _uv()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    # uv's download cache and its Python downloads are kept, so a warm machine stays quick and
    # offline-capable; they hold downloads, never walle state. Everything walle could read or
    # write lives under the fresh HOME. The certificate variables pass through for networks
    # that inspect TLS; a fresh Mac has no such thing to need.
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "UV_CACHE_DIR": _uv_dir(uv, "cache"),
        "UV_PYTHON_INSTALL_DIR": _uv_dir(uv, "python"),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(bin_dir),
    }
    env |= {k: v for k, v in os.environ.items() if k in ("SSL_CERT_FILE", "SSL_CERT_DIR")}

    dist = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(dist)], env, REPO)
    (wheel,) = dist.glob("walle-*.whl")

    # The one install command, with this checkout's wheel in place of the git source.
    install = shlex.split(INSTALL.replace(SOURCE, str(wheel)))
    assert install[:3] == ["uv", "tool", "install"]
    _run([uv, *install[1:]], env, tmp_path)
    for exe in ("walle", "walle-hook", "walle-mcp"):
        assert (bin_dir / exe).is_file(), f"the install did not put {exe} on the tool PATH"
    version = _run([str(bin_dir / "walle"), "--version"], env, tmp_path)
    assert version.stdout.startswith("walle, version ")

    # The one config line: everything after `--` is the command Claude Code will start. The
    # shell expands `~` when the line is typed, so it is expanded against the fresh HOME here.
    words = shlex.split(CONFIG)
    assert words[:4] == ["claude", "mcp", "add", "walle"]
    assert words[4] == "--"
    command, *args = words[5:]
    args = [str(home) + arg[1:] if arg.startswith("~/") else arg for arg in args]
    data = home / "walle-data"
    assert not data.exists()

    tools, reply = asyncio.run(_drive(str(bin_dir / command), args, env))

    assert tools == {"recall", "remember", "why"}
    assert reply.startswith("Staged d-mac:1."), reply
    assert (data / "principals" / "p-me" / "log" / "d-mac.jsonl").is_file()
