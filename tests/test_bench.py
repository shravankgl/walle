"""``walle bench``: the README's persist / replay / recover numbers come from this command (D69)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import bench
from walle.cli import main
from walle.paths import index_path, stream_path
from walle.reader import read_stream


def test_measure_reports_every_number_and_the_rebuild_matches(tmp_path: Path) -> None:
    root = tmp_path / "bench"
    report = bench.measure(root, events=40, runs=3)

    assert report.events == 40
    assert report.persist.samples == 40
    assert report.replay.samples == 3
    assert report.recover.samples == 3
    for timing in (report.persist, report.replay, report.recover):
        assert 0 < timing.median_ms <= timing.p95_ms
    assert report.state_equal
    assert report.hot_chars > 0
    assert report.hot_tokens == -(-report.hot_chars // 4)
    # Every tenth event retracts the one before it, so the fold has work to do.
    assert report.live == 32
    assert len(read_stream(root, bench.PRINCIPAL, bench.DEVICE)) == 40
    assert index_path(root, bench.PRINCIPAL).is_file()


def test_a_root_given_is_kept_and_a_root_not_given_is_removed(tmp_path: Path) -> None:
    kept = tmp_path / "kept"
    bench.measure(kept, events=5, runs=1)
    assert stream_path(kept, bench.PRINCIPAL, bench.DEVICE).is_file()

    report = bench.measure(None, events=5, runs=1)
    assert not report.root.exists()


def test_refuses_a_folder_that_already_holds_anything(tmp_path: Path) -> None:
    (tmp_path / "principals").mkdir()
    with pytest.raises(ValueError, match="not empty"):
        bench.measure(tmp_path, events=5, runs=1)
    assert list(tmp_path.iterdir()) == [tmp_path / "principals"]


def test_refuses_a_file_and_silly_counts(tmp_path: Path) -> None:
    afile = tmp_path / "f"
    afile.write_text("x")
    with pytest.raises(ValueError, match="not a folder"):
        bench.measure(afile, events=5, runs=1)
    with pytest.raises(ValueError, match="events"):
        bench.measure(None, events=0, runs=1)
    with pytest.raises(ValueError, match="runs"):
        bench.measure(None, events=5, runs=0)


def test_the_command_prints_the_table_rows(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["bench", "--events", "20", "--runs", "2", "--root", str(tmp_path / "b")]
    )
    assert result.exit_code == 0, result.output
    out = result.output
    for word in ("persist", "replay", "recover", "hot tier", "machine"):
        assert word in out
    assert "state hash equal after rebuild: yes" in out


def test_the_command_refuses_a_non_empty_root_by_name(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("mine")
    result = CliRunner().invoke(main, ["bench", "--root", str(tmp_path)])
    assert result.exit_code != 0
    assert "not empty" in result.output
    assert (tmp_path / "x").read_text() == "mine"


def test_a_temporary_folder_that_cannot_be_removed_is_an_error_not_a_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The folder this test leaves behind lands under tmp_path, which pytest cleans up.
    monkeypatch.setattr(bench.tempfile, "tempdir", str(tmp_path))

    def refuse(path: object, ignore_errors: bool = False) -> None:
        if not ignore_errors:
            raise OSError(f"cannot remove {path}")

    monkeypatch.setattr(bench.shutil, "rmtree", refuse)
    result = CliRunner().invoke(main, ["bench", "--events", "5", "--runs", "1"])
    assert result.exit_code != 0
    assert "cannot remove" in result.output
    assert "now removed" not in result.output
    (left,) = tmp_path.iterdir()
    assert left.name.startswith("walle-bench-")
