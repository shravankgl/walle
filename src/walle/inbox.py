"""The inbox: where a run that nobody is watching asks a person for something (D63, D78).

A mission runs for forty minutes with nobody at the keyboard (D67). When it needs a yes, an
answer or just to be seen, it writes an ``ask`` event: an approval, a question or a
notification. A person answers from any surface — the app, Slack, the CLI after resuming —
with an ``answer`` event. The inbox is not a file and not a table. It is ``fold_inbox``, a fold
over the log, so a restart loses nothing: the waiting run's item, and the mission step it
holds, are read back from the log by whoever starts next (hard constraint 5).

The rules
---------
1. **An item is ``(session, tool_call_id)``.** Asking again with the same pair is the same
   item: ``ask`` returns it and writes nothing, and a repeated ask that reached the log some
   other way is reported, with the item it repeats. A run that restarts and re-issues its call
   does not ask the person twice.
2. **Resolved once; the first answer wins.** An item goes ``pending -> resolved`` exactly once.
   The first live ask's first fitting answer *in ref order* is the resolution, and every later
   answer is reported as late. ``answer`` takes ``inbox.lock`` before it reads, so on one
   machine the second responder waits, reads the first answer, writes nothing and is told it.
3. **An answer must fit.** ``FITS`` says which outcomes fit which kind of item; a question with
   ``choices`` takes only one of them. ``misfit`` is the one check, used by ``answer`` before
   writing and by the fold after reading, so a line that bypassed ``answer`` still cannot
   resolve anything it should not.
4. **Only a person resolves; the kernel only orphans.** No machine actor — observer, proposer,
   curator, kernel, MCP — can approve, deny, answer or acknowledge. The kernel writes exactly
   one outcome, ``orphaned``, for an item whose session has died, because an orphaned approval
   can never be meaningfully answered, and orphaned is never a yes.
5. **Unattended is routing, not permission.** ``route`` says whether the person is reached
   inline or through the inbox, and ``Ask.unattended`` records which. Nothing in ``misfit``
   reads it: the same answers fit the same item, by the same actors, either way. So an
   unattended run never approves itself, and a person who is away gains nothing for the run.
6. **Answers are final.** A superseded *ask* is withdrawn: it leaves the inbox, and an answer
   to it is reported. A superseded *answer* still stands, because the run it released may
   already have acted on it. Changing one's mind is a new ask, not an edit of the old answer.
7. **Off by default.** Nothing here writes unless the owner has set ``WALLE_INBOX=on`` in the
   environment, outside the log (D117: install is not enable). Reading is not acting:
   ``pending`` and the fold work either way.

Anything the fold cannot apply is reported in ``unapplied`` with a reason, never dropped.

Two responders, step by step (the walkthrough this file is required to carry)
-------------------------------------------------------------------------------
Lena's approval ``d-mbp:2`` is pending. Responder A approves from the app (stream ``d-mbp``);
responder B denies from Slack (stream ``d-phone``), at the same moment. Runner R, meanwhile,
asks a new question.

1. A takes ``inbox.lock`` (exclusive). B blocks here, having read nothing.
2. A reads every stream of Lena's log, each under its own ``LOCK_SH`` for a moment, and folds
   them: ``d-mbp:2`` is pending, and approved fits it.
3. R calls ``ask`` and blocks on ``inbox.lock`` too: a new item has to be checked against the
   items already there, under the same lock, or two runs re-issuing one call would both write.
4. A appends its answer as ``d-mbp:3`` under the stream's ``LOCK_EX``, which the writer holds
   until the bytes are ``fsync``ed, then releases ``inbox.lock``.
5. B wakes **and only now** reads the log. It sees ``d-mbp:3`` resolving ``d-mbp:2``, writes
   nothing, and returns ``Resolution(won=False)`` carrying A's approval. Both responders are
   told the same result.
6. R wakes, reads, finds no item for its pair, and appends its ask.

Had B checked "still pending?" before waiting for the lock, it would wake holding a stale
answer and append a deny beside A's approve. The fold would still give one result — rule 2 —
but a log that contradicts itself is a bug to prevent, not one to fold around.

Lock order is ``inbox.lock``, then stream files (shared, each released), then one stream file
(exclusive). Nothing takes ``inbox.lock`` while holding another lock, so no cycle exists. The
lock is held for one decision, never across waiting on a person.

What this does not cover: two devices that do not share a filesystem. Each takes its own
``inbox.lock``, and an item answered on both before their logs meet has two answers. Rule 2
still gives one result on every machine once they meet — the first in ref order, which is
stream order and then seq, not wall-clock order — and the other is reported as late.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from walle.events import (
    ACTOR_CURATOR,
    ACTOR_KERNEL,
    ACTOR_MCP,
    ACTOR_OBSERVER,
    ACTOR_PROPOSER,
    ACTOR_USER,
    Answer,
    AnswerOutcome,
    Ask,
    AskKind,
    make_ref,
    now_instant,
    parse_ref,
)
from walle.paths import inbox_lock_path, log_dir
from walle.reader import Record, read_file
from walle.resolver import live_events, retired_by
from walle.writer import StreamWriter

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's inbox needs POSIX file locking (fcntl); Windows is not supported in Phase 0. "
        "Run it under WSL."
    )

import fcntl

INBOX_SWITCH = "WALLE_INBOX"
"""The environment variable the owner sets to turn the inbox on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_MISSIONS``: a stray ``1`` is not consent."""

FITS: dict[AskKind, frozenset[AnswerOutcome]] = {
    AskKind.APPROVAL: frozenset({AnswerOutcome.APPROVED, AnswerOutcome.DENIED}),
    AskKind.QUESTION: frozenset({AnswerOutcome.ANSWERED}),
    AskKind.NOTIFICATION: frozenset({AnswerOutcome.ACKNOWLEDGED}),
}
"""Rule 3: the outcomes a person may give each kind of item. ``orphaned`` fits every kind and
is the kernel's alone (rule 4), so it is not listed here."""

MACHINE_ACTORS = frozenset({ACTOR_OBSERVER, ACTOR_PROPOSER, ACTOR_CURATOR, ACTOR_KERNEL, ACTOR_MCP})
"""Rule 4: the actors that are parts of walle, not people. None of them resolves an item."""

type Route = Literal["inline", "inbox"]
type Status = Literal["pending", "resolved"]


class InboxOffError(Exception):
    """The owner has not turned the inbox on. Raised before anything is read or written."""


class NotAnswerableError(Exception):
    """The answer cannot resolve this item: no such item, withdrawn, or the answer does not fit
    or is not the actor's to give. Raised before anything is written."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the inbox on: ``WALLE_INBOX`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(INBOX_SWITCH) == SWITCH_ON


def route(*, unattended: bool) -> Route:
    """Rule 5: where the person is reached. That is all unattended changes."""
    return "inbox" if unattended else "inline"


def misfit(ask: Ask, answer: Answer) -> str | None:
    """Why ``answer`` cannot resolve ``ask``, or ``None`` if it can (rules 3 and 4).

    Pure, and blind to ``ask.unattended`` on purpose (rule 5). Does not know whether the item
    is already resolved; that is the fold's to say.
    """
    outcome, actor = answer.outcome, answer.actor
    if outcome is AnswerOutcome.ORPHANED:
        if actor != ACTOR_KERNEL:
            return f"only the kernel orphans an item, not {actor!r}"
        return None
    if outcome not in FITS[ask.kind]:
        return f"{outcome.value} does not fit this {ask.kind.value}"
    if actor in MACHINE_ACTORS:
        return f"{actor!r} is part of walle, not a person: only a person resolves this {ask.kind}"
    if ask.choices and answer.value not in ask.choices:
        return f"{answer.value!r} is not one of {ask.choices}"
    return None


# --- the fold ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One inbox item, as the fold leaves it."""

    ref: str
    ask: Ask
    answer_ref: str | None = None
    answer: Answer | None = None

    @property
    def status(self) -> Status:
        return "pending" if self.answer is None else "resolved"

    @property
    def outcome(self) -> AnswerOutcome | None:
        return None if self.answer is None else self.answer.outcome


@dataclass(frozen=True)
class Unapplied:
    """A live ask or an answer the fold could not apply, and why."""

    ref: str
    reason: str


@dataclass(frozen=True)
class InboxState:
    """The fold's answer: every item in ref order of its ask, and what could not be applied."""

    items: tuple[Item, ...]
    unapplied: tuple[Unapplied, ...]

    def item(self, ref: str) -> Item:
        """The item asked at ``ref``. ``KeyError`` if no live, first ask is there."""
        for item in self.items:
            if item.ref == ref:
                return item
        raise KeyError(f"{ref} is not an inbox item")

    def find(self, session: str, tool_call_id: str) -> Item | None:
        """The item for one call, if it has been asked (rule 1)."""
        for item in self.items:
            if (item.ask.session, item.ask.tool_call_id) == (session, tool_call_id):
                return item
        return None

    @property
    def pending(self) -> tuple[Item, ...]:
        return tuple(item for item in self.items if item.answer is None)


def fold_inbox(records: Iterable[Record]) -> InboxState:
    """Fold records from any number of streams into the inbox as it stands.

    ``records`` is what ``walle.reader`` returns. The same record passed twice counts once, and
    the order they arrive in does not matter: asks and answers are applied in ref order.
    """
    events, live = live_events(records)
    ordered = sorted(events, key=parse_ref)

    items: dict[str, Item] = {}
    by_call: dict[tuple[str, str], str] = {}
    repeats: dict[str, str] = {}
    unapplied: list[Unapplied] = []

    for ref in ordered:
        event = events[ref]
        if not isinstance(event, Ask) or not live[ref]:
            continue
        key = (event.session, event.tool_call_id)
        if (first := by_call.get(key)) is not None:
            repeats[ref] = first
            unapplied.append(
                Unapplied(
                    ref,
                    f"repeats session {key[0]}, tool call {key[1]}: the item is {first}",
                )
            )
            continue
        by_call[key] = ref
        items[ref] = Item(ref, event)

    for ref in ordered:
        event = events[ref]
        if not isinstance(event, Answer):  # rule 6: a superseded answer still stands
            continue
        target = events.get(event.ask)
        if target is None:
            reason = f"answers {event.ask}, which the fold was not given"
        elif not isinstance(target, Ask):
            reason = f"answers {event.ask}, which is not an inbox item"
        elif not live[event.ask]:
            reason = f"answers {event.ask}, which was withdrawn"
        elif event.ask in repeats:
            reason = f"answers {event.ask}, a repeat of {repeats[event.ask]}"
        elif (why := misfit(target, event)) is not None:
            reason = why
        elif (winner := items[event.ask].answer_ref) is not None:
            reason = f"{event.ask} was already resolved by {winner}: the first answer wins"
        else:
            items[event.ask] = Item(event.ask, target, ref, event)
            continue
        unapplied.append(Unapplied(ref, reason))

    return InboxState(items=tuple(items.values()), unapplied=tuple(unapplied))


# --- reading and writing -------------------------------------------------------------------


@dataclass(frozen=True)
class Asked:
    """What ``ask`` did: the item, and whether this call wrote it (``False``: already asked)."""

    item: Item
    new: bool


@dataclass(frozen=True)
class Resolution:
    """What ``answer`` did: the item as resolved, and whether this call's answer is the one
    that resolved it (``False``: someone answered first, and ``item`` carries their answer)."""

    item: Item
    won: bool


def pending(root: Path, principal: str) -> tuple[Item, ...]:
    """Every pending item, in ref order, from the log as it is now. Works while the inbox is
    off: reading is not acting. A principal with no log raises ``FileNotFoundError``."""
    log = log_dir(Path(root), principal)
    if not log.is_dir():
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}")
    return fold_inbox(_read(Path(root), principal)).pending


def ask(
    root: Path,
    principal: str,
    device: str,
    *,
    kind: AskKind,
    session: str,
    tool_call_id: str,
    body: str,
    choices: Iterable[str] = (),
    unattended: bool = False,
    mission: str | None = None,
    node: str | None = None,
    actor: str = ACTOR_KERNEL,
    environ: Mapping[str, str] | None = None,
) -> Asked:
    """Put an item in the inbox, or return the one already there for this call (rule 1).

    The same call asked again returns the first item, whatever its ``body`` now says; asked
    again as another kind, or for another mission step, it is a ``ValueError``. Raises
    ``InboxOffError`` while the inbox is off, and ``ValueError`` (a pydantic
    ``ValidationError``) for a malformed item, both before anything is written.
    """
    _require_on(environ)
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    event = Ask(
        ts=now_instant(),
        actor=actor,
        kind=kind,
        session=session,
        tool_call_id=tool_call_id,
        body=body,
        choices=list(choices),
        unattended=unattended,
        mission=mission,
        node=node,
    )
    with _exclusive(root, principal):
        existing = fold_inbox(_read(root, principal)).find(session, tool_call_id)
        if existing is not None:
            was = (existing.ask.kind, existing.ask.mission, existing.ask.node)
            if was != (kind, mission, node):
                # Returning it anyway would hand the caller an item it did not ask for: a
                # question's caller waiting on an approval's yes or no.
                raise ValueError(
                    f"session {session}, tool call {tool_call_id} was already asked as "
                    f"{existing.ref} ({existing.ask.kind}, mission {existing.ask.mission}, "
                    f"node {existing.ask.node}); one call is one item"
                )
            return Asked(existing, new=False)
        written = writer.append(event)
    assert written.seq is not None  # the writer always assigns one
    return Asked(Item(make_ref(writer.stream, written.seq), written), new=True)


def answer(
    root: Path,
    principal: str,
    device: str,
    ref: str,
    outcome: AnswerOutcome,
    *,
    value: str | None = None,
    actor: str = ACTOR_USER,
    surface: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Resolution:
    """Resolve the item asked at ``ref``, unless someone already has (rule 2).

    The check and the append happen under one hold of ``inbox.lock``, and the check reads the
    log only after the lock is taken (see the walkthrough). Raises ``InboxOffError`` while off,
    ``FileNotFoundError`` for a principal with no log, and ``NotAnswerableError`` when this
    answer cannot resolve the item — all before anything is written.
    """
    _require_on(environ)
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    parse_ref(ref)  # a malformed ref is a ValueError, before any lock or file
    log = log_dir(root, principal)
    if not log.is_dir():
        # Checked before inbox.lock is created: a misspelt principal must not get a folder.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to answer")
    given = Answer(
        ts=now_instant(), actor=actor, ask=ref, outcome=outcome, value=value, surface=surface
    )
    with _exclusive(root, principal):
        records = _read(root, principal)
        item = _item(records, ref)
        if (why := misfit(item.ask, given)) is not None:
            raise NotAnswerableError(f"{ref}: {why}")
        if item.answer is not None:
            return Resolution(item, won=False)
        written = writer.append(given)
    assert written.seq is not None
    return Resolution(Item(ref, item.ask, make_ref(writer.stream, written.seq), written), won=True)


def orphan(
    root: Path,
    principal: str,
    device: str,
    *,
    alive: Iterable[str],
    environ: Mapping[str, str] | None = None,
) -> tuple[Item, ...]:
    """Resolve, as ``orphaned``, every pending item whose session is not in ``alive`` (rule 4).

    ``alive`` is the caller's knowledge of which sessions are still running — at startup,
    none of the previous process's are. Returns the items this call resolved, in ref order.
    A principal with no log has nothing to orphan, and nothing is created for it.
    """
    _require_on(environ)
    root = Path(root)
    writer = StreamWriter(root, principal, device)
    living = frozenset(alive)
    if not log_dir(root, principal).is_dir():
        return ()
    resolved: list[Item] = []
    with _exclusive(root, principal):
        for item in fold_inbox(_read(root, principal)).pending:
            if item.ask.session in living:
                continue
            given = Answer(
                ts=now_instant(), actor=ACTOR_KERNEL, ask=item.ref, outcome=AnswerOutcome.ORPHANED
            )
            assert misfit(item.ask, given) is None  # orphaned by the kernel fits every kind
            written = writer.append(given)
            assert written.seq is not None
            resolved.append(Item(item.ref, item.ask, make_ref(writer.stream, written.seq), written))
    return tuple(resolved)


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise InboxOffError(
            f"the inbox is off; the owner turns it on with {INBOX_SWITCH}={SWITCH_ON}"
        )


def _read(root: Path, principal: str) -> list[Record]:
    """Every record in every stream of the principal's log, each read under its own
    ``LOCK_SH``. No index: the inbox's answer must not depend on a derived file."""
    log = log_dir(root, principal)
    if not log.is_dir():
        return []
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _item(records: list[Record], ref: str) -> Item:
    """The item asked at ``ref``, or ``NotAnswerableError`` saying what is there instead."""
    state = fold_inbox(records)
    with contextlib.suppress(KeyError):
        return state.item(ref)
    event = next((r.event for r in records if r.ref == ref), None)
    if event is None:
        raise NotAnswerableError(f"no inbox item {ref} in the log")
    if not isinstance(event, Ask):
        raise NotAnswerableError(f"{ref} is not an inbox item (it is a {event.type!r} event)")
    by = retired_by(records, ref)
    if by:
        raise NotAnswerableError(f"{ref} was withdrawn by {by[0]}")
    first = state.find(event.session, event.tool_call_id)
    raise NotAnswerableError(f"{ref} repeats {first.ref if first else 'another ask'}")


@contextlib.contextmanager
def _exclusive(root: Path, principal: str) -> Iterator[None]:
    """Hold ``inbox.lock`` exclusively: one ask or one decision per principal at a time."""
    lock = inbox_lock_path(root, principal)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
