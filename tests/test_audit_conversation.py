"""Issue #122: the conversation rules, their switch, and the hygiene of two folds.

Each of the audit's three reproductions is a test here that failed on main at dfbeb90 and now
passes as a refusal that writes nothing. Beside each is a seam test that drives the real
modules together, so the two sides of each seam cannot drift apart again.

  3.1  Another principal speaking as an operator kept the owner's ``learned.md`` and
       ``user.md`` loaded: ``turn`` wrote their role and ``fold`` ranked it. Now a speaker who
       is not this folder's owner is the lowest tier for what this folder loads, whatever
       role they hold in their own folder. ``turn`` records no role for them — the role is a
       fact about their folder, not this one — and ``fold`` takes the principal and enforces
       the same rule over every utterance already in the log, so a rebuild cannot restore a
       tier from an old record.
  4.1  ``permit`` had no switch: with ``WALLE_GRANTS=on`` alone it read grant files while the
       conversation rules were off. Now every public entry point that takes an environment
       raises ``ConversationOffError`` first.
  5.3  ``digests.window_records`` handed a forgotten assertion to the composer, and
       ``conversation.fold`` counted a duplicated record twice. Both now go through
       ``resolver.live_events``: a window holds live events only, each once, and the fold
       sees each utterance once — a tombstone still neither raises a floor nor uncounts a
       turn. A record stamped exactly at the schedule's anchor is in the first window.

Every root is a ``tmp_path``. Every person, device, conversation and note is invented.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from walle import conversation, digests, grants, hot, profiles, resolver
from walle.conversation import (
    Conversation,
    ConversationOffError,
    Tier,
    Turn,
    files_for,
    fold,
    open_conversation,
    permit,
    turn,
)
from walle.events import (
    ACTOR_KERNEL,
    ACTOR_USER,
    Assertion,
    Observation,
    RoleName,
    Supersede,
    Utterance,
)
from walle.index import rebuild
from walle.paths import log_dir
from walle.profiles import Role
from walle.reader import Record, read_file
from walle.speakers import Request, Speaker
from walle.writer import StreamWriter

PACKAGE = Path(__file__).parent.parent / "src" / "walle"
P = "ada"
D = "mbp"
C = "chat-1"
TS = "2026-11-05T10:00:00.000000+00:00"
ON = {conversation.SWITCH: conversation.SWITCH_ON}
GRANTS_ON = {grants.GRANTS_SWITCH: grants.SWITCH_ON}
BOTH_ON = {**ON, **GRANTS_ON}

ADA = Speaker(P, Role.OPERATOR)
ADA_ADMIN = Speaker(P, Role.ADMIN)
SAM = Speaker("sam", Role.OPERATOR)
SAM_ADMIN = Speaker("sam", Role.ADMIN)
SAM_STAKEHOLDER = Speaker("sam", Role.STAKEHOLDER)
GUEST = Speaker("platform:U0G7", None)
OTHERS = (SAM, SAM_ADMIN, SAM_STAKEHOLDER, GUEST)

T0 = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


# --- builders -----------------------------------------------------------------------------


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    log_dir(root, P).mkdir(parents=True)
    return root


def _talk(root: Path, *speakers: Speaker, conversation_id: str = C) -> list[Turn]:
    convo = open_conversation(root, P, conversation_id, environ=ON)
    turns: list[Turn] = []
    for speaker in speakers:
        done = turn(convo, speaker, "hello", device=D, ts=TS, environ=ON)
        turns.append(done)
        convo = done.conversation
    return turns


def _records(root: Path, principal: str = P) -> list[Record]:
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _utterance(
    speaker: str, role: RoleName | None, conversation_id: str = C, ts: str = TS
) -> Utterance:
    return Utterance(
        ts=ts,
        actor=ACTOR_KERNEL,
        conversation=conversation_id,
        speaker=speaker,
        role=role,
        body="x",
    )


def _spell(at: datetime) -> str:
    return at.isoformat(timespec="microseconds")


def _grant_for_ada(root: Path, done: Turn, call: grants.Call) -> grants.Live:
    return grants.grant(
        grants.Live.start("s-1", "r-1"),
        call,
        grants.Scope.SESSION,
        by=done.authority,
        call_id="c-1",
        root=root,
        ts=TS,
        environ=BOTH_ON,
    ).live


# --- 3.1: another principal's role is theirs, not this folder's --------------------------------


def test_3_1_another_operator_speaking_here_drops_the_floor_to_the_bottom(tmp_path: Path) -> None:
    """The reproduction: Sam, an operator in his own folder, speaks in Ada's conversation."""
    root = _root(tmp_path)
    (sam,) = _talk(root, SAM)
    assert isinstance(sam.authority, Request)
    assert sam.conversation.floor is Tier.UNKNOWN
    assert sam.files == (hot.IDENTITY,)
    assert hot.LEARNED not in sam.files and hot.USER not in sam.files


def test_3_1_the_owner_s_context_unloads_when_another_principal_joins(tmp_path: Path) -> None:
    root = _root(tmp_path)
    ada, sam, ada_again = _talk(root, ADA, SAM, ADA)
    assert ada.files == hot.ORDER
    assert sam.restart, "the process has loaded the operator's context and must restart"
    assert sam.files == ada_again.files == files_for(Tier.UNKNOWN)
    assert ada_again.conversation.floor is Tier.UNKNOWN  # and it never recovers
    assert "lowest" in sam.frame
    assert "Their role here is operator" not in sam.frame, "the frame gives Sam no role here"
    assert "in their own folder" in sam.frame  # what he is elsewhere is said, and set aside


def test_3_1_the_record_carries_no_role_for_a_speaker_who_is_not_the_principal(
    tmp_path: Path,
) -> None:
    """Sam's role is a fact about Sam's folder (§7); Ada's log records none for him."""
    root = _root(tmp_path)
    _talk(root, ADA, SAM, SAM_STAKEHOLDER, GUEST, ADA_ADMIN)
    said = [r.event for r in _records(root) if isinstance(r.event, Utterance)]
    assert [(e.speaker, e.role) for e in said] == [
        (P, "operator"),
        ("sam", None),
        ("sam", None),
        ("platform:U0G7", None),
        (P, "admin"),
    ]


def test_3_1_an_old_utterance_with_a_foreign_role_cannot_restore_a_tier_on_rebuild(
    tmp_path: Path,
) -> None:
    """A log written before this fix holds ``speaker=sam, role=operator``. The fold ranks the
    speaker by who owns the folder, not by the role the record claims, from the JSONL and
    from the index alike."""
    root = _root(tmp_path)
    writer = StreamWriter(root, P, D)
    writer.append(_utterance(P, "operator"))
    writer.append(_utterance("sam", "operator"))
    writer.append(_utterance("sam", "admin", conversation_id="chat-2"))
    writer.append(_utterance("sam", "stakeholder", conversation_id="chat-3"))

    records = _records(root)
    want = {C: Tier.UNKNOWN, "chat-2": Tier.UNKNOWN, "chat-3": Tier.UNKNOWN}
    assert fold(records, principal=P) == want
    assert fold(rebuild(root, P).records, principal=P) == want
    assert open_conversation(root, P, C, environ=ON).floor is Tier.UNKNOWN
    # The same records, folded as if they were Sam's own folder, rank him by his role and
    # Ada at the bottom: the rule is about whose folder it is, not about Sam.
    assert fold(records, principal="sam") == {
        C: Tier.UNKNOWN,  # Ada spoke there too, and she is not Sam
        "chat-2": Tier.ADMIN,
        "chat-3": Tier.STAKEHOLDER,
    }


def test_3_1_the_fold_needs_to_know_whose_folder_it_folds() -> None:
    """There is no fold without a principal, so no caller can forget the rule."""
    assert inspect.signature(fold).parameters["principal"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "principal" in inspect.signature(conversation.floor).parameters
    with pytest.raises(TypeError):
        fold([])  # type: ignore[call-arg]


@pytest.mark.parametrize("other", OTHERS, ids=lambda s: f"{s.id}-{s.role or 'unknown'}")
def test_3_1_seam_nobody_but_the_owner_loads_the_owner_s_files(
    tmp_path: Path, other: Speaker
) -> None:
    """The seam with ``profiles``: the owner's decision of 2026-11-05 is that nobody reads
    another principal's folder, not even an admin. Here it is driven end to end: a turn is
    written, the floor is folded back from the log and from a rebuilt index, and the files
    the conversation would load are the ones a stranger gets."""
    root = _root(tmp_path)
    _, them = _talk(root, ADA, other)
    assert isinstance(them.authority, Request)
    assert them.conversation.floor is Tier.UNKNOWN
    assert them.files == files_for(Tier.UNKNOWN) == (hot.IDENTITY,)
    assert set(hot.ORDER) - set(them.files) == {hot.USER, hot.RULES, hot.LEARNED}
    assert them.restart
    for records in (_records(root), rebuild(root, P).records):
        assert conversation.floor(records, C, principal=P) is Tier.UNKNOWN
    # The role on the context is the principal's own (profiles); the conversation gives the
    # other speaker none here, so the two modules agree about who may read what.
    context = profiles.bind(root, P, device=D)
    assert context.principal == P
    assert them.speaker.id != context.principal


def test_3_1_the_principal_s_own_lower_roles_still_ratchet_by_role(tmp_path: Path) -> None:
    root = _root(tmp_path)
    a, b = _talk(root, ADA, ADA_ADMIN)
    assert a.conversation.floor is Tier.OPERATOR
    assert b.conversation.floor is Tier.ADMIN
    assert b.files == files_for(Tier.ADMIN)


# --- 4.1: every entry point honours the switch ------------------------------------------------


def _done(root: Path) -> Turn:
    convo = Conversation(root=root, principal=P, id=C, floor=Tier.OPERATOR, turns=1)
    return Turn(convo, ADA, f"d-{D}:1", "", (), False, conversation.authority(P, ADA))


def test_4_1_permit_is_off_while_the_conversation_switch_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduction: grants are on, the conversation rules are not."""
    root = _root(tmp_path)
    done = _done(root)
    call = grants.Call("shell", "git status")
    live = _grant_for_ada(root, done, call)
    before = _tree(root)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("grants.check was reached while the conversation rules are off")

    monkeypatch.setattr(grants, "check", boom)
    assert conversation.enabled(GRANTS_ON) is False
    with pytest.raises(ConversationOffError):
        permit(done, call, live=live, call_id="c-1", environ=GRANTS_ON)
    assert _tree(root) == before
    monkeypatch.undo()
    # The one difference is the switch: with it on, the same call is allowed.
    assert isinstance(permit(done, call, live=live, call_id="c-1", environ=BOTH_ON), grants.Allowed)


def test_4_1_permit_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's dormant condition: nothing in the process environment, nothing happens."""
    monkeypatch.delenv(conversation.SWITCH, raising=False)
    monkeypatch.setenv(grants.GRANTS_SWITCH, grants.SWITCH_ON)
    root = _root(tmp_path)
    done = _done(root)
    with pytest.raises(ConversationOffError):
        permit(
            done, grants.Call("shell", "git status"), live=grants.Live.start("s", "r"), call_id="c"
        )


def _entry_points() -> list[Any]:
    found = [
        getattr(conversation, name)
        for name in conversation.__all__
        if callable(fn := getattr(conversation, name))
        and not isinstance(fn, type)
        and "environ" in inspect.signature(fn).parameters
    ]
    assert {fn.__name__ for fn in found} >= {"open_conversation", "turn", "permit"}
    return found


def test_4_1_every_public_entry_point_checks_the_switch_before_anything_else() -> None:
    """By source: the first statement of each is the one check. By name: ``enabled`` is the
    only public function with an environment that is not an entry point, and it is the check."""
    tree = ast.parse((PACKAGE / "conversation.py").read_text(encoding="utf-8"))
    bodies = {n.name: n.body for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for fn in _entry_points():
        if fn is conversation.enabled:
            continue
        first = bodies[fn.__name__][0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            first = bodies[fn.__name__][1]  # past the docstring
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), fn.__name__
        assert ast.unparse(first.value) == "_require_on(environ)", fn.__name__


@pytest.mark.parametrize("environ", [{}, GRANTS_ON, {conversation.SWITCH: "ON"}], ids=str)
def test_4_1_every_public_entry_point_refuses_while_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> None:
    root = _root(tmp_path)
    done = _done(root)
    before = _tree(root)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("touched the log or a grant file while off")

    monkeypatch.setattr(conversation, "read_file", boom)
    monkeypatch.setattr(StreamWriter, "append", boom)
    monkeypatch.setattr(grants, "check", boom)
    with pytest.raises(ConversationOffError):
        open_conversation(root, P, C, environ=environ)
    with pytest.raises(ConversationOffError):
        turn(done.conversation, GUEST, "hi", device=D, ts=TS, environ=environ)
    with pytest.raises(ConversationOffError):
        permit(
            done,
            grants.Call("shell", "git status"),
            live=grants.Live.start("s", "r"),
            call_id="c",
            environ=environ,
        )
    assert _tree(root) == before


def test_4_1_seam_the_conversation_switch_gates_grants_and_grants_switch_gates_the_rest(
    tmp_path: Path,
) -> None:
    """The seam with ``grants``, driven with real grant files on disk: a grant Ada gave as
    the operator covers her call only when both switches are on; with the conversation off
    nothing is consulted, and with grants off ``grants.check`` refuses as it always did."""
    root = _root(tmp_path)
    (ada,) = _talk(root, ADA)
    call = grants.Call("shell", "git status")
    live = _grant_for_ada(root, ada, call)
    assert isinstance(permit(ada, call, live=live, call_id="c-1", environ=BOTH_ON), grants.Allowed)
    with pytest.raises(ConversationOffError):
        permit(ada, call, live=live, call_id="c-1", environ=GRANTS_ON)
    assert isinstance(permit(ada, call, live=live, call_id="c-1", environ=ON), grants.Refused)


# --- 5.3: two folds go through the resolver -----------------------------------------------------


def _assertion(seq: int, ts: str, body: str) -> Assertion:
    return Assertion(seq=seq, ts=ts, actor=ACTOR_USER, body=body, summary=body[:40])


def test_5_3_a_superseded_assertion_never_reaches_a_digest_window() -> None:
    """The reproduction: forgotten with ``walle forget``, still handed to the composer."""
    a = _assertion(1, "2026-11-05T10:01:00+00:00", "retracted secret")
    s = Supersede(
        seq=2, ts="2026-11-05T10:02:00+00:00", actor=ACTOR_USER, target="d-mbp:1", reason="x"
    )
    recs = [Record("d-mbp:1", 1, a, 1), Record("d-mbp:2", 2, s, 2)]
    win = digests.window_records(recs, T0, T0 + HOUR)
    assert not any(r.ref == "d-mbp:1" for r in win)
    # The tombstone itself is live and in the window: the composer may say a fact was
    # withdrawn, it may not say what it was.
    assert [r.ref for r in win] == ["d-mbp:2"]


def test_5_3_a_supersede_after_the_window_still_removes_what_it_targets() -> None:
    """Liveness is over the whole log, not the window: a fact forgotten after the slot
    closed is still forgotten by the time the digest is composed."""
    a = _assertion(1, "2026-11-05T10:01:00+00:00", "retracted later")
    s = Supersede(seq=2, ts="2026-11-05T12:00:00+00:00", actor=ACTOR_USER, target="d-mbp:1")
    recs = [Record("d-mbp:1", 1, a, 1), Record("d-mbp:2", 2, s, 2)]
    assert digests.window_records(recs, T0, T0 + HOUR) == ()


def test_5_3_a_window_is_the_resolver_s_liveness_not_a_second_one() -> None:
    a = _assertion(1, "2026-11-05T10:01:00+00:00", "kept")
    b = _assertion(2, "2026-11-05T10:02:00+00:00", "dropped")
    s = Supersede(seq=3, ts="2026-11-05T10:03:00+00:00", actor=ACTOR_USER, target="d-mbp:2")
    recs = [Record("d-mbp:1", 1, a, 1), Record("d-mbp:2", 2, b, 2), Record("d-mbp:3", 3, s, 3)]
    _, live = resolver.live_events(recs)
    win = digests.window_records(recs, T0, T0 + HOUR)
    assert {r.ref for r in win} == {ref for ref, is_live in live.items() if is_live}


def test_5_3_duplicate_and_conflicting_records_in_a_window() -> None:
    a = _assertion(1, "2026-11-05T10:01:00+00:00", "once")
    rec = Record("d-mbp:1", 1, a, 1)
    assert len(digests.window_records([rec, rec], T0, T0 + HOUR)) == 1
    other = Record("d-mbp:1", 1, _assertion(1, "2026-11-05T10:01:00+00:00", "twice"), 1)
    with pytest.raises(resolver.ResolutionError):
        digests.window_records([rec, other], T0, T0 + HOUR)


def test_5_3_a_record_stamped_at_the_anchor_is_in_the_first_window(tmp_path: Path) -> None:
    """Slot 0 starts at the anchor, so a note stamped exactly then belongs to the first
    digest. Later windows start where the last run ended, exclusive, as before."""
    root = tmp_path / "root"
    log_dir(root, "the-hollies").mkdir(parents=True)
    anchor = T0
    schedule = digests.Schedule(recipient="household-inbox:synthetic", every=HOUR, anchor=anchor)
    writer = StreamWriter(root, "the-hollies", D)
    for at, text in ((anchor - timedelta(minutes=1), "before"), (anchor, "at the anchor")):
        writer.append(
            Observation(
                ts=_spell(at),
                actor=ACTOR_USER,
                source="synthetic",
                source_version="0",
                observed_at=_spell(at),
                body=text,
            )
        )
    seen: list[digests.Window] = []

    def compose(window: digests.Window) -> str:
        seen.append(window)
        return ""

    context = profiles.bind(root, "the-hollies", device=D)
    environ = {digests.DIGESTS_SWITCH: digests.SWITCH_ON}

    def send(recipient: str, body: str) -> None:
        pass

    first = digests.run(
        context, schedule, compose, send, now=anchor + timedelta(minutes=5), environ=environ
    )
    assert first.due
    assert [r.event.body for r in seen[0].records] == ["at the anchor"]  # type: ignore[union-attr]
    second = digests.run(
        context, schedule, compose, send, now=anchor + HOUR + timedelta(minutes=5), environ=environ
    )
    assert second.due
    # The first run's own digest is stamped at its ``until``; the next window starts after it.
    assert [r.event.body for r in seen[1].records] == []  # type: ignore[union-attr]


def test_5_3_duplicate_records_do_not_double_a_conversation_s_turns() -> None:
    """The reproduction: the same stream read twice."""
    u = Record("d-mbp:1", 1, _utterance(P, "operator"), 1)
    assert conversation._turns([u, u], C) == 1  # pyright: ignore[reportPrivateUsage]
    assert fold([u, u], principal=P) == {C: Tier.OPERATOR}


def test_5_3_one_ref_naming_two_utterances_is_a_resolution_error() -> None:
    one = Record("d-mbp:1", 1, _utterance(P, "operator"), 1)
    two = Record("d-mbp:1", 1, _utterance(P, "admin"), 1)
    with pytest.raises(resolver.ResolutionError):
        fold([one, two], principal=P)
    with pytest.raises(resolver.ResolutionError):
        conversation._turns([one, two], C)  # pyright: ignore[reportPrivateUsage]


def test_5_3_a_tombstone_neither_raises_a_floor_nor_uncounts_a_turn(tmp_path: Path) -> None:
    """Deduplication is the resolver's; liveness is deliberately not applied. A supersede is
    an event the agent can append, and nothing in the log may raise a floor (D61, D117)."""
    root = _root(tmp_path)
    _, guest = _talk(root, ADA, GUEST)
    assert guest.conversation.floor is Tier.UNKNOWN
    StreamWriter(root, P, D).append(
        Supersede(ts=TS, actor=ACTOR_USER, target=guest.ref, reason="synthetic: try to forget")
    )
    after = open_conversation(root, P, C, environ=ON)
    assert after.floor is Tier.UNKNOWN
    assert after.turns == 2


def test_5_3_seam_a_forgotten_note_is_not_in_the_digest_the_run_composes(tmp_path: Path) -> None:
    """The seam between ``digests`` and ``resolver``, driven through a real run: the Hollies
    log a note, forget it, and the composer the run calls never sees it."""
    root = tmp_path / "root"
    household = "the-hollies"
    log_dir(root, household).mkdir(parents=True)
    writer = StreamWriter(root, household, D)
    at = T0 + timedelta(minutes=10)
    noted = writer.append(
        Observation(
            ts=_spell(at),
            actor=ACTOR_USER,
            source="synthetic",
            source_version="0",
            observed_at=_spell(at),
            body="synthetic: a note later forgotten",
        )
    )
    writer.append(
        Observation(
            ts=_spell(at + timedelta(minutes=1)),
            actor=ACTOR_USER,
            source="synthetic",
            source_version="0",
            observed_at=_spell(at + timedelta(minutes=1)),
            body="synthetic: a note that stays",
        )
    )
    writer.append(
        Supersede(
            ts=_spell(at + timedelta(minutes=2)), actor=ACTOR_USER, target=f"d-{D}:{noted.seq}"
        )
    )
    seen: list[digests.Window] = []

    def compose(window: digests.Window) -> str:
        seen.append(window)
        return "\n".join(r.event.body for r in window.records if isinstance(r.event, Observation))

    sent: list[tuple[str, str]] = []
    run = digests.run(
        profiles.bind(root, household, device=D),
        digests.Schedule(recipient="household-inbox:synthetic", every=HOUR, anchor=T0),
        compose,
        lambda recipient, body: sent.append((recipient, body)),
        now=T0 + timedelta(minutes=30),
        environ={digests.DIGESTS_SWITCH: digests.SWITCH_ON},
    )
    assert run.sent
    (window,) = seen
    bodies = [r.event.body for r in window.records if isinstance(r.event, Observation)]
    assert bodies == ["synthetic: a note that stays"]
    assert "forgotten" not in sent[0][1]
    assert all(resolver.live_events(_records(root, household))[1][r.ref] for r in window.records)


def test_5_3_the_two_folds_import_the_resolver_s_seam_and_no_private_name() -> None:
    for name in ("conversation", "digests"):
        source = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        assert "live_events" in source, name
        assert "_by_ref" not in source and "_liveness" not in source, name
