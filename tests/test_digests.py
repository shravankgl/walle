"""Issue #104's verify criteria, and the cases around them (D31, D63, D79).

  silence is a result    a run with nothing to say sends nothing; it records that it ran, so
                         the same slot does not try again
  config picks where     the recipient comes from the schedule the caller was configured with;
                         nothing the run composes, and nothing in the log, can name another
  no catch-up            missed slots never fire on their own: ten missed days are one run, not
                         ten, and a slot older than one that already ran never runs
  own cadence            each principal's digest runs on its own schedule, by the same function,
                         from its own folder only
  off by default         nothing runs unless the owner has set the switch, outside the log

Everything here is synthetic: a made-up household (the Hollies), made-up people and a fake
sender that only remembers what it was handed. No real address, no real transport.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from pydantic import ValidationError

from test_constraints import forbidden_in_closure
from walle import digests, profiles
from walle.events import (
    ACTOR_KERNEL,
    ACTOR_USER,
    Digest,
    Observation,
    Supersede,
    parse_event,
)
from walle.index import rebuild, update
from walle.paths import log_dir, principal_dir
from walle.reader import Record, read_file, read_stream
from walle.writer import StreamWriter

P = "the-hollies"
D = "mbp"
ON = {digests.DIGESTS_SWITCH: digests.SWITCH_ON}
T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # a Monday, 09:00
DAY = timedelta(days=1)
WEEK = timedelta(days=7)
RECIPIENT = "household-inbox:the-hollies"
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "digests.py"
GOLDEN = Path(__file__).parent / "fixtures" / "v1" / "digests"


# --- builders -----------------------------------------------------------------------------


class FakeSender:
    """Remembers every ``(recipient, body)`` it was handed. Sends nothing anywhere."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, recipient: str, body: str) -> None:
        if self.fail:
            raise ConnectionError("synthetic: the fake transport is down")
        self.sent.append((recipient, body))


def _says(text: str) -> digests.Composer:
    return lambda window: text


_SYNTHETIC = _says("synthetic digest")


def _silent(window: digests.Window) -> str:
    return ""


def _counts(window: digests.Window) -> str:
    """A composer that has something to say only when something happened in the window."""
    notes = [r.event for r in window.records if isinstance(r.event, Observation)]
    return f"{len(notes)} new notes" if notes else ""


def _ts(at: datetime) -> str:
    return at.isoformat(timespec="microseconds")


def _note(root: Path, at: datetime, text: str = "synthetic note", principal: str = P) -> str:
    event = Observation(
        ts=_ts(at),
        actor=ACTOR_USER,
        source="synthetic",
        source_version="0",
        observed_at=_ts(at),
        body=text,
    )
    written = StreamWriter(root, principal, D).append(event)
    return f"d-{D}:{written.seq}"


def _profile(root: Path, principal: str = P) -> None:
    log_dir(root, principal).mkdir(parents=True)


def _context(root: Path, principal: str = P) -> profiles.Context:
    return profiles.bind(root, principal, device=D)


def _schedule(every: timedelta = WEEK, recipient: str = RECIPIENT) -> digests.Schedule:
    return digests.Schedule(recipient=recipient, every=every, anchor=T0)


def _run(
    root: Path,
    at: datetime,
    compose: digests.Composer = _SYNTHETIC,
    send: digests.Sender | None = None,
    schedule: digests.Schedule | None = None,
    principal: str = P,
) -> digests.Run:
    return digests.run(
        _context(root, principal),
        schedule or _schedule(),
        compose,
        send if send is not None else FakeSender(),
        now=at,
        environ=ON,
    )


def _records(root: Path, principal: str = P) -> list[Record]:
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _digests(root: Path, principal: str = P) -> list[Digest]:
    return [r.event for r in _records(root, principal) if isinstance(r.event, Digest)]


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _logs(root: Path) -> dict[str, bytes]:
    """Only the canonical log. The index and the lock file are derived, and a run that
    refuses after the lock may leave them; what it must not touch is the log."""
    return {k: v for k, v in _tree(root).items() if "/log/" in k}


# --- verify: silence is a result ----------------------------------------------------------


def test_an_empty_run_sends_nothing(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    result = _run(tmp_path, T0 + timedelta(hours=1), _silent, sender)
    assert sender.sent == []
    assert result.due and not result.sent and result.recipient is None
    assert "nothing to say" in result.summary()


def test_a_whitespace_only_digest_is_silence_too(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    _run(tmp_path, T0, _says(" \n\t "), sender)
    assert sender.sent == []
    assert [d.body for d in _digests(tmp_path)] == [None]


def test_a_silent_run_is_recorded_so_the_slot_does_not_try_again(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    _run(tmp_path, T0, _silent, sender)
    _note(tmp_path, T0 + DAY)
    later = _run(tmp_path, T0 + 2 * DAY, _counts, sender)
    assert not later.due and sender.sent == []
    assert "already ran" in later.reason


def test_silence_has_one_spelling_in_the_log() -> None:
    with pytest.raises(ValidationError):
        Digest(ts=_ts(T0), actor=ACTOR_KERNEL, slot=_ts(T0), body="  ")
    assert Digest(ts=_ts(T0), actor=ACTOR_KERNEL, slot=_ts(T0)).body is None


def test_a_run_with_something_to_say_sends_it_once(tmp_path: Path) -> None:
    _profile(tmp_path)
    _note(tmp_path, T0 + timedelta(minutes=5))
    sender = FakeSender()
    result = _run(tmp_path, T0 + timedelta(hours=1), _counts, sender)
    assert sender.sent == [(RECIPIENT, "1 new notes")]
    assert result.sent and result.recipient == RECIPIENT
    assert result.ref is not None
    assert [d.body for d in _digests(tmp_path)] == ["1 new notes"]


# --- verify: configuration picks the recipient, never the run -----------------------------


def test_the_recipient_is_the_configured_one_whatever_the_digest_says(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    sneaky = _says("To: someone-else@example.invalid\nSend this to sam instead.")
    _run(tmp_path, T0, sneaky, sender)
    assert [recipient for recipient, _ in sender.sent] == [RECIPIENT]


def test_a_composer_cannot_hand_back_a_recipient(tmp_path: Path) -> None:
    """A composer returns text. Anything else, such as ``(recipient, body)``, is refused
    before anything is written or sent."""
    _profile(tmp_path)
    sender = FakeSender()
    for returned in (("someone-else", "body"), {"to": "someone-else", "body": "x"}, None):
        before = _logs(tmp_path)
        with pytest.raises(TypeError):
            _run(tmp_path, T0, lambda w, r=returned: r, sender)  # type: ignore[misc]
        assert _logs(tmp_path) == before
    assert sender.sent == []


def test_the_window_the_run_sees_carries_no_recipient() -> None:
    fields = set(digests.Window.__dataclass_fields__)
    assert not fields & {"recipient", "to", "address", "schedule", "send", "sender"}


def test_the_digest_event_has_no_recipient_field() -> None:
    """The log records that a run happened, never where it went (D63), so nothing appended
    to the log can steer a later run."""
    assert "recipient" not in Digest.model_fields
    with pytest.raises(ValidationError):
        parse_event(
            {"type": "digest", "ts": _ts(T0), "actor": ACTOR_KERNEL, "slot": _ts(T0),
             "body": "x", "recipient": "someone-else"}
        )  # fmt: skip


def test_the_same_log_under_another_configuration_goes_to_the_other_recipient(
    tmp_path: Path,
) -> None:
    for name, recipient in (("one", "inbox:ada"), ("two", "inbox:sam")):
        root = tmp_path / name
        _profile(root)
        sender = FakeSender()
        _run(root, T0, schedule=_schedule(recipient=recipient), send=sender)
        assert [r for r, _ in sender.sent] == [recipient]


def test_only_the_schedule_carries_a_recipient_into_a_run() -> None:
    params = inspect.signature(digests.run).parameters
    assert "recipient" not in params
    assert set(params) == {"context", "schedule", "compose", "send", "now", "model", "environ"}
    assert "recipient" in digests.Schedule.__dataclass_fields__


@pytest.mark.parametrize("recipient", ["", "   ", "a\nb", "inbox:a\rcc:b", "x" * 257, 7])
def test_a_schedule_with_an_unusable_recipient_cannot_be_made(recipient: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        digests.Schedule(recipient=recipient, every=WEEK, anchor=T0)


# --- verify: recurring runs don't catch up ------------------------------------------------


def test_ten_missed_daily_slots_are_one_run_not_ten(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    daily = _schedule(every=DAY)
    _run(tmp_path, T0, schedule=daily, send=sender)
    woke = T0 + 10 * DAY + timedelta(hours=3)
    first = _run(tmp_path, woke, schedule=daily, send=sender)
    again = _run(tmp_path, woke + timedelta(minutes=1), schedule=daily, send=sender)
    assert first.due and first.missed == 9
    assert not again.due
    assert len(sender.sent) == 2  # day 0, and once on waking: never the nine in between
    assert [d.slot for d in _digests(tmp_path)] == [_ts(T0), _ts(T0 + 10 * DAY)]
    assert "9 earlier slots" in first.reason


def test_the_one_run_after_a_gap_covers_everything_since_the_last(tmp_path: Path) -> None:
    """No catch-up does not mean anything is lost: the window starts at the last run."""
    _profile(tmp_path)
    daily = _schedule(every=DAY)
    _run(tmp_path, T0, schedule=daily)
    for day in (2, 5, 9):
        _note(tmp_path, T0 + day * DAY)
    sender = FakeSender()
    _run(tmp_path, T0 + 10 * DAY, _counts, sender, schedule=daily)
    assert sender.sent == [(RECIPIENT, "3 new notes")]


def test_a_slot_older_than_one_that_ran_never_runs(tmp_path: Path) -> None:
    """A clock that steps back must not make a past slot due again."""
    _profile(tmp_path)
    daily = _schedule(every=DAY)
    _run(tmp_path, T0 + 10 * DAY, schedule=daily)
    sender = FakeSender()
    back = _run(tmp_path, T0 + 4 * DAY, schedule=daily, send=sender)
    assert not back.due and sender.sent == []
    assert "later slot" in back.reason


def test_nothing_runs_before_the_first_slot(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    early = _run(tmp_path, T0 - timedelta(seconds=1), send=sender)
    assert not early.due and sender.sent == [] and _digests(tmp_path) == []


def test_each_slot_runs_once_when_nothing_is_missed(tmp_path: Path) -> None:
    _profile(tmp_path)
    sender = FakeSender()
    for week in range(4):
        for hour in (0, 5, 30):
            _run(tmp_path, T0 + week * WEEK + timedelta(hours=hour), send=sender)
    assert len(sender.sent) == 4
    assert [d.slot for d in _digests(tmp_path)] == [_ts(T0 + w * WEEK) for w in range(4)]


def test_a_superseded_digest_still_counts_as_having_run(tmp_path: Path) -> None:
    """A sent message cannot be unsent, so a tombstone must not make its slot fire again."""
    _profile(tmp_path)
    first = _run(tmp_path, T0)
    assert first.ref is not None
    StreamWriter(tmp_path, P, D).append(
        Supersede(ts=_ts(T0 + timedelta(hours=1)), actor=ACTOR_USER, target=first.ref)
    )
    sender = FakeSender()
    assert not _run(tmp_path, T0 + timedelta(hours=2), send=sender).due
    assert sender.sent == []


def test_due_is_a_pure_function_of_the_records_and_the_instant(tmp_path: Path) -> None:
    _profile(tmp_path)
    _run(tmp_path, T0)
    _run(tmp_path, T0 + 3 * WEEK)
    records = _records(tmp_path)
    at = T0 + 3 * WEEK + 2 * DAY
    one = digests.due(records, _schedule(), at=at)
    assert one == digests.due(list(reversed(records)) + records, _schedule(), at=at)
    assert one == digests.due(update(tmp_path, P).records, _schedule(), at=at)
    with pytest.raises(ValueError):
        digests.due(records, _schedule(), at=at.replace(tzinfo=None))


def test_due_reads_no_clock_and_opens_no_file() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    pure = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "due"]
    assert len(pure) == 1
    names = {n.id for n in ast.walk(pure[0]) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(pure[0]) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "now_instant", "time", "os"}
    assert not attrs & {"read_text", "now", "environ", "today"}


# --- each principal, its own cadence, its own folder --------------------------------------


def test_each_principal_runs_at_its_own_cadence_from_the_same_function(tmp_path: Path) -> None:
    """The caregiver's weekly check-in and a household's daily note are the same machinery
    (D31): one ``run``, two schedules."""
    for who in ("the-hollies", "nan"):
        _profile(tmp_path, who)
    daily, weekly = _schedule(every=DAY), _schedule(every=WEEK, recipient="inbox:carer")
    sent: dict[str, FakeSender] = {"the-hollies": FakeSender(), "nan": FakeSender()}
    for day in range(14):
        at = T0 + day * DAY + timedelta(hours=1)
        _run(tmp_path, at, schedule=daily, send=sent["the-hollies"], principal="the-hollies")
        _run(tmp_path, at, schedule=weekly, send=sent["nan"], principal="nan")
    assert len(sent["the-hollies"].sent) == 14
    assert sent["nan"].sent == [("inbox:carer", "synthetic digest")] * 2


def test_a_digest_is_built_from_its_own_folder_only(tmp_path: Path) -> None:
    """D32 rule 3: the household's digest never reads a person's folder."""
    _profile(tmp_path)
    _profile(tmp_path, "sam")
    canary = "CANARY-not-real-sam-private"
    _note(tmp_path, T0 + timedelta(minutes=1), canary, principal="sam")
    _note(tmp_path, T0 + timedelta(minutes=2), "the boiler is booked")
    seen: list[digests.Window] = []

    def compose(window: digests.Window) -> str:
        seen.append(window)
        return "synthetic digest"

    _run(tmp_path, T0 + timedelta(hours=1), compose)
    (window,) = seen
    assert window.principal == P
    contents = [getattr(r.event, "body", "") for r in window.records]
    assert contents == ["the boiler is booked"]
    assert canary not in repr(window)


def test_the_window_is_since_the_last_run_up_to_now(tmp_path: Path) -> None:
    _profile(tmp_path)
    seen: list[digests.Window] = []

    def compose(window: digests.Window) -> str:
        seen.append(window)
        return ""

    _note(tmp_path, T0 - DAY, "before the first slot")
    _note(tmp_path, T0 + DAY, "week one")
    _run(tmp_path, T0 + 2 * DAY, compose)
    _note(tmp_path, T0 + 3 * DAY, "after the first run")
    _run(tmp_path, T0 + WEEK + DAY, compose)
    first, second = seen
    assert first.since == _ts(T0) and first.until == _ts(T0 + 2 * DAY)
    assert [r.event.body for r in first.records] == ["week one"]  # type: ignore[union-attr]
    assert second.since == _ts(T0 + 2 * DAY)
    assert [r.event.body for r in second.records] == [  # type: ignore[union-attr]
        "after the first run"
    ]


def test_a_run_needs_a_bound_context_with_a_device(tmp_path: Path) -> None:
    _profile(tmp_path)
    for context in (None, P, profiles.bind(tmp_path, P)):
        with pytest.raises(profiles.EmptyContextError):
            digests.run(context, _schedule(), _says("x"), FakeSender(), now=T0, environ=ON)  # type: ignore[arg-type]
    assert _records(tmp_path) == []


def test_a_principal_with_no_log_gets_no_folder(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _run(tmp_path, T0)
    assert not principal_dir(tmp_path, P).exists()


# --- a slot is an instant, whatever zone names it (review of #113, medium 1) ---------------


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # pragma: no cover - a system with no tz database
        pytest.skip(f"no time zone data for {name} on this system")


def test_the_slot_is_the_same_whatever_zone_names_the_instant() -> None:
    """Across autumn's fall-back, one instant named in UTC and in New York is one slot, and
    the slot never starts after the instant it contains."""
    ny = _zone("America/New_York")
    weekly = digests.Schedule(
        recipient=RECIPIENT, every=WEEK, anchor=datetime(2026, 10, 26, 9, 0, tzinfo=ny)
    )
    at_utc = datetime(2026, 11, 2, 13, 30, tzinfo=UTC)  # 08:30 EST
    at_ny = at_utc.astimezone(ny)
    assert at_utc == at_ny
    in_utc, in_ny = weekly.slot(at_utc), weekly.slot(at_ny)
    assert in_utc is not None and in_ny is not None
    assert in_utc == in_ny <= at_utc
    assert digests.due([], weekly, at=at_utc) == digests.due([], weekly, at=at_ny)


def test_hourly_slots_across_spring_forward_each_run(tmp_path: Path) -> None:
    """Spring-forward must not make two slots one instant, so the second is not skipped as
    already run."""
    _profile(tmp_path)
    ny = _zone("America/New_York")
    hourly = digests.Schedule(
        recipient=RECIPIENT,
        every=timedelta(hours=1),
        anchor=datetime(2026, 3, 8, 0, 0, tzinfo=ny),  # 05:00 UTC; clocks jump at 07:00 UTC
    )
    sender = FakeSender()
    for hour in (7, 8):
        at = datetime(2026, 3, 8, hour, 1, tzinfo=UTC)
        assert _run(tmp_path, at, send=sender, schedule=hourly).sent
    assert len(sender.sent) == 2
    assert len({d.slot for d in _digests(tmp_path)}) == 2


def test_weekly_missed_slots_across_spring_forward_are_counted_whole() -> None:
    ny = _zone("America/New_York")
    weekly = digests.Schedule(
        recipient=RECIPIENT, every=WEEK, anchor=datetime(2026, 3, 1, 9, 0, tzinfo=ny)
    )
    ran = Record(
        ref=f"d-{D}:1",
        seq=1,
        event=Digest(
            ts="2026-03-01T14:00:01.000000+00:00",
            actor=ACTOR_KERNEL,
            slot="2026-03-01T14:00:00.000000+00:00",
            body=None,
        ),
        line=1,
    )
    verdict = digests.due([ran], weekly, at=datetime(2026, 3, 15, 15, 0, tzinfo=ny))
    assert verdict.due and verdict.missed == 1


def test_a_sub_minute_offset_still_records_a_valid_slot(tmp_path: Path) -> None:
    _profile(tmp_path)
    odd = timezone(timedelta(seconds=30))
    schedule = digests.Schedule(recipient=RECIPIENT, every=WEEK, anchor=T0.astimezone(odd))
    result = _run(tmp_path, (T0 + DAY).astimezone(odd), schedule=schedule)
    assert result.sent
    (digest,) = _digests(tmp_path)
    assert digest.slot == _ts(T0)


# --- the run touches only its own folder (review of #113, medium 2) -------------------------


def test_a_profile_folder_linked_to_another_principal_is_refused_untouched(
    tmp_path: Path,
) -> None:
    _profile(tmp_path, "sam")
    sam = principal_dir(tmp_path, "sam")
    before = _tree(tmp_path)
    principal_dir(tmp_path, P).symlink_to(sam, target_is_directory=True)
    with pytest.raises(profiles.CrossPrincipalError):
        _run(tmp_path, T0)
    assert not (sam / digests.DIGEST_LOCK_FILENAME).exists()
    assert _tree(tmp_path) == before


def test_a_digest_lock_linked_into_another_principal_is_refused(tmp_path: Path) -> None:
    _profile(tmp_path)
    _profile(tmp_path, "sam")
    target = principal_dir(tmp_path, "sam") / digests.DIGEST_LOCK_FILENAME
    (principal_dir(tmp_path, P) / digests.DIGEST_LOCK_FILENAME).symlink_to(target)
    sender = FakeSender()
    with pytest.raises(profiles.CrossPrincipalError):
        _run(tmp_path, T0, send=sender)
    assert not target.exists()
    assert _digests(tmp_path) == [] and sender.sent == []


# --- ties between runs break by ref order (review of #113, low 1) ---------------------------


def test_runs_tied_on_slot_and_time_break_by_ref_order_not_spelling(tmp_path: Path) -> None:
    _profile(tmp_path)
    writer = StreamWriter(tmp_path, P, D)
    tied = Digest(ts=_ts(T0 + timedelta(seconds=1)), actor=ACTOR_KERNEL, slot=_ts(T0), body=None)
    _note(tmp_path, T0 - DAY)
    writer.append(tied)  # d-mbp:2
    for _ in range(7):
        _note(tmp_path, T0 - DAY)
    writer.append(tied)  # d-mbp:10
    verdict = digests.due(_records(tmp_path), _schedule(), at=T0 + DAY)
    assert not verdict.due and f"(d-{D}:10)" in verdict.reason


# --- errors that must not lie --------------------------------------------------------------


def test_a_composer_that_fails_writes_nothing_and_the_slot_stays_due(tmp_path: Path) -> None:
    _profile(tmp_path)

    def broken(window: digests.Window) -> str:
        raise RuntimeError("synthetic: the model is unavailable")

    with pytest.raises(RuntimeError):
        _run(tmp_path, T0, broken)
    assert _digests(tmp_path) == []
    assert _run(tmp_path, T0 + timedelta(minutes=5)).due


def test_a_failed_delivery_says_the_run_was_recorded_and_not_delivered(tmp_path: Path) -> None:
    """Claim before send (D79): the slot is spent, so it is never sent twice. The error says
    exactly that, rather than letting the caller believe nothing happened."""
    _profile(tmp_path)
    with pytest.raises(digests.DeliveryError) as caught:
        _run(tmp_path, T0, send=FakeSender(fail=True))
    assert caught.value.ref is not None and caught.value.recipient == RECIPIENT
    message = str(caught.value)
    assert (
        caught.value.ref in message and "not delivered" in message and "not be retried" in message
    )
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert [d.body for d in _digests(tmp_path)] == ["synthetic digest"]
    assert not _run(tmp_path, T0 + timedelta(minutes=5)).due


@pytest.mark.parametrize("every", [timedelta(0), timedelta(seconds=-1), 7])
def test_a_schedule_needs_a_positive_cadence(every: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        digests.Schedule(recipient=RECIPIENT, every=every, anchor=T0)


def test_a_schedule_needs_an_anchor_with_a_time_zone() -> None:
    with pytest.raises(ValueError):
        digests.Schedule(recipient=RECIPIENT, every=WEEK, anchor=T0.replace(tzinfo=None))


def test_a_naive_now_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    _profile(tmp_path)
    before = _tree(tmp_path)
    with pytest.raises(ValueError):
        _run(tmp_path, T0.replace(tzinfo=None))
    assert _tree(tmp_path) == before


def test_two_runs_for_one_slot_send_once(tmp_path: Path) -> None:
    """The walkthrough, for real: B starts while A is composing under the lock. B must block
    until A has recorded its digest, then read the log, find the slot spent and send nothing."""
    _profile(tmp_path)
    sender = FakeSender()
    at = T0 + timedelta(seconds=1)
    outcome: dict[str, digests.Run] = {}
    threads: list[threading.Thread] = []

    def b() -> None:
        outcome["b"] = _run(tmp_path, at, send=sender)

    def compose_while_b_starts(window: digests.Window) -> str:
        thread = threading.Thread(target=b)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive(), "B ran while A held digest.lock"
        threads.append(thread)
        return "synthetic digest"

    a = _run(tmp_path, at, compose_while_b_starts, sender)
    threads[0].join(timeout=10)
    assert a.sent and not outcome["b"].due
    assert outcome["b"].reason.endswith(f"({a.ref})")
    assert len(sender.sent) == 1 and len(_digests(tmp_path)) == 1


def test_a_first_run_after_missed_slots_does_not_claim_a_last_run(tmp_path: Path) -> None:
    _profile(tmp_path)
    first = _run(tmp_path, T0 + 3 * WEEK)
    assert first.missed == 3 and "since the first slot" in first.reason
    assert "last run" not in first.reason


# --- the dormant rule ---------------------------------------------------------------------


def test_digests_are_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(digests.DIGESTS_SWITCH, raising=False)
    assert not digests.enabled()
    _profile(tmp_path)
    before = _tree(tmp_path)
    sender = FakeSender()
    with pytest.raises(digests.DigestsOffError):
        digests.run(_context(tmp_path), _schedule(), _says("x"), sender, now=T0)
    assert sender.sent == [] and _tree(tmp_path) == before


def test_off_is_checked_before_anything_is_read(tmp_path: Path) -> None:
    """Off wins even over a principal that does not exist: nothing is looked at."""
    with pytest.raises(digests.DigestsOffError):
        digests.run(_context(tmp_path), _schedule(), _says("x"), FakeSender(), now=T0, environ={})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "on\n"])
def test_only_the_one_spelling_turns_digests_on(value: str) -> None:
    assert not digests.enabled({digests.DIGESTS_SWITCH: value})
    assert digests.enabled({digests.DIGESTS_SWITCH: "on"})


def test_other_switches_do_not_turn_digests_on() -> None:
    for other in ("WALLE_PROFILES", "WALLE_OUTCOMES", "WALLE_INBOX", "WALLE_AUTOMATIONS"):
        assert not digests.enabled({other: "on"})


def test_reading_whether_a_digest_is_due_works_while_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(digests.DIGESTS_SWITCH, raising=False)
    _profile(tmp_path)
    assert digests.due(_records(tmp_path), _schedule(), at=T0).due


def test_nothing_in_the_log_can_turn_digests_on() -> None:
    assert not any(f in Digest.model_fields for f in ("enabled", "on", "recipient", "every"))
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "environ" and isinstance(n.value, ast.Name)
    ]
    assert all(n.value.id == "os" for n in reads)  # type: ignore[union-attr]


def test_the_cli_gains_no_digest_command() -> None:
    from walle.cli import main

    assert not any("digest" in name for name in main.commands)


def test_digests_reach_no_model_and_no_network() -> None:
    assert forbidden_in_closure(PACKAGE, ("digests",)) == {}


# --- the golden log -------------------------------------------------------------------------


def _golden_root(tmp_path: Path) -> tuple[Path, str]:
    expected = json.loads((GOLDEN / "__expected__.json").read_text(encoding="utf-8"))
    root = tmp_path / "root"
    shutil.copytree(GOLDEN, root, ignore=shutil.ignore_patterns("__expected__.json"))
    for path in (root, *root.rglob("*")):  # a read-only checkout must not fail the rebuild
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    return root, expected["principal"]


def test_due_over_the_golden_log_is_the_one_it_shipped_with(tmp_path: Path) -> None:
    """The Hollies' weekly digest: ran 5 Oct, silent 12 Oct, missed 19 and 26 Oct, ran
    2 Nov, and the 5 Oct one was later retracted."""
    root, principal = _golden_root(tmp_path)
    from_log = read_stream(root, principal, "mbp").records
    from_index = rebuild(root, principal).records
    weekly = _schedule()

    spent = digests.due(from_log, weekly, at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    assert spent == digests.due(from_index, weekly, at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    assert not spent.due and "already ran" in spent.reason

    past = digests.due(from_log, weekly, at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    assert not past.due and "later slot" in past.reason

    nxt = digests.due(from_log, weekly, at=datetime(2026, 11, 9, 10, 0, tzinfo=UTC))
    assert nxt.due and nxt.missed == 0
    assert nxt.slot == "2026-11-09T09:00:00.000000+00:00"
    assert nxt.since == "2026-11-02T09:00:04.000000+00:00"
