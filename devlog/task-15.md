# Task 15: srt profile for the server, and the tests that hold it

Issue #12. Branch `task/15`. One commit. Built by the walle-author agent (Claude Sonnet 5.5).

**Shipped:**
- `src/walle/sandbox.py` (new). `srt_settings(root, principal)` builds the `srt` settings dict
  and `srt_settings_json` renders it. It only builds a dict: no file read, none written, no
  process started. A bad principal raises `ValueError` before anything exists.
  - Reads: `denyRead` is the home folder and `<root>/principals`; `allowRead` re-allows this
    principal's folder and the Python that runs the server (`sys.prefix`, `sys.base_prefix`,
    walle's own folder, absolute existing `sys.path` entries), plus every symlink on the way
    from `sys.executable` to the real binary, each by its own name (`_symlink_hops`).
  - Writes: `allowWrite` is the principal's `log/` folder and nothing else.
  - Network: `allowedDomains` is empty.
  - Every path is absolute and symlink-resolved, because `srt` matches resolved paths. The
    links on the way to the interpreter are the exception: each is named as itself, with its
    parent folder resolved.
- `src/walle/mcp_server.py` (changed). `walle-mcp --srt-profile` prints the profile on stdout
  and exits, after `build_server` has validated the names. The module docstring's pointer to
  task 15 now says what the profile does to the file-mode behaviour.
- `tests/test_sandbox.py` (new). 20 tests, described under Verify.
- `README.md` (changed). "Running the server sandboxed": the two commands and what the profile
  allows.

**Why:**
- **D52.** We are the third party. A memory layer that cannot run sandboxed has no business
  asking to be trusted.
- **D99.** The Phase 0 server ships its own `srt` profile: read the profile folder, write only
  the log, no network, and is tested under it.
- **Hard constraint 6.** Denying `<root>/principals` and re-allowing one folder makes another
  principal's facts unreadable by the OS, not by a resolver.
- **Existing file-mode behaviour.** Task 13 already made the server survive a read-only
  profile: `remember` writes only the log, and `recall` and `why` fall back to the index as
  last built and say so. The profile relies on exactly that; it adds no server code for it.

**Verify:** "server passes tests sandboxed: profile folder read, log write only, no network."
- Profile content: `test_the_profile_writes_the_log_folder_and_nothing_else`,
  `..._allows_no_network`, `..._reads_the_profile_folder_and_denies_every_other_principal`,
  `..._lets_the_interpreter_run`, and the path-shape, relative-root, bad-name and
  not-yet-created-folder tests.
- The command: prints the profile, changes nothing on disk, refuses a bad device before
  printing.
- The same split as real file modes (`test_the_server_stages_into_the_log_when_everything_else_is_read_only`):
  principal folder read-only except `log/`, then `remember` appends exactly one event and
  `recall` answers "as last built". It runs where modes are enforced and skips, found by
  trying to write, where they are not. Nothing in the suite asks who the user is or assumes
  root.
- Under the real `srt` (four tests): the server runs and `remember`/`recall` work; a write to
  the log folder succeeds and a write beside it is refused; Maya's log is readable and
  Ravi's is not; a loopback connection is refused.
- **The `srt` tests are confirmed on macOS by the owner, not by the author.** `srt` cannot
  be installed or run in the author's environment (npm returns 403, no bubblewrap), so the
  author has never run them and makes no claim about their result. They skip with the
  reason when `srt` is missing or cannot start a sandbox, so CI, which does not install `srt`,
  skips them too.
  - Owner's run, macOS on Apple Silicon, `srt` 0.0.81, against the head before the fix below:
    the profile enforces what it claims. Probed by a script under `srt --settings`: reading and
    writing the own log were allowed. Reading or writing another principal, writing elsewhere
    in the own profile folder, listing `~/.ssh` and the network were all refused.
  - Same run, `tests/test_sandbox.py`: 3 failed, 14 passed, from quoting in the test harness.
    Fixed below in the first fix.
  - Owner's second run, at `3258186`, `srt` 0.0.81: 15 passed, 3 failed. The quoting was fixed,
    and the three failures uncovered a **product bug in `sandbox.py`**, not a test bug. The
    profile `walle-mcp --srt-profile` printed would have stopped the server starting under
    `srt` for anyone whose Python is reached through a symlink under home, which is the uv
    default. Fixed below in the second fix. The earlier line in this log, saying the failures
    were the test harness and not the profile, was right only for the first run.
  - Whether the tests pass at the second fix's head is for the owner's next macOS run to
    confirm.
- Checks, at the second fix's head: `uv run pytest` 949 passed, 10 skipped (the merge from
  `main` brought in the extra tests). 5 of the skips are this task's: 4 need `srt`, and 1 needs a process that
  cannot write a read-only folder, which this environment's user can. `ruff check` and
  `ruff format --check` are clean, and `pyright` reports 0 errors.

**First fix, after the owner's first macOS run (one further commit):**
- Cause, as the owner found it: `_inside` passed `python -c <code>` to `srt` as separate
  arguments. `srt` 0.0.81 joins its arguments with spaces, without escaping, and runs the
  result in a shell, so the quotes and brackets in `code` broke the shell parse. `srt` also
  has a `-c <command>` option of its own, so the `-c` meant for Python could be taken by `srt`.
  Either way, the command never ran as written.
- Change, `tests/test_sandbox.py` only: a new helper, `_srt_argv`, gives
  `srt --settings <file> -c <shlex.join(command)>`, so `srt` receives one string that the
  shell splits back into the original argv. `_inside`, the `_srt_or_skip` probe and the server
  transport in `test_the_server_passes_under_srt` all use it now. No assertion changed.
- New test, `test_a_command_reaches_srt_as_one_string_it_does_not_reparse`, written first and
  seen failing with the same shell syntax error. It runs `_inside` through a stand-in `srt`
  that copies 0.0.81's argument handling (it joins unescaped and honours `-c`) and enforces
  nothing, then writes a file whose path has a space and a quote. It runs in CI, where the real
  `srt` tests skip.
- `test_the_server_passes_under_srt`, checked for the same cause: its transport had the same
  separate-argument wiring, so it now goes through `_srt_argv` as well. But the same cause may
  not explain its MCP error. Through the 0.0.81-style stand-in, the old wiring for the server
  **passed** here, because its arguments (interpreter path, `-m walle.mcp_server`, `--root`,
  `--principal`, `--device`) hold no quote, space or bracket for the shell to re-parse.
  Unless a path on the Mac holds such a character, the MCP error probably has another cause.
  It did: see the second fix.
- Through the same stand-in, with the fix: the server test passes, the allowed halves of the
  write and read tests pass, and all three refusal assertions fail, as they must when nothing
  enforces. That stand-in run was a one-off and is not in the diff.
- Stranger pass on the fix: `shlex.join` quotes for a POSIX shell, which is what `srt` runs the
  `-c` string with on macOS and Linux. Windows `srt` is out of scope here.

**Second fix, after the owner's second macOS run (one further commit). A product bug:**
- Symptom, from the owner's run: every failure was `bash: .venv/bin/python: Operation not
  permitted`. The server test's MCP "Connection closed" was the same refusal: the server never
  started.
- Cause, as the owner measured it: `.venv/bin/python` links to
  `~/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12`, and that
  `cpython-3.12-...` folder is itself a link to `cpython-3.12.14-macos-aarch64-none`.
  `_interpreter_paths` resolved every entry, so only the final `cpython-3.12.14` folder was
  allowed. The link folder stayed under the home denial, and macOS refused the exec (rc 126).
  The owner also measured that allowing all of `~/.local/share/uv/python` gives rc 0 with
  `import walle` working, and `~/.ssh` still refused.
- Why the earlier tests missed it: they checked that the *resolved* interpreter was inside
  `allowRead`, which was true. Nothing checked the links on the way, and the stand-in `srt`
  enforces nothing.
- Change, `src/walle/sandbox.py`: `_symlink_hops(path)` walks `sys.executable` one component at
  a time, as the OS does. It records every symlink it meets, each named as itself with its
  parent folder already resolved, which is the name read when the link is followed. It stops
  after 40 links (a loop) or at a link it cannot read, and then the profile only lacks hops, so
  nothing is widened. `_interpreter_paths` adds each hop under the same drops as before,
  checked on both the hop and what it resolves to: nothing that is or holds home, and nothing
  that holds or sits inside `principals/`. This allows the links themselves, as the owner
  preferred, not the whole uv folder.
- On this Linux machine the profile now gains exactly the two links of the same uv layout:
  `.venv/bin/python` and `~/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu`.
- Whether `srt` honours an `allowRead` entry that is itself a link: the author read `srt`'s
  current source on GitHub (`main`, not the 0.0.81 tag). It keeps a link's own spelling when
  the link points at a sibling, as `cpython-3.12` does, and reads an `allowRead` link as naming
  the link, with its parent resolved. That matches what this change emits. Whether 0.0.81
  behaves the same is for the owner's macOS run to show. If it does not, the fallback is the
  owner's measured wider allow of the uv Python folder.
- New tests, both written first and seen failing, both run in CI with no `srt`:
  - `test_every_link_on_the_way_to_the_interpreter_is_allowed_by_its_own_name`: a synthetic
    uv layout under a tmp home. The venv's `python` links to `python3`, a link to a link, which
    goes through the `cpython-3.12` folder link to the real `cpython-3.12.14` folder. All three
    links must be in `allowRead`, and the home folder must not be, nor any entry that holds it.
  - `test_a_link_on_the_way_that_holds_home_or_principals_is_not_allowed`: the same interpreter
    reached through a link to home's parent, then through a link to `principals/`. Neither
    alias link is allowed, and the uv links behind the first still are.
- Stranger pass on the fix: the walk only reads link targets (`readlink`, `is_symlink`), runs
  outside the sandbox, opens nothing and swallows only a link it cannot read, which leaves
  the profile narrower, never wider. A `..` after a link is taken from the resolved folder,
  as the OS does. The docstrings of the module, `srt_settings` and `_interpreter_paths` now
  say the links are the one exception to "every path is resolved", so none of them lies.

**Stranger pass:**
- Found: `allowRead` beats `denyRead`, so a `PYTHONPATH` entry above `principals/` would have
  re-allowed every principal's folder through the interpreter paths. Fixed: entries that hold
  or sit inside `principals/` are dropped. Test: `test_an_import_path_that_holds_the_principals_does_not_reopen_them`,
  written to fail first.
- Not an issue: no IO beyond `Path.resolve` and `exists`; no lock, no error path swallowed.
  `Path.home()` raises if no home can be found, which refuses loudly rather than emitting a
  profile with no home rule.

**Review fix (first review failed, medium + 2 low):**
- Medium (srt tests never ran, CI skips them): still true, and not something this author can
  clear, because `srt` and `bwrap` cannot be installed here and CI is a `.github/` path.
  Fixed the part that was in reach: README no longer states the denials as proven; it says the
  profile is untested under `srt` until the four tests are run, and names `bwrap` and `socat`.
  Shravan can clear it for good by running `uv run pytest tests/test_sandbox.py -rs` on a
  machine with `srt` and recording the result, or by overriding the status.
- Low 1 (home-holding import path voids the home denial): fixed. `_interpreter_paths` drops an
  entry that is the home folder or holds it. Test, written to fail first:
  `test_an_import_path_that_holds_the_home_folder_does_not_reopen_it`.
- Low 2 (loopback probe passes in a fresh network namespace regardless): not changed. A
  non-loopback probe needs a reachable address, which a CI or sandbox host may not offer, and
  the choice is a test-design call; left under *Noticed, not done*.

**Review:** `scripts/review.sh` did not run: `ocr` is not installed in the author's
environment (it exits 2 and says so). No independent pre-commit review happened; the PR review
is the first.

**Choices made inside the task:**
- Generate the profile from `--root`/`--principal` instead of shipping a fixed JSON file: the
  paths are per-install, and a static file would need hand-editing, which is how a profile gets
  loosened.
- Deny `<root>/principals` as well as home, because a root outside the home folder would
  otherwise leave other principals readable.
- Writable set is the whole `log/` folder, not one stream file: the writer may create the
  device's stream file and cannot do so in a folder it cannot write.
- No index write access, per D99's "write only the log". The cost is that `recall` can be
  behind the log until `walle` refreshes the index outside the sandbox.
- A test of the whole pytest suite *run under* `srt` is not added: it would need a second
  profile (temp dirs, cache dirs) that is not the server's, and `scripts/` and `.github/` are
  CODEOWNERS paths I may not change. The server itself is what runs under `srt` here.

**What to read first:** `_interpreter_paths` and `_symlink_hops` in `sandbox.py` (the one place
the profile can be widened by something outside walle, and the bug that would have stopped the
server starting), then `_srt_argv` and the four `srt` tests. The owner ran them on macOS; the
author never has.

**Noticed, not done:**
- Owner to re-run `uv run pytest tests/test_sandbox.py -rs` on macOS with `srt` at this head.
  If they pass, add `srt` to CI (a `.github/` change for Shravan).
- Hops are walked only for `sys.executable`. `sys.base_prefix` and the `sys.path` entries are
  still resolved only. In the uv layout they pass through the same `cpython-3.12` link, which
  is now allowed, but a layout that links a library folder separately would not be covered.
- If the server test still fails after this fix, candidates not checked, because `srt` cannot
  run here: the server reading something under the denied home folder at start-up (fastmcp's
  settings look for `.env` in the working directory, and on Python 3.12 `Path.is_file()` raises
  on EPERM rather than returning False), or `srt` writing to stdout.
- `test_under_srt_there_is_no_network` passed in the owner's failing run only because the
  broken command exited non-zero: it asserts only `returncode != 0`, so a command that never
  ran passes it. Having the probe exit with a code of its own when the connection is refused,
  and asserting that code, would make the pass mean something.
- The README still calls the profile untested under `srt`. The owner's probe has since
  confirmed it on macOS, so the README is now behind the evidence.
- On Linux `srt` needs `bwrap` and `socat`; the README does not say so yet.
- `test_under_srt_there_is_no_network` probes loopback, which a fresh network namespace refuses
  by itself, so it would pass even with `allowedDomains` set. A non-loopback probe would
  prove more.
- The profile has no `deniedDomainReasons`; with no allowed domains there is nothing to explain.

**Cost:** not available to the author; Hyperagent holds it.
