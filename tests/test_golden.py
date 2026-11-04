"""Task 24's verify criterion: rebuild over every golden fixture equals its hash (D88, D96).

A lifetime log holds events written by every schema version walle ever shipped, and the
current build must load all of them forever (D88). A golden fixture is a log written by one
shipped version, frozen in the repo and never regenerated. This file is the compatibility
check: CI runs ``uv run pytest``, so it runs on every push and every PR.

Layout
------
``tests/fixtures/v<N>/<name>/`` is a walle root. It holds ``principals/p-<who>/log/*.jsonl``
and nothing derived, plus ``__expected__.json``:

``principal``  whose log it is.
``events``     how many events the log holds.
``state_hash`` ``walle.index.state_hash`` of the resolved state, as the shipping build saw it.
``fold``       the refs the resolver put in ``current``, ``retired``, ``conflicts``,
               ``unapplied`` and ``staged``: the hash in a form a person can read, and the
               first thing to diff when the hash moves.
``sentinels``  dotted paths to the values they must still hold after a rebuild:
               ``"d-mbp:1.source_version": "2.1.0"``. The path is a ref, then field names or
               list indexes. They catch *silent* data loss, which the hash cannot: the hash
               covers assertions, so an observation that quietly lost a field, or a field the
               fold never reads, would leave it unchanged (D88).

``vN`` names the event schema version the fixture's events carry (D96 versions each type).
Every type this build knows needs a fixture at every version up to its current one, so a
bump that ships without a golden log for the old shape fails here.

A fixture is copied to ``tmp_path`` before anything runs, so the repo copy is never written
to and the suite needs no write access to it.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from walle.events import EVENT_TYPES
from walle.index import load_records, rebuild, state_hash, update
from walle.paths import index_path, log_dir
from walle.reader import Record, read_file
from walle.resolver import resolve, staged

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = "__expected__.json"
EXPECTED_KEYS = {"principal", "events", "state_hash", "fold", "sentinels"}
FOLD_KEYS = {"current", "retired", "conflicts", "unapplied", "staged"}


def _golden() -> list[Path]:
    return sorted(p.parent for p in FIXTURES.glob(f"v[0-9]*/*/{EXPECTED}"))


GOLDEN = _golden()
IDS = [f"{p.parent.name}/{p.name}" for p in GOLDEN]


def _expected(fixture: Path) -> dict[str, Any]:
    return json.loads((fixture / EXPECTED).read_text(encoding="utf-8"))


def _copy(fixture: Path, tmp_path: Path) -> tuple[Path, str]:
    """The fixture as a fresh walle root under ``tmp_path``, and its principal."""
    root = tmp_path / "root"
    shutil.copytree(fixture, root, ignore=shutil.ignore_patterns(EXPECTED))
    # copytree keeps the checkout's modes. A read-only checkout would make the copy
    # read-only too, and the rebuild would fail for a reason that has nothing to do with
    # compatibility, so the copy is made the current user's to write.
    for path in (root, *root.rglob("*")):
        path.chmod(path.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    return root, _expected(fixture)["principal"]


def _from_log(root: Path, principal: str) -> list[Record]:
    """Every record read straight from the JSONL, no index involved."""
    log = log_dir(root, principal)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _fold(records: list[Record] | tuple[Record, ...]) -> dict[str, list[str]]:
    resolution = resolve(records)
    return {
        "current": [b.ref for b in resolution.current],
        "retired": [r.ref for r in resolution.retired],
        "conflicts": [c.ref for c in resolution.conflicts],
        "unapplied": [u.ref for u in resolution.unapplied],
        "staged": [s.ref for s in staged(records)],
    }


_MISSING = object()


def _lookup(events: dict[str, dict[str, Any]], path: str) -> Any:
    """The value at ``<ref>.<field>[.<field or index>...]``, or ``_MISSING``."""
    ref, *parts = path.split(".")
    node: Any = events.get(ref, _MISSING)
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isascii() and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def sentinel_failures(records: tuple[Record, ...], sentinels: dict[str, Any]) -> list[str]:
    """One line per sentinel whose path is gone or holds another value. Empty when all hold.

    Events are compared in their loaded form, ``model_dump(mode="json")``, so a field the
    upcast chain or the index dropped reads as its default and fails here.
    """
    events = {r.ref: r.event.model_dump(mode="json") for r in records}
    failures = []
    for path, want in sorted(sentinels.items()):
        if path.split(".")[0] not in events or "." not in path:
            failures.append(f"{path}: no such event, or no field named")
            continue
        got = _lookup(events, path)
        if got is _MISSING:
            failures.append(f"{path}: missing after the rebuild, expected {want!r}")
        elif got != want or type(got) is not type(want):
            failures.append(f"{path}: {got!r} after the rebuild, expected {want!r}")
    return failures


# --- the set of fixtures ----------------------------------------------------------------


def test_there_are_golden_fixtures() -> None:
    assert GOLDEN, f"no golden fixtures under {FIXTURES}/v*/ (D88)"


def test_every_event_type_has_a_golden_log_at_every_version_it_has_shipped() -> None:
    """A schema bump without a frozen log of the old shape is the hole D88 closes."""
    seen: set[tuple[str, int, str]] = set()
    for fixture in GOLDEN:
        for record in _from_log(fixture, _expected(fixture)["principal"]):
            seen.add((record.event.type, record.event.v, fixture.parent.name))
    missing = [
        f"{name} v{v} under tests/fixtures/v{v}/"
        for name, cls in sorted(EVENT_TYPES.items())
        for v in range(1, cls.SCHEMA_VERSION + 1)
        if (name, v, f"v{v}") not in seen
    ]
    assert not missing, "no golden log for: " + ", ".join(missing)


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_a_fixture_holds_a_log_and_its_expectations_and_nothing_derived(fixture: Path) -> None:
    expected = _expected(fixture)
    assert set(expected) == EXPECTED_KEYS
    assert set(expected["fold"]) == FOLD_KEYS
    assert expected["sentinels"], "a fixture with no sentinels cannot catch silent data loss"
    principal = expected["principal"]
    log = log_dir(fixture, principal)
    files = sorted(p.relative_to(fixture).as_posix() for p in fixture.rglob("*") if p.is_file())
    streams = sorted(p.relative_to(fixture).as_posix() for p in log.glob("*.jsonl"))
    assert streams, f"{log}: no streams"
    assert files == sorted([EXPECTED, *streams]), "only the log and __expected__.json belong here"


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_every_event_in_a_fixture_carries_its_directory_version(fixture: Path) -> None:
    version = int(fixture.parent.name.removeprefix("v"))
    records = _from_log(fixture, _expected(fixture)["principal"])
    assert {r.event.v for r in records} == {version}


# --- verify: rebuild over every fixture equals its hash ---------------------------------


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_rebuild_over_every_fixture_equals_its_hash(fixture: Path, tmp_path: Path) -> None:
    root, principal = _copy(fixture, tmp_path)
    expected = _expected(fixture)

    report = rebuild(root, principal)

    assert report.full
    assert report.skips == (), "a golden log has no torn or skipped lines"
    assert report.events == expected["events"]
    assert state_hash(resolve(report.records)) == expected["state_hash"]
    assert state_hash(resolve(load_records(root, principal))) == expected["state_hash"]
    assert state_hash(resolve(_from_log(root, principal))) == expected["state_hash"]


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_an_incremental_build_over_a_fixture_equals_its_hash(fixture: Path, tmp_path: Path) -> None:
    """The normal path (D96) must reach the same state as the rebuild test."""
    root, principal = _copy(fixture, tmp_path)
    update(root, principal)
    second = update(root, principal)
    assert not second.full
    assert second.added == ()
    assert state_hash(resolve(load_records(root, principal))) == _expected(fixture)["state_hash"]


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_the_fold_over_every_fixture_is_the_one_it_shipped_with(
    fixture: Path, tmp_path: Path
) -> None:
    root, principal = _copy(fixture, tmp_path)
    report = rebuild(root, principal)
    assert _fold(report.records) == _expected(fixture)["fold"]


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_every_sentinel_survives_the_rebuild(fixture: Path, tmp_path: Path) -> None:
    root, principal = _copy(fixture, tmp_path)
    rebuild(root, principal)
    records = load_records(root, principal)
    assert sentinel_failures(records, _expected(fixture)["sentinels"]) == []


@pytest.mark.parametrize("fixture", GOLDEN, ids=IDS)
def test_the_rebuild_never_writes_into_the_fixture(fixture: Path, tmp_path: Path) -> None:
    root, principal = _copy(fixture, tmp_path)
    rebuild(root, principal)
    assert index_path(root, principal).is_file()
    assert not index_path(fixture, principal).exists()


# --- the checker catches what it is for --------------------------------------------------

MAYA = FIXTURES / "v1" / "maya"


def _rewrite_line(root: Path, stream: str, seq: int, change: Any) -> None:
    """Apply ``change`` to one event's JSON in a copied log. Only ever on a copy."""
    path = log_dir(root, _expected(MAYA)["principal"]) / f"{stream}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        data = json.loads(line)
        if data["seq"] == seq:
            change(data)
            lines[i] = json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text("".join(lines), encoding="utf-8")


def test_a_field_lost_silently_is_caught_by_a_sentinel_and_not_by_the_hash(
    tmp_path: Path,
) -> None:
    """Why D88 asks for sentinels: this loss loads cleanly and leaves the hash as it was."""
    root, principal = _copy(MAYA, tmp_path)
    _rewrite_line(root, "d-mbp", 1, lambda d: d.pop("source_version"))

    report = rebuild(root, principal)

    assert state_hash(resolve(report.records)) == _expected(MAYA)["state_hash"]
    failures = sentinel_failures(load_records(root, principal), _expected(MAYA)["sentinels"])
    assert failures == ["d-mbp:1.source_version: None after the rebuild, expected '2.1.0'"]


def test_a_changed_belief_moves_the_hash(tmp_path: Path) -> None:
    root, principal = _copy(MAYA, tmp_path)
    _rewrite_line(root, "d-phone", 1, lambda d: d.update(valid_from="2026-10-01"))
    report = rebuild(root, principal)
    assert state_hash(resolve(report.records)) != _expected(MAYA)["state_hash"]


def test_a_sentinel_that_names_nothing_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    root, principal = _copy(MAYA, tmp_path)
    rebuild(root, principal)
    records = load_records(root, principal)
    failures = sentinel_failures(
        records,
        {
            "d-mbp:99.body": "x",
            "d-mbp:1": "x",
            "d-mbp:1.no_such_field": "x",
            "d-mbp:1.caused_by.0": "x",
            "d-mbp:2.confidence": 1,
        },
    )
    assert len(failures) == 5
