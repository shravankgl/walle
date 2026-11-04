"""The MCP server's sandbox profile, for Anthropic's ``srt`` (D52, D99).

D52 says walle is the third party and must meet its own bar. D99 says the Phase 0 server
ships a profile that reads the profile folder, writes only the log, and has no network, and
is tested under it. This module is the profile. ``walle-mcp --srt-profile`` prints it and the
person points ``srt`` at the result::

    walle-mcp --root R --principal maya --device mbp --srt-profile > walle.srt.json
    srt --settings walle.srt.json walle-mcp --root R --principal maya --device mbp

What the profile says, in ``srt``'s terms (reads are allowed everywhere unless denied, and
``allowRead`` wins over ``denyRead``; writes are denied everywhere unless allowed):

- **Reads.** The home folder and ``<root>/principals`` are denied, then this principal's
  folder and the Python that runs the server are allowed back. That includes every symlink
  on the way from ``sys.executable`` to the real binary, each by its own name: macOS refuses
  to exec through a link left under the home denial. Denying the parent of every
  principal is what keeps another principal's facts unreadable (hard constraint 6): the
  sandbox enforces on the filesystem what no resolver is trusted to.
- **Writes.** The log folder, and only the log folder. The index, its lock, the staging lock
  and the hot tier all sit beside the log in the principal's folder, so none is writable
  from inside. ``remember`` appends to the log and nothing else. ``recall`` and ``why`` cannot
  take ``index.lock``, so they answer from the index as last built and say so
  (``mcp_server``, "File modes"). Rebuild the index with ``walle`` outside the sandbox.
- **Network.** None: ``allowedDomains`` is empty. The server never needs it.

The log folder must exist before the server starts, because the sandbox may not create it.
A first ``walle`` command, run outside the sandbox, makes it.

This module only builds a dict. It reads no file, writes none and starts no process, so a
bad name is refused before anything is made.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import walle
from walle.paths import log_dir, principal_dir


def srt_settings(root: Path, principal: str) -> dict[str, Any]:
    """The ``srt`` settings for serving ``principal``'s profile under ``root``.

    Raises ``ValueError`` for a name that is not a usable principal (``paths.check_name``).
    Every path is absolute and symlink-resolved: ``srt`` matches resolved paths, and on macOS
    ``/var`` is a symlink to ``/private/var``, so an unresolved path would match nothing. The
    one exception is the links on the way to the interpreter (``_symlink_hops``): each is
    named as itself, with its parent folder resolved, because the link is what is read.
    """
    root = Path(root)
    folder = _resolved(principal_dir(root, principal))
    home = Path.home().resolve()
    return {
        "filesystem": {
            "denyRead": [str(home), str(_resolved(root / "principals"))],
            "allowRead": [str(folder), *_interpreter_paths(_resolved(root / "principals"), home)],
            "allowWrite": [str(_resolved(log_dir(root, principal)))],
            "denyWrite": [],
        },
        "network": {"allowedDomains": [], "deniedDomains": []},
    }


def srt_settings_json(root: Path, principal: str) -> str:
    """``srt_settings`` as the text of a settings file, ending in a newline."""
    return json.dumps(srt_settings(root, principal), indent=2) + "\n"


def _resolved(path: Path) -> Path:
    """Absolute and symlink-free, whether or not ``path`` exists yet."""
    return path.resolve(strict=False)


def _interpreter_paths(principals: Path, home: Path) -> list[str]:
    """What Python needs to run the server: its prefixes, walle itself, and the import path.

    Without these, denying the home folder would deny a virtualenv or a ``uv`` Python that
    lives there, and the server would not start. Only absolute entries that exist are kept; a
    relative one (``""`` is the current folder) would make the profile depend on where
    ``walle-mcp`` was run from.

    ``allowRead`` beats ``denyRead``, so an entry that holds ``principals`` (a ``PYTHONPATH``
    set to the data root) would hand back every principal's folder, and one inside it would
    hand back a single other's. Both are dropped: the profile reads one folder there. The same
    goes for an entry that is the home folder or holds it (``PYTHONPATH=$HOME``): it would void
    the home denial, so it is dropped too. One inside home (a virtualenv) stays.

    Resolved entries are not enough to start Python. A uv interpreter is reached through
    links (``.venv/bin/python`` to ``cpython-3.12-<platform>/bin/python3.12``, where the
    ``cpython-3.12`` folder is itself a link to ``cpython-3.12.14``), and macOS refuses to exec
    through a link that is still under the home denial. So every link on the way from
    ``sys.executable`` is allowed too, by its own name, under the same two drops. That is
    narrower than allowing the whole uv folder.
    """
    wanted = [sys.prefix, sys.base_prefix, str(Path(walle.__file__).parent), *sys.path]
    found: list[str] = []

    def keep(path: Path) -> None:
        resolved = path.resolve()
        for form in {path, resolved}:
            if form.is_relative_to(principals) or principals.is_relative_to(form):
                return
            if home.is_relative_to(form):
                return
        if str(path) not in found:
            found.append(str(path))

    for entry in wanted:
        path = Path(entry)
        if not entry or not path.is_absolute() or not path.exists():
            continue
        keep(path.resolve())
    if sys.executable and Path(sys.executable).is_absolute():
        for hop in _symlink_hops(Path(sys.executable)):
            keep(hop)
    return found


def _symlink_hops(path: Path, limit: int = 40) -> list[Path]:
    """Every symlink met while resolving ``path``, each named as itself.

    Each hop's parent folder is already resolved and its last component is the link, which
    is the name the OS reads when it follows it. Stops quietly at a link it cannot read or
    after ``limit`` links (a loop): the profile then lacks those hops, and ``srt`` refuses
    the exec loudly rather than anything being widened.
    """
    hops: list[Path] = []
    current = Path(path.anchor)
    parts = list(path.parts[1:])
    while parts:
        part = parts.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if not candidate.is_symlink():
            current = candidate
            continue
        if len(hops) >= limit:
            break
        try:
            target = Path(os.readlink(candidate))
        except OSError:
            break
        hops.append(candidate)
        if target.is_absolute():
            current = Path(target.anchor)
            parts = [*target.parts[1:], *parts]
        else:
            parts = [*target.parts, *parts]
    return hops
