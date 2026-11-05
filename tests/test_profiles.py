"""Issue #102's verify criteria, and the cases around them (D27, D28, D32, D33, D113).

  fusion can't cross principals     two principals hold beliefs that would fuse if the Curator
                                    saw them together; a run for one stages fusions only among
                                    that one's refs, and a profile's records are its own folder's
  an empty context fails closed     a job started with no context, or a context with no
                                    principal, raises before the job runs; nothing falls back
                                    to a default profile
  one run can't read another's      a profile built from one principal's context reaches no
  folder                            path in another's: not by ``..``, not by an absolute path,
                                    not through a symlink made inside its own folder, not
                                    through a link in place of a folder, not through a dangling
                                    link whose target would be created there
  an admin gets no read path        the same profile, built with the admin role, is refused the
                                    same way; the admin's one power over another's folder is to
                                    delete it, and deleting opens no file
  only the owner shares             publication reads only the publisher's own folder, so there
                                    is no call that publishes someone else's file
  shared/ holds capabilities only   publication accepts skills, MCP configs and hooks, refuses
                                    anything data-shaped, and the audit names a stray
  no ambient principal              the module reads no environment for a principal, keeps no
                                    context variable and no default; every entry point takes a
                                    ``Context``
  off by default                    nothing that writes runs unless the owner sets
                                    ``WALLE_PROFILES=on``, in that one spelling; while off, add,
                                    delete and publish touch nothing and the command refuses

Every root here is a ``tmp_path``; every principal, device, file and belief is made up.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from test_constraints import MODEL_OR_NETWORK, forbidden_in_closure
from walle import curator, profiles
from walle.cli import main
from walle.events import ACTOR_USER, Assertion, Proposal
from walle.index import update
from walle.paths import log_dir, principal_dir, stream_path
from walle.profiles import (
    CAPABILITY_KINDS,
    OFF_MESSAGE,
    SWITCH,
    SWITCH_ON,
    Context,
    CrossPrincipalError,
    EmptyContextError,
    Profile,
    ProfilesOffError,
    Role,
    RoleError,
    Shared,
    SharedDataError,
    add_profile,
    audit_shared,
    bind,
    delete_profile,
    enabled,
    publish,
    run,
    spawn,
)
from walle.resolver import resolve, staged
from walle.writer import StreamWriter

PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "profiles.py"
ON = {SWITCH: SWITCH_ON}
TS = "2026-11-05T09:00:00.000000+00:00"
A = "ada"  # a made-up household member
B = "sam"  # another
D = "mbp"
CANARY = b"SECRET-NOT-REAL-sam-only-7731"


def _household(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for who in (A, B):
        log_dir(root, who).mkdir(parents=True)
    return root


def _remember(root: Path, who: str, body: str) -> str:
    writer = StreamWriter(root, who, D)
    written = writer.append(Assertion(ts=TS, actor=ACTOR_USER, body=body, summary=body[:120]))
    assert written.seq is not None
    return f"{writer.stream}:{written.seq}"


def _plant(root: Path, who: str, relative: str, data: bytes = CANARY) -> Path:
    path = principal_dir(root, who) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _admin_args(verb: str, root: Path, principal: str) -> list[str]:
    return [
        "profiles",
        verb,
        "--root",
        str(root),
        "--as",
        A,
        "--role",
        "admin",
        "--principal",
        principal,
    ]


def _tree(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        out[rel] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return out


# --- fusion can't cross principals (D28, D32) ----------------------------------------------------


def test_a_curate_run_for_one_principal_fuses_only_that_principal_s_beliefs(
    tmp_path: Path,
) -> None:
    """Ada's two beliefs about the allotment fuse under the fake Curator. Sam holds one that
    would fuse with Ada's if anything put them side by side. It is never shown."""
    root = _household(tmp_path)
    ada_1 = _remember(root, A, "water the allotment tomatoes before eight every morning")
    ada_2 = _remember(root, A, "the allotment tomatoes want water before eight, every morning")
    sam_1 = _remember(root, B, "allotment tomatoes before eight every morning WITHOUTFAIL")

    report = curator.curate(root, A, D, curator.FakeCurator(), ts=TS)

    assert report.proposed, "the fake fuses Ada's pair; the test needs a fusion to inspect"
    ada_refs = {ada_1, ada_2}
    # A ref is ``d-<device>:<seq>``, scoped to a folder: Sam's first event is also ``d-mbp:1``.
    # So the check is by ref *and* by words — Sam's marker word never reaches Ada's queue.
    assert sam_1 == ada_1
    items = staged(update(root, A).records)
    assert items
    for item in items:
        assert set(item.proposal.caused_by) <= ada_refs
        assert "WITHOUTFAIL" not in item.proposal.body
        assert "WITHOUTFAIL" not in item.proposal.summary
    # Sam's log is byte-for-byte what it was: one assertion, nothing staged.
    sam_records = update(root, B).records
    assert len(sam_records) == 1
    assert not staged(sam_records)
    assert not any(isinstance(r.event, Proposal) for r in sam_records)


def test_a_profile_s_records_are_its_own_folder_s_and_nothing_else(tmp_path: Path) -> None:
    root = _household(tmp_path)
    ada_ref = _remember(root, A, "ada likes the kitchen window open")
    _remember(root, B, "sam keeps the kitchen window shut")

    profile = Profile(bind(root, A))
    refs = {r.ref for r in profile.records()}
    assert refs == {ada_ref}
    resolution = profile.resolution()
    assert [b.ref for b in resolution.current] == [ada_ref]
    assert resolution == resolve(update(root, A).records)


def test_nothing_in_the_curator_takes_two_principals() -> None:
    """By source: every public function on the fusion path names at most one principal, so
    there is no call that could be handed two folders."""
    for module in (curator, profiles):
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if function.__module__ != module.__name__ or name.startswith("_"):
                continue
            params = inspect.signature(function).parameters
            named = [p for p in params if "principal" in p and p != "principal"]
            assert not named, f"{module.__name__}.{name} takes {named}"
            assert sum(1 for p in params if p == "principal") <= 1


# --- an empty context fails closed (D113) -------------------------------------------------------


def test_a_job_started_with_no_context_raises_before_it_runs(tmp_path: Path) -> None:
    ran: list[Context] = []

    def job(context: Context) -> str:
        ran.append(context)
        return "ran"

    with pytest.raises(EmptyContextError):
        run(None, job)  # type: ignore[arg-type]  # the point: a None slips in at runtime
    with pytest.raises(EmptyContextError):
        spawn(None, job)  # type: ignore[arg-type]
    assert ran == []

    context = bind(_household(tmp_path), A)
    assert run(context, job) == "ran"
    assert ran == [context]


def test_a_context_with_no_principal_cannot_be_made(tmp_path: Path) -> None:
    root = _household(tmp_path)
    for empty in ("", " ", "\n"):
        with pytest.raises(EmptyContextError):
            bind(root, empty)
        with pytest.raises(EmptyContextError):
            Context(root=root, principal=empty)
    with pytest.raises(ValueError):
        bind(root, "../sam")
    with pytest.raises(ValueError):
        bind(root, A, device="../x")


def test_a_wrong_object_in_place_of_a_context_is_refused(tmp_path: Path) -> None:
    """A string that happens to be a principal's name is the likeliest wrong thing to pass.
    It is not a context and does not become one."""
    root = _household(tmp_path)
    for wrong in (A, (root, A), {"root": root, "principal": A}, object()):
        with pytest.raises(EmptyContextError):
            run(wrong, lambda c: c)  # type: ignore[arg-type]
        with pytest.raises(EmptyContextError):
            Profile(wrong)  # type: ignore[arg-type]


def test_a_spawned_job_runs_with_the_context_it_was_given_and_no_other(tmp_path: Path) -> None:
    root = _household(tmp_path)
    seen: dict[str, Context] = {}
    done = threading.Event()

    def job(context: Context) -> None:
        seen["context"] = context
        done.set()

    thread = spawn(bind(root, B), job)
    assert done.wait(5)
    thread.join(5)
    assert seen["context"].principal == B
    assert seen["context"].root == root.resolve()


def test_a_job_that_writes_needs_a_bound_stream(tmp_path: Path) -> None:
    root = _household(tmp_path)
    with pytest.raises(EmptyContextError):
        bind(root, A).writer()
    writer = bind(root, A, device=D).writer()
    assert writer.stream == f"d-{D}"
    written = writer.append(Assertion(ts=TS, actor=ACTOR_USER, body="x", summary="x"))
    assert written.seq == 1
    assert stream_path(root, A, D).is_file()
    assert not stream_path(root, B, D).exists()


def test_a_job_s_error_is_the_job_s_and_the_context_is_not_kept(tmp_path: Path) -> None:
    root = _household(tmp_path)

    def job(context: Context) -> None:
        raise KeyError("the job's own failure")

    with pytest.raises(KeyError):
        run(bind(root, A), job)
    assert not hasattr(profiles, "current"), "no 'current context' exists to be left behind"


# --- one principal's run can't read another's folder (D32, hard constraint 6) ---------------------


def _cases(root: Path) -> list[str]:
    sam = principal_dir(root, B)
    return [
        f"../p-{B}/log/d-{D}.jsonl",
        f"log/../../p-{B}/log/d-{D}.jsonl",
        f"../p-{B}/../p-{A}/log",  # lands in Ada's own log, having stood in Sam's folder
        str(sam / "log" / f"d-{D}.jsonl"),
        str(root / "shared" / "skills" / "x.md"),
        str(root),
        "..",
        "../..",
        "",
        ".",
        "/",
    ]


@pytest.mark.parametrize("role", list(Role))
def test_no_path_reaches_another_principal_s_folder_by_name(tmp_path: Path, role: Role) -> None:
    root = _household(tmp_path)
    _plant(root, B, f"log/d-{D}.jsonl")
    profile = Profile(bind(root, A, role=role))
    for case in _cases(root):
        with pytest.raises(CrossPrincipalError):
            profile.path(case)
        with pytest.raises(CrossPrincipalError):
            profile.open(case)
    # Its own log is reachable, by the same call.
    own = profile.path(f"log/d-{D}.jsonl")
    assert own == principal_dir(root, A) / "log" / f"d-{D}.jsonl"


@pytest.mark.parametrize("role", list(Role))
def test_no_path_reaches_another_principal_s_folder_through_a_link(
    tmp_path: Path, role: Role
) -> None:
    """Links made inside Ada's folder, each pointing at Sam's. Every one is refused on the way
    through, not after a read: the canary never comes back."""
    root = _household(tmp_path)
    sam_log = _plant(root, B, f"log/d-{D}.jsonl")
    ada = principal_dir(root, A)
    (ada / "notes").mkdir()

    (ada / "notes" / "peek").symlink_to(sam_log)  # a file link, absolute
    (ada / "notes" / "rel").symlink_to(Path("..") / ".." / f"p-{B}" / "log" / f"d-{D}.jsonl")
    (ada / "notes" / "theirs").symlink_to(principal_dir(root, B))  # a folder link
    (ada / "notes" / "dangling").symlink_to(principal_dir(root, B) / "new-file")
    (ada / "hot").symlink_to(principal_dir(root, B) / "log")  # a folder replaced by a link
    (ada / "loop").symlink_to(ada / "loop")

    profile = Profile(bind(root, A, role=role))
    for relative in (
        "notes/peek",
        "notes/rel",
        f"notes/theirs/log/d-{D}.jsonl",
        "notes/theirs",
        "notes/dangling",
        f"hot/d-{D}.jsonl",
        "hot",
        "loop",
        "loop/x",
    ):
        with pytest.raises(CrossPrincipalError) as excinfo:
            profile.path(relative)
        assert CANARY.decode() not in str(excinfo.value)
        with pytest.raises(CrossPrincipalError), profile.open(relative) as f:
            f.read()
    # A link that stays inside the folder is fine.
    (ada / "notes" / "mine").symlink_to(ada / "log")
    assert profile.path("notes/mine").resolve() == (ada / "log").resolve()


def test_a_plain_open_follows_no_link_even_one_that_stays_inside(tmp_path: Path) -> None:
    """Issue #118 finding 1.4: the grants' rule files are opened through this door with
    ``plain=True``, which refuses a name that lands anywhere but on itself — a link inside the
    folder included, which ``open`` alone would follow because it is still Ada's."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    (ada / "grants").mkdir()
    (ada / "grants" / "real.jsonl").write_text("")
    (ada / "grants" / "always.jsonl").symlink_to("real.jsonl")
    (ada / "rules").symlink_to("grants")
    profile = Profile(bind(root, A))
    with profile.open("grants/always.jsonl") as f:  # the ordinary door follows it: it is inside
        assert f.read() == b""
    for name in ("grants/always.jsonl", "rules/real.jsonl", "rules/missing.jsonl"):
        assert not profile.resolve(name).plain
        with pytest.raises(CrossPrincipalError, match="link"):
            profile.open(name, plain=True)
    assert profile.resolve("grants/real.jsonl").plain
    assert profile.resolve("grants/missing.jsonl").plain  # nothing there yet: it would be made here
    with profile.open("grants/real.jsonl", plain=True) as f:
        assert f.read() == b""
    assert profile.resolve("GRANTS/Real.jsonl").plain  # compared case-folded, as everywhere here


def test_a_principal_folder_that_is_itself_a_link_is_refused(tmp_path: Path) -> None:
    """``principals/p-ada`` replaced by a link to Sam's folder: Ada's own paths would all land
    in Sam's. Nothing is reachable."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    import shutil

    shutil.rmtree(ada)
    ada.symlink_to(principal_dir(root, B))
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError):
        profile.path("log")
    with pytest.raises(CrossPrincipalError):
        profile.records()


def test_a_profile_s_paths_are_all_inside_its_folder(tmp_path: Path) -> None:
    root = _household(tmp_path)
    profile = Profile(bind(root, A, device=D))
    folder = principal_dir(root, A)
    stream = profile.stream_path
    assert stream is not None
    for path in (profile.folder, profile.log_dir, stream, profile.index_path, profile.hot_dir):
        assert path == folder or folder in path.parents, path
    assert Profile(bind(root, A)).stream_path is None


def test_the_context_is_the_only_way_to_build_a_profile() -> None:
    """``Profile.__init__`` takes a ``Context`` and nothing else: no root, no name, no role
    and no second principal can be passed in beside it."""
    params = list(inspect.signature(Profile.__init__).parameters)
    assert params == ["self", "context"]


# --- an admin role gets no read path into another principal's folder -----------------------------


def test_an_admin_s_profile_is_its_own_folder_like_any_other(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, B, f"log/d-{D}.jsonl")
    admin = Profile(bind(root, A, role=Role.ADMIN))
    assert admin.folder == principal_dir(root, A)
    with pytest.raises(CrossPrincipalError):
        admin.path(f"../p-{B}/log/d-{D}.jsonl")
    assert {r.ref for r in admin.records()} == set()


def test_no_function_takes_a_role_and_another_principal_to_read() -> None:
    """By source: of the module's public functions, the only ones that take both a context and
    another principal's name are the admin's two whole-folder powers, ``add_profile`` and
    ``delete_profile``, and neither opens a file. There is no ``read_as``, no ``open_for``,
    nothing a role could unlock."""
    takes_both: list[str] = []
    for name, function in inspect.getmembers(profiles, inspect.isfunction):
        if function.__module__ != profiles.__name__ or name.startswith("_"):
            continue
        params = inspect.signature(function).parameters
        if "context" in params and any(p in ("target", "principal", "other") for p in params):
            takes_both.append(name)
    assert takes_both == ["add_profile", "delete_profile"]
    assert "role" not in inspect.signature(Profile.path).parameters
    assert "role" not in inspect.signature(Profile.open).parameters


def test_an_admin_deletes_a_profile_without_opening_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every way of reading a file's bytes is made to fail. Deletion still completes: it
    unlinks by name and never opens anything."""
    root = _household(tmp_path)
    _plant(root, B, f"log/d-{D}.jsonl")
    _plant(root, B, "hot/learned.md")
    _plant(root, B, "index.sqlite")
    (principal_dir(root, B) / "notes").mkdir()
    (principal_dir(root, B) / "notes" / "out").symlink_to(principal_dir(root, A))
    ada_log = _plant(root, A, f"log/d-{D}.jsonl", b"ada's own")

    def no_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a file was opened: {args[:1]}")

    monkeypatch.setattr(builtins, "open", no_read)
    monkeypatch.setattr(Path, "read_bytes", no_read)
    monkeypatch.setattr(Path, "read_text", no_read)
    monkeypatch.setattr(Path, "open", no_read)
    monkeypatch.setattr(os, "read", no_read)

    deleted = delete_profile(bind(root, A, role=Role.ADMIN), B, environ=ON)
    assert deleted.principal == B
    assert deleted.folder == principal_dir(root, B)
    assert not principal_dir(root, B).exists()
    assert deleted.files == 3
    assert deleted.links == 1
    # The link out of Sam's folder was removed as a link; what it pointed at is untouched.
    monkeypatch.undo()
    assert ada_log.read_bytes() == b"ada's own"
    assert principal_dir(root, A).is_dir()


def test_only_an_admin_deletes_and_only_a_principal_s_folder(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, B, f"log/d-{D}.jsonl")
    before = _tree(root)
    for role in (Role.OPERATOR, Role.STAKEHOLDER):
        with pytest.raises(RoleError):
            delete_profile(bind(root, A, role=role), B, environ=ON)
    admin = bind(root, A, role=Role.ADMIN)
    with pytest.raises(ValueError):
        delete_profile(admin, "../shared", environ=ON)
    with pytest.raises(ValueError):
        delete_profile(admin, "", environ=ON)
    with pytest.raises(FileNotFoundError):
        delete_profile(admin, "nobody", environ=ON)
    assert _tree(root) == before


def test_deleting_a_profile_whose_folder_is_a_link_removes_nothing_behind_it(
    tmp_path: Path,
) -> None:
    root = _household(tmp_path)
    import shutil

    shutil.rmtree(principal_dir(root, B))
    principal_dir(root, B).symlink_to(principal_dir(root, A))
    _plant(root, A, f"log/d-{D}.jsonl", b"ada's own")
    with pytest.raises(CrossPrincipalError):
        delete_profile(bind(root, A, role=Role.ADMIN), B, environ=ON)
    assert principal_dir(root, B).is_symlink()
    assert (principal_dir(root, A) / "log" / f"d-{D}.jsonl").read_bytes() == b"ada's own"


# --- only the folder's owner shares; shared/ holds capabilities only (D32 rule 1, D33) -----------


def test_publication_reads_only_the_publisher_s_own_folder(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"# water the tomatoes\n")
    _plant(root, B, "skills/shut.md", CANARY)

    published = publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    assert published.path == root / "shared" / "skills" / "water.md"
    assert published.path.read_bytes() == b"# water the tomatoes\n"
    assert published.source == principal_dir(root, A) / "skills" / "water.md"

    # Nobody, whatever their role, publishes Sam's file: the only source is one's own folder.
    for role in Role:
        for source in (f"../p-{B}/skills/shut.md", str(principal_dir(root, B) / "skills/shut.md")):
            with pytest.raises(CrossPrincipalError):
                publish(bind(root, A, role=role), source, "skills", environ=ON)
    (principal_dir(root, A) / "skills" / "borrowed.md").symlink_to(
        principal_dir(root, B) / "skills" / "shut.md"
    )
    with pytest.raises(CrossPrincipalError):
        publish(bind(root, A, role=Role.ADMIN), "skills/borrowed.md", "skills", environ=ON)
    assert not (root / "shared" / "skills" / "shut.md").exists()
    assert not (root / "shared" / "skills" / "borrowed.md").exists()
    assert CANARY not in b"".join(
        p.read_bytes() for p in (root / "shared").rglob("*") if p.is_file()
    )


def test_publication_never_replaces_what_is_already_shared(tmp_path: Path) -> None:
    """Append, never mutate (hard constraint 2): a second version is a second file."""
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"v1")
    publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    _plant(root, A, "skills/water.md", b"v2")
    with pytest.raises(FileExistsError):
        publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    assert (root / "shared" / "skills" / "water.md").read_bytes() == b"v1"


@pytest.mark.parametrize(
    "source",
    [
        f"log/d-{D}.jsonl",
        "index.sqlite",
        "hot/learned.md",
        "hot/rules.md",
        "skills/notes.jsonl",
        "skills/cache.sqlite",
        "skills/index.lock",
        "grants/x.json",
        "keys/x",
    ],
)
def test_data_shaped_files_cannot_be_published(tmp_path: Path, source: str) -> None:
    root = _household(tmp_path)
    _plant(root, A, source, b"not a capability")
    with pytest.raises(SharedDataError):
        publish(bind(root, A), source, "skills", environ=ON)
    assert not (root / "shared").exists()


def test_only_capability_kinds_are_accepted(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"x")
    assert CAPABILITY_KINDS == ("skills", "mcp", "hooks")
    for kind in ("log", "memory", "data", "workflows", "", "../log"):
        with pytest.raises(SharedDataError):
            publish(bind(root, A), "skills/water.md", kind, environ=ON)  # type: ignore[arg-type]
    shared = Shared(root)
    for kind in CAPABILITY_KINDS:
        assert shared.capability_dir(kind) == root / "shared" / kind
    with pytest.raises(SharedDataError):
        shared.capability_dir("log")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        shared.capability_path("skills", "../x")
    assert not any("log" in name for name in dir(shared) if not name.startswith("_"))


def test_the_audit_names_anything_in_shared_that_is_not_a_capability(tmp_path: Path) -> None:
    root = _household(tmp_path)
    assert audit_shared(root) == ()
    _plant(root, A, "skills/water.md", b"x")
    publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    assert audit_shared(root) == ()

    shared = root / "shared"
    (shared / "log").mkdir()
    (shared / "log" / f"d-{D}.jsonl").write_bytes(CANARY)
    (shared / "skills" / "beliefs.jsonl").write_bytes(CANARY)
    (shared / "memory.sqlite").write_bytes(CANARY)
    (shared / "hooks").mkdir()
    (shared / "hooks" / "peek").symlink_to(principal_dir(root, B))

    strays = audit_shared(root)
    assert {s.path for s in strays} == {
        "log",
        f"log/d-{D}.jsonl",
        "skills/beliefs.jsonl",
        "memory.sqlite",
        "hooks/peek",
    }
    for stray in strays:
        assert CANARY.decode() not in stray.reason
        assert stray.reason


# --- no ambient principal ------------------------------------------------------------------------


def test_the_module_takes_no_principal_from_the_environment_or_a_context_variable() -> None:
    tree = ast.parse(SOURCE.read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "contextvars" not in imported | modules
    assert "ContextVar" not in names | imported
    assert not {"getenv", "home", "expanduser", "getcwd", "cwd", "getlogin", "getuid"} & attrs
    # ``environ`` is read for exactly one thing: the switch. Never for a name.
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript | ast.Call)
        and isinstance(getattr(node, "value", getattr(node, "func", None)), ast.Attribute)
    ]
    for node in reads:
        target = node.value if isinstance(node, ast.Subscript) else node.func
        assert isinstance(target, ast.Attribute)
        if target.attr == "get" and isinstance(target.value, ast.Name) and target.value.id == "env":
            key = node.slice if isinstance(node, ast.Subscript) else node.args[0]
            assert isinstance(key, ast.Name) and key.id == "SWITCH"


def test_no_default_principal_root_or_device_exists() -> None:
    for name in ("DEFAULT_PRINCIPAL", "DEFAULT_ROOT", "DEFAULT_DEVICE", "current", "default"):
        assert not hasattr(profiles, name)
    for field in ("root", "principal"):
        assert Context.__dataclass_fields__[field].default is inspect.Parameter.empty or (
            Context.__dataclass_fields__[field].default.__class__.__name__ == "_MISSING_TYPE"
        )


def test_nothing_the_profiles_module_imports_reaches_a_model() -> None:
    assert forbidden_in_closure(PACKAGE, ("profiles",)) == {}
    source = SOURCE.read_text()
    assert not any(f"import {m}" in source for m in MODEL_OR_NETWORK)


# --- off by default (the dormant rule) ----------------------------------------------------------


def test_profiles_are_off_by_default() -> None:
    assert enabled({}) is False
    assert enabled({SWITCH: ""}) is False
    assert enabled({"WALLE_PROFILE": SWITCH_ON}) is False
    assert enabled(dict.fromkeys([SWITCH], SWITCH_ON)) is True


@pytest.mark.parametrize("value", ["ON", "On", "1", "true", "yes", " on", "on ", "on\n", "enabled"])
def test_only_the_one_spelling_turns_it_on(value: str) -> None:
    assert enabled({SWITCH: value}) is False


def test_the_process_environment_is_read_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    assert enabled() is False
    monkeypatch.setenv(SWITCH, SWITCH_ON)
    assert enabled() is True


def test_while_off_add_delete_and_publish_touch_nothing(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"x")
    _plant(root, B, f"log/d-{D}.jsonl")
    before = _tree(root)
    for environ in ({}, {SWITCH: "ON"}, {SWITCH: ""}):
        with pytest.raises(ProfilesOffError, match=OFF_MESSAGE):
            add_profile(bind(root, A, role=Role.ADMIN), "kim", environ=environ)
        with pytest.raises(ProfilesOffError):
            delete_profile(bind(root, A, role=Role.ADMIN), B, environ=environ)
        with pytest.raises(ProfilesOffError):
            publish(bind(root, A), "skills/water.md", "skills", environ=environ)
    assert _tree(root) == before
    assert not (root / "shared").exists()


def test_isolation_has_no_switch(tmp_path: Path) -> None:
    """What refuses a cross-principal path is not something the owner turns on. On or off,
    the answer is the same refusal, and the pure parts take no environment at all."""
    root = _household(tmp_path)
    _plant(root, B, f"log/d-{D}.jsonl")
    for environ in ({}, ON):
        with pytest.raises(CrossPrincipalError):
            Profile(bind(root, A)).path(f"../p-{B}/log/d-{D}.jsonl")
        del environ
    for function in (bind, run, spawn, Profile.path, Profile.open, Profile.records):
        assert "environ" not in inspect.signature(function).parameters


def test_the_command_refuses_while_off_and_creates_nothing(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"x")
    before = _tree(root)
    runner = CliRunner()
    publish_args = [
        "profiles",
        "publish",
        "--root",
        str(root),
        "--principal",
        A,
        "--kind",
        "skills",
    ]
    for args in (
        _admin_args("add", root, "kim"),
        _admin_args("delete", root, B),
        [*publish_args, "skills/water.md"],
    ):
        result = runner.invoke(main, args, env={SWITCH: ""})
        assert result.exit_code == 1, result.output
        assert OFF_MESSAGE in result.output
    assert _tree(root) == before


def test_the_command_adds_deletes_and_publishes_when_on(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"# water\n")
    runner = CliRunner()

    result = runner.invoke(main, _admin_args("add", root, "kim"), env=ON)
    assert result.exit_code == 0, result.output
    assert log_dir(root, "kim").is_dir()

    result = runner.invoke(main, _admin_args("add", root, "kim"), env=ON)
    assert result.exit_code == 1 and "already" in result.output

    publish_args = [
        "profiles",
        "publish",
        "--root",
        str(root),
        "--principal",
        A,
        "--kind",
        "skills",
    ]
    result = runner.invoke(main, [*publish_args, "skills/water.md"], env=ON)
    assert result.exit_code == 0, result.output
    assert (root / "shared" / "skills" / "water.md").read_bytes() == b"# water\n"

    as_operator = [a if a != "admin" else "operator" for a in _admin_args("delete", root, B)]
    result = runner.invoke(main, as_operator, env=ON)
    assert result.exit_code == 1 and "admin" in result.output
    assert principal_dir(root, B).is_dir()

    result = runner.invoke(main, _admin_args("delete", root, B), env=ON)
    assert result.exit_code == 0, result.output
    assert not principal_dir(root, B).exists()
    assert principal_dir(root, A).is_dir()


def test_adding_a_profile_makes_an_empty_folder_and_needs_an_admin(tmp_path: Path) -> None:
    root = _household(tmp_path)
    with pytest.raises(RoleError):
        add_profile(bind(root, A), "kim", environ=ON)
    added = add_profile(bind(root, A, role=Role.ADMIN), "kim", environ=ON)
    assert added.folder == principal_dir(root, "kim")
    assert log_dir(root, "kim").is_dir()
    assert sorted(p.name for p in added.folder.iterdir()) == ["log"]
    with pytest.raises(FileExistsError):
        add_profile(bind(root, A, role=Role.ADMIN), "kim", environ=ON)
    with pytest.raises(ValueError):
        add_profile(bind(root, A, role=Role.ADMIN), "../x", environ=ON)


# --- found by the stranger pass -------------------------------------------------------------------


def test_a_stream_file_that_is_a_link_into_another_folder_is_never_indexed(tmp_path: Path) -> None:
    """``log/d-x.jsonl`` as a link to Sam's stream: the index would read Sam's bytes as Ada's.
    ``records`` refuses before the index runs, and no index is built."""
    root = _household(tmp_path)
    _remember(root, B, "sam's private note WITHOUTFAIL")
    (principal_dir(root, A) / "log" / "d-phone.jsonl").symlink_to(stream_path(root, B, D))
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError):
        profile.records()
    with pytest.raises(CrossPrincipalError):
        profile.resolution()
    assert not profile.index_path.exists()
    # ``log/`` itself as a link is the same refusal.
    import shutil

    shutil.rmtree(principal_dir(root, A) / "log")
    (principal_dir(root, A) / "log").symlink_to(log_dir(root, B))
    with pytest.raises(CrossPrincipalError):
        profile.records()


def test_publishing_through_a_shared_folder_that_is_a_link_is_refused(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, A, "skills/water.md", b"x")
    (root / "shared").symlink_to(principal_dir(root, B))
    with pytest.raises(CrossPrincipalError):
        publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    assert not (principal_dir(root, B) / "skills").exists()
    (root / "shared").unlink()
    (root / "shared").mkdir()
    (root / "shared" / "skills").symlink_to(principal_dir(root, B))
    with pytest.raises(CrossPrincipalError):
        publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    assert not (principal_dir(root, B) / "water.md").exists()


def test_a_source_swapped_for_a_link_after_the_check_is_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check passes on a regular file; the name is a link by the time it is opened. The
    open does not follow it."""
    root = _household(tmp_path)
    mine = _plant(root, A, "skills/water.md", b"x")
    _plant(root, B, "skills/shut.md", CANARY)
    real_resolve = Profile.resolve

    def swap_after_check(self: Profile, relative: Any) -> Any:
        checked = real_resolve(self, relative)
        mine.unlink()
        mine.symlink_to(principal_dir(root, B) / "skills" / "shut.md")
        return checked

    monkeypatch.setattr(Profile, "resolve", swap_after_check)
    with pytest.raises(CrossPrincipalError):
        publish(bind(root, A), "skills/water.md", "skills", environ=ON)
    assert not (root / "shared").exists()


def test_a_spawned_thread_is_not_a_daemon_unless_asked(tmp_path: Path) -> None:
    root = _household(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def job(context: Context) -> None:
        started.set()
        release.wait(5)

    thread = spawn(bind(root, A), job)
    assert started.wait(5)
    assert thread.daemon is False
    release.set()
    thread.join(5)


# --- the second review round: decide on the resolved path, act on the same path -----------------


def test_a_link_to_an_ancestor_of_the_profile_folder_is_refused(tmp_path: Path) -> None:
    """The review's reproduction: ``up -> <root>/principals`` passed the first version, because
    an ancestor was allowed as a place to *stand* and the landing was never required to be
    strictly inside. Listing it would have shown every principal's name."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    (ada / "up").symlink_to(root / "principals")
    (ada / "up2").symlink_to(root)
    (ada / "slash").symlink_to(Path("/"))
    (ada / "dots").symlink_to(Path(".."))
    (ada / "dotdots").symlink_to(Path("..") / "..")
    (ada / "self").symlink_to(Path("."))
    (ada / "selfabs").symlink_to(ada)
    profile = Profile(bind(root, A))
    for name in ("up", "up2", "slash", "dots", "dotdots", "self", "selfabs", "up/p-ada/log"):
        with pytest.raises(CrossPrincipalError):
            profile.path(name)
        with pytest.raises(CrossPrincipalError):
            profile.open(name)


def test_the_path_given_out_is_the_resolved_one(tmp_path: Path) -> None:
    """What is checked is what is used: ``path`` returns where the name lands, not the name."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    (ada / "notes").mkdir()
    (ada / "notes" / "mine").symlink_to(ada / "log")
    profile = Profile(bind(root, A))
    assert profile.path("notes/mine") == (ada / "log").resolve()
    assert profile.path(f"notes/mine/d-{D}.jsonl") == (ada / "log" / f"d-{D}.jsonl").resolve()


@pytest.mark.parametrize(
    "source",
    [
        "HOT/learned.md",
        "Hot/learned.md",
        "KEYS/api_key",
        "Grants/x.json",
        "LOG/d-mbp.jsonl",
        "Index.SQLITE",
        "skills/Notes.JSONL",
    ],
)
def test_data_places_are_matched_without_regard_to_case(tmp_path: Path, source: str) -> None:
    """APFS and NTFS fold case: ``HOT/`` is ``hot/`` there. The check folds it everywhere, so
    the answer does not depend on the filesystem the test runs on."""
    root = _household(tmp_path)
    _plant(root, A, source, b"not a capability")
    with pytest.raises(SharedDataError):
        publish(bind(root, A), source, "skills", environ=ON)
    assert not (root / "shared").exists()


def test_an_in_folder_link_cannot_publish_data_under_a_capability_name(tmp_path: Path) -> None:
    """The review's second reproduction: ``skills -> hot`` stays inside the folder, so the
    first version checked the shape of ``skills/learned.md`` and copied ``hot/learned.md``."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    _plant(root, A, "hot/learned.md", CANARY)
    _plant(root, A, "keys/api_key", CANARY)
    _plant(root, A, "index.sqlite", CANARY)
    (ada / "skills").symlink_to(ada / "hot")
    (ada / "mcp").symlink_to(Path("keys"))
    (ada / "hooks").mkdir()
    (ada / "hooks" / "link.md").symlink_to(Path("..") / "hot" / "learned.md")
    (ada / "hooks" / "idx").symlink_to(ada / "index.sqlite")
    for source, kind in (
        ("skills/learned.md", "skills"),
        ("mcp/api_key", "mcp"),
        ("hooks/link.md", "hooks"),
        ("hooks/idx", "hooks"),
    ):
        with pytest.raises(SharedDataError):
            publish(bind(root, A), source, kind, environ=ON)  # type: ignore[arg-type]
    assert not (root / "shared").exists(), "nothing is written before every check has passed"


def test_nothing_is_written_before_every_check_passes(tmp_path: Path) -> None:
    """Every refusal ``publish`` can give, and after each the root is byte-identical: no
    ``shared/`` folder, no empty kind folder, no partial file."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    _plant(root, A, "skills/water.md", b"x")
    _plant(root, A, "hot/learned.md", CANARY)
    _plant(root, B, "skills/shut.md", CANARY)
    (ada / "skills" / "theirs.md").symlink_to(principal_dir(root, B) / "skills" / "shut.md")
    (ada / "skills" / "data.md").symlink_to(Path("..") / "hot" / "learned.md")
    (ada / "skills" / "gone.md").symlink_to(ada / "skills" / "nowhere.md")
    (ada / "skills" / "folder").mkdir()
    before = _tree(root)
    cases: list[tuple[str, type[Exception]]] = [
        ("skills/theirs.md", CrossPrincipalError),
        ("skills/data.md", SharedDataError),
        ("skills/gone.md", FileNotFoundError),
        ("skills/folder", SharedDataError),
        ("skills/missing.md", FileNotFoundError),
        ("skills/water.md", SharedDataError),  # wrong kind, below
    ]
    for source, error in cases:
        kind = "log" if source == "skills/water.md" else "skills"
        with pytest.raises(error):
            publish(bind(root, A), source, kind, environ=ON)  # type: ignore[arg-type]
        assert _tree(root) == before, source


def test_the_audit_names_a_shared_folder_that_is_itself_a_link(tmp_path: Path) -> None:
    root = _household(tmp_path)
    _plant(root, B, "skills/shut.md", CANARY)
    (root / "shared").symlink_to(principal_dir(root, B))
    strays = audit_shared(root)
    assert [s.path for s in strays] == ["."]
    assert "link" in strays[0].reason
    assert CANARY.decode() not in strays[0].reason
    (root / "shared").unlink()
    (root / "shared").symlink_to(root / "nowhere")
    assert [s.path for s in audit_shared(root)] == ["."]


def test_a_principals_folder_that_is_a_link_is_refused_by_every_entry(tmp_path: Path) -> None:
    """``add_profile`` and ``delete_profile`` refused a linked ``principals/``; ``_real_folder``
    did not. Now every entry does."""
    root = tmp_path / "root"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / f"p-{A}" / "log").mkdir(parents=True)
    root.mkdir()
    (root / "principals").symlink_to(elsewhere)
    profile = Profile(bind(root, A))
    with pytest.raises(CrossPrincipalError):
        profile.path("log")
    with pytest.raises(CrossPrincipalError):
        profile.records()
    with pytest.raises(CrossPrincipalError):
        publish(bind(root, A), "skills/x.md", "skills", environ=ON)
    with pytest.raises(CrossPrincipalError):
        add_profile(bind(root, A, role=Role.ADMIN), "kim", environ=ON)
    with pytest.raises(CrossPrincipalError):
        delete_profile(bind(root, A, role=Role.ADMIN), A, environ=ON)


HOP_KINDS = (
    "absolute",
    "relative",
    "chain",
    "ancestor",
    "case",
    "folder-absolute",
    "folder-relative",
    "dangling-into",
)


def _hop(root: Path, kind: str, *, target_file: Path, own_data: Path) -> tuple[str, bool]:
    """Make one link of ``kind`` inside Ada's ``skills/`` and return the source to name and
    whether it stays inside Ada's folder (so ``path`` allows it and only the shape refuses)."""
    ada = principal_dir(root, A)
    skills = ada / "skills"
    skills.mkdir(exist_ok=True)
    rel_to_target = Path(os.path.relpath(target_file, skills))
    if kind == "absolute":
        (skills / "a.md").symlink_to(target_file)
        return "skills/a.md", False
    if kind == "relative":
        (skills / "r.md").symlink_to(rel_to_target)
        return "skills/r.md", False
    if kind == "chain":
        (skills / "c1.md").symlink_to(Path("c2.md"))
        (skills / "c2.md").symlink_to(ada / "c3.md")
        (ada / "c3.md").symlink_to(target_file)
        return "skills/c1.md", False
    if kind == "ancestor":
        (skills / "anc").symlink_to(Path("..") / "..")
        return f"skills/anc/p-{B}/skills/shut.md", False
    if kind == "case":
        return str(Path(*[p.upper() for p in own_data.relative_to(ada).parts])), True
    if kind == "folder-absolute":
        (skills / "f").symlink_to(target_file.parent)
        return f"skills/f/{target_file.name}", False
    if kind == "folder-relative":
        (skills / "g").symlink_to(Path(os.path.relpath(own_data.parent, skills)))
        return f"skills/g/{own_data.name}", True
    if kind == "dangling-into":
        (skills / "d.md").symlink_to(target_file.parent / "not-yet.md")
        return "skills/d.md", False
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", HOP_KINDS)
def test_every_hop_kind_is_decided_on_where_it_lands(tmp_path: Path, kind: str) -> None:
    """One table, both doors. Each kind of hop is made inside Ada's folder; ``path`` and
    ``publish`` must answer by the landing, never by the name: a landing in Sam's folder or
    above Ada's is ``CrossPrincipalError`` from both; a landing on Ada's own data passes
    ``path`` and is ``SharedDataError`` from ``publish``. Nothing is ever written."""
    root = _household(tmp_path)
    sam_file = _plant(root, B, "skills/shut.md", CANARY)
    own_data = _plant(root, A, "hot/learned.md", CANARY)
    source, inside = _hop(root, kind, target_file=sam_file, own_data=own_data)
    before = _tree(root)
    profile = Profile(bind(root, A))
    if inside:
        landed = profile.path(source)
        # Compared case-folded: on a case-sensitive filesystem the ``case`` kind names a file
        # that does not exist, and the landing is the name as given.
        assert landed.as_posix().lower() == own_data.resolve().as_posix().lower()
        with pytest.raises(SharedDataError):
            publish(bind(root, A), source, "skills", environ=ON)
    else:
        with pytest.raises(CrossPrincipalError):
            profile.path(source)
        with pytest.raises(CrossPrincipalError):
            publish(bind(root, A), source, "skills", environ=ON)
    assert _tree(root) == before
    assert not (root / "shared").exists()


def test_open_follows_no_link_at_open_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution decides; the open walks the resolved components relative to the folder's
    descriptor and refuses a link at any of them. A folder swapped for a link between the two
    is ``CrossPrincipalError``, not a read of what it points at."""
    root = _household(tmp_path)
    ada = principal_dir(root, A)
    _plant(root, A, "notes/a.md", b"mine")
    _plant(root, B, "notes/a.md", CANARY)
    profile = Profile(bind(root, A))
    with profile.open("notes/a.md") as f:
        assert f.read() == b"mine"
    real_resolve = Profile.resolve

    def swap_after(self: Profile, relative: Any) -> Any:
        resolved = real_resolve(self, relative)
        import shutil

        shutil.rmtree(ada / "notes")
        (ada / "notes").symlink_to(principal_dir(root, B) / "notes")
        return resolved

    monkeypatch.setattr(Profile, "resolve", swap_after)
    with pytest.raises(CrossPrincipalError):
        profile.open("notes/a.md")
