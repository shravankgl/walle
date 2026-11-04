"""Task 15, the server's `srt` profile (D52, D99).

  profile       generated from the root and principal: reads the profile folder only, writes
                the log folder only, no network, and never another principal's folder
  command       `walle-mcp --srt-profile` prints it and touches nothing
  modes         the same write/read split staged with real file modes, so it runs where `srt`
                is not installed (CI). Skips where the process can write anyway (root)
  under srt     the server run under the real `srt`, and the profile's three promises probed
                from inside it. Skips, with the reason, where `srt` is not installed or cannot
                start a sandbox on this machine

Every event here is synthetic: a made-up person's kitchen, no real data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp.types import TextContent

from walle import index, mcp_server, sandbox
from walle.events import ACTOR_USER, Assertion, now_instant
from walle.paths import log_dir, principal_dir
from walle.reader import read_stream
from walle.writer import StreamWriter

P = "maya"
D = "mbp"
OTHER = "ravi"


# --- helpers ----------------------------------------------------------------------------


def _profile_folder(root: Path) -> None:
    """Maya has a log with one belief; Ravi has a folder of his own."""
    StreamWriter(root, P, D).append(
        Assertion(
            ts=now_instant(),
            actor=ACTOR_USER,
            body="Maya waters the basil at 7 every morning.",
            summary="basil watering",
        )
    )
    other = principal_dir(root, OTHER)
    (other / "log").mkdir(parents=True)
    (other / "log" / "d-phone.jsonl").write_text("synthetic, not Maya's\n")


def _settings(root: Path) -> dict[str, Any]:
    return sandbox.srt_settings(root, P)


def _write_settings(root: Path, where: Path) -> Path:
    path = where / "walle.srt.json"
    path.write_text(sandbox.srt_settings_json(root, P))
    return path


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _server_args(root: Path) -> list[str]:
    return ["-m", "walle.mcp_server", "--root", str(root), "--principal", P, "--device", D]


async def _remember_and_recall(transport: StdioTransport) -> tuple[Any, Any]:
    async with Client(transport) as client:
        remembered = await client.call_tool(
            "remember", {"text": "Maya grows mint by the door."}, raise_on_error=False
        )
        recalled = await client.call_tool("recall", {"query": "basil"}, raise_on_error=False)
        return remembered, recalled


def _text(result: Any) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


@contextlib.contextmanager
def frozen_except_log(root: Path) -> Iterator[None]:
    """Maya's folder read-only with the log folder still writable: the profile as file modes.

    Skips where modes are not enforced (root, CAP_DAC_OVERRIDE), found by trying, not by
    asking who we are.
    """
    folder = principal_dir(root, P)
    modes = {p: p.stat().st_mode for p in [folder, *folder.iterdir()] if p != log_dir(root, P)}
    for p, mode in modes.items():
        p.chmod(stat.S_IMODE(mode) & ~0o222)
    try:
        probe = folder / "probe"
        with contextlib.suppress(PermissionError):
            probe.touch()
            probe.unlink()
            pytest.skip("this process can write a read-only folder (root or CAP_DAC_OVERRIDE)")
        yield
    finally:
        for p, mode in modes.items():
            p.chmod(stat.S_IMODE(mode))


# --- the profile ------------------------------------------------------------------------


def test_the_profile_writes_the_log_folder_and_nothing_else(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    fs = _settings(tmp_path)["filesystem"]
    assert fs["allowWrite"] == [str(log_dir(tmp_path, P).resolve())]
    assert fs.get("denyWrite", []) == []


def test_the_profile_allows_no_network(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    network = _settings(tmp_path)["network"]
    assert network["allowedDomains"] == []
    assert not network.get("allowLocalBinding", False)
    assert not network.get("allowUnixSockets")


def test_the_profile_reads_the_profile_folder_and_denies_every_other_principal(
    tmp_path: Path,
) -> None:
    _profile_folder(tmp_path)
    fs = _settings(tmp_path)["filesystem"]
    folder = str(principal_dir(tmp_path, P).resolve())
    assert folder in fs["allowRead"]
    # Denying the parent of all principals, then re-allowing Maya's folder inside it, is what
    # keeps Ravi's facts unreadable (hard constraint 6). `allowRead` wins over `denyRead`.
    assert str((tmp_path / "principals").resolve()) in fs["denyRead"]
    assert str(Path.home().resolve()) in fs["denyRead"]


def test_the_profile_lets_the_interpreter_run(tmp_path: Path) -> None:
    """Denying the home folder must not deny the Python that is running the server."""
    _profile_folder(tmp_path)
    allowed = [Path(p) for p in _settings(tmp_path)["filesystem"]["allowRead"]]
    for needed in (Path(sys.prefix), Path(sys.base_prefix), Path(mcp_server.__file__).parent):
        assert any(needed.resolve().is_relative_to(a) for a in allowed), needed


def test_an_import_path_that_holds_the_principals_does_not_reopen_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allowRead` beats `denyRead`, so a `PYTHONPATH` entry above `principals/` would hand
    back every principal's folder. Such entries are left out, and so are ones inside it."""
    _profile_folder(tmp_path)
    inside = principal_dir(tmp_path, OTHER)
    monkeypatch.setattr(sys, "path", [*sys.path, str(tmp_path), str(tmp_path.parent), str(inside)])
    allowed = _settings(tmp_path)["filesystem"]["allowRead"]
    folder = str(principal_dir(tmp_path, P).resolve())
    assert str(tmp_path.resolve()) not in allowed
    assert str(tmp_path.parent.resolve()) not in allowed
    assert str(inside.resolve()) not in allowed
    assert folder in allowed


def test_an_import_path_that_holds_the_home_folder_does_not_reopen_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allowRead` beats `denyRead`, so a `PYTHONPATH` of the home folder (or of a parent of
    it) would void the home denial. Such entries are left out. One inside home stays: a
    virtualenv there must still run."""
    _profile_folder(tmp_path)
    home = tmp_path / "home" / "maya"
    venv = home / "venv"
    venv.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "path", [*sys.path, str(home), str(home.parent), str(venv)])
    allowed = _settings(tmp_path)["filesystem"]["allowRead"]
    assert str(home.resolve()) not in allowed
    assert str(home.parent.resolve()) not in allowed
    assert str(venv.resolve()) in allowed


def test_every_path_is_absolute_and_json_round_trips(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    settings = _settings(tmp_path)
    assert json.loads(sandbox.srt_settings_json(tmp_path, P)) == settings
    for key in ("denyRead", "allowRead", "allowWrite"):
        assert all(os.path.isabs(p) for p in settings["filesystem"][key]), key


def test_a_relative_root_gives_the_same_profile_as_an_absolute_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_folder(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert sandbox.srt_settings(Path("."), P) == _settings(tmp_path)


def test_a_bad_principal_is_refused_and_nothing_is_made(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="principal"):
        sandbox.srt_settings(tmp_path, "../ravi")
    assert list(tmp_path.iterdir()) == []


def test_the_profile_of_a_folder_that_does_not_exist_yet_is_still_the_same_shape(
    tmp_path: Path,
) -> None:
    """The profile can be written before the first `remember`; the log folder is then made
    by `walle`, outside the sandbox, because the sandbox may not create it."""
    fs = _settings(tmp_path)["filesystem"]
    assert fs["allowWrite"] == [str(log_dir(tmp_path, P).resolve())]
    assert list(tmp_path.iterdir()) == []


# --- the command ------------------------------------------------------------------------


def test_the_command_prints_the_profile_and_touches_nothing(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = CliRunner().invoke(
        mcp_server.main,
        ["--root", str(tmp_path), "--principal", P, "--device", D, "--srt-profile"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _settings(tmp_path)
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_the_command_refuses_a_bad_device_before_printing(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        mcp_server.main,
        ["--root", str(tmp_path), "--principal", P, "--device", "MBP", "--srt-profile"],
    )
    assert result.exit_code != 0
    assert "device" in result.output
    assert "allowWrite" not in result.output


# --- the same split as file modes -------------------------------------------------------


def test_the_server_stages_into_the_log_when_everything_else_is_read_only(
    tmp_path: Path,
) -> None:
    """What the profile promises, as modes: no write beside the log, and `remember` still
    works because the log is the one place it writes. `recall` falls back to the index as
    last built, and says so, because it cannot take the index lock."""
    _profile_folder(tmp_path)
    index.update(tmp_path, P)
    before = len(read_stream(tmp_path, P, D).events)
    transport = StdioTransport(command=sys.executable, args=_server_args(tmp_path))
    with frozen_except_log(tmp_path):
        remembered, recalled = _run(_remember_and_recall(transport))
        listing = sorted(p.name for p in principal_dir(tmp_path, P).iterdir())
    assert not remembered.is_error, _text(remembered)
    assert not recalled.is_error, _text(recalled)
    assert "as last built" in _text(recalled)
    assert len(read_stream(tmp_path, P, D).events) == before + 1
    assert listing == sorted(["log", "index.sqlite", "index.lock"])


# --- under the real srt -----------------------------------------------------------------


def _srt_or_skip(settings: Path) -> str:
    srt = shutil.which("srt")
    if srt is None:
        pytest.skip("srt is not installed (npm install -g @anthropic-ai/sandbox-runtime)")
    probe = subprocess.run(
        [srt, "--settings", str(settings), "true"], capture_output=True, text=True, timeout=60
    )
    if probe.returncode != 0:
        pytest.skip(f"srt cannot start a sandbox here: {probe.stderr.strip()[:200]}")
    return srt


def _inside(srt: str, settings: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [srt, "--settings", str(settings), sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_server_passes_under_srt(tmp_path: Path) -> None:
    """The verify criterion: the profile folder is read, the log is written, nothing else is."""
    _profile_folder(tmp_path)
    index.update(tmp_path, P)
    settings = _write_settings(tmp_path, tmp_path)
    srt = _srt_or_skip(settings)
    before = len(read_stream(tmp_path, P, D).events)
    transport = StdioTransport(
        command=srt, args=["--settings", str(settings), sys.executable, *_server_args(tmp_path)]
    )
    remembered, recalled = _run(_remember_and_recall(transport))
    assert not remembered.is_error, _text(remembered)
    assert not recalled.is_error, _text(recalled)
    assert len(read_stream(tmp_path, P, D).events) == before + 1


def test_under_srt_nothing_but_the_log_can_be_written(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    settings = _write_settings(tmp_path, tmp_path)
    srt = _srt_or_skip(settings)
    folder = principal_dir(tmp_path, P)
    ok = _inside(srt, settings, f"open({str(log_dir(tmp_path, P) / 'probe')!r}, 'w').close()")
    assert ok.returncode == 0, ok.stderr
    refused = _inside(srt, settings, f"open({str(folder / 'probe')!r}, 'w').close()")
    assert refused.returncode != 0
    assert not (folder / "probe").exists()


def test_under_srt_another_principals_folder_cannot_be_read(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    settings = _write_settings(tmp_path, tmp_path)
    srt = _srt_or_skip(settings)
    mine = _inside(srt, settings, f"open({str(StreamWriter(tmp_path, P, D).path)!r}).read()")
    assert mine.returncode == 0, mine.stderr
    theirs = principal_dir(tmp_path, OTHER) / "log" / "d-phone.jsonl"
    refused = _inside(srt, settings, f"open({str(theirs)!r}).read()")
    assert refused.returncode != 0


def test_under_srt_there_is_no_network(tmp_path: Path) -> None:
    _profile_folder(tmp_path)
    settings = _write_settings(tmp_path, tmp_path)
    srt = _srt_or_skip(settings)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        code = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=5)"
        refused = _inside(srt, settings, code)
    assert refused.returncode != 0
