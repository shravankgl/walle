# Task 8: SessionStart hook

Issue #6. Branch `task/08`.

**Shipped:**
- `src/walle/hook.py` is the Claude Code `SessionStart` hook, run as
  `walle-hook --root <folder holding principals/> --principal <name> [--measure]`. It reads the
  payload on stdin, takes a shared lock on `hot/render.lock`, reads the hot tier task 7
  rendered (`identity.md`, `user.md`, `rules.md` if present, `learned.md`, in `ORDER`), and
  writes one JSON object to stdout:
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}`.
  Past a 2 s budget, or on any error, it writes nothing to stdout or stderr and exits 0.
  `--measure` adds one line on stderr: characters injected, the estimated token count, and a
  warning when the context is over Claude Code's 10,000-character cap.
- `pyproject.toml`: one line, the `walle-hook = "walle.hook:main"` console script. It is not
  a `walle` subcommand on purpose; see *Choices*.
- `README.md`: a new section, "Install the SessionStart hook", with the snippet for
  `~/.claude/settings.json`. It is documentation only. Nothing in walle reads or writes any
  Claude Code settings file; the person pastes it in.
- `tests/test_hook.py`: 15 tests (29 cases). Every test of the contract with the host runs the
  installed console script as a fresh process with the payload on stdin, and the cold-start
  test runs the README snippet's own command through `/bin/sh -c`. Fixtures are synthetic: a
  made-up person's dinners, fern and basil, and a fake payload with a zero session id.

**Why:**
- **D54, D83.** The hot tier is injected by the host's hook, not fetched by a tool the model
  may never call. This is D83's first surface. **D90:** the event is Claude Code's
  `SessionStart`, the name D90 adopts as the shared vocabulary.
- **D115.** "A memory layer that can hang the agent it serves will be uninstalled the first
  time it does." I checked Claude Code's hooks reference: a command hook's default timeout is
  **600 s**, and a `SessionStart` hook that times out counts as no output and the session goes
  on. So the host does not protect a session from a slow hook for ten minutes, and the 2 s
  budget has to live in the hook. It is a `SIGALRM` timer started first thing in `main`. The
  handler raises, which interrupts whatever is blocking: the wait on `render.lock`, a stdin
  that never closes, a slow disk. Every exit path then writes nothing and exits 0, including
  bad arguments and `--help` (argparse would print usage to stdout and exit 2, and the host
  would read either as a hook failure or as context).
- **D115, "the hook's normal path is reading a file, not building one".** The hook never
  renders, never opens the log or the index, and imports none of that code. The only thing
  that can make it slow is a render in progress, which is what the verify criterion stages.
- **D102.** Resident context gets a number. `--measure` prints the hot tier's size in
  characters and estimated tokens, and the cold-start test prints both too.
- **D118.** "Python now; the first Rust port candidate after M1." The cold start below is the
  number that decides whether that port is ever needed.

**Verify:** the criterion from PLAN §9, part by part:
- *Fake hook payload on stdin → JSON with the hot tier:*
  `test_a_fake_payload_gets_the_hot_tier_as_json`. A real render plus a `rules.md`. stdout is
  one JSON object with `hookEventName: SessionStart`, and each of the four files' full text
  appears in `ORDER`, with `rules.md` above `learned.md`. stderr is empty and the exit is 0.
  `test_no_rules_file_is_fine_and_the_rules_are_labelled_when_there_is_one` covers the absent
  and labelled cases.
- *Measured token count printed:* `test_the_measured_token_count_is_printed` parses the
  `--measure` line and checks its numbers against the JSON actually emitted (characters, and
  tokens = ⌈characters / 4⌉). `test_cold_start_through_the_install_snippet` prints the cold
  start and the token count. On the three-assertion fixture:

```
walle hook: injected 2,380 characters, about 595 tokens (characters / 4, an estimate), in 3 ms after start-up
cold start via the install snippet: median 31 ms, worst 32 ms over 7 runs; injected 2,380 characters, about 595 tokens
```

- ***A render made slow emits nothing and the session still starts:***
  `test_a_render_made_slow_emits_nothing_and_the_session_still_starts`. A second process runs
  the real `hot.render` with `index.update` slowed by 5 s. That happens *inside*
  `render.lock`, which is what a big log or a slow disk does. While it holds the lock, the
  hook is run. It must give stdout `b""`, stderr `b""`, exit 0, and take between 0.95 × and
  budget + 1 s. The test also asserts that the render was still running when the hook gave up,
  so it cannot pass by accident. Once the render finishes, the next hook run carries the new
  assertion: D115's "the *next* session gets it". "The session still starts" is checked as
  what the host sees: exit 0 and no output. I checked Claude Code's reference: that is a
  successful hook with nothing to add.
- Around it:
  - `test_a_short_render_is_waited_out_not_given_up_on`: a 0.4 s render is waited for, and the
    new render is what gets injected.
  - `test_stdin_that_never_closes_still_ends_within_budget`.
  - Six broken hot tiers, all silent, exit 0, and faster than the budget:
    - a symlinked `learned.md`
    - a `learned.md` that is not UTF-8
    - a FIFO for `rules.md`
    - a `rules.md` over 1 MiB
    - no `learned.md`
    - no `render.lock`
  - Ten kinds of bad input, all silent, exit 0:
    - a payload that is not JSON, or not an object
    - a payload for another event, or with no event name
    - a payload over 1 MiB
    - an unsafe principal (`../maya`)
    - a missing `--principal`
    - an unknown option
    - `--help`
    - a principal with no render
  - `test_the_hook_writes_nothing_and_never_touches_claude_settings`: with `HOME` pointed at a
    temp folder holding `~/.claude/settings.json`, every path under the data root and under
    `HOME` has the same mode, size, mtime and bytes after a successful run and after a failed
    one.
  - `test_importing_the_hook_stays_light`: importing `walle.hook` loads no `pydantic`,
    `click`, `sqlite3`, `walle.hot`, `walle.index` or `walle.events`.
- **The tests were written first.** Before `hook.py` existed, collection failed with
  `ModuleNotFoundError: No module named 'walle.hook'`. Once the code passed, I broke it on
  purpose, one change at a time, to check that the key tests can fail:
  - No alarm: the slow-render test and the stdin test fail.
  - No `flock`: the slow-render and short-render tests fail. The hook then reads mid-render
    and emits the old tier.
  - No event-name check: the two wrong-event cases fail.
  - `import walle.hot` in the hook: the light-import test fails.
  - Letting non-`Silent` errors escape: the unsafe-principal case fails with a traceback and
    exit 1.
  - Dropping `O_NOFOLLOW`: the symlink case fails. Dropping `O_NONBLOCK`: the FIFO case fails
    (it waits out the budget).
  - Making `rules.md` required: the no-rules case fails.
  - `add_help=True`: the `--help` case fails. My first try at this one changed only a comment
    and every test passed. I re-ran it against the real line, and it fails.
  - I re-ran the four timing-sensitive tests three more times to check for flakes. All
    passed. The slow-render test takes about 5 s, because it waits for its render to finish.
- The four checks, run on the final tree:

```
$ uv run pytest
276 passed, 3 skipped in 15.94s
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
38 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

  The 3 skips predate this task (`test_index.py`'s read-only-folder tests, which skip in this
  sandbox). `test_hook.py` has none.

**Cold start, measured the way the hook is really invoked.** The README snippet's command
(`<repo>/.venv/bin/walle-hook --root … --principal maya`) run through `/bin/sh -c`, as a fresh
process each time, with the payload on stdin, in this sandbox (Linux, Python 3.12):

| | wall time |
|---|---|
| first run after `uv sync`, no bytecode cache | 34 ms |
| next 20 runs | median 31 ms (30–34) |
| `python3 -c pass`, for scale | 10 ms |
| `walle --version` (click, pydantic), for comparison | 144 ms |
| `python -c "import walle.hot"`, what importing the renderer would cost | 135 ms |

`python -X importtime -c "import walle.hook"` totals 16 ms, mostly `argparse`, `pathlib`,
`enum` and `typing`. That leaves about 1.97 of the 2 s for reading, and reading takes 3 ms.
D118's case for Rust ("buys headroom that already exists") holds more strongly than it did
for the CLI. The budget starts in `main`, so start-up is not inside it. That is why the
snippet also sets the host's `timeout` (10 s) as the guard behind it.

**Two-runner walkthrough:** the hook takes a lock and reads derived files. The same text is in
the module docstring. Take hook H and renderer R for the same principal:

1. R takes `hot/render.lock` exclusively and renders: index update, fold, the files, and the
   manifest twice.
2. H opens `render.lock` read-only (`O_NOFOLLOW`; it never creates it) and asks for
   `LOCK_SH`. H blocks until R is done, so it never reads a new `identity.md` beside an old
   `learned.md`. That was the cross-file gap task 7 left under *Noticed*.
3. R releases. H gets the shared lock, reads the four files, closes the lock fd (which
   releases it) and only then builds and writes the answer. If R runs past the budget, H's
   alarm fires during the wait. H emits nothing and exits, and its fd closes with the process.
   R is unaffected, and the next session reads its render.
4. If H holds the shared lock first, a render R that starts then waits for H, which holds it
   for the few milliseconds of four reads. Two sessions starting together take two shared
   locks and do not wait for each other. A hook that hangs while holding the lock is killed
   by its own alarm at 2 s, so a render can wait at most that long for it.
5. A person P editing a rendered file takes no lock, as in task 7. Editors and the renderer
   both replace whole files, so H reads either the old file or the new one. A kept edit
   (D107) is injected as it is on disk, which is what task 7's CLI message promises: "the hot
   tier carries your version until you deal with it."

Lock order: H takes only `render.lock`, and never while holding anything else. No cycle with
task 7's `render.lock` → `index.lock` → stream order.

**Stranger pass:** done because the diff is over 30 lines and is IO, a lock and almost
entirely error paths. I re-read `hook.py` cold. Found and fixed:
- **A status line that misled.** `--measure` said "in 3 ms", but that clock starts in `main`,
  after Python's start-up, so it reads as the hook's whole cost when the real cost is about
  31 ms. It now says "in 3 ms after start-up", and the docstring points to the cold-start test
  for the whole command.
- **An fd leaked on an error path.** In `_open`, if `fstat` raised, the descriptor was never
  closed. That is harmless in a process about to exit, but `_open` is a function that should
  not leak. It now closes on any failure after the open.
- **An alarm landing in the write.** The first draft cancelled the timer and then wrote. An
  alarm the kernel had already delivered, but Python had not yet run the handler for, could
  still raise inside `os.write` and leave a fragment of JSON on stdout. The host treats
  `SessionStart` stdout that is not one JSON object as plain context, so the model would read
  the fragment. The handler is now set to `SIG_IGN` right after the cancel, which drops a
  pending alarm. That ordering is commented in the code.
- **The README's manual-run line** assumed `walle-hook` was on `PATH`. It now uses the venv
  path, as the snippet does.
- Looked at and left alone:
  - Every `except` either turns the error into the silent path or is the outer catch-all.
    Nothing is swallowed without leaving a reason that `--measure` prints.
  - A `BrokenPipeError` while writing stdout is caught. The `--measure` line is then not
    printed either, so it cannot claim "injected" for an answer that never arrived.
  - Python ignores `SIGPIPE` by default, so a closed pipe is an exception, not a kill.
  - A `SIGINT` from the host is caught as `KeyboardInterrupt`, and the hook exits 0 silently.
    `SIGTERM` kills it with no output, and that is the host's own decision.
  - Reading a file updates its atime on filesystems that track it. That is not a write in any
    sense the test or the brief means, and the snapshot test deliberately compares mtime.

**Review:** pending.

**Choices made inside the task:** each is mine, and any of them can be overridden.
- **Wait for a render in progress (shared lock) rather than read without the lock.**
  Lock-free would never wait, and the session would get the previous render. But during a
  render the four files can be from two different renders, and task 7 flagged exactly that.
  Renders take milliseconds, the budget bounds the wait, and the verify criterion ("a render
  made slow emits nothing") describes this design.
- **A separate `walle-hook` console script, not `walle hook`.** `walle.cli` imports click,
  the renderer, the index and pydantic, which is 135–144 ms of imports every session. That is
  inside budget, but each import is something that can fail or slow down before the alarm is
  even set. The hook imports only the standard library and `walle.paths`, and the
  light-import test keeps it that way. It copies four constants from `walle.hot`, and a test
  checks they still match.
- **The three rendered files are required; `rules.md` is optional.** Without `learned.md`, the
  injected identity text would point at a file that is not there. The cost: a person who
  deletes `learned.md` to discard a kept edit (task 7's advice) gets no memory until the next
  `walle render`.
- **Strict about everything: a symlink, bad UTF-8, a FIFO, an oversized file, a wrong event.
  Each gives nothing, not a partial tier.** The brief says "on any error it emits nothing",
  and the alternative (best-effort partial context) is harder to reason about.
  - Symlinks are refused for a reason, not only for strictness. A link planted in `hot/`
    (by anything that can write there, including an agent) would otherwise put any file the
    user can read, such as a key, in front of the model.
  - The cost: a person who symlinks `rules.md` to their own notes gets silence. `--measure`
    says why.
- **The payload must say `SessionStart`.** A hook pasted under the wrong event would
  otherwise inject the whole tier on every prompt.
- **Tokens are characters / 4, labelled as an estimate everywhere it is printed.** Claude's
  tokenizer is not available offline. Counting with the API would need a key and a network
  call, and a tokenizer library is outside the dependency allowlist.
- **Over the host's 10,000-character cap, the hook still emits, and `--measure` warns.**
  Truncating would cut `learned.md` mid-entry, and emitting nothing would drop memory
  entirely. The host saves an oversized string to a file the model can read, which is the
  least bad of the three. See *Noticed*.
- **No `matcher` in the snippet.** It therefore runs on startup, resume, `/clear` and
  compaction. After a compaction or a `/clear`, the hot tier is gone from context, and
  re-injecting it is the point.
- **The snippet's `timeout` is 10 s.** It is above the budget plus start-up, so it only fires
  if Python itself never gets to `main`.

**What to read first:** `main` in `hook.py` (about 50 lines). It covers the alarm, the order
"build, cancel, ignore, then write", and that every path ends in `SystemExit(0)`. Then
`context` and `_open` for the lock and the file checks. Then the README snippet, which is
what a person will actually copy.

**Noticed, not done:**
- **The hot tier can pass Claude Code's 10,000-character cap with no human edit.** D81's
  limit is 8,000 characters on the three rendered files. The fixed framing plus up to ten
  long assertions can exceed that (task 7's follow-up made full mode honest about it), and
  `rules.md` adds more on top. Past 10,000, the model sees a path and a 2,000-character
  preview, which is mostly `identity.md`. Whether `FULL_LIMIT` should count `rules.md` and the
  host's cap is a decision for D81, not for the hook. `--measure` makes it visible meanwhile.
- **The budget does not cover interpreter start-up.** It is measured at about 30 ms here. A
  cold disk or a network-mounted venv could be much slower, and only the host's `timeout`
  guards that.
- **D102 asks for the hot-tier token number in the README beside the tool-schema number.**
  There is no real hot tier to measure yet, so the README does not carry a number.
- **A symlinked `hot/` folder is followed.** `O_NOFOLLOW` covers only the last path component.
  This is the same as the renderer (task 7's note).
- **macOS.** Everything used is POSIX and present on Darwin: `setitimer`, `flock` on a
  read-only fd, `O_NOFOLLOW`, `O_NONBLOCK` and `O_CLOEXEC`. I could not run the tests on macOS
  here; CI is Ubuntu.
- **Windows** gets silence, not a traceback: `fcntl` is imported inside `context`, and
  `SIGALRM`/`setitimer` are missing there, so both fail into the catch-all. That is tested
  only by reasoning.

**Cost:** pending.
