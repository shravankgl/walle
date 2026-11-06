"""Task 12, `walle why`: the full `caused_by` chain, from a recursive query over `edges` (D59).

  full chain        every ancestor, across streams, each once at its shortest depth
  from the index    the chain is the index's `WITH RECURSIVE` answer, not a walk of the files
  cycle             a hand-made cycle in `caused_by` still ends
  status            a forgotten assertion is still explained, and says what retired it
  refusals          a ref with no event, or not a ref at all, is said plainly

Every event here is synthetic: a made-up person's kitchen and plants, no real data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import index, why
from walle.cli import main
from walle.events import (
    ACTOR_OBSERVER,
    ACTOR_USER,
    Assertion,
    Event,
    Observation,
    Proposal,
    Supersede,
    now_instant,
)
from walle.paths import stream_path
from walle.writer import StreamWriter

P = "maya"


def _append(root: Path, device: str, event: Event) -> str:
    written = StreamWriter(root, P, device).append(event)
    return f"d-{device}:{written.seq}"


def _obs(body: str, *causes: str) -> Observation:
    return Observation(
        ts=now_instant(),
        actor=ACTOR_OBSERVER,
        source="transcript",
        observed_at=now_instant(),
        body=body,
        caused_by=list(causes),
    )


def _assertion(text: str, *causes: str) -> Assertion:
    return Assertion(
        ts=now_instant(), actor=ACTOR_USER, body=text, summary=text, caused_by=list(causes)
    )


def _chain_log(root: Path) -> dict[str, str]:
    """Two streams, a diamond and a bystander.

    obs_a (mbp) ─┐
                 ├─ proposal (mbp) ── kept (mbp) ──┐
    obs_b (phone)┘                                 ├─ restated (phone)
    obs_a ─────────────────────────────────────────┘
    unrelated (mbp): cites nothing in the chain, and nothing cites it.
    """
    refs: dict[str, str] = {}
    refs["obs_a"] = _append(root, "mbp", _obs("Maya said the basil wilts by noon."))
    refs["obs_b"] = _append(root, "phone", _obs("Maya moved the basil to the east sill."))
    refs["proposal"] = _append(
        root,
        "mbp",
        Proposal(
            ts=now_instant(),
            actor=ACTOR_USER,
            body="The basil lives on the east windowsill.",
            summary="basil on the east sill",
            caused_by=[refs["obs_a"], refs["obs_b"]],
        ),
    )
    refs["kept"] = _append(
        root, "mbp", _assertion("The basil lives on the east windowsill.", refs["proposal"])
    )
    refs["unrelated"] = _append(root, "mbp", _obs("Maya bought rice."))
    refs["restated"] = _append(
        root, "phone", _assertion("Basil: east sill, morning sun.", refs["kept"], refs["obs_a"])
    )
    return refs


# --- the verify criterion ---------------------------------------------------------------


def test_why_returns_the_full_caused_by_chain(tmp_path: Path) -> None:
    """PLAN §9 task 12: `why` returns the full `caused_by` chain. Every ancestor, across both
    streams, once each at its shortest distance, and nothing outside the chain."""
    refs = _chain_log(tmp_path)

    explanation = why.explain(tmp_path, P, refs["restated"])

    got = [(step.ref, step.depth) for step in explanation.steps]
    assert got == [
        (refs["restated"], 0),
        (refs["obs_a"], 1),  # cited directly, so depth 1 even though it is also deeper
        (refs["kept"], 1),
        (refs["proposal"], 2),
        (refs["obs_b"], 3),
    ]
    assert refs["unrelated"] not in {step.ref for step in explanation.steps}
    by_ref = {step.ref: step for step in explanation.steps}
    assert isinstance(by_ref[refs["proposal"]].event, Proposal)
    assert by_ref[refs["proposal"]].event.caused_by == [refs["obs_a"], refs["obs_b"]]


def test_the_chain_comes_from_the_index_edges(tmp_path: Path) -> None:
    """D59: `why` is a recursive query over `edges` in the index. `ancestors` reads the index
    only, so an event the index has not taken in yet is not in its answer."""
    refs = _chain_log(tmp_path)
    index.rebuild(tmp_path, P)
    late = _append(tmp_path, "mbp", _assertion("late", refs["restated"]))

    with pytest.raises(KeyError, match=late):
        index.ancestors(tmp_path, P, late)  # not indexed yet

    chain = index.ancestors(tmp_path, P, refs["kept"])
    assert [(ref, depth) for ref, depth, _ in chain] == [
        (refs["kept"], 0),
        (refs["proposal"], 1),
        (refs["obs_a"], 2),
        (refs["obs_b"], 2),
    ]

    # `explain` brings the index up to date first, so the late event is explained.
    assert why.explain(tmp_path, P, late).steps[1].ref == refs["restated"]


def test_a_cycle_in_caused_by_still_ends(tmp_path: Path) -> None:
    """The writer refuses a cause that is not in the log yet (#121), so a cycle can only come
    from a hand-made log: the first line is planted by hand. Both refs exist, so the index
    takes it; the query must still terminate and list each event once."""
    first = _obs("one", "d-phone:1").model_copy(update={"seq": 1})
    path = stream_path(tmp_path, P, "mbp")
    path.parent.mkdir(parents=True)
    path.write_bytes(json.dumps(first.model_dump(mode="json", exclude_none=True)).encode() + b"\n")
    _append(tmp_path, "phone", _obs("two", "d-mbp:1"))

    steps = why.explain(tmp_path, P, "d-mbp:1").steps

    assert [(s.ref, s.depth) for s in steps] == [("d-mbp:1", 0), ("d-phone:1", 1)]


def test_why_still_explains_a_forgotten_assertion(tmp_path: Path) -> None:
    """A supersede hides an assertion from the fold; it never removes it from the log, so
    `why` still finds it, its chain, and the supersede that retired it, with the world time."""
    refs = _chain_log(tmp_path)
    gone = _append(
        tmp_path,
        "mbp",
        Supersede(
            ts=now_instant(),
            actor=ACTOR_USER,
            target=refs["kept"],
            reason="forgotten",
            valid_to="2026-10-01",
        ),
    )

    explanation = why.explain(tmp_path, P, refs["kept"])

    assert [s.ref for s in explanation.steps][:2] == [refs["kept"], refs["proposal"]]
    status = explanation.steps[0].status
    assert status is not None
    assert "forgotten" in status and gone in status and "2026-10-01" in status
    # The restated assertion on the phone is untouched and still current.
    assert why.explain(tmp_path, P, refs["restated"]).steps[0].status == "current"


def test_why_says_where_a_proposal_went(tmp_path: Path) -> None:
    refs = _chain_log(tmp_path)
    proposal = next(s for s in why.explain(tmp_path, P, refs["kept"]).steps if s.depth == 1)
    assert proposal.status == f"kept as {refs['kept']}"


def test_why_refuses_a_ref_with_no_event(tmp_path: Path) -> None:
    _chain_log(tmp_path)
    with pytest.raises(KeyError, match="d-mbp:99"):
        why.explain(tmp_path, P, "d-mbp:99")
    with pytest.raises(ValueError, match="not a provenance ref"):
        why.explain(tmp_path, P, "mbp-1")


# --- the command ------------------------------------------------------------------------


def test_walle_why_prints_the_chain(tmp_path: Path) -> None:
    refs = _chain_log(tmp_path)
    result = CliRunner().invoke(
        main, ["why", "--root", str(tmp_path), "--principal", P, refs["kept"]]
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith(f"{refs['kept']} assertion")
    assert "current" in lines[0]
    assert any(line.startswith(f"  {refs['proposal']} proposal") for line in lines)
    assert any(line.startswith(f"    {refs['obs_b']} observation") for line in lines)
    assert "east windowsill" in result.stdout


def test_walle_why_on_a_missing_ref_fails_plainly(tmp_path: Path) -> None:
    _chain_log(tmp_path)
    result = CliRunner().invoke(
        main, ["why", "--root", str(tmp_path), "--principal", P, "d-mbp:99"]
    )
    assert result.exit_code == 1
    assert "no event d-mbp:99" in result.stderr
    assert "Traceback" not in result.output


# --- issue #47 item 1: the status names other events, and the ledger says which ----------


def test_the_ledger_says_which_events_a_status_quotes(tmp_path: Path) -> None:
    """A status is built from other events' fields (a supersede's reason and day, the keep
    that decided a proposal), so a label over the status must cover them (D84, D97)."""
    refs = _chain_log(tmp_path)
    gone = _append(
        tmp_path,
        "mbp",
        Supersede(ts=now_instant(), actor=ACTOR_USER, target=refs["kept"], reason="forgotten"),
    )
    ledger = why.Ledger(index.update(tmp_path, P).records)
    assert ledger.cited(refs["kept"]) == (gone,)
    assert ledger.cited(refs["proposal"]) == (refs["kept"],)
    assert ledger.cited(refs["restated"]) == ()  # current, and nothing closed it
    assert ledger.cited(refs["obs_a"]) == ()  # an observation has no status
    assert ledger.cited("d-mbp:99") == ()
