"""Issue #130: container links, index links and the WIP cap (D6, D28, D32, D68, D72).

The second read-only audit's three findings, each a test that failed on main at 8af004b and
now passes as a refusal that writes nothing, or as the right result. Beside each are the
cases around it, and one seam test that drives the real modules together.

  1  ``_linked_floor`` walked a step's declared writes on the host, but the step runs in a
     container that sees the workspace at ``/workspace``. A link ``plans -> /workspace/kernel``
     lands outside the workspace on the host (so nothing was floored) and inside it in the
     container (so the write landed under ``kernel/``). Now a link met on the way to a
     declared write is trusted only when its target is relative and every step of the walk,
     and the landing, stays strictly inside the workspace — the one case where host and
     container agree on where the bytes go. Everything else is refused. And the workspace
     goes through ``executor.check_workspace`` before ``missions.lock`` is taken, so a
     relative or missing workspace is a ``ValueError`` with no mission and no step in the log.
  2  ``Profile.records`` resolved ``log/`` and every stream, and nothing else the index
     touches: ``index.sqlite`` as a link to another principal's index handed that principal's
     records to this one's digest and wrote this one's events into it; ``index.lock`` as a
     link created a file wherever it pointed. Now ``index.sqlite``, ``index.lock`` and the
     SQLite sidecars go through the same ``resolve`` as the streams, and each must be plain:
     a link at any of them, inside the folder or out, is refused before the index runs. The
     index itself opens its lock ``O_NOFOLLOW`` and refuses a linked ``index.sqlite``, so the
     callers that reach ``index.update`` without a ``Profile`` are held to the same rule.
  3  One ``add`` whose ``caused_by`` named eight workflows took one WIP slot and, through
     ``builder._automation``, authorised all eight. Now ``refusal`` — the one check the writes
     and the fold share — allows at most one workflow ref beside at most one retire ref, so
     such a line is reported, never applied, and none of its workflows runs.

Every root is a ``tmp_path``. Every person, device, path, command and sentence is invented.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
from pathlib import Path

import pytest

from test_builder import (
    ALL_ON,
    D,
    P,
    _calls,
    _lines,
    _workspace,
)
from test_builder_chain import _fake, _run, _two_steps
from test_digests import T0, FakeSender, _tree
from test_digests import _run as _digest_run
from walle import automations, builder, digests, executor, index, profiles
from walle.automations import fold_automations
from walle.builder import Refusal, run_step
from walle.events import ACTOR_USER, Assertion, Automation, AutomationVerb, Mission, Step
from walle.index import load_records, update
from walle.paths import (
    INDEX_FILENAME,
    INDEX_LOCK_FILENAME,
    index_path,
    log_dir,
    missions_lock_path,
    principal_dir,
    stream_path,
)
from walle.profiles import CrossPrincipalError, Profile, bind
from walle.reader import Record
from walle.writer import StreamWriter

PACKAGE = Path(__file__).parent.parent / "src" / "walle"
A = "ada"  # a made-up household member
B = "sam"  # another
TS = "2026-11-05T10:00:00.000000+00:00"
CANARY = b"SECRET-NOT-REAL-sam-only-4410"
SIDECARS = ("-journal", "-wal", "-shm")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


def _mission_lines(root: Path) -> int:
    """How many missions and steps the log holds: what must not grow on a refusal."""
    return sum(1 for r in index.update(root, P).records if isinstance(r.event, Mission | Step))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- 1: a link in the workspace is read as the container will read it ------------------------


@pytest.mark.parametrize(
    "target",
    [
        f"{executor.MOUNT_POINT}/kernel",  # the reproduction: a floor, as the container sees it
        f"{executor.MOUNT_POINT}/notes",  # harmless on the host, elsewhere in the container
        "/etc",  # the host's; a different thing, or nothing, in the container
    ],
)
def test_1_a_link_with_an_absolute_target_is_refused_whatever_it_names(
    root: Path, tmp_path: Path, target: str
) -> None:
    """The reproduction: ``plans -> /workspace/kernel``. On the host that lands outside the
    workspace, so ``floors.floored`` saw no floor; in the container ``/workspace`` *is* the
    workspace and ``plans/meals.md`` is ``kernel/meals.md``. An absolute target names a
    container path the host cannot see, so no absolute link is trusted (D68, D72)."""
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "plans").symlink_to(target)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal), result
    assert "plans" in result.reason and "container" in result.reason
    assert calls == [] and _lines(root) == before


def test_1_an_absolute_target_inside_the_workspace_on_the_host_is_still_refused(
    root: Path, tmp_path: Path
) -> None:
    """``plans -> <host workspace>/notes`` lands inside the workspace on the host and nowhere
    in the container, where that host path does not exist. The rule is about what the
    container will see, not where the host says it lands."""
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "notes").mkdir()
    (ws / "plans").symlink_to(ws.resolve() / "notes")
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "container" in result.reason
    assert calls == [] and _lines(root) == before


@pytest.mark.parametrize("target", ["../elsewhere", "../ws/notes", "sub/../../ws/notes"])
def test_1_a_relative_link_that_leaves_the_workspace_is_refused_even_if_it_comes_back(
    root: Path, tmp_path: Path, target: str
) -> None:
    """``plans -> ../ws/notes`` lands back inside the workspace on the host; in the container
    ``/workspace/../ws/notes`` is ``/ws/notes``. Any hop above the workspace is a place where
    the two disagree, so the walk is refused the moment it climbs out (D86: fail closed)."""
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "notes").mkdir()
    (ws / "sub").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (ws / "plans").symlink_to(target)
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "container" in result.reason
    assert calls == [] and _lines(root) == before


@pytest.mark.parametrize("target", ["notes", "sub/notes", "./notes", "sub/../notes"])
def test_1_a_relative_link_that_stays_inside_the_workspace_still_runs(
    root: Path, tmp_path: Path, target: str
) -> None:
    """Host and container agree on a relative link that never leaves the workspace: the one
    shape of link a step may write through."""
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "notes").mkdir()
    (ws / "sub").mkdir()
    (ws / "sub" / "notes").mkdir()
    (ws / "plans").symlink_to(target)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, executor.Outcome), result
    assert len(calls) == 1


def test_1_a_link_two_hops_deep_is_judged_at_every_hop(root: Path, tmp_path: Path) -> None:
    """``plans -> notes`` is fine on its own; ``notes -> /workspace/kernel`` is not, and the
    walk through the first reaches the second."""
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "plans").symlink_to("notes")
    (ws / "notes").symlink_to(f"{executor.MOUNT_POINT}/kernel")
    before = _lines(root)
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "notes" in result.reason
    assert calls == [] and _lines(root) == before


def test_1_the_floor_checks_still_answer_first_for_a_relative_link_to_a_floor_name(
    root: Path, tmp_path: Path
) -> None:
    """``plans -> kernel`` is relative and stays inside the workspace, so the container rule
    lets it through — and the floor, which looks where it lands, refuses it as before."""
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "plans").symlink_to("kernel")
    result, calls = _run(root, tmp_path, kept, "write-plan")
    assert isinstance(result, Refusal) and "floored" in result.reason and calls == []


def test_1_a_relative_workspace_is_a_value_error_before_the_lock_and_the_log(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduction's second half: a relative workspace passed ``_overlaps`` (resolved
    against the working directory), the mission and ``running`` reached the log, and only
    then did ``executor.run`` raise for it. Now ``executor.check_workspace`` is asked before
    ``missions.lock`` exists, so the caller's mistake leaves nothing behind."""
    kept = _two_steps(root)
    _workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = _lines(root)
    lock = missions_lock_path(root, P)
    assert not lock.exists()
    docker, record = _fake(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        run_step(root, P, D, kept, "write-plan", Path("ws"), environ=ALL_ON, docker=docker)
    assert _lines(root) == before and _mission_lines(root) == 0
    assert not lock.exists() and _calls(record) == []


@pytest.mark.parametrize("missing", ["nowhere", "ws/not-a-dir"])
def test_1_a_workspace_that_is_not_a_directory_is_refused_before_anything_is_appended(
    root: Path, tmp_path: Path, missing: str
) -> None:
    kept = _two_steps(root)
    ws = _workspace(tmp_path)
    (ws / "not-a-dir").write_text("a file, not a folder", encoding="utf-8")
    before = _lines(root)
    docker, record = _fake(tmp_path)
    with pytest.raises(ValueError, match="existing directory"):
        run_step(root, P, D, kept, "write-plan", tmp_path / missing, environ=ALL_ON, docker=docker)
    assert _lines(root) == before and _mission_lines(root) == 0
    assert not missions_lock_path(root, P).exists() and _calls(record) == []


def test_1_the_workspace_rule_is_the_executor_s_own(root: Path) -> None:
    """By source: ``run_step`` calls ``executor.check_workspace`` — the rule the mount is
    built from — and does so before ``missions_lock_path`` is named, so the two cannot
    disagree about what a workspace is and nothing is locked for one the executor refuses."""
    tree = ast.parse((PACKAGE / "builder.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_step")
    calls = [
        ast.unparse(n.func)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "executor.check_workspace" in calls
    assert calls.index("executor.check_workspace") < calls.index("paths.missions_lock_path")


def test_1_a_symlinked_workspace_is_judged_as_the_folder_it_mounts(
    root: Path, tmp_path: Path
) -> None:
    """``check_workspace`` resolves the workspace, and the mount is of what it points at; the
    link rule is then read against that folder, where a relative link stays inside."""
    kept = _two_steps(root)
    real = _workspace(tmp_path)
    (real / "notes").mkdir()
    (real / "plans").symlink_to("notes")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    docker, record = _fake(tmp_path)
    result = run_step(root, P, D, kept, "write-plan", alias, environ=ALL_ON, docker=docker)
    assert isinstance(result, executor.Outcome), result
    mounts = [a for call in _calls(record) for a in call if a.startswith("type=bind,source=")]
    assert mounts == [f"type=bind,source={real.resolve()},target={executor.MOUNT_POINT}"]


# --- 2: every file the index touches goes through the profile's resolve ------------------------


def _household(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for who in (A, B):
        log_dir(root, who).mkdir(parents=True)
    return root


def _remember(root: Path, who: str, body: str, device: str = D) -> str:
    writer = StreamWriter(root, who, device)
    written = writer.append(Assertion(ts=TS, actor=ACTOR_USER, body=body, summary=body[:120]))
    assert written.seq is not None
    return f"{writer.stream}:{written.seq}"


def _shared_head(root: Path) -> str:
    """Sam's log, with Ada's a byte-for-byte copy of its one stream — the state the auditor
    named, where the two logs' heads agree and an incremental build sees no disagreement.
    Sam's index is built. Returns Sam's one ref."""
    ref = _remember(root, B, "sam's private note WITHOUTFAIL")
    shutil.copyfile(stream_path(root, B, D), stream_path(root, A, D))
    update(root, B)
    return ref


def test_2_index_sqlite_linked_to_another_principal_s_index_is_refused_untouched(
    tmp_path: Path,
) -> None:
    """The reproduction: ``p-ada/index.sqlite -> ../p-sam/index.sqlite`` with matching stream
    heads. ``index.update(root, "ada")`` opened Sam's file through the link, found its
    tracking agreed with Ada's log, wrote Ada's new event into it and returned Sam's rows
    as Ada's records. Now ``records`` refuses before the index is opened, and Sam's index is
    byte-for-byte what it was (D28, D32, hard constraint 6)."""
    root = _household(tmp_path)
    _shared_head(root)
    private = _remember(root, A, "ada's private note SECRETNOTREAL")
    sam_index = index_path(root, B)
    before = _sha(sam_index)
    index_path(root, A).symlink_to(Path("..") / f"p-{B}" / INDEX_FILENAME)
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError, match=INDEX_FILENAME):
        profile.records()
    with pytest.raises(CrossPrincipalError):
        profile.resolution()
    assert _sha(sam_index) == before
    assert private not in {r.ref for r in load_records(root, B)}


def test_2_index_lock_linked_elsewhere_creates_nothing_there(tmp_path: Path) -> None:
    """``p-ada/index.lock -> ../p-sam/hot/planted.lock``, dangling: ``open(lock, "ab")`` made
    a file in Sam's hot tier. Now the lock is resolved with the rest, and nothing is made."""
    root = _household(tmp_path)
    _remember(root, A, "ada's note")
    (principal_dir(root, B) / "hot").mkdir()
    planted = principal_dir(root, B) / "hot" / "planted.lock"
    (principal_dir(root, A) / INDEX_LOCK_FILENAME).symlink_to(
        Path("..") / f"p-{B}" / "hot" / "planted.lock"
    )
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError, match=INDEX_LOCK_FILENAME):
        profile.records()
    assert not planted.exists() and not os.path.lexists(planted)
    assert not index_path(root, A).exists()


@pytest.mark.parametrize("name", [INDEX_FILENAME, INDEX_LOCK_FILENAME])
def test_2_a_linked_index_file_inside_the_folder_is_refused_too(tmp_path: Path, name: str) -> None:
    """A link that stays inside the folder is still a link where a plain file belongs:
    ``index.sqlite -> hot/index.sqlite`` would put the derived index where the rendered
    tier is read from (D109). Refused, and nothing is created at the target."""
    root = _household(tmp_path)
    _remember(root, A, "ada's note")
    hot = principal_dir(root, A) / "hot"
    hot.mkdir()
    (principal_dir(root, A) / name).symlink_to(Path("hot") / name)
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError, match="link"):
        profile.records()
    assert list(hot.iterdir()) == []


@pytest.mark.parametrize("suffix", SIDECARS)
def test_2_a_linked_sqlite_sidecar_is_refused_before_the_index_runs(
    tmp_path: Path, suffix: str
) -> None:
    """SQLite writes ``index.sqlite-journal``, ``-wal`` and ``-shm`` beside the file by name;
    a link at any of them is a write elsewhere. Resolved with the rest, each must be plain."""
    root = _household(tmp_path)
    _remember(root, A, "ada's note")
    target = principal_dir(root, B) / f"stolen{suffix}"
    (principal_dir(root, A) / f"{INDEX_FILENAME}{suffix}").symlink_to(
        Path("..") / f"p-{B}" / f"stolen{suffix}"
    )
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError, match=suffix):
        profile.records()
    assert not index_path(root, A).exists() and not target.exists()


def test_2_a_plain_profile_still_indexes_and_the_index_files_are_its_own(
    tmp_path: Path,
) -> None:
    root = _household(tmp_path)
    ref = _remember(root, A, "ada's note")
    profile = Profile(bind(root, A))
    assert {r.ref for r in profile.records()} == {ref}
    assert index_path(root, A).is_file() and not index_path(root, A).is_symlink()
    assert (principal_dir(root, A) / INDEX_LOCK_FILENAME).is_file()
    assert not index_path(root, B).exists()


def test_2_the_profile_resolves_every_name_the_index_writes(tmp_path: Path) -> None:
    """By source: the names ``Profile.records`` resolves are the index file, its lock and
    every sidecar ``index._remove`` deletes — one list, shared, so a sidecar added to one
    place is checked in the other."""
    assert set(profiles.INDEX_FILES) == {
        INDEX_FILENAME,
        INDEX_LOCK_FILENAME,
        *(f"{INDEX_FILENAME}{s}" for s in index.SIDECARS),
    }
    assert set(index.SIDECARS) == set(SIDECARS)


def test_2_seam_a_digest_run_through_a_linked_index_writes_nothing(tmp_path: Path) -> None:
    """``digests.run`` reads through ``Profile.records``. With Ada's ``index.sqlite`` linked
    to Sam's, the run is refused after ``digest.lock`` and before any read or write: no
    digest event, nothing sent, Sam's index unchanged, and the only new file is Ada's own
    lock."""
    root = _household(tmp_path)
    _shared_head(root)
    _remember(root, A, "ada's private note SECRETNOTREAL")
    index_path(root, A).symlink_to(Path("..") / f"p-{B}" / INDEX_FILENAME)
    before = _tree(root)
    sender = FakeSender()
    with pytest.raises(CrossPrincipalError):
        _digest_run(root, T0, send=sender, principal=A)
    after = _tree(root)
    new = set(after) - set(before)
    assert new <= {f"principals/p-{A}/{digests.DIGEST_LOCK_FILENAME}"}, new
    assert all(after[k] == before[k] for k in before)
    assert sender.sent == []


# --- 2: the index holds the same line for callers that do not come through a Profile ---------


def test_2_index_update_refuses_a_linked_lock_without_following_it(tmp_path: Path) -> None:
    """``index.lock`` opened ``O_NOFOLLOW``: a link there is an error naming it, and the
    target is not created."""
    root = _household(tmp_path)
    _remember(root, A, "ada's note")
    target = principal_dir(root, B) / "planted.lock"
    (principal_dir(root, A) / INDEX_LOCK_FILENAME).symlink_to(
        Path("..") / f"p-{B}" / "planted.lock"
    )
    with pytest.raises(ValueError, match=INDEX_LOCK_FILENAME):
        update(root, A)
    assert not target.exists() and not index_path(root, A).exists()
    with pytest.raises(ValueError, match=INDEX_LOCK_FILENAME):
        load_records(root, A)
    assert not target.exists()


def test_2_index_update_refuses_a_linked_index_file(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _shared_head(root)
    _remember(root, A, "ada's private note SECRETNOTREAL")
    sam_index = index_path(root, B)
    before = _sha(sam_index)
    index_path(root, A).symlink_to(Path("..") / f"p-{B}" / INDEX_FILENAME)
    with pytest.raises(ValueError, match=INDEX_FILENAME):
        update(root, A)
    assert _sha(sam_index) == before
    with pytest.raises(ValueError, match=INDEX_FILENAME):
        load_records(root, A)


def test_2_the_index_lock_is_opened_nofollow_everywhere(tmp_path: Path) -> None:
    """By source: no bare ``open(lock`` remains in ``index.py``; the lock is opened only
    through ``_open_lock``, and every set of flags handed to it carries ``O_NOFOLLOW``."""
    source = (PACKAGE / "index.py").read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w.])open\(lock", source)
    tree = ast.parse(source)
    constants = {
        t.id: ast.unparse(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    raw = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.open" and n.args
    ]
    assert [ast.unparse(c.args[0]) for c in raw] == ["lock"], "one os.open, inside _open_lock"
    opens = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "_open_lock"
    ]
    assert len(opens) >= 3  # the builder's, and the reader's two
    for call in opens:
        flags = ast.unparse(call.args[1])
        assert "O_NOFOLLOW" in constants.get(flags, flags), flags


# --- 3: one add binds one workflow ----------------------------------------------------------------


def _records_with_add(n_workflows: int, *, retires: int = 0) -> tuple[list[Record], str, list[str]]:
    """``n_workflows`` synthetic kept events, ``retires`` earlier adds each retired, then one
    add whose ``caused_by`` names every workflow and every retire. Returns the records, the
    add's ref and the workflow refs."""
    records: list[Record] = []
    seq = 0

    def put(event: Assertion | Automation) -> str:
        nonlocal seq
        seq += 1
        ref = f"d-{D}:{seq}"
        records.append(Record(ref, seq, event, seq))
        return ref

    workflows = [
        put(Assertion(seq=i + 1, ts=TS, actor=ACTOR_USER, body=f"plan {i}", summary=f"plan {i}"))
        for i in range(n_workflows)
    ]
    retire_refs = []
    for i in range(retires):
        added = put(
            Automation(ts=TS, actor=P, verb=AutomationVerb.ADD, name=f"old-{i}", title=f"old {i}")
        )
        retire_refs.append(
            put(Automation(ts=TS, actor=P, verb=AutomationVerb.RETIRE, target=added, note="swap"))
        )
    add = put(
        Automation(
            ts=TS,
            actor=P,
            verb=AutomationVerb.ADD,
            name="many",
            title="one slot, many workflows",
            caused_by=[*workflows, *retire_refs],
        )
    )
    return records, add, workflows


@pytest.mark.parametrize("count", [2, 8])
def test_3_an_add_naming_many_workflows_is_reported_and_authorises_none(count: int) -> None:
    """The reproduction: one ``add`` with eight workflow refs in ``caused_by`` took one WIP
    slot and ``builder._automation`` let every one of the eight run. Now the fold reports
    it, the register does not hold it, and no workflow it named is runnable (D6, hard
    constraint 9)."""
    records, add, workflows = _records_with_add(count)
    register = fold_automations(records)
    assert register.find("many") is None and register.entry(add) is None
    reasons = {u.ref: u.reason for u in register.unapplied}
    assert add in reasons and "one workflow" in reasons[add], reasons
    for ref in workflows:
        why = builder._automation(records, ref)
        assert why is not None and "not in the register" in why


def test_3_one_workflow_beside_its_swap_retire_still_applies() -> None:
    """The shape ``add(..., workflow=, retire=)`` writes: one kept workflow and the retire it
    swapped with. Applied, and the workflow is runnable."""
    records, add, workflows = _records_with_add(1, retires=1)
    register = fold_automations(records)
    entry = register.entry(add)
    assert entry is not None and entry.status == "active" and register.unapplied == ()
    assert builder._automation(records, workflows[0]) is None


def test_3_two_retires_beside_a_workflow_is_refused() -> None:
    """A swap retires one to make room for one; an add that names two retires as its cause
    claims two slots' worth of room for one entry."""
    records, add, _ = _records_with_add(1, retires=2)
    register = fold_automations(records)
    assert register.entry(add) is None
    reasons = {u.ref: u.reason for u in register.unapplied}
    assert add in reasons and "retire" in reasons[add]


def test_3_the_api_cannot_write_such_an_add_and_a_swap_still_can(tmp_path: Path) -> None:
    """``add`` takes one ``workflow`` and one ``retire`` by signature; the real swap path
    writes two refs and the fold applies them. The rule in ``refusal`` is for the line that
    reaches the log another way."""
    on = {automations.AUTOMATIONS_SWITCH: automations.SWITCH_ON}
    kept = _remember(tmp_path, P, "a kept plan")
    for i in range(automations.WIP_CAP):
        automations.add(tmp_path, P, D, name=f"a-{i}", title=f"a {i}", environ=on)
    entry = automations.add(
        tmp_path, P, D, name="swapped", title="swapped in", workflow=kept, retire="a-0", environ=on
    )
    assert len(entry.caused_by) == 2 and entry.caused_by[0] == kept
    register = fold_automations(index.update(tmp_path, P).records)
    assert register.find("swapped") is not None and register.unapplied == ()
    assert builder._automation(index.update(tmp_path, P).records, kept) is None


def test_3_the_rule_lives_in_refusal_the_one_check_both_callers_share() -> None:
    """By source: ``fold_automations`` and ``add`` both call ``refusal``, and nothing else in
    the module counts ``caused_by`` — rule 6 of the module docstring holds for this rule."""
    tree = ast.parse((PACKAGE / "automations.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    counting = [
        name
        for name, fn in fns.items()
        if any(
            isinstance(n, ast.Attribute) and n.attr == "caused_by"
            for n in ast.walk(fn)
            if not isinstance(n, ast.FunctionDef) or n is fn
        )
    ]
    assert "refusal" in counting
    assert set(counting) <= {"refusal", "_apply", "add"}


@pytest.mark.parametrize("suffix", SIDECARS)
def test_2_index_update_refuses_a_linked_sidecar_too(tmp_path: Path, suffix: str) -> None:
    """The index's own check covers what SQLite writes beside the file by name."""
    root = _household(tmp_path)
    _remember(root, A, "ada's note")
    target = principal_dir(root, B) / f"stolen{suffix}"
    (principal_dir(root, A) / f"{INDEX_FILENAME}{suffix}").symlink_to(
        Path("..") / f"p-{B}" / f"stolen{suffix}"
    )
    with pytest.raises(ValueError, match=suffix):
        update(root, A)
    assert not target.exists() and not index_path(root, A).exists()
