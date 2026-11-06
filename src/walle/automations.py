"""The register of active automations, and the WIP cap on it (D6, D114, issue #95).

D6: *restraint is enforced, not encouraged.* There is a hard cap on active automations,
starting at 7, and adding one past it means retiring one (hard constraint 9). The register is
not a file and not a table. It is ``fold_automations``, a fold over ``automation`` events in
the log, so it passes the rebuild test and keeps no state outside the log (hard constraints 2
and 5), and picking which automations are active needs no model (hard constraint 3).

The rules
---------
1. **Four verbs, each an event.** ``add`` puts an automation in the register as active;
   ``retire`` takes it out, and it stays in the log as retired, never deleted (D8, D114);
   ``pin`` and ``unpin`` set and clear the human mark. An automation's identity is the ref of
   its add, so a name retired and added again is a new automation.
2. **The cap is hard.** With ``WIP_CAP`` automations active, an add is refused and writes
   nothing, and the refusal names the one to retire: the oldest unpinned active automation,
   by the ``ts`` of its add. ``add(..., retire=name)`` retires one and adds the new one under
   one hold of the lock, so a swap cannot lose its slot to another writer. The fold holds an
   add past the cap to the same rule, so an eighth that reached the log some other way (a
   second device, a hand-written line) is reported, never applied. One slot is one workflow:
   an add's ``caused_by`` names at most one ref that is not a retire the register holds —
   the kept workflow it runs — beside at most one retire, the swap. An add naming eight
   workflows would take one slot and, through ``walle.builder.run_step``, authorise all
   eight; it is refused and reported like an eighth (#130).
3. **``pinned`` is a floor (D114).** A pinned automation is never offered for retirement, and
   a retire or a swap naming it is refused until a person unpins it, which is its own event.
   If every active automation is pinned, the refusal says so and offers none.
4. **Only a person changes the register (D72).** Creating, updating or deleting an automation
   is a persistent-authority act, and narrowing is floored along with broadening. No machine
   actor (``walle.events.MACHINE_ACTORS``) adds, retires, pins or unpins: ``add`` and the rest
   refuse before writing, and the fold reports such a line rather than applying it.
5. **Retire is how an automation leaves; a tombstone is not.** A ``supersede`` of an
   automation event is reported and changes nothing. Applied, it would remove a pinned
   automation without the unpin rule 3 requires, or free a slot with no retire on record.
   This is the inbox's rule 6 in another place: a fold says what a supersede means for it.
6. **One check, two callers.** ``refusal`` is the only test of whether an event may change the
   register. The writes call it before appending and the fold calls it after reading.
7. **Off by default.** Nothing here writes unless the owner has set ``WALLE_AUTOMATIONS=on``
   in the environment, outside the log (D117: install is not enable, and the owner's
   condition on #95). Reading is not acting: ``active`` and the fold work either way. Nothing
   calls the writes on its own, and the CLI has no command for them.

Anything the fold cannot apply is reported in ``unapplied`` with a reason, never dropped.
Events are applied in ``walle.merge``'s order — each stream in its own seq order, streams
interleaved by time — like every other fold, so the same records in any order give the same
register, and a retire from the laptop of an add on the phone finds it (issue #120).

Two writers, step by step (the walkthrough this file is required to carry)
-------------------------------------------------------------------------------
Ines has six automations active. Writer A adds ``car-service`` from the laptop (stream
``d-mbp``); writer B adds ``gym-class`` from the phone (stream ``d-phone``), at the same
moment. Each would be the seventh.

1. A takes ``automations.lock`` (exclusive). B blocks here, having read nothing.
2. A reads every stream of Ines's log, each under its own ``LOCK_SH`` for a moment, and folds
   them: six active, so ``car-service`` fits.
3. A appends its add as ``d-mbp:7`` under the stream's ``LOCK_EX``, which the writer holds
   until the bytes are ``fsync``ed, then releases ``automations.lock``.
4. B wakes **and only now** reads the log. It sees seven active, writes nothing, and raises
   ``CapReachedError`` naming the oldest unpinned automation to retire.

Had B folded before waiting for the lock, it would wake holding a stale count of six and
append an eighth. The fold would still refuse it — rule 2 — but a log that contradicts itself
is a bug to prevent, not one to fold around.

Lock order is ``automations.lock``, then stream files (shared, each released), then one stream
file (exclusive). Nothing takes ``automations.lock`` while holding another lock, so no cycle
exists. The lock is held for one decision, never across waiting on a person.

What this does not cover: two devices that do not share a filesystem. Each takes its own
lock, and both may add a seventh before their logs meet. Rule 2 still gives one register on
every machine once they meet — the earlier add by its stamp is applied (``walle.merge``; at
the same instant, the stream that sorts first), and the other is reported as past the cap.
A retire or pin stamped *before* the add it names — a device whose clock is behind — is
reported as naming an automation the register does not hold yet; the order does not guess.
With one device there is one stream, and seq order is the order things happened.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from walle.events import (
    ACTOR_USER,
    MACHINE_ACTORS,
    Automation,
    AutomationVerb,
    Supersede,
    make_ref,
    now_instant,
    parse_ref,
)
from walle.merge import fold_order
from walle.paths import automations_lock_path, log_dir
from walle.reader import Record, read_file
from walle.resolver import live_events
from walle.writer import StreamWriter

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's automation register needs POSIX file locking (fcntl); Windows is not "
        "supported in Phase 0. Run it under WSL."
    )

import fcntl

AUTOMATIONS_SWITCH = "WALLE_AUTOMATIONS"
"""The environment variable the owner sets to turn the register's writes on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_INBOX``: a stray ``1`` is not consent."""

WIP_CAP = 7
"""D6's hard cap on active automations, "start at 7". Not a parameter and not an event: a
second caller with another number would fold the same log to a different register, and
moving the cap is a decision for ``CONVERSATION.md``, not a call site."""

type Status = Literal["active", "retired"]


class AutomationsOffError(Exception):
    """The owner has not turned the register on. Raised before anything is read or written."""


class RefusedError(Exception):
    """This change may not be made to the register: a machine actor, a name already active, a
    target that is not active, a pinned target, or a full register. Raised before anything is
    written."""


class CapReachedError(RefusedError):
    """The register is full (rule 2). ``suggestion`` is the automation to retire, or ``None``
    when every active one is pinned; ``pinned`` are the active ones not offered."""

    def __init__(self, message: str, *, suggestion: Entry | None, pinned: tuple[Entry, ...]):
        super().__init__(message)
        self.suggestion = suggestion
        self.pinned = pinned


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the register on: ``WALLE_AUTOMATIONS`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(AUTOMATIONS_SWITCH) == SWITCH_ON


# --- the fold ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One automation, as the fold leaves it. ``ref`` is its add, and its identity."""

    ref: str
    name: str
    title: str
    added: str
    """The ``ts`` of its add: when the log learned of it, which orders the suggestion."""
    pinned: bool = False
    pin_ref: str | None = None
    """The pin that holds it, while it is pinned."""
    retired_ref: str | None = None
    """The retire that took it out, once it is retired."""
    caused_by: tuple[str, ...] = ()
    """The add's ``caused_by``, as written: the kept workflow it runs, when ``add`` was given
    one (``workflow=``), and the retire it swapped with. ``walle.builder.run_step`` looks for
    the kept workflow here, so a workflow runs only while an active automation names it
    (D6, #121)."""

    @property
    def status(self) -> Status:
        return "active" if self.retired_ref is None else "retired"


@dataclass(frozen=True)
class Unapplied:
    """An automation event, or a supersede of one, that the fold did not apply, and why."""

    ref: str
    reason: str


@dataclass(frozen=True)
class Register:
    """The fold's answer: every automation in merge order of its add, and what was not applied."""

    entries: tuple[Entry, ...]
    unapplied: tuple[Unapplied, ...] = ()

    @property
    def active(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.status == "active")

    @property
    def retired(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.status == "retired")

    @property
    def pinned(self) -> tuple[Entry, ...]:
        """The active automations that are pinned, in merge order."""
        return tuple(e for e in self.active if e.pinned)

    @property
    def full(self) -> bool:
        return len(self.active) >= WIP_CAP

    @property
    def suggestion(self) -> Entry | None:
        """The automation to retire to make room (rule 2): the oldest unpinned active one, by
        the ``ts`` of its add and then by ref. ``None`` when every active one is pinned."""
        unpinned = [e for e in self.active if not e.pinned]
        if not unpinned:
            return None
        return min(unpinned, key=lambda e: (datetime.fromisoformat(e.added), parse_ref(e.ref)))

    def find(self, name: str) -> Entry | None:
        """The active automation called ``name``, if there is one."""
        return next((e for e in self.active if e.name == name), None)

    def entry(self, ref: str) -> Entry | None:
        """The automation added at ``ref``, active or retired, if the register holds it."""
        return next((e for e in self.entries if e.ref == ref), None)


def full_message(register: Register, name: str) -> str:
    """What a refused add says (rule 2): the count, and which one to retire, or that none can
    be offered because every active automation is pinned."""
    head = (
        f"the register is full: {len(register.active)} of {WIP_CAP} automations are active, "
        f"so '{name}' is past the cap"
    )
    offer = register.suggestion
    if offer is None:
        return (
            f"{head}, and every active automation is pinned: a person must unpin one before "
            "it can be retired"
        )
    pinned = ", ".join(f"'{e.name}' ({e.ref})" for e in register.pinned)
    tail = f" Pinned, so not offered: {pinned}." if pinned else ""
    return (
        f"{head}. Retire one to make room: the oldest unpinned is '{offer.name}' "
        f"({offer.ref}).{tail}"
    )


def refusal(register: Register, event: Automation) -> str | None:
    """Why ``event`` may not change ``register`` as it stands, or ``None`` if it may (rules 2
    to 4). Pure: the one check the writes and the fold share (rule 6)."""
    actor = event.actor
    if actor in MACHINE_ACTORS:
        return f"{actor!r} is part of walle, not a person: only a person changes the register (D72)"
    if event.verb is AutomationVerb.ADD:
        assert event.name is not None  # the event's validator requires it
        if (same := register.find(event.name)) is not None:
            return f"'{event.name}' is already active as {same.ref}"
        if register.full:
            return full_message(register, event.name)
        retires = {e.retired_ref for e in register.retired}
        swapped = [c for c in event.caused_by if c in retires]
        workflows = [c for c in event.caused_by if c not in retires]
        if len(workflows) > 1:
            return (
                f"'{event.name}' names {len(workflows)} workflows in caused_by; one automation "
                "is one slot under the cap and runs one workflow (D6, hard constraint 9)"
            )
        if len(swapped) > 1:
            return (
                f"'{event.name}' names {len(swapped)} retires in caused_by; a swap retires one "
                "to make room for one (rule 2)"
            )
        return None
    assert event.target is not None  # the event's validator requires it
    target = register.entry(event.target)
    verb = event.verb.value
    if target is None:
        return f"{verb} names {event.target}, which is not an automation the register holds"
    if target.status == "retired":
        return f"{verb} names '{target.name}' ({target.ref}), which is retired"
    if event.verb is AutomationVerb.RETIRE and target.pinned:
        return (
            f"'{target.name}' ({target.ref}) is pinned: a person unpins it before it can be "
            "retired (D114)"
        )
    if event.verb is AutomationVerb.PIN and target.pinned:
        return f"'{target.name}' ({target.ref}) is already pinned"
    if event.verb is AutomationVerb.UNPIN and not target.pinned:
        return f"'{target.name}' ({target.ref}) is not pinned"
    return None


def _apply(register: Register, ref: str, event: Automation) -> Register:
    """``register`` with ``event`` applied. The caller has already checked ``refusal``."""
    if event.verb is AutomationVerb.ADD:
        assert event.name is not None and event.title is not None
        added = Entry(
            ref=ref,
            name=event.name,
            title=event.title,
            added=event.ts,
            caused_by=tuple(event.caused_by),
        )
        return replace(register, entries=(*register.entries, added))
    entries = []
    for e in register.entries:
        if e.ref == event.target:
            if event.verb is AutomationVerb.RETIRE:
                e = replace(e, retired_ref=ref)
            elif event.verb is AutomationVerb.PIN:
                e = replace(e, pinned=True, pin_ref=ref)
            else:
                e = replace(e, pinned=False, pin_ref=None)
        entries.append(e)
    return replace(register, entries=tuple(entries))


def fold_automations(records: Iterable[Record]) -> Register:
    """Fold records from any number of streams into the register as it stands.

    ``records`` is what ``walle.reader`` returns. The same record passed twice counts once,
    and the order they arrive in does not matter: events are applied in merge order. A live
    supersede of an automation event is reported, and the event it names still counts (rule
    5); ``live_events`` is still what decides whether that supersede is itself live.
    """
    events, live = live_events(records)
    register = Register(entries=())
    unapplied: list[Unapplied] = []
    for ref in fold_order(events):
        event = events[ref]
        if isinstance(event, Supersede):
            target = events.get(event.target)
            if live[ref] and isinstance(target, Automation):
                unapplied.append(
                    Unapplied(
                        ref,
                        f"supersedes {event.target}, an automation {target.verb.value}: an "
                        "automation leaves the register by retire, and a pin by unpin, never "
                        "by a tombstone",
                    )
                )
            continue
        if not isinstance(event, Automation):
            continue
        if (why := refusal(register, event)) is not None:
            unapplied.append(Unapplied(ref, why))
            continue
        register = _apply(register, ref, event)
    return replace(register, unapplied=tuple(unapplied))


# --- reading and writing -------------------------------------------------------------------


def active(root: Path, principal: str) -> tuple[Entry, ...]:
    """Every active automation, in merge order of its add, from the log as it is now. Works
    while the register is off: reading is not acting. A principal with no log raises
    ``FileNotFoundError``."""
    log = log_dir(Path(root), principal)
    if not log.is_dir():
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}")
    return fold_automations(_read(Path(root), principal)).active


def add(
    root: Path,
    principal: str,
    device: str,
    *,
    name: str,
    title: str,
    note: str | None = None,
    retire: str | None = None,
    workflow: str | None = None,
    actor: str = ACTOR_USER,
    environ: Mapping[str, str] | None = None,
) -> Entry:
    """Add an automation to the register, active, and return it.

    With ``workflow``, the ref of a kept workflow (``walle.builder.workflow``), the add's
    ``caused_by`` names it: that is what binds the automation to the workflow it runs, and
    what ``walle.builder.run_step`` requires before a step of that workflow runs (#121). The
    ref must be an event this principal's log already holds — checked under the lock, from
    the same read the register is folded from, and refused with ``RefusedError`` before
    anything is written, because an append-only log cannot take a dangling cause back
    (D111; the writer refuses it again as the last line). Whether it is a live, kept
    workflow is the Builder's question, asked at every run. With ``retire``, the active
    automation of that name is
    retired first, in the same hold of ``automations.lock``, and the add's ``caused_by``
    names that retire as well (rule 2). Raises
    ``AutomationsOffError`` while off; ``RefusedError`` for a machine actor, a name already
    active, or a ``retire`` that names nothing active or something pinned; and
    ``CapReachedError`` when the register is full, naming which one to retire. All before
    anything is written. A malformed name or title is a ``ValueError`` (pydantic's
    ``ValidationError``), also before.
    """
    _require_on(environ)
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    event = Automation(
        ts=now_instant(),
        actor=actor,
        verb=AutomationVerb.ADD,
        name=name,
        title=title,
        note=note,
        caused_by=[] if workflow is None else [workflow],
    )
    _require_person(actor)
    with _exclusive(root, principal):
        records = _read(root, principal)
        if workflow is not None and all(r.ref != workflow for r in records):
            raise RefusedError(
                f"workflow {workflow} is not in the log of principal {principal!r}; an "
                "automation is bound to an event that exists (D111)"
            )
        register = fold_automations(records)
        swap: Automation | None = None
        if retire is not None:
            out = _active_named(register, retire)
            swap = Automation(
                ts=event.ts,
                actor=actor,
                verb=AutomationVerb.RETIRE,
                target=out.ref,
                note=f"retired to make room for '{name}'",
            )
            if (why := refusal(register, swap)) is not None:
                raise RefusedError(why)
            # The ref is a stand-in: only the add's refusal is read from this register.
            register = _apply(register, "d-pending:1", swap)
        if (why := refusal(register, event)) is not None:
            if register.full and register.find(name) is None:
                raise CapReachedError(why, suggestion=register.suggestion, pinned=register.pinned)
            raise RefusedError(why)
        if swap is not None:
            retired = writer.append(swap)
            assert retired.seq is not None  # the writer always assigns one
            event = Automation.model_validate(
                {
                    **event.model_dump(exclude_none=True),
                    "caused_by": [*event.caused_by, make_ref(writer.stream, retired.seq)],
                }
            )
        written = writer.append(event)
    assert written.seq is not None
    return Entry(
        ref=make_ref(writer.stream, written.seq),
        name=name,
        title=title,
        added=written.ts,
        caused_by=tuple(written.caused_by),
    )


def retire(
    root: Path,
    principal: str,
    device: str,
    name: str,
    *,
    note: str | None = None,
    actor: str = ACTOR_USER,
    environ: Mapping[str, str] | None = None,
) -> Entry:
    """Retire the active automation called ``name``; it stays in the log as retired. Refused
    while it is pinned (rule 3). Returns it as retired."""
    return _change(root, principal, device, name, AutomationVerb.RETIRE, note, actor, environ)


def pin(
    root: Path,
    principal: str,
    device: str,
    name: str,
    *,
    note: str | None = None,
    actor: str = ACTOR_USER,
    environ: Mapping[str, str] | None = None,
) -> Entry:
    """Pin the active automation called ``name``: it is never offered for retirement and
    cannot be retired until a person unpins it. Returns it as pinned."""
    return _change(root, principal, device, name, AutomationVerb.PIN, note, actor, environ)


def unpin(
    root: Path,
    principal: str,
    device: str,
    name: str,
    *,
    note: str | None = None,
    actor: str = ACTOR_USER,
    environ: Mapping[str, str] | None = None,
) -> Entry:
    """Clear the pin on the active automation called ``name``. Returns it as unpinned."""
    return _change(root, principal, device, name, AutomationVerb.UNPIN, note, actor, environ)


def _change(
    root: Path,
    principal: str,
    device: str,
    name: str,
    verb: AutomationVerb,
    note: str | None,
    actor: str,
    environ: Mapping[str, str] | None,
) -> Entry:
    """Retire, pin or unpin one active automation, deciding under ``automations.lock`` with
    the log read only after the lock is taken (see the walkthrough). Raises
    ``AutomationsOffError`` while off, ``FileNotFoundError`` for a principal with no log, and
    ``RefusedError`` when the change may not be made — all before anything is written."""
    _require_on(environ)
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    _require_person(actor)
    log = log_dir(root, principal)
    if not log.is_dir():
        # Checked before automations.lock is created: a misspelt principal gets no folder.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to {verb}")
    with _exclusive(root, principal):
        register = fold_automations(_read(root, principal))
        target = _active_named(register, name)
        event = Automation(ts=now_instant(), actor=actor, verb=verb, target=target.ref, note=note)
        if (why := refusal(register, event)) is not None:
            raise RefusedError(why)
        written = writer.append(event)
    assert written.seq is not None
    changed = _apply(register, make_ref(writer.stream, written.seq), written).entry(target.ref)
    assert changed is not None
    return changed


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise AutomationsOffError(
            f"the automation register is off; the owner turns it on with "
            f"{AUTOMATIONS_SWITCH}={SWITCH_ON}"
        )


def _require_person(actor: str) -> None:
    """Rule 4, before any lock or file: a machine actor gets nowhere near the log."""
    if actor in MACHINE_ACTORS:
        raise RefusedError(
            f"{actor!r} is part of walle, not a person: only a person changes the register (D72)"
        )


def _active_named(register: Register, name: str) -> Entry:
    found = register.find(name)
    if found is None:
        raise RefusedError(f"no active automation named '{name}'")
    return found


def _read(root: Path, principal: str) -> list[Record]:
    """Every record in every stream of the principal's log, each read under its own
    ``LOCK_SH``. No index: the register's answer must not depend on a derived file."""
    log = log_dir(root, principal)
    if not log.is_dir():
        return []
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


@contextlib.contextmanager
def _exclusive(root: Path, principal: str) -> Iterator[None]:
    """Hold ``automations.lock`` exclusively: one change to the register per principal at a
    time."""
    lock = automations_lock_path(root, principal)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
