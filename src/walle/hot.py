"""The hot-tier renderer: what walle carries into every session, as files (D50, D64, D81, D107).

``principals/p-<name>/hot/`` holds four files, read in this order by the host hook (task 8):

``identity.md``  who the model is and how to read the rest. Rendered.
``user.md``      who it is working for. Rendered.
``rules.md``     the person's own rules, which win over everything learned. **Human-only**
                 (D81, D72): the renderer never opens, writes, chmods, replaces or creates it.
``learned.md``   the current assertions, from the resolver fold. Rendered.

Every rendered file opens by saying what it is and what to do with it, because a model handed
its own memory without framing reads it as notes about a stranger (D64).

``learned.md`` renders every assertion in full while the whole render is at most
``FULL_LIMIT`` characters. Past that it flips to **index mode**: the newest ``KEEP_FULL`` stay
in full and the rest become ``[#ref] summary`` lines, with a note telling the model to fetch
before acting on anything a summary only hints at (D81). One-line entries come first, oldest
first, and the full entries last, so a new assertion changes the end of the file and the start
stays a stable prefix (R1). Mode is chosen by the render, so it is fixed for every session that
reads that render. The mode reported is the mode of the text written: when the full render
is over the limit but holds no more than ``KEEP_FULL`` assertions, nothing can become a
summary line, so the file is the full render and the mode is ``full``, over the limit or not.

The files are derived from the log (D10), so they are written ``0444`` and the renderer
records the SHA-256 of what it wrote in ``rendered.json`` (D107). A file whose bytes are not
ones walle wrote has been edited out of band. The render then **keeps the edit** and reports
it. It does not render over it, and it does not delete it. Deleting the file is how a person
discards their edit; ``walle context`` (task 12) is how they will make one properly. Staging
the edit as a proposal (D107's last step) needs the staging queue from task 9 and is not done
here.

Writing
-------
Each file is written to a temp file **in the same folder**, ``fsync``ed, set ``0444``, then
moved over the real name with ``os.replace``, and the folder is ``fsync``ed. A rename within
one folder is atomic on every POSIX filesystem (APFS included), so a reader sees the old file
or the new one, never half of either. Nothing here uses Linux-only calls: no ``O_TMPFILE``, no
``renameat2``, no ``/proc``.

Files and the manifest cannot change in one step, so the manifest is written twice. First it
vouches for both the old and the new bytes of every file about to change, then the files move,
then it vouches for the new bytes only. A render killed anywhere in between leaves files that
the manifest vouches for, so the next render does not report an edit nobody made.

Two runners, step by step
-------------------------
Renderers A and B for the same principal, writer W appending to ``d-mbp``, and a person H.

1. A takes ``hot/render.lock`` (exclusive). B blocks here, having read nothing.
2. A clears temp files a killed render left, then brings the index up to date
   (``index.update``, which takes ``index.lock`` and every stream lock for a moment, as
   described in ``index.py``). A gets the log up to ``d-mbp:3`` and folds it.
3. W appends ``d-mbp:4``. It only needs its stream lock, which A no longer holds, so the
   append goes ahead and A's render does not include it.
4. A reads ``rendered.json`` and each file, and finds all three are bytes it wrote last time.
   It writes the manifest (old and new hashes), moves the three files into place, writes the
   manifest again (new hashes) and releases the lock.
5. B wakes **and only now** reads the log. It gets ``d-mbp:4`` too, because the log only grows.
   It finds A's files, which A's manifest vouches for, and renders over them. What is left on
   disk is B's render, of the newer log.

If B had read the log before waiting for the lock, it would wake with an older fold than A's
and replace A's newer files with it. ``test_a_second_renderer_waits_and_renders_the_newer_log``
stages that with a second process.

H does not take the lock, because an editor does not know about it. If H saves
``learned.md`` before step 4, A finds bytes the manifest does not vouch for and keeps them.
If H saves after step 4, the next render finds them. One window is left open: H saving
between A's read of the file in step 4 and A's ``os.replace``, a few microseconds, loses H's
edit to A's render. Closing it would need a lock the editor takes, and editors take none.

Lock order is ``render.lock``, then ``index.lock``, then stream files. Nothing takes
``render.lock`` while holding either of the others, so no cycle exists.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from walle import index
from walle.events import parse_ref
from walle.paths import hot_dir, log_dir
from walle.reader import Skip
from walle.resolver import Belief, Resolution, resolve

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's hot-tier renderer needs POSIX file locking (fcntl); Windows is not supported "
        "in Phase 0. Run it under WSL."
    )

import fcntl

IDENTITY = "identity.md"
USER = "user.md"
RULES = "rules.md"
LEARNED = "learned.md"
MANIFEST = "rendered.json"
RENDER_LOCK = "render.lock"

ORDER = (IDENTITY, USER, RULES, LEARNED)
"""The order the hook injects the hot tier in. ``rules.md`` sits above everything the agent
learned, so on conflict the rule is what the model read with more authority (D81)."""

FILES = (IDENTITY, USER, LEARNED)
"""The files the renderer writes. ``rules.md`` is not one of them, and never will be."""

FULL_LIMIT = 8000
"""Characters. At or under this, the whole render is in full; over it, index mode (D81's
"~8k chars"). Measured on the full render of all three files, in characters, not bytes, so
a log in Hindi does not flip sooner than one in English."""

KEEP_FULL = 10
"""How many of the newest assertions stay in full in index mode (D81)."""

READ_ONLY = 0o444
MANIFEST_FORMAT = 1


class Mode(StrEnum):
    FULL = "full"
    INDEX = "index"


@dataclass(frozen=True)
class Rendered:
    """What ``compose`` decided: the mode, and every rendered file's text."""

    mode: Mode
    files: dict[str, str] = field(hash=False)
    in_full: int
    summarised: int


@dataclass(frozen=True)
class Preserved:
    """A rendered file that changed after walle wrote it, and was left alone."""

    name: str
    path: Path
    found: str | None
    """SHA-256 of what is there, or ``None`` when it is not a regular file at all."""

    reason: str


@dataclass(frozen=True)
class RenderReport:
    """What one ``render`` did. Nothing in it is a guess about the files: each was checked."""

    folder: Path
    mode: Mode
    assertions: int
    in_full: int
    summarised: int
    written: tuple[str, ...]
    """Files moved into place by this render."""

    unchanged: tuple[str, ...]
    """Files that already held exactly this render, read-only. Not rewritten (R1)."""

    preserved: tuple[Preserved, ...]
    """Files kept because they were edited out of band (D107). Not rendered over."""

    rules_present: bool
    state: str
    """``index.state_hash`` of the fold this render came from (D66)."""

    manifest_note: str | None
    """Set when ``rendered.json`` could not be read and was treated as empty."""

    skips: tuple[tuple[str, Skip], ...]
    """Lines the reader skipped while the index was brought up to date (D80)."""


# --- the pure part ------------------------------------------------------------------------


def compose(principal: str, resolution: Resolution, *, limit: int = FULL_LIMIT) -> Rendered:
    """The text of every rendered file, from a fold. Reads nothing, writes nothing.

    Deterministic: the same resolution always gives the same bytes, which is what lets the
    renderer skip a write when nothing changed and recognise its own files by hash.
    """
    beliefs = sorted(resolution.current, key=_newness)
    # A set per side: two contradiction events about one pair are one conflict to the reader,
    # so they render as one line. ``_entry`` sorts what is left.
    conflicts: dict[str, set[str]] = {}
    for conflict in resolution.conflicts:
        a, b = conflict.between
        conflicts.setdefault(a, set()).add(b)
        conflicts.setdefault(b, set()).add(a)

    fixed = {IDENTITY: _identity(principal), USER: _user(principal)}
    full = _learned(principal, beliefs, conflicts, summarised=0)
    summarised = max(0, len(beliefs) - KEEP_FULL)
    # Full when it fits, and also when it does not but every assertion is among the newest
    # KEEP_FULL: index mode would then summarise nothing, so the file is the full render and
    # is reported as such. The mode must describe the text, because D81 fixes it per session.
    if not summarised or sum(len(t) for t in fixed.values()) + len(full) <= limit:
        return Rendered(Mode.FULL, {**fixed, LEARNED: full}, len(beliefs), 0)

    learned = _learned(principal, beliefs, conflicts, summarised=summarised)
    return Rendered(Mode.INDEX, {**fixed, LEARNED: learned}, len(beliefs) - summarised, summarised)


def _newness(belief: Belief) -> tuple[datetime, tuple[str, int]]:
    """Oldest first: by when the log learned it, then by ref so a tie is still ordered."""
    return datetime.fromisoformat(belief.assertion.ts), parse_ref(belief.ref)


def _one_line(text: str) -> str:
    """Whitespace collapsed, so a summary cannot start a line that looks like another entry."""
    return " ".join(text.split())


def _banner(name: str, principal: str) -> str:
    return (
        f"<!-- walle hot tier: {name}, rendered from {principal}'s log. This file is "
        "read-only: change what it says with `walle context`, not here. An edit made here "
        "is kept, but walle stops rendering this file until the edit is dealt with. -->\n"
    )


def _identity(principal: str) -> str:
    return (
        _banner(IDENTITY, principal) + "# Who you are\n\n"
        f"You are an assistant working for {principal}. walle keeps your memory of them: what "
        "they have told you and approved, written down so that you carry it from one "
        "conversation to the next. The files in this folder are that memory, written to you. "
        "Read them as things you already know, not as notes about somebody else.\n\n"
        "Your memory comes in four files, in this order:\n\n"
        f"1. `{IDENTITY}`, this file: who you are and how to read the rest.\n"
        f"2. `{USER}`: who you are working for.\n"
        f"3. `{RULES}`: {principal}'s own rules, written only by them. When a rule and "
        "anything you have learned disagree, the rule wins. It may be absent.\n"
        f"4. `{LEARNED}`: what you have learned about {principal}. Each entry has a tag such "
        "as `[#d-mbp:12]`, which is where it sits in the log.\n\n"
        "You cannot change these files and should not try. If something in them looks wrong, "
        f"say so, and {principal} can correct it.\n"
    )


def _user(principal: str) -> str:
    return (
        _banner(USER, principal) + "# Who you are working for\n\n"
        f"You are working for {principal}. Unless your host tells you otherwise, the person "
        f"in this conversation is {principal}, and theirs are the requests you act on.\n\n"
        f"What you know about {principal} is in `{LEARNED}`, and the rules they have set for "
        f"you are in `{RULES}`. Both are about the person you are talking to.\n"
    )


def _learned(
    principal: str, beliefs: list[Belief], conflicts: dict[str, set[str]], *, summarised: int
) -> str:
    out = [
        _banner(LEARNED, principal),
        f"# What you have learned about {principal}\n\n",
        f"Each entry is something {principal} approved for you to remember. "
        f"`{RULES}` overrides any entry here.\n\n",
    ]
    if not beliefs:
        out.append("Nothing learned yet.\n")
        return "".join(out)

    if summarised:
        out.append(
            "This file is in index mode: there is more here than fits in full. The older "
            "entries are one line each, `[#ref] summary`, and the newest "
            f"{len(beliefs) - summarised} follow in full. Before you act on anything a "
            "one-line entry only hints at, fetch it in full with the `recall` tool and its "
            "ref.\n\n"
            "## One line each, oldest first\n\n"
        )
        out.extend(f"[#{b.ref}] {_one_line(b.assertion.summary)}\n" for b in beliefs[:summarised])
        out.append("\n## In full, newest last\n\n")
    out.append(
        "\n".join(_entry(b, conflicts.get(b.ref, set()), principal) for b in beliefs[summarised:])
    )
    return "".join(out)


def _entry(belief: Belief, conflicts: set[str], principal: str) -> str:
    a = belief.assertion
    lines = [f"### [#{belief.ref}] {_one_line(a.summary)}\n\n", a.body.rstrip("\n"), "\n"]
    notes: list[str] = []
    start, end = belief.window.valid_from, belief.window.valid_to
    if start or end:
        span = " ".join(
            part
            for part in (f"from {start}" if start else "", f"until {end}" if end else "")
            if part
        )
        took_over = ", ".join(f"[#{r}]" for r in belief.closed_by)
        notes.append(f"True {span}" + (f", when {took_over} took over." if took_over else "."))
    if a.about:
        notes.append(f"About {a.about}, not {principal}.")
    notes.extend(
        f"This conflicts with [#{other}], and both are current: ask {principal} which holds "
        "before acting on either."
        for other in sorted(conflicts, key=parse_ref)
    )
    if notes:
        lines.append("\n" + "".join(f"- {n}\n" for n in notes))
    return "".join(lines)


# --- the render ---------------------------------------------------------------------------


def render(root: Path, principal: str) -> RenderReport:
    """Bring the index up to date, fold it, and write the hot tier (D107).

    Holds ``hot/render.lock`` for all of it, so the fold a render writes is never older than
    the one before it. Raises what ``index.update`` raises, and ``ResolutionError``.
    """
    root = Path(root)
    log = log_dir(root, principal)  # validates the name before it touches the filesystem
    if not log.is_dir():
        # Checked before anything is created: a misspelt principal must not get a folder.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to render")
    folder = hot_dir(root, principal)
    folder.mkdir(exist_ok=True)

    with _exclusive(folder / RENDER_LOCK):
        _clear_temps(folder)
        built = index.update(root, principal)
        resolution = resolve(built.records)
        rendered = compose(principal, resolution)

        on_disk, vouched, manifest_note = _read_manifest(folder)
        data = {name: rendered.files[name].encode("utf-8") for name in FILES}
        unchanged: list[str] = []
        preserved: list[Preserved] = []
        new: dict[str, bytes] = {}
        for name in FILES:
            verdict = _judge(folder / name, data[name], vouched.get(name, ()))
            if isinstance(verdict, Preserved):
                preserved.append(verdict)
            elif verdict == "unchanged":
                unchanged.append(name)
            else:
                new[name] = data[name]

        state = index.state_hash(resolution)
        kept = {p.name for p in preserved}
        # A kept file keeps whatever the manifest vouched for, so it is reported again next
        # time; every other file is vouched for by exactly the bytes now rendered.
        final = {
            name: sorted(vouched.get(name, ())) if name in kept else [_sha(data[name])]
            for name in FILES
        }
        if new:
            during: dict[str, list[str]] = {}
            for name in FILES:
                hashes = set(vouched.get(name, ()))
                if name in new:
                    hashes.add(_sha(new[name]))
                during[name] = sorted(hashes)
            _write_manifest(folder, _manifest(rendered.mode, state, during, kept))
            for name, payload in new.items():
                _write_file(folder / name, payload)
        manifest = _manifest(rendered.mode, state, final, kept)
        if manifest != on_disk:  # also when only the state or the mode moved
            _write_manifest(folder, manifest)

    return RenderReport(
        folder=folder,
        mode=rendered.mode,
        assertions=len(resolution.current),
        in_full=rendered.in_full,
        summarised=rendered.summarised,
        written=tuple(new),
        unchanged=tuple(unchanged),
        preserved=tuple(preserved),
        rules_present=os.path.lexists(folder / RULES),  # looked at, never opened
        state=state,
        manifest_note=manifest_note,
        skips=built.skips,
    )


def _judge(path: Path, new: bytes, vouched: tuple[str, ...] | list[str]) -> str | Preserved:
    """``"write"``, ``"unchanged"``, or the ``Preserved`` record for a file to leave alone."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "write"
    if not stat.S_ISREG(st.st_mode):
        return Preserved(path.name, path, None, "it is not a regular file walle wrote")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "write"  # gone since the lstat: nothing there to keep
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        # A symlink swapped in since the lstat: O_NOFOLLOW refused it. Same verdict as a
        # symlink found by the lstat, and nothing at the other end is opened.
        return Preserved(path.name, path, None, "it is not a regular file walle wrote")
    with os.fdopen(fd, "rb") as f:
        # The descriptor is what is read, so its type and mode are the ones that count. The
        # lstat above keeps the open off anything that is not a regular file, but the path can
        # change between the two calls.
        opened = os.fstat(f.fileno())
        if not stat.S_ISREG(opened.st_mode):
            return Preserved(path.name, path, None, "it is not a regular file walle wrote")
        current = f.read()
    if current == new:
        return "unchanged" if stat.S_IMODE(opened.st_mode) == READ_ONLY else "write"
    found = _sha(current)
    if found in vouched:
        return "write"
    reason = (
        "it changed after walle wrote it"
        if vouched
        else "walle has no record of writing it, so it may be someone's own file"
    )
    return Preserved(path.name, path, found, reason)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(
    mode: Mode, state: str, files: dict[str, list[str]], kept: set[str]
) -> dict[str, object]:
    """``mode`` and ``state`` describe this render; ``kept`` names the files on disk that are
    not part of it, so nothing reading the manifest mistakes a kept edit for the render."""
    return {
        "format": MANIFEST_FORMAT,
        "mode": mode.value,
        "state": state,
        "order": list(ORDER),
        "files": files,
        "kept": sorted(kept),
    }


def _read_manifest(
    folder: Path,
) -> tuple[object, dict[str, tuple[str, ...]], str | None]:
    """The manifest as read, ``{file: hashes walle may have left there}``, and a note if the
    manifest was unusable.

    An unusable manifest vouches for nothing. That is the safe direction: a file walle cannot
    prove it wrote is kept, and the worst case is an edit reported that nobody made.
    """
    path = folder / MANIFEST
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None, {}, None
    except (OSError, ValueError) as exc:
        return (
            None,
            {},
            f"{path}: unreadable ({exc}); treated as empty, so no file was rendered over",
        )
    files = data.get("files") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format") != MANIFEST_FORMAT
        or not isinstance(files, dict)
        or not all(
            isinstance(v, list) and all(isinstance(h, str) for h in v) for v in files.values()
        )
    ):
        return None, {}, f"{path}: not a manifest this build knows; treated as empty"
    return data, {str(k): tuple(v) for k, v in files.items()}, None


def _write_manifest(folder: Path, data: dict[str, object]) -> None:
    text = json.dumps(data, sort_keys=True, indent=2) + "\n"
    _replace(folder / MANIFEST, text.encode("utf-8"))


def _write_file(path: Path, data: bytes) -> None:
    _replace(path, data)


def _replace(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` read-only, through a temp file in the same folder."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), READ_ONLY)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    """Make the rename durable: the new name lives in the folder, not the file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _clear_temps(folder: Path) -> None:
    """Remove temp files a killed render left. Called under ``render.lock``, so no live
    render owns any of them. Only names ``_replace`` could have made are touched."""
    for name in (*FILES, MANIFEST):
        for stale in folder.glob(f".{name}.*.tmp"):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()


@contextlib.contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    """Hold ``hot/render.lock`` exclusively: one render per principal at a time."""
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
