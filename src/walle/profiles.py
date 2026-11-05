"""Household profiles: one folder per principal, one context per job, and no read path across
(D27, D28, D32, D33, D113).

Phase 0 built one principal. This module is what a second one needs, and it adds no new way
to reach data: it narrows the ways that already exist.

The context (D113)
------------------
A ``Context`` is who a run acts for: the root, the principal, the device stream it writes to
(when it writes), and the role the kernel attached to the speaker (D27, D61). It is built by
``bind`` and validated whole: the names go through ``walle.paths.check_name`` and an empty
principal is ``EmptyContextError``, not a default. **There is no default.** The module keeps
no context variable, reads no environment for a name, and has no ``current()``: a job that
wants its principal is handed one, as an argument, by the caller that knows it. A job started
through ``run`` or ``spawn`` with ``None`` — or with a string that merely looks like a name —
raises before the job's first line. D113's words: the default profile is the most likely wrong
answer, not the safest one.

The profile (D32, hard constraint 6)
------------------------------------
A ``Profile`` is everything a run may touch of its own principal, and it is built from a
``Context`` and nothing else. Every path it gives out is inside ``principals/p-<name>/``.

The rule that holds it together, after two review rounds found gaps between the path that
was checked and the path that was used: **resolve once, decide on the resolved path, act on
that same path.** ``resolve`` takes a folder-relative name and returns where it lands, as
components below the profile's real folder, both as spelled and case-folded. Every decision
after — is it inside, is it data, what to open — is made on those components. It refuses,
with ``CrossPrincipalError``, before anything is opened:

- a name that is absolute, empty, ``.``, or has any ``..`` segment (a profile path never
  needs one, so none is allowed rather than some being normalised away);
- a name whose walk — ``walle.floors``' link walk, the one this project already has, not a
  second one — meets a link that is not strictly inside the folder, or a link whose own
  landing is not strictly inside it (a link to ``..`` that the rest of the name climbs back
  down from), or that lands anywhere not **strictly** inside the folder: the folder itself,
  an ancestor (``principals/``, the root, ``/``), a sibling principal, ``shared/``, or the
  target a dangling link would *create* elsewhere;
- a walk that cannot finish — a loop, a name that cannot be read — because where it lands is
  then unknown (D86: fail closed);
- a profile folder that is itself a link, or a ``principals/`` that is: the same refusal
  ``add_profile`` and ``delete_profile`` give.

``path`` returns the resolved landing, never the name. ``open`` opens the resolved
components one by one relative to the folder's own descriptor, each ``O_NOFOLLOW``, so no
link is followed at open time at all: the decision followed the links, and the act follows
none. A link that has appeared since is the layout having changed under the decision, and is
refused rather than followed. Names are compared case-folded throughout (``HOT/`` is ``hot/``
on APFS and NTFS, and so it is here, on every filesystem).

The role is on the context and ``path`` does not look at it. There is no ``read_as``, no
``open_for``, no parameter that names a second principal on anything that reads. An admin's
profile is the admin's own folder, exactly as an operator's is, and the tests check this by
signature as well as by behaviour. That is the owner's decision of 2026-11-05: nobody reads
another principal's folder, not even an admin.

Fusion (D28)
------------
``Profile.records`` is the index of one folder, and ``Profile.resolution`` is the fold of
those records. The Curator already reads one folder per run (``walle.curator.curate`` takes
one principal); D32 made D28 a filesystem fact, and this module adds the handle a background
job reads through so the fact holds for jobs too. Nothing here, and nothing in the Curator,
takes two principals.

Roles (D27)
-----------
Three, not an ACL system: ``operator``, ``stakeholder``, ``admin``. The role is a claim the
kernel attaches to a context per message (D61, ARCHITECTURE §7), not a file this module keeps.
What a role unlocks here is exactly two things, both for an admin and both whole-folder:
``add_profile`` makes a new principal's empty folder, and ``delete_profile`` removes one
(D32 rule 5: deleting a person is deleting a folder; the crypto-shred of D32). Deletion
unlinks by name and **opens no file**: the admin's power is to end a profile, not to see it.
A profile folder that is itself a link is refused, so deleting a profile never reaches behind
the link into whatever it points at.

Sharing (D32 rule 1, D33)
-------------------------
``publish`` copies one capability file from the publisher's **own** folder into
``shared/<kind>/``. The source goes through ``Profile.resolve``, so the only files that can
be published are the publisher's, whatever their role: that is how "only the folder's owner
can share" is a shape rather than a check. ``shared/`` holds the *how*, never the *what*
(D33): the kinds are ``skills``, ``mcp`` and ``hooks``, and a source whose **resolved,
case-folded** location is data by place (``log/``, ``hot/``, ``grants/``, ``keys/``, the
index) or by shape (``.jsonl``, ``.sqlite``, ``.lock``) is ``SharedDataError`` — so
``skills -> hot`` and ``HOT/`` are both refused, because the question is where the bytes are,
not what the name says. Every check, the source open included, comes before the first write;
the first write is the kind folder, made and entered relative to the root's descriptor with
no link followed, and the file is created exclusively. A name already in ``shared/`` is never
replaced (hard constraint 2): a new version is a new file. ``Shared`` knows only capability
paths — it has no notion of a log, a stream or a memory under ``shared/`` — and
``audit_shared`` lists anything there that is not a capability, by name only, ``shared/``
itself included when it is a link.

Publication is a person's act. ``walle.floors`` already refuses the *agent* every write to
``shared/`` in every mode (D72); this is the human path those floors leave open, and it is
reached only through the command.

The dormant rule
----------------
Nothing that writes runs unless the owner sets ``WALLE_PROFILES=on`` — that one spelling —
outside the log, where no event the agent appends can reach (D117). ``add_profile``,
``delete_profile`` and ``publish`` raise ``ProfilesOffError`` before touching anything. The
parts that refuse — ``Context``, ``Profile``, ``run``, ``spawn`` — take no environment and
have no switch, for the reason ``walle.floors`` gives: isolation that can be turned off is not
isolation.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from walle import index
from walle.floors import _walk as walk_links  # the one link walk; see the module docstring
from walle.paths import (
    HOT_DIRNAME,
    INDEX_FILENAME,
    INDEX_LOCK_FILENAME,
    LOG_DIRNAME,
    check_name,
    hot_dir,
    index_path,
    log_dir,
    principal_dir,
    stream_path,
)
from walle.resolver import Resolution, resolve
from walle.writer import StreamWriter

if TYPE_CHECKING:
    from walle.reader import Record

__all__ = [
    "CAPABILITY_KINDS",
    "OFF_MESSAGE",
    "SWITCH",
    "SWITCH_ON",
    "Added",
    "CapabilityKind",
    "Context",
    "CrossPrincipalError",
    "Deleted",
    "EmptyContextError",
    "Job",
    "Profile",
    "ProfilesOffError",
    "Published",
    "Role",
    "RoleError",
    "Shared",
    "SharedDataError",
    "Stray",
    "add_profile",
    "audit_shared",
    "bind",
    "delete_profile",
    "enabled",
    "publish",
    "run",
    "spawn",
]

SWITCH = "WALLE_PROFILES"
"""The environment variable the owner sets to turn the profile commands on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that counts."""

OFF_MESSAGE = f"profiles are off: the owner turns them on with {SWITCH}={SWITCH_ON}"

SHARED_DIRNAME = "shared"

type CapabilityKind = Literal["skills", "mcp", "hooks"]
CAPABILITY_KINDS: tuple[CapabilityKind, ...] = ("skills", "mcp", "hooks")
"""What ``shared/`` may hold (D33): the how, never the what."""

DATA_PLACES = frozenset({LOG_DIRNAME, HOT_DIRNAME, "grants", "keys"})
"""Folders of a profile whose contents are data or permission, never a capability."""

DATA_SUFFIXES = frozenset({".jsonl", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".lock"})
"""File shapes that are a log, an index or a lock wherever they sit."""

HOP_LIMIT = 40
"""Links followed before a walk is called a loop and refused (``walle.floors.HOP_LIMIT``)."""


class Role(StrEnum):
    """D27's three roles."""

    OPERATOR = "operator"
    STAKEHOLDER = "stakeholder"
    ADMIN = "admin"


class ProfilesOffError(RuntimeError):
    """A profile command was asked for while profiles are off. Nothing was touched."""


class EmptyContextError(Exception):
    """A job or a profile was started without a principal bound (D113). Nothing ran."""


class CrossPrincipalError(Exception):
    """A path that would leave the profile's folder: by name, through a link, or to somewhere
    that cannot be resolved. Nothing was read or written."""


class RoleError(Exception):
    """The context's role does not have this power."""


class SharedDataError(Exception):
    """Something that is not a capability was offered to ``shared/`` (D33)."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the profile commands on.

    Off unless ``WALLE_PROFILES`` is exactly ``on``. Reads the process environment when no
    mapping is given; a mapping is the whole answer when one is.
    """
    env = os.environ if environ is None else environ
    return env.get(SWITCH) == SWITCH_ON


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise ProfilesOffError(OFF_MESSAGE)


# --- the context (D113) --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Context:
    """Who a run acts for. Built by ``bind``; validated whole on construction."""

    root: Path
    principal: str
    device: str | None = None
    role: Role = Role.OPERATOR

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise EmptyContextError(
                "a context needs a principal; none was bound (D113). There is no default "
                "profile to fall back to."
            )
        check_name(self.principal, kind="principal")
        if self.device is not None:
            check_name(self.device, kind="device")
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "role", Role(self.role))

    def writer(self) -> StreamWriter:
        """The stream this context appends to. ``EmptyContextError`` when no device is bound:
        a job that writes must say where."""
        if self.device is None:
            raise EmptyContextError(
                f"the context for {self.principal!r} has no device bound, so it cannot write"
            )
        return StreamWriter(self.root, self.principal, self.device)


def bind(
    root: Path | str, principal: str, *, device: str | None = None, role: Role = Role.OPERATOR
) -> Context:
    """The one way a caller binds a principal for a job. Raises before anything is touched."""
    return Context(root=Path(root), principal=principal, device=device, role=role)


def _require_context(context: object) -> Context:
    if not isinstance(context, Context):
        raise EmptyContextError(
            f"a job needs a bound Context, got {type(context).__name__} (D113). "
            "Use walle.profiles.bind(root, principal)."
        )
    return context


type Job[T] = Callable[[Context], T]


def run[T](context: Context, job: Job[T]) -> T:
    """Run ``job`` for ``context``, now, on this thread. ``EmptyContextError`` when the context
    is missing, before ``job`` is called. The job's own exceptions propagate as they are."""
    bound = _require_context(context)
    return job(bound)


def spawn(
    context: Context, job: Job[Any], *, name: str | None = None, daemon: bool = False
) -> threading.Thread:
    """Start ``job`` for ``context`` on a new thread and return it, started. The context is
    checked here, on the caller's thread, so a missing one fails where it can be seen rather
    than inside a worker nobody is watching. Not a daemon unless asked: a thread the
    interpreter kills mid-append is how a torn tail happens."""
    bound = _require_context(context)
    thread = threading.Thread(
        target=job, args=(bound,), name=name or f"walle-{bound.principal}", daemon=daemon
    )
    thread.start()
    return thread


# --- the profile (D32, hard constraint 6) --------------------------------------------------------


def _inside(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or under it, compared as ``walle.floors`` compares:
    whole segments, without regard to case."""
    if path.lower() == folder.lower():
        return True
    base = folder.rstrip(os.sep) + os.sep
    return path[: len(base)].lower() == base.lower()


def _strictly_inside(path: str, folder: str) -> bool:
    return _inside(path, folder) and path.lower() != folder.lower()


_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
"""How a folder is opened before it is entered: never through a link."""

_CHANGED = (errno.ELOOP, errno.ENOTDIR)
"""What ``open`` with ``O_NOFOLLOW`` reports when a link now stands where a resolved component
was: the layout changed between the decision and the act, and the act is refused."""


@dataclass(frozen=True, slots=True)
class Resolved:
    """Where a profile-relative name lands, decided once and used for every decision after.

    ``parts`` are the landing's components below the profile's real folder, as spelled on
    disk; ``folded`` is the same, lower-cased, for every rule that compares names (the
    filesystems walle ships on fold case, so ``HOT/`` is ``hot/`` there). ``real`` is the
    landing as an absolute path. Nothing has been opened.
    """

    given: str
    folder: str
    parts: tuple[str, ...]

    @property
    def real(self) -> str:
        return os.path.join(self.folder, *self.parts)

    @property
    def folded(self) -> tuple[str, ...]:
        return tuple(p.lower() for p in self.parts)


def _open_below(folder: str, parts: tuple[str, ...], flags: int, *, mkdirs: bool = False) -> int:
    """Open ``folder/parts`` following **no** link at any component: the folder is opened
    ``O_NOFOLLOW|O_DIRECTORY``, each component below it likewise relative to the one before,
    and the last with ``flags | O_NOFOLLOW``. The caller decided on resolved components, so
    every one of them is a real folder or file; a link found here is the layout having
    changed since, and is ``CrossPrincipalError``. With ``mkdirs`` the folders on the way are
    made, relative to their parent's descriptor, when missing. Returns the final descriptor."""
    try:
        fd = os.open(folder, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in _CHANGED:
            raise CrossPrincipalError(f"{folder}: a link now stands where the folder was") from None
        raise
    try:
        for part in parts[:-1]:
            if mkdirs:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=fd)
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in _CHANGED:
                    raise CrossPrincipalError(
                        f"{part!r} under {folder}: a link now stands where a folder was; refused"
                    ) from None
                raise
            os.close(fd)
            fd = child
        try:
            return os.open(parts[-1], flags | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        except OSError as exc:
            if exc.errno in _CHANGED:
                raise CrossPrincipalError(
                    f"{parts[-1]!r} under {folder}: a link now stands where a file was; refused"
                ) from None
            raise
    finally:
        os.close(fd)


_MODES: dict[str, int] = {
    "r": os.O_RDONLY,
    "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    "x": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
}


class Profile:
    """Everything a run may touch of its own principal. Built from a ``Context``, only."""

    def __init__(self, context: Context) -> None:
        self.context = _require_context(context)

    @property
    def principal(self) -> str:
        return self.context.principal

    @property
    def folder(self) -> Path:
        """``<root>/principals/p-<name>``."""
        return principal_dir(self.context.root, self.context.principal)

    @property
    def log_dir(self) -> Path:
        return log_dir(self.context.root, self.context.principal)

    @property
    def stream_path(self) -> Path | None:
        """The stream this context writes to, or ``None`` when no device is bound."""
        if self.context.device is None:
            return None
        return stream_path(self.context.root, self.context.principal, self.context.device)

    @property
    def index_path(self) -> Path:
        return index_path(self.context.root, self.context.principal)

    @property
    def hot_dir(self) -> Path:
        return hot_dir(self.context.root, self.context.principal)

    def _real_folder(self) -> str:
        """The profile folder's real path. Neither it nor ``principals/`` may be a link: a
        profile whose folder points elsewhere has nothing of its own to read, and a linked
        ``principals/`` moves every profile at once (the same refusal ``add_profile`` and
        ``delete_profile`` give)."""
        folder = self.folder
        if folder.parent.is_symlink():
            raise CrossPrincipalError(f"{folder.parent}: principals/ is a link; refused")
        if folder.is_symlink():
            raise CrossPrincipalError(
                f"{self.principal!r}: the profile folder is a link, so its paths would land "
                "elsewhere; refused"
            )
        try:
            parent = os.path.realpath(folder.parent, strict=True)
        except OSError as exc:
            raise CrossPrincipalError(
                f"{self.principal!r}: the profile folder cannot be resolved ({exc.strerror})"
            ) from None
        return os.path.join(parent, folder.name)

    def resolve(self, relative: str | os.PathLike[str]) -> Resolved:
        """Where ``relative`` lands inside this profile's folder, decided once.

        Refused with ``CrossPrincipalError``, before anything is opened: a name that is
        absolute, empty, ``.``, or has a ``..`` segment (a profile path never needs one);
        then, walking the name with ``walle.floors``' link walk, any link met that is not
        itself inside the folder, a landing that is not **strictly** inside the folder — the
        folder itself, an ancestor, a sibling principal, ``shared/``, anywhere else — and a
        walk that cannot finish (a loop, a name that cannot be read).
        """
        given = os.fspath(relative)
        segments = [p for p in given.split(os.sep) if p not in ("", ".")]
        if not segments or os.path.isabs(given) or "\x00" in given:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} is not a name inside the profile folder"
            )
        if ".." in segments:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} climbs with '..'; a profile path never needs to"
            )
        real_folder = self._real_folder()
        walked = walk_links(os.path.join(real_folder, *segments))
        if walked is None:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} cannot be resolved (a link loop, or a link "
                "that cannot be read), so where it lands is unknown; refused"
            )
        hops, landing = walked
        for hop in hops:
            # The link must sit inside the folder, and so must what it points at: a link to
            # an ancestor that the rest of the name climbs back down from would otherwise
            # pass by its landing while standing, for a moment, above the folder.
            hop_lands = walk_links(hop)
            if (
                not _strictly_inside(hop, real_folder)
                or hop_lands is None
                or not _strictly_inside(hop_lands[1], real_folder)
            ):
                raise CrossPrincipalError(
                    f"{self.principal!r}: {given!r} passes through a link that leaves the "
                    "profile folder; refused"
                )
        if not _strictly_inside(landing, real_folder):
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} lands outside the profile folder; refused"
            )
        below = landing[len(real_folder) :].strip(os.sep)
        return Resolved(given, real_folder, tuple(below.split(os.sep)))

    def path(self, relative: str | os.PathLike[str]) -> Path:
        """Where ``relative`` lands, as a path: the resolved one, so what is checked is what
        is used. ``CrossPrincipalError`` as ``resolve`` gives."""
        return Path(self.resolve(relative).real)

    def open(self, relative: str | os.PathLike[str], mode: str = "rb") -> IO[Any]:
        """Open where ``relative`` lands, following no link at open time: the resolved
        components are opened one by one relative to the folder's descriptor, each
        ``O_NOFOLLOW``. Modes are ``r``, ``w``, ``a``, ``x``, binary or text."""
        kind, text = mode.replace("b", ""), "b" not in mode
        if kind not in _MODES:
            raise ValueError(f"mode {mode!r} is not one of r, w, a, x (with or without b)")
        resolved = self.resolve(relative)
        fd = _open_below(resolved.folder, resolved.parts, _MODES[kind])
        return open(fd, mode if not text else kind, encoding="utf-8" if text else None)

    def records(self) -> tuple[Record, ...]:
        """This folder's log, indexed and up to date: the only input fusion gets (D28).

        ``log/`` and every stream file in it are resolved first, so a stream that is a link
        into another folder is refused before the index reads a byte of it.
        """
        log = self.path(LOG_DIRNAME)
        if log.is_dir():
            for entry in sorted(os.listdir(log)):
                self.resolve(os.path.join(LOG_DIRNAME, entry))
        return index.update(self.context.root, self.context.principal).records

    def resolution(self) -> Resolution:
        """The fold of this folder's log."""
        return resolve(self.records())


# --- the admin's two powers (D27, D32 rule 5) ---------------------------------------------------


def _require_admin(context: Context, power: str) -> Context:
    bound = _require_context(context)
    if bound.role is not Role.ADMIN:
        raise RoleError(f"{power} needs the admin role; {bound.principal!r} is {bound.role}")
    return bound


@dataclass(frozen=True)
class Added:
    """A new, empty profile folder."""

    principal: str
    folder: Path


def add_profile(
    context: Context, principal: str, *, environ: Mapping[str, str] | None = None
) -> Added:
    """Make ``principals/p-<principal>/log``, empty. Needs the admin role and the switch.
    ``FileExistsError`` when the folder is already there: it is never reused or emptied."""
    _require_on(environ)
    admin = _require_admin(context, "adding a profile")
    check_name(principal, kind="principal")
    folder = principal_dir(admin.root, principal)
    if folder.parent.is_symlink():
        raise CrossPrincipalError(f"{folder.parent}: principals/ is a link; refused")
    if folder.exists() or folder.is_symlink():
        raise FileExistsError(f"{folder}: a profile for {principal!r} already exists")
    (folder / LOG_DIRNAME).mkdir(parents=True)
    return Added(principal, folder)


@dataclass(frozen=True)
class Deleted:
    """What ``delete_profile`` removed: counted by name, never read."""

    principal: str
    folder: Path
    files: int
    links: int
    folders: int


def _remove_tree(fd: int) -> tuple[int, int, int]:
    """Remove everything inside the folder open at ``fd``, by name and relative to that
    descriptor, the way ``shutil.rmtree`` does where it can resist a link swapped in under
    it: a child folder is opened with ``O_NOFOLLOW`` before it is entered, so a folder
    replaced by a link between the listing and the descent is unlinked as a link, never
    followed. Files and links are ``unlink``ed, folders ``rmdir``ed after their contents.
    Nothing is opened for reading. Returns files, links and folders removed below ``fd``."""
    files = links = folders = 0
    with os.scandir(fd) as it:
        entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in it]
    for name, was_dir in entries:
        child = -1
        if was_dir:
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                was_dir = False  # a link or a file now stands where a folder was listed
        if was_dir:
            try:
                f, ln, d = _remove_tree(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
            files, links, folders = files + f, links + ln, folders + d + 1
        elif stat.S_ISLNK(os.lstat(name, dir_fd=fd).st_mode):
            os.unlink(name, dir_fd=fd)
            links += 1
        else:
            os.unlink(name, dir_fd=fd)
            files += 1
    return files, links, folders


def delete_profile(
    context: Context, target: str, *, environ: Mapping[str, str] | None = None
) -> Deleted:
    """Delete the whole folder of principal ``target`` (D32 rule 5). Needs the admin role and
    the switch. Opens no file: the admin may end a profile, not read it.

    ``FileNotFoundError`` when there is no such profile; ``CrossPrincipalError`` when the
    profile folder is a link, so that nothing behind the link is touched.
    """
    _require_on(environ)
    admin = _require_admin(context, "deleting a profile")
    check_name(target, kind="principal")
    folder = principal_dir(admin.root, target)
    if folder.parent.is_symlink():
        raise CrossPrincipalError(f"{folder.parent}: principals/ is a link; refused")
    try:
        fd = os.open(folder, _DIR_FLAGS)
    except FileNotFoundError:
        raise FileNotFoundError(f"{folder}: no profile for {target!r}") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CrossPrincipalError(
                f"{folder}: the profile folder is a link; deleting through it would reach "
                "elsewhere, refused"
            ) from None
        raise
    try:
        files, links, folders = _remove_tree(fd)
    finally:
        os.close(fd)
    os.rmdir(folder)
    return Deleted(target, folder, files, links, folders + 1)


# --- shared/ (D32 rule 1, D33) -------------------------------------------------------------------


def _require_kind(kind: object) -> CapabilityKind:
    if kind not in CAPABILITY_KINDS:
        raise SharedDataError(
            f"shared/ holds capabilities only: {', '.join(CAPABILITY_KINDS)}; not {kind!r} (D33)"
        )
    return kind  # type: ignore[return-value]  # narrowed by the membership test above


class Shared:
    """The capability folders under ``<root>/shared/``, and nothing else there."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    @property
    def folder(self) -> Path:
        return self.root / SHARED_DIRNAME

    def capability_dir(self, kind: CapabilityKind) -> Path:
        return self.folder / _require_kind(kind)

    def capability_path(self, kind: CapabilityKind, name: str) -> Path:
        """``shared/<kind>/<name>``; ``name`` is one file name, not a path."""
        if not name or name in (".", "..") or os.sep in name or "/" in name or "\x00" in name:
            raise ValueError(f"not a capability file name: {name!r}")
        return self.capability_dir(kind) / name


def _data_shaped(folded: tuple[str, ...]) -> str | None:
    """Why a resolved, case-folded profile-relative location is data, or ``None`` when it may
    be a capability. Decided on where a name lands, never on how it was spelled."""
    if not folded:
        return "the profile folder itself"
    if folded[0] in DATA_PLACES:
        return f"{folded[0]}/ holds data, never a capability"
    if folded[-1] in (INDEX_FILENAME, INDEX_LOCK_FILENAME):
        return "the index is derived data"
    name = folded[-1]
    for suffix in DATA_SUFFIXES:
        if name.endswith(suffix):
            return f"a {suffix} file is a log, an index or a lock, not a capability"
    return None


@dataclass(frozen=True)
class Published:
    """One capability copied into ``shared/``."""

    principal: str
    kind: CapabilityKind
    source: Path
    path: Path


def publish(
    context: Context,
    source: str | os.PathLike[str],
    kind: CapabilityKind,
    *,
    environ: Mapping[str, str] | None = None,
) -> Published:
    """Copy ``source``, a file in the publisher's own folder, to ``shared/<kind>/``. Needs the
    switch. Any role may publish; what makes it the owner's act is that the source is
    resolved inside the publisher's profile and nowhere else (D32 rule 1).

    ``SharedDataError`` for a kind that is not a capability, or a source whose resolved,
    case-folded location is data or is not one regular file (D33); ``CrossPrincipalError``
    for a source that lands outside the folder, by name or through a link, or for a
    ``shared/`` that is a link; ``FileExistsError`` when ``shared/<kind>/<name>`` is already
    there (hard constraint 2); ``FileNotFoundError`` for a source that is not there. All of
    these, and the open of the source, come before the first write.
    """
    _require_on(environ)
    profile = Profile(context)
    wanted = _require_kind(kind)
    resolved = profile.resolve(source)
    where = "/".join(resolved.parts)
    if (why := _data_shaped(resolved.folded)) is not None:
        raise SharedDataError(f"{where}: {why} (D33)")
    shared = Shared(profile.context.root)
    destination = shared.capability_path(wanted, resolved.parts[-1])
    for folder in (shared.folder, destination.parent):
        if folder.is_symlink():
            raise CrossPrincipalError(
                f"{folder}: a link where a shared folder should be; publishing through it "
                "would write elsewhere, refused"
            )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"{destination}: already published; a new version is a new file name, nothing in "
            "shared/ is replaced"
        )
    # The source is opened on its resolved components, following no link, before any write.
    src_fd = _open_below(resolved.folder, resolved.parts, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            raise SharedDataError(f"{where}: a capability is one regular file")
        # First write: the kind folder, then the file, both relative to the root's descriptor
        # and following no link; the file is created exclusively.
        out_fd = _open_below(
            os.fspath(shared.root),
            (SHARED_DIRNAME, wanted, resolved.parts[-1]),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mkdirs=True,
        )
    except BaseException:
        os.close(src_fd)
        raise
    with open(src_fd, "rb") as src, open(out_fd, "wb") as out:
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())
    return Published(profile.principal, wanted, Path(resolved.real), destination)


@dataclass(frozen=True)
class Stray:
    """Something under ``shared/`` that is not a capability: its path, relative to
    ``shared/``, and why. Names only; nothing is read."""

    path: str
    reason: str


def audit_shared(root: Path | str) -> tuple[Stray, ...]:
    """Everything under ``shared/`` that D33 does not allow: a top-level entry that is not a
    capability kind, a link anywhere (it could lead into a profile), and a data-shaped file
    anywhere. Reads names only. Empty when ``shared/`` is clean or absent."""
    shared = Shared(root).folder
    if shared.is_symlink():
        return (Stray(".", "shared/ itself is a link; it must be a folder of its own"),)
    if not shared.is_dir():
        return ()
    strays: list[Stray] = []
    found: list[Path] = []
    for here, dirs, files in os.walk(shared, followlinks=False):
        found.extend(Path(here) / name for name in dirs + files)
    for path in sorted(found):
        relative = path.relative_to(shared)
        top = relative.parts[0]
        if top not in CAPABILITY_KINDS:
            strays.append(Stray(relative.as_posix(), f"{top!r} is not a capability kind (D33)"))
            continue
        if path.is_symlink():
            strays.append(Stray(relative.as_posix(), "a link; shared/ holds files only"))
        elif len(relative.parts) == 1:
            continue  # the kind folder itself
        elif (why := _data_shaped(tuple(p.lower() for p in relative.parts[1:]))) is not None:
            strays.append(Stray(relative.as_posix(), why))
    return tuple(strays)
