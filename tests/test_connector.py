"""Issue #54's verify criteria, as tests (D92, D93).

  a re-run adds no duplicates              sync twice, the stream is byte-identical
  deleting the index loses no cursor       the cursor is read from the log, never the index
  two runners append each item once        two *processes*, each item has exactly one seq

The race test spawns real subprocesses rather than threads, for the reason ``test_writer.py``
gives: ``flock`` serialises between processes, and threads in one interpreter share a file
description and would pass a test the mechanism does not earn.

Nothing here touches a Google API or any other provider. The sources are fakes made of lists,
which is all the base needs to see; #57 and #58 bring their own fakes of their own APIs.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import walle.connector as connector
from walle import index
from walle.connector import Cursor, Item, Source, SyncReport, cursor_of, fold_cursor, sync
from walle.events import ACTOR_OBSERVER, ACTOR_USER, Assertion, Observation
from walle.paths import index_path
from walle.reader import read_stream
from walle.writer import StreamWriter

TS = "2026-11-04T08:00:00.000000+00:00"
LATER = "2026-11-04T09:00:00.000000+00:00"


def at(hour: int, minute: int = 0) -> str:
    return f"2026-11-03T{hour:02d}:{minute:02d}:00.000000+00:00"


class ListSource:
    """A source that hands back whatever it was built with and remembers what it was asked."""

    name = "fake"

    def __init__(self, items: Iterable[Item]) -> None:
        self.items = tuple(items)
        self.asked: list[Cursor] = []

    def pull(self, cursor: Cursor) -> Iterable[Item]:
        self.asked.append(cursor)
        return self.items


def items(*stamps: str, prefix: str = "item") -> list[Item]:
    return [Item(observed_at=stamp, body=f"{prefix} at {stamp}") for stamp in stamps]


def lines_of(writer: StreamWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in writer.path.read_bytes().splitlines()]


def writer_for(tmp_path: Path) -> StreamWriter:
    return StreamWriter(tmp_path / "root", "maya", "mbp")


# --- the interface -------------------------------------------------------------------------


def test_a_list_of_items_is_a_source() -> None:
    """The protocol asks for a name and ``pull``; a fake made of a list satisfies it."""
    source: Source = ListSource(items(at(9)))
    assert source.name == "fake"
    assert isinstance(source, Source)


def test_an_item_is_frozen_and_validated_when_it_becomes_an_observation(tmp_path: Path) -> None:
    item = Item(observed_at=at(9), body="hello")
    with pytest.raises(AttributeError):
        item.body = "changed"  # type: ignore[misc]
    bad = ListSource([Item(observed_at="yesterday", body="x")])
    with pytest.raises(ValidationError):
        sync(bad, writer_for(tmp_path), ts=TS)
    assert not writer_for(tmp_path).path.exists()


# --- the cursor is a fold (D92) ------------------------------------------------------------


def test_an_empty_or_missing_stream_folds_to_an_empty_cursor(tmp_path: Path) -> None:
    empty = Cursor(source="fake", observed_at=None, ts=None, events=0)
    assert fold_cursor(tmp_path / "root", "maya", "mbp", "fake") == empty
    writer = writer_for(tmp_path)
    writer.path.parent.mkdir(parents=True)
    writer.path.touch()
    assert fold_cursor(tmp_path / "root", "maya", "mbp", "fake") == empty


def test_the_cursor_is_the_max_observed_at_per_source_not_the_last_line(tmp_path: Path) -> None:
    """Items arrive out of order and from several sources; the cursor is a max, per source."""
    writer = writer_for(tmp_path)
    for source, observed_at, ts in [
        ("fake", at(10), TS),
        ("other", at(23), TS),
        ("fake", at(12), LATER),
        ("fake", at(11), LATER),
    ]:
        writer.append(
            Observation(
                ts=ts, actor=ACTOR_OBSERVER, source=source, observed_at=observed_at, body="b"
            )
        )
    writer.append(Assertion(ts=LATER, actor=ACTOR_USER, body="not an observation", summary="s"))
    contents = read_stream(tmp_path / "root", "maya", "mbp")
    assert cursor_of(contents, "fake") == Cursor(
        source="fake", observed_at=at(12), ts=LATER, events=3
    )
    assert cursor_of(contents, "other") == Cursor(
        source="other", observed_at=at(23), ts=TS, events=1
    )
    assert cursor_of(contents, "mail").events == 0


def test_the_cursor_compares_instants_not_strings(tmp_path: Path) -> None:
    """``+05:30`` and ``+00:00`` spellings of nearby moments must order by the moment."""
    writer = writer_for(tmp_path)
    earlier_but_later_string = "2026-11-03T14:00:00.000000+05:30"  # 08:30 UTC
    later_but_earlier_string = "2026-11-03T09:00:00.000000+00:00"
    for stamp in (earlier_but_later_string, later_but_earlier_string):
        writer.append(
            Observation(ts=TS, actor=ACTOR_OBSERVER, source="fake", observed_at=stamp, body="b")
        )
    cursor = cursor_of(read_stream(tmp_path / "root", "maya", "mbp"), "fake")
    assert cursor.observed_at == later_but_earlier_string


def test_the_source_is_asked_with_the_folded_cursor(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    first = ListSource(items(at(9), at(10)))
    sync(first, writer, ts=TS)
    assert first.asked == [Cursor(source="fake", observed_at=None, ts=None, events=0)]
    second = ListSource(items(at(11)))
    sync(second, writer, ts=LATER)
    assert second.asked == [Cursor(source="fake", observed_at=at(10), ts=TS, events=2)]


class ScopedSource(ListSource):
    """A source that asks for one slice of its provider (one calendar, one query) and writes
    which slice on each body's first line. Its cursor is folded over that slice only."""

    def __init__(self, scope: str, items: Iterable[Item]) -> None:
        super().__init__(Item(i.observed_at, f"{scope}: {i.body}", i.source_version) for i in items)
        self.scope = scope

    def covers(self, body: str) -> bool:
        return body.split("\n", 1)[0].startswith(f"{self.scope}: ")


def test_a_source_that_covers_only_some_bodies_is_asked_with_the_cursor_over_those(
    tmp_path: Path,
) -> None:
    """Issue #136: two calendars, or two queries, share one source name. Each is asked for
    changes since *its own* last item, or the second one loses everything older than the
    first one's cursor. The log already says which slice each item came from (D92)."""
    writer = writer_for(tmp_path)
    sync(ScopedSource("primary", items(at(9), at(10))), writer, ts=TS)
    team = ScopedSource("team", items(at(5), at(7)))
    report = sync(team, writer, ts=LATER)
    assert team.asked == [Cursor(source="fake", observed_at=None, ts=None, events=0)]
    assert [o.observed_at for o in report.appended] == [at(5), at(7)]
    assert report.after == Cursor(source="fake", observed_at=at(7), ts=LATER, events=2)
    primary = ScopedSource("primary", items(at(10)))
    sync(primary, writer, ts=LATER)
    assert primary.asked == [Cursor(source="fake", observed_at=at(10), ts=TS, events=2)]
    # Without ``covers`` the fold is over the whole source, as before.
    whole = ListSource([])
    sync(whole, writer, ts=LATER)
    assert whole.asked == [Cursor(source="fake", observed_at=at(10), ts=LATER, events=4)]
    contents = read_stream(writer.root, writer.principal, writer.device)
    assert cursor_of(contents, "fake", within=team.covers).observed_at == at(7)
    assert fold_cursor(writer.root, "maya", "mbp", "fake", within=team.covers).events == 2


def test_an_item_with_a_borrowed_time_is_the_same_item_whatever_the_time_says(
    tmp_path: Path,
) -> None:
    """Issue #136: a cancelled event Google sends with no time of its own takes the
    calendar's last change as ``observed_at``, and that moves with every edit. Such an item
    is matched by its body alone, or it is appended again on every sync."""
    writer = writer_for(tmp_path)
    bare = Item(observed_at=at(10), body="event gone", borrowed_time=True)
    sync(ListSource([bare]), writer, ts=TS)
    moved = Item(observed_at=at(11), body="event gone", borrowed_time=True)
    again = sync(ListSource([moved, moved]), writer, ts=LATER)
    assert again.appended == () and len(again.already_logged) == 2
    # Only the item that borrows its time is matched that way; an item with its own time
    # and the same text is a new observation, as it always was.
    own = sync(ListSource([Item(observed_at=at(11), body="event gone")]), writer, ts=LATER)
    assert [o.observed_at for o in own.appended] == [at(11)]
    # And a borrowed-time item seen in one run is not appended twice within it either.
    fresh = writer_for(tmp_path / "other")
    both = sync(
        ListSource([bare, Item(observed_at=at(12), body="event gone", borrowed_time=True)]),
        fresh,
        ts=TS,
    )
    assert len(both.appended) == 1 and len(both.already_logged) == 1
    assert [r["observed_at"] for r in lines_of(writer)] == [at(10), at(11)]


# --- verify 1: a re-run adds no duplicates -------------------------------------------------


def test_a_first_sync_appends_every_item_in_order_with_seqs(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    report = sync(ListSource(items(at(9), at(10), at(8))), writer, ts=TS)
    assert isinstance(report, SyncReport)
    assert [o.seq for o in report.appended] == [1, 2, 3]
    assert [o.observed_at for o in report.appended] == [at(9), at(10), at(8)]
    assert report.already_logged == ()
    assert report.before.events == 0
    assert report.after == Cursor(source="fake", observed_at=at(10), ts=TS, events=3)
    logged = read_stream(tmp_path / "root", "maya", "mbp").events
    assert all(isinstance(e, Observation) and e.source == "fake" for e in logged)
    assert all(e.actor == ACTOR_OBSERVER for e in logged)


def test_a_rerun_appends_nothing_and_leaves_the_stream_byte_identical(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(9), at(10))), writer, ts=TS)
    before = writer.path.read_bytes()
    again = sync(ListSource(items(at(9), at(10))), writer)  # a later run, a new ts
    assert again.appended == ()
    assert [i.observed_at for i in again.already_logged] == [at(9), at(10)]
    assert writer.path.read_bytes() == before


def test_a_source_that_returns_the_boundary_item_again_adds_nothing(tmp_path: Path) -> None:
    """A source asked for "since 10:00" may well hand 10:00 back. That is not a duplicate."""
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(9), at(10))), writer, ts=TS)
    later = sync(ListSource(items(at(10), at(11))), writer, ts=LATER)
    assert [o.observed_at for o in later.appended] == [at(11)]
    assert [r["observed_at"] for r in lines_of(writer)] == [at(9), at(10), at(11)]


def test_a_late_item_older_than_the_cursor_is_still_appended_once(tmp_path: Path) -> None:
    """The cursor is advice to the source, not a filter: a late arrival is not dropped."""
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(10))), writer, ts=TS)
    late = sync(ListSource(items(at(7), at(10))), writer, ts=LATER)
    assert [o.observed_at for o in late.appended] == [at(7)]
    assert sync(ListSource(items(at(7), at(10))), writer, ts=LATER).appended == ()


def test_the_same_item_twice_in_one_pull_is_appended_once(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    report = sync(ListSource(items(at(9), at(9))), writer, ts=TS)
    assert len(report.appended) == 1 and len(report.already_logged) == 1
    assert [r["seq"] for r in lines_of(writer)] == [1]


def test_items_are_matched_by_content_so_a_changed_item_is_a_new_observation(
    tmp_path: Path,
) -> None:
    writer = writer_for(tmp_path)
    sync(ListSource([Item(observed_at=at(9), body="meeting at 3")]), writer, ts=TS)
    changed = sync(ListSource([Item(observed_at=at(9), body="meeting at 4")]), writer, ts=TS)
    assert [o.body for o in changed.appended] == ["meeting at 4"]
    versioned = sync(
        ListSource([Item(observed_at=at(9), body="meeting at 4", source_version="v2")]),
        writer,
        ts=TS,
    )
    assert len(versioned.appended) == 1


def test_an_identical_item_from_another_source_does_not_count_as_logged(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(9))), writer, ts=TS)

    class Other(ListSource):
        name = "other"

    report = sync(Other(items(at(9))), writer, ts=TS)
    assert [o.seq for o in report.appended] == [2]
    assert [r["source"] for r in lines_of(writer)] == ["fake", "other"]


def test_with_nothing_to_append_no_stream_file_is_created(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    report = sync(ListSource([]), writer, ts=TS)
    assert report.appended == () and report.already_logged == ()
    assert not writer.path.exists()


def test_ts_is_the_run_time_once_for_the_batch_and_observed_at_is_the_items(
    tmp_path: Path,
) -> None:
    writer = writer_for(tmp_path)
    report = sync(ListSource(items(at(9), at(10))), writer, ts=TS)
    assert {o.ts for o in report.appended} == {TS}
    assert [o.observed_at for o in report.appended] == [at(9), at(10)]
    defaulted = sync(ListSource(items(at(11))), writer)
    assert defaulted.appended[0].ts != TS


# --- a failed pull appends nothing (the property #57 and #58 lean on) ----------------------


class Failing:
    name = "fake"

    def __init__(self, before_failing: Iterable[Item]) -> None:
        self.before_failing = tuple(before_failing)

    def pull(self, cursor: Cursor) -> Iterable[Item]:
        yield from self.before_failing
        raise ConnectionError("the fake API went away mid-page")


def test_a_source_that_fails_mid_pull_appends_nothing_not_even_the_items_before(
    tmp_path: Path,
) -> None:
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(8))), writer, ts=TS)
    before = writer.path.read_bytes()
    with pytest.raises(ConnectionError):
        sync(Failing(items(at(9), at(10))), writer, ts=TS)
    assert writer.path.read_bytes() == before


def test_a_bad_item_anywhere_in_the_pull_appends_nothing(tmp_path: Path) -> None:
    writer = writer_for(tmp_path)
    bad = ListSource([*items(at(9)), Item(observed_at="2026-11-03", body="a date, not an instant")])
    with pytest.raises(ValidationError):
        sync(bad, writer, ts=TS)
    assert not writer.path.exists()


# --- verify 2: deleting the index loses no cursor ------------------------------------------


def test_deleting_the_index_loses_no_cursor(tmp_path: Path) -> None:
    root = tmp_path / "root"
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(9), at(10))), writer, ts=TS)
    index.rebuild(root, "maya")
    assert index_path(root, "maya").exists()
    cursor = fold_cursor(root, "maya", "mbp", "fake")

    index_path(root, "maya").unlink()
    assert fold_cursor(root, "maya", "mbp", "fake") == cursor
    report = sync(ListSource(items(at(10), at(11))), writer, ts=LATER)
    assert report.before == cursor
    assert [o.observed_at for o in report.appended] == [at(11)]


def test_the_cursor_never_reads_the_index(tmp_path: Path) -> None:
    """A stale or missing index must not change what the source is asked for."""
    root = tmp_path / "root"
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(9))), writer, ts=TS)
    index.rebuild(root, "maya")
    sync(ListSource(items(at(10))), writer, ts=TS)  # the index now lags the log
    assert fold_cursor(root, "maya", "mbp", "fake").observed_at == at(10)
    index_path(root, "maya").write_bytes(b"not a database")
    assert fold_cursor(root, "maya", "mbp", "fake").observed_at == at(10)


def test_the_connector_module_imports_neither_the_index_nor_sqlite() -> None:
    source = inspect.getsource(connector)
    for needle in ("walle.index", "import index", "sqlite3"):
        assert needle not in source, f"connector.py names {needle}"


# --- the base holds no provider -----------------------------------------------------------


def test_the_base_imports_no_provider_no_network_and_no_model() -> None:
    """#57 and #58 build on this file; Google, OAuth and HTTP belong to them, not here.

    Checked on the import graph, not on words: a docstring may say "calendar", a provider
    client may not be imported.
    """
    tree = ast.parse(inspect.getsource(connector))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
        "walle.events",
        "walle.reader",
        "walle.writer",
    }, imported
    source = inspect.getsource(connector).lower()
    for needle in ("google", "gmail", "oauth", "token", "secret"):
        assert needle not in source, f"connector.py names {needle}"


def test_the_base_names_no_home_folder_or_environment() -> None:
    source = inspect.getsource(connector)
    for needle in ("expanduser", "Path.home", "os.environ", "getenv", 'Path("~'):
        assert needle not in source, f"connector.py uses {needle}"


# --- verify 3: two runners append each item once ------------------------------------------

# Each child syncs the same three items into one stream. The fold under the lock is slowed
# down so that, if it ran outside the writer's lock, both children would read an empty stream
# and both would append every item. Under the lock the second child waits and appends nothing.
_CHILD = """
import sys, time
from pathlib import Path
import walle.connector as connector
from walle.connector import Item
from walle.writer import StreamWriter

root = Path(sys.argv[1])
fold = connector._logged
def slow(contents, source):
    found = fold(contents, source)
    time.sleep(1.0)
    return found
connector._logged = slow

class Fake:
    name = "fake"
    def pull(self, cursor):
        return [Item(observed_at=f"2026-11-03T{h:02d}:00:00.000000+00:00", body=f"item {h}")
                for h in (9, 10, 11)]

result = connector.sync(Fake(), StreamWriter(root, "maya", "mbp"))
print(len(result.appended))
"""


def test_two_processes_at_once_append_each_item_once(tmp_path: Path) -> None:
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(tmp_path / "root")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    counts = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err.decode()
        counts.append(int(out))
    assert sorted(counts) == [0, 3]
    records = lines_of(writer_for(tmp_path))
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert len({r["body"] for r in records}) == 3
    assert fold_cursor(tmp_path / "root", "maya", "mbp", "fake").events == 3


def test_a_sync_does_not_hold_the_stream_lock_while_pulling(tmp_path: Path) -> None:
    """A slow source must not block every other writer to the stream."""
    writer = writer_for(tmp_path)
    sync(ListSource(items(at(8))), writer, ts=TS)

    class DuringPull:
        name = "fake"

        def pull(self, cursor: Cursor) -> Iterable[Item]:
            # Another writer appends while this source is "fetching". If the lock were held
            # around the pull, that writer would wait on it: a second open of the same file
            # takes its own flock, even in one process. A bounded join turns that wait into
            # a failure instead of a hang.
            other = threading.Thread(
                target=writer.append,
                args=(
                    Observation(
                        ts=TS, actor=ACTOR_OBSERVER, source="fake", observed_at=at(9), body="x"
                    ),
                ),
            )
            other.start()
            other.join(timeout=10)
            if other.is_alive():
                pytest.fail("the stream lock is held while the source pulls")
            return items(at(10))

    report = sync(DuringPull(), writer, ts=TS)
    assert [o.observed_at for o in report.appended] == [at(10)]
    assert [r["seq"] for r in lines_of(writer)] == [1, 2, 3]
