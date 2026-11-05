"""Issue #118: the path checks agree, because they are one check (D32, D72, D109, D86).

Audit findings 1.1, 1.2 and 1.4 each found two modules answering the same question about a
path differently. Each reproduction below was a pass on ``main`` at 834c6d8 and is a refusal
now, and each seam has a test that drives the real modules together so they cannot drift
apart again:

  1.1  ``missions.admit`` and ``manifest.covers_path`` admitted an absolute write that
       ``reviewer.review`` blocked, and admitted ``..`` as "missions are off". Now every one
       of them asks ``floors.hit``, which answers ``unanswerable`` for both, before the switch.
  1.2  ``floors.floored`` let a linked ``principals/p-<name>`` through as a hop *above* a
       floor, then lost the trail on the other disk. Now a link above a floor path is refused,
       the same answer ``profiles.Profile`` gives (hard constraint 6: profiles are physical).
  1.4  ``grants`` and ``rungs.load_envelope`` checked only the leaf for a link, so a linked
       ``grants/`` or ``p-<name>`` read, and wrote, another principal's rule files. Now they
       open through ``profiles.Profile.open(plain=True)``, which follows no link at all.

Nothing is written on a refusal, and every refusal holds with every switch off: the floor and
the profile have no switch, and the gates refuse before they read theirs.

Every root is a ``tmp_path``; every principal, file and link here is made up for the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from walle import grants, rungs
from walle.events import Mission, Node
from walle.floors import Kind, Op, floored, hit, segments
from walle.manifest import Touch, covers_path
from walle.missions import MISSIONS_SWITCH, SWITCH_ON, admit, fold_missions
from walle.paths import principal_dir
from walle.profiles import CrossPrincipalError, Profile, Role, bind
from walle.reader import Record
from walle.reviewer import REVIEWER_SWITCH, Action, Context, FakeReviewer, Standing, review
from walle.speakers import Order, Speaker

TS = "2026-11-05T10:00:00+00:00"
ADA = Order("ada", Speaker("ada", Role.OPERATOR))
"""Ada speaking as the operator: the authority a grant is given by (issue #119)."""
EVERYTHING_ON = {
    MISSIONS_SWITCH: SWITCH_ON,
    REVIEWER_SWITCH: SWITCH_ON,
    grants.GRANTS_SWITCH: grants.SWITCH_ON,
    rungs.RUNGS_SWITCH: rungs.SWITCH_ON,
    "WALLE_MATCHING": "on",
}
EVERYTHING_OFF: dict[str, str] = {}

UNANSWERABLE = (
    "/",
    "/principals/p-sam/hot/rules.md",
    "/principals/p-sam/hot",
    "..",
    "../principals/p-sam/hot/rules.md",
    "principals/../../x",
    "a/\x00b",
)
"""Paths the floor cannot answer about by name: absolute, climbing out, or not a path."""


def _mission(*writes: str) -> tuple[Record, ...]:
    event = Mission(
        seq=1,
        ts=TS,
        actor="user",
        title="test",
        nodes=[Node(id="step1", title="step 1", writes=list(writes))],
    )
    return (Record(ref="d-mbp:1", seq=1, event=event, line=1),)


# --- 1.1: one answer for a path nobody can answer by name -----------------------------------


@pytest.mark.parametrize("path", UNANSWERABLE)
def test_1_1_the_floor_answers_unanswerable_not_none(path: str) -> None:
    assert segments(path) is None
    found = hit(path)
    assert found is not None and found.how == "unanswerable"
    assert "D72" in found.reason(Kind.WRITE, path) and "D86" in found.reason(Kind.WRITE, path)


@pytest.mark.parametrize("declared", ["/", "/principals/p-sam/hot", "..", "../x"])
@pytest.mark.parametrize("touched", ["/principals/p-sam/hot/rules.md", "principals/p-sam/x", "/"])
def test_1_1_an_unanswerable_declaration_covers_nothing(declared: str, touched: str) -> None:
    assert covers_path(declared, touched) is False


@pytest.mark.parametrize("environ", [EVERYTHING_OFF, EVERYTHING_ON, {MISSIONS_SWITCH: "1"}])
@pytest.mark.parametrize("path", UNANSWERABLE)
def test_1_1_the_gate_and_the_reviewer_refuse_the_same_write_for_the_same_reason(
    path: str, environ: dict[str, str]
) -> None:
    """The seam: the reproduction, with the node declaring ``/`` so the manifest alone would
    have let it through. ``admit`` refuses before the switch, with the floor's sentence, and
    ``review`` blocks with the same sentence, in every standing, calling no model."""
    state = fold_missions(_mission("/")).mission("d-mbp:1")
    touch = Touch(writes=(path,))
    found = hit(path)
    assert found is not None
    refusal = admit(state, "step1", touch, environ=environ)
    assert refusal is not None and refusal.reason == found.reason(Kind.WRITE, path)
    assert "missions are off" not in refusal.reason
    model = FakeReviewer()
    for standing in Standing:
        verdict = review(
            Action("file.write", "{}", touch=touch), Context("req"), model, standing=standing
        )
        assert verdict.decision == "blocked"
        assert verdict.reason.startswith(refusal.reason)
    assert model.prompts == []


def test_1_1_a_root_relative_write_the_node_declared_is_still_admitted() -> None:
    """The fix narrows nothing legitimate: a plain declared write goes, on."""
    state = fold_missions(_mission("principals/p-sam/plans")).mission("d-mbp:1")
    assert (
        admit(state, "step1", Touch(writes=("principals/p-sam/plans/week.md",)), EVERYTHING_ON)
        is None
    )


# --- 1.2: a link above a floor path is refused, as the profile refuses it -------------------


@pytest.fixture
def linked_principal(tmp_path: Path) -> Path:
    """The reproduction's layout: ``principals/p-sam`` is a link to another disk, where
    ``notes`` links to ``hot``; ``scratch/up`` links to ``..``."""
    root, disk2 = tmp_path / "root", tmp_path / "disk2" / "p-sam"
    (root / "principals").mkdir(parents=True)
    (disk2 / "hot").mkdir(parents=True)
    (disk2 / "grants").mkdir()
    os.symlink(disk2, root / "principals" / "p-sam")
    os.symlink("hot", disk2 / "notes")
    (root / "scratch").mkdir()
    os.symlink("..", root / "scratch" / "up")
    return root


@pytest.mark.parametrize(
    "op",
    [
        Op(Kind.WRITE, "principals/p-sam/notes/rules.md"),
        Op(Kind.WRITE, "scratch/up/principals/p-sam/hot/rules.md"),
        Op(Kind.WRITE, "scratch/up/principals/p-sam/grants/config.jsonl"),
        Op(Kind.DELETE, "scratch/up/principals/p-sam"),
        Op(Kind.WRITE, "principals/p-sam/log/d-mbp.jsonl"),
    ],
)
def test_1_2_a_write_through_a_linked_principal_folder_is_floored(
    linked_principal: Path, op: Op
) -> None:
    found = floored(linked_principal, op)
    assert found is not None, op
    assert found.protected.startswith("principals/p-sam/") and "D72" in found.reason
    assert found.resolved is not None  # the link was seen on the way, not found by name


def test_1_2_the_floor_and_the_profile_give_the_same_answer_to_a_linked_folder(
    linked_principal: Path,
) -> None:
    """The seam: ``Profile`` has always refused a linked profile folder (hard constraint 6);
    the floor now does too, for the very path the profile refuses."""
    profile = Profile(bind(linked_principal, "sam"))
    with pytest.raises(CrossPrincipalError, match="link"):
        profile.path("log/d-mbp.jsonl")
    assert floored(linked_principal, Op(Kind.WRITE, "principals/p-sam/log/d-mbp.jsonl")) is not None
    # and a linked ``principals/`` is refused by both as well
    other = linked_principal.parent / "other"
    (other / "elsewhere").mkdir(parents=True)
    os.symlink(other / "elsewhere", other / "principals")
    with pytest.raises(CrossPrincipalError, match="principals/"):
        Profile(bind(other, "sam")).path("log/d-mbp.jsonl")
    assert floored(other, Op(Kind.WRITE, "principals/p-sam/log/d-mbp.jsonl")) is not None


def test_1_2_a_real_principal_folder_is_still_not_floored(tmp_path: Path) -> None:
    """A principal folder that is a folder: the log is writable, as before."""
    root = tmp_path / "root"
    (principal_dir(root, "sam") / "log").mkdir(parents=True)
    assert floored(root, Op(Kind.WRITE, "principals/p-sam/log/d-mbp.jsonl")) is None
    assert floored(root, Op(Kind.DELETE, "principals/p-sam")) is not None


# --- 1.4: a linked grants/ or p-<name> reaches nobody else's rule files ---------------------


@pytest.fixture
def two_principals(tmp_path: Path) -> Path:
    """``sam`` has a config rule and an envelope; ``ada``'s ``grants/`` is a link to sam's."""
    sam_grants = grants.grants_dir(tmp_path, "sam")
    sam_grants.mkdir(parents=True)
    (sam_grants / grants.CONFIG_FILE).write_text(
        '{"v": 1, "tool": "shell", "arguments": "git status"}\n'
    )
    (sam_grants / rungs.ENVELOPE_FILE).write_text("shell: git *\n")
    ada = principal_dir(tmp_path, "ada")
    ada.mkdir(parents=True)
    os.symlink(sam_grants, ada / grants.GRANTS_DIRNAME)
    return tmp_path


def _untouched(root: Path) -> None:
    sam_grants = grants.grants_dir(root, "sam")
    assert sorted(p.name for p in sam_grants.iterdir()) == [
        grants.CONFIG_FILE,
        rungs.ENVELOPE_FILE,
    ]
    assert sorted(p.name for p in principal_dir(root, "ada").iterdir()) == [grants.GRANTS_DIRNAME]


@pytest.mark.parametrize("environ", [EVERYTHING_ON, EVERYTHING_OFF])
def test_1_4_a_linked_grants_folder_grants_nothing_and_is_written_nothing(
    two_principals: Path, environ: dict[str, str]
) -> None:
    """The reproduction. Off, nothing is read at all; on, the link is the problem named."""
    live = grants.Live.start("s1", "r1")
    call = grants.Call("shell", "git status")
    decision = grants.check(
        live, call, call_id="c1", root=two_principals, principal="ada", environ=environ
    )
    assert isinstance(decision, grants.Refused)
    if environ:
        assert any("link" in p for p in decision.problems)
        given = grants.grant(
            live,
            call,
            grants.Scope.ALWAYS,
            call_id="c1",
            root=two_principals,
            by=ADA,
            ts=TS,
            environ=environ,
        )
        assert given.degraded and given.scope is grants.Scope.SESSION
        assert "CrossPrincipalError" in given.note
    else:
        assert decision.problems == ()
        with pytest.raises(grants.GrantsOffError):
            grants.grant(
                live,
                call,
                grants.Scope.ALWAYS,
                call_id="c1",
                root=two_principals,
                by=ADA,
                ts=TS,
                environ=environ,
            )
    _untouched(two_principals)


def test_1_4_a_linked_grants_folder_is_no_envelope(two_principals: Path) -> None:
    loaded = rungs.load_envelope(two_principals, "ada")
    assert isinstance(loaded, str) and "link" in loaded
    call = grants.Call("shell", "git status")
    assert isinstance(rungs.bounded(loaded, call, environ=EVERYTHING_ON), rungs.Refused)
    _untouched(two_principals)


def test_1_4_a_linked_principal_folder_is_refused_the_same_way(tmp_path: Path) -> None:
    """``principals/p-ada`` itself a link to ``p-sam``: the same three refusals."""
    sam_grants = grants.grants_dir(tmp_path, "sam")
    sam_grants.mkdir(parents=True)
    (sam_grants / grants.CONFIG_FILE).write_text(
        '{"v": 1, "tool": "shell", "arguments": "git status"}\n'
    )
    os.symlink(principal_dir(tmp_path, "sam"), principal_dir(tmp_path, "ada"))
    live = grants.Live.start("s1", "r1")
    call = grants.Call("shell", "git status")
    decision = grants.check(
        live, call, call_id="c1", root=tmp_path, principal="ada", environ=EVERYTHING_ON
    )
    assert isinstance(decision, grants.Refused) and any("link" in p for p in decision.problems)
    given = grants.grant(
        live,
        call,
        grants.Scope.ALWAYS,
        call_id="c1",
        root=tmp_path,
        by=ADA,
        ts=TS,
        environ=EVERYTHING_ON,
    )
    assert given.degraded
    loaded = rungs.load_envelope(tmp_path, "ada")
    assert isinstance(loaded, str) and "link" in loaded
    assert sorted(p.name for p in sam_grants.iterdir()) == [grants.CONFIG_FILE]


def test_1_4_the_grants_and_the_profile_give_the_same_answer(two_principals: Path) -> None:
    """The seam: what ``Profile.path`` refuses, ``grants`` and ``rungs`` refuse; what it
    resolves, they read. Sam's own files are read through the same door."""
    ada, sam = Profile(bind(two_principals, "ada")), Profile(bind(two_principals, "sam"))
    with pytest.raises(CrossPrincipalError):
        ada.path(f"{grants.GRANTS_DIRNAME}/{grants.CONFIG_FILE}")
    assert sam.path(f"{grants.GRANTS_DIRNAME}/{grants.CONFIG_FILE}").is_file()
    call = grants.Call("shell", "git status")
    live = grants.Live.start("s1", "r1")
    for_sam = grants.check(
        live, call, call_id="c1", root=two_principals, principal="sam", environ=EVERYTHING_ON
    )
    assert isinstance(for_sam, grants.Allowed) and for_sam.scope is grants.Scope.CONFIG
    assert isinstance(rungs.load_envelope(two_principals, "sam"), rungs.Envelope)
    for_ada = grants.check(
        live, call, call_id="c1", root=two_principals, principal="ada", environ=EVERYTHING_ON
    )
    assert isinstance(for_ada, grants.Refused)


def test_1_4_a_rule_file_that_is_a_link_inside_the_folder_is_still_not_read(
    tmp_path: Path,
) -> None:
    """The stricter rule the grants always had survives the move to the profile's door: a
    link is not a rule file even when it points inside the principal's own folder."""
    folder = grants.grants_dir(tmp_path, "sam")
    folder.mkdir(parents=True)
    (folder / "other.jsonl").write_text('{"v": 1, "tool": "shell", "arguments": "git status"}\n')
    os.symlink("other.jsonl", folder / grants.CONFIG_FILE)
    decision = grants.check(
        grants.Live.start("s1", "r1"),
        grants.Call("shell", "git status"),
        call_id="c1",
        root=tmp_path,
        principal="sam",
        environ=EVERYTHING_ON,
    )
    assert isinstance(decision, grants.Refused) and any("link" in p for p in decision.problems)
