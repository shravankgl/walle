"""``walle forget`` and ``walle context``: a person changing what walle believes (D93, D107).

Both write only by appending (hard constraint 2). Nothing is edited and nothing is deleted:

``forget <ref>``   a ``supersede`` targeting a current assertion, with ``valid_to`` when the
                   person knows when it stopped being true (D93). The assertion stays in the
                   log at its ref, so ``walle why`` still explains it; the fold hides it from
                   ``current``, so the next render drops it.
``context``        the live view, every current assertion as ``[#ref] summary`` and its text,
                   opened in ``$EDITOR`` (D107). What comes back is diffed against what was
                   shown, by ref, and each difference becomes the events it implies:

                   * an entry whose summary or text changed: a new ``assertion`` with the new
                     text, ``caused_by`` the old one, then a ``supersede`` retiring the old one,
                     ``caused_by`` the new one. Level, confidence, evidence count, world time
                     and ``about`` carry over unchanged;
                   * an entry that is gone: a ``supersede``, exactly as ``forget`` writes;
                   * an entry headed ``[#new]``: a new ``assertion`` by the user.

The view is a projection of the fold, never the rendered ``learned.md``: the hot tier is a
derived file, and reading state back out of one is the second-truth problem D107 removes.
After a context edit, ``walle render`` puts the change in the hot tier like any other.

The view's format
-----------------
Comment lines (``#`` and blank) come first, then one entry per fact::

    ### [#d-mbp:6] dinner at eight

    Maya's dinner is at eight. On Fridays, nine.

A header is a whole line, ``### [#<ref or new>] <summary>``. Everything up to the next header
is the entry's text, with blank lines at either end trimmed. A text line that would read as a
header, or as an escaped one, is written with one more leading backslash and read back with
one fewer, so any text round-trips. Anything before the first header that is not a comment is
refused rather than silently dropped.

A save is checked whole before anything is written: every ref must be one the view showed,
none may appear twice, every summary must fit on one line of 120 characters, and no text may
be empty. A saved file that is empty, like an editor quit without saving, changes nothing. A
crashed editor and a deliberate wipe look the same, and only one of them is safe to act on;
``forget`` is there for the other.

Two runners, step by step (the walkthrough this file is required to carry)
--------------------------------------------------------------------------
Person A runs ``walle context`` and edits ``d-mbp:6``. Person B, on another terminal, runs
``walle forget d-mbp:6``. Writer W appends observations throughout.

1. A brings the index up to date and folds it, **without a lock**, and opens the editor on the
   view. A may keep it open for an hour, and holding ``staging.lock`` that long would stall
   every review and every forget.
2. B takes ``staging.lock``, brings the index up to date, folds it, finds ``d-mbp:6`` current,
   and appends ``supersede d-mbp:6`` as ``d-mbp:9`` under its stream lock, ``fsync``ed. B
   releases ``staging.lock``.
3. W appends ``d-mbp:10``. It needs only its stream lock and changes no assertion.
4. A saves. ``apply`` parses and checks the save (no lock, no IO), then takes ``staging.lock``
   and **only now** reads the log again. ``d-mbp:6`` is no longer current, so A raises
   ``StaleViewError("d-mbp:6 ... forgotten by d-mbp:9")`` having written nothing. The CLI keeps
   A's edited text in ``principals/p-<name>/context-unsaved-*.md`` and says where.

Had A trusted the view it read in step 1, it would append a replacement for a fact B had just
retired, and the fact would come back under a new ref. Had B arrived while A held the lock in
step 4, B would wait, re-read, and find ``d-mbp:6`` already retired by A's edit.
``test_an_apply_that_waits_on_another_decision_reads_the_log_after`` stages step 4 with a second
process. Facts A's save does not touch are not checked: a fact B kept meanwhile was not in A's
view, so A's save neither edits nor removes it.

A save writes its events under one hold of its stream lock, in this order: each edit's
assertion and then its supersede, then removals, then additions. Every append is ``fsync``ed
before the next. A crash part-way leaves a prefix. After an edit's assertion and before its
supersede, both texts are current. That is a duplicate the next ``walle context`` shows, never
a lost fact, which is why the assertion goes first. ``PartialWriteError`` names what was written
when an append fails part-way.

Lock order is ``staging.lock``, then ``index.lock``, then stream files (shared), released, then
one stream file (exclusive): the order ``staging.py`` already uses, so no cycle exists.
"""

from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from walle import index
from walle.events import (
    ACTOR_USER,
    Assertion,
    Proposal,
    Supersede,
    WorldTime,
    make_ref,
    now_instant,
    parse_ref,
)
from walle.paths import log_dir, staging_lock_path
from walle.staging import clean_body, clean_summary, summarise
from walle.why import Ledger
from walle.writer import StreamWriter

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's context editing needs POSIX file locking (fcntl); Windows is not supported in "
        "Phase 0. Run it under WSL."
    )

import fcntl

EDIT_REASON = "edited in walle context"
REMOVE_REASON = "removed in walle context"
NEW = "new"

_WORLD_TIME: TypeAdapter[str] = TypeAdapter(WorldTime)
_HEADER = re.compile(r"### \[#(new|d-[a-z0-9][a-z0-9_-]{0,62}:[1-9][0-9]{0,18})\]\s*(.*)")
_HEADERISH = re.compile(r"\\*### \[#")


class NotCurrentError(Exception):
    """``forget`` was given a ref that is not a current assertion. Nothing was written."""


class ContextError(ValueError):
    """A saved view that cannot be applied as written. Nothing was written."""


class StaleViewError(Exception):
    """A fact the save changes is no longer current: something else retired it after the view
    was read. Nothing was written."""


class PartialWriteError(Exception):
    """An append failed part-way through a save. ``written`` is what reached the log."""

    def __init__(self, written: tuple[str, ...], cause: BaseException) -> None:
        shown = ", ".join(written) if written else "nothing"
        super().__init__(
            f"writing the save failed part-way ({cause}); already in the log: {shown}. "
            "Run `walle context` again to see what it holds now."
        )
        self.written = written


# --- forget -----------------------------------------------------------------------------


def forget(
    root: Path,
    principal: str,
    device: str,
    ref: str,
    *,
    valid_to: str | None = None,
    reason: str | None = None,
) -> str:
    """Retire the current assertion ``ref`` with a ``supersede``. Returns the supersede's ref.

    ``valid_to`` is when the fact stopped being true in the world, if known (D93). Raises
    ``NotCurrentError`` when ``ref`` is not a current assertion, and ``ValueError`` for a bad
    ref, name or world time, all before anything is written.
    """
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    parse_ref(ref)
    if valid_to is not None:
        valid_to = _WORLD_TIME.validate_python(valid_to)
    _require_log(root, principal)

    with _exclusive(staging_lock_path(root, principal)):
        ledger = Ledger(index.update(root, principal).records)
        if ref not in ledger.current:
            raise NotCurrentError(_not_current(ledger, ref))
        written = writer.append(
            Supersede(
                ts=now_instant(),
                actor=ACTOR_USER,
                target=ref,
                reason=reason or None,
                valid_to=valid_to,
            )
        )
    assert written.seq is not None  # the writer always assigns one
    return make_ref(writer.stream, written.seq)


def _not_current(ledger: Ledger, ref: str) -> str:
    event = ledger.events.get(ref)
    if event is None:
        return f"no event {ref} in the log"
    if not isinstance(event, Assertion):
        hint = (
            "; a staged proposal is dropped with `walle review`"
            if isinstance(event, Proposal)
            else ""
        )
        return f"{ref} is a {event.type!r} event, not an assertion{hint}"
    return f"{ref} was already {ledger.status(ref)}"


# --- the view ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One fact as the view shows it. ``ref`` is ``None`` for an entry headed ``[#new]``."""

    ref: str | None
    summary: str
    body: str


@dataclass(frozen=True)
class View:
    """What ``walle context`` showed: the text, and the entries it parses to."""

    principal: str
    text: str
    entries: tuple[Entry, ...] = field(repr=False)


def open_view(root: Path, principal: str) -> View:
    """The live view of ``principal``'s current assertions, oldest first, from the log now."""
    ledger = Ledger(index.update(Path(root), principal).records)
    beliefs = sorted(
        ledger.current.values(),
        key=lambda b: (datetime.fromisoformat(b.assertion.ts), parse_ref(b.ref)),
    )
    out = [_preamble(principal)]
    for belief in beliefs:
        summary = " ".join(belief.assertion.summary.split())
        body = "\n".join(_escape(line) for line in belief.assertion.body.strip().splitlines())
        out.append(f"\n### [#{belief.ref}] {summary}\n\n{body}\n")
    text = "".join(out)
    return View(principal, text, parse(text))


def _preamble(principal: str) -> str:
    # No line here may contain a header: the comment lines are not parsed as entries.
    return (
        f"# walle context for {principal}: what walle believes now, one entry per fact.\n"
        "#\n"
        "# Change an entry's summary (its header line) or its text, then save and quit.\n"
        "# walle appends the change to the log; the old version stays there.\n"
        "# Delete an entry, header and all, to forget it.\n"
        "# To add one, copy a header line, write new in place of its ref so the tag\n"
        "# reads [#new], and put the text below it.\n"
        "# Leave the other tags as they are. Lines up here starting with # are ignored.\n"
        "# Saving an empty file, or quitting without saving, changes nothing.\n"
    )


def _escape(line: str) -> str:
    return f"\\{line}" if _HEADERISH.match(line) else line


def _unescape(line: str) -> str:
    return line[1:] if line.startswith("\\") and _HEADERISH.match(line) else line


def parse(text: str) -> tuple[Entry, ...]:
    """The entries in a view's text. ``ContextError``, with a line number, for text before the
    first header that is not a comment. Summaries are whitespace-collapsed, texts trimmed."""
    entries: list[Entry] = []
    head: tuple[str | None, str] | None = None
    lines: list[str] = []

    def close() -> None:
        if head is not None:
            entries.append(Entry(head[0], head[1], "\n".join(lines).strip()))

    for number, line in enumerate(text.splitlines(), start=1):
        match = _HEADER.fullmatch(line.rstrip())
        if match:
            close()
            tag, summary = match.group(1), " ".join(match.group(2).split())
            head, lines = (None if tag == NEW else tag, summary), []
        elif head is not None:
            lines.append(_unescape(line))
        elif line.strip() and not line.startswith("#"):
            raise ContextError(
                f"line {number}: text before the first entry ({line.strip()[:40]!r}). Put it "
                "under a [#new] header to add it, or start the line with # to keep it as a note."
            )
    close()
    return tuple(entries)


# --- the diff ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What a save implies, checked and ready to write."""

    edits: tuple[tuple[str, Entry], ...]
    """``(old ref, new entry)``."""

    forgets: tuple[str, ...]
    adds: tuple[Entry, ...]

    def __bool__(self) -> bool:
        return bool(self.edits or self.forgets or self.adds)


def plan(view: View, edited: str) -> Plan:
    """Diff a save against the view it came from. ``ContextError`` names the first problem."""
    shown = {e.ref: e for e in view.entries if e.ref is not None}
    seen: set[str] = set()
    edits: list[tuple[str, Entry]] = []
    adds: list[Entry] = []
    for entry in parse(edited):
        label = f"[#{entry.ref or NEW}] {entry.summary[:40]!r}"
        body = _checked(label, clean_body, entry.body, "the text is empty")
        if entry.ref is None:
            summary = summarise(body) if not entry.summary else entry.summary
            adds.append(Entry(None, _checked(label, clean_summary, summary), body))
            continue
        if entry.ref in seen:
            raise ContextError(f"{entry.ref} appears twice; keep one entry per ref")
        seen.add(entry.ref)
        before = shown.get(entry.ref)
        if before is None:
            raise ContextError(
                f"[#{entry.ref}] is not in the view walle opened. Leave the tags as they are; "
                "to add an entry, head it [#new]."
            )
        summary = _checked(label, clean_summary, entry.summary)
        if (summary, body) != (before.summary, before.body):
            edits.append((entry.ref, Entry(entry.ref, summary, body)))
    forgets = tuple(ref for ref in shown if ref not in seen)
    return Plan(tuple(edits), forgets, tuple(adds))


def _checked(label: str, check: Callable[[str], str], value: str, empty: str | None = None) -> str:
    try:
        return check(value)
    except ValueError as exc:
        raise ContextError(f"{label}: {empty or exc}") from None


# --- apply ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Applied:
    """What one save wrote. Every tuple is in the order it was written."""

    edited: tuple[tuple[str, str, str], ...] = ()
    """``(old ref, new assertion, supersede retiring the old)``."""

    forgotten: tuple[tuple[str, str], ...] = ()
    """``(ref, supersede retiring it)``."""

    added: tuple[str, ...] = ()
    written: tuple[str, ...] = ()
    """Every ref appended, in order."""


def apply(root: Path, principal: str, device: str, view: View, edited: str) -> Applied:
    """Append the events a save implies (see the module docstring).

    Raises ``ContextError`` for a save that does not check, and ``StaleViewError`` when a fact
    it changes is no longer current. Both are raised before anything is written. An empty save,
    or one identical to the view, writes nothing and returns an empty ``Applied``.
    """
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    if not edited.strip():
        return Applied()
    changes = plan(view, edited)
    if not changes:
        return Applied()
    _require_log(root, principal)

    with _exclusive(staging_lock_path(root, principal)):
        ledger = Ledger(index.update(root, principal).records)
        touched = [ref for ref, _ in changes.edits] + list(changes.forgets)
        stale = [ref for ref in touched if ref not in ledger.current]
        if stale:
            reasons = "; ".join(f"{ref} was {ledger.status(ref)}" for ref in stale)
            raise StaleViewError(f"the log changed while the view was open: {reasons}")
        return _write(writer, ledger, changes)


def _write(writer: StreamWriter, ledger: Ledger, changes: Plan) -> Applied:
    edited: list[tuple[str, str, str]] = []
    forgotten: list[tuple[str, str]] = []
    added: list[str] = []
    written: list[str] = []

    with writer.locked() as stream:

        def append(event: Assertion | Supersede) -> str:
            seq = stream.append(event).seq
            assert seq is not None  # the writer always assigns one
            ref = make_ref(stream.stream, seq)
            written.append(ref)
            return ref

        try:
            for old_ref, entry in changes.edits:
                old = ledger.current[old_ref].assertion
                new_ref = append(
                    Assertion(
                        ts=now_instant(),
                        actor=ACTOR_USER,
                        caused_by=[old_ref],
                        body=entry.body,
                        summary=entry.summary,
                        level=old.level,
                        confidence=old.confidence,
                        evidence_count=old.evidence_count,
                        valid_from=old.valid_from,
                        valid_to=old.valid_to,
                        about=old.about,
                    )
                )
                retire = append(
                    Supersede(
                        ts=now_instant(),
                        actor=ACTOR_USER,
                        target=old_ref,
                        caused_by=[new_ref],
                        reason=EDIT_REASON,
                    )
                )
                edited.append((old_ref, new_ref, retire))
            for ref in changes.forgets:
                retire = append(
                    Supersede(ts=now_instant(), actor=ACTOR_USER, target=ref, reason=REMOVE_REASON)
                )
                forgotten.append((ref, retire))
            for entry in changes.adds:
                added.append(
                    append(
                        Assertion(
                            ts=now_instant(),
                            actor=ACTOR_USER,
                            body=entry.body,
                            summary=entry.summary,
                        )
                    )
                )
        except Exception as exc:
            if not written:
                raise  # nothing reached the log; the original error says it all
            raise PartialWriteError(tuple(written), exc) from exc

    return Applied(tuple(edited), tuple(forgotten), tuple(added), tuple(written))


# --- locks ------------------------------------------------------------------------------


def _require_log(root: Path, principal: str) -> None:
    log = log_dir(root, principal)
    if not log.is_dir():
        # Checked before staging.lock is created: a misspelt principal must not get a folder.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}")


@contextlib.contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    """Hold ``staging.lock`` exclusively: one decision about a principal's beliefs at a time."""
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
