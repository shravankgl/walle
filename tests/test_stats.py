"""Issue #53's verify criterion: `walle stats`, the Phase 1 gate metric (D49, PLAN Phase 1).

  hand count        the counts on tests/fixtures/stats match the table below, by hand
  log alone         a rebuilt, deleted or damaged index gives the same numbers, and so does
                    folding the stream files directly with no index at all
  counts only       nothing a proposal or an assertion says is ever printed

The fixture is synthetic: a made-up person's basil, book club and bake sale. Its hand count:

  ref          what became of it                                         counted as
  d-mbp:1      kept on the other device (d-phone:1), same text           kept
  d-mbp:2      kept with a changed body (d-mbp:5)                        edited
  d-mbp:3      dropped (d-mbp:6)                                         dropped
  d-mbp:4      nothing                                                   still staged
  d-mbp:7      kept (d-mbp:8), then the assertion forgotten (d-mbp:9)    kept
  d-mbp:10     kept with only the summary changed (d-mbp:11)             edited
  d-mbp:12     kept here (d-mbp:13), dropped on the phone (d-phone:5)    kept
  d-mbp:15     "edited" with nothing changed (d-mbp:16)                  kept
  d-phone:2    dropped (d-phone:3), the drop undone (d-phone:4)          still staged

  d-mbp:14 is an assertion with no proposal behind it and d-phone:6 an observation: neither
  is a proposal, so neither is counted.

  9 proposals: kept 4, edited 2, dropped 1, still staged 2. Accepted 6 of 7 decided, 86%.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import staging, stats
from walle.cli import main
from walle.index import rebuild, update
from walle.paths import index_path, log_dir
from walle.reader import read_file

P = "maya"
FIXTURE = Path(__file__).parent / "fixtures" / "stats"

HAND_COUNT = (
    "maya: 9 proposals in the log.\n"
    "  kept: 4\n"
    "  edited: 2\n"
    "  dropped: 1\n"
    "  still staged: 2\n"
    "acceptance: 6 of 7 decided (86%).\n"
)


def _seed(root: Path) -> Path:
    log = log_dir(root, P)
    log.mkdir(parents=True)
    for stream in sorted(FIXTURE.glob("d-*.jsonl")):
        shutil.copy(stream, log / stream.name)
    return log


def _stats(root: Path) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, ["stats", "--root", str(root), "--principal", P])
    return result.exit_code, result.stdout, result.stderr


# --- the verify criterion ---------------------------------------------------------------


def test_counts_on_the_fixture_match_the_hand_count(tmp_path: Path) -> None:
    _seed(tmp_path)
    code, out, err = _stats(tmp_path)
    assert code == 0, out + err
    assert out == HAND_COUNT
    assert err == ""


def test_the_fold_matches_the_hand_count(tmp_path: Path) -> None:
    _seed(tmp_path)
    counts = stats.count(update(tmp_path, P).records)
    assert counts == stats.Counts(kept=4, edited=2, dropped=1, staged=2)
    assert counts.proposals == 9
    assert counts.decided == 7
    assert counts.accepted == 6


def test_a_rebuilt_index_gives_the_same_numbers(tmp_path: Path) -> None:
    """Hard constraint 5: the index is derived, so it cannot change the answer."""
    _seed(tmp_path)
    first = _stats(tmp_path)
    assert first[1] == HAND_COUNT

    rebuild(tmp_path, P)
    assert _stats(tmp_path) == first

    index_path(tmp_path, P).unlink()
    assert _stats(tmp_path) == first

    index_path(tmp_path, P).write_bytes(b"not an index at all")
    assert _stats(tmp_path) == first


def test_the_numbers_are_the_streams_folded_with_no_index(tmp_path: Path) -> None:
    """The same count from the stream files read directly, no index ever built."""
    log = _seed(tmp_path)
    records = [
        record
        for path in sorted(log.glob("d-*.jsonl"))
        for record in read_file(path, path.stem).records
    ]
    assert not index_path(tmp_path, P).exists()
    assert stats.count(records) == stats.count(update(tmp_path, P).records)
    assert stats.count(reversed(records)) == stats.count(records)  # order does not matter


def test_an_index_behind_the_log_is_brought_up_to_date_first(tmp_path: Path) -> None:
    """The count is of the log as it is now, not whatever the index last saw."""
    _seed(tmp_path)
    assert _stats(tmp_path)[1] == HAND_COUNT
    staging.remember(tmp_path, P, "phone", "Maya's library card renews in June.")
    code, out, err = _stats(tmp_path)
    assert code == 0, out + err
    assert "maya: 10 proposals in the log." in out
    assert "  still staged: 3\n" in out
    assert "acceptance: 6 of 7 decided (86%)." in out


def test_only_counts_are_printed_never_what_anything_says(tmp_path: Path) -> None:
    log = _seed(tmp_path)
    code, out, err = _stats(tmp_path)
    assert code == 0, out + err
    said: set[str] = set()
    for path in log.glob("d-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            for field in ("body", "summary", "reason"):
                if field in event:
                    said.add(event[field])
            for field in ("body", "summary"):  # a reason's words are walle's own, "dropped"
                if field in event:
                    said.update(word for word in event[field].split() if len(word) > 4)
    assert said
    leaked = sorted(text for text in said if text in out or text in err)
    assert leaked == []
    assert "d-mbp" not in out and "d-phone" not in out  # not even which items


# --- around it --------------------------------------------------------------------------


def test_a_torn_line_is_reported_as_uncounted_without_its_text(tmp_path: Path) -> None:
    """D80: an interrupted append is skipped. It may have been a proposal, so the report says
    the counts are short, and says so without quoting the line."""
    log = _seed(tmp_path)
    with open(log / "d-mbp.jsonl", "ab") as f:
        f.write(b'{"actor":"user","body":"Maya hides the spare key under the heron')
    code, out, err = _stats(tmp_path)
    assert code == 0, out + err
    assert out == HAND_COUNT
    assert "skipped 1 unreadable line" in err
    assert "heron" not in out + err


def test_a_line_that_does_not_validate_fails_without_quoting_it(tmp_path: Path) -> None:
    """pydantic quotes the value it refuses; the error must not carry proposal text."""
    log = _seed(tmp_path)
    bad = {
        "actor": "user",
        "body": "Maya's diary sits under the heron lamp.",
        "caused_by": [],
        "level": "explicit",
        "seq": 17,
        "summary": "heron lamp " * 20,  # over the 120-character limit
        "ts": "2026-11-03T19:00:00+00:00",
        "type": "proposal",
        "v": 1,
    }
    with open(log / "d-mbp.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(bad) + "\n")
    code, out, err = _stats(tmp_path)
    assert code == 1
    assert out == ""
    assert "d-mbp.jsonl" in err  # where it is, so it can be found
    assert "heron" not in err
    assert "diary" not in err


def test_nothing_decided_yet_is_said_rather_than_a_percentage(tmp_path: Path) -> None:
    """No 0% and no division by zero when everything is still staged."""
    staging.remember(tmp_path, P, "mbp", "Maya's umbrella lives by the door.")
    code, out, err = _stats(tmp_path)
    assert code == 0, out + err
    assert out == (
        "maya: 1 proposal in the log.\n"
        "  kept: 0\n"
        "  edited: 0\n"
        "  dropped: 0\n"
        "  still staged: 1\n"
        "acceptance: nothing decided yet.\n"
    )


def test_an_empty_log_counts_zero_rather_than_failing(tmp_path: Path) -> None:
    log_dir(tmp_path, P).mkdir(parents=True)
    code, out, err = _stats(tmp_path)
    assert code == 0, out + err
    assert out.startswith("maya: 0 proposals in the log.\n")
    assert out.endswith("acceptance: nothing decided yet.\n")


def test_no_log_is_an_error_not_zero(tmp_path: Path) -> None:
    """A misspelt principal must not read as a person with nothing decided."""
    code, out, err = _stats(tmp_path)
    assert code == 1
    assert out == ""
    assert "no log" in err
    assert not log_dir(tmp_path, P).exists()


def test_a_live_review_moves_the_counts(tmp_path: Path) -> None:
    """End to end through `walle review`: one keep, one edit, one drop."""
    for text in ("Maya's choir is on Monday.", "The bins go out Tuesday.", "Maya hates jazz."):
        staging.remember(tmp_path, P, "mbp", text)
    items = staging.pending(tmp_path, P)
    staging.keep(tmp_path, P, "mbp", items[0].ref)
    staging.edit(tmp_path, P, "mbp", items[1].ref, body="The bins go out on Wednesday now.")
    staging.drop(tmp_path, P, "mbp", items[2].ref)
    assert stats.stats(tmp_path, P).counts == stats.Counts(kept=1, edited=1, dropped=1, staged=0)


@pytest.mark.parametrize(
    ("accepted", "decided", "percent"),
    [(6, 7, 86), (1, 2, 50), (1, 3, 33), (2, 3, 67), (1, 8, 13), (0, 5, 0), (5, 5, 100)],
)
def test_the_percentage_rounds_half_up(accepted: int, decided: int, percent: int) -> None:
    assert stats.percent(accepted, decided) == percent
