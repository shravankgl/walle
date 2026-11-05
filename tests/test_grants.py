"""Issue #99's verify criteria, and the cases around them (D62, D73, D116, D72).

  request ends at the run    a grant for this request covers the exact call for the rest of
  boundary                   the request and nothing after ``next_request``
  always degrades to         an ``always`` whose file cannot be written becomes a session
  session and says so        grant, in memory, and ``Granted.note`` says why
  config is human-edited     ``grant`` refuses ``config``, nothing here ever writes it, and a
                             rule in it is read as a rule only when every line parses
  once, never always         a destructive call is offered ``request`` only, its key carries
                             its call id, and no file can ever cover it
  what is checked is what    keys are exact bytes, computed by one function on both sides,
  is used                    and a near miss is refused
  fail closed                a malformed line, a link, a newer version, an unreadable file
                             or a wrong type never produces a grant
  off by default             with the switch unset nothing is granted, nothing is written
                             and every check refuses

Every root is a ``root``. Every tool, argument, session and request is invented.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from walle import floors, grants, reviewer
from walle.grants import (
    Allowed,
    Call,
    Granted,
    GrantsOffError,
    Live,
    NotGrantableError,
    Refused,
    Scope,
    check,
    grant,
    key_for,
    offered,
)
from walle.paths import principal_dir

ON = {grants.GRANTS_SWITCH: grants.SWITCH_ON}
P = "lena"
TS = "2026-11-05T10:00:00+00:00"
LIVE = Live(session="s-aaaa", request="r-1")
LS = Call(tool="shell", arguments="ls -la ~/allotment")
PRUNE = Call(tool="shell", arguments="rm -rf ~/allotment/old", destructive=True)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A root with the principal's folder and nothing in it: no log, no grants yet."""
    principal_dir(tmp_path, P).mkdir(parents=True)
    return tmp_path


def _grants_dir(root: Path) -> Path:
    return grants.grants_dir(root, P)


def _config(root: Path, *rules: dict[str, object]) -> Path:
    folder = _grants_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / grants.CONFIG_FILE
    path.write_text("".join(json.dumps(r) + "\n" for r in rules))
    return path


def _grant(
    live: Live, call: Call, scope: Scope, root: Path, *, call_id: str = "c-1", ts: str = TS
) -> Granted:
    return grant(live, call, scope, call_id=call_id, root=root, principal=P, ts=ts, environ=ON)


def _check(live: Live, call: Call, root: Path, *, call_id: str = "c-1") -> Allowed | Refused:
    return check(live, call, call_id=call_id, root=root, principal=P, environ=ON)


# --- the verify criterion: a request grant ends at the run boundary ---------------------------


def test_a_request_grant_covers_the_exact_call_for_the_rest_of_the_request(root: Path) -> None:
    before = _check(LIVE, LS, root)
    assert isinstance(before, Refused)
    given = _grant(LIVE, LS, Scope.REQUEST, root)
    assert given.scope is Scope.REQUEST and not given.degraded
    again = _check(given.live, LS, root, call_id="c-2")
    assert isinstance(again, Allowed)
    assert again.scope is Scope.REQUEST


def test_a_request_grant_ends_at_the_run_boundary(root: Path) -> None:
    """The verify criterion. The next request of the same session has no grant."""
    given = _grant(LIVE, LS, Scope.REQUEST, root)
    assert isinstance(_check(given.live, LS, root), Allowed)
    after = given.live.next_request("r-2")
    assert after.session == LIVE.session
    assert after.grants == ()
    refused = _check(after, LS, root)
    assert isinstance(refused, Refused)
    assert "no grant" in refused.reason


def test_a_stale_request_grant_left_in_memory_still_does_not_apply(root: Path) -> None:
    """Belt and braces: even if a caller forgets ``next_request``, a request grant is checked
    against the request it was given in, not merely against its presence."""
    given = _grant(LIVE, LS, Scope.REQUEST, root)
    stale = Live(session=LIVE.session, request="r-2", grants=given.live.grants)
    assert isinstance(_check(stale, LS, root), Refused)


def test_a_session_grant_outlives_the_request_and_not_the_session(root: Path) -> None:
    given = _grant(LIVE, LS, Scope.SESSION, root)
    assert given.scope is Scope.SESSION
    later = given.live.next_request("r-9")
    allowed = _check(later, LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.SESSION
    other_session = Live(session="s-bbbb", request="r-1", grants=later.grants)
    assert isinstance(_check(other_session, LS, root), Refused)
    fresh = Live.start("s-bbbb", "r-1")
    assert fresh.grants == ()
    assert isinstance(_check(fresh, LS, root), Refused)


def test_a_request_or_session_grant_writes_nothing(root: Path) -> None:
    _grant(LIVE, LS, Scope.REQUEST, root)
    _grant(LIVE, LS, Scope.SESSION, root)
    assert not _grants_dir(root).exists()
    assert list(principal_dir(root, P).iterdir()) == []


def test_an_always_for_a_principal_with_no_folder_degrades_and_makes_none(tmp_path: Path) -> None:
    """A misspelt principal must not get a profile folder (as the inbox refuses one). The
    grant degrades to session and the note names the error."""
    given = _grant(LIVE, LS, Scope.ALWAYS, tmp_path)
    assert given.degraded and given.scope is Scope.SESSION
    assert "FileNotFoundError" in given.note
    assert list(tmp_path.iterdir()) == []


# --- the verify criterion: always degrades to session and says so -----------------------------


def test_an_always_grant_is_durable_across_sessions(root: Path) -> None:
    given = _grant(LIVE, LS, Scope.ALWAYS, root)
    assert given.scope is Scope.ALWAYS and not given.degraded
    assert given.rule is not None
    assert given.note == ""
    fresh = Live.start("s-bbbb", "r-1")
    allowed = _check(fresh, LS, root)
    assert isinstance(allowed, Allowed)
    assert allowed.scope is Scope.ALWAYS
    lines = (_grants_dir(root) / grants.ALWAYS_FILE).read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {
        "v": 1,
        "tool": "shell",
        "arguments": "ls -la ~/allotment",
        "given_at": TS,
        "session": LIVE.session,
        "request": LIVE.request,
    }


def test_an_always_that_cannot_be_saved_drops_to_session_and_says_so(root: Path) -> None:
    """The verify criterion. A file sits where the grants folder should be, so nothing can be
    written there, on any account."""
    grants.grants_dir(root, P).write_text("not a folder")
    given = _grant(LIVE, LS, Scope.ALWAYS, root)
    assert given.asked is Scope.ALWAYS
    assert given.scope is Scope.SESSION
    assert given.degraded
    assert given.rule is None
    assert "always" in given.note and "session" in given.note
    assert "could not be saved" in given.note
    assert "NotADirectoryError" in given.note or "FileExistsError" in given.note
    # the session grant is really there, and is a session grant: it survives the request
    later = given.live.next_request("r-2")
    allowed = _check(later, LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.SESSION
    # and nothing durable exists
    assert isinstance(_check(Live.start("s-bbbb", "r-1"), LS, root), Refused)


def test_the_note_names_the_error_class_never_its_message(root: Path) -> None:
    grants.grants_dir(root, P).write_text("not a folder")
    given = _grant(LIVE, LS, Scope.ALWAYS, root)
    assert str(root) not in given.note
    assert "Not a directory" not in given.note


def _permissions_hold(root: Path) -> bool:
    """Whether this account is refused by a read-only folder at all. The superuser, and some
    sandboxes, are not; the test is skipped rather than passed on them."""
    probe = root / "probe"
    probe.mkdir()
    probe.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        (probe / "x").write_text("")
    except PermissionError:
        return True
    finally:
        probe.chmod(stat.S_IRWXU)
    return False


def test_a_read_only_grants_folder_degrades_too(root: Path) -> None:
    if not _permissions_hold(root):
        pytest.skip("this account is not refused by a read-only folder")
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        given = _grant(LIVE, LS, Scope.ALWAYS, root)
    finally:
        folder.chmod(stat.S_IRWXU)
    assert given.degraded and given.scope is Scope.SESSION
    assert "PermissionError" in given.note
    assert not (folder / grants.ALWAYS_FILE).exists()


def test_an_always_file_that_is_a_link_is_not_written_through(root: Path) -> None:
    """A link in place of the rule file would carry a grant somewhere else. Not written, and
    the grant degrades."""
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    elsewhere = root / "elsewhere.jsonl"
    elsewhere.write_text("")
    (folder / grants.ALWAYS_FILE).symlink_to(elsewhere)
    given = _grant(LIVE, LS, Scope.ALWAYS, root)
    assert given.degraded and given.scope is Scope.SESSION
    assert elsewhere.read_text() == ""


def test_a_degraded_grant_is_not_silently_retried_as_always(root: Path) -> None:
    """Granting again after a failure asks for ``always`` again and fails again; the person
    is told each time. Nothing promotes the session grant behind their back."""
    grants.grants_dir(root, P).write_text("not a folder")
    first = _grant(LIVE, LS, Scope.ALWAYS, root)
    second = _grant(first.live, LS, Scope.ALWAYS, root, call_id="c-2")
    assert second.degraded
    assert all(g.scope is Scope.SESSION for g in second.live.grants)


# --- the verify criterion: config is human-edited only ----------------------------------------


def test_config_cannot_be_granted(root: Path) -> None:
    with pytest.raises(NotGrantableError, match="human"):
        _grant(LIVE, LS, Scope.CONFIG, root)
    assert not _grants_dir(root).exists()


def test_config_is_never_offered() -> None:
    assert Scope.CONFIG not in offered(LS)
    assert Scope.CONFIG not in offered(PRUNE)


def test_a_config_rule_is_read_and_covers_the_exact_call(root: Path) -> None:
    path = _config(root, {"tool": "shell", "arguments": "ls -la ~/allotment"})
    before = path.read_bytes()
    allowed = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.CONFIG
    assert path.read_bytes() == before


def test_nothing_in_the_module_writes_the_config_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write goes through ``_append``; it is watched, and the config path never
    appears. The config bytes are the same before and after a grant at every scope."""
    path = _config(root, {"tool": "shell", "arguments": "ls -la ~/allotment"})
    before = path.read_bytes()
    written: list[Path] = []
    real = grants._append

    def watching(target: Path, rule: grants.AlwaysRule) -> None:
        written.append(target)
        real(target, rule)

    monkeypatch.setattr(grants, "_append", watching)
    live = LIVE
    for scope in offered(LS):
        live = _grant(live, LS, scope, root).live
    with pytest.raises(NotGrantableError):
        _grant(live, LS, Scope.CONFIG, root)
    _check(live, LS, root)
    assert written == [_grants_dir(root) / grants.ALWAYS_FILE]
    assert path.read_bytes() == before
    assert sorted(p.name for p in _grants_dir(root).iterdir()) == sorted(
        {grants.CONFIG_FILE, grants.ALWAYS_FILE, grants.LOCK_FILE}
    )


def test_both_grant_files_sit_under_a_floor() -> None:
    """D72: the agent cannot write either file, in any mode. The floor table already names
    ``principals/*/grants``; this pins the two paths this module uses to it."""
    for name in (grants.CONFIG_FILE, grants.ALWAYS_FILE):
        found = floors.hit(f"principals/p-{P}/grants/{name}")
        assert found is not None and found.how == "under"
        assert found.protected == f"principals/p-{P}/grants"


def test_a_config_file_with_one_bad_line_grants_nothing(root: Path) -> None:
    """Fail closed: a file that does not parse whole is not a rule file. The good line beside
    the bad one does not apply, and the problem is reported."""
    _config(root, {"tool": "shell", "arguments": "ls -la ~/allotment"})
    path = _grants_dir(root) / grants.CONFIG_FILE
    with path.open("a") as f:
        f.write("{not json\n")
    refused = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(refused, Refused)
    assert any(grants.CONFIG_FILE in p and "line 2" in p for p in refused.problems)


@pytest.mark.parametrize(
    "row",
    [
        {"tool": "shell", "arguments": "ls -la ~/allotment", "extra": 1},
        {"tool": "shell"},
        {"tool": "sh ell", "arguments": "ls"},
        {"tool": "shell", "arguments": 7},
        {"tool": "shell", "arguments": "ls -la ~/allotment", "v": 2},
        {"tool": "shell", "arguments": "ls -la ~/allotment", "v": 0},
        {"tool": "shell", "arguments": "ls -la ~/allotment", "v": "1"},
        ["shell", "ls -la ~/allotment"],
        "shell ls",
        None,
    ],
    ids=[
        "extra-field",
        "missing-field",
        "bad-tool-name",
        "arguments-not-a-string",
        "newer-version",
        "version-zero",
        "version-as-string",
        "a-list",
        "a-string",
        "null",
    ],
)
def test_a_rule_that_does_not_validate_grants_nothing(root: Path, row: object) -> None:
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    (folder / grants.CONFIG_FILE).write_text(json.dumps(row) + "\n")
    refused = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(refused, Refused)
    assert refused.problems and all(grants.CONFIG_FILE in p for p in refused.problems)


def test_an_always_file_from_a_newer_build_grants_nothing(root: Path) -> None:
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    row = {
        "v": 2,
        "tool": "shell",
        "arguments": "ls -la ~/allotment",
        "given_at": TS,
        "session": "s-aaaa",
        "request": "r-1",
    }
    (folder / grants.ALWAYS_FILE).write_text(json.dumps(row) + "\n")
    refused = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(refused, Refused)
    assert any("v2" in p and grants.ALWAYS_FILE in p for p in refused.problems)


def test_a_config_file_that_is_a_link_grants_nothing(root: Path) -> None:
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    elsewhere = root / "elsewhere.jsonl"
    elsewhere.write_text(json.dumps({"tool": "shell", "arguments": "ls -la ~/allotment"}) + "\n")
    (folder / grants.CONFIG_FILE).symlink_to(elsewhere)
    refused = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(refused, Refused)
    assert any("link" in p for p in refused.problems)


def test_a_line_nested_past_the_recursion_limit_grants_nothing(root: Path) -> None:
    """The reviewer's lesson (#94): deep nesting is a ``RecursionError`` out of ``json``, and
    an exception out of ``check`` would be a crash where a refusal was owed."""
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    (folder / grants.CONFIG_FILE).write_text("[" * 200_000 + "\n")
    refused = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(refused, Refused)
    assert any("line 1" in p for p in refused.problems)


def test_two_kernels_saving_always_at_once_leave_two_whole_lines(root: Path) -> None:
    """The walkthrough: each line is appended whole under the lock, and the file parses."""
    import threading

    calls = [Call(tool="shell", arguments=f"ls -la ~/allotment/{i}") for i in range(8)]
    results: list[Granted] = []

    def save(call: Call) -> None:
        results.append(_grant(LIVE, call, Scope.ALWAYS, root))

    threads = [threading.Thread(target=save, args=(c,)) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(not g.degraded for g in results)
    lines = (_grants_dir(root) / grants.ALWAYS_FILE).read_text().splitlines()
    assert sorted(json.loads(line)["arguments"] for line in lines) == sorted(
        c.arguments for c in calls
    )
    fresh = Live.start("s-bbbb", "r-1")
    assert all(isinstance(_check(fresh, c, root), Allowed) for c in calls)


def test_a_rule_file_that_cannot_be_read_grants_nothing(root: Path) -> None:
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    (folder / grants.CONFIG_FILE).mkdir()  # a folder where the file should be
    refused = _check(Live.start("s-bbbb", "r-1"), LS, root)
    assert isinstance(refused, Refused)
    assert any("IsADirectoryError" in p for p in refused.problems)


def test_a_missing_rule_file_is_not_a_problem(root: Path) -> None:
    refused = _check(LIVE, LS, root)
    assert isinstance(refused, Refused)
    assert refused.problems == ()


def test_an_empty_line_in_a_rule_file_is_skipped(root: Path) -> None:
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    body = "\n" + json.dumps({"tool": "shell", "arguments": "ls -la ~/allotment"}) + "\n\n"
    (folder / grants.CONFIG_FILE).write_text(body)
    assert isinstance(_check(LIVE, LS, root), Allowed)


def test_a_broken_always_file_does_not_void_a_session_grant(root: Path) -> None:
    """Fail closed is about the grant that cannot be resolved. The session grant the person
    just gave is resolvable, so it still applies, and the problem is still reported."""
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    (folder / grants.ALWAYS_FILE).write_text("{broken\n")
    given = _grant(LIVE, LS, Scope.SESSION, root)
    allowed = _check(given.live, LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.SESSION
    assert allowed.problems and grants.ALWAYS_FILE in allowed.problems[0]


# --- D116: once, never always ------------------------------------------------------------------


def test_a_destructive_call_is_offered_request_only() -> None:
    assert offered(PRUNE) == (Scope.REQUEST,)
    assert offered(LS) == (Scope.REQUEST, Scope.SESSION, Scope.ALWAYS)


@pytest.mark.parametrize(
    ("scope", "why"),
    [(Scope.SESSION, "destructive"), (Scope.ALWAYS, "destructive"), (Scope.CONFIG, "human")],
)
def test_a_destructive_call_cannot_be_granted_wider_than_once(
    root: Path, scope: Scope, why: str
) -> None:
    with pytest.raises(NotGrantableError, match=why):
        _grant(LIVE, PRUNE, scope, root)
    assert not _grants_dir(root).exists()


def test_a_destructive_grant_is_keyed_to_the_one_call(root: Path) -> None:
    """The rule such an approval writes is keyed to that one call, so it matches nothing
    afterwards (D116): not the same command from the next call, in the same request."""
    given = _grant(LIVE, PRUNE, Scope.REQUEST, root, call_id="c-7")
    assert given.grant is not None and given.grant.key.call == "c-7"
    assert isinstance(_check(given.live, PRUNE, root, call_id="c-7"), Allowed)
    assert isinstance(_check(given.live, PRUNE, root, call_id="c-8"), Refused)
    assert isinstance(_check(given.live.next_request("r-2"), PRUNE, root, call_id="c-7"), Refused)


def test_no_file_covers_a_destructive_call(root: Path) -> None:
    """A rule in either file matching a destructive command byte for byte is not a grant for
    it: once is the ceiling, and a file is never once."""
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    row = {"tool": PRUNE.tool, "arguments": PRUNE.arguments}
    (folder / grants.CONFIG_FILE).write_text(json.dumps(row) + "\n")
    always = {**row, "given_at": TS, "session": "s-aaaa", "request": "r-1"}
    (folder / grants.ALWAYS_FILE).write_text(json.dumps(always) + "\n")
    refused = _check(Live.start("s-bbbb", "r-1"), PRUNE, root)
    assert isinstance(refused, Refused)
    assert "destructive" in refused.reason
    # the same bytes, not destructive, are covered by the config rule
    same = Call(tool=PRUNE.tool, arguments=PRUNE.arguments)
    assert isinstance(_check(Live.start("s-bbbb", "r-1"), same, root), Allowed)


def test_a_destructive_key_cannot_be_held_in_memory_wider_than_request() -> None:
    """A ``Grant`` built by hand is held to D116 too, so nothing that constructs one can
    widen a once into a session."""
    with pytest.raises(ValueError, match="once"):
        grants.Grant(key_for(PRUNE, "c-1"), Scope.SESSION, "s-aaaa", "r-1")
    grants.Grant(key_for(PRUNE, "c-1"), Scope.REQUEST, "s-aaaa", "r-1")


def test_a_grant_for_the_plain_call_does_not_cover_it_marked_destructive(root: Path) -> None:
    """The flag is part of the key. A grant given when a tool was declared safe does not
    carry over once it is declared destructive (D75: overrides only tighten)."""
    plain = Call(tool=PRUNE.tool, arguments=PRUNE.arguments)
    given = _grant(LIVE, plain, Scope.SESSION, root)
    assert isinstance(_check(given.live, plain, root), Allowed)
    assert isinstance(_check(given.live, PRUNE, root), Refused)


# --- what is checked is what is used ----------------------------------------------------------


@pytest.mark.parametrize(
    "near",
    [
        Call(tool="shell", arguments="ls -la ~/allotment "),
        Call(tool="shell", arguments="ls -la ~/Allotment"),
        Call(tool="shell", arguments="ls  -la ~/allotment"),
        Call(tool="shell", arguments="ls -la ~/allotment\n"),
        Call(tool="shell", arguments="ls -la ~/allotment; rm -rf ~"),
        Call(tool="Shell", arguments="ls -la ~/allotment"),
        Call(tool="shell.run", arguments="ls -la ~/allotment"),
        Call(tool="shell", arguments="ls -la \u00a0~/allotment"),
    ],
    ids=[
        "trailing-space",
        "case",
        "double-space",
        "newline",
        "appended",
        "tool-case",
        "tool",
        "nbsp",
    ],
)
def test_a_near_miss_is_not_the_call(root: Path, near: Call) -> None:
    live = LIVE
    for scope in offered(LS):
        live = _grant(live, LS, scope, root).live
    _config(root, {"tool": LS.tool, "arguments": LS.arguments})
    assert isinstance(_check(live, LS, root), Allowed)
    assert isinstance(_check(live, near, root), Refused)


def test_the_key_is_one_function_on_both_sides(root: Path) -> None:
    given = _grant(LIVE, LS, Scope.SESSION, root)
    assert given.grant is not None
    assert given.grant.key == key_for(LS, "c-1")
    allowed = _check(given.live, LS, root, call_id="c-2")
    assert isinstance(allowed, Allowed)
    assert allowed.key == given.grant.key
    assert key_for(LS, "c-1") == key_for(LS, "c-2")  # the call id is not in a plain key
    assert key_for(PRUNE, "c-1") != key_for(PRUNE, "c-2")


def test_the_narrowest_covering_scope_is_reported(root: Path) -> None:
    _config(root, {"tool": LS.tool, "arguments": LS.arguments})
    live = _grant(LIVE, LS, Scope.ALWAYS, root).live
    live = _grant(live, LS, Scope.SESSION, root).live
    live = _grant(live, LS, Scope.REQUEST, root).live
    allowed = _check(live, LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.REQUEST
    allowed = _check(live.next_request("r-2"), LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.SESSION
    allowed = _check(Live.start("s-zzzz", "r-1"), LS, root)
    assert isinstance(allowed, Allowed) and allowed.scope is Scope.ALWAYS


def test_the_tool_name_shape_is_the_reviewers() -> None:
    """One shape for a tool name across the permission layer, so what the reviewer is shown
    and what a grant is keyed by cannot drift."""
    assert grants.TOOL_NAME_RE.pattern == reviewer.TOOL_NAME_RE.pattern
    assert grants.TOOL_NAME_CHARS == reviewer.TOOL_NAME_CHARS


# --- types up front ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tool": 1, "arguments": "ls"},
        {"tool": "shell", "arguments": b"ls"},
        {"tool": "shell", "arguments": "ls", "destructive": 1},
        {"tool": "shell", "arguments": "ls", "destructive": "yes"},
        {"tool": "shell", "arguments": None},
    ],
    ids=["tool-int", "arguments-bytes", "destructive-int", "destructive-str", "arguments-none"],
)
def test_a_call_with_a_wrong_type_is_a_type_error(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        Call(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("tool", ["", "sh ell", "shell\n", "shell;", "x" * 65, "1shell", ".shell"])
def test_a_call_with_a_bad_tool_name_is_a_value_error(tool: str) -> None:
    with pytest.raises(ValueError):
        Call(tool=tool, arguments="ls")


def test_a_call_with_a_nul_in_its_arguments_is_refused() -> None:
    with pytest.raises(ValueError, match="NUL"):
        Call(tool="shell", arguments="ls\x00-la")


def test_a_scope_given_as_a_string_is_a_type_error(root: Path) -> None:
    """``"always"`` is not ``Scope.ALWAYS``: a plain string could come from anywhere, and a
    permission call takes the type or nothing."""
    with pytest.raises(TypeError):
        grant(LIVE, LS, "always", call_id="c-1", root=root, principal=P, ts=TS, environ=ON)  # type: ignore[arg-type]
    assert not _grants_dir(root).exists()


def test_a_call_or_live_of_the_wrong_type_is_a_type_error(root: Path) -> None:
    with pytest.raises(TypeError):
        _grant(LIVE, ("shell", "ls"), Scope.REQUEST, root)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _check(("s", "r"), LS, root)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _check(LIVE, {"tool": "shell"}, root)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "c 1", "c/1", "c-1\n", "x" * 129])
def test_a_bad_call_id_is_refused_before_anything(root: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _grant(LIVE, LS, Scope.ALWAYS, root, call_id=bad)
    with pytest.raises(ValueError):
        _check(LIVE, LS, root, call_id=bad)
    assert not _grants_dir(root).exists()


@pytest.mark.parametrize("bad", ["", "s 1", "s/1", 7, None])
def test_a_live_with_a_bad_session_or_request_is_refused(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Live(session=bad, request="r-1")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        Live(session="s-1", request=bad)  # type: ignore[arg-type]


def test_a_live_holding_something_that_is_not_a_grant_is_refused() -> None:
    with pytest.raises(TypeError):
        Live(session="s-1", request="r-1", grants=("shell",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Live(session="s-1", request="r-1", grants=[])  # type: ignore[arg-type]


def test_a_bad_timestamp_is_refused_before_anything_is_written(root: Path) -> None:
    with pytest.raises(ValueError):
        _grant(LIVE, LS, Scope.ALWAYS, root, ts="yesterday")
    with pytest.raises(ValueError):
        _grant(LIVE, LS, Scope.ALWAYS, root, ts="2026-11-05T10:00:00")  # naive
    assert not _grants_dir(root).exists()


def test_a_bad_principal_is_refused_before_anything(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        grant(
            LIVE,
            LS,
            Scope.ALWAYS,
            call_id="c-1",
            root=tmp_path,
            principal="../x",
            ts=TS,
            environ=ON,
        )
    with pytest.raises(ValueError):
        check(LIVE, LS, call_id="c-1", root=tmp_path, principal="Lena", environ=ON)
    assert list(tmp_path.iterdir()) == []


# --- the dormant rule --------------------------------------------------------------------------


def test_grants_are_off_by_default(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's condition. Switch unset: nothing is granted, nothing is written, and a
    check refuses even with a grant in memory and a rule in config."""
    monkeypatch.delenv(grants.GRANTS_SWITCH, raising=False)
    assert not grants.enabled()
    assert not grants.enabled({})
    _config(root, {"tool": LS.tool, "arguments": LS.arguments})
    with pytest.raises(GrantsOffError, match=grants.GRANTS_SWITCH):
        grant(LIVE, LS, Scope.ALWAYS, call_id="c-1", root=root, principal=P, ts=TS)
    with pytest.raises(GrantsOffError):
        grant(LIVE, LS, Scope.REQUEST, call_id="c-1", root=root, principal=P, ts=TS, environ={})
    assert not (_grants_dir(root) / grants.ALWAYS_FILE).exists()
    in_memory = Live(
        session=LIVE.session,
        request=LIVE.request,
        grants=(grants.Grant(key_for(LS, "c-1"), Scope.SESSION, LIVE.session, LIVE.request),),
    )
    off = check(in_memory, LS, call_id="c-1", root=root, principal=P)
    assert isinstance(off, Refused)
    assert "off" in off.reason and grants.GRANTS_SWITCH in off.reason
    still_off = check(in_memory, LS, call_id="c-1", root=root, principal=P, environ={})
    assert isinstance(still_off, Refused)


@pytest.mark.parametrize("value", ["1", "true", "ON", "On", "yes", " on", "on "])
def test_only_the_one_spelling_turns_grants_on(value: str) -> None:
    assert not grants.enabled({grants.GRANTS_SWITCH: value})
    assert grants.enabled({grants.GRANTS_SWITCH: "on"})


def test_while_off_nothing_is_read(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off is checked before any file: a rule file that would blow up on read is never
    opened."""
    folder = _grants_dir(root)
    folder.mkdir(parents=True)
    (folder / grants.CONFIG_FILE).mkdir()

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("a rule file was opened while off")

    monkeypatch.setattr(grants, "_load", boom)
    off = check(LIVE, LS, call_id="c-1", root=root, principal=P, environ={})
    assert isinstance(off, Refused)


def test_the_module_starts_nothing_and_reads_no_clock() -> None:
    """Like the floors: no clock, no environment read outside ``enabled``, no command."""
    source = (Path(__file__).parent.parent / "src" / "walle" / "grants.py").read_text()
    for forbidden in ("now_instant", "datetime", "time.", "subprocess", "click", "threading"):
        assert forbidden not in source, forbidden
    assert source.count("os.environ") == 1
