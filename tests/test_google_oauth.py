"""What the two Google connectors share (issue #136): the one check that a grant is no wider
than asked, and the scrub that keeps a secret and a line break out of an error message."""

from __future__ import annotations

import ast
import inspect

import walle.gmail as gmail
import walle.google_calendar as gcal
from walle import google_oauth
from walle.google_oauth import scrubbed, wider_than

READ = "https://www.googleapis.com/auth/example.readonly"
WRITE = "https://www.googleapis.com/auth/example"


def test_a_grant_no_wider_than_asked_is_nothing_to_report() -> None:
    assert wider_than((READ,), {"scope": READ}) is None
    assert wider_than((READ,), {"scope": f"  {READ}  "}) is None
    assert wider_than((READ, WRITE), {"scope": f"{WRITE} {READ}"}) is None


def test_a_reply_without_a_scope_is_taken_as_asked() -> None:
    """A refresh reply may omit ``scope``; the grant is then the one already held."""
    assert wider_than((READ,), {}) is None
    assert wider_than((READ,), {"scope": None}) is None
    assert wider_than((READ,), {"scope": 7}) is None


def test_the_extra_scopes_are_named_sorted_on_one_line_and_cut() -> None:
    extra = wider_than((READ,), {"scope": f"{WRITE} {READ} zeta\nalpha"})
    assert extra == f"alpha {WRITE} zeta"
    long = wider_than((READ,), {"scope": " ".join(f"https://example.com/{n}" for n in range(50))})
    assert long is not None and len(long) <= 200


def test_scrubbed_replaces_every_secret_folds_lines_and_cuts() -> None:
    text = "bad\nsecret=S3CRET code=C0DE more"
    out = scrubbed(text, ("S3CRET", "C0DE", ""), limit=80)
    assert out == "bad secret=[redacted] code=[redacted] more"
    assert len(scrubbed("x" * 500, (), limit=80)) == 80
    assert scrubbed("  spaced   out  ", ()) == "spaced out"


def test_both_connectors_call_this_check_and_neither_keeps_its_own() -> None:
    for module in (gmail, gcal):
        tree = ast.parse(inspect.getsource(module._token_from))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "wider_than" in called, module.__name__
        assert module.wider_than is google_oauth.wider_than
        assert "_granted_too_much" not in inspect.getsource(module)
        source = inspect.getsource(module.exchange_code)
        assert "scrubbed(" in source, module.__name__


def test_the_shared_module_names_no_scope_and_imports_no_network() -> None:
    source = inspect.getsource(google_oauth)
    assert "googleapis.com/auth/" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "collections", "typing"}, imported
