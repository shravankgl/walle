"""The staging queue: ``walle remember`` proposes, ``walle review`` decides (D49, D60, D81, D86).

A belief that will be injected into every future context gets a human yes first (D49). So
``remember`` writes a ``proposal`` event and nothing else, and ``review`` turns each staged
proposal into one of three events:

``keep``   an ``assertion`` with the proposal's text, ``caused_by`` the proposal.
``edit``   the same, with the text the reviewer changed. The proposal's own text stays in the
           log beside it.
``drop``   a ``supersede`` targeting the proposal: a tombstone. The proposal is never deleted,
           and superseding the tombstone stages it again (hard constraint 2).

The queue is not a file. It is ``resolver.staged``, a fold over the log, so it survives the
rebuild test like everything else (hard constraint 5) and no state lives outside the log.
The resolver never puts a proposal in ``current``, so the hot tier cannot carry one until a
keep or an edit has written an assertion (D86).

Every proposal has a one-line ``summary`` from the moment it is written (D81). If the person
gives none, it is the first non-blank line of the text, whitespace collapsed, cut to 120
characters. That is a deterministic rule rather than a model's paraphrase on purpose: the
reviewer sees it and can edit it, and nothing on this path may call a model.

Two reviewers, step by step (the walkthrough this file is required to carry)
----------------------------------------------------------------------------
Reviewers A and B for principal maya, both deciding ``d-mbp:4``, a staged proposal. A keeps
it; B drops it. Writer W runs ``walle remember`` meanwhile.

1. A takes ``staging.lock`` (exclusive). B blocks here, having read nothing.
2. A brings the index up to date (``index.update``: ``index.lock``, then every stream file
   shared, for a moment) and folds the records it committed. ``d-mbp:4`` is staged.
3. W appends ``d-mbp:5``, a new proposal. It needs only its stream lock, which A no longer
   holds. A new proposal changes nothing about ``d-mbp:4``, so W does not take
   ``staging.lock``.
4. A appends its ``assertion`` (``caused_by: [d-mbp:4]``) as ``d-mbp:6`` under the stream lock,
   which the writer holds until the bytes are ``fsync``ed. Then A releases ``staging.lock``.
5. B wakes **and only now** reads the log. It sees ``d-mbp:6`` citing ``d-mbp:4``, so
   ``d-mbp:4`` is no longer staged, and B raises ``NotStagedError("already kept as
   d-mbp:6")`` having written nothing. ``walle review`` prints that and moves on.

If B had checked "still staged?" before waiting for the lock, it would wake holding a stale
answer and append a drop beside A's keep: one proposal, two decisions, and a log that says
both. ``test_a_second_reviewer_waits_and_finds_the_item_decided`` stages that with a second
process. Even then the fold gives one answer (an assertion citing a proposal decides it, a
supersede beside it changes nothing), but a log that contradicts itself is a bug to prevent,
not to fold around.

The lock is held for one decision, never across a prompt: a review that waits on a person for
ten minutes must not stall another review for ten minutes.

Lock order is ``staging.lock``, then ``index.lock``, then stream files (shared), released,
then one stream file (exclusive). The renderer and the indexer never take ``staging.lock``,
and nothing takes it while holding another lock, so no cycle exists.

What this does not cover: two devices that do not share a filesystem. Each takes its own
``staging.lock``, and a proposal reviewed on both before their logs meet gets two decisions.
Sync is deferred (D57); when it lands, the fold above is what it will have to answer to.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from walle import index
from walle.events import (
    ACTOR_USER,
    Assertion,
    Event,
    Level,
    Proposal,
    Supersede,
    make_ref,
    now_instant,
    parse_ref,
)
from walle.paths import check_name, log_dir, staging_lock_path
from walle.reader import Record
from walle.resolver import Staged, retired_by, staged
from walle.writer import StreamWriter

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's staging queue needs POSIX file locking (fcntl); Windows is not supported in "
        "Phase 0. Run it under WSL."
    )

import fcntl

SUMMARY_LIMIT = 120
"""``Assertion.summary``'s own limit, so a kept summary always fits (D81)."""

DROP_REASON = "dropped in review"


class NotStagedError(Exception):
    """The ref is not a staged proposal: no such event, not a proposal, or already decided.

    Raised before anything is written, so a refused decision leaves the log as it was.
    """


def check_device(device: str) -> str:
    """Validate a device name before a review starts asking questions it cannot record."""
    return check_name(device, kind="device")


# --- the summary ------------------------------------------------------------------------


def summarise(text: str) -> str:
    """The default one-line summary: the first non-blank line, whitespace collapsed, at most
    ``SUMMARY_LIMIT`` characters, cut with an ellipsis. ``""`` for blank text."""
    for line in text.splitlines():
        words = " ".join(line.split())
        if words:
            if len(words) <= SUMMARY_LIMIT:
                return words
            return words[: SUMMARY_LIMIT - 1].rstrip() + "…"
    return ""


def clean_summary(summary: str) -> str:
    """A given summary on one line, or ``ValueError`` saying what is wrong with it."""
    one_line = " ".join(summary.split())
    if not one_line:
        raise ValueError("the summary is empty — give one line, or leave it out")
    if len(one_line) > SUMMARY_LIMIT:
        raise ValueError(
            f"the summary is {len(one_line)} characters; it must fit on one line of "
            f"{SUMMARY_LIMIT} at most"
        )
    return one_line


def clean_body(text: str) -> str:
    """The text without surrounding whitespace, or ``ValueError`` if nothing is left."""
    body = text.strip()
    if not body:
        raise ValueError("nothing to remember — the text is empty")
    return body


# --- remember ---------------------------------------------------------------------------


def remember(
    root: Path,
    principal: str,
    device: str,
    text: str,
    *,
    summary: str | None = None,
    actor: str = ACTOR_USER,
    level: Level = Level.EXPLICIT,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> Staged:
    """Stage ``text`` as a proposal. Writes one event, and never an assertion (D86).

    Raises ``ValueError`` for an empty text, a bad summary, a bad world time or an unsafe
    name, all before anything is written.
    """
    writer = StreamWriter(Path(root), principal, device)  # validates both names first
    body = clean_body(text)
    proposal = Proposal(
        ts=now_instant(),
        actor=actor,
        body=body,
        summary=summarise(body) if summary is None else clean_summary(summary),
        level=level,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    written = writer.append(proposal)
    assert written.seq is not None  # the writer always assigns one
    return Staged(make_ref(writer.stream, written.seq), written)


# --- review -----------------------------------------------------------------------------


def pending(root: Path, principal: str) -> tuple[Staged, ...]:
    """Every staged proposal, in ref order, from the log as it is now.

    Brings the index up to date first, so a proposal appended a moment ago is in the answer.
    A principal with no log raises ``FileNotFoundError``: no log must not read as nothing to
    review.
    """
    return staged(index.update(Path(root), principal).records)


def keep(root: Path, principal: str, device: str, ref: str) -> str:
    """Keep the staged proposal ``ref`` as it is. Returns the new assertion's ref."""
    return _decide(root, principal, device, ref, lambda item: _assertion(item, None, None))


def edit(
    root: Path,
    principal: str,
    device: str,
    ref: str,
    *,
    body: str | None = None,
    summary: str | None = None,
) -> str:
    """Keep ``ref`` with the reviewer's text. ``None`` keeps the proposal's own."""
    new_body = None if body is None else clean_body(body)
    new_summary = None if summary is None else clean_summary(summary)
    return _decide(
        root, principal, device, ref, lambda item: _assertion(item, new_body, new_summary)
    )


def drop(root: Path, principal: str, device: str, ref: str) -> str:
    """Drop ``ref``: append a ``supersede`` targeting it. Returns the tombstone's ref."""
    return _decide(
        root,
        principal,
        device,
        ref,
        lambda _item: Supersede(ts=now_instant(), actor=ACTOR_USER, target=ref, reason=DROP_REASON),
    )


def _assertion(item: Staged, body: str | None, summary: str | None) -> Assertion:
    """The assertion a keep or an edit writes. Confidence and evidence start where D60 says a
    new belief starts, which are the model's defaults."""
    proposal = item.proposal
    return Assertion(
        ts=now_instant(),
        actor=ACTOR_USER,
        caused_by=[item.ref],
        body=proposal.body if body is None else body,
        summary=proposal.summary if summary is None else summary,
        level=proposal.level,
        valid_from=proposal.valid_from,
        valid_to=proposal.valid_to,
    )


def _decide(
    root: Path,
    principal: str,
    device: str,
    ref: str,
    build: Callable[[Staged], Event],
) -> str:
    """Check ``ref`` is still staged and append ``build``'s event, under ``staging.lock``.

    The check and the append happen under one hold of the lock, and the check reads the log
    only after the lock is taken (see the walkthrough above).
    """
    root = Path(root)
    writer = StreamWriter(root, principal, device)  # validates both names first
    parse_ref(ref)  # a malformed ref is a ValueError, before any lock or file
    log = log_dir(root, principal)
    if not log.is_dir():
        # Checked before staging.lock is created: a misspelt principal must not get a folder.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to review")

    with _exclusive(staging_lock_path(root, principal)):
        records = index.update(root, principal).records
        item = _still_staged(records, ref)
        written = writer.append(build(item))
    assert written.seq is not None  # the writer always assigns one
    return make_ref(writer.stream, written.seq)


def _still_staged(records: tuple[Record, ...], ref: str) -> Staged:
    """The staged item for ``ref``, or ``NotStagedError`` saying what it is instead."""
    for item in staged(records):
        if item.ref == ref:
            return item
    event = next((r.event for r in records if r.ref == ref), None)
    if event is None:
        raise NotStagedError(f"no event {ref} in the log")
    if not isinstance(event, Proposal):
        raise NotStagedError(f"{ref} is not a proposal (it is a {event.type!r} event)")
    kept = [r.ref for r in records if isinstance(r.event, Assertion) and ref in r.event.caused_by]
    if kept:
        raise NotStagedError(f"{ref} was already kept as {kept[0]}")
    by = retired_by(records, ref)
    raise NotStagedError(f"{ref} was already dropped by {by[0] if by else 'a supersede'}")


@contextlib.contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    """Hold ``staging.lock`` exclusively: one decision per principal at a time."""
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
