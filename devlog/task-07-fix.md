# Task 7 follow-up: reported render mode must match the file written

Issue #28, from the independent review of PR #27. Branch `task/07-fix`.

**Shipped:**
- `src/walle/hot.py`, the four findings:
  - **The mode tells the truth (medium).** `compose` now returns `Mode.FULL` when the full
    render is over `FULL_LIMIT` but there are no more than `KEEP_FULL` assertions. In that case
    index mode would summarise nothing, and the file written is the full render. Before, the
    file was full while the report, `rendered.json` and the CLI all said `index`. The CLI line
    read `index mode, 3 in full, 0 as one-line summaries`. It now reads
    `full mode, every one in full`. The two full-mode cases share one condition, so there is
    still one place that returns `FULL`. The module docstring says the rule.
  - **One conflict line per pair (low).** The per-assertion conflict lists are now sets, so two
    `Contradiction` events about the same pair render one line on each side. `_entry` still
    sorts what is left by ref.
  - **A symlink swapped in after the `lstat` is preserved (low).** `_judge` catches the
    `OSError` that `os.open(..., O_NOFOLLOW)` raises with `ELOOP` and returns the same
    `Preserved` record as a symlink found by the `lstat` (`found=None`, "not a regular file
    walle wrote"). Any other `OSError` is re-raised, so a real IO failure is not hidden.
  - **The mode is read from the descriptor (nit).** `_judge` takes one `fstat` of the opened
    fd and uses it both for the regular-file check and for the `0444` check behind
    `unchanged`. The earlier `lstat` now only keeps the `open` off things that are not regular
    files.
- `tests/test_hot.py`: 6 new tests (9 cases), in a new section at the end. Fixtures are
  synthetic: a made-up person's fern, basil and home town.

**Why:**
- **D81.** "Mode is fixed per session." Something downstream (the hook, task 8; `rendered.json`
  readers) will act on the mode, for example by telling the model to `recall` before acting.
  A mode that does not describe the text sends the model looking for one-line entries that
  are not there. The fix keeps D81's rule as written: the newest ten stay in full. So a hot
  tier of up to ten long assertions can be over ~8k characters. That is the honest result of
  the rule, not a new one; see *Choices*.
- **D107.** "Never a silent clobber, never a silent discard": every outcome of `_judge` is a
  checked verdict. A race that crashed the render with `ELOOP` gave the user a traceback-shaped
  error instead of the report D107 asks for. A mode taken from the `lstat` could report
  `unchanged` for a file that is in fact writable. That is a status that lies, and it left the
  file outside the `0444` protection D107 relies on.

**Verify:** the criterion from issue #28, part by part:
- *A few long assertions over the limit report the mode that matches the file written:*
  `test_a_few_long_assertions_over_the_limit_report_the_mode_written`. It uses three
  assertions with about 3,700-character bodies and asserts all of these: the three rendered
  files total more than `FULL_LIMIT`; every body is in `learned.md` with its full heading;
  there is no index-mode note; `summarised == 0`; `report.mode is Mode.FULL`; and the
  manifest's `mode` is `"full"`.
  `test_the_reported_mode_always_matches_the_text[1, 10, 11, 30]` pins the invariant at both
  sides of `KEEP_FULL`: index mode is reported exactly when the text is in index mode, exactly
  when something was summarised, and `in_full + summarised` is the count.
- *Duplicate contradictions render one line:*
  `test_duplicate_contradictions_for_one_pair_render_one_line`. Two contradictions about one
  pair, the second naming the sides in reverse order. The test first asserts that the fold
  really returns two `Conflict`s, so it would notice if the resolver ever deduplicated and
  the test stopped exercising the renderer. Then each side's line appears exactly once.
- *A simulated ELOOP preserves and reports rather than raising:*
  `test_a_symlink_swapped_in_after_the_lstat_is_preserved_not_raised`. This simulates the
  race, not the error. `os.lstat` is wrapped so that right after the renderer's real `lstat`
  of `learned.md`, the file is replaced by a symlink. The `ELOOP` therefore comes from the
  kernel's `O_NOFOLLOW`, as it would in production. The test asserts that the wrapper fired,
  the symlink and its target are untouched, `preserved` is `[(learned.md, None)]`, and the
  manifest's `kept` names it. `test_an_open_error_other_than_eloop_still_raises` is the other
  side: an injected `EIO` from `os.open` still raises.
- *Mode from the `fstat`:* `test_the_mode_is_checked_on_the_opened_file_not_the_lstat`. The
  same wrapper chmods `learned.md` to `0644` between the `lstat` and the `open`, with the bytes
  unchanged. The file must come back as `written` and `0444`, not `unchanged`.
- **The tests were written first.** Against the unmodified `hot.py`, five failed, each for its
  finding's reason:
  - `report.mode` was `INDEX`, expected `FULL`.
  - At `[10]`, `in_full=10, summarised=0` was reported as `INDEX`.
  - `count('conflicts with [#d-mbp:2]')` was 2.
  - `OSError: [Errno 40] Too many levels of symbolic links`.
  - `'learned.md' in ()` failed for `written`.

  The other new cases passed on the old code as expected: `[1]` is under the limit, `[11]` and
  `[30]` really summarise, and `EIO` already raised.
- **Each fix is load-bearing.** I undid each one on the final tree to check that its test
  fails:
  - Dropping the `not summarised` condition fails the two mode tests.
  - Re-raising every `OSError` fails the symlink-race test.
  - Using the `lstat` mode again fails the `fstat` test.
  - Swallowing every `OSError` fails the `EIO` test.
  - A no-op change fails nothing.
- The four checks, run on the final tree:

```
$ uv run pytest
247 passed, 3 skipped in 7.77s
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
34 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

  The 3 skips predate this task. They are `test_index.py`'s read-only-folder tests, which skip
  in this sandbox, as in task 7's log.

**Two-runner walkthrough:** this touches a derived file's write path, but no lock and no
ordering. `render.lock`, the two-phase manifest and task 7's walkthrough are unchanged. The new
cases sit inside A's step 4, while A holds `render.lock` and judges `learned.md`. The other
party is H, who takes no lock (an editor, or anything else with write access to `hot/`):

1. A `lstat`s `learned.md` and sees a regular file.
2. H replaces it with a symlink. Before, A's `open` raised `ELOOP` and the whole render failed
   with nothing written. Now A records it as preserved, writes the other two files and the
   manifest (which keeps `learned.md`'s old vouched hashes and lists it under `kept`), and
   reports it. The next render finds the symlink at the `lstat` and reports it again, until
   H removes it.
3. If H instead chmods the file `0644` without changing its bytes, A's `fstat` sees `0644` and
   re-renders it `0444` through the temp file and rename. Before, A reported it `unchanged`
   and left it writable.

A second renderer B still waits on `render.lock` and sees A's finished state, as before.

**Stranger pass:** the diff is under 30 lines of code, but it touches IO and an error path, so
I re-read it cold. Found:
- **A duplicated early return.** The first version added a second
  `return Rendered(Mode.FULL, …)` for the over-limit case. I folded both into one condition
  (`not summarised or <fits>`), so a later change to full-mode output cannot drift between the
  two. Re-ran the four checks and the mutation check after.
- **A comment that undersold the `lstat`.** I had written that the `lstat` "only saved an
  open". It does more: it keeps `open` away from a FIFO, which would block. I rewrote the
  comment. The same race also means a FIFO swapped in between the `lstat` and the `open` would
  block the render while it holds `render.lock`. That is not in this issue, so it is under
  *Noticed*.
- **Error classes.** Only `ELOOP` is turned into a preserve. `FileNotFoundError` keeps its old
  meaning ("gone, write it"). Everything else propagates to the CLI's existing `OSError`
  handler (`Error: …`, exit 1) and is tested with `EIO`. No error is swallowed.
- **Status messages.** The preserved record for the race says "it is not a regular file walle
  wrote", which is true of a symlink, and `found=None` matches the existing symlink case. The
  CLI's mode line is now true in the over-limit case. I checked `cli.py`: it reads
  `report.mode` and needed no change.
- Leaks and locks: the new `except` returns before `fdopen`. On `ELOOP` no fd was opened, so
  there is nothing to close. No lock scope changed.

**Review:** pending.

**Choices made inside the task:**
- **Report `full`, rather than force index mode with fewer than ten kept in full.** The other
  way to make mode and text agree is to keep fewer than `KEEP_FULL` in full when even ten
  don't fit. But D81 says "the newest ten stay in full", and changing that number is a
  decision, not a render detail. So the file can be over ~8k characters with at most ten
  assertions. The CLI and report say so truthfully.
- **Deduplicate in the renderer, not the resolver.** The fold reports every contradiction
  event, which is what the log holds and what a later "who flagged this, when" view needs.
  Collapsing them is a presentation choice, so it lives in `compose`. The issue asked for this
  ("deduplicate before sorting").
- **`ELOOP` only, not `EMLINK`.** Linux and macOS both give `ELOOP` for `O_NOFOLLOW` on a
  symlink (POSIX specifies it). FreeBSD gives `EMLINK`, but Phase 0 targets Linux and macOS,
  and catching `EMLINK` elsewhere could hide a real error.
- **The race is simulated by wrapping `os.lstat`, not by mocking `os.open` to raise `ELOOP`.**
  The test then proves that a real swap is handled, not only that one errno is caught.

**What to read first:** the three changed blocks in `_judge` (about 15 lines). Check the
`except OSError` order (`FileNotFoundError` first), and that `opened` is used for both checks.
Then the one-line condition in `compose`.

**Noticed, not done:**
- **A FIFO swapped in between the `lstat` and the `open` blocks the render.** `os.open` with
  `O_RDONLY` on a FIFO waits for a writer, and the renderer holds `render.lock` while it waits.
  Adding `O_NONBLOCK` to that `open` would close it: the `fstat` check that follows already
  rejects a FIFO. It needs only someone with write access to `hot/`, so it is low severity.
- **The CLI does not say when a full render is over the limit.** `full mode, every one in full`
  is true, but a person watching the hot tier grow might want "(9,400 characters, over the
  8,000 limit; index mode starts after 10 assertions)". This is wording, for task 8 or later.
- Task 7's open items (D107 staging, cross-file consistency, `F_FULLFSYNC`, a symlinked
  `hot/`) are unchanged.

**Cost:** pending.
