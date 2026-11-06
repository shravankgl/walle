"""Issue #131: leftovers across actors, answers and ordering (audit 2, R8).

Each finding gets a test that failed on main 8af004b before the fix:

- **4.2** ``builder`` was missing from ``labels.RESERVED_ACTORS``, so the MCP server accepted
  ``--principal builder`` and the Builder's own words were labelled ``trusted`` for a
  principal of that name. The reserved set is now ``events.MACHINE_ACTORS`` itself.
- **5** an inbox answer timed before its ask, or an item whose ask is not timed at the
  pending's ask, settled a rung-3 pending. ``settle`` refuses the first as ``EARLY`` and the
  second as ``MISMATCH``, as ``resolve`` does for a typed reply.
- **7** ``staging`` and ``why`` named the assertion that kept a proposal by ref order, while
  ``stats`` decides by ``fold_order``; across devices they named different assertions. All
  three now agree. ``rungs.reply_for`` is private: nothing outside ``rungs`` turns an inbox
  outcome into a reply without ``settle``'s binding.
- **4.3** ``walle review`` edited a multi-line body through a one-line prompt. A body that
  spans lines is now edited in the editor.

Every event is synthetic: made-up people, plants and commands.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from walle import conversation, events, inbox, labels, mcp_server, rungs, staging, stats
from walle.cli import main
from walle.events import (
    ACTOR_BUILDER,
    ACTOR_USER,
    Answer,
    AnswerOutcome,
    Assertion,
    Proposal,
)
from walle.grants import Call
from walle.paths import log_dir, principal_dir
from walle.profiles import Role
from walle.reader import Record, read_file
from walle.resolver import resolve
from walle.speakers import Speaker
from walle.why import Ledger
from walle.writer import StreamWriter

P = "ada"
D = "mbp"
SESSION = "s-1"
T_BEFORE = "2026-11-05T09:59:00+00:00"
T0 = "2026-11-05T10:00:00+00:00"
T1 = "2026-11-05T10:01:00+00:00"
STATUS = Call(tool="shell", arguments="git status")
ORDER = conversation.authority(P, Speaker(P, Role.OPERATOR))
INBOX_ON = {inbox.INBOX_SWITCH: inbox.SWITCH_ON}
RUNGS_ON = {rungs.RUNGS_SWITCH: rungs.SWITCH_ON}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    principal_dir(tmp_path, P).mkdir(parents=True)
    return tmp_path


def _records(root: Path, principal: str = P) -> list[Record]:
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _refused(outcome: object, kind: rungs.Refusal) -> rungs.Refused:
    assert isinstance(outcome, rungs.Refused), outcome
    assert outcome.kind is kind, outcome
    return outcome


# --- 4.2: the Builder is a reserved actor everywhere ------------------------------------------


def test_the_builder_words_are_never_trusted_as_a_principal_of_that_name() -> None:
    """The audit's reproduction: a Builder proposal read for principal ``builder``."""
    p = Proposal(seq=1, ts=T0, actor=ACTOR_BUILDER, body="x", summary="x")
    assert labels.ifc({"d-mbp:1": p}, "d-mbp:1", ACTOR_BUILDER)["integrity"] == labels.UNTRUSTED


def test_the_reserved_actors_are_the_machine_actors() -> None:
    """One list (issue #119): a machine actor added to ``events`` is reserved here at once."""
    assert labels.RESERVED_ACTORS is events.MACHINE_ACTORS


def test_the_mcp_server_refuses_the_builder_as_a_principal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        mcp_server.build_server(tmp_path, ACTOR_BUILDER, D)
    assert not principal_dir(tmp_path, ACTOR_BUILDER).exists()


# --- 5: an inbox answer is bound to the moment of its ask -------------------------------------


def _placed(root: Path, ask: events.Ask) -> inbox.Item:
    return inbox.ask_event(root, P, D, ask, environ=INBOX_ON).item


def _item(root: Path, ref: str) -> inbox.Item:
    return inbox.fold_inbox(_records(root)).item(ref)


def test_an_answer_timed_before_its_ask_settles_nothing(root: Path) -> None:
    """The audit's reproduction: an approval on another device, stamped a minute before the
    ask, folds as the item's answer (the inbox folds asks, then answers). Rung 3 refuses it
    as early, as ``resolve`` refuses a typed reply timed before the request."""
    asked = rungs.ask(
        rungs.Ledger.start(P),
        STATUS,
        id="c-1",
        session=SESSION,
        request="r-1",
        ts=T0,
        environ=RUNGS_ON,
    )
    assert isinstance(asked, rungs.Asked)
    placed = _placed(root, rungs.to_ask(asked.pending, unattended=True))
    StreamWriter(root, P, "phone").append(
        Answer(ts=T_BEFORE, actor=ACTOR_USER, ask=placed.ref, outcome=AnswerOutcome.APPROVED)
    )
    item = _item(root, placed.ref)
    assert item.answer is not None and item.answer.ts == T_BEFORE  # the fold took it
    before = len(_records(root))

    refused = _refused(
        rungs.settle(asked.ledger, item, by=ORDER, environ=RUNGS_ON), rungs.Refusal.EARLY
    )
    assert refused.ledger == asked.ledger
    assert "git status" not in refused.reason
    assert len(_records(root)) == before  # nothing written on a refusal

    # the same item answered at or after the ask still settles
    on_time = inbox.Item(
        item.ref, item.ask, item.answer_ref, item.answer.model_copy(update={"ts": T0})
    )
    assert isinstance(
        rungs.settle(asked.ledger, on_time, by=ORDER, environ=RUNGS_ON), rungs.Approved
    )


def test_an_item_whose_ask_is_not_timed_at_the_pending_ask_is_another_item(root: Path) -> None:
    """The binding includes the moment: same session, id and card, but asked at another time,
    is not the item ``to_ask`` made for this pending."""
    asked = rungs.ask(
        rungs.Ledger.start(P),
        STATUS,
        id="c-1",
        session=SESSION,
        request="r-1",
        ts=T0,
        environ=RUNGS_ON,
    )
    assert isinstance(asked, rungs.Asked)
    later = rungs.to_ask(asked.pending, unattended=True).model_copy(update={"ts": T1})
    placed = _placed(root, later)
    resolved = inbox.answer(root, P, D, placed.ref, AnswerOutcome.APPROVED, environ=INBOX_ON)
    refused = _refused(
        rungs.settle(asked.ledger, resolved.item, by=ORDER, environ=RUNGS_ON),
        rungs.Refusal.MISMATCH,
    )
    assert refused.ledger == asked.ledger


# --- 7: one answer for which assertion kept a proposal ----------------------------------------


def _kept_on_two_devices(root: Path) -> tuple[str, str, str]:
    """A proposal on the laptop, kept on the phone at 09:00 and on the laptop at 10:00
    before the logs met. By ref the laptop's keep (``d-mbp:2``) sorts first; by time, which
    is how ``stats`` decides, the phone's (``d-phone:1``) came first."""
    mbp = StreamWriter(root, P, "mbp")
    phone = StreamWriter(root, P, "phone")
    body, summary = "Ada waters the fern on Sundays.", "waters the fern"
    mbp.append(
        Proposal(ts="2026-11-05T08:00:00+00:00", actor=ACTOR_USER, body=body, summary=summary)
    )
    phone.append(
        Assertion(
            ts="2026-11-05T09:00:00+00:00",
            actor=ACTOR_USER,
            body=body,
            summary=summary,
            caused_by=["d-mbp:1"],
        )
    )
    mbp.append(
        Assertion(
            ts="2026-11-05T10:00:00+00:00",
            actor=ACTOR_USER,
            body=body,
            summary="waters the fern weekly",
            caused_by=["d-mbp:1"],
        )
    )
    return "d-mbp:1", "d-phone:1", "d-mbp:2"


def test_staging_names_the_keep_stats_counts(root: Path) -> None:
    proposal, first, _ = _kept_on_two_devices(root)
    counts = stats.count(_records(root))
    assert (counts.kept, counts.edited) == (1, 0)  # stats decides by the phone's keep
    with pytest.raises(staging.NotStagedError, match=f"already kept as {first}$"):
        staging.keep(root, P, D, proposal)


def test_why_lists_the_deciding_keep_first(root: Path) -> None:
    proposal, first, second = _kept_on_two_devices(root)
    ledger = Ledger(_records(root))
    assert ledger.status(proposal) == f"kept as {first}, {second}"
    assert ledger.cited(proposal) == (first, second)


def test_reply_for_is_not_public() -> None:
    """Only ``settle`` turns an inbox outcome into a reply, after binding the item."""
    assert "reply_for" not in rungs.__all__
    assert not hasattr(rungs, "reply_for")


# --- 4.3: a multi-line body is edited in the editor --------------------------------------------


def _remember(root: Path, text: str) -> str:
    return staging.remember(root, P, D, text, summary="fern care").ref


def test_review_edits_a_multi_line_body_in_the_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "Ada waters the fern on Sundays.\nShe mists it on Wednesdays."
    _remember(tmp_path, body)
    seen: list[str | None] = []

    def fake_edit(text: str | None = None, **_: object) -> str:
        seen.append(text)
        return "Ada waters the fern on Sundays.\nShe mists it on Wednesdays and Fridays.\n"

    monkeypatch.setattr(click, "edit", fake_edit)
    result = CliRunner().invoke(
        main,
        ["review", "--root", str(tmp_path), "--principal", P, "--device", D],
        input="e\n\n",
    )
    assert result.exit_code == 0, result.output
    assert seen == [body]  # the editor opened on the whole current body
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.body == (
        "Ada waters the fern on Sundays.\nShe mists it on Wednesdays and Fridays."
    )


def test_review_keeps_a_multi_line_body_when_the_editor_saves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "Ada waters the fern on Sundays.\nShe mists it on Wednesdays."
    _remember(tmp_path, body)
    monkeypatch.setattr(click, "edit", lambda text=None, **_: None)
    result = CliRunner().invoke(
        main,
        ["review", "--root", str(tmp_path), "--principal", P, "--device", D],
        input="e\n\n",
    )
    assert result.exit_code == 0, result.output
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.body == body


def test_review_leaves_an_emptied_multi_line_body_staged_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _remember(tmp_path, "Ada waters the fern on Sundays.\nShe mists it on Wednesdays.")
    monkeypatch.setattr(click, "edit", lambda text=None, **_: "  \n")
    before = len(_records(tmp_path))
    result = CliRunner().invoke(
        main,
        ["review", "--root", str(tmp_path), "--principal", P, "--device", D],
        input="e\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "empty" in result.output and "still staged" in result.output
    assert len(_records(tmp_path)) == before
    assert [s.ref for s in staging.pending(tmp_path, P)] == [ref]


def test_review_still_prompts_for_a_one_line_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remember(tmp_path, "Ada waters the fern on Sundays.")

    def no_editor(*_: object, **__: object) -> str:
        raise AssertionError("a one-line body is edited at the prompt")

    monkeypatch.setattr(click, "edit", no_editor)
    result = CliRunner().invoke(
        main,
        ["review", "--root", str(tmp_path), "--principal", P, "--device", D],
        input="e\n\nAda waters the fern twice a week.\n",
    )
    assert result.exit_code == 0, result.output
    (belief,) = resolve(_records(tmp_path)).current
    assert belief.assertion.body == "Ada waters the fern twice a week."


def test_review_reports_and_writes_nothing_when_the_editor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``click.edit`` raises ``ClickException`` when the editor cannot run. The batch stops
    with the summary line, as for an abort, and the item stays staged."""
    ref = _remember(tmp_path, "Ada waters the fern on Sundays.\nShe mists it on Wednesdays.")

    def broken(*_: object, **__: object) -> str:
        raise click.ClickException("Editing failed")

    monkeypatch.setattr(click, "edit", broken)
    before = len(_records(tmp_path))
    result = CliRunner().invoke(
        main,
        ["review", "--root", str(tmp_path), "--principal", P, "--device", D],
        input="e\n\n",
    )
    assert result.exit_code != 0
    assert "Editing failed" in result.output and "1 not reached" in result.output
    assert len(_records(tmp_path)) == before
    assert [s.ref for s in staging.pending(tmp_path, P)] == [ref]
