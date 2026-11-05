"""The approval floors: files that govern permission cannot be written in any mode (D72, D109).

openworker's finding, adopted as D72: approve one ordinary-looking command, it quietly appends
to the rule file, and every later session is more permissive. That happens in the default
interactive mode, so the protection cannot be a property of a sandbox or a mode. It is a
floor: a set of paths nothing the agent does may write — not through a tool, not with a grant,
not while a step's manifest declares them, not with missions on or off. The kernel and the
renderer are the only writers, and their input is the log.

The protected set (D72, D109, ``ARCHITECTURE.md`` §7)
------------------------------------------------------
``PROTECTED`` is the table, relative to the walle root, one line per entry:

- ``kernel/`` — the kernel is immutable (hard constraint 1).
- ``shared/`` — capabilities enter only by explicit publication, which is human-only
  (D33 rule 1, D72).
- ``principals/*/grants/`` and ``principals/*/keys/`` — what a principal has allowed and what
  unlocks it (D72). ``*`` is any principal: the agent working for one may not touch
  another's either (hard constraint 6), and a folder that is not a principal's gets no
  benefit of the doubt.
- ``principals/*/hot/`` — the rendered hot tier, ``rules.md`` which is human-only (D81), and
  ``rendered.json`` which vouches for the rest (D107): a write there puts chosen words into a
  trusted context (D109).

What is **not** here, and why: the watch file (D108) and the host's hook configuration have
no settled place under the root yet, so there is no path to name; ``trust`` is an event, not a
file, and the writer is its gate; D72's Phase 2 third floor — files inside a *workspace* that
execute later — needs a workspace root the layout does not have. Each is a one-line addition
to the table when its path exists.

How a path hits a floor
-----------------------
A path is floored when, split into segments and compared against an entry:

- it **is** the entry, or is **under** it — a write to the file or to anything inside the
  folder;
- it is **above** it — a rename or a delete of a parent takes the protected file with it, and
  a permission change on a parent changes who can reach it, so no kind of write to a parent
  is let through. The root itself is above everything.

Segments are compared after ``posixpath.normpath`` and without regard to case: the
filesystems walle ships on fold case (APFS, NTFS), so ``HOT/rules.md`` is ``hot/rules.md``
there, and nothing legitimate lives at the other spelling elsewhere. A shared prefix is not
containment (``hotel/`` is not ``hot/``), and the names only count where the layout puts them
(``notes/kernel/`` is a folder called kernel, not the kernel).

Two layers
----------
``hit`` is pure: a root-relative path in, a ``Hit`` or ``None`` out, nothing read. It is what
``walle.missions.admit`` and ``walle.reviewer.review`` ask about every path in a step's
``writes`` before the switch, before the manifest, before anything — so a plan that declares a
floored write is refused the same way in every mode, by every gate. A path it cannot answer
about by name — absolute, climbing out through ``..``, or holding a NUL — is not ``None``: it
is a ``Hit`` of how ``unanswerable``, and a refusal (D86). Issue #118 found two gates reading
``None`` two ways; now there is one answer and one reader of it. ``segments`` is the spelling
that answer rests on, and the manifest (``walle.manifest``) and the builder's workspace rule
(``walle.builder``) read a path through it too, so what one calls a path the others do.

``floored`` has a root and a filesystem. For each end of the operation it asks ``hit`` about
three things, and any one floors it:

- the path **as given**, made absolute against the root and folded lexically (``abspath``), so
  ``../walle/principals/p-sam/grants`` is read as the floor path it names and a root spelled
  relative to the working directory agrees with a path spelled absolute;
- **every symlink met on the way**, walking the path one component at a time from the real
  root, as ``walle.sandbox._symlink_hops`` does, each link named as itself with its folder
  already resolved. A protected folder replaced by a link pointing *out* of the root is seen
  here whatever name it was reached through, because the host loads ``hot/rules.md`` by that
  name and follows the link: a write through the name is a write to what the host reads. A
  hop counts when it *is* or is *under* a floor path, and also when it is *above* one —
  ``principals/`` or a principal's own folder standing as a link. That last used to be let
  through, as a folder read on the way to a write elsewhere; but a profile is physical (hard
  constraint 6, D32), ``walle.profiles`` and ``walle.osusers`` already refuse it, and once a
  walk has followed such a link onto another disk the floor paths under it are out of the
  root's sight (issue #118 finding 1.2). The refusal names the link;
- **where it lands**, the fully resolved path, including the missing target of a dangling link,
  which is where a write would create something.

A relative target of a ``link`` is, as the OS reads it, relative to the folder the link is
made in, not to the root, so ``to`` is also walked from ``dirname(path)`` — that folder
resolved first, since it may itself be a link. It is walked from the root as well, which is
what a hard link's ``to`` means. A path that cannot be resolved — a link loop, a link or a
folder that cannot be read, an embedded NUL — is refused: fail closed (D86).

Every kind of write is one ``Op``: ``write`` (create, change, truncate, chmod — anything that
changes what is at the path), ``delete``, ``rename`` (both ends) and ``link`` (hard or
symbolic; the name made and the thing it names). The kind is carried into the refusal so it
reads as what was asked; no kind is let through that another is not.

The dormant rule, for a check
-----------------------------
This module never writes, starts nothing, reads no clock and no environment, and has no
command. Its only filesystem calls are the ones that resolve a name (``lstat`` and
``readlink``). There is no switch, because a floor that can be turned off is not a floor; what
the switch governs — whether any step runs at all — stays in ``walle.missions`` and stays off
(D117).
"""

from __future__ import annotations

import os
import posixpath
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

ANY = "*"
"""Any one segment, in a ``Protected.pattern``: the principal's own folder name."""


@dataclass(frozen=True)
class Protected:
    """One entry of the floor table: a root-relative pattern, what it is, and why."""

    pattern: tuple[str, ...]
    what: str
    decisions: str


PROTECTED: tuple[Protected, ...] = (
    Protected(("kernel",), "the kernel", "hard constraint 1"),
    Protected(("shared",), "shared capabilities, published by a person only", "D33, D72, D109"),
    Protected(("principals", ANY, "grants"), "what a principal has allowed", "D72, D109"),
    Protected(("principals", ANY, "keys"), "what unlocks a principal's connectors", "D72, D109"),
    Protected(
        ("principals", ANY, "hot"),
        "the rendered hot tier, the rules file and the render manifest",
        "D72, D81, D107, D109",
    ),
)
"""The floor, in the order a refusal cites. Lower-case; matching ignores case."""


class Kind(StrEnum):
    """What a write is. The two-ended kinds carry a second path in ``Op.to``."""

    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"
    LINK = "link"


type End = Literal["path", "to"]
type How = Literal["is", "under", "above", "unresolvable", "unanswerable"]


@dataclass(frozen=True)
class Op:
    """One write a step is about to make. ``to`` is the destination of a rename, or what a
    link will name; the other kinds have one path and refuse a second."""

    kind: Kind
    path: str
    to: str | None = None

    def __post_init__(self) -> None:
        two_ended = self.kind in (Kind.RENAME, Kind.LINK)
        if two_ended and self.to is None:
            raise ValueError(f"{self.kind} needs both ends: `to` is missing")
        if not two_ended and self.to is not None:
            raise ValueError(f"{self.kind} has one path; `to` was given")

    @property
    def ends(self) -> tuple[tuple[End, str], ...]:
        if self.to is None:
            return (("path", self.path),)
        return (("path", self.path), ("to", self.to))


@dataclass(frozen=True)
class Hit:
    """A root-relative path's relation to a floor path: what it hit, and how."""

    protected: str
    """The entry, spelled with the actual principal where the path reached one."""

    how: How

    def reason(
        self,
        kind: Kind,
        path: str,
        *,
        end: End = "path",
        resolved: str | None = None,
        through: bool = False,
    ) -> str:
        """The sentence a refusal carries. ``resolved`` is where the hit was found when it was
        not found by name; ``through`` says it was a link met on the way rather than where the
        path lands."""
        verb = f"{kind} to" if end == "to" else str(kind)
        if self.how == "unresolvable":
            return (
                f"{verb} {path!r} is floored: it cannot be resolved (a link loop, or a link that "
                "cannot be read), so where it would land is unknown and it is refused (D72)"
            )
        if self.how == "unanswerable":
            return (
                f"{verb} {path!r} is floored: it is absolute, climbs out through '..', or is not "
                "a path, so the floor cannot be answered by name and it is refused (D72, D86)"
            )
        if through and self.how == "above":
            return (
                f"{verb} {path!r} is floored: it passes through a link at {resolved!r}, which "
                f"stands above {self.protected!r}; a folder above a floor path is physical, never "
                "a link (D32, D72)"
            )
        if through:
            where = f"passes through a link at {resolved!r}, {self.how}"
        elif resolved:
            where = f"resolves to {resolved!r}, {self.how}"
        else:
            where = self.how
        return (
            f"{verb} {path!r} is floored: {where} {self.protected!r}, "
            "which nothing the agent does may write, in any mode (D72)"
        )


@dataclass(frozen=True)
class Floored:
    """Why ``floored`` refused an operation: which end, which path, which floor, the
    resolved path when the hit came through a link, and whether that path is the link itself
    (``through``) or where the operation lands."""

    op: Op
    end: End
    path: str
    protected: str
    how: How
    resolved: str | None = None
    through: bool = False

    @property
    def reason(self) -> str:
        return Hit(self.protected, self.how).reason(
            self.op.kind, self.path, end=self.end, resolved=self.resolved, through=self.through
        )


# --- by name ------------------------------------------------------------------------------------


def segments(relative: str) -> tuple[str, ...] | None:
    """The one spelling of a path by name: its segments after ``posixpath.normpath``, or
    ``None`` when there is no answer by name — absolute, climbing out through ``..``, or
    holding a NUL, which no filesystem takes. Empty is the root (``""`` or ``.``). Pure.

    Every module that asks whether a path is answerable reads this: ``hit`` for the floor,
    ``walle.manifest`` for what a declaration covers, ``walle.builder`` for what stays inside
    a workspace. One function, so the answers cannot disagree (issue #118).
    """
    if posixpath.isabs(relative) or "\x00" in relative:
        return None
    norm = posixpath.normpath(relative) if relative else "."
    if norm == ".":
        return ()
    if norm == ".." or norm.startswith("../"):
        return None
    return tuple(norm.split("/"))


def _against(parts: tuple[str, ...], entry: Protected) -> Hit | None:
    pattern = entry.pattern
    for actual, wanted in zip(parts, pattern, strict=False):
        if wanted != ANY and actual.lower() != wanted:
            return None
    spelled = "/".join(
        parts[i] if i < len(parts) and wanted == ANY else wanted for i, wanted in enumerate(pattern)
    )
    if len(parts) == len(pattern):
        return Hit(spelled, "is")
    if len(parts) > len(pattern):
        return Hit(spelled, "under")
    return Hit(spelled, "above")


UNANSWERABLE = Hit("", "unanswerable")
"""The answer for a path the floor cannot judge by name. A refusal, not a pass (D86)."""


def hit(relative: str) -> Hit | None:
    """Whether a root-relative path is, is under, or is above a floor path. Pure: nothing is
    read. ``None`` for a path that is clear. A path this cannot answer about by name —
    absolute, climbing out, or not a path — is ``UNANSWERABLE``, a hit of its own: the gates
    that ask by name have no root in hand to do better, so the answer is no. ``floored``,
    which has a root, never asks this about such a path; it folds the path against the root
    first."""
    parts = segments(relative)
    if parts is None:
        return UNANSWERABLE
    for entry in PROTECTED:
        if (found := _against(parts, entry)) is not None:
            return found
    return None


# --- on disk ------------------------------------------------------------------------------------


HOP_LIMIT = 40
"""Links followed before a walk is called a loop and refused. The kernel's own limit is of
the same order; a legitimate layout is nowhere near it."""


def _walk(path: str) -> tuple[list[str], str] | None:
    """Resolve an absolute ``path`` one component at a time. Returns every symlink met, each
    named as itself with its folder already resolved, and where the path finally lands —
    through a dangling link to the name it would create. ``None`` when the answer is not
    knowable: a loop, a link or folder that cannot be read, a NUL in a name."""
    hops: list[str] = []
    current = os.sep
    parts = [p for p in path.split(os.sep) if p]
    while parts:
        part = parts.pop(0)
        if part == ".":
            continue
        if part == "..":
            current = os.path.dirname(current)
            continue
        candidate = os.path.join(current, part)
        try:
            is_link = stat.S_ISLNK(os.lstat(candidate).st_mode)
        except FileNotFoundError:
            is_link = False  # nothing there yet: the rest of the path is what would be made
        except (OSError, ValueError):
            return None
        if not is_link:
            current = candidate
            continue
        if len(hops) >= HOP_LIMIT:
            return None
        hops.append(candidate)
        try:
            target = os.readlink(candidate)
        except (OSError, ValueError):
            return None
        if os.path.isabs(target):
            current = os.sep
        parts = [p for p in target.split(os.sep) if p] + parts
    return hops, current


def _relative(path: str, root: str) -> str | None:
    """``path`` relative to ``root``, both absolute and normalized the same way, compared
    without regard to case as ``hit`` compares, or ``None`` when it is not under the root. An
    empty string is the root itself."""
    if path.lower() == root.lower():
        return ""
    base = root.rstrip(os.sep) + os.sep
    if path[: len(base)].lower() == base.lower():
        return path[len(base) :]
    return None


def _absolute(root: str, path: str) -> str:
    """``path`` as an absolute path, read from ``root`` when relative, folded lexically."""
    return os.path.abspath(os.path.join(root, path))


def _unfolded(root: str, path: str) -> str:
    """The same, with every ``.`` and ``..`` left in place for ``_walk`` to apply *after* the
    link before it is followed, as the OS does: ``L/../x`` with ``L -> keys/sub`` is
    ``keys/x``, which folding first would hide."""
    joined = os.path.join(root, path)
    return joined if os.path.isabs(joined) else os.path.join(os.getcwd(), joined)


def _check(
    root_abs: str, root_real: str, path: str, *, at: str | None = None
) -> tuple[Hit, str | None, bool] | None:
    """The three questions of the module docstring about one path. ``at`` is the folder a
    relative path is read from, the root when not given. Returns the hit, the resolved path
    it was found at (``None`` when found by name) and whether that path is a link met on the
    way; or ``None`` when clear; a hit of ``unresolvable`` when the walk could not finish."""
    given = _absolute(at or root_abs, path)
    if (rel := _relative(given, root_abs)) is not None and (found := hit(rel)) is not None:
        return found, None, False
    walked = _walk(_unfolded(at or root_abs, path))
    if walked is None:
        return Hit("", "unresolvable"), None, False
    hops, landing = walked
    for hop in hops:
        # A hop above a floor path is a linked ``principals/`` or principal folder: refused,
        # as the profile refuses it (hard constraint 6), not read through (issue #118, 1.2).
        if (rel := _relative(hop, root_real)) and (found := hit(rel)) is not None:
            return found, hop, True
    if (rel := _relative(landing, root_real)) is not None and (found := hit(rel)) is not None:
        return found, landing, False
    return None


def floored(root: str | os.PathLike[str], op: Op) -> Floored | None:
    """Whether any end of ``op`` reaches a floor path under ``root``: by name, through any
    link on the way, or where it lands. ``None`` means the floor does not apply; it says
    nothing about the manifest, which is ``walle.manifest``'s question.

    Reads the filesystem only to resolve names: ``lstat`` and ``readlink``. Writes nothing.
    """
    root_abs = os.path.abspath(os.fspath(root))
    walked_root = _walk(root_abs)
    if walked_root is None:
        return Floored(op, "path", op.path, "", "unresolvable")
    root_real = walked_root[1]
    for end, path in op.ends:
        if (result := _check(root_abs, root_real, path)) is not None:
            found, where, through = result
            return Floored(op, end, path, found.protected, found.how, where, through)
    if op.kind is Kind.LINK and op.to is not None and not os.path.isabs(op.to):
        # a relative symlink target is read from the link's own folder, resolved first
        folder = _walk(_unfolded(root_abs, os.path.dirname(op.path)))
        if folder is None:
            return Floored(op, "to", op.to, "", "unresolvable")
        if (result := _check(root_abs, root_real, op.to, at=folder[1])) is not None:
            found, where, through = result
            return Floored(op, "to", op.to, found.protected, found.how, where, through)
    return None
