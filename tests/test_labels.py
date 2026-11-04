"""Task 13, information-flow labels computed from the provenance closure (D84, D97).

  closure           an event and everything its caused_by reaches, cycle-safe
  integrity         untrusted iff any event in the closure is not the principal's own words
  never assigned    no field on any event can set a label; changing the log changes it
  confidentiality   private: every event lives in one principal's folder (D32)

Every event here is synthetic.
"""

from __future__ import annotations

import inspect

import pytest

from walle import labels
from walle.events import (
    ACTOR_OBSERVER,
    ACTOR_PROPOSER,
    ACTOR_USER,
    AnyEvent,
    Assertion,
    Observation,
    Proposal,
    now_instant,
)
from walle.labels import PRIVATE, TRUSTED, UNTRUSTED, closure, ifc, integrity

P = "maya"


def _obs(*causes: str) -> Observation:
    return Observation(
        ts=now_instant(),
        actor=ACTOR_OBSERVER,
        source="transcript",
        observed_at=now_instant(),
        body="user: the basil wilts\nassistant: move it east",
        caused_by=list(causes),
    )


def _proposal(actor: str, *causes: str) -> Proposal:
    return Proposal(ts=now_instant(), actor=actor, body="b", summary="s", caused_by=list(causes))


def _assertion(actor: str, *causes: str) -> Assertion:
    return Assertion(ts=now_instant(), actor=actor, body="b", summary="s", caused_by=list(causes))


def test_closure_is_the_event_and_every_ancestor() -> None:
    events: dict[str, AnyEvent] = {
        "d-mbp:1": _obs(),
        "d-mbp:2": _proposal(ACTOR_USER, "d-mbp:1"),
        "d-mbp:3": _assertion(ACTOR_USER, "d-mbp:2"),
        "d-mbp:4": _assertion(ACTOR_USER),
    }
    assert closure(events, "d-mbp:3") == frozenset({"d-mbp:1", "d-mbp:2", "d-mbp:3"})
    assert closure(events, "d-mbp:4") == frozenset({"d-mbp:4"})


def test_closure_ends_on_a_cycle() -> None:
    events: dict[str, AnyEvent] = {
        "d-mbp:1": _assertion(ACTOR_USER, "d-mbp:2"),
        "d-mbp:2": _assertion(ACTOR_USER, "d-mbp:1"),
    }
    assert closure(events, "d-mbp:1") == frozenset({"d-mbp:1", "d-mbp:2"})


def test_closure_of_an_unknown_ref_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        closure({}, "d-mbp:1")


def test_the_principals_own_words_are_trusted() -> None:
    events: dict[str, AnyEvent] = {
        "d-mbp:1": _proposal(ACTOR_USER),
        "d-mbp:2": _assertion(ACTOR_USER, "d-mbp:1"),
        "d-mbp:3": _assertion(P),
    }
    assert integrity(events, "d-mbp:2", P) == TRUSTED
    assert integrity(events, "d-mbp:3", P) == TRUSTED


@pytest.mark.parametrize("actor", [ACTOR_OBSERVER, ACTOR_PROPOSER, "mcp", "curator", "ravi"])
def test_one_non_principal_ancestor_makes_it_untrusted(actor: str) -> None:
    """A transcript turn holds the assistant's words and tool output; a model's proposal is
    another agent's output; another principal is not this one. A human keep on top of any of
    them does not launder it (D97)."""
    events: dict[str, AnyEvent] = {
        "d-mbp:1": _proposal(actor),
        "d-mbp:2": _assertion(ACTOR_USER, "d-mbp:1"),
        "d-mbp:3": _assertion(ACTOR_USER, "d-mbp:2"),
    }
    assert integrity(events, "d-mbp:3", P) == UNTRUSTED


def test_an_observation_anywhere_upstream_makes_it_untrusted() -> None:
    events: dict[str, AnyEvent] = {
        "d-phone:1": _obs(),
        "d-mbp:1": _proposal(ACTOR_USER),
        "d-mbp:2": _assertion(ACTOR_USER, "d-mbp:1", "d-phone:1"),
    }
    assert integrity(events, "d-mbp:2", P) == UNTRUSTED
    assert integrity(events, "d-mbp:1", P) == TRUSTED


def test_a_missing_ancestor_fails_closed() -> None:
    """The index refuses a dangling ref, so this cannot come from a built index. If it ever
    does, what cannot be checked is not trusted."""
    events: dict[str, AnyEvent] = {"d-mbp:2": _assertion(ACTOR_USER, "d-mbp:1")}
    assert integrity(events, "d-mbp:2", P) == UNTRUSTED


def test_the_label_follows_the_log_not_a_field() -> None:
    """Never assigned: the same assertion changes label when, and only when, its closure
    changes. No event field names a label."""
    trusted: dict[str, AnyEvent] = {
        "d-mbp:1": _proposal(ACTOR_USER),
        "d-mbp:2": _assertion(ACTOR_USER, "d-mbp:1"),
    }
    tainted = {**trusted, "d-mbp:1": _proposal("mcp")}
    assert integrity(trusted, "d-mbp:2", P) == TRUSTED
    assert integrity(tainted, "d-mbp:2", P) == UNTRUSTED
    for model in (Assertion, Proposal, Observation):
        assert not {"integrity", "confidentiality", "ifc", "label"} & set(model.model_fields)


def test_ifc_is_the_fides_shape() -> None:
    events: dict[str, AnyEvent] = {"d-mbp:1": _obs(), "d-mbp:2": _assertion(P)}
    assert ifc(events, "d-mbp:1", P) == {"integrity": UNTRUSTED, "confidentiality": PRIVATE}
    assert ifc(events, "d-mbp:2", P) == {"integrity": TRUSTED, "confidentiality": PRIVATE}


def test_labels_do_no_io_and_call_no_model() -> None:
    """A pure function over events (D97, hard constraint 3)."""
    source = inspect.getsource(labels)
    for word in ("open(", "Path(", "sqlite3", "import os", "aisuite", "anthropic"):
        assert word not in source
