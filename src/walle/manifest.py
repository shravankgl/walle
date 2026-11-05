"""The manifest check: is what a step is about to touch inside what its node declared?

A node's ``reads``, ``writes`` and ``egress`` are its permission (D32 rule 2, D67, D75): the
paths it may read, the paths it may write, the hosts it may send to. This module answers one
question about them — given a ``Touch``, what is *not* declared — and nothing else. It opens
no file, resolves nothing against a real filesystem and reads no environment; the switch that
decides whether anything runs at all lives in ``walle.missions`` (D117). The gate there calls
``undeclared`` before a step starts and again at every tool call while it runs, so a step is
refused *before* it touches the thing, never after.

What a declaration covers
-------------------------
- A declared path covers itself and everything under it, after both sides are normalized
  with ``posixpath.normpath``: ``a/b`` covers ``a/b/c/../d`` and ``./a//b``. It does not cover
  ``a/b-old`` (a shared prefix is not containment), ``a`` (above it), or anything that climbs
  out through ``..`` — a touched path that normalizes to start with ``..`` is inside nothing.
  Absolute and relative never match each other, and case is significant: the log is one
  spelling for one thing, and a grant is read the same way.
- A declared host covers exactly that host, compared lower-case and without a trailing dot.
  Not its subdomains, no wildcard, no port, no path: a grant names what it grants.
- ``reads`` and ``writes`` are separate grants. Writing a path needs it in ``writes``;
  reading it needs it in ``reads``. A step that does both declares both.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from walle.events import Node


@dataclass(frozen=True)
class Touch:
    """What a step is about to do: the paths it will read and write and the hosts it will
    send to. Empty is a step that touches nothing, which every manifest covers."""

    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    egress: tuple[str, ...] = ()


def _normalized(path: str) -> str | None:
    """One spelling for a path, or ``None`` for a path no grant can cover: empty, the current
    directory, or one that climbs out of wherever it starts."""
    if not path:
        return None
    norm = posixpath.normpath(path)
    if norm == "." or norm == ".." or norm.startswith("../"):
        return None
    return norm


def covers_path(declared: str, touched: str) -> bool:
    """Whether ``declared`` covers ``touched``: the same path, or one under it."""
    grant, target = _normalized(declared), _normalized(touched)
    if grant is None or target is None:
        return False
    return target == grant or target.startswith(grant.rstrip("/") + "/")


def _host(host: str) -> str:
    return host.lower().rstrip(".")


def covers_host(declared: str, touched: str) -> bool:
    """Whether ``declared`` covers ``touched``: that host, and only that host."""
    if not declared or not touched:
        return False
    return _host(declared) == _host(touched)


def undeclared(node: Node, touch: Touch) -> tuple[str, ...]:
    """Every item of ``touch`` the node's manifest does not cover, in touch order, reads then
    writes then egress, each saying which list it is missing from. Empty means covered."""
    out: list[str] = []
    for path in touch.reads:
        if not any(covers_path(d, path) for d in node.reads):
            out.append(f"read {path!r} is not in reads")
    for path in touch.writes:
        if not any(covers_path(d, path) for d in node.writes):
            out.append(f"write {path!r} is not in writes")
    for host in touch.egress:
        if not any(covers_host(d, host) for d in node.egress):
            out.append(f"egress to {host!r} is not in egress")
    return tuple(out)
