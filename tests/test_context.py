"""Task 12, `walle forget` and `walle context` (D57, D93, D103, D107; hard constraints 2 and 5).

  forget hides       a forgotten fact leaves the render; the log only grew; `why` explains it
  forget refuses     a ref that is not a current assertion, before anything is written
  view round trip    the view parses back to exactly what it shows, header-like lines included
  context edits      an edit, a removal and an addition append the events they imply
  survives rebuild   the resolved state after a context edit is the same from a fresh index
  stale view         an edit to a fact retired meanwhile is refused whole, nothing written
  two runners        an apply that waits on another decision re-reads the log after the wait

Every event here is synthetic: a made-up person's dinners and plants, no real data.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import context, index, staging, why
from walle.cli import main
from walle.events import ACTOR_USER, Assertion, Supersede
from walle.hot import LEARNED, render
from walle.paths import hot_dir, principal_dir, staging_lock_path, stream_path
from walle.reader import read_stream
from walle.resolver import resolve

P = "maya"
D = "mbp"


def _cli(root: Path, *args: str, input: str | None = None) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, [args[0], "--root", str(root), *args[1:]], input=input)
    return result.exit_code, result.stdout, result.stderr


def _believe(root: Path, text: str, summary: str | None = None) -> str:
    """Stage `text` and keep it, the way a person would. Returns the assertion's ref."""
    ref = staging.remember(root, P, D, text, summary=summary).ref
    return staging.keep(root, P, D, ref)


def _learned(root: Path) -> str:
    render(root, P)
    return (hot_dir(root, P) / LEARNED).read_text(encoding="utf-8")


def _current(root: Path) -> dict[str, Assertion]:
    return {b.ref: b.assertion for b in resolve(index.update(root, P).records).current}


# --- forget: the verify criterion -------------------------------------------------------


def test_forget_hides_the_fact_from_the_render_and_why_still_explains_it(tmp_path: Path) -> None:
    """PLAN §9 task 12: `forget` hides the fact from the render and `why` still explains it.
    Hard constraint 2: the forgotten assertion is still in the log, byte for byte."""
    ref = _believe(tmp_path, "Maya keeps the basil on the east windowsill.")
    kept = _believe(tmp_path, "Maya's dinner is at eight.")
    assert "east windowsill" in _learned(tmp_path)
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()

    code, out, err = _cli(tmp_path, "forget", "--principal", P, "--device", D, ref)
    assert code == 0, out + err

    after = log.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    learned = _learned(tmp_path)
    assert "east windowsill" not in learned
    assert "dinner is at eight" in learned
    assert list(_current(tmp_path)) == [kept]

    events = {r.ref: r.event for r in read_stream(tmp_path, P, D)}
    assert isinstance(events[ref], Assertion)  # still there
    (tombstone_ref,) = [r for r, e in events.items() if isinstance(e, Supersede)]
    tombstone = events[tombstone_ref]
    assert isinstance(tombstone, Supersede) and tombstone.target == ref
    assert tombstone.actor == ACTOR_USER
    assert f"forgot {ref}" in out and tombstone_ref in out

    explanation = why.explain(tmp_path, P, ref)
    head = explanation.steps[0]
    assert head.ref == ref and isinstance(head.event, Assertion)
    assert head.status is not None and "forgotten" in head.status and tombstone_ref in head.status
    # ...and the chain behind it: the proposal it was kept from.
    assert explanation.steps[1].ref == head.event.caused_by[0]


def test_forget_records_when_the_fact_stopped_being_true(tmp_path: Path) -> None:
    """D93: a retraction has a log time and a world time."""
    ref = _believe(tmp_path, "Maya lives in Pune.")
    gone = context.forget(tmp_path, P, D, ref, valid_to="2026-03-01", reason="moved")
    (retired,) = resolve(index.update(tmp_path, P).records).retired
    assert retired.ref == ref and retired.by == (gone,)
    assert retired.valid_to == "2026-03-01"
    event = {r.ref: r.event for r in read_stream(tmp_path, P, D)}[gone]
    assert isinstance(event, Supersede) and event.reason == "moved"


def test_forget_refuses_what_is_not_a_current_assertion(tmp_path: Path) -> None:
    ref = _believe(tmp_path, "Maya likes rasam.")
    staged = staging.remember(tmp_path, P, D, "Maya dislikes coriander.").ref
    gone = context.forget(tmp_path, P, D, ref)
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()

    with pytest.raises(context.NotCurrentError, match=f"already forgotten by {gone}"):
        context.forget(tmp_path, P, D, ref)
    with pytest.raises(context.NotCurrentError, match=r"is a 'proposal'.*walle review"):
        context.forget(tmp_path, P, D, staged)
    with pytest.raises(context.NotCurrentError, match="no event d-mbp:99"):
        context.forget(tmp_path, P, D, "d-mbp:99")
    with pytest.raises(ValueError, match="not a world time"):
        context.forget(tmp_path, P, D, ref, valid_to="March")
    assert log.read_bytes() == before


def test_walle_forget_on_a_forgotten_fact_fails_plainly(tmp_path: Path) -> None:
    ref = _believe(tmp_path, "Maya likes rasam.")
    context.forget(tmp_path, P, D, ref)
    code, out, err = _cli(tmp_path, "forget", "--principal", P, "--device", D, ref)
    assert code == 1
    assert "already forgotten" in err and "Traceback" not in out + err


# --- the view ---------------------------------------------------------------------------


def test_the_view_shows_every_current_assertion_and_parses_back(tmp_path: Path) -> None:
    a = _believe(tmp_path, "Maya keeps the basil on the east windowsill.", "basil on the sill")
    b = _believe(tmp_path, "Dinner is at eight.\n\nOn Fridays, nine.", "dinner at eight")
    gone = _believe(tmp_path, "Maya lives in Pune.")
    context.forget(tmp_path, P, D, gone)

    view = context.open_view(tmp_path, P)

    assert f"### [#{a}] basil on the sill" in view.text
    assert f"### [#{b}] dinner at eight" in view.text
    assert gone not in view.text
    assert context.parse(view.text) == view.entries
    assert view.entries == (
        context.Entry(a, "basil on the sill", "Maya keeps the basil on the east windowsill."),
        context.Entry(b, "dinner at eight", "Dinner is at eight.\n\nOn Fridays, nine."),
    )


def test_a_body_line_that_looks_like_a_header_round_trips(tmp_path: Path) -> None:
    body = "Notes:\n### [#d-mbp:1] not a header\n\\### [#new] nor this\n\\ a plain backslash"
    ref = _believe(tmp_path, body, "tricky")
    view = context.open_view(tmp_path, P)
    assert view.entries == (context.Entry(ref, "tricky", body),)
    # Saved unchanged, nothing is written.
    applied = context.apply(tmp_path, P, D, view, view.text)
    assert applied.written == ()


# --- context: the verify criterion ------------------------------------------------------


def test_an_edit_through_context_appends_events_and_survives_a_rebuild(tmp_path: Path) -> None:
    """PLAN §9 task 12: an edit through `context` appends events and survives a rebuild.

    One save edits one entry, removes another and adds a third. The file only grows; the
    events are an assertion and a supersede for the edit, a supersede for the removal and an
    assertion for the addition. A fresh index folds to the same state."""
    basil = _believe(tmp_path, "Maya keeps the basil on the east windowsill.", "basil")
    pune = _believe(tmp_path, "Maya lives in Pune.", "lives in Pune")
    dinner = _believe(tmp_path, "Dinner is at eight.", "dinner")
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()
    view = context.open_view(tmp_path, P)

    edited = (
        view.text.replace(
            "Maya keeps the basil on the east windowsill.",
            "Maya keeps the basil on the south windowsill now.",
        )
        .replace(f"### [#{pune}] lives in Pune\n\nMaya lives in Pune.\n", "")
        .rstrip("\n")
        + "\n\n### [#new] fern on Sundays\n\nMaya waters the fern on Sundays.\n"
    )
    applied = context.apply(tmp_path, P, D, view, edited)

    after = log.read_bytes()
    assert after.startswith(before)
    written = [r for r in read_stream(tmp_path, P, D) if r.ref in applied.written]
    assert [type(r.event).__name__ for r in written] == [
        "Assertion",
        "Supersede",
        "Supersede",
        "Assertion",
    ]
    (new_basil,) = [old_new[1] for old_new in applied.edited if old_new[0] == basil]
    assert applied.forgotten[0][0] == pune
    (fern,) = applied.added

    current = _current(tmp_path)
    assert set(current) == {dinner, new_basil, fern}
    assert current[new_basil].body == "Maya keeps the basil on the south windowsill now."
    assert current[new_basil].summary == "basil"
    assert current[new_basil].caused_by == [basil]  # an edit cites what it replaces
    assert current[fern].summary == "fern on Sundays" and current[fern].actor == ACTOR_USER

    # The rebuild test (hard constraint 5): destroy the index, fold the log again.
    state = index.state_hash(resolve(index.update(tmp_path, P).records))
    index.index_path(tmp_path, P).unlink()
    assert index.state_hash(resolve(index.rebuild(tmp_path, P).records)) == state

    learned = _learned(tmp_path)
    assert "south windowsill" in learned and "east windowsill" not in learned
    assert "Pune" not in learned and "fern on Sundays" in learned
    # And the replaced text is still explained.
    status = why.explain(tmp_path, P, basil).steps[0].status
    assert status is not None and "edited" in status and new_basil in status


def test_walle_context_edits_through_the_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _believe(tmp_path, "Maya's dinner is at eight.", "dinner")
    seen: list[str] = []

    def editor(text: str, **_: object) -> str:
        seen.append(text)
        return text.replace("at eight", "at half past eight")

    monkeypatch.setattr("walle.cli.click.edit", editor)
    code, out, err = _cli(tmp_path, "context", "--principal", P, "--device", D)
    assert code == 0, out + err
    assert f"[#{ref}]" in seen[0]
    assert f"edited {ref}" in out and "walle render" in out
    assert "half past eight" in _learned(tmp_path)


@pytest.mark.parametrize("saved", [None, "", "   \n"])
def test_quitting_the_editor_or_emptying_it_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: str | None
) -> None:
    """An empty file is read as "cancelled", never as "forget everything": a crashed editor
    and a deliberate wipe look the same, and only one of them is safe to act on."""
    _believe(tmp_path, "Maya's dinner is at eight.")
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()
    monkeypatch.setattr("walle.cli.click.edit", lambda text, **_: saved)
    code, out, err = _cli(tmp_path, "context", "--principal", P, "--device", D)
    assert code == 0, out + err
    assert "nothing written" in out
    assert log.read_bytes() == before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t + "\n### [#d-mbp:1] not in the view\n\nresurrected\n", "not in the view"),
        (lambda t: t + "\n" + t[t.index("### ") :], "appears twice"),
        (lambda t: "some stray text\n" + t, "before the first entry"),
        (lambda t: t + "\n### [#new] empty body\n", "empty"),
        (lambda t: t + "\n### [#new] " + "x" * 121 + "\n\nbody\n", "120"),
    ],
)
def test_a_save_that_does_not_parse_is_refused_whole(
    tmp_path: Path, change: Callable[[str], str], message: str
) -> None:
    _believe(tmp_path, "Maya's dinner is at eight.", "dinner")
    view = context.open_view(tmp_path, P)
    edited = view.text.replace("at eight", "at nine")  # a valid edit alongside the bad one
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()
    with pytest.raises(context.ContextError, match=message):
        context.apply(tmp_path, P, D, view, change(edited))
    assert log.read_bytes() == before


def test_an_edit_to_a_fact_retired_meanwhile_is_refused(tmp_path: Path) -> None:
    """The view is read without a lock, because a person may keep the editor open for an
    hour. Applying re-reads the log under staging.lock; a fact the save touches that is no
    longer current refuses the whole save, and nothing is written."""
    ref = _believe(tmp_path, "Maya's dinner is at eight.", "dinner")
    other = _believe(tmp_path, "Maya likes rasam.", "rasam")
    view = context.open_view(tmp_path, P)
    gone = context.forget(tmp_path, P, D, ref)  # meanwhile, somewhere else
    log = stream_path(tmp_path, P, D)
    before = log.read_bytes()

    edited = view.text.replace("at eight", "at nine").replace("likes rasam", "loves rasam")
    with pytest.raises(context.StaleViewError, match=f"{ref}.*forgotten by {gone}"):
        context.apply(tmp_path, P, D, view, edited)
    assert log.read_bytes() == before
    assert list(_current(tmp_path)) == [other]


def test_walle_context_keeps_a_refused_save_for_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _believe(tmp_path, "Maya's dinner is at eight.", "dinner")

    def editor(text: str, **_: object) -> str:
        context.forget(tmp_path, P, D, ref)  # someone else, while the editor is open
        return text.replace("at eight", "at nine")

    monkeypatch.setattr("walle.cli.click.edit", editor)
    code, _, err = _cli(tmp_path, "context", "--principal", P, "--device", D)
    assert code == 1
    assert "nothing was written" in err.lower()
    (kept,) = principal_dir(tmp_path, P).glob("context-unsaved-*.md")
    assert str(kept) in err
    assert "at nine" in kept.read_text(encoding="utf-8")


# --- two runners ------------------------------------------------------------------------

_HOLD_AND_FORGET = """
import fcntl, os, sys, time
from pathlib import Path
from walle.events import ACTOR_USER, Supersede, now_instant
from walle.writer import StreamWriter

root, ref, lock = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
fd = os.open(lock, os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(0.8)
StreamWriter(root, "maya", "phone").append(
    Supersede(ts=now_instant(), actor=ACTOR_USER, target=ref, reason="forgotten")
)
os.close(fd)
"""


def test_an_apply_that_waits_on_another_decision_reads_the_log_after(tmp_path: Path) -> None:
    """Runner A holds staging.lock and forgets the fact. Runner B, applying an edit to the
    same fact, waits for A, then reads the log and refuses.

    Had B checked "still current?" before waiting, it would wake with a stale answer and
    append an edit beside A's forget: a replacement for a fact the person had just retired."""
    ref = _believe(tmp_path, "Maya's dinner is at eight.", "dinner")
    view = context.open_view(tmp_path, P)
    edited = view.text.replace("at eight", "at nine")
    lock = staging_lock_path(tmp_path, P)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    runner_a = subprocess.Popen(
        [sys.executable, "-c", _HOLD_AND_FORGET, str(tmp_path), ref, str(lock)],
        stdout=subprocess.PIPE,
        env=env,
    )
    try:
        assert runner_a.stdout is not None
        assert runner_a.stdout.readline().strip() == b"held"
        started = time.monotonic()
        with pytest.raises(context.StaleViewError, match="forgotten by d-phone:1"):
            context.apply(tmp_path, P, D, view, edited)
        waited = time.monotonic() - started
    finally:
        runner_a.wait(timeout=10)
    assert waited >= 0.5
    assert not any(
        isinstance(r.event, Assertion) and r.event.caused_by == [ref]
        for r in read_stream(tmp_path, P, D)
    )
