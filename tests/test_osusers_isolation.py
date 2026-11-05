"""Issue #105's Linux criterion: with real OS users, one principal's process cannot read
another's folder, and the check reports exactly the mistakes that would let it (D110, D32).

    WALLE_REQUIRE_OS_USERS=1 uv run pytest tests/test_osusers_isolation.py -v

These tests need Linux, ``useradd``/``userdel`` and passwordless ``sudo`` — a GitHub Linux
runner has all three; the owner's Mac has none of them, and nothing here calls ``dscl``. Where a
user cannot be made the module **skips, and says why**. ``WALLE_REQUIRE_OS_USERS=1`` turns the
skip into a failure, so a skip cannot pass as a run.

What is created, and removed. The fixture makes two throwaway system users with made-up names
(``wt<pid>a`` and ``wt<pid>b``, no home, no login shell), a household under ``/tmp`` owned by
them, and removes the users and the folder in a ``finally`` whatever the tests did. The only
privileged commands are in this file: ``useradd``, ``userdel``, ``chown`` to hand the fixture to
the throwaway users and back, and ``sudo -u`` to act as one of them. The shipped check
(``walle.osusers``) runs ``lstat`` and nothing else; ``tests/test_osusers.py`` proves that by
AST.

Every byte here is synthetic: the "streams" are one made-up line each.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from walle.osusers import REQUIRE_OS_USERS, SWITCH, SWITCH_ON, Owner, Problem, check
from walle.paths import PRINCIPALS_DIRNAME, principal_dir

COMMAND = f"{REQUIRE_OS_USERS}=1 uv run pytest tests/test_osusers_isolation.py -v"
ON = {SWITCH: SWITCH_ON}
A = "ada"  # a made-up household member
B = "sam"  # another
CANARY_A = "synthetic: ada's one line\n"
CANARY_B = "synthetic: sam's one line\n"


def _why_no_users() -> str | None:
    """Why a throwaway user cannot be made here, or ``None`` when one can."""
    if sys.platform != "linux":
        return f"not Linux ({sys.platform}); the owner's Mac is out by design, dscl is never used"
    for tool in ("useradd", "userdel", "sudo"):
        if shutil.which(tool) is None:
            return f"no {tool} on PATH"
    try:
        probe = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"sudo -n true did not run: {exc}"
    if probe.returncode != 0:
        return f"sudo is not passwordless here: {probe.stderr.strip() or 'refused'}"
    return None


REASON = _why_no_users()
"""Why these tests cannot run here, or ``None``. Probed once, at import."""


@pytest.fixture(scope="module", autouse=True)
def _users_or_an_honest_answer() -> None:
    """Module scope, so it runs before ``household`` and nothing is attempted without users."""
    if REASON is None:
        return
    if os.environ.get(REQUIRE_OS_USERS) == "1":
        pytest.fail(
            f"{REQUIRE_OS_USERS}=1 is set but no OS user can be made here: {REASON}. "
            "This run proves nothing about the OS-user boundary.",
            pytrace=False,
        )
    pytest.skip(
        f"no OS user can be made here ({REASON}); the isolation tests did not run. "
        f"On a Linux machine with useradd and passwordless sudo: {COMMAND}  — "
        f"{REQUIRE_OS_USERS}=1 makes this a failure."
    )


def _sudo(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )


def _make_user(name: str) -> Owner:
    made = _sudo(
        "useradd", "--system", "--user-group", "--no-create-home", "--shell", "/bin/false", name
    )
    assert made.returncode == 0, f"useradd {name}: {made.stderr}"
    entry = pwd.getpwnam(name)
    return Owner(entry.pw_uid, entry.pw_gid)


def _remove_user(name: str) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return  # never made, or already gone
    gone = _sudo("userdel", name)
    assert gone.returncode == 0, f"userdel {name}: {gone.stderr}"
    _sudo("groupdel", name)  # userdel removes the private group when it can; this is belt
    with pytest.raises(KeyError):
        pwd.getpwnam(name)


def _as(user: str, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run a command as ``user``: the kernel's answer, not Python's."""
    return _sudo("-u", user, *argv, timeout=30)


@dataclass(frozen=True)
class RootReport:
    """The check's answer from the root run, as plain data."""

    findings: list[dict[str, Any]]
    unlisted: list[str]
    checked: int


class Household:
    def __init__(self, root: Path, users: dict[str, str], owners: dict[str, Owner]) -> None:
        self.root = root
        self.users = users  # principal -> OS user name
        self.owners = owners  # principal -> Owner

    def stream(self, who: str) -> Path:
        return principal_dir(self.root, who) / "log" / "d-mbp.jsonl"

    def restore(self) -> None:
        """Back to the layout D110 wants, after a test loosened it."""
        for who, owner in self.owners.items():
            folder = principal_dir(self.root, who)
            _sudo("chown", "-R", f"{owner.uid}:{owner.gid}", str(folder))
            _sudo("chmod", "0700", str(folder), str(folder / "log"))
            _sudo("chmod", "0600", str(self.stream(who)))


@pytest.fixture(scope="module")
def household() -> Iterator[Household]:
    """Two throwaway users, two private folders, removed again whatever happens."""
    tag = f"wt{os.getpid() % 1_000_000}"
    users = {A: f"{tag}a", B: f"{tag}b"}
    owners: dict[str, Owner] = {}
    root: Path | None = None
    me = f"{os.getuid()}:{os.getgid()}"
    try:
        for who, name in users.items():
            owners[who] = _make_user(name)
        # Under /tmp, not pytest's tmp_path: pytest's basetemp is 0o700 and the throwaway
        # users could not pass through it to reach their own folders.
        root = Path(tempfile.mkdtemp(prefix="walle-osusers-", dir="/tmp"))
        root.chmod(0o711)
        principals = root / PRINCIPALS_DIRNAME
        principals.mkdir(mode=0o711)
        for who, canary in ((A, CANARY_A), (B, CANARY_B)):
            folder = principal_dir(root, who)
            (folder / "log").mkdir(parents=True, mode=0o700)
            folder.chmod(0o700)
            stream = folder / "log" / "d-mbp.jsonl"
            stream.write_text(canary)
            stream.chmod(0o600)
            owner = owners[who]
            handed = _sudo("chown", "-R", f"{owner.uid}:{owner.gid}", str(folder))
            assert handed.returncode == 0, handed.stderr
        yield Household(root, users, owners)
    finally:
        if root is not None:
            _sudo("chown", "-R", me, str(root))
            shutil.rmtree(root, ignore_errors=True)
            if root.exists():
                _sudo("rm", "-rf", str(root))
        failures = []
        for name in users.values():
            try:
                _remove_user(name)
            except AssertionError as exc:
                failures.append(str(exc))
        assert not failures, failures


def _check_as_root(household: Household, shared: int | None = None) -> RootReport:
    """The check needs to list every folder to call the household clean, and only root can
    list two users' private folders. The check itself is the same code; this is the admin
    running it, which is the one person D110 lets."""
    script = (
        "import json, sys\n"
        "from dataclasses import asdict\n"
        "from walle.osusers import Owner, check\n"
        "root, owners, shared = json.loads(sys.argv[1])\n"
        "report = check(root, {k: Owner(*v) for k, v in owners.items()}, shared=shared)\n"
        "print(json.dumps({'findings': [asdict(f) for f in report.findings],"
        " 'unlisted': list(report.unlisted), 'checked': report.checked}))\n"
    )
    payload = json.dumps(
        [str(household.root), {k: [o.uid, o.gid] for k, o in household.owners.items()}, shared]
    )
    result = _sudo(
        "env", f"{SWITCH}={SWITCH_ON}", sys.executable, "-c", script, payload, timeout=60
    )
    assert result.returncode == 0, result.stderr
    raw = json.loads(result.stdout)
    return RootReport(raw["findings"], raw["unlisted"], raw["checked"])


# --- the criterion ------------------------------------------------------------------------------


def test_the_laid_out_household_is_clean_and_each_user_reads_only_their_own(
    household: Household,
) -> None:
    household.restore()
    report = _check_as_root(household)
    assert report.findings == [], report
    assert report.unlisted == []

    own = _as(household.users[A], "cat", str(household.stream(A)))
    assert own.returncode == 0, f"ada cannot read her own stream: {own.stderr}"
    assert own.stdout == CANARY_A

    other = _as(household.users[A], "cat", str(household.stream(B)))
    assert other.returncode != 0, "ada read sam's stream"
    assert "Permission denied" in other.stderr
    assert CANARY_B.strip() not in other.stdout

    listing = _as(household.users[A], "ls", str(principal_dir(household.root, B)))
    assert listing.returncode != 0, "ada listed sam's folder"
    assert "Permission denied" in listing.stderr

    names = _as(household.users[A], "ls", str(household.root / PRINCIPALS_DIRNAME))
    assert names.returncode != 0, "ada listed the household's principals"

    the_other_way = _as(household.users[B], "cat", str(household.stream(A)))
    assert the_other_way.returncode != 0 and CANARY_A.strip() not in the_other_way.stdout


def test_the_check_reports_the_mode_that_lets_a_neighbour_read(household: Household) -> None:
    """The finding and the leak are the same fact: loosen sam's folder and stream, the check
    names both paths, and ada can now read. Restore, and both go away."""
    household.restore()
    sam = principal_dir(household.root, B)
    try:
        _sudo("chmod", "0755", str(sam), str(sam / "log"))
        _sudo("chmod", "0644", str(household.stream(B)))
        report = _check_as_root(household)
        findings = {(f["path"], f["problem"]) for f in report.findings}
        assert findings == {
            (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.MODE),
            (f"{PRINCIPALS_DIRNAME}/p-{B}/log", Problem.MODE),
            (f"{PRINCIPALS_DIRNAME}/p-{B}/log/d-mbp.jsonl", Problem.MODE),
        }
        leak = _as(household.users[A], "cat", str(household.stream(B)))
        assert leak.returncode == 0 and leak.stdout == CANARY_B, "the finding was not a leak"
        # The unprivileged test process sees the same findings now that the folder is open.
        here = check(household.root, household.owners, environ=ON)
        assert {(f.path, f.problem) for f in here.findings} == findings
    finally:
        household.restore()
    assert _check_as_root(household).findings == []
    assert _as(household.users[A], "cat", str(household.stream(B))).returncode != 0


def test_the_check_reports_a_folder_handed_to_the_wrong_user(household: Household) -> None:
    household.restore()
    sam = principal_dir(household.root, B)
    ada = household.owners[A]
    try:
        _sudo("chown", f"{ada.uid}:{ada.gid}", str(sam))
        report = _check_as_root(household)
        findings = {(f["path"], f["problem"]) for f in report.findings}
        assert findings == {
            (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.OWNER),
            (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.GROUP),
        }
        for finding in report.findings:
            assert finding["principal"] == B
        # Owned by ada and still 0o700: ada may now list it, which is the point of the finding.
        assert _as(household.users[A], "ls", str(sam)).returncode == 0
    finally:
        household.restore()
    assert _check_as_root(household).findings == []


def test_an_unprivileged_check_sees_the_top_folders_and_says_what_it_could_not_list(
    household: Household,
) -> None:
    """Run as the test user, not root: both private folders are closed to it. It still checks
    each top folder's owner and mode, and reports the insides as unlisted rather than clean."""
    household.restore()
    try:
        os.listdir(principal_dir(household.root, B))
    except PermissionError:
        pass
    else:
        pytest.skip("this test process lists another user's 0o700 folder (CAP_DAC_READ_SEARCH)")
    report = check(household.root, household.owners, environ=ON)
    assert report.findings == ()
    assert set(report.unlisted) == {f"{PRINCIPALS_DIRNAME}/p-{A}", f"{PRINCIPALS_DIRNAME}/p-{B}"}
    assert not report.clean
    swapped = {A: household.owners[B], B: household.owners[A]}
    report = check(household.root, swapped, environ=ON)
    assert {(f.path, f.problem) for f in report.findings} == {
        (f"{PRINCIPALS_DIRNAME}/p-{A}", Problem.OWNER),
        (f"{PRINCIPALS_DIRNAME}/p-{A}", Problem.GROUP),
        (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.OWNER),
        (f"{PRINCIPALS_DIRNAME}/p-{B}", Problem.GROUP),
    }


def test_a_throwaway_user_is_removed_again() -> None:
    """The fixture's teardown, exercised on its own so a leak would be seen here and not only
    by the next run's ``useradd`` failing."""
    name = f"wt{os.getpid() % 1_000_000}z"
    owner = None
    try:
        owner = _make_user(name)
        assert owner.uid > 0 and pwd.getpwnam(name).pw_uid == owner.uid
    finally:
        _remove_user(name)
    assert owner is not None
    assert asdict(owner) == {"uid": owner.uid, "gid": owner.gid}
