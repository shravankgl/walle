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
  folder and the Python that runs the server are allowed back. Denying the parent of every
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
import sys
from pathlib import Path
from typing import Any

import walle
from walle.paths import log_dir, principal_dir


def srt_settings(root: Path, principal: str) -> dict[str, Any]:
    """The ``srt`` settings for serving ``principal``'s profile under ``root``.

    Raises ``ValueError`` for a name that is not a usable principal (``paths.check_name``).
    Every path is absolute and symlink-resolved: ``srt`` matches resolved paths, and on macOS
    ``/var`` is a symlink to ``/private/var``, so an unresolved path would match nothing.
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
    """
    wanted = [sys.prefix, sys.base_prefix, str(Path(walle.__file__).parent), *sys.path]
    found: list[str] = []
    for entry in wanted:
        path = Path(entry)
        if not entry or not path.is_absolute() or not path.exists():
            continue
        resolved = path.resolve()
        if resolved.is_relative_to(principals) or principals.is_relative_to(resolved):
            continue
        if home.is_relative_to(resolved):
            continue
        text = str(resolved)
        if text not in found:
            found.append(text)
    return found
