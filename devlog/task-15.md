# Task 15: srt profile for the server, and the tests that hold it

Issue #12. Branch `task/15`. One commit. Built by the walle-author agent (Claude Sonnet 5.5).

**Shipped:**
- `src/walle/sandbox.py` (new). `srt_settings(root, principal)` builds the `srt` settings dict
  and `srt_settings_json` renders it. It only builds a dict: no file read, none written, no
  process started. A bad principal raises `ValueError` before anything exists.
  - Reads: `denyRead` is the home folder and `<root>/principals`; `allowRead` re-allows this
    principal's folder and the Python that runs the server (`sys.prefix`, `sys.base_prefix`,
    walle's own folder, absolute existing `sys.path` entries).
  - Writes: `allowWrite` is the principal's `log/` folder and nothing else.
  - Network: `allowedDomains` is empty.
  - Every path is absolute and symlink-resolved, because `srt` matches resolved paths.
- `src/walle/mcp_server.py` (changed). `walle-mcp --srt-profile` prints the profile on stdout
  and exits, after `build_server` has validated the names. The module docstring's pointer to
  task 15 now says what the profile does to the file-mode behaviour.
- `tests/test_sandbox.py` (new). 18 tests, described under Verify.
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
  - Same run, `tests/test_sandbox.py`: 3 failed, 14 passed. The failures were the test harness,
    not the profile. Fixed below. Whether the fixed tests pass on macOS is for the owner's
    re-run to confirm.
- Checks, at the fix head: `uv run pytest` 947 passed, 10 skipped (the merge from `main` brought
  in the extra tests). 5 of the skips are this task's: 4 need `srt`, and 1 needs a process that
  cannot write a read-only folder, which this environment's user can. `ruff check` and
  `ruff format --check` are clean, and `pyright` reports 0 errors.

**Fix after the owner's macOS run (one further commit):**
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
  See *Noticed, not done*.
- Through the same stand-in, with the fix: the server test passes, the allowed halves of the
  write and read tests pass, and all three refusal assertions fail, as they must when nothing
  enforces. That stand-in run was a one-off and is not in the diff.
- Stranger pass on the fix: `shlex.join` quotes for a POSIX shell, which is what `srt` runs the
  `-c` string with on macOS and Linux. Windows `srt` is out of scope here.

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

**What to read first:** `_interpreter_paths` in `sandbox.py` (the one place the profile can be
widened by something outside walle), then `_srt_argv` and the four `srt` tests. The owner ran them on macOS; the author
never has.

**Noticed, not done:**
- Owner to re-run `uv run pytest tests/test_sandbox.py -rs` on macOS with `srt` at this head.
  If they pass, add `srt` to CI (a `.github/` change for Shravan).
- If `test_the_server_passes_under_srt` still fails, the MCP error text and the server's
  stderr will name the cause. Candidates not checked, because `srt` cannot run here: the server
  reading something under the denied home folder at start-up (its working directory is the
  repo, and fastmcp's settings look for `.env` there, and on Python 3.12 `Path.is_file()`
  raises on EPERM rather than returning False), or `srt` itself writing to stdout. An
  unreadable working directory alone did not stop the server importing on Linux.
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
