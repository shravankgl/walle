"""Issue #105's verify criteria that need no real OS users (D110, D32).

  the check reports a wrong owner    a principal's folder, or anything in it, owned by another
  or mode                            uid or gid, or with any permission bit for group or others,
                                     is a finding that names the path, the problem, what was
                                     expected and what was found; ``principals/`` that is
                                     listable, a profile folder that is a link, a declared
                                     principal with no folder and a folder with no declared
                                     owner are findings too
  the check is read-only             it opens no file and changes nothing: ``lstat`` and a
                                     listing, names and metadata only; a folder it cannot list
                                     is reported as unlisted, never called clean
  no code path makes users           the module imports no ``subprocess`` and calls nothing on
                                     ``os`` that would change an owner, a mode or a user; no
                                     shipped module spawns ``sudo``, ``dscl`` or ``useradd``
  off by default                     ``WALLE_OSUSERS=on``, that spelling only; while off the
                                     check stats nothing and the command refuses
  the Linux test cannot pass by      ``tests/test_osusers_isolation.py`` skips, saying why and
  skipping                           how to make it a failure, where no users can be made, and
                                     fails instead with ``WALLE_REQUIRE_OS_USERS=1``

Every root here is a ``tmp_path`` owned by the test process; the principals are a made-up Ada
and Sam; the uids are this process's own and a neighbour that is not.
"""

from __future__ import annotations

import ast
import builtins
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from walle import osusers
from walle.cli import main
from walle.osusers import (
    OFF_MESSAGE,
    PASSTHROUGH_MASK,
    PRIVATE_MASK,
    REQUIRE_OS_USERS,
    SHARED_MASK,
    SWITCH,
    SWITCH_ON,
    Finding,
    OsUsersOffError,
    Owner,
    Problem,
    Report,
    check,
    enabled,
)
from walle.paths import PRINCIPALS_DIRNAME, principal_dir
from walle.profiles import SHARED_DIRNAME

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "src" / "walle"
SOURCE = PACKAGE / "osusers.py"
ISOLATION_TESTS = ROOT / "tests" / "test_osusers_isolation.py"
ON = {SWITCH: SWITCH_ON}
A = "ada"  # a made-up household member
B = "sam"  # another
ME = Owner(os.getuid(), os.getgid())
NOT_ME = Owner(os.getuid() + 1, os.getgid() + 1)
CANARY = b"synthetic fixture: sam's stream, not real data\n"


def _household(tmp_path: Path, *members: str) -> Path:
    """A root with one private folder per member, each as the check wants it."""
    root = tmp_path / "root"
    root.mkdir(mode=0o711)
    principals = root / PRINCIPALS_DIRNAME
    principals.mkdir(mode=0o711)
    for who in members or (A, B):
        folder = principal_dir(root, who)
        folder.mkdir(mode=0o700)
        (folder / "log").mkdir(mode=0o700)
        stream = folder / "log" / "d-mbp.jsonl"
        stream.write_bytes(CANARY)
        stream.chmod(0o600)
    return root


def _findings(report: Report) -> set[tuple[str, str]]:
    return {(f.path, f.problem) for f in report.findings}


def _can_list_a_closed_folder(tmp_path: Path) -> bool:
    """Root, or a process with ``CAP_DAC_READ_SEARCH``, lists a ``0o000`` folder; the tests
    that need a refusal skip there. Tried, not inferred from ``access``: a capability set
    makes ``access`` say no while ``scandir`` succeeds."""
    probe = tmp_path / "probe"
    probe.mkdir(mode=0o000)
    try:
        os.listdir(probe)
    except PermissionError:
        return False
    finally:
        probe.chmod(0o700)
    return True


# --- the check reports a wrong owner or mode (D110) ---------------------------------------------


def test_a_household_laid_out_as_d110_wants_is_clean(tmp_path: Path) -> None:
    root = _household(tmp_path)
    report = check(root, {A: ME, B: ME}, environ=ON)
    assert report.findings == ()
    assert report.unlisted == ()
    assert report.clean
    assert report.checked >= 8  # root, principals/, two folders, two log/, two streams


def test_a_folder_owned_by_another_uid_is_a_finding(tmp_path: Path) -> None:
    root = _household(tmp_path)
    report = check(root, {A: ME, B: NOT_ME}, environ=ON)
    assert not report.clean
    paths = {f.path for f in report.findings}
    assert f"{PRINCIPALS_DIRNAME}/p-{B}" in paths
    assert f"{PRINCIPALS_DIRNAME}/p-{B}/log/d-mbp.jsonl" in paths
    assert f"{PRINCIPALS_DIRNAME}/p-{A}" not in paths
    for finding in report.findings:
        assert finding.principal == B
        assert finding.problem in (Problem.OWNER, Problem.GROUP)
        assert finding.expected in (f"uid {NOT_ME.uid}", f"gid {NOT_ME.gid}")
        assert finding.actual in (f"uid {ME.uid}", f"gid {ME.gid}")
    assert {f.problem for f in report.findings} == {Problem.OWNER, Problem.GROUP}


def test_a_wrong_group_alone_is_a_group_finding(tmp_path: Path) -> None:
    root = _household(tmp_path)
    report = check(root, {A: ME, B: Owner(ME.uid, NOT_ME.gid)}, environ=ON)
    assert {f.problem for f in report.findings} == {Problem.GROUP}
    assert all(f.principal == B for f in report.findings)


@pytest.mark.parametrize(
    ("relative", "mode"),
    [
        ("", 0o750),  # the folder itself, group may list
        ("", 0o701),  # others may pass through
        ("log", 0o705),
        ("log/d-mbp.jsonl", 0o640),
        ("log/d-mbp.jsonl", 0o604),
    ],
)
def test_any_permission_for_group_or_others_inside_a_profile_is_a_finding(
    tmp_path: Path, relative: str, mode: int
) -> None:
    root = _household(tmp_path)
    target = principal_dir(root, B) / relative if relative else principal_dir(root, B)
    target.chmod(mode)
    report = check(root, {A: ME, B: ME}, environ=ON)
    assert _findings(report) == {(target.relative_to(root).as_posix(), Problem.MODE)}
    (finding,) = report.findings
    assert finding.principal == B
    assert "group or others" in finding.expected
    assert finding.actual.startswith(("d", "-")) and len(finding.actual) == 10  # ls -l style


def test_the_strict_modes_are_what_the_module_says_they_are() -> None:
    assert PRIVATE_MASK == 0o077, "a profile: nothing for group or others"
    assert PASSTHROUGH_MASK == 0o066, "principals/ and the root: pass through, never list or write"
    assert SHARED_MASK == 0o027, "shared/: the household group reads, nobody else; nobody writes"


def test_a_listable_or_writable_principals_folder_is_a_finding(tmp_path: Path) -> None:
    root = _household(tmp_path)
    (root / PRINCIPALS_DIRNAME).chmod(0o755)
    report = check(root, {A: ME, B: ME}, environ=ON)
    assert _findings(report) == {(PRINCIPALS_DIRNAME, Problem.MODE)}
    assert report.findings[0].principal is None
    (root / PRINCIPALS_DIRNAME).chmod(0o711)
    assert check(root, {A: ME, B: ME}, environ=ON).clean
    root.chmod(0o722)
    assert _findings(check(root, {A: ME, B: ME}, environ=ON)) == {(".", Problem.MODE)}


def test_a_profile_folder_that_is_a_link_is_a_finding_and_is_not_entered(tmp_path: Path) -> None:
    root = _household(tmp_path, A)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (elsewhere / "secret").write_bytes(CANARY)
    principal_dir(root, B).symlink_to(elsewhere)
    report = check(root, {A: ME, B: ME}, environ=ON)
    assert _findings(report) == {(f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.LINK)}
    assert not any("secret" in f.path for f in report.findings)
    assert not any("elsewhere" in f.actual for f in report.findings), "never says where it points"


def test_a_principals_folder_that_is_a_link_is_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o711)
    real = tmp_path / "real-principals"
    real.mkdir(mode=0o711)
    (root / PRINCIPALS_DIRNAME).symlink_to(real)
    report = check(root, {A: ME}, environ=ON)
    assert (PRINCIPALS_DIRNAME, Problem.LINK) in _findings(report)


def test_a_declared_principal_with_no_folder_is_missing(tmp_path: Path) -> None:
    root = _household(tmp_path, A)
    report = check(root, {A: ME, B: ME}, environ=ON)
    assert _findings(report) == {(f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.MISSING)}
    principal_dir(root, B).write_bytes(b"a file where a folder should be")
    report = check(root, {A: ME, B: ME}, environ=ON)
    assert _findings(report) == {(f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.MISSING)}
    assert report.findings[0].actual == "not a folder"


def test_a_folder_with_no_declared_owner_is_undeclared(tmp_path: Path) -> None:
    root = _household(tmp_path)
    (root / PRINCIPALS_DIRNAME / "notes.txt").write_bytes(b"x")
    report = check(root, {A: ME}, environ=ON)
    assert _findings(report) == {
        (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.UNDECLARED),
        (f"{PRINCIPALS_DIRNAME}/notes.txt", Problem.UNDECLARED),
    }
    assert all(f.principal is None for f in report.findings)


def test_a_link_inside_a_profile_is_checked_for_its_owner_and_not_its_mode(
    tmp_path: Path,
) -> None:
    """A symlink's mode is not a permission (Linux reports 0o777 for every link), so a link is
    judged by who owns it. What it points at is the kernel's business under D110 and
    ``profiles.Profile.resolve``'s before then."""
    root = _household(tmp_path)
    (principal_dir(root, A) / "away").symlink_to(principal_dir(root, B) / "log")
    assert check(root, {A: ME, B: ME}, environ=ON).clean
    report = check(root, {A: NOT_ME, B: ME}, environ=ON)
    assert (f"{PRINCIPALS_DIRNAME}/p-{A}/away", Problem.OWNER) in _findings(report)
    assert (f"{PRINCIPALS_DIRNAME}/p-{A}/away", Problem.MODE) not in _findings(report)


def test_shared_is_checked_for_the_household_group_when_one_is_given(tmp_path: Path) -> None:
    root = _household(tmp_path)
    shared = root / SHARED_DIRNAME
    (shared / "skills").mkdir(parents=True, mode=0o750)
    shared.chmod(0o750)
    skill = shared / "skills" / "water.md"
    skill.write_bytes(b"# a made-up skill\n")
    skill.chmod(0o640)
    assert check(root, {A: ME, B: ME}, environ=ON).clean, "no group given: shared/ not judged"
    assert check(root, {A: ME, B: ME}, shared=ME.gid, environ=ON).clean
    report = check(root, {A: ME, B: ME}, shared=NOT_ME.gid, environ=ON)
    assert {f.problem for f in report.findings} == {Problem.GROUP}
    assert {f.path for f in report.findings} == {
        SHARED_DIRNAME,
        f"{SHARED_DIRNAME}/skills",
        f"{SHARED_DIRNAME}/skills/water.md",
    }
    skill.chmod(0o664)
    report = check(root, {A: ME, B: ME}, shared=ME.gid, environ=ON)
    assert _findings(report) == {(f"{SHARED_DIRNAME}/skills/water.md", Problem.MODE)}
    skill.chmod(0o644)
    report = check(root, {A: ME, B: ME}, shared=ME.gid, environ=ON)
    assert _findings(report) == {(f"{SHARED_DIRNAME}/skills/water.md", Problem.MODE)}


def test_a_missing_shared_folder_is_not_a_finding(tmp_path: Path) -> None:
    root = _household(tmp_path)
    assert check(root, {A: ME, B: ME}, shared=ME.gid, environ=ON).clean


def test_a_household_with_no_principals_folder_is_one_finding(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o711)
    report = check(root, {A: ME}, environ=ON)
    assert _findings(report) == {(PRINCIPALS_DIRNAME, Problem.MISSING)}


def test_findings_are_sorted_by_path_and_carry_no_file_contents(tmp_path: Path) -> None:
    root = _household(tmp_path)
    (principal_dir(root, A) / "log" / "d-mbp.jsonl").chmod(0o644)
    report = check(root, {A: ME, B: NOT_ME}, environ=ON)
    paths = [f.path for f in report.findings]
    assert paths == sorted(paths)
    for finding in report.findings:
        for text in (finding.path, finding.expected, finding.actual):
            assert "synthetic fixture" not in text


# --- the check is read-only ---------------------------------------------------------------------


def test_the_check_opens_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _household(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the check opened something: {args[:1]}")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr(os, "read", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    report = check(root, {A: ME, B: NOT_ME}, environ=ON)
    assert report.findings


def test_the_check_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _household(tmp_path)
    (principal_dir(root, B) / "log").chmod(0o755)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the check changed something: {args[:1]}")

    for name in ("chmod", "lchmod", "fchmod", "chown", "lchown", "fchown", "unlink", "rename"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse)
    before = {p: os.lstat(p).st_mode for p in root.rglob("*")}
    report = check(root, {A: ME, B: NOT_ME}, environ=ON)
    assert report.findings
    assert {p: os.lstat(p).st_mode for p in root.rglob("*")} == before


def test_a_folder_the_check_cannot_list_is_unlisted_and_never_clean(tmp_path: Path) -> None:
    if _can_list_a_closed_folder(tmp_path):
        pytest.skip("this process can list a 0o000 folder (root or CAP_DAC_OVERRIDE)")
    root = _household(tmp_path)
    closed = principal_dir(root, B) / "log"
    closed.chmod(0o000)
    try:
        report = check(root, {A: ME, B: ME}, environ=ON)
    finally:
        closed.chmod(0o700)
    assert report.findings == (), "the mode is still private, so there is nothing to report"
    assert report.unlisted == (f"{PRINCIPALS_DIRNAME}/p-{B}/log",)
    assert not report.clean, "what was not looked at cannot be called clean"


def test_a_principals_folder_the_check_cannot_list_still_checks_each_declared_top_folder(
    tmp_path: Path,
) -> None:
    if _can_list_a_closed_folder(tmp_path):
        pytest.skip("this process can list a 0o000 folder (root or CAP_DAC_OVERRIDE)")
    root = _household(tmp_path)
    principals = root / PRINCIPALS_DIRNAME
    principals.chmod(0o111)  # pass through, do not list: what D110 wants of it
    try:
        report = check(root, {A: ME, B: NOT_ME}, environ=ON)
    finally:
        principals.chmod(0o711)
    assert report.unlisted == (PRINCIPALS_DIRNAME,)
    assert (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.OWNER) in _findings(report)
    assert not any(f.problem == Problem.UNDECLARED for f in report.findings)


# --- no code path makes users (the owner's limit) -----------------------------------------------

CHANGES_A_USER_OR_A_MODE = {
    "system",
    "popen",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "posix_spawn",
    "posix_spawnp",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "fork",
    "forkpty",
    "chown",
    "lchown",
    "fchown",
    "chmod",
    "lchmod",
    "fchmod",
    "setuid",
    "seteuid",
    "setreuid",
    "setresuid",
    "setgid",
    "setegid",
    "setregid",
    "setresgid",
    "setgroups",
    "initgroups",
    "mkdir",
    "makedirs",
    "unlink",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "symlink",
    "link",
    "open",
    "truncate",
}
FORBIDDEN_WORDS = ("sudo", "dscl", "useradd", "userdel", "groupadd", "chown", "chmod")
SPAWNERS = {"subprocess", "pty", "multiprocessing", "asyncio"}


def _calls_and_strings(source: str) -> tuple[set[str], list[str]]:
    """Every ``module.attr`` and bare name called, and every string constant that is not a
    docstring."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    calls: set[str] = set()
    strings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                calls.add(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Attribute):
                calls.add(f"?.{func.attr}")
            elif isinstance(func, ast.Name):
                calls.add(func.id)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            strings.append(node.value)
    return calls, strings


def test_the_module_spawns_nothing_and_changes_no_owner_mode_or_user() -> None:
    source = SOURCE.read_text()
    imported = {
        (alias.name.split(".")[0])
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & SPAWNERS, imported & SPAWNERS
    calls, strings = _calls_and_strings(source)
    offending = {
        c
        for c in calls
        if c.startswith("os.") and c.removeprefix("os.") in CHANGES_A_USER_OR_A_MODE
    }
    assert not offending, offending
    assert "open" not in calls and "?.open" not in calls and "?.write_bytes" not in calls
    for text in strings:
        for word in FORBIDDEN_WORDS:
            assert word not in text.lower(), f"{word!r} appears in a string: {text!r}"


def test_no_shipped_module_spawns_sudo_dscl_or_useradd() -> None:
    """The verify criterion's last clause, over every module: no spawn whose arguments name
    one of the three. Docstrings and comments may mention them; a call may not."""
    spawn_calls = {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.system",
        "os.popen",
        "os.execv",
        "os.execvp",
        "os.execve",
        "os.execvpe",
        "os.spawnv",
        "os.spawnvp",
        "os.posix_spawn",
        "os.posix_spawnp",
    }
    for module in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                f"{func.value.id}.{func.attr}"
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                else None
            )
            if name not in spawn_calls:
                continue
            for arg in ast.walk(node):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    for word in ("sudo", "dscl", "useradd"):
                        assert word not in arg.value, f"{module.name} spawns {word}: {arg.value!r}"


def test_only_the_isolation_test_names_the_three_commands() -> None:
    for test in sorted((ROOT / "tests").glob("*.py")):
        if test.name == ISOLATION_TESTS.name or test.name == Path(__file__).name:
            continue
        text = test.read_text()
        for word in ("sudo", "dscl", "useradd"):
            assert word not in text, f"{test.name} mentions {word}"


# --- owners are declared, never guessed --------------------------------------------------------


def test_an_owner_is_parsed_from_numbers_or_names() -> None:
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    assert Owner.parse("1001") == Owner(1001, 1001)
    assert Owner.parse("1001:2002") == Owner(1001, 2002)
    assert Owner.parse(me.pw_name) == Owner(me.pw_uid, me.pw_gid)
    assert Owner.parse(f"{me.pw_name}:{group.gr_name}") == Owner(me.pw_uid, group.gr_gid)
    assert Owner.parse(f"{me.pw_uid}:{group.gr_name}") == Owner(me.pw_uid, group.gr_gid)
    for bad in ("", ":", "1001:", ":1001", "no-such-user-xq9", "1001:no-such-group-xq9", "-1"):
        with pytest.raises(ValueError):
            Owner.parse(bad)


def test_a_principal_name_is_validated_before_anything_is_stated(tmp_path: Path) -> None:
    root = _household(tmp_path)
    with pytest.raises(ValueError, match="principal"):
        check(root, {"../x": ME}, environ=ON)


# --- off by default (the owner's condition) ----------------------------------------------------


def test_os_user_checks_are_off_by_default() -> None:
    assert enabled({}) is False
    assert enabled({SWITCH: ""}) is False
    assert enabled({"WALLE_OSUSER": SWITCH_ON}) is False
    assert enabled({SWITCH: SWITCH_ON}) is True
    assert SWITCH == "WALLE_OSUSERS"


@pytest.mark.parametrize("value", ["ON", "On", "1", "true", "yes", " on", "on ", "on\n", "enabled"])
def test_only_the_one_spelling_turns_it_on(value: str) -> None:
    assert enabled({SWITCH: value}) is False


def test_the_process_environment_is_read_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    assert enabled() is False
    monkeypatch.setenv(SWITCH, SWITCH_ON)
    assert enabled() is True


def test_while_off_the_check_looks_at_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _household(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the check looked while off: {args[:1]}")

    for name in ("lstat", "stat", "listdir", "scandir", "walk"):
        monkeypatch.setattr(os, name, refuse)
    monkeypatch.delenv(SWITCH, raising=False)
    for environ in (None, {}, {SWITCH: "ON"}, {SWITCH: ""}):
        with pytest.raises(OsUsersOffError, match=OFF_MESSAGE):
            check(root, {A: ME, B: ME}, environ=environ)


def test_the_command_refuses_while_off_and_reports_when_on(tmp_path: Path) -> None:
    root = _household(tmp_path)
    (principal_dir(root, B) / "log").chmod(0o755)
    args = [
        "profiles",
        "owners",
        "--root",
        str(root),
        "--owner",
        f"{A}={ME.uid}:{ME.gid}",
        "--owner",
        f"{B}={ME.uid}:{ME.gid}",
    ]
    runner = CliRunner()
    off = runner.invoke(main, args, env={SWITCH: ""})
    assert off.exit_code == 1, off.output
    assert OFF_MESSAGE in off.output
    assert "log" not in off.output

    on = runner.invoke(main, args, env=ON)
    assert on.exit_code == 1, on.output
    assert f"{PRINCIPALS_DIRNAME}/p-{B}/log: mode" in on.output
    assert "1 finding" in on.output

    (principal_dir(root, B) / "log").chmod(0o700)
    clean = runner.invoke(main, args, env=ON)
    assert clean.exit_code == 0, clean.output
    assert "clean" in clean.output


def test_the_command_takes_a_shared_group_and_refuses_a_bad_owner(tmp_path: Path) -> None:
    root = _household(tmp_path)
    runner = CliRunner()
    base = ["profiles", "owners", "--root", str(root), "--owner", f"{A}={ME.uid}:{ME.gid}"]
    bad = runner.invoke(main, [*base, "--owner", f"{B}=no-such-user-xq9"], env=ON)
    assert bad.exit_code == 2, bad.output  # click's usage error: the option was bad
    assert "no-such-user-xq9" in bad.output
    bad = runner.invoke(main, [*base, "--owner", B], env=ON)
    assert bad.exit_code != 0, bad.output
    shared = root / SHARED_DIRNAME / "skills"
    shared.mkdir(parents=True, mode=0o750)
    (root / SHARED_DIRNAME).chmod(0o750)
    result = runner.invoke(
        main,
        [*base, "--owner", f"{B}={ME.uid}", "--shared-group", str(NOT_ME.gid)],
        env=ON,
    )
    assert result.exit_code == 1, result.output
    assert f"{SHARED_DIRNAME}: group" in result.output


def test_the_command_says_what_it_could_not_list(tmp_path: Path) -> None:
    if _can_list_a_closed_folder(tmp_path):
        pytest.skip("this process can list a 0o000 folder (root or CAP_DAC_OVERRIDE)")
    root = _household(tmp_path)
    closed = principal_dir(root, B) / "log"
    closed.chmod(0o000)
    args = ["profiles", "owners", "--root", str(root)]
    for who in (A, B):
        args += ["--owner", f"{who}={ME.uid}:{ME.gid}"]
    try:
        result = CliRunner().invoke(main, args, env=ON)
    finally:
        closed.chmod(0o700)
    assert result.exit_code == 1, result.output
    assert "not listed" in result.output
    assert "clean" not in result.output


# --- the Linux test cannot pass by skipping ----------------------------------------------------


def _without_useradd(tmp_path: Path) -> dict[str, str]:
    """An environment whose PATH holds no ``useradd``, ``userdel`` or ``sudo``: the isolation
    tests' reason to skip, without touching the machine's real users."""
    empty = tmp_path / "bin"
    empty.mkdir()
    env = dict(os.environ)
    env["PATH"] = str(empty)
    env.pop(REQUIRE_OS_USERS, None)
    return env


def _pytest_isolation(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", str(ISOLATION_TESTS)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_isolation_tests_skip_with_the_reason_when_no_user_can_be_made(tmp_path: Path) -> None:
    result = _pytest_isolation(_without_useradd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" not in result.stdout.splitlines()[-1]
    assert "skipped" in result.stdout
    assert "useradd" in result.stdout
    assert f"{REQUIRE_OS_USERS}=1" in result.stdout, "the skip must say how to make it a failure"


def test_the_isolation_tests_fail_rather_than_skip_when_users_are_required(tmp_path: Path) -> None:
    """A skip on the owner's Mac would look like a pass. With ``WALLE_REQUIRE_OS_USERS=1`` set,
    no way to make a user is a failure that says so."""
    env = _without_useradd(tmp_path)
    env[REQUIRE_OS_USERS] = "1"
    result = _pytest_isolation(env)
    assert result.returncode != 0
    assert "skipped" not in result.stdout.splitlines()[-1]
    assert f"{REQUIRE_OS_USERS}=1" in result.stdout
    assert "proves nothing" in result.stdout


def test_the_isolation_tests_document_the_one_command_for_linux() -> None:
    text = ISOLATION_TESTS.read_text()
    assert f"{REQUIRE_OS_USERS}=1 uv run pytest tests/test_osusers_isolation.py -v" in text
    assert REQUIRE_OS_USERS == "WALLE_REQUIRE_OS_USERS"


def test_the_isolation_module_removes_every_user_it_makes() -> None:
    """By source: each ``useradd`` has a ``userdel`` in a ``finally``."""
    tree = ast.parse(ISOLATION_TESTS.read_text())
    finals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.finalbody:
            finals.extend(ast.unparse(stmt) for stmt in node.finalbody)
    assert any("_remove_user" in text for text in finals), "no userdel in a finally"


def test_the_module_is_pure_of_models() -> None:
    from test_constraints import MODEL_OR_NETWORK, forbidden_in_closure

    assert forbidden_in_closure(PACKAGE, ("osusers",)) == {}
    assert not (set(sys.modules) & MODEL_OR_NETWORK & {"osusers"})


def test_the_public_surface_is_small() -> None:
    assert set(osusers.__all__) >= {
        "Finding",
        "Owner",
        "Problem",
        "Report",
        "check",
        "enabled",
        "OsUsersOffError",
    }
    assert not any(name.startswith(("make", "create", "add", "set")) for name in osusers.__all__)
    assert isinstance(Finding("x", Problem.MODE, "a", "b", None), Finding)
