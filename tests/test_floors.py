"""Issue #69's verify criterion, and the cases around it (D72, D109).

  every floor path         each entry of the protected table — ``kernel/``, ``shared/``, and a
                           principal's ``grants/``, ``keys/`` and ``hot/`` — is unwritable: the
                           path itself, anything under it, and anything above it (renaming or
                           deleting a parent takes the protected file with it)
  in every mode            every kind of write — write, delete, both ends of a rename, both
                           ends of a link — and, through the gate, whether missions are off,
                           explicitly empty, or on: the refusal is the same sentence
  through symlinks         a link from outside the root into a protected path, a link inside
                           the root under an innocent name, a dangling link whose target would
                           be created inside a protected folder, a protected folder replaced by
                           a link pointing out, the root itself reached through a link, and a
                           link loop, which cannot be resolved and so is refused
  through renames          renaming a protected path away, renaming something onto a protected
                           name, renaming a parent of one, and a rename whose source is a link
  nothing can lift it      no switch, no environment, no field on a node, no parameter; the
                           sandbox profile's one writable folder is not floored and every floor
                           path is outside it
  off by default           the module has no command and never writes, and the gate still
                           refuses an ordinary, declared write while missions are off
  what passes              the log, a plan under the principal's folder, a path outside the
                           root, and names that merely share a prefix with a protected one

Every root here is a ``tmp_path``; every principal, file and link is made up for the test.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from typing import Any

import pytest

from test_constraints import MODEL_OR_NETWORK, forbidden_in_closure
from walle.events import ACTOR_KERNEL, ACTOR_USER, Mission, Node, Step, StepStatus, StepVerb
from walle.floors import PROTECTED, Floored, Hit, Kind, Op, floored, hit
from walle.manifest import Touch
from walle.missions import (
    MISSIONS_SWITCH,
    SWITCH_ON,
    MissionState,
    Refusal,
    admit,
    fold_missions,
)
from walle.paths import hot_dir, log_dir, principal_dir, stream_path
from walle.reader import Record
from walle.sandbox import srt_settings

TS = "2026-11-08T09:00:00.000000+00:00"
ON = {MISSIONS_SWITCH: SWITCH_ON}
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
FLOORS_SOURCE = PACKAGE / "floors.py"
P = "sam"  # a made-up principal

FLOOR_PATHS = (
    "kernel",
    "shared",
    f"principals/p-{P}/grants",
    f"principals/p-{P}/keys",
    f"principals/p-{P}/hot",
)
"""Every floor path, spelled for ``sam``. ``test_the_table_is_exactly_these`` pins the table."""

UNDER = {
    "kernel": "kernel/router.py",
    "shared": "shared/skills/grocer/SKILL.md",
    f"principals/p-{P}/grants": f"principals/p-{P}/grants/calendar.json",
    f"principals/p-{P}/keys": f"principals/p-{P}/keys/grocer.key",
    f"principals/p-{P}/hot": f"principals/p-{P}/hot/rules.md",
}

ABOVE = {
    "kernel": "",
    "shared": ".",
    f"principals/p-{P}/grants": "principals",
    f"principals/p-{P}/keys": f"principals/p-{P}",
    f"principals/p-{P}/hot": f"principals/p-{P}/",
}

HOT_FILES = ("identity.md", "user.md", "rules.md", "learned.md", "rendered.json")


def _ops(path: str, other: str = f"principals/p-{P}/scratch/x") -> list[Op]:
    """Every kind of write that reaches ``path``: as the path and, for the two-ended kinds,
    as either end."""
    return [
        Op(Kind.WRITE, path),
        Op(Kind.DELETE, path),
        Op(Kind.RENAME, path, other),
        Op(Kind.RENAME, other, path),
        Op(Kind.LINK, path, other),
        Op(Kind.LINK, other, path),
    ]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A synthetic tenant: the principal's log and hot tier, plus somewhere to scratch."""
    r = tmp_path / "walle"
    log_dir(r, P).mkdir(parents=True)
    stream_path(r, P, "mbp").write_text("")
    hot_dir(r, P).mkdir()
    for name in HOT_FILES:
        (hot_dir(r, P) / name).write_text("synthetic\n")
    (principal_dir(r, P) / "scratch").mkdir()
    (principal_dir(r, P) / "plans").mkdir()
    return r


# --- the table -----------------------------------------------------------------------------


def test_the_table_is_exactly_these() -> None:
    """D72 and D109 name the set; a reader can check it against this list. An entry that
    goes missing fails here before any write test gets to notice."""
    spelled = tuple("/".join(p.pattern) for p in PROTECTED)
    assert spelled == (
        "kernel",
        "shared",
        "principals/*/grants",
        "principals/*/keys",
        "principals/*/hot",
    )
    for entry in PROTECTED:
        assert entry.what and ("D" in entry.decisions or "constraint" in entry.decisions)


# --- by name: the pure check ---------------------------------------------------------------


@pytest.mark.parametrize("path", FLOOR_PATHS)
def test_every_floor_path_is_a_hit(path: str) -> None:
    assert hit(path) == Hit(path, "is")


@pytest.mark.parametrize("path", FLOOR_PATHS)
def test_everything_under_a_floor_path_is_a_hit(path: str) -> None:
    assert hit(UNDER[path]) == Hit(path, "under")
    assert hit(UNDER[path] + "/deeper/still") == Hit(path, "under")


@pytest.mark.parametrize("path", FLOOR_PATHS)
def test_everything_above_a_floor_path_is_a_hit(path: str) -> None:
    """Renaming or deleting a parent takes the protected file with it, so the parent is
    floored too — up to and including the root itself."""
    found = hit(ABOVE[path])
    assert found is not None and found.how == "above"


@pytest.mark.parametrize("name", HOT_FILES)
def test_each_hot_tier_file_is_under_the_hot_floor(name: str) -> None:
    """The four files the host loads and the manifest that vouches for them (D107)."""
    assert hit(f"principals/p-{P}/hot/{name}") == Hit(f"principals/p-{P}/hot", "under")


@pytest.mark.parametrize(
    "path",
    [
        f"principals/p-{P}/log/../hot/rules.md",
        f"./principals/p-{P}//hot/rules.md",
        f"principals/p-{P}/HOT/rules.md",
        f"principals/P-{P.upper()}/Keys/x",
        "Shared/skills/x",
        "KERNEL",
    ],
)
def test_a_floor_path_is_found_however_it_is_spelled(path: str) -> None:
    """``..`` and ``.`` are folded before matching, and case is ignored because the
    filesystems walle ships on fold it (macOS, Windows): on those, ``HOT/rules.md`` *is*
    ``hot/rules.md``, and on the others nothing legitimate lives at the other spelling."""
    assert hit(path) is not None


@pytest.mark.parametrize("other", ["any", "p-dev", "p-household", "not-a-principal"])
def test_every_principal_is_floored_not_only_this_one(other: str) -> None:
    """Hard constraint 6 from the write side: the agent for one principal may not touch
    another's grants either, and a folder that is not a principal gets no benefit of doubt."""
    assert hit(f"principals/{other}/keys/x") == Hit(f"principals/{other}/keys", "under")


@pytest.mark.parametrize(
    "path",
    [
        f"principals/p-{P}/log/d-mbp.jsonl",
        f"principals/p-{P}/log",
        f"principals/p-{P}/plans/week.md",
        f"principals/p-{P}/index.sqlite",
        f"principals/p-{P}/hotel/booking.md",
        f"principals/p-{P}/keystone",
        f"principals/p-{P}/grants-notes.md",
        "sharedx/y",
        "shared-recipes/soup.md",
        "kernel-notes.md",
        "notes/kernel/x",
    ],
)
def test_what_is_not_a_floor_path_is_not_a_hit(path: str) -> None:
    """The log is the agent's one write; a shared prefix is not containment; the names only
    count at the place in the tree where the layout puts them."""
    assert hit(path) is None


@pytest.mark.parametrize("path", ["../kernel/x", "../../shared", "/abs/principals/p-sam/hot"])
def test_by_name_cannot_tell_for_a_path_that_leaves_or_is_absolute(path: str) -> None:
    """Without a root there is no answer by name; the resolving check has one."""
    assert hit(path) is None


# --- on disk: every floor path in every mode -------------------------------------------------


@pytest.mark.parametrize("path", FLOOR_PATHS)
@pytest.mark.parametrize("kind", list(Kind))
def test_every_floor_path_is_refused_by_every_kind_of_write(
    root: Path, path: str, kind: Kind
) -> None:
    for op in _ops(path):
        if op.kind is not kind:
            continue
        found = floored(root, op)
        assert found is not None, op
        assert found.protected == path
        assert found.how in ("is", "under")
        assert path in found.reason and "D72" in found.reason


@pytest.mark.parametrize("path", FLOOR_PATHS)
def test_under_and_above_every_floor_path_is_refused_on_disk(root: Path, path: str) -> None:
    for op in _ops(UNDER[path]):
        assert floored(root, op) is not None, op
    for op in _ops(ABOVE[path]):
        found = floored(root, op)
        assert found is not None and found.how == "above", op


def test_the_refusal_names_the_end_of_the_rename_that_hit(root: Path) -> None:
    away = floored(root, Op(Kind.RENAME, f"principals/p-{P}/hot", f"principals/p-{P}/hot-old"))
    onto = floored(root, Op(Kind.RENAME, f"principals/p-{P}/scratch", f"principals/p-{P}/hot"))
    assert away is not None and away.path == f"principals/p-{P}/hot" and away.end == "path"
    assert onto is not None and onto.path == f"principals/p-{P}/hot" and onto.end == "to"
    assert away.reason.startswith(f"rename 'principals/p-{P}/hot'")
    assert onto.reason.startswith(f"rename to 'principals/p-{P}/hot'")


def test_renaming_a_parent_of_a_floor_path_is_refused(root: Path) -> None:
    found = floored(root, Op(Kind.RENAME, f"principals/p-{P}", f"principals/p-{P}-old"))
    assert found is not None and found.how == "above"
    assert floored(root, Op(Kind.RENAME, "principals", "people")) is not None
    assert floored(root, Op(Kind.DELETE, f"principals/p-{P}")) is not None


def test_a_hard_link_to_a_floor_path_is_refused_whichever_end_it_is(root: Path) -> None:
    """A second name for ``rules.md`` elsewhere would write through to it."""
    assert floored(
        root, Op(Kind.LINK, f"principals/p-{P}/scratch/r.md", UNDER[f"principals/p-{P}/hot"])
    )
    assert floored(root, Op(Kind.LINK, f"principals/p-{P}/keys/new", f"principals/p-{P}/scratch/k"))


def test_an_absolute_path_inside_the_root_is_checked_like_a_relative_one(root: Path) -> None:
    path = str(hot_dir(root, P) / "rules.md")
    found = floored(root, Op(Kind.WRITE, path))
    assert found == Floored(
        op=Op(Kind.WRITE, path),
        end="path",
        path=path,
        protected=f"principals/p-{P}/hot",
        how="under",
        resolved=None,
    )


# --- through symlinks ------------------------------------------------------------------------


def test_a_link_from_outside_the_root_into_a_floor_path_is_refused(
    root: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "r.md").symlink_to(hot_dir(root, P) / "rules.md")
    found = floored(root, Op(Kind.WRITE, str(elsewhere / "r.md")))
    assert found is not None
    assert found.protected == f"principals/p-{P}/hot" and found.how == "under"
    assert found.resolved == str((hot_dir(root, P) / "rules.md").resolve())
    assert "resolves to" in found.reason


def test_a_link_inside_the_root_under_an_innocent_name_is_refused(root: Path) -> None:
    (principal_dir(root, P) / "notes").symlink_to("hot")
    assert hit(f"principals/p-{P}/notes/rules.md") is None  # by name it looks fine
    found = floored(root, Op(Kind.WRITE, f"principals/p-{P}/notes/rules.md"))
    assert found is not None and found.protected == f"principals/p-{P}/hot"


def test_a_dangling_link_whose_target_would_land_in_a_floor_path_is_refused(root: Path) -> None:
    """Writing through a dangling link creates the target; here the target is a new key."""
    (principal_dir(root, P) / "scratch" / "g").symlink_to("../keys/grocer.key")
    assert not (principal_dir(root, P) / "keys").exists()
    found = floored(root, Op(Kind.WRITE, f"principals/p-{P}/scratch/g"))
    assert found is not None and found.protected == f"principals/p-{P}/keys"


def test_a_floor_path_replaced_by_a_link_pointing_out_is_still_refused(
    root: Path, tmp_path: Path
) -> None:
    """The host loads ``hot/rules.md`` by that name and follows the link, so a write to the
    name is a write to what the host reads, wherever the bytes end up."""
    outside = tmp_path / "outside"
    outside.mkdir()
    grants = principal_dir(root, P) / "grants"
    grants.symlink_to(outside, target_is_directory=True)
    found = floored(root, Op(Kind.WRITE, f"principals/p-{P}/grants/calendar.json"))
    assert found is not None and found.protected == f"principals/p-{P}/grants"
    assert found.resolved is None  # it hit by name, before resolution was needed


def test_the_root_reached_through_a_link_still_floors(root: Path, tmp_path: Path) -> None:
    """macOS puts ``/var`` behind a link; the root a caller names may be one too."""
    via = tmp_path / "via"
    via.symlink_to(root, target_is_directory=True)
    assert floored(via, Op(Kind.WRITE, f"principals/p-{P}/hot/rules.md")) is not None
    assert floored(via, Op(Kind.WRITE, str(hot_dir(root, P) / "rules.md"))) is not None
    assert (
        floored(root, Op(Kind.WRITE, str(via / "principals" / f"p-{P}" / "keys" / "k"))) is not None
    )
    assert floored(via, Op(Kind.WRITE, f"principals/p-{P}/plans/week.md")) is None


def test_a_link_loop_cannot_be_resolved_and_is_refused(root: Path) -> None:
    scratch = principal_dir(root, P) / "scratch"
    (scratch / "a").symlink_to("b")
    (scratch / "b").symlink_to("a")
    found = floored(root, Op(Kind.WRITE, f"principals/p-{P}/scratch/a/x"))
    assert found is not None and found.how == "unresolvable"
    assert "cannot be resolved" in found.reason


def test_a_relative_path_that_climbs_out_and_back_in_is_refused(root: Path) -> None:
    """By name there is no answer for a path that leaves the root; resolution has one."""
    path = f"../{root.name}/principals/p-{P}/hot/rules.md"
    assert hit(path) is None
    found = floored(root, Op(Kind.WRITE, path))
    assert found is not None and found.protected == f"principals/p-{P}/hot"


def test_a_path_that_cannot_be_a_path_is_refused_not_raised(root: Path) -> None:
    """An embedded NUL makes ``realpath`` raise ``ValueError``, not ``OSError``; fail closed."""
    found = floored(root, Op(Kind.WRITE, f"principals/p-{P}/plans/we\0ek.md"))
    assert found is not None and found.how == "unresolvable"


def test_a_link_that_cannot_be_looked_at_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``realpath`` treats a component it cannot ``lstat`` as a plain name and carries on;
    a write through it would be a write to wherever it points. Refused, not guessed. A
    folder without search permission is the real case; the sandbox this runs in may not
    honour one, so the refusal is staged with ``lstat`` itself."""
    scratch = principal_dir(root, P) / "scratch"
    (scratch / "sealed").mkdir()
    (scratch / "sealed" / "k").symlink_to("../../keys")
    real_lstat = os.lstat

    def sealed(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path).startswith(str(scratch / "sealed" / "k")):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", sealed)
    found = floored(root, Op(Kind.WRITE, f"principals/p-{P}/scratch/sealed/k/grocer.key"))
    assert found is not None and found.how == "unresolvable"


def test_a_rename_whose_source_is_a_link_to_a_floor_path_is_refused(root: Path) -> None:
    (principal_dir(root, P) / "scratch" / "k").symlink_to("../keys")
    found = floored(
        root, Op(Kind.RENAME, f"principals/p-{P}/scratch/k", f"principals/p-{P}/scratch/k2")
    )
    assert found is not None and found.protected == f"principals/p-{P}/keys"


# --- what passes -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        Op(Kind.WRITE, f"principals/p-{P}/log/d-mbp.jsonl"),
        Op(Kind.WRITE, f"principals/p-{P}/plans/week.md"),
        Op(Kind.DELETE, f"principals/p-{P}/plans/week.md"),
        Op(Kind.RENAME, f"principals/p-{P}/scratch", f"principals/p-{P}/scratch-old"),
        Op(Kind.LINK, f"principals/p-{P}/plans/latest.md", f"principals/p-{P}/plans/week.md"),
        Op(Kind.WRITE, f"principals/p-{P}/hotel/booking.md"),
    ],
)
def test_an_ordinary_write_is_not_floored(root: Path, op: Op) -> None:
    assert floored(root, op) is None


def test_a_path_outside_the_root_that_links_nowhere_inside_is_not_floored(
    root: Path, tmp_path: Path
) -> None:
    assert floored(root, Op(Kind.WRITE, str(tmp_path / "elsewhere.md"))) is None


def test_the_sandbox_writes_only_where_no_floor_is(root: Path) -> None:
    """D72 says the floor cannot be a property of a sandbox or a mode. Here the two agree:
    ``srt``'s one writable folder is not floored, and every floor path is outside it."""
    settings = srt_settings(root, P)
    for allowed in settings["filesystem"]["allowWrite"]:
        assert floored(root, Op(Kind.WRITE, allowed)) is None
        assert floored(root, Op(Kind.WRITE, allowed + "/d-mbp.jsonl")) is None
        for path in FLOOR_PATHS:
            assert not str((root / path).resolve()).startswith(allowed.rstrip("/") + "/")


# --- through the gate: in every mode ---------------------------------------------------------


def _plan(**extra: Any) -> Mission:
    node = Node(
        id="write-plan",
        title="write the plan",
        reads=[f"principals/p-{P}/notes"],
        writes=[f"principals/p-{P}/plans", f"principals/p-{P}/hot/rules.md"],
        **extra,
    )
    return Mission(ts=TS, actor=ACTOR_USER, title="Sunday", nodes=[node], links=[])


def _state(*steps: Step) -> MissionState:
    events = [_plan(), *steps]
    records = [
        Record(ref=f"d-tab:{i}", seq=i, event=e.model_copy(update={"seq": i}), line=i)
        for i, e in enumerate(events, start=1)
    ]
    return fold_missions(records).mission("d-tab:1")


FLOORED_WRITE = Touch(writes=(f"principals/p-{P}/hot/rules.md",))
EXPECTED = Refusal(
    node="write-plan",
    reason=(
        f"write 'principals/p-{P}/hot/rules.md' is floored: under 'principals/p-{P}/hot', "
        "which nothing the agent does may write, in any mode (D72)"
    ),
)


@pytest.mark.parametrize("environ", [None, {}, {MISSIONS_SWITCH: "1"}, ON])
def test_a_declared_write_to_a_floor_path_is_refused_in_every_mode(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str] | None
) -> None:
    """The node *declares* the write and the manifest would admit it; the floor is under the
    manifest. Off, explicitly empty, wrongly spelled, on: the same refusal, word for word."""
    monkeypatch.delenv(MISSIONS_SWITCH, raising=False)
    assert admit(_state(), "write-plan", FLOORED_WRITE, environ) == EXPECTED


def test_the_floor_is_the_first_reason_even_before_the_switch() -> None:
    """A paused, breached or not-ready mission still says "floored" about a floored write:
    that reason tells the plan's author the plan is wrong, and the others are transient."""
    paused = Step(ts=TS, actor=ACTOR_USER, mission="d-tab:1", verb=StepVerb.PAUSE)
    assert admit(_state(paused), "write-plan", FLOORED_WRITE, ON) == EXPECTED
    assert admit(_state(), "no-such-node", FLOORED_WRITE, ON) == Refusal(
        node="no-such-node", reason=EXPECTED.reason
    )


@pytest.mark.parametrize("path", FLOOR_PATHS)
def test_every_floor_path_is_refused_at_the_gate(path: str) -> None:
    for touched in (path, UNDER[path]):
        refusal = admit(_state(), "write-plan", Touch(writes=(touched,)), ON)
        assert refusal is not None and "floored" in refusal.reason and path in refusal.reason


def test_the_floor_is_on_writes_not_reads() -> None:
    """Reading ``rules.md`` is what the hot tier is for; the floor is a write floor."""
    touch = Touch(reads=(f"principals/p-{P}/notes", f"principals/p-{P}/hot/rules.md"))
    refusal = admit(_state(), "write-plan", touch, ON)
    assert refusal is not None and "floored" not in refusal.reason  # undeclared, not floored
    declared = Touch(reads=(f"principals/p-{P}/notes",), writes=(f"principals/p-{P}/plans/w.md",))
    assert admit(_state(), "write-plan", declared, ON) is None


def test_a_running_step_is_held_to_the_floor_at_each_call() -> None:
    running = Step(
        ts=TS,
        actor=ACTOR_KERNEL,
        mission="d-tab:1",
        verb=StepVerb.STATUS,
        node="write-plan",
        status=StepStatus.RUNNING,
    )
    assert admit(_state(running), "write-plan", FLOORED_WRITE, ON) == EXPECTED


# --- nothing can lift it ---------------------------------------------------------------------


def test_nothing_in_the_environment_the_log_or_a_call_can_lift_the_floor() -> None:
    tree = ast.parse(FLOORS_SOURCE.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "environ" not in attrs and "getenv" not in attrs
    assert not any(f in Node.model_fields for f in ("floors", "unfloored", "allow_protected"))
    for fn in (floored, hit):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"environ", "allow", "unchecked", "force", "override"}, fn
    assert "enabled" not in names and "SWITCH" not in " ".join(names)


def test_the_floors_module_never_writes_starts_nothing_and_reads_no_clock() -> None:
    """The dormant rule, as it applies to a check: it refuses, it never acts. The only
    filesystem calls are the ones that resolve a name."""
    tree = ast.parse(FLOORS_SOURCE.read_text())
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    forbidden = {
        "open",
        "write_text",
        "write_bytes",
        "unlink",
        "rename",
        "replace",
        "remove",
        "rmdir",
        "chmod",
        "symlink",
        "link",
        "mkdir",
        "makedirs",
        "rmtree",
        "touch",
        "run",
        "Popen",
    }
    assert not (attrs & forbidden), attrs & forbidden
    assert "open" not in names
    imports = _runtime_imports(FLOORS_SOURCE)
    allowed = {"__future__", "dataclasses", "enum", "os", "posixpath", "stat", "typing"}
    assert imports <= allowed, imports - allowed
    assert not (imports & MODEL_OR_NETWORK)
    assert "subprocess" not in imports and "datetime" not in imports and "time" not in imports
    assert forbidden_in_closure(PACKAGE, ("floors",)) == {}


def test_the_cli_gains_no_floor_command() -> None:
    from walle.cli import main

    assert not any(
        word in name for name in main.commands for word in ("floor", "protect", "mission", "step")
    )


def test_an_ordinary_declared_write_is_still_refused_while_missions_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default stays off (D117): the floor refuses less than the switch does, never more."""
    monkeypatch.delenv(MISSIONS_SWITCH, raising=False)
    plain = Touch(writes=(f"principals/p-{P}/plans/week.md",))
    assert admit(_state(), "write-plan", plain) == Refusal("write-plan", "missions are off")
    assert admit(_state(), "write-plan", plain, {}) == Refusal("write-plan", "missions are off")
    assert admit(_state(), "write-plan", plain, ON) is None


def test_the_resolver_and_events_do_not_import_the_floor() -> None:
    assert "walle.floors" not in _runtime_imports(PACKAGE / "resolver.py")
    assert "walle.floors" not in _runtime_imports(PACKAGE / "events.py")
    assert "walle.missions" not in _runtime_imports(FLOORS_SOURCE)
    assert "walle.manifest" not in _runtime_imports(FLOORS_SOURCE)


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# --- the op itself ---------------------------------------------------------------------------


def test_a_two_ended_kind_needs_both_ends_and_a_one_ended_kind_refuses_a_second() -> None:
    with pytest.raises(ValueError, match="rename needs"):
        Op(Kind.RENAME, "a")
    with pytest.raises(ValueError, match="link needs"):
        Op(Kind.LINK, "a")
    with pytest.raises(ValueError, match="write has one path"):
        Op(Kind.WRITE, "a", "b")
    with pytest.raises(ValueError, match="delete has one path"):
        Op(Kind.DELETE, "a", "b")


def test_an_empty_path_is_the_root_and_the_root_is_above_everything(root: Path) -> None:
    for op in (
        Op(Kind.WRITE, ""),
        Op(Kind.DELETE, "."),
        Op(Kind.RENAME, str(root), str(root) + "2"),
    ):
        found = floored(root, op)
        assert found is not None and found.how == "above", op


def test_floored_is_a_pure_function_of_the_tree(root: Path) -> None:
    """Two calls, nothing changed on disk: the same answer, and nothing written."""
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    op = Op(Kind.WRITE, f"principals/p-{P}/hot/rules.md")
    assert floored(root, op) == floored(root, op)
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before
    assert os.path.exists(hot_dir(root, P) / "rules.md")
