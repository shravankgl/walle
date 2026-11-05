# Task 25–26: Packaging — one install command, one config line, the measured table

Issue #18. Branch `task/25`, cut from `main` at `c17fef6`.

**Shipped:**

- `README.md`: a new **Install** section (one `uv tool install` command, one `claude mcp add`
  line, the first-run loop, uninstall); a **Measured** table where every number has the command
  that produced it and the unmeasured rows say *not measured*; a **What works and what is
  untested** table; a **Support** section. The hook and MCP sections now use the installed
  console scripts, not a checkout's `.venv`, and drop "not usable yet".
- `src/walle/bench.py`: `walle bench`, the command behind the persist / replay / recover rows
  (and the hot-tier rows for synthetic profiles). Writes synthetic events into a fresh folder
  through the real writer, then times one append, a full fold from the log, and a full index
  rebuild, and checks the rebuilt state hash.
- `src/walle/cli.py`: registers `bench` (two lines).
- `tests/test_packaging.py`: the verify criterion (below).
- `tests/test_bench.py`: `walle bench`'s numbers, its refusals, and that it never claims a
  removal that did not happen.
- `tests/test_hook.py`: **edited because the task requires it.** Two tests read the README's
  hook snippet and expected the old `/path/to/walle/.venv/bin/walle-hook` form. The snippet now
  points at the installed script (`/Users/you/.local/bin/walle-hook`), so the placeholder strings
  they substitute and the path suffix they check changed with it. What they check did not: no
  matcher, an outer timeout above the 2 s budget, and a cold start through `sh -c` under budget.

**Why:**

- D53: walle ships as an open-source repo with a one-line install, not an app. PLAN §2 spells
  that out as `uvx`/`pipx`, no clone-and-build, plus a copy-pasteable MCP config for Claude Code.
- D69: the README states a *measured* resident-context number and an honest Working / Untested
  table. Here every number was measured by a command shown beside it.
- D102: the schema cost is reported beside the hot-tier number (368 tokens, cap 400, already a
  test from task 13–14).

**Verify:** PLAN §9: *fresh machine: one command to install, one config line to run.*

`tests/test_packaging.py` holds the README to two exact lines, `INSTALL` and `CONFIG`, then does
what they say: it builds this checkout's wheel and runs the install command with the wheel in
place of the GitHub source, using an empty `HOME`, its own uv tool and bin folders, and `PATH`
cut to `/usr/bin:/bin`. It runs `walle --version`, then takes the command after `--` in the
config line (`walle-mcp --root ~/walle-data --principal me --device mac`, with `~` expanded
against the fresh home), starts it with a real MCP stdio client, lists the tools
(`recall`, `remember`, `why`), stages a memory and finds the log file on disk. The data folder
does not exist beforehand.

It failed before the change, for two reasons, and the second one was a real bug:

1. The README had neither line.
2. With the plain command (`uv tool install <source>`), the install **failed**: `the current
   Python version (3.9.25) does not satisfy Python>=3.12`. uv picks the interpreter for a tool
   before it reads the package's `requires-python`, and in a fresh home it found
   `/usr/bin/python3`, which is 3.9. A fresh Mac's `/usr/bin/python3` is also 3.9, so the README's
   command would have failed on exactly the machine the criterion names. With `--python 3.12`, uv
   downloads its own 3.12 and the install works.

Check output on the final tree:

```
$ uv run pytest
1474 passed, 10 skipped in 58.62s
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
113 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

The 10 skips are the ones on `main` (the `srt` tests and others that need tools absent here).

**What the fresh-machine check proved, and what it did not.** The sandbox is a 2-core Linux
x86_64 container, not a Mac.

Proved here:

- The README's exact install command, against GitHub, installs `walle`, `walle-hook` and
  `walle-mcp` with an empty home folder, an empty uv cache and Python folder, and **neither `git`
  nor any Python on `PATH`**. uv downloaded CPython 3.12.16 itself. It took 3.2 s. (Run by hand.
  It installs `main`, at `6cda300` when run, so it proves the source URL and the build, not this
  branch's code.)
- The same with a `git+https://` URL needs `git`, which a fresh Mac lacks until the Command Line
  Tools are installed. That is why the README uses the archive URL instead.
- This branch's wheel installs the same way and the config line's command serves MCP from an
  empty data root (the test above, run in the suite).
- The first-run loop works on a fresh install, by hand: `walle remember` → `walle review`
  (kept) → `walle render` → `walle-hook --measure` injected 2,065 characters.

Not proved, left for the owner's Mac:

- that `curl -LsSf https://astral.sh/uv/install.sh | sh` works there, and that uv's Python
  download works on macOS arm64;
- that the uv tool bin folder (`~/.local/bin`) is on the `PATH` Claude Code starts servers and
  hooks with. If it is not, the server is not found; the README says to run `uv tool
  update-shell`, and the hook snippet uses a full path for this reason;
- that `claude mcp add walle -- walle-mcp …` registers the server and that a real Claude Code
  session calls it. Claude Code was not run here, and the hook has never been exercised by an
  agent inside a real session;
- every number in the Measured table on a Mac. `fsync` dominates persist and is very different
  on a laptop SSD; re-run `walle bench`.

Suggested check on the Mac, from a new user account or after `uv tool uninstall walle`:
the two README lines, then ask Claude to remember something, then `walle review`.

**How each measured number was produced** (all on 2026-11-05, in the sandbox, Python 3.12.16):

```
$ uv run walle bench
walle bench: 1,000 synthetic events in a temporary folder, now removed
persist   one fsync'd append: median 0.18 ms, p95 0.21 ms (1,000)
replay    read and fold 1,000 events from the log: median 14.59 ms, p95 16.13 ms (5 runs)
recover   delete the index, rebuild it from the log, fold: median 64.53 ms, p95 113.68 ms (5 runs)
          state hash equal after rebuild: yes
hot tier  800 live facts: 63,633 characters, about 15,909 tokens (characters / 4), index mode
machine   Linux x86_64, Python 3.12.16, cpython

$ uv run walle bench --events 100 --root /tmp/walle-bench-100
persist   one fsync'd append: median 0.17 ms, p95 0.23 ms (100)
replay    read and fold 100 events from the log: median 1.47 ms, p95 1.66 ms (5 runs)
recover   delete the index, rebuild it from the log, fold: median 7.50 ms, p95 8.88 ms (5 runs)
          state hash equal after rebuild: yes
hot tier  80 live facts: 8,911 characters, about 2,228 tokens (characters / 4), index mode

$ echo '{"hook_event_name": "SessionStart"}' | uv run walle-hook --root /tmp/walle-bench-100 --principal bench --measure
walle hook: injected 8,911 characters, about 2,228 tokens (characters / 4, an estimate), in 3 ms after start-up

$ uv run walle-mcp --root /tmp/walle-bench-100 --principal bench --device bench --measure
tool schema: 1,102 characters, about 368 tokens (cap 400)

# on the fresh install, after remember / review / render of one fact
$ echo '{"hook_event_name": "SessionStart"}' | walle-hook --root ~/walle-data --principal me --measure
walle hook: injected 2,065 characters, about 517 tokens (characters / 4, an estimate), in 3 ms after start-up
$ time (for i in $(seq 20); do echo '{"hook_event_name": "SessionStart"}' | walle-hook --root ~/walle-data --principal me >/dev/null; done)
real	0m0.581s
```

The bench numbers were taken before the stranger-pass fix below, which changed only how the
temporary folder is removed afterwards, not anything that is timed. The 1-fact and hook-latency
rows were measured with a build of `main` (no bench yet); the hot tier and hook code are the same
on this branch. Install times (2.8 s, 3.2 s, 3.5 s) are `time` on three fresh-home installs:
this wheel, the archive URL, and a `git+` URL.

**Two-runner walkthrough:** `walle bench` writes a log, an index and a hot tier, but only in a
folder it owns. With no `--root`, each run gets its own `mkdtemp` folder, so A and B never meet.
With the same `--root`, both check that it is empty before either writes; if both pass, both
append to `d-bench.jsonl`. The writer's lock keeps the seqs contiguous and no line is torn, but
each bench assumes its own n-th append got seq n, so its supersedes can name the other run's
events, and both sets of timings include waiting for the other's lock. The state hash check
still holds (both fold the same log). That is a polluted measurement, never a damaged log, in a
folder made for the purpose; not guarded further.

**Stranger pass** (bench and the packaging test, both touch IO and error paths):

- Found: the temporary folder was removed with `shutil.rmtree(..., ignore_errors=True)` while
  the command printed "a temporary folder, now removed". A failed removal would have been a
  status message that lies, and a leak in `/tmp`. Fixed: on success, removal raises if it fails
  and the command reports it as an error (`OSError` → a click error naming the folder); on a
  failure during the run, removal is best effort and the original error is the one raised. Test
  added (`test_a_temporary_folder_that_cannot_be_removed_is_an_error_not_a_claim`), which itself
  first leaked a folder into `/tmp` and was pointed at `tmp_path`.
- Checked: `--root` that is a file or a non-empty folder is refused before anything is
  written, and the refusal leaves the folder as it was (tested). A symlink to a non-empty
  folder is refused the same way, since `is_dir` and `iterdir` follow it (read, not tested). `--events` and
  `--runs` below 1 are refused before a folder is claimed.
- Checked: a state hash that differs after rebuild prints `NO` and exits 1; it is never assumed.
- Checked: the packaging test fails rather than skipping when uv or the package index is
  missing, because a skipped verify criterion reads as a pass. It passes the TLS certificate
  variables through, and shares only uv's download cache and Python downloads with the machine.

**Review:** not yet. The reviewer agent reviews this PR's head commit (R6). `scripts/review.sh`
was not run: the brief replaces it for agent work.

**Choices made inside the task:**

- **No `uvx walle`.** The task names `uvx walle`, but `walle` is not on PyPI (the index returns
  404 for it), so `uvx walle` cannot work, and publishing is not the agent's to do. Worse, if
  anyone registers the name, `uvx walle` would run a stranger's code, which is hard constraint 7.
  The README says plainly that walle is not on PyPI and that a `walle` package there is not this
  project. `uv tool install <archive URL>` meets D53 and PLAN §2's "no clone-and-build".
- **`uv tool install`, not `uvx --from …`.** A persistent install gives the config line a short,
  stable command (`walle-mcp`) and puts `walle` on the `PATH` for `review` and `render`. `uvx`
  would re-resolve on every server start.
- **Archive URL, not `git+`**: no `git` on a fresh Mac. It always tracks `main`; a tagged release
  would be the stable alternative once one exists.
- **`--python 3.12`**, not `>=3.12`: matches CI exactly, and uv downloads it if missing.
- **`walle bench` as a command, not a script**: `scripts/` is a CODEOWNERS path, and the README
  needs a command a user can run on an installed copy.
- **Status rows for the Google connectors, the model route and ACP say "tested against fakes
  only"**, as the owner asked. ACP is still an open PR (#66), and Calendar landed on `main`
  (#65) after this branch was cut; both rows describe the project, not this branch.

**What to read first:** the Measured table in `README.md` beside the command log above, and
`tests/test_packaging.py`'s docstring for exactly what the verify test can and cannot prove.

**Noticed, not done:**

- **The hot tier is not bounded by index mode.** At 800 live facts it is 63,633 characters,
  about six times Claude Code's 10,000-character cap, so the host would show the model a file
  path and a 2,000-character preview instead (D81's index mode keeps a summary line for every
  fact). At 80 facts it is 8,911, already near the cap. Needs a decision on a hard ceiling.
- **Reserve `walle` on PyPI**, or pick a published name, before the repo goes public (D37), so
  `uvx walle` cannot be squatted. Owner's call.
- `walle eval` defaults to `tests/corpora` relative to the working folder, so it works only
  from a checkout, not from the installed tool. Noted in the status table.
- The hook estimates tokens at characters / 4 and the MCP schema at characters / 3; the two
  README numbers use different estimates. Both are labelled.
- A tagged release, so the install command can pin a version rather than track `main`.

**Cost:** not known to the author agent when this was written; to be filled in from Hyperagent.
