"""Task 7's verify criterion, and the cases around it (D50, D64, D81, D107).

  under the threshold, in full    every assertion renders with its body
  over the threshold, index mode  the newest ten in full, the rest as `[#ref] summary` lines
  rules is untouched              the renderer never writes, chmods, replaces or creates it
  an edit is kept and reported    a render over an out-of-band edit keeps the edit, says so
  0444, hash recorded             rendered files are read-only and the manifest has their hash
  temp file, then rename          a write never leaves a half-written file under the real name
  two renderers                   a second render waits, and never mistakes the first for a human

Every event here is synthetic: a made-up person's dinners and plants, no real data.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import walle.hot as hot
from walle.cli import main
from walle.events import ACTOR_CURATOR, ACTOR_USER, Assertion, Contradiction, Event
from walle.hot import (
    FILES,
    FULL_LIMIT,
    IDENTITY,
    KEEP_FULL,
    LEARNED,
    MANIFEST,
    ORDER,
    RULES,
    USER,
    Mode,
    compose,
    render,
)
from walle.paths import hot_dir
from walle.reader import Record
from walle.resolver import resolve
from walle.writer import StreamWriter

P = "maya"


def _ts(minute: int) -> str:
    return f"2026-11-04T09:{minute // 60:02d}:{minute % 60:02d}.000000+00:00"


def _assertion(
    summary: str, *, body: str | None = None, minute: int = 0, valid_from: str | None = None
) -> Assertion:
    return Assertion(
        ts=_ts(minute),
        actor=ACTOR_USER,
        body=body or summary,
        summary=summary,
        valid_from=valid_from,
    )


def _write(root: Path, device: str, *events: Event) -> list[str]:
    writer = StreamWriter(root, P, device)
    return [f"d-{device}:{writer.append(e).seq}" for e in events]


def _few(root: Path) -> list[str]:
    return _write(
        root,
        "mbp",
        _assertion("Eats dinner after eight", body="Eats dinner after eight on weekdays."),
        _assertion("Waters the fern on Sundays", body="Waters the fern on Sundays.", minute=1),
        _assertion("Prefers short replies", body="Prefers short replies, no preamble.", minute=2),
    )


def _many(root: Path, n: int = 30) -> list[str]:
    """``n`` assertions whose bodies are long enough to pass the threshold together."""
    events = [
        _assertion(
            f"Plant note {i:02d}",
            body=f"BODY-{i:02d} " + "The basil on the sill wants morning sun. " * 12,
            minute=i,
        )
        for i in range(n)
    ]
    return _write(root, "mbp", *events)


def _text(root: Path, name: str) -> str:
    return (hot_dir(root, P) / name).read_text(encoding="utf-8")


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((hot_dir(root, P) / MANIFEST).read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _edit(path: Path, extra: str) -> None:
    """What a person does with an editor: make it writable, change it."""
    path.chmod(0o644)
    with open(path, "a", encoding="utf-8") as f:
        f.write(extra)


# --- the verify criterion -----------------------------------------------------------------


def test_under_the_threshold_every_assertion_renders_in_full(tmp_path: Path) -> None:
    refs = _few(tmp_path)
    report = render(tmp_path, P)
    learned = _text(tmp_path, LEARNED)

    assert report.mode is Mode.FULL
    assert report.preserved == ()
    for ref, body in zip(
        refs,
        [
            "Eats dinner after eight on weekdays.",
            "Waters the fern on Sundays.",
            "Prefers short replies, no preamble.",
        ],
        strict=True,
    ):
        assert f"[#{ref}]" in learned
        assert body in learned
    assert "index mode" not in learned


def test_over_the_threshold_flips_to_index_mode(tmp_path: Path) -> None:
    refs = _many(tmp_path)
    report = render(tmp_path, P)
    learned = _text(tmp_path, LEARNED)

    assert report.mode is Mode.INDEX
    newest, older = refs[-KEEP_FULL:], refs[:-KEEP_FULL]
    for i, ref in enumerate(refs):
        in_full = ref in newest
        assert (f"BODY-{i:02d}" in learned) is in_full, ref
    for i, ref in enumerate(older):
        assert f"[#{ref}] Plant note {i:02d}\n" in learned
    assert "recall" in learned  # the note telling the model to fetch before acting
    assert report.summarised == len(older)
    assert report.in_full == KEEP_FULL


def test_rules_is_untouched_by_the_renderer(tmp_path: Path) -> None:
    _many(tmp_path)
    folder = hot_dir(tmp_path, P)
    folder.mkdir(parents=True)
    rules = folder / RULES
    rules.write_text("Never book anything before 9am.\n", encoding="utf-8")
    rules.chmod(0o644)
    os.utime(rules, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    before = os.stat(rules)

    render(tmp_path, P)
    _write(tmp_path, "mbp", _assertion("Likes rosemary", minute=99))
    render(tmp_path, P)

    after = os.stat(rules)
    assert rules.read_bytes() == b"Never book anything before 9am.\n"
    assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert RULES not in _manifest(tmp_path)["files"]  # type: ignore[operator]


def test_rules_is_never_created_by_the_renderer(tmp_path: Path) -> None:
    _few(tmp_path)
    report = render(tmp_path, P)
    assert not os.path.lexists(hot_dir(tmp_path, P) / RULES)
    assert report.rules_present is False


def test_a_render_over_an_out_of_band_edit_keeps_the_edit_and_reports_it(tmp_path: Path) -> None:
    _few(tmp_path)
    render(tmp_path, P)
    learned = hot_dir(tmp_path, P) / LEARNED
    _edit(learned, "\nAlso: hates cilantro.\n")
    edited = learned.read_bytes()

    new = _write(tmp_path, "mbp", _assertion("Likes rosemary", minute=99))
    report = render(tmp_path, P)

    assert learned.read_bytes() == edited  # kept, byte for byte
    assert [p.name for p in report.preserved] == [LEARNED]
    assert report.preserved[0].found == hashlib.sha256(edited).hexdigest()
    assert LEARNED not in report.written
    assert _manifest(tmp_path)["kept"] == [LEARNED]
    # The other files are still walle's, so they render as usual.
    assert set(report.written) | set(report.unchanged) == {IDENTITY, USER}
    # Still reported on the next render: nothing has reconciled it.
    assert [p.name for p in render(tmp_path, P).preserved] == [LEARNED]

    # Deleting the file is how the edit is discarded; the next render writes it fresh.
    learned.unlink()
    report = render(tmp_path, P)
    assert report.preserved == ()
    assert f"[#{new[0]}]" in learned.read_text(encoding="utf-8")


def test_the_cli_reports_a_kept_edit_and_exits_non_zero(tmp_path: Path) -> None:
    _few(tmp_path)
    runner = CliRunner()
    args = ["render", "--root", str(tmp_path), "--principal", P]
    first = runner.invoke(main, args)
    assert first.exit_code == 0, first.output
    _edit(hot_dir(tmp_path, P) / LEARNED, "\nAlso: hates cilantro.\n")

    second = runner.invoke(main, args)
    assert second.exit_code == 1
    assert "learned.md" in second.stderr
    assert "kept" in second.stderr and "not rendered over" in second.stderr
    assert "kept as edited, not this render: learned.md" in second.stdout


def test_an_edit_saved_by_replacing_the_file_is_kept(tmp_path: Path) -> None:
    """Editors often save by writing a new file and renaming it over the old one."""
    _few(tmp_path)
    render(tmp_path, P)
    user = hot_dir(tmp_path, P) / USER
    replacement = user.with_name("user.md.swp")
    replacement.write_text("Maya's own version.\n", encoding="utf-8")
    os.replace(replacement, user)

    report = render(tmp_path, P)
    assert user.read_text(encoding="utf-8") == "Maya's own version.\n"
    assert [p.name for p in report.preserved] == [USER]


def test_a_file_walle_never_wrote_is_not_clobbered(tmp_path: Path) -> None:
    """No manifest entry means no proof walle wrote it, so it is kept."""
    _few(tmp_path)
    folder = hot_dir(tmp_path, P)
    folder.mkdir(parents=True)
    (folder / LEARNED).write_text("hand-written before walle ever rendered\n", encoding="utf-8")

    report = render(tmp_path, P)
    assert (folder / LEARNED).read_text(encoding="utf-8") == (
        "hand-written before walle ever rendered\n"
    )
    assert [p.name for p in report.preserved] == [LEARNED]


def test_the_cli_says_plainly_when_the_folder_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only hot/ folder, staged by refusing the temp file: some sandboxes and root
    ignore 0o555, so a chmod would make this test skip exactly where it is least read."""
    _few(tmp_path)

    def refuse(*args: object, **kwargs: object) -> tuple[int, str]:
        raise PermissionError(13, "Permission denied", str(hot_dir(tmp_path, P)))

    monkeypatch.setattr(hot.tempfile, "mkstemp", refuse)
    result = CliRunner().invoke(main, ["render", "--root", str(tmp_path), "--principal", P])
    assert result.exit_code == 1
    assert "Error:" in result.output and "Permission denied" in result.output
    assert not isinstance(result.exception, PermissionError)  # handled, not a traceback


def test_a_symlink_in_place_of_a_rendered_file_is_not_followed(tmp_path: Path) -> None:
    _few(tmp_path)
    render(tmp_path, P)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("not walle's\n", encoding="utf-8")
    learned = hot_dir(tmp_path, P) / LEARNED
    learned.unlink()
    learned.symlink_to(elsewhere)

    report = render(tmp_path, P)
    assert learned.is_symlink()
    assert elsewhere.read_text(encoding="utf-8") == "not walle's\n"
    assert [p.name for p in report.preserved] == [LEARNED]


# --- 0444 and the recorded hash -----------------------------------------------------------


def test_rendered_files_are_0444_and_their_hashes_are_recorded(tmp_path: Path) -> None:
    _few(tmp_path)
    render(tmp_path, P)
    folder = hot_dir(tmp_path, P)
    files = _manifest(tmp_path)["files"]
    assert isinstance(files, dict)
    for name in FILES:
        path = folder / name
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o444, name
        assert files[name] == [_sha(path)], name


def test_an_unchanged_render_does_not_rewrite_the_files(tmp_path: Path) -> None:
    """A stable prefix stays byte- and inode-stable (R1)."""
    _few(tmp_path)
    render(tmp_path, P)
    names = (*FILES, MANIFEST)
    before = {n: os.stat(hot_dir(tmp_path, P) / n).st_ino for n in names}
    report = render(tmp_path, P)
    assert report.written == ()
    assert set(report.unchanged) == set(FILES)
    assert {n: os.stat(hot_dir(tmp_path, P) / n).st_ino for n in names} == before


def test_a_file_made_writable_but_not_changed_is_rendered_back_to_0444(tmp_path: Path) -> None:
    _few(tmp_path)
    render(tmp_path, P)
    learned = hot_dir(tmp_path, P) / LEARNED
    learned.chmod(0o644)
    report = render(tmp_path, P)
    assert report.preserved == ()
    assert LEARNED in report.written
    assert stat.S_IMODE(os.stat(learned).st_mode) == 0o444


def test_every_file_opens_by_saying_what_it_is(tmp_path: Path) -> None:
    """D64: unframed, the model reads its own memory as notes about a stranger."""
    _few(tmp_path)
    render(tmp_path, P)
    for name in FILES:
        first = _text(tmp_path, name).splitlines()[0]
        assert first.startswith("<!-- walle hot tier:"), name
        assert name in first and "read-only" in first and "walle context" in first, name
    assert "maya" in _text(tmp_path, IDENTITY)
    assert "rules.md" in _text(tmp_path, IDENTITY)  # says the rules win
    assert ORDER == (IDENTITY, USER, RULES, LEARNED)


# --- the write itself ---------------------------------------------------------------------


def test_a_write_goes_through_a_temp_file_in_the_same_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _few(tmp_path)
    moves: list[tuple[Path, Path]] = []
    real = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        moves.append((Path(src), Path(dst)))
        real(src, dst)

    monkeypatch.setattr(hot.os, "replace", spy)
    render(tmp_path, P)
    targets = {dst.name for _, dst in moves}
    assert set(FILES) <= targets and MANIFEST in targets
    for src, dst in moves:
        assert src.parent == dst.parent
        assert src.name.startswith(f".{dst.name}.") and src.name.endswith(".tmp")


def test_a_failed_write_leaves_the_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _few(tmp_path)
    render(tmp_path, P)
    folder = hot_dir(tmp_path, P)
    old = (folder / LEARNED).read_bytes()
    _write(tmp_path, "mbp", _assertion("Likes rosemary", minute=99))

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hot.os, "replace", boom)
    with pytest.raises(OSError, match="No space"):
        render(tmp_path, P)
    assert (folder / LEARNED).read_bytes() == old
    assert [p.name for p in folder.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("crash_at", ["after the first manifest", "after the files"])
def test_a_crash_mid_render_is_not_mistaken_for_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str
) -> None:
    """Files and manifest cannot change in one step. Whichever was written last, the next
    render must recognise walle's own bytes, or it would report an edit nobody made."""
    _few(tmp_path)
    render(tmp_path, P)
    _write(tmp_path, "mbp", _assertion("Likes rosemary", minute=99))

    real = hot._write_manifest
    calls = {"n": 0}
    real_file = hot._write_file

    def manifest(folder: Path, data: dict[str, object]) -> None:
        calls["n"] += 1
        if crash_at == "after the files" and calls["n"] == 2:
            raise KeyboardInterrupt  # killed before the final manifest
        real(folder, data)

    def file(path: Path, data: bytes) -> None:
        if crash_at == "after the first manifest":
            raise KeyboardInterrupt  # killed before any file moved
        real_file(path, data)

    monkeypatch.setattr(hot, "_write_manifest", manifest)
    monkeypatch.setattr(hot, "_write_file", file)
    with pytest.raises(KeyboardInterrupt):
        render(tmp_path, P)
    monkeypatch.undo()

    report = render(tmp_path, P)
    assert report.preserved == ()
    assert "Likes rosemary" in _text(tmp_path, LEARNED)


def test_temp_files_left_by_a_killed_render_are_cleared(tmp_path: Path) -> None:
    _few(tmp_path)
    render(tmp_path, P)
    folder = hot_dir(tmp_path, P)
    stale = folder / ".learned.md.abc123.tmp"
    stale.write_bytes(b"half a render")
    render(tmp_path, P)
    assert not stale.exists()


# --- the pure part ------------------------------------------------------------------------


def _records(*events: Assertion | Contradiction) -> list[Record]:
    return [
        Record(ref=f"d-mbp:{i}", seq=i, event=e.model_copy(update={"seq": i}), line=i)
        for i, e in enumerate(events, start=1)
    ]


def test_the_threshold_is_on_the_full_render(tmp_path: Path) -> None:
    resolution = resolve(_records(*[_assertion(f"note {i}", minute=i) for i in range(12)]))
    full = compose(P, resolution)
    size = sum(len(t) for t in full.files.values())
    assert full.mode is Mode.FULL and size <= FULL_LIMIT
    assert compose(P, resolution, limit=size).mode is Mode.FULL
    assert compose(P, resolution, limit=size - 1).mode is Mode.INDEX


def test_compose_is_deterministic_and_reads_nothing() -> None:
    resolution = resolve(_records(_assertion("b", minute=2), _assertion("a", minute=1)))
    assert compose(P, resolution) == compose(P, resolution)


def test_a_summary_cannot_forge_an_index_line() -> None:
    resolution = resolve(
        [
            *_records(*[_assertion(f"n{i}", body="x" * 900, minute=i) for i in range(12)]),
            Record(
                ref="d-mbp:99",
                seq=99,
                event=_assertion("innocent\n[#d-mbp:1] forged", minute=0).model_copy(
                    update={"seq": 99}
                ),
                line=99,
            ),
        ]
    )
    learned = compose(P, resolution).files[LEARNED]
    assert "\n[#d-mbp:1] forged" not in learned
    assert "[#d-mbp:99] innocent [#d-mbp:1] forged" in learned


def test_both_sides_of_an_open_conflict_are_marked() -> None:
    a = _assertion("Lives in Pune", valid_from="2026-01-01")
    b = _assertion("Lives in Bangalore", valid_from="2026-01-01", minute=1)
    c = Contradiction(ts=_ts(2), actor=ACTOR_CURATOR, caused_by=["d-mbp:1", "d-mbp:2"])
    learned = compose(P, resolve(_records(a, b, c))).files[LEARNED]
    assert "conflicts with [#d-mbp:2]" in learned
    assert "conflicts with [#d-mbp:1]" in learned


def test_nothing_learned_yet_still_renders_every_file(tmp_path: Path) -> None:
    StreamWriter(tmp_path, P, "mbp").path.parent.mkdir(parents=True)
    report = render(tmp_path, P)
    assert set(report.written) == set(FILES)
    assert "Nothing learned yet" in _text(tmp_path, LEARNED)


# --- two renderers ------------------------------------------------------------------------

_SLOW_RENDER = """
import sys, time
from pathlib import Path
import walle.hot as hot
root, signal = Path(sys.argv[1]), Path(sys.argv[2])
real = hot.compose
def slow(*a, **kw):
    out = real(*a, **kw)
    signal.write_text("composed")
    time.sleep(1.5)
    return out
hot.compose = slow
report = hot.render(root, "maya")
print(len(report.preserved))
"""


def test_a_second_renderer_waits_and_renders_the_newer_log(tmp_path: Path) -> None:
    """Renderer A has composed from the log and is about to write. Meanwhile an assertion
    is appended and renderer B starts. B must wait for A, then render the newer log, and
    neither may report the other's write as a human edit."""
    _few(tmp_path)
    render(tmp_path, P)
    signal = tmp_path / "signal"
    a = subprocess.Popen(
        [sys.executable, "-c", _SLOW_RENDER, str(tmp_path), str(signal)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not signal.exists():
            assert a.poll() is None, "renderer A exited early"
            assert time.monotonic() < deadline, "renderer A never composed"
            time.sleep(0.02)
        _write(tmp_path, "mbp", _assertion("Likes rosemary", minute=99))
        b = render(tmp_path, P)
        out, _ = a.communicate(timeout=20)
    finally:
        if a.poll() is None:
            a.kill()
    assert a.returncode == 0
    assert out.strip() == "0"  # A reported no edit
    assert b.preserved == ()
    assert "Likes rosemary" in _text(tmp_path, LEARNED)  # B's newer render is what is left


# --- task 7 follow-up (issue #28): the reported mode, duplicate conflicts, the open race ---


def _long_few(root: Path, n: int = 3) -> list[str]:
    """A few assertions, each long enough that together they pass the threshold."""
    events = [
        _assertion(
            f"Long note {i}",
            body=f"LONG-{i} " + "The fern by the north window wants damp soil. " * 80,
            minute=i,
        )
        for i in range(n)
    ]
    return _write(root, "mbp", *events)


def test_a_few_long_assertions_over_the_limit_report_the_mode_written(tmp_path: Path) -> None:
    """Over the limit but with no more than KEEP_FULL assertions, nothing can be summarised,
    so the file is the full render and the report and manifest must say full (D81)."""
    refs = _long_few(tmp_path)
    folder = hot_dir(tmp_path, P)
    report = render(tmp_path, P)
    learned = _text(tmp_path, LEARNED)

    assert sum(len((folder / n).read_text(encoding="utf-8")) for n in FILES) > FULL_LIMIT
    assert len(refs) <= KEEP_FULL
    for i, ref in enumerate(refs):
        assert f"### [#{ref}] Long note {i}" in learned
        assert f"LONG-{i} " in learned
    assert "index mode" not in learned
    assert report.summarised == 0
    assert report.mode is Mode.FULL
    assert _manifest(tmp_path)["mode"] == Mode.FULL.value


@pytest.mark.parametrize("n", [1, KEEP_FULL, KEEP_FULL + 1, 30])
def test_the_reported_mode_always_matches_the_text(n: int) -> None:
    """Whatever the count, index mode is reported exactly when the file is in index mode."""
    events = [_assertion(f"n{i}", body=f"B{i} " + "x" * 3000, minute=i) for i in range(n)]
    rendered = compose(P, resolve(_records(*events)))
    in_index = "index mode" in rendered.files[LEARNED]
    assert (rendered.mode is Mode.INDEX) is in_index
    assert (rendered.summarised > 0) is in_index
    assert rendered.in_full + rendered.summarised == n


def test_duplicate_contradictions_for_one_pair_render_one_line() -> None:
    a = _assertion("Lives in Pune", valid_from="2026-01-01")
    b = _assertion("Lives in Bangalore", valid_from="2026-01-01", minute=1)
    c1 = Contradiction(ts=_ts(2), actor=ACTOR_CURATOR, caused_by=["d-mbp:1", "d-mbp:2"])
    c2 = Contradiction(ts=_ts(3), actor=ACTOR_CURATOR, caused_by=["d-mbp:2", "d-mbp:1"])
    resolution = resolve(_records(a, b, c1, c2))
    assert len(resolution.conflicts) == 2  # the fold reports both events; the render dedupes
    learned = compose(P, resolution).files[LEARNED]
    assert learned.count("conflicts with [#d-mbp:2]") == 1
    assert learned.count("conflicts with [#d-mbp:1]") == 1


def _after_lstat(
    monkeypatch: pytest.MonkeyPatch, target: Path, act: Callable[[], None]
) -> list[int]:
    """Run ``act()`` once, right after the renderer's ``lstat`` of ``target``: the window
    between that check and the ``open``. Returns a list that records the call."""
    real = os.lstat
    fired: list[int] = []

    def lstat(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> os.stat_result:
        st = real(path, *args, **kwargs)
        if not fired and os.fspath(path) == os.fspath(target):
            fired.append(1)
            act()
        return st

    monkeypatch.setattr(os, "lstat", lstat)
    return fired


def test_a_symlink_swapped_in_after_the_lstat_is_preserved_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ELOOP from O_NOFOLLOW, not a mocked one: the file is a regular file at the
    lstat and a symlink by the open."""
    _few(tmp_path)
    render(tmp_path, P)
    learned = hot_dir(tmp_path, P) / LEARNED
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("not walle's\n", encoding="utf-8")

    def swap() -> None:
        learned.unlink()
        learned.symlink_to(elsewhere)

    fired = _after_lstat(monkeypatch, learned, swap)
    _write(tmp_path, "mbp", _assertion("Likes rain", minute=9))
    report = render(tmp_path, P)

    assert fired
    assert learned.is_symlink()
    assert elsewhere.read_text(encoding="utf-8") == "not walle's\n"
    assert [(p.name, p.found) for p in report.preserved] == [(LEARNED, None)]
    assert _manifest(tmp_path)["kept"] == [LEARNED]


def test_an_open_error_other_than_eloop_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the swapped-symlink case is a preserve; a real IO failure is not hidden."""
    _few(tmp_path)
    render(tmp_path, P)
    real_open = os.open
    learned = os.fspath(hot_dir(tmp_path, P) / LEARNED)

    def failing_open(path: str | os.PathLike[str], flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fspath(path) == learned:
            raise OSError(errno.EIO, "simulated I/O error", learned)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(OSError) as excinfo:
        render(tmp_path, P)
    assert excinfo.value.errno == errno.EIO


def test_the_mode_is_checked_on_the_opened_file_not_the_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Made writable between the lstat and the open, same bytes: the descriptor's mode is the
    truth, so the file is rendered back to 0444 rather than reported unchanged."""
    _few(tmp_path)
    render(tmp_path, P)
    learned = hot_dir(tmp_path, P) / LEARNED
    fired = _after_lstat(monkeypatch, learned, lambda: learned.chmod(0o644))
    report = render(tmp_path, P)

    assert fired
    assert report.preserved == ()
    assert LEARNED in report.written
    assert LEARNED not in report.unchanged
    assert stat.S_IMODE(os.stat(learned).st_mode) == 0o444
