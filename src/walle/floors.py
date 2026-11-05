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
``walle.missions.admit`` asks about every path in a step's ``writes`` before the switch, before
the manifest, before anything — so a plan that declares a floored write is refused the same
way in every mode. It answers nothing about an absolute path or one that climbs out through
``..``, because without a root there is no answer.

``floored`` has a root and a filesystem. For each end of the operation it asks ``hit`` twice:
once about the path **as given** (made relative to the root as given), and once about where it
**resolves to** — ``os.path.realpath`` through every symlink, including a dangling one, whose
missing target is where a write would land. Either answer floors it. Both are needed: a link
under an innocent name only shows under resolution, and a protected folder replaced by a link
pointing *out* of the root only shows by name — the host loads ``hot/rules.md`` by that name and
follows the link, so a write to the name is a write to what the host reads. A path that cannot
be resolved (a link loop, an unreadable link) is refused: fail closed (D86).

Every kind of write is one ``Op``: ``write`` (create, change, truncate, chmod — anything that
changes what is at the path), ``delete``, ``rename`` (both ends) and ``link`` (hard or
symbolic; the name made and the thing it names). The kind is carried into the refusal so it
reads as what was asked; no kind is let through that another is not.

The dormant rule, for a check
-----------------------------
This module never writes, starts nothing, reads no clock and no environment, and has no
command. Its only filesystem calls are the ones that resolve a name (``realpath`` and
``lstat``). There is no switch, because a floor that can be turned off is not a floor; what
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
type How = Literal["is", "under", "above", "unresolvable"]


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
        self, kind: Kind, path: str, *, end: End = "path", resolved: str | None = None
    ) -> str:
        """The sentence a refusal carries."""
        verb = f"{kind} to" if end == "to" else str(kind)
        where = f"resolves to {resolved!r}, {self.how}" if resolved else self.how
        if self.how == "unresolvable":
            return (
                f"{verb} {path!r} is floored: it cannot be resolved (a link loop, or a link that "
                "cannot be read), so where it would land is unknown and it is refused (D72)"
            )
        return (
            f"{verb} {path!r} is floored: {where} {self.protected!r}, "
            "which nothing the agent does may write, in any mode (D72)"
        )


@dataclass(frozen=True)
class Floored:
    """Why ``floored`` refused an operation: which end, which path, which floor, and the
    resolved path when the hit came through a link."""

    op: Op
    end: End
    path: str
    protected: str
    how: How
    resolved: str | None = None

    @property
    def reason(self) -> str:
        return Hit(self.protected, self.how).reason(
            self.op.kind, self.path, end=self.end, resolved=self.resolved
        )


# --- by name ------------------------------------------------------------------------------------


def _segments(relative: str) -> tuple[str, ...] | None:
    """The segments of a root-relative path, or ``None`` when there is no answer by name:
    absolute, or climbing out through ``..``. Empty is the root."""
    if posixpath.isabs(relative):
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


def hit(relative: str) -> Hit | None:
    """Whether a root-relative path is, is under, or is above a floor path. Pure: nothing is
    read. ``None`` for a path that is clear, and for one this cannot answer about (absolute,
    or climbing out) — ``floored`` answers those with a root in hand."""
    parts = _segments(relative)
    if parts is None:
        return None
    for entry in PROTECTED:
        if (found := _against(parts, entry)) is not None:
            return found
    return None


# --- on disk ------------------------------------------------------------------------------------


def _real(path: str) -> str | None:
    """Where ``path`` lands, through every link including a dangling one; ``None`` when that
    cannot be known. ``realpath`` never raises here: it leaves a loop in place, and treats a
    component it cannot ``lstat`` as a plain name. So every prefix of the result is looked at
    again, and one that is still a link, or cannot be looked at, means the answer is unknown."""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):  # ValueError: an embedded NUL, which no path may carry
        return None
    prefix = real
    while True:
        try:
            if stat.S_ISLNK(os.lstat(prefix).st_mode):
                return None
        except FileNotFoundError:
            pass  # the missing tail ``realpath`` appended: nothing there to be a link
        except (OSError, ValueError):
            return None  # unreadable, a loop, a file in the way: where it lands is unknown
        parent = os.path.dirname(prefix)
        if parent == prefix:
            return real
        prefix = parent


def _relative(path: str, root: str) -> str | None:
    """``path`` relative to ``root``, both already normalized the same way, or ``None`` when
    it is not under the root. An empty string is the root itself."""
    if path == root:
        return ""
    base = root.rstrip(os.sep) + os.sep
    if path.startswith(base):
        return path[len(base) :]
    return None


def _as_given(root: str, path: str) -> str | None:
    """The path as the caller spelled it, relative to the root as the caller spelled it."""
    if not os.path.isabs(path):
        return path
    return _relative(os.path.normpath(path), os.path.normpath(root))


def floored(root: str | os.PathLike[str], op: Op) -> Floored | None:
    """Whether any end of ``op`` reaches a floor path under ``root``, by name or after
    resolving links. ``None`` means the floor does not apply; it says nothing about the
    manifest, which is ``walle.manifest``'s question.

    Reads the filesystem only to resolve names: ``realpath`` and ``lstat``. Writes nothing.
    """
    root_text = os.fspath(root)
    root_real = _real(root_text)
    if root_real is None:
        return Floored(op, "path", op.path, "", "unresolvable")
    for end, path in op.ends:
        given = _as_given(root_text, path)
        if given is not None and (found := hit(given)) is not None:
            return Floored(op, end, path, found.protected, found.how)
        real = _real(os.path.join(root_text, path))
        if real is None:
            return Floored(op, end, path, "", "unresolvable")
        resolved = _relative(real, root_real)
        if resolved is not None and (found := hit(resolved)) is not None:
            return Floored(op, end, path, found.protected, found.how, resolved=real)
    return None
