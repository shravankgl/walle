"""Task 2's verify criteria, as tests (D69).

Each of these would have failed before `walle.events` existed:

  round-trip per type          every event type survives dump -> parse unchanged
  hash stable across key order constructing fields in a different order hashes the same
  unknown field rejected       extra="forbid" is load-bearing, not decoration
  v on every event             D96's version is present and defaulted
  bad ref raises               a ref that could escape a path never becomes one (D85)
  future v halts               an event from a newer build refuses to load (D111)
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from pydantic import ValidationError

from walle.events import (
    ACTOR_CURATOR,
    ACTOR_KERNEL,
    ACTOR_OBSERVER,
    ACTOR_USER,
    EVENT_TYPES,
    AnyEvent,
    Assertion,
    Contradiction,
    Event,
    FutureSchemaError,
    Level,
    Link,
    Mission,
    Node,
    Observation,
    Proposal,
    Snapshot,
    Step,
    StepStatus,
    StepVerb,
    Supersede,
    canonical_json,
    content_hash,
    make_ref,
    parse_event,
    parse_ref,
)

TS = "2026-10-10T14:37:02+00:00"


def _one_of_each() -> list[AnyEvent]:
    return [
        Observation(
            ts=TS,
            actor=ACTOR_OBSERVER,
            source="transcript",
            observed_at="2026-10-09T09:00:00+00:00",
            body="Explained the deployment setup again.",
            source_version="2.1.0",
        ),
        Assertion(
            ts=TS,
            actor=ACTOR_CURATOR,
            caused_by=["d-mbp:44881", "d-mbp:44903"],
            body="Deploys go through the staging cluster first.",
            summary="deploys via staging first",
            level=Level.INDUCTIVE,
            confidence=0.45,
            evidence_count=3,
            valid_from="2026-03-01",
        ),
        Supersede(
            ts=TS,
            actor=ACTOR_USER,
            target="d-mbp:44881",
            reason="moved to Pune",
            valid_to="2027-01-01",
        ),
        Proposal(
            ts=TS,
            actor=ACTOR_USER,
            body="Maya keeps the basil on the east windowsill.",
            summary="basil on the east windowsill",
            level=Level.EXPLICIT,
            valid_from="2026-05-01",
        ),
        Snapshot(
            ts=TS,
            actor=ACTOR_CURATOR,
            forgets=["d-mbp:10", "d-phone:11"],
            body="Through September: deployment, billing, the quota work.",
            model="claude-opus-5",
        ),
        Contradiction(
            ts=TS,
            actor=ACTOR_CURATOR,
            caused_by=["d-mbp:100", "d-mbp:220"],
            note="stated vegetarian; three dessert orders",
        ),
        Mission(
            ts=TS,
            actor=ACTOR_USER,
            title="Saturday chores",
            nodes=[
                Node(id="fetch-mail", title="Read the mail", egress=["gmail.googleapis.com"]),
                Node(id="digest", title="Write the digest", token_budget=5000),
            ],
            links=[Link(source="fetch-mail", target="digest")],
        ),
        Step(
            ts=TS,
            actor=ACTOR_KERNEL,
            mission="d-mbp:300",
            verb=StepVerb.STATUS,
            node="fetch-mail",
            status=StepStatus.COMPLETED,
            cost_usd=0.02,
            tokens=900,
        ),
    ]


# --- round-trip --------------------------------------------------------------------------


@pytest.mark.parametrize("event", _one_of_each(), ids=lambda e: e.type)
def test_round_trips_through_json(event: Event) -> None:
    """dump -> JSON text -> parse gives back an equal event.

    Through a real JSON string, not just a dict, because the log is a file: anything that
    survives `model_dump` but not `json.dumps` would break on the way to disk.
    """
    wire = json.dumps(event.model_dump(mode="json", exclude_none=True))
    assert parse_event(json.loads(wire)) == event


def test_every_taxonomy_type_is_registered() -> None:
    """EVENT_TYPES and the discriminated union agree, so neither can gain a type quietly."""
    assert {e.type for e in _one_of_each()} == set(EVENT_TYPES)


# --- the content hash --------------------------------------------------------------------


def test_hash_is_stable_across_field_order() -> None:
    a = Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", confidence=0.5, evidence_count=2)
    b = Assertion(evidence_count=2, confidence=0.5, summary="s", body="b", actor=ACTOR_USER, ts=TS)
    assert content_hash(a) == content_hash(b)


def test_hash_ignores_seq_but_not_content() -> None:
    """seq is position, not content: the same event appended twice has one hash (D66)."""
    base = Observation(ts=TS, actor=ACTOR_OBSERVER, source="transcript", observed_at=TS, body="x")
    assert content_hash(base.model_copy(update={"seq": 1})) == content_hash(
        base.model_copy(update={"seq": 9999})
    )
    assert content_hash(base.model_copy(update={"body": "y"})) != content_hash(base)


def test_canonical_json_is_sorted_compact_ascii() -> None:
    event = Assertion(ts=TS, actor=ACTOR_USER, body="café", summary="s")
    text = canonical_json(event)
    assert ", " not in text and '": ' not in text  # compact
    assert "café" not in text and "caf\\u00e9" in text  # ascii-escaped, byte-stable
    keys = list(json.loads(text))
    assert keys == sorted(keys)
    assert "seq" not in keys


# --- strictness --------------------------------------------------------------------------


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        parse_event(
            {
                "type": "assertion",
                "v": 1,
                "ts": TS,
                "actor": ACTOR_USER,
                "body": "b",
                "summary": "s",
                "importance": 0.9,  # not a field — a silent drop would be data loss
            }
        )


def test_events_are_frozen() -> None:
    event = _one_of_each()[0]
    with pytest.raises(ValidationError):
        event.ts = TS  # type: ignore[misc]


def test_every_event_carries_a_version() -> None:
    for event in _one_of_each():
        assert event.v == type(event).SCHEMA_VERSION == 1
        assert "v" in event.model_dump(mode="json")


@pytest.mark.parametrize(
    "ts",
    ["2026-10-10T14:37:02", "not a time", "2026-13-01T00:00:00+00:00", ""],
    ids=["naive", "garbage", "month-13", "empty"],
)
def test_bad_timestamps_are_rejected(ts: str) -> None:
    """A naive timestamp is refused: this log is meant to outlive a timezone change."""
    with pytest.raises(ValidationError):
        Observation(ts=ts, actor=ACTOR_OBSERVER, source="s", observed_at=TS, body="b")


def test_world_time_takes_a_date_or_an_instant() -> None:
    """ "Since March" is a date; widening it to midnight invents precision (D93)."""
    assert (
        Assertion(
            ts=TS, actor=ACTOR_USER, body="b", summary="s", valid_from="2026-03-01"
        ).valid_from
        == "2026-03-01"
    )
    assert Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", valid_from=TS).valid_from == TS
    with pytest.raises(ValidationError):
        Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", valid_from="March")


def test_instants_have_one_spelling() -> None:
    """`datetime.fromisoformat` takes four spellings of one moment; the log takes one.

    Three different strings for the same instant would be three hashes and three things to
    grep for, in a file whose whole claim is that you can read it with `cat` (D10).
    """
    for alias in ["20261010T143702+0000", "2026-10-10 14:37:02+00:00", "2026-10-10T14:37:02"]:
        with pytest.raises(ValidationError):
            Observation(ts=alias, actor=ACTOR_OBSERVER, source="s", observed_at=TS, body="b")
    # Z and +00:00 are the same offset by definition, so one is normalised to the other.
    event = Observation(
        ts="2026-10-10T14:37:02Z", actor=ACTOR_OBSERVER, source="s", observed_at=TS, body="b"
    )
    assert event.ts == "2026-10-10T14:37:02+00:00"
    assert content_hash(event) == content_hash(
        Observation(ts=TS, actor=ACTOR_OBSERVER, source="s", observed_at=TS, body="b")
    )
    # Sub-second precision is a real difference, not a spelling, so it survives.
    assert (
        Observation(
            ts="2026-10-10T14:37:02.5+00:00",
            actor=ACTOR_OBSERVER,
            source="s",
            observed_at=TS,
            body="b",
        ).ts
        == "2026-10-10T14:37:02.5+00:00"
    )


def test_world_dates_have_one_spelling() -> None:
    """`date.fromisoformat` accepts basic and week forms; both name a day another way."""
    for alias in ["20260301", "2026-W09-1", "2026-03", "2026-02-30"]:
        with pytest.raises(ValidationError):
            Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", valid_from=alias)


def test_summary_and_seq_reject_degenerate_values() -> None:
    """An empty summary renders a blank index line — the one thing summary exists for."""
    with pytest.raises(ValidationError):
        Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="")
    with pytest.raises(ValidationError):
        Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", seq=0)


@pytest.mark.parametrize(
    "value",
    [
        "../../etc/passwd:1",
        "d-../mbp:1",
        "d-MBP:1",
        "d-mbp:0",
        "d-mbp:-1",
        "d-mbp",
        "mbp:1",
        "d-mbp:1:2",
        "d-mb p:1",
    ],
    ids=[
        "traversal",
        "traversal-in-device",
        "uppercase",
        "zero-seq",
        "negative-seq",
        "no-seq",
        "no-prefix",
        "two-colons",
        "space",
    ],
)
def test_bad_refs_are_rejected(value: str) -> None:
    """A ref becomes a path segment, so anything that could escape is refused here (D85).

    Uppercase is refused too: on a case-insensitive filesystem `d-MBP` and `d-mbp` would be
    one file under two stream ids.
    """
    with pytest.raises(ValidationError):
        Supersede(ts=TS, actor=ACTOR_USER, target=value)


def test_ref_helpers_round_trip() -> None:
    assert parse_ref(make_ref("d-mbp", 44881)) == ("d-mbp", 44881)
    with pytest.raises(ValueError):
        make_ref("d-mbp", 0)
    with pytest.raises(ValueError):
        make_ref("../etc", 1)


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", confidence=1.5)


# --- schema evolution (D88, D96, D111) ---------------------------------------------------


def test_an_event_from_a_newer_build_halts() -> None:
    """D111: refuse, naming both versions. A newer build may have repurposed a field this
    one thinks it understands, so reading it at all risks acting on a misreading."""
    with pytest.raises(FutureSchemaError) as caught:
        parse_event(
            {
                "type": "assertion",
                "v": 99,
                "ts": TS,
                "actor": ACTOR_USER,
                "body": "b",
                "summary": "s",
            }
        )
    assert caught.value.found == 99
    assert caught.value.known == 1
    assert "v99" in str(caught.value) and "v1" in str(caught.value)


def test_the_upcast_chain_runs_in_sequence() -> None:
    """The scaffold works before it is needed (D88).

    Built against a throwaway subclass rather than a real event type, so this test keeps
    passing unchanged when a real version bump lands — it is testing the mechanism.
    """

    def rename_title_to_name(data: dict) -> dict:
        # A removed field must be dropped explicitly. `{**d, ...}` splats *before* the
        # later expressions run, so `{**d, "name": d.pop("title")}` leaves `title` in
        # place — and extra="forbid" then rejects the event it was meant to rescue.
        out = {k: v for k, v in data.items() if k != "title"}
        out["name"] = data.get("title", "")
        return out

    class Versioned(Event):
        type: str = "versioned"
        name: str

        SCHEMA_VERSION: ClassVar[int] = 3
        UPCASTS: ClassVar[dict] = {
            1: rename_title_to_name,
            2: lambda d: {**d, "name": d["name"].strip()},
        }

    event = Versioned.model_validate({"v": 1, "ts": TS, "actor": "x", "title": "  hi  "})
    assert (event.name, event.v) == ("hi", 3)


def test_a_missing_upcaster_is_an_error_not_a_guess() -> None:
    class Gappy(Event):
        type: str = "gappy"

        SCHEMA_VERSION: ClassVar[int] = 2
        UPCASTS: ClassVar[dict] = {}

    with pytest.raises(ValueError, match="no upcaster"):
        Gappy.model_validate({"v": 1, "ts": TS, "actor": "x"})


@pytest.mark.parametrize(
    "ref", ["d-mbp:1\n", "d-mbp:1\r\n", "\nd-mbp:1"], ids=["lf", "crlf", "leading"]
)
def test_a_ref_with_a_newline_is_rejected(ref: str) -> None:
    """The worst version of the `$`-matches-before-newline hole.

    "d-mbp:1\\n" prints identically to "d-mbp:1" and compares unequal, so one event could be
    named by two refs that no one could tell apart by eye — the ambiguity this project calls
    its worst failure. Two author-blind passes missed it; an independent reviewer found it.
    """
    with pytest.raises(ValidationError):
        Supersede(ts=TS, actor=ACTOR_USER, target=ref)


def test_times_with_a_newline_are_rejected() -> None:
    """A trailing newline on a timestamp hashes differently while looking identical."""
    with pytest.raises(ValidationError):
        Observation(ts=TS + "\n", actor=ACTOR_OBSERVER, source="s", observed_at=TS, body="b")
    with pytest.raises(ValidationError):
        Assertion(ts=TS, actor=ACTOR_USER, body="b", summary="s", valid_from="2026-03-01\n")
