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
``path`` and ``open`` take a folder-relative name and refuse, with ``CrossPrincipalError``:

- a path that is absolute, empty, or climbs out through ``..``, by name, before anything is
  read;
- a path that reaches another folder **through a link**: every symlink met on the way is
  followed one hop at a time, as ``walle.floors`` does, and a hop that leaves the profile's
  real folder is refused where it is met — a link to another principal's file, a link to their
  folder, a folder of one's own replaced by a link, a dangling link whose target would be
  *created* in another folder, and the profile folder itself replaced by a link;
- a path that cannot be resolved — a link loop, a name that cannot be read — because where it
  lands is then unknown (D86: fail closed).

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
``shared/<kind>/``. The source is resolved through ``Profile.path``, so the only files that
can be published are the publisher's, whatever their role: that is how "only the folder's
owner can share" is a shape rather than a check. ``shared/`` holds the *how*, never the
*what* (D33): the kinds are ``skills``, ``mcp`` and ``hooks``, and a source that is data by
place (``log/``, ``hot/``, ``grants/``, ``keys/``, the index) or by shape (``.jsonl``,
``.sqlite``, ``.lock``) is ``SharedDataError``. A name already in ``shared/`` is never
replaced (hard constraint 2): a new version is a new file. ``Shared`` knows only capability
paths — it has no notion of a log, a stream or a memory under ``shared/`` — and
``audit_shared`` lists anything there that is not a capability, by name only.

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


def _walk(start: str, parts: list[str]) -> list[str] | None:
    """Resolve ``parts`` from the real folder ``start`` one component at a time, following
    each symlink by hand as ``walle.floors._walk`` does. Returns the **trail**: where the walk
    stood after every component and after every link, ending where the path lands — through a
    dangling link, the name it would create. ``None`` when the answer is not knowable: a loop,
    a name that cannot be read, a NUL."""
    trail: list[str] = []
    current = start
    hops = 0
    pending = list(parts)
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = os.path.dirname(current)
            trail.append(current)
            continue
        candidate = os.path.join(current, part)
        try:
            is_link = stat.S_ISLNK(os.lstat(candidate).st_mode)
        except FileNotFoundError:
            is_link = False
        except (OSError, ValueError):
            return None
        if not is_link:
            current = candidate
            trail.append(current)
            continue
        if hops >= HOP_LIMIT:
            return None
        hops += 1
        try:
            target = os.readlink(candidate)
        except (OSError, ValueError):
            return None
        if os.path.isabs(target):
            current = os.sep
        pending = [p for p in target.split(os.sep) if p] + pending
        trail.append(current)
    if not trail:
        trail.append(current)
    return trail


def _inside(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or under it, compared as ``walle.floors`` compares:
    whole segments, without regard to case."""
    if path.lower() == folder.lower():
        return True
    base = folder.rstrip(os.sep) + os.sep
    return path[: len(base)].lower() == base.lower()


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
        """The profile folder's real path. The folder itself must not be a link: a profile
        whose folder points elsewhere has nothing of its own to read."""
        folder = self.folder
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

    def path(self, relative: str | os.PathLike[str]) -> Path:
        """A path inside this profile's folder, checked by name and then through every link it
        passes. ``CrossPrincipalError`` for anything else; nothing is read beyond the names.

        The path returned is the one asked for, under the folder, not its resolution: a link
        that stays inside the folder is fine to use by its own name.
        """
        given = os.fspath(relative)
        if not given.strip() or given.strip() == ".":
            raise CrossPrincipalError(f"{self.principal!r}: a profile path names a file or folder")
        if os.path.isabs(given) or "\x00" in given:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} is not a path inside the profile folder"
            )
        folder = os.fspath(self.folder)
        by_name = os.path.normpath(os.path.join(folder, given))
        if not _inside(by_name, folder) or by_name == folder:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} leaves the profile folder; refused"
            )
        real_folder = self._real_folder()
        trail = _walk(real_folder, given.split(os.sep))
        if trail is None:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} cannot be resolved (a link loop, or a link "
                "that cannot be read), so where it lands is unknown; refused"
            )
        for stood in trail:
            # Standing inside the folder is the point; standing on an ancestor is passing
            # through on the way down an absolute link, and reads nothing of anyone's.
            # Anywhere else — a sibling principal, shared/, outside the root — is refused.
            if not (_inside(stood, real_folder) or _inside(real_folder, stood)):
                raise CrossPrincipalError(
                    f"{self.principal!r}: {given!r} leaves the profile folder through a link; "
                    "refused"
                )
        if trail[-1] == real_folder:
            raise CrossPrincipalError(
                f"{self.principal!r}: {given!r} resolves to the profile folder itself; refused"
            )
        return Path(by_name)

    def open(self, relative: str | os.PathLike[str], mode: str = "rb") -> IO[Any]:
        """``open`` on ``self.path(relative)``. The check happens first; the open is the
        standard one."""
        return open(self.path(relative), mode)

    def records(self) -> tuple[Record, ...]:
        """This folder's log, indexed and up to date: the only input fusion gets (D28).

        ``log/`` and every stream file in it go through ``path`` first, so a stream that is a
        link into another folder is refused before the index reads a byte of it.
        """
        log = self.path(LOG_DIRNAME)
        if log.is_dir():
            for entry in sorted(os.listdir(log)):
                self.path(os.path.join(LOG_DIRNAME, entry))
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


_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
"""How a folder is opened before it is entered: never through a link."""


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


def _data_shaped(relative: Path) -> str | None:
    """Why a profile-relative source is data, or ``None`` when it may be a capability."""
    parts = relative.parts
    if parts and parts[0] in DATA_PLACES:
        return f"{parts[0]}/ holds data, never a capability"
    if relative.name in (INDEX_FILENAME, INDEX_LOCK_FILENAME):
        return "the index is derived data"
    suffixes = "".join(relative.suffixes[-2:]).lower()
    for suffix in DATA_SUFFIXES:
        if suffixes.endswith(suffix):
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

    ``SharedDataError`` for a kind that is not a capability or a source that is data (D33);
    ``CrossPrincipalError`` for a source outside the folder, by name or through a link;
    ``FileExistsError`` when ``shared/<kind>/<name>`` is already there (hard constraint 2).
    All raised before anything is written.
    """
    _require_on(environ)
    profile = Profile(context)
    wanted = _require_kind(kind)
    path = profile.path(source)
    relative = path.relative_to(profile.folder)
    if (why := _data_shaped(relative)) is not None:
        raise SharedDataError(f"{relative}: {why} (D33)")
    if not path.is_file():
        raise SharedDataError(f"{relative}: a capability is one regular file")
    shared = Shared(profile.context.root)
    destination = shared.capability_path(wanted, path.name)
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The source is opened without following a final link: ``path`` checked the name, and
    # this makes the open agree with the check even if the name changed in between.
    src_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with open(src_fd, "rb") as src, open(destination, "xb") as out:
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())
    return Published(profile.principal, wanted, path, destination)


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
        elif (why := _data_shaped(Path(*relative.parts[1:]))) is not None:
            strays.append(Stray(relative.as_posix(), why))
    return tuple(strays)
