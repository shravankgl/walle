"""Digests: each principal's periodic summary, at their own cadence (D31, D63, D79, issue #104).

D31 makes the digest a first-class output: the caregiver's weekly check-in and a household's
daily note are the same machinery with different schedules. This module is that machinery.
It decides *when* a digest runs and *where* it goes, and records that it ran. *What* it says
comes from a composer the caller hands in, the way ``snapshots`` takes a body: no model is
reachable from here.

The rules
---------
1. **A schedule is configuration.** ``Schedule(recipient, every, anchor)`` is built by whoever
   configures the install, outside the log. Slot ``k`` starts at ``anchor + k * every``. Each
   principal has their own schedule, and ``run`` is the same function for all of them (D31).
   Slots are instants: the anchor is held in UTC and ``every`` is elapsed time, so the slot
   containing a moment does not depend on which zone names it, and daylight saving neither
   merges two slots nor starts one after the moment it contains. A weekly 09:00 anchored in
   a zone with daylight saving therefore lands at 08:00 or 10:00 local for half the year.
2. **Configuration decides where; the run decides whether** (D63). The composer is handed a
   ``Window`` that carries no recipient and must return text, nothing else. The ``digest``
   event has no recipient field. The sender is called with ``schedule.recipient`` and nothing
   the composer said. So no run, and nothing in the log, can pick or change a recipient.
3. **Silence is a result** (D63). A composer that returns nothing, or only whitespace, sends
   nothing. The run is still recorded, as a ``digest`` with no body, so the slot is spent and
   later ticks in the same slot do not try again.
4. **Recurring runs do not catch up** (D63, as amended by D79). Only the slot containing
   ``now`` can be due, and only if no digest has run for it or for any later slot. Missed
   slots never fire on their own: a laptop that slept for ten daily slots runs once on waking
   (D79's run-once), never ten times (D63), and a clock that steps back cannot make a past
   slot due. Nothing is lost by this, because a run's window starts where the last run
   ended, so the one run covers every missed slot.
5. **A run that happened stays happened.** Every ``digest`` event counts as a run, including
   a superseded one. A sent message cannot be unsent, so a tombstone must not make its slot
   fire a second time.
6. **Claim before send** (D79). The ``digest`` event is appended, under ``digest.lock``,
   before the sender is called. A run that waited for the lock reads the log only after it
   has the lock, sees the slot spent and writes nothing. If delivery then fails, the slot is
   still spent and the digest is not retried: ``DeliveryError`` says so, with the ref, rather
   than letting the caller believe nothing happened. Missing one digest is the honest
   failure; sending one twice is the one D79 exists to prevent.
7. **Built from its own folder only** (D32 rule 3, D33, D113). A run takes a bound
   ``profiles.Context`` and touches its folder only through ``Profile(context)``: ``log/`` is
   resolved, ``digest.lock`` is opened with ``Profile.open`` (each component ``O_NOFOLLOW``)
   and the records come from ``Profile(context).records()``. A linked ``principals/``, a
   profile folder that is a link, or a ``digest.lock`` or stream that resolves into another
   principal's folder is refused with ``CrossPrincipalError`` before any file is created or
   read there. A household's digest is the household profile's, never a person's.
8. **Off by default.** ``run`` writes and sends nothing unless the owner has set
   ``WALLE_DIGESTS=on`` in the environment, outside the log (the owner's dormant condition on
   #104, D117). It is checked before anything is read. Reading is not acting: ``due`` works
   either way. Nothing calls ``run``, there is no real sender, and the CLI has no command.

``due`` is a pure function of the records, the schedule and an explicit instant: no clock, no
file, no model (hard constraint 3).

Two runners, step by step (the walkthrough this file is required to carry)
--------------------------------------------------------------------------
Runs A and B for the household, both started at 09:00:01 on Monday, in the weekly slot that
starts 09:00. Last week's digest ran. Observer W appends notes meanwhile.

1. A checks the switch, the context and that ``log/`` resolves inside its own folder and
   exists, then takes ``digest.lock``
   (exclusive). B does the same and blocks here, having read nothing.
2. A reads the clock (after the lock, so a run that waited is never stamped before the one it
   waited for), brings the index up to date (``index.lock``, then each stream file shared for a
   moment) and folds: this slot has not run. Due. The window is everything after last week's
   run up to now.
3. W appends a note to ``d-mbp`` under that stream's lock. W never takes ``digest.lock``. The
   note's ``ts`` is after A's ``until``, so it is in next week's window, not lost.
4. A calls the composer, under the lock. It returns text. A appends the ``digest`` event to its
   stream (``LOCK_EX`` until ``fsync``ed) and releases ``digest.lock``.
5. B wakes **and only now** reads the log. It sees A's digest for this slot: not due. B writes
   nothing, sends nothing, and returns a ``Run`` saying the slot already ran, with A's ref.
6. A calls the sender with the configured recipient. Outside the lock: a slow transport must
   not hold up anything, and B is already answered.

Had B folded before waiting for the lock, it would wake with a stale "due" and send a second
digest for the same slot. That is D79's race: the guard must be claimed before the work.

The composer runs under the lock. Only digest runs for this principal take ``digest.lock``,
and any of them waiting behind a slow composer would find the slot spent anyway, so holding
it across the composer costs nothing and keeps the check and the claim one step. A composer
that itself calls ``run`` for the same principal would deadlock; nothing does.

Lock order is ``digest.lock``, then ``index.lock``, then stream files (shared), released, then
one stream file (exclusive). Nothing else takes ``digest.lock``, and nothing takes it while
holding another lock, so no cycle exists with ``consolidate.lock``, ``staging.lock`` or the
indexer.

What this does not cover: two devices that do not share a filesystem each take their own
lock, and both may run the same slot before their logs meet. The log then says the slot ran
twice, and the recipient got two digests. Sync is deferred (D57).
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from walle.events import ACTOR_KERNEL, Digest, make_ref, parse_ref
from walle.paths import LOG_DIRNAME, principal_dir
from walle.profiles import Context, EmptyContextError, Profile
from walle.reader import Record

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's digests need POSIX file locking (fcntl); Windows is not supported in Phase 0. "
        "Run it under WSL."
    )

import fcntl

DIGESTS_SWITCH = "WALLE_DIGESTS"
"""The environment variable the owner sets to turn digests on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for the other dormant switches: a stray ``1`` is not consent."""

DIGEST_LOCK_FILENAME = "digest.lock"

RECIPIENT_MAX = 256
"""Long enough for any address or channel id; short enough that a body pasted into the
recipient field by mistake is refused rather than used as an address."""


class DigestsOffError(RuntimeError):
    """The owner has not turned digests on. Raised before anything is read or written."""


class DeliveryError(Exception):
    """The digest was recorded, so its slot is spent, but the sender failed. It is not
    retried. ``ref`` is the recorded digest, ``recipient`` where it should have gone."""

    def __init__(self, ref: str, recipient: str, slot: str) -> None:
        super().__init__(
            f"digest {ref} for slot {slot} was recorded but not delivered to {recipient!r}; "
            "the slot is spent and it will not be retried"
        )
        self.ref = ref
        self.recipient = recipient
        self.slot = slot


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned digests on: ``WALLE_DIGESTS`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(DIGESTS_SWITCH) == SWITCH_ON


# --- configuration ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    """One principal's digest configuration: where it goes and how often (rules 1 and 2)."""

    recipient: str
    """An address the sender understands. Opaque here; one line, never empty."""
    every: timedelta
    anchor: datetime
    """The start of slot 0. Must carry a time zone."""

    def __post_init__(self) -> None:
        if not isinstance(self.recipient, str):
            raise TypeError(f"a recipient is text, got {type(self.recipient).__name__}")
        if not self.recipient.strip():
            raise ValueError("a schedule needs a recipient")
        if len(self.recipient) > RECIPIENT_MAX:
            raise ValueError(f"a recipient is at most {RECIPIENT_MAX} characters")
        if any(ch.isspace() and ch != " " for ch in self.recipient) or not (
            self.recipient.isprintable()
        ):
            raise ValueError(f"a recipient is one printable line, got {self.recipient!r}")
        if not isinstance(self.every, timedelta):
            raise TypeError(f"every is a timedelta, got {type(self.every).__name__}")
        if self.every <= timedelta(0):
            raise ValueError(f"every must be positive, got {self.every}")
        _require_aware(self.anchor, "anchor")
        # Held in UTC (rule 1): arithmetic on a zone with daylight saving is wall-clock
        # arithmetic in Python, and a sub-minute offset has no spelling in the log.
        object.__setattr__(self, "anchor", self.anchor.astimezone(UTC))

    def slot(self, at: datetime) -> datetime | None:
        """The start of the slot containing ``at``, in UTC, or ``None`` before slot 0."""
        _require_aware(at, "at")
        at = at.astimezone(UTC)
        if at < self.anchor:
            return None
        return self.anchor + ((at - self.anchor) // self.every) * self.every


# --- the pure part ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Due:
    """Whether a run at ``until`` has a slot to run for, and why."""

    due: bool
    reason: str
    slot: str | None = None
    since: str | None = None
    """Where this run's window starts: the last run's ``ts``, or the anchor if none ran."""
    until: str | None = None
    missed: int = 0
    """Slots between the last run and this one that never ran, and never will (rule 4)."""


def due(records: Iterable[Record], schedule: Schedule, *, at: datetime) -> Due:
    """Whether the slot containing ``at`` is due (rules 4 and 5). Pure: the same records, in
    any order and with duplicates, and the same ``at`` give the same answer."""
    _require_aware(at, "at")
    at = at.astimezone(UTC)
    until = _spell(at)
    current = schedule.slot(at)
    if current is None:
        return Due(False, f"before the first slot, which starts at {_spell(schedule.anchor)}")

    runs = sorted(
        {
            (_at(r.event.slot), _at(r.event.ts), parse_ref(r.ref), r.ref)
            for r in records
            if isinstance(r.event, Digest)
        }
    )
    if runs:
        last_slot, last_ts, _, last_ref = runs[-1]
        if last_slot == current:
            return Due(
                False,
                f"slot {_spell(current)} already ran at {_spell(last_ts)} ({last_ref})",
                slot=_spell(current),
            )
        if last_slot > current:
            return Due(
                False,
                f"a later slot, {_spell(last_slot)}, already ran ({last_ref}); a past slot "
                f"never runs",
                slot=_spell(current),
            )
        since = last_ts
        missed = max(0, (current - last_slot) // schedule.every - 1)
    else:
        since = schedule.anchor
        missed = (current - schedule.anchor) // schedule.every

    reason = f"slot {_spell(current)} has not run"
    if missed:
        after = "the last run" if runs else "the first slot"
        reason += f"; {missed} earlier slots since {after} are not run on their own"
    return Due(True, reason, slot=_spell(current), since=_spell(since), until=until, missed=missed)


@dataclass(frozen=True)
class Window:
    """What a composer is shown: one principal's records after ``since``, up to ``until``.

    It carries no recipient and no schedule on purpose (rule 2): a composer can decide
    whether there is anything to say, never where it goes.
    """

    principal: str
    slot: str
    since: str
    until: str
    records: tuple[Record, ...]


type Composer = Callable[[Window], str]
"""Turns a window into the digest's text. Empty or whitespace means nothing to say."""


class Sender(Protocol):
    """Delivers one digest. Raises on failure. walle ships none: the caller supplies it."""

    def __call__(self, recipient: str, body: str) -> None: ...


def window_records(
    records: Iterable[Record], since: datetime, until: datetime
) -> tuple[Record, ...]:
    """The records whose ``ts`` is after ``since`` and at or before ``until``, in ref order,
    each once."""
    picked = {r.ref: r for r in records if since < _at(r.event.ts) <= until}
    return tuple(sorted(picked.values(), key=lambda r: parse_ref(r.ref)))


# --- running ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """What one call to ``run`` did."""

    principal: str
    due: bool
    reason: str
    slot: str | None = None
    missed: int = 0
    ref: str | None = None
    """The ``digest`` event this run recorded, or ``None`` when it was not due."""
    sent: bool = False
    recipient: str | None = None
    """Where it was sent, or ``None`` when nothing was."""

    def summary(self) -> str:
        if not self.due:
            return f"No digest for {self.principal}: {self.reason}."
        if not self.sent:
            return (
                f"Digest for {self.principal}, slot {self.slot}: nothing to say, so nothing "
                f"was sent (recorded as {self.ref})."
            )
        return (
            f"Digest for {self.principal}, slot {self.slot}: sent to {self.recipient} "
            f"(recorded as {self.ref})."
        )


def digest_lock_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/digest.lock`` — held for one run's check and claim."""
    return principal_dir(Path(root), principal) / DIGEST_LOCK_FILENAME


def run(
    context: Context,
    schedule: Schedule,
    compose: Composer,
    send: Sender,
    *,
    now: datetime | None = None,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Run:
    """Run the digest for ``context``'s principal, if its slot is due (rules 1 to 8).

    ``now`` defaults to the clock, read after the lock is taken. ``model`` names what
    composed the body, the digest's own provenance.

    Raises, having read and written nothing: ``DigestsOffError`` while off,
    ``EmptyContextError`` for a missing context or one with no device bound,
    ``FileNotFoundError`` for a principal with no log, ``ValueError`` for a naive ``now``,
    ``CrossPrincipalError`` when ``principals/``, the profile folder, ``log/`` or
    ``digest.lock`` leads out of the principal's own folder.
    Raises having written nothing: ``TypeError`` when the composer returns anything but
    text, and whatever the composer itself raises. Raises after recording the run:
    ``DeliveryError`` when the sender fails.
    """
    if not enabled(environ):
        raise DigestsOffError(
            f"digests are off; the owner turns them on with {DIGESTS_SWITCH}={SWITCH_ON}"
        )
    if not isinstance(context, Context):
        raise EmptyContextError(
            f"a digest run needs a bound Context, got {type(context).__name__} (D113)"
        )
    if not isinstance(schedule, Schedule):
        raise TypeError(f"a digest run needs a Schedule, got {type(schedule).__name__}")
    writer = context.writer()  # EmptyContextError when no device is bound
    if now is not None:
        _require_aware(now, "now")
    principal = context.principal
    profile = Profile(context)
    if not os.path.lexists(profile.folder):
        # Checked before anything is resolved or created: a misspelt principal must not get
        # a folder, and is told so plainly.
        raise FileNotFoundError(
            f"{profile.folder}: no profile for principal {principal!r}, nothing to digest"
        )
    log = profile.path(LOG_DIRNAME)  # CrossPrincipalError for any link out of the folder
    if not log.is_dir():
        # Checked before the lock file is created.
        raise FileNotFoundError(f"{log}: no log for principal {principal!r}, nothing to digest")

    with _exclusive(profile, DIGEST_LOCK_FILENAME):
        at = datetime.now(UTC) if now is None else now
        records = profile.records()
        verdict = due(records, schedule, at=at)
        if not verdict.due:
            return Run(principal, False, verdict.reason, slot=verdict.slot)
        assert verdict.slot is not None and verdict.since is not None and verdict.until is not None
        window = Window(
            principal=principal,
            slot=verdict.slot,
            since=verdict.since,
            until=verdict.until,
            records=window_records(records, _at(verdict.since), at),
        )
        body = compose(window)
        if not isinstance(body, str):
            raise TypeError(
                f"a composer returns the digest's text, got {type(body).__name__}; "
                "where it goes is the schedule's to say (D63)"
            )
        said = body if body.strip() else None
        written = writer.append(
            Digest(
                ts=_spell(at.astimezone(UTC)),
                actor=ACTOR_KERNEL,
                slot=verdict.slot,
                body=said,
                model=model if said is not None else None,
            )
        )
    assert written.seq is not None  # the writer always assigns one
    ref = make_ref(writer.stream, written.seq)
    common = {"slot": verdict.slot, "missed": verdict.missed, "ref": ref}
    if said is None:
        return Run(principal, True, verdict.reason, **common)
    try:
        send(schedule.recipient, said)
    except Exception as exc:
        raise DeliveryError(ref, schedule.recipient, verdict.slot) from exc
    return Run(principal, True, verdict.reason, sent=True, recipient=schedule.recipient, **common)


# --- helpers ------------------------------------------------------------------------------------


def _require_aware(moment: object, name: str) -> None:
    if not isinstance(moment, datetime):
        raise TypeError(f"{name} is a datetime, got {type(moment).__name__}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must carry a time zone: a naive time is not an instant")


def _at(ts: str) -> datetime:
    """A logged instant, in UTC, so comparisons and differences are elapsed time."""
    return datetime.fromisoformat(ts).astimezone(UTC)


def _spell(moment: datetime) -> str:
    """A moment in the log's own spelling, so a reason quotes the ``ts`` it read."""
    return moment.isoformat(timespec="microseconds")


@contextlib.contextmanager
def _exclusive(profile: Profile, name: str) -> Iterator[None]:
    """Hold ``digest.lock`` exclusively: one run's check and claim at a time. Opened through
    the profile, so a lock file that is a link out of the folder is refused, not followed."""
    with profile.open(name, "ab") as f:  # "a": never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.
