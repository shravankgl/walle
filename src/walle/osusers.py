"""The OS-user permissions check: does the household's layout on disk match D110 (D32)?

D110 strengthens D32 for Phase 3: each principal's folder, ``principals/p-<name>/``, is owned
by that principal's own OS user and group, so that a process running as one principal cannot
read another's folder — the kernel refuses, not a resolver. The parents (``principals/`` and
the root) may be passed through but not listed; ``shared/`` belongs to a common group.

**What this module is, and is not.** The owner's decision of 2026-11-05: *build a permissions
check and a Linux test only.* So this is a **read-only** check. It ``lstat``\\s and lists; it
opens no file, follows no link, changes no owner, mode or user, and spawns nothing. Creating
the users, handing each folder to its user, and the ``sudo`` rule a trusted command would need
are steps the owner runs himself, if a second person ever uses walle. Nothing here calls
``sudo``, ``dscl`` or ``useradd``, and ``tests/test_osusers.py`` holds the module to that by
AST.

Owners are **declared, never guessed**. ``check`` takes the principals the household has and,
for each, the ``Owner`` (uid, gid) its folder must have. A folder on disk for a principal nobody
declared is a finding; so is a declared principal with no folder. The module does not read
``/etc/passwd`` to decide who *should* own a folder — ``Owner.parse`` turns a user name the
owner typed into numbers, and that is all it does with it.

The rules, each one a mask of bits a place must not have:

- a principal's folder and everything in it: ``PRIVATE_MASK`` (``0o077``) — no permission of
  any kind for group or others. The group *is* the principal's own, so a group bit would be
  harmless today and a leak the day a second member joins that group; the rule stays simple;
- ``principals/`` and the root: ``PASSTHROUGH_MASK`` (``0o066``) — others may pass through
  (``x``) to reach their own folder, never list (``r``) the household's names or write
  (``w``) a folder into it;
- ``shared/`` and everything in it, when a household group is given: ``SHARED_MASK``
  (``0o027``) — the household group reads and passes through, nobody else does, and only the
  owner of each file writes. Judged only when ``check`` is given the group, because a Phase 0
  household has no ``shared/`` group to judge against.

A symlink is judged by its owner only: Linux reports ``0o777`` for every link, and where it
points is the kernel's business under D110 and ``profiles.Profile.resolve``'s before then. A
profile folder that is itself a link, or a ``principals/`` that is, is a finding, and the check
does not enter it (the same refusal ``profiles`` gives).

**A folder the check cannot list is reported as unlisted, never called clean.** Run as one
principal, the check sees every top folder's owner and mode (``principals/`` lets it pass
through) and reports every other principal's inside as unlisted; ``Report.clean`` is false.
Run as the admin — root, by the owner's hand — it sees everything. A check that did not look
must not say the household is fine (hard constraint: status messages that lie).

The dormant rule
----------------
Off unless the owner sets ``WALLE_OSUSERS=on`` — that one spelling — outside the log, where no
event the agent appends can reach (D117). While off, ``check`` raises ``OsUsersOffError``
before its first ``lstat``, and ``walle profiles owners`` refuses and prints how to turn it on.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from walle.paths import PRINCIPAL_PREFIX, PRINCIPALS_DIRNAME, check_name, principal_dir
from walle.profiles import SHARED_DIRNAME

__all__ = [
    "OFF_MESSAGE",
    "PASSTHROUGH_MASK",
    "PRIVATE_MASK",
    "REQUIRE_OS_USERS",
    "SHARED_MASK",
    "SWITCH",
    "SWITCH_ON",
    "Finding",
    "OsUsersOffError",
    "Owner",
    "Problem",
    "Report",
    "check",
    "enabled",
    "group_id",
]

SWITCH = "WALLE_OSUSERS"
"""The environment variable the owner sets to turn the check on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that counts."""

OFF_MESSAGE = f"the OS-user check is off: the owner turns it on with {SWITCH}={SWITCH_ON}"

REQUIRE_OS_USERS = "WALLE_REQUIRE_OS_USERS"
"""Set to ``1``, the Linux isolation test fails instead of skipping where no user can be made,
so a skip cannot pass as a run. Read by ``tests/test_osusers_isolation.py``, not by this
module: the check itself makes no users anywhere."""

PRIVATE_MASK = 0o077
"""Bits a principal's folder, and everything in it, must not have: anything for group or others."""

PASSTHROUGH_MASK = 0o066
"""Bits ``principals/`` and the root must not have: listable or writable by group or others.
Pass-through (``x``) is allowed, so each principal reaches their own folder."""

SHARED_MASK = 0o027
"""Bits ``shared/`` and everything in it must not have: anything for others, and write for the
household group. The group reads capabilities; only each file's owner writes one."""


class OsUsersOffError(RuntimeError):
    """The check was asked for while it is off. Nothing was looked at."""


class Problem(StrEnum):
    """What a finding is about."""

    OWNER = "owner"
    GROUP = "group"
    MODE = "mode"
    LINK = "link"
    MISSING = "missing"
    UNDECLARED = "undeclared"


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the check on.

    Off unless ``WALLE_OSUSERS`` is exactly ``on``. Reads the process environment when no
    mapping is given; a mapping is the whole answer when one is.
    """
    env = os.environ if environ is None else environ
    return env.get(SWITCH) == SWITCH_ON


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise OsUsersOffError(OFF_MESSAGE)


@dataclass(frozen=True, slots=True)
class Owner:
    """Who a principal's folder must belong to: a uid and a gid, as numbers."""

    uid: int
    gid: int

    @classmethod
    def parse(cls, spec: str) -> Owner:
        """``user[:group]``, each a number or a name. A name is looked up in the system's
        user and group databases — read-only — and a bare user takes that user's primary
        group. ``ValueError`` for anything else, naming what was not found."""
        user, sep, group = spec.partition(":")
        if not user or (sep and not group):
            raise ValueError(f"an owner is user[:group], not {spec!r}")
        uid, primary = _user(user)
        gid = group_id(group) if group else (primary if primary is not None else uid)
        return cls(uid, gid)


def _user(text: str) -> tuple[int, int | None]:
    """``(uid, primary gid)``; the gid is ``None`` when the user was given as a number."""
    if text.isdigit():
        return int(text), None
    import pwd  # Unix only; imported here so the module imports everywhere

    try:
        entry = pwd.getpwnam(text)
    except KeyError:
        raise ValueError(f"no such user: {text!r}") from None
    return entry.pw_uid, entry.pw_gid


def group_id(text: str) -> int:
    """A gid from a number or a group name, read-only. ``ValueError`` when not found."""
    if text.isdigit():
        return int(text)
    import grp  # Unix only; imported here so the module imports everywhere

    try:
        return grp.getgrnam(text).gr_gid
    except KeyError:
        raise ValueError(f"no such group: {text!r}") from None


@dataclass(frozen=True)
class Finding:
    """One thing on disk that is not as D110 wants it. Names and metadata only: no bytes of
    any file, and never where a link points."""

    path: str
    """Relative to the root, POSIX-style; ``.`` is the root itself."""
    problem: Problem
    expected: str
    actual: str
    principal: str | None
    """Whose folder it is in, or ``None`` for ``principals/``, the root and ``shared/``."""


@dataclass(frozen=True)
class Report:
    """What ``check`` found. ``clean`` only when everything was looked at and nothing is wrong."""

    root: Path
    findings: tuple[Finding, ...]
    unlisted: tuple[str, ...]
    """Folders the check could not list, relative to the root. Their insides were not judged."""
    checked: int
    """Entries whose owner and mode were looked at."""

    @property
    def clean(self) -> bool:
        return not self.findings and not self.unlisted


class _Check:
    """One run of the check over one root. Collects findings; opens nothing."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.findings: list[Finding] = []
        self.unlisted: list[str] = []
        self.checked = 0

    def _rel(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        return rel or "."

    def _found(
        self, path: Path, problem: Problem, expected: str, actual: str, who: str | None
    ) -> None:
        self.findings.append(Finding(self._rel(path), problem, expected, actual, who))

    def _lstat(self, path: Path) -> os.stat_result | None:
        """The entry's own metadata, following no link. ``None`` when it is not there; a folder
        that cannot be reached is recorded as unlisted."""
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None
        except PermissionError:
            # The folder holding the entry could not be passed through, so it is the one
            # whose contents went unjudged. The root has no parent to blame.
            self.unlisted.append(self._rel(path if path == self.root else path.parent))
            return None

    def _judge(
        self,
        path: Path,
        st: os.stat_result,
        *,
        mask: int,
        rule: str,
        who: str | None,
        uid: int | None,
        gid: int | None,
    ) -> None:
        """Owner, group and mode of one entry against what is expected of its place."""
        self.checked += 1
        if uid is not None and st.st_uid != uid:
            self._found(path, Problem.OWNER, f"uid {uid}", f"uid {st.st_uid}", who)
        if gid is not None and st.st_gid != gid:
            self._found(path, Problem.GROUP, f"gid {gid}", f"gid {st.st_gid}", who)
        if stat.S_ISLNK(st.st_mode):
            return  # a link has no mode of its own worth judging
        if stat.S_IMODE(st.st_mode) & mask:
            self._found(path, Problem.MODE, rule, stat.filemode(st.st_mode), who)

    def _tree(
        self,
        folder: Path,
        *,
        mask: int,
        rule: str,
        who: str | None,
        uid: int | None,
        gid: int | None,
    ) -> None:
        """Every entry below ``folder`` (not the folder itself), following no link."""

        def could_not_list(error: OSError) -> None:
            where = Path(error.filename) if error.filename else folder
            self.unlisted.append(self._rel(where))

        for here, dirs, files in os.walk(folder, onerror=could_not_list, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(here) / name
                st = self._lstat(path)
                if st is not None:
                    self._judge(path, st, mask=mask, rule=rule, who=who, uid=uid, gid=gid)

    def principals(self, owners: Mapping[str, Owner]) -> None:
        principals = self.root / PRINCIPALS_DIRNAME
        if (st := self._lstat(self.root)) is not None:
            self._judge(
                self.root,
                st,
                mask=PASSTHROUGH_MASK,
                rule=_PASSTHROUGH,
                who=None,
                uid=None,
                gid=None,
            )
        st = self._lstat(principals)
        if st is None:
            self._found(principals, Problem.MISSING, "a folder", "nothing there", None)
            return
        if stat.S_ISLNK(st.st_mode):
            self._found(principals, Problem.LINK, "a folder of its own", "a link", None)
            return
        self._judge(
            principals, st, mask=PASSTHROUGH_MASK, rule=_PASSTHROUGH, who=None, uid=None, gid=None
        )
        declared = {f"{PRINCIPAL_PREFIX}{name}" for name in owners}
        try:
            for name in sorted(os.listdir(principals)):
                if name not in declared:
                    self._found(
                        principals / name,
                        Problem.UNDECLARED,
                        "a folder of a declared principal",
                        "a name no owner was declared for",
                        None,
                    )
        except PermissionError:
            self.unlisted.append(self._rel(principals))
        for who, owner in owners.items():
            self.principal(who, owner)

    def principal(self, who: str, owner: Owner) -> None:
        folder = principal_dir(self.root, who)
        st = self._lstat(folder)
        if st is None:
            self._found(folder, Problem.MISSING, "a folder", "nothing there", who)
            return
        if stat.S_ISLNK(st.st_mode):
            self._found(folder, Problem.LINK, "a folder of its own", "a link", who)
            return
        if not stat.S_ISDIR(st.st_mode):
            self._found(folder, Problem.MISSING, "a folder", "not a folder", who)
            return
        self._judge(
            folder, st, mask=PRIVATE_MASK, rule=_PRIVATE, who=who, uid=owner.uid, gid=owner.gid
        )
        self._tree(folder, mask=PRIVATE_MASK, rule=_PRIVATE, who=who, uid=owner.uid, gid=owner.gid)

    def shared(self, gid: int) -> None:
        folder = self.root / SHARED_DIRNAME
        st = self._lstat(folder)
        if st is None:
            return  # nothing shared yet is nothing wrong
        if stat.S_ISLNK(st.st_mode):
            self._found(folder, Problem.LINK, "a folder of its own", "a link", None)
            return
        if not stat.S_ISDIR(st.st_mode):
            self._found(folder, Problem.MISSING, "a folder", "not a folder", None)
            return
        self._judge(folder, st, mask=SHARED_MASK, rule=_SHARED, who=None, uid=None, gid=gid)
        self._tree(folder, mask=SHARED_MASK, rule=_SHARED, who=None, uid=None, gid=gid)

    def report(self) -> Report:
        return Report(
            self.root,
            tuple(sorted(self.findings, key=lambda f: (f.path, f.problem))),
            tuple(sorted(set(self.unlisted))),
            self.checked,
        )


_PRIVATE = "no permission for group or others"
_PASSTHROUGH = "pass-through only: not listable or writable by group or others"
_SHARED = "readable by the household group only, writable by nobody else"


def check(
    root: Path | str,
    owners: Mapping[str, Owner],
    *,
    shared: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> Report:
    """Read-only: is the household under ``root`` laid out as D110 wants?

    ``owners`` maps each principal (unprefixed, validated by ``paths.check_name``) to the
    ``Owner`` its folder must have. ``shared`` is the household group's gid; when given,
    ``shared/`` is judged too. Needs the switch: ``OsUsersOffError`` before the first look.

    Returns a ``Report``; raises only for a bad principal name (``ValueError``) or an
    ``OSError`` that is neither "not there" nor "not allowed", which is something the owner
    should see as it is.
    """
    _require_on(environ)
    for who in owners:
        check_name(who, kind="principal")
    run = _Check(Path(root))
    run.principals(owners)
    if shared is not None:
        run.shared(shared)
    return run.report()
