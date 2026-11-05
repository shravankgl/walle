"""Issue #101's verify criteria, and the cases around them (D62, D73, D116, D74, D72, D117).

  only the literal word     ``#id always`` is the one reply that hands back ``Scope.ALWAYS``;
  ``always`` makes a        every case, whitespace and punctuation variant, every keen reply,
  standing rule             and every inbox outcome resolve to nothing standing
  a bare ``*`` is refused   as an envelope pattern, as an envelope tool, and in the shapes a
                            person might spell it
  a replayed or expired     the same reply a second time is refused; a reply past fifteen
  approval fails            minutes is refused, and so is one from before the ask
  exact match, one command  the reply names the id, the id names the one command, and what
                            ``Approved`` hands back is that command, not a paraphrase; a
                            second ask under the same id with another command is refused
  a button is its label     a payload that differs from its label resolves nothing
  rung 4 is bounded         the envelope is ``tool: pattern*``; each part of a command must be
                            justified (D74); a destructive call has no standing yes (D116)
  off by default            with the switch unset nothing is asked, nothing resolves, nothing
                            is bounded, and nothing is parsed

Every root is a ``root``. Every tool, command, id, session and request is invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from walle import floors, grants, matching, rungs
from walle.events import AnswerOutcome, AskKind
from walle.grants import Call, Scope
from walle.paths import principal_dir
from walle.profiles import Role
from walle.rungs import (
    Approved,
    Asked,
    Button,
    Denied,
    Envelope,
    Ledger,
    Pending,
    Refusal,
    Refused,
    Reply,
    RungsOffError,
    Standing,
    ask,
    bounded,
    parse,
    reply_for,
    resolve,
    to_ask,
)
from walle.speakers import Order, Speaker

ON = {rungs.RUNGS_SWITCH: rungs.SWITCH_ON, matching.MATCHING_SWITCH: matching.SWITCH_ON}
P = "lena"
T0 = "2026-11-05T10:00:00+00:00"
T5 = "2026-11-05T10:05:00+00:00"
T15 = "2026-11-05T10:15:00+00:00"
T15_PLUS = "2026-11-05T10:15:00.000001+00:00"
T16 = "2026-11-05T10:16:00+00:00"
SESSION = "s-aaaa"
REQUEST = "r-1"
STATUS = Call(tool="shell", arguments="git status")
DIFF = Call(tool="shell", arguments="git diff --stat")
PRUNE = Call(tool="shell", arguments="rm -rf /tmp/allotment/old", destructive=True)
EMPTY = Ledger.start(P)
ORDER = Order(P, Speaker(P, Role.OPERATOR))
"""Every reply in this file is the owner's, speaking as the operator; who else may reply is
``test_audit_approvals.py``'s subject (issue #119)."""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    principal_dir(tmp_path, P).mkdir(parents=True)
    return tmp_path


def _ask(
    ledger: Ledger, call: Call, *, id: str = "c-1", ts: str = T0, environ: dict[str, str] = ON
) -> Asked:
    outcome = ask(ledger, call, id=id, session=SESSION, request=REQUEST, ts=ts, environ=environ)
    assert isinstance(outcome, Asked), outcome
    return outcome


def _resolve(ledger: Ledger, reply: str | Button, *, ts: str = T5) -> Approved | Denied | Refused:
    return resolve(ledger, reply, by=ORDER, ts=ts, environ=ON)


def _refused(outcome: object, kind: Refusal) -> Refused:
    assert isinstance(outcome, Refused), outcome
    assert outcome.kind is kind, (outcome.kind, outcome.reason)
    return outcome


# --- rung 3: the request names the exact command -----------------------------------------------


def test_the_card_names_the_exact_command_and_the_answers() -> None:
    asked = _ask(EMPTY, STATUS)
    card = asked.pending.card()
    assert "git status" in card
    assert "#c-1 approve" in card and "#c-1 always" in card and "#c-1 no" in card
    assert asked.pending.answers() == ("#c-1 approve", "#c-1 always", "#c-1 no")
    assert asked.ledger.pending == (asked.pending,)
    assert asked.ledger.settled == ()


def test_an_approve_covers_the_one_command_once() -> None:
    asked = _ask(EMPTY, STATUS)
    outcome = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(outcome, Approved)
    assert outcome.scope is Scope.REQUEST
    assert outcome.pending is asked.pending
    assert outcome.ledger.pending == ()
    assert [s.id for s in outcome.ledger.settled] == ["c-1"]


def test_what_is_approved_is_what_runs() -> None:
    """The command is resolved once, when the card is made, and ``Approved.call`` is that
    very object. A paraphrase, a trailing space, a changed flag is not permitted."""
    asked = _ask(EMPTY, STATUS)
    outcome = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(outcome, Approved)
    assert outcome.call is asked.pending.call
    assert outcome.call.arguments == "git status"
    assert outcome.permits(Call(tool="shell", arguments="git status"))
    for other in (
        Call(tool="shell", arguments="git status "),
        Call(tool="shell", arguments="git  status"),
        Call(tool="shell", arguments="Git status"),
        Call(tool="shell", arguments="git status && git diff"),
        Call(tool="shell", arguments="git status", destructive=True),
        Call(tool="sh", arguments="git status"),
    ):
        assert not outcome.permits(other), other


def test_a_changed_command_under_the_same_id_is_refused() -> None:
    """An id names one command for its whole life: a second ask under it with another
    command is refused, the first stays as it was, and approving the id approves the first."""
    asked = _ask(EMPTY, STATUS)
    changed = ask(asked.ledger, DIFF, id="c-1", session=SESSION, request=REQUEST, ts=T0, environ=ON)
    taken = _refused(changed, Refusal.TAKEN)
    assert "c-1" not in taken.reason and "git" not in taken.reason
    outcome = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(outcome, Approved)
    assert outcome.call == STATUS
    assert not outcome.permits(DIFF)


def test_a_settled_id_is_never_reused_even_for_another_command() -> None:
    asked = _ask(EMPTY, STATUS)
    approved = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(approved, Approved)
    again = ask(
        approved.ledger, DIFF, id="c-1", session=SESSION, request=REQUEST, ts=T5, environ=ON
    )
    _refused(again, Refusal.TAKEN)


def test_asking_the_same_command_under_the_same_id_again_is_the_same_item() -> None:
    """A run that restarts and re-issues its call does not ask twice (inbox rule 1)."""
    asked = _ask(EMPTY, STATUS)
    again = _ask(asked.ledger, STATUS, ts=T5)
    assert again.pending is asked.pending
    assert again.ledger == asked.ledger


def test_the_same_id_from_another_session_or_request_is_refused() -> None:
    """One session's reply never answers another's call: the same id and command from
    another session, or another request, is not the same item."""
    asked = _ask(EMPTY, STATUS)
    other_session = ask(
        asked.ledger, STATUS, id="c-1", session="s-bbbb", request=REQUEST, ts=T0, environ=ON
    )
    _refused(other_session, Refusal.TAKEN)
    other_request = ask(
        asked.ledger, STATUS, id="c-1", session=SESSION, request="r-2", ts=T0, environ=ON
    )
    _refused(other_request, Refusal.TAKEN)


def test_a_no_settles_the_item_and_runs_nothing() -> None:
    asked = _ask(EMPTY, STATUS)
    outcome = _resolve(asked.ledger, "#c-1 no")
    assert isinstance(outcome, Denied)
    assert outcome.pending is asked.pending
    assert outcome.ledger.pending == ()
    _refused(_resolve(outcome.ledger, "#c-1 approve"), Refusal.REPLAYED)


def test_two_pending_items_resolve_independently() -> None:
    first = _ask(EMPTY, STATUS, id="c-1")
    second = _ask(first.ledger, DIFF, id="c-2")
    outcome = _resolve(second.ledger, "#c-2 approve")
    assert isinstance(outcome, Approved)
    assert outcome.call == DIFF
    assert [p.id for p in outcome.ledger.pending] == ["c-1"]
    still = _resolve(outcome.ledger, "#c-1 approve")
    assert isinstance(still, Approved)
    assert still.call == STATUS


# --- replay and expiry -------------------------------------------------------------------------


def test_a_replayed_approval_fails() -> None:
    asked = _ask(EMPTY, STATUS)
    first = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(first, Approved)
    replay = _refused(_resolve(first.ledger, "#c-1 approve"), Refusal.REPLAYED)
    assert replay.ledger == first.ledger
    _refused(_resolve(first.ledger, "#c-1 always"), Refusal.REPLAYED)
    _refused(_resolve(first.ledger, "#c-1 no"), Refusal.REPLAYED)


def test_a_replayed_always_fails_too() -> None:
    asked = _ask(EMPTY, STATUS)
    first = _resolve(asked.ledger, "#c-1 always")
    assert isinstance(first, Approved)
    assert first.scope is Scope.ALWAYS
    _refused(_resolve(first.ledger, "#c-1 always"), Refusal.REPLAYED)


def test_an_approval_within_fifteen_minutes_stands_and_one_after_does_not() -> None:
    asked = _ask(EMPTY, STATUS)
    assert isinstance(_resolve(asked.ledger, "#c-1 approve", ts=T0), Approved)
    assert isinstance(_resolve(asked.ledger, "#c-1 approve", ts=T15), Approved)
    late = _refused(_resolve(asked.ledger, "#c-1 approve", ts=T15_PLUS), Refusal.EXPIRED)
    assert "fifteen" in late.reason or "15" in late.reason
    _refused(_resolve(asked.ledger, "#c-1 always", ts=T16), Refusal.EXPIRED)
    _refused(_resolve(asked.ledger, "#c-1 no", ts=T16), Refusal.EXPIRED)


def test_an_expired_item_is_settled_and_stays_expired() -> None:
    """Expiry settles the item: a later reply, even one back inside the window by a clock
    that went backwards, is refused, and the id cannot be asked again."""
    asked = _ask(EMPTY, STATUS)
    late = _refused(_resolve(asked.ledger, "#c-1 approve", ts=T16), Refusal.EXPIRED)
    assert late.ledger is not None  # a rung 3 refusal always carries the ledger
    assert late.ledger.pending == ()
    assert [s.id for s in late.ledger.settled] == ["c-1"]
    _refused(_resolve(late.ledger, "#c-1 approve", ts=T5), Refusal.EXPIRED)
    again = ask(late.ledger, STATUS, id="c-1", session=SESSION, request=REQUEST, ts=T16, environ=ON)
    _refused(again, Refusal.TAKEN)


def test_a_reply_from_before_the_ask_is_refused() -> None:
    """A clock that reads earlier than the ask is not a reply inside the window."""
    asked = _ask(EMPTY, STATUS, ts=T5)
    early = _refused(_resolve(asked.ledger, "#c-1 approve", ts=T0), Refusal.EARLY)
    assert early.ledger == asked.ledger


def test_the_window_is_fifteen_minutes_exactly() -> None:
    assert rungs.WINDOW_MINUTES == 15


def test_an_expired_item_does_not_take_the_others_with_it() -> None:
    first = _ask(EMPTY, STATUS, id="c-1", ts=T0)
    second = _ask(first.ledger, DIFF, id="c-2", ts=T15)
    late = _refused(_resolve(second.ledger, "#c-1 approve", ts=T16), Refusal.EXPIRED)
    assert late.ledger is not None
    assert [p.id for p in late.ledger.pending] == ["c-2"]
    assert isinstance(_resolve(late.ledger, "#c-2 approve", ts=T16), Approved)


# --- only the literal word always --------------------------------------------------------------


def test_only_the_literal_word_always_makes_a_standing_rule() -> None:
    asked = _ask(EMPTY, STATUS)
    outcome = _resolve(asked.ledger, "#c-1 always")
    assert isinstance(outcome, Approved)
    assert outcome.scope is Scope.ALWAYS
    assert outcome.scope in grants.offered(STATUS)


KEEN = [
    "#c-1 Always",
    "#c-1 ALWAYS",
    "#c-1 always ",
    " #c-1 always",
    "#c-1  always",
    "#c-1\talways",
    "#c-1 always\n",
    "#c-1 always\r\n",
    "#c-1 always!",
    "#c-1 always.",
    "#c-1 always always",
    "#c-1 approve always",
    "#c-1 always, please",
    "#c-1 alwáys",
    "#c-1 a\u200blways",
    "#c-1 \u0430lways",  # Cyrillic small a, which prints as a Latin one
    "#c-1 always\u00a0",
    "#c-1always",
    "# c-1 always",
    "#C-1 always",
    "c-1 always",
    "always",
    "yes, always!",
    "yes please, do it always from now on",
    "sure, approve",
    "approve #c-1",
    "#c-1 approve, and always",
    "#c-1 yes",
    "#c-1 ok",
    "#c-1 y",
    "#c-1 approved",
    "#c-1 Approve",
    "#c-1 APPROVE",
    "#c-1 approve ",
    "#c-1 no.",
    "#c-1 No",
    "#c-1 nope",
    "#c-1",
    "#c-1 ",
    "",
    "#",
    "#c-1 always #c-1 always",
    "#c-2 always",
]


@pytest.mark.parametrize("text", KEEN)
def test_a_reply_that_merely_sounds_keen_creates_nothing(text: str) -> None:
    """Exact match only. Every variant here is refused, nothing is settled, and the item is
    still pending for the one reply that does match."""
    asked = _ask(EMPTY, STATUS)
    outcome = _resolve(asked.ledger, text)
    assert isinstance(outcome, Refused), text
    assert outcome.kind in (Refusal.NOT_A_REPLY, Refusal.UNKNOWN), (text, outcome.kind)
    assert outcome.ledger == asked.ledger
    if len(text) > len("always"):
        assert text not in outcome.reason
    still = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(still, Approved)


WELL_FORMED_FOR_ANOTHER_ID = {"#C-1 always", "#c-2 always"}


@pytest.mark.parametrize("text", KEEN)
def test_parse_is_exact(text: str) -> None:
    """Of every keen variant, only the two that are a well-formed reply to *another* id
    parse at all, and they resolve to nothing because no such id exists."""
    parsed = parse(text)
    if text in WELL_FORMED_FOR_ANOTHER_ID:
        assert parsed is not None and parsed.word == rungs.ALWAYS and parsed.id != "c-1"
    else:
        assert parsed is None


def test_parse_accepts_exactly_three_words() -> None:
    assert parse("#c-1 approve") == Reply(id="c-1", word=rungs.APPROVE)
    assert parse("#c-1 always") == Reply(id="c-1", word=rungs.ALWAYS)
    assert parse("#c-1 no") == Reply(id="c-1", word=rungs.NO)
    assert parse("#toolu_01AbC.x:y approve") == Reply(id="toolu_01AbC.x:y", word=rungs.APPROVE)
    assert parse("#c/1 approve") is None
    assert parse("#-c approve") is None
    assert parse("#" + "a" * 129 + " approve") is None
    assert rungs.WORDS == (rungs.APPROVE, rungs.ALWAYS, rungs.NO)


def test_parse_takes_only_a_str() -> None:
    with pytest.raises(TypeError):
        parse(b"#c-1 approve")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _resolve(EMPTY, 7)  # type: ignore[arg-type]


def test_an_unknown_id_is_refused_and_named_as_unknown_not_as_the_id() -> None:
    asked = _ask(EMPTY, STATUS)
    unknown = _refused(_resolve(asked.ledger, "#c-9 approve"), Refusal.UNKNOWN)
    assert "c-9" not in unknown.reason
    assert unknown.ledger == asked.ledger


def test_always_is_not_offered_for_a_destructive_call_and_is_refused_if_said() -> None:
    """D116: for an irreversible action the option is not offered at all, and the reply is
    refused even so. The item stays pending for ``approve`` or ``no``."""
    asked = _ask(EMPTY, PRUNE)
    assert asked.pending.answers() == ("#c-1 approve", "#c-1 no")
    assert "#c-1 always" not in asked.pending.card()
    assert [b.label for b in asked.pending.buttons()] == ["#c-1 approve", "#c-1 no"]
    refused = _refused(_resolve(asked.ledger, "#c-1 always"), Refusal.NOT_OFFERED)
    assert refused.ledger == asked.ledger
    once = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(once, Approved)
    assert once.scope is Scope.REQUEST
    assert once.scope in grants.offered(PRUNE)


def test_the_scopes_handed_back_are_the_ones_grants_offers() -> None:
    """The protocol's two yeses are the first and last of D73's grantable scopes, so what
    ``Approved.scope`` says can go straight to ``grants.grant``."""
    assert rungs.SCOPE_FOR[rungs.APPROVE] is Scope.REQUEST
    assert rungs.SCOPE_FOR[rungs.ALWAYS] is Scope.ALWAYS
    assert rungs.NO not in rungs.SCOPE_FOR
    assert set(rungs.SCOPE_FOR.values()) <= set(grants.GRANTABLE)


def test_an_approved_always_goes_into_grants_as_the_same_call(root: Path) -> None:
    """End to end with #99: the reply makes a standing rule for the exact call and nothing
    wider, and a near miss is not covered."""
    asked = _ask(EMPTY, STATUS)
    outcome = _resolve(asked.ledger, "#c-1 always")
    assert isinstance(outcome, Approved)
    live = grants.Live.start(SESSION, REQUEST)
    on = {grants.GRANTS_SWITCH: grants.SWITCH_ON}
    granted = grants.grant(
        live, outcome.call, outcome.scope, by=ORDER, call_id="c-1", root=root, ts=T5, environ=on
    )
    assert granted.rule is not None and not granted.degraded
    later = grants.Live.start("s-bbbb", "r-9")
    covered = grants.check(later, STATUS, call_id="c-7", root=root, principal=P, environ=on)
    assert isinstance(covered, grants.Allowed) and covered.scope is Scope.ALWAYS
    near = grants.check(later, DIFF, call_id="c-7", root=root, principal=P, environ=on)
    assert isinstance(near, grants.Refused)


# --- buttons -----------------------------------------------------------------------------------


def test_a_buttons_payload_is_exactly_its_label() -> None:
    asked = _ask(EMPTY, STATUS)
    buttons = asked.pending.buttons()
    assert [b.label for b in buttons] == list(asked.pending.answers())
    for button in buttons:
        assert button.payload == button.label
    outcome = _resolve(asked.ledger, buttons[0])
    assert isinstance(outcome, Approved) and outcome.scope is Scope.REQUEST


def test_a_button_whose_payload_differs_from_its_label_creates_nothing() -> None:
    asked = _ask(EMPTY, STATUS)
    for label, payload in (
        ("#c-1 approve", "#c-1 always"),
        ("#c-1 no", "#c-1 approve"),
        ("Approve", "#c-1 approve"),
        ("#c-1 approve", "#c-1 approve "),
        ("#c-1 approve", "#c-1 approve\n"),
        ("#c-1 always", "#c-1 Always"),
    ):
        refused = _refused(
            _resolve(asked.ledger, Button(label=label, payload=payload)), Refusal.BUTTON
        )
        assert refused.ledger == asked.ledger
        assert payload not in refused.reason
    still = _resolve(asked.ledger, "#c-1 approve")
    assert isinstance(still, Approved)


def test_a_button_is_two_strings() -> None:
    with pytest.raises(TypeError):
        Button(label="#c-1 approve", payload=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Button(label=1, payload="#c-1 approve")  # type: ignore[arg-type]


# --- the inbox ---------------------------------------------------------------------------------


def test_an_unattended_ask_is_an_inbox_approval_with_the_id_as_the_item() -> None:
    """The inbox resolves an item once, first answer wins (rule 2), so the replay rule holds
    there too. The body is the card: it names the exact command."""
    asked = _ask(EMPTY, STATUS)
    event = to_ask(asked.pending, unattended=True)
    assert event.kind is AskKind.APPROVAL
    assert event.session == SESSION and event.tool_call_id == "c-1"
    assert event.body == asked.pending.card(via_inbox=True)
    assert event.choices == []
    assert event.unattended is True
    assert event.ts == T0 and event.actor == "kernel"
    assert "git status" in event.body


def test_an_inbox_answer_can_approve_once_or_deny_and_never_always() -> None:
    """An inbox approval carries no words, so the reply it stands for is ``approve`` or
    ``no``. Nothing that arrives through the inbox is the literal word ``always``."""
    asked = _ask(EMPTY, STATUS)
    assert reply_for(asked.pending, AnswerOutcome.APPROVED) == "#c-1 approve"
    assert reply_for(asked.pending, AnswerOutcome.DENIED) == "#c-1 no"
    for outcome in AnswerOutcome:
        text = reply_for(asked.pending, outcome)
        if outcome in (AnswerOutcome.APPROVED, AnswerOutcome.DENIED):
            assert text is not None
            resolved = _resolve(asked.ledger, text)
            assert isinstance(resolved, Approved | Denied)
            if isinstance(resolved, Approved):
                assert resolved.scope is Scope.REQUEST
        else:
            assert text is None


# --- rung 4: the envelope ----------------------------------------------------------------------


def test_an_envelope_is_a_list_of_standing_rules() -> None:
    envelope = Envelope.parse(
        "shell: git status\nshell: git diff*\n\n# a comment\ncalendar.read: family*\n"
    )
    assert [(s.tool, s.rule.pattern) for s in envelope.rules] == [
        ("shell", "git status"),
        ("shell", "git diff*"),
        ("calendar.read", "family*"),
    ]
    assert Standing.parse("shell: git diff*") == Standing("shell", matching.Rule("git diff*"))
    assert envelope.for_tool("shell") == (matching.Rule("git status"), matching.Rule("git diff*"))
    assert envelope.for_tool("mail.send") == ()


@pytest.mark.parametrize(
    "line",
    [
        "shell: *",
        "shell:*",
        "shell: **",
        "shell:  *",
        "shell: * ",
        "shell: ",
        "shell:",
        "*: git status",
        "*: *",
        "shell: *git",
        "shell: g*t status",
        "shell: sudo*",
        "shell: xargs rm*",
        "shell: git status | ls",
        "shell: git status && ls",
        "shell: bash -c*",
        "shell: python3 -c*",
        "shell: find . -exec*",
        'shell: git commit -m "x"*',
        "shell: echo $HOME",
        "shell: ls > out",
        "git status",
        "shell git status",
        "shell:git status",
        "shell:  git status",
        " shell: git status",
        "she ll: git status",
        "shell: git status\t",
    ],
)
def test_a_bare_star_and_every_rule_the_matcher_refuses_cannot_be_an_envelope_line(
    line: str,
) -> None:
    with pytest.raises(ValueError):
        Standing.parse(line)
    with pytest.raises(ValueError, match="line 2"):
        Envelope.parse("shell: git status\n" + line + "\n")


def test_a_bare_star_is_refused_as_a_pattern() -> None:
    with pytest.raises(ValueError, match="bare"):
        Standing.parse("shell: *")
    with pytest.raises(ValueError, match="bare"):
        matching.Rule("*")


def test_an_envelope_with_one_bad_line_is_no_envelope() -> None:
    with pytest.raises(ValueError, match="line 3"):
        Envelope.parse("shell: git status\nshell: git diff*\nshell: *\n")


def test_a_bounded_call_is_justified_part_by_part() -> None:
    envelope = Envelope.parse("shell: git status\nshell: git diff*\n")
    both = bounded(
        envelope, Call(tool="shell", arguments="git status && git diff --stat"), environ=ON
    )
    assert isinstance(both, rungs.Bounded)
    assert [" ".join(j.words) for j in both.justified] == ["git status", "git diff --stat"]
    one_not = bounded(envelope, Call(tool="shell", arguments="git status && rm -rf /"), environ=ON)
    refused = _refused(one_not, Refusal.ENVELOPE)
    assert "rm" not in refused.reason and "part 2" in refused.reason


def test_a_rule_for_one_tool_does_not_cover_another() -> None:
    """Tool names are exact too: ``Shell`` is another tool, not a spelling of ``shell``."""
    envelope = Envelope.parse("shell: git status\n")
    _refused(
        bounded(envelope, Call(tool="Shell", arguments="git status"), environ=ON), Refusal.ENVELOPE
    )
    _refused(
        bounded(envelope, Call(tool="sh", arguments="git status"), environ=ON), Refusal.ENVELOPE
    )
    _refused(
        bounded(envelope, Call(tool="shell", arguments="git statu"), environ=ON), Refusal.ENVELOPE
    )
    _refused(bounded(Envelope.parse(""), STATUS, environ=ON), Refusal.ENVELOPE)
    assert isinstance(bounded(envelope, STATUS, environ=ON), rungs.Bounded)


def test_a_destructive_call_has_no_standing_yes(root: Path) -> None:
    """D116: however well the envelope matches, a destructive call is approved once, by a
    person, and never by a rule."""
    envelope = Envelope.parse("shell: rm -rf*\n")
    _refused(bounded(envelope, PRUNE, environ=ON), Refusal.DESTRUCTIVE)
    assert isinstance(
        bounded(envelope, Call(tool="shell", arguments=PRUNE.arguments), environ=ON),
        rungs.Bounded,
    )


def test_bounded_needs_matching_on_too() -> None:
    """Rung 4 splits and justifies through ``walle.matching``; while that is off, nothing is
    bounded, and the refusal says which switch."""
    envelope = Envelope.parse("shell: git status\n")
    only_rungs = {rungs.RUNGS_SWITCH: rungs.SWITCH_ON}
    off = _refused(bounded(envelope, STATUS, environ=only_rungs), Refusal.ENVELOPE)
    assert matching.MATCHING_SWITCH in off.reason


def test_the_envelope_file_is_read_from_under_the_floor(root: Path) -> None:
    folder = grants.grants_dir(root, P)
    folder.mkdir()
    (folder / rungs.ENVELOPE_FILE).write_text("shell: git status\nshell: git diff*\n")
    loaded = rungs.load_envelope(root, P)
    assert isinstance(loaded, Envelope)
    assert len(loaded.rules) == 2
    relative = f"principals/p-{P}/{grants.GRANTS_DIRNAME}/{rungs.ENVELOPE_FILE}"
    assert floors.hit(relative) is not None


def test_a_missing_envelope_is_empty_and_a_broken_one_is_a_problem(root: Path) -> None:
    folder = grants.grants_dir(root, P)
    assert rungs.load_envelope(root, P) == Envelope(())
    folder.mkdir()
    (folder / rungs.ENVELOPE_FILE).write_text("shell: git status\nshell: *\n")
    problem = rungs.load_envelope(root, P)
    assert isinstance(problem, str)
    assert "line 2" in problem and rungs.ENVELOPE_FILE in problem
    assert isinstance(bounded(problem, STATUS, environ=ON), Refused)


def test_an_envelope_that_is_a_link_or_a_folder_is_not_read(root: Path) -> None:
    folder = grants.grants_dir(root, P)
    folder.mkdir()
    elsewhere = root / "elsewhere.txt"
    elsewhere.write_text("shell: git status\n")
    (folder / rungs.ENVELOPE_FILE).symlink_to(elsewhere)
    linked = rungs.load_envelope(root, P)
    assert isinstance(linked, str) and "link" in linked
    (folder / rungs.ENVELOPE_FILE).unlink()
    (folder / rungs.ENVELOPE_FILE).mkdir()
    unreadable = rungs.load_envelope(root, P)
    assert isinstance(unreadable, str) and "IsADirectoryError" in unreadable


def test_nothing_in_the_module_writes_a_file() -> None:
    source = (Path(__file__).parent.parent / "src" / "walle" / "rungs.py").read_text()
    for forbidden in ("write_text", "write_bytes", '"a"', '"w"', '"ab"', '"wb"', "mkdir", "unlink"):
        assert forbidden not in source, forbidden
    assert "open(" not in source


# --- types and shapes --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "c 1", "c/1", "-c", "a" * 129])
def test_a_bad_id_is_refused_before_anything(bad: str) -> None:
    with pytest.raises(ValueError):
        ask(EMPTY, STATUS, id=bad, session=SESSION, request=REQUEST, ts=T0, environ=ON)


def test_a_bad_timestamp_or_type_is_the_callers_bug() -> None:
    with pytest.raises(ValueError):
        ask(EMPTY, STATUS, id="c-1", session=SESSION, request=REQUEST, ts="yesterday", environ=ON)
    with pytest.raises(TypeError):
        ask(EMPTY, "git status", id="c-1", session=SESSION, request=REQUEST, ts=T0, environ=ON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ask((), STATUS, id="c-1", session=SESSION, request=REQUEST, ts=T0, environ=ON)  # type: ignore[arg-type]
    asked = _ask(EMPTY, STATUS)
    with pytest.raises(ValueError):
        resolve(asked.ledger, "#c-1 approve", by=ORDER, ts="soon", environ=ON)
    with pytest.raises(TypeError):
        bounded(Envelope(()), "git status", environ=ON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bounded(("shell: git status",), STATUS, environ=ON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Pending(id="c-1", call="git status", asked_at=T0, session=SESSION, request=REQUEST)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Pending(id="c-1", call=STATUS, asked_at="2026-11-05", session=SESSION, request=REQUEST)


def test_a_ledger_holds_only_pendings_and_settleds() -> None:
    with pytest.raises(TypeError):
        Ledger(P, pending=("c-1",), settled=())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Ledger(P, pending=(), settled=("c-1",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Ledger(
            P,
            pending=(_ask(EMPTY, STATUS).pending,),
            settled=(rungs.Settled("c-1", rungs.How.APPROVED),),
        )


# --- the dormant rule --------------------------------------------------------------------------


def test_rungs_are_off_by_default(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's condition. Switch unset: nothing is asked, nothing resolves, nothing is
    bounded, with a matching envelope and a pending item in hand."""
    monkeypatch.delenv(rungs.RUNGS_SWITCH, raising=False)
    assert not rungs.enabled()
    assert not rungs.enabled({})
    with pytest.raises(RungsOffError, match=rungs.RUNGS_SWITCH):
        ask(EMPTY, STATUS, id="c-1", session=SESSION, request=REQUEST, ts=T0)
    with pytest.raises(RungsOffError):
        ask(EMPTY, STATUS, id="c-1", session=SESSION, request=REQUEST, ts=T0, environ={})
    asked = _ask(EMPTY, STATUS)
    off = resolve(asked.ledger, "#c-1 approve", by=ORDER, ts=T5)
    assert isinstance(off, Refused) and off.kind is Refusal.OFF
    assert "off" in off.reason and rungs.RUNGS_SWITCH in off.reason
    assert off.ledger == asked.ledger
    still_off = resolve(asked.ledger, "#c-1 always", by=ORDER, ts=T5, environ={})
    assert isinstance(still_off, Refused) and still_off.kind is Refusal.OFF
    envelope = Envelope.parse("shell: git status\n")
    matching_only = {matching.MATCHING_SWITCH: matching.SWITCH_ON}
    unbounded = bounded(envelope, STATUS, environ=matching_only)
    assert isinstance(unbounded, Refused) and unbounded.kind is Refusal.OFF
    assert isinstance(bounded(envelope, STATUS), Refused)


@pytest.mark.parametrize("value", ["1", "true", "ON", "On", "yes", " on", "on "])
def test_only_the_one_spelling_turns_rungs_on(value: str) -> None:
    assert not rungs.enabled({rungs.RUNGS_SWITCH: value})
    assert rungs.enabled({rungs.RUNGS_SWITCH: rungs.SWITCH_ON})


def test_while_off_nothing_is_parsed_or_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _ask(EMPTY, STATUS)

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("something was parsed or matched while off")

    monkeypatch.setattr(rungs, "parse", boom)
    monkeypatch.setattr(matching, "match", boom)
    off = resolve(asked.ledger, "#c-1 approve", by=ORDER, ts=T5, environ={})
    assert isinstance(off, Refused) and off.kind is Refusal.OFF
    unbounded = bounded(Envelope.parse("shell: git status\n"), STATUS, environ={})
    assert isinstance(unbounded, Refused) and unbounded.kind is Refusal.OFF


def test_the_module_starts_nothing_runs_nothing_and_reads_no_clock() -> None:
    """Like the grants and the matcher: ``ts`` is passed in, so no clock; no command is run;
    the environment is read in ``enabled`` and nowhere else."""
    source = (Path(__file__).parent.parent / "src" / "walle" / "rungs.py").read_text()
    for forbidden in (
        "now_instant",
        "datetime.now",
        "utcnow",
        "time.time",
        "time.monotonic",
        "subprocess",
        "os.system",
        "os.exec",
        "os.spawn",
        "os.popen",
        "shlex",
        "click",
        "threading",
        "import pty",
    ):
        assert forbidden not in source, forbidden
    assert source.count("os.environ") == 1
