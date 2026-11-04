# Task 11-cli: `walle observe`, the command that runs the transcript observer

Issue #40. Branch `task/11-cli`. One commit. Task 11 (issue #9, PR #34) built `observer.py`
but left nothing on the command line to run it. Its log listed the command under *Noticed,
not done*. This task adds that command and nothing else.

**Shipped:**
- `src/walle/cli.py` (changed), three additions:
  - **`walle observe --root DIR --principal P --device D --projects-dir DIR`.** It builds the
    `StreamWriter` first, so an unsafe principal or device name fails before anything is
    read. Then it runs `observe_projects(projects_dir)` and `append_observations(report,
    writer)`, and prints six counts:
    - turns seen;
    - appended;
    - already logged;
    - pending;
    - skipped lines;
    - duplicate entries.

    `--projects-dir` is required, has no default and reads no environment variable. It must
    be an existing directory.
  - **`_quiet_transcript_warnings()`.** For the length of the read, it puts a `NullHandler`
    on the `walle.transcript` logger. Without it, Python's last-resort handler prints every
    transcript warning to stderr: one line per skipped line, repair and conflicting
    duplicate, each naming a file path and a uuid. A real folder has hundreds. The records
    still reach any handler the caller configured. What they report is in the counts.
  - **`_without_content(exc)`.** This is the error message for `walle observe`. It is
    `_describe` with pydantic's quoting removed. The stream holds transcript text, and the
    reader's `CorruptStreamError` for an invalid line carries pydantic's message, which
    quotes the offending value. This helper keeps the reader's `<file>:<line>: …` prefix,
    and lists each failed field with pydantic's error **code** (`note: extra_forbidden`).
    It does not use pydantic's message, which can name an input as well (`Input tag 'x'
    …`).
- `tests/test_cli_observe.py` (new): 14 tests, 16 with their parameters.

No fixture was added. The tests use task 11's synthetic garden fixture as it is, plus
transcripts built in the test from made-up uuids and marker strings.

**Why:**
- **D40, and issue #9's comment:** walle reads only a folder the person names. So
  `--projects-dir` has no default, here or in `observe_projects`. A folder that does not
  exist is refused too. Otherwise a typo would read as "no transcripts" and print zeros.
- **D51:** the harness transcript is Phase 0's observation source. Until this command
  existed, nothing a person could run put a transcript into the log, and PLAN §9's M1 gate
  needs that.
- **D56:** only completed turns are observed. An unfinished turn is counted as pending and
  appended by a later run.
- **D92:** "already logged" is a fold over the stream, rebuilt every run under the stream's
  lock. So re-running is safe, with no cursor file. The command adds no state of its own.
- **D101:** turns are found through the `parentUuid` graph, and a resume's replayed entries
  are de-duplicated by `uuid` in `transcript.load`. The command reports how many duplicates
  were folded, so that number can be compared with a real folder's (834 on the issue).

**Verify:** issue #40: "a fixture folder appends N observations on the first run and 0 on
the second; a missing `--projects-dir` is refused; output contains no transcript text; two
concurrent runs append each turn once."
- **N then 0:**
  - `test_the_fixture_folder_appends_its_turns_on_the_first_run_and_0_on_the_second`. The
    garden fixture gives 3 seen, 2 appended (seqs 1 and 2) and 1 pending. The second run
    appends 0, reports 2 already logged, and leaves the stream byte-identical.
  - `test_n_completed_turns_append_n_then_0` repeats this for N = 0, 1 and 4. With N = 0, no
    stream file is created.
- **Refused:**
  - `test_a_missing_projects_dir_is_refused`: exit 2, the message names `--projects-dir`,
    and nothing is created under `--root`.
  - `test_the_projects_dir_option_has_no_default`: the option is required, has no `envvar`,
    and its default is not a path.
  - `test_a_projects_dir_that_does_not_exist_is_refused_not_read_as_empty`.
- **No transcript text:** `test_the_output_is_counts_and_holds_no_transcript_text`. It runs
  `walle` in a real subprocess, not `CliRunner`. Under pytest the root logger has handlers,
  so a last-resort warning that would reach a terminal is invisible in-process. The
  transcript puts a marker string in:
  - prompts, replies, thinking, tool names and tool inputs;
  - the `cwd` and the project folder's name;
  - a skipped line;
  - an orphan;
  - a duplicate that disagrees about its parent.

  The test checks three things. Stderr is empty. Every stdout line matches the counts-only
  pattern. The marker is nowhere in the output, but it is in the stream's bodies.
- **Errors:**
  - `test_a_refused_stream_is_reported_without_its_contents` adds an unknown field holding
    the marker to a logged observation. The run exits 1, names `<stream>:1` and the field
    `note`, and does not quote the marker.
  - `test_an_error_message_never_quotes_what_pydantic_was_given` covers `_without_content`
    on a bare `ValidationError`, and on one wrapped without the reader's ` — ` separator.
  - `test_a_bad_principal_or_device_is_refused_before_anything_is_read`: with
    `--device ../elsewhere`, the run exits 1 with no traceback and creates nothing.
- **Two at once:** `test_two_concurrent_runs_append_each_turn_once` starts two `walle
  observe` processes on the fixture and one stream. Each slows its fold by one second. The
  result:
  - one process appends 2 and the other 0;
  - seqs are `[1, 2]`;
  - there are 2 distinct bodies.
- **Real shapes from the issue:**
  - **The long turn.** `test_a_turn_of_180000_characters_is_appended_once` uses a
    180,000-character reply, past the writer's 64 KiB tail window, followed by a normal
    turn. Both are appended, and the second append finds seq 1 by widening its tail read.
    The re-run appends 0, and the output has no word of the text.
  - **Duplicates.** `test_834_duplicate_entries_from_resumed_sessions_give_one_observation_per_turn`
    uses 417 two-entry turns, replayed whole into a resume file with a new `sessionId`: 834
    duplicates. The resume adds 2 turns of its own, so the run appends 419 distinct bodies.
    A second resume replays both files into a third (838 more duplicates, 1,672 in all). It
    appends 0, with 419 already logged.
- **Tests first:** all 13 tests of the first draft failed before `observe` existed. One of
  them, the nonexistent-folder test, failed only on exit code 2. That code is also what
  "no such command" exits with, so it would have passed against the bare stub for the wrong
  reason. I tightened it to require "does not exist" in the message before writing the
  command. After the command was written, one assertion of mine was wrong:
  `option.default is None`. In Click 8.3, "no default" is a sentinel, not `None`. I changed
  the test to check that the default is not a path. No existing test was changed.
- **Mutation check:** I undid each part in turn, and in each case exactly one test failed:
  - without `_quiet_transcript_warnings`, the counts-only test fails (warnings on stderr);
  - with `_describe` in place of `_without_content`, the refused-stream test fails (the
    marker is quoted);
  - with the fold moved outside the lock in `observer.py`, the two-runs test fails (both
    processes append);
  - with `exists=True` dropped, the nonexistent-folder test fails.
- Checks:
  ```
  uv run pytest              934 passed, 5 skipped
  uv run ruff check          All checks passed!
  uv run ruff format --check 74 files already formatted
  uv run pyright             0 errors, 0 warnings, 0 informations
  ```

**Two-runner walkthrough:**

*Two `walle observe` runs, A and B, same `--root`, `--principal maya` and `--device mbp`.*
The stream `d-mbp.jsonl` ends at seq 40. T1 is already in it, from yesterday. Both runs will
find completed turns T1 and T2.
1. A and B each build a `StreamWriter`. That validates the names and opens nothing.
2. A and B each read the transcripts. Neither takes a lock: they only read Claude Code's
   files, and Claude Code only appends to them (task 10's walkthrough). Each holds its own
   `ObserveReport`. If T2 completed between A's read and B's, B's report has T2 and A's has
   it as pending.
3. A calls `append_observations`. It opens the stream (`a+b`) and takes `LOCK_EX`.
4. B calls `append_observations`. It opens the stream and blocks on `LOCK_EX`, having read
   nothing of the stream.
5. A folds the stream through its locked handle. It finds T1 and not T2, appends T2 as seq
   41, `fsync`s and closes. The lock goes with the close. A prints `appended: 1`, `already
   logged: 1`.
6. B gets the lock and folds the stream: T1, and T2 at 41. It appends nothing and prints
   `appended: 0`, `already logged: 2`.

If A's report had T2 as pending, A appends nothing new in step 5, and B appends T2 in step 6.
Either way T2 is logged once, and each run's counts are true of what that run did.

If A dies in step 5, its lock drops when the process does. Its appends so far are durable,
since each was `fsync`ed. A half-written line is a torn tail, which the reader skips and the
writer steps over. B folds whatever is complete and appends the rest. A prints nothing,
because the exception stops it before the counts. No count line claims an append that did
not happen.

A reader asking for `LOCK_SH` during step 5 waits until A closes, then sees T2 whole. That
can be the index (`walle rebuild`) or the proposer.

*Same principal, different devices (`--device mbp` and `--device air` on one machine).* Each
run locks and folds its own stream only (task 11's choice, to avoid a lock-order deadlock).
So both append every turn, once per stream. That is two logs of the same transcript, not a
race. It is under *Noticed, not done*.

**Stranger pass:** I re-read the `cli.py` diff and the new tests as a stranger. The diff is
over 30 lines, touches an error path, and starts the log writer.
- **A status message that lied.** The first help text said "two runs at once append each
  turn once". That holds only for the same `--device` (see above). It now says "with the
  same --device".
- **A leak in the error fallback.** `_without_content` split the message on ` — ` to drop
  pydantic's part. If a wrapper had no ` — `, the whole message, pydantic's quoted input
  included, would have printed. Now a wrapper without the separator is named by its type
  only, and a test covers both branches.
- **Lock scope.** The command holds no lock itself. The transcript read happens before
  `append_observations` takes `LOCK_EX`, so the stream's lock is held only for the fold and
  the appends, not for parsing every transcript. That is correct, because nothing appended
  depends on the transcripts staying still once read. A turn that completes after the read
  is pending in this run and appended by the next.
- **Ignored errors.** None are swallowed. Every exception in `_LOG_ERRORS` becomes a
  `ClickException` (exit 1) with a message. Anything else, `KeyboardInterrupt` included,
  propagates. In both cases the appends made so far are durable and nothing was printed
  about them, so nothing claims more than happened.
- **The logging handler.** It is added and removed in a `try`/`finally`, and only around
  the transcript read. A warning from `walle.writer` (a torn tail in our own stream) still
  prints. It names a path and a seq, never content.
- **What still reaches a message:** paths. An `OSError` reading a transcript names the
  file, and the project folder's name is Claude Code's encoding of the `cwd`. That is not
  transcript text, and the person needs it to fix the file. The reader's bad-`seq` message
  quotes the `seq` value (`found 'x'`). That is not covered by `_without_content`, since no
  `ValidationError` is involved. In a log walle wrote, `seq` is always an integer.
- I checked these and they hold:
  - The counts add up: turns seen = appended + already logged + pending. `append_observations`
    sorts every completed turn into exactly one of its two lists.
  - "Skipped lines" also counts a transcript file that vanished between listing and reading
    (`line` 0). Both are input walle did not read, so one count covers both.
  - No path reads `~` or the environment. Click's `exists=True` check is not a race worth
    guarding: a folder removed after it is just read as having no transcripts, as before.

**Review:** pending.

**Choices made inside the task:**
- **Six counts, not five.** The issue lists five. I added "duplicate entries" because the
  issue calls duplicates out (834 in one real folder). Seeing that number is how the person
  can tell resumes were folded and not double-counted. It is still a count.
- **A nonexistent `--projects-dir` is refused.** `observe_projects` treats a missing folder
  as "no transcripts yet", and that is right for the library. At the command line, a typo
  that prints six zeros would look like success.
- **Warnings silenced, not reformatted.** A `--verbose` flag could list skips by file and
  line, but the issue asks for counts only, and the skips are logged for anyone who
  configures logging. See *Noticed, not done*.
- **The test lives in `tests/test_cli_observe.py`,** not in `test_observer.py`. It tests the
  command, and it runs the CLI the way a person does, both in-process and in a subprocess.
- **`--root` defaults to `.`**, like every other command. D40's "only a folder the person
  names" is about what walle reads. `--root` is where it writes.

**What to read first:** `_without_content` and the refused-stream test. They are the one
place where content could still reach a terminal. Then the two-runner walkthrough's
different-devices case, which is the one way to log a turn twice.

**Noticed, not done:**
- **Two device names on one machine log every turn twice,** once per stream. Nothing stops
  `walle observe` being run with `--device mbp` today and `--device laptop` tomorrow. A
  guard would need to fold every stream of the principal, which is the deadlock task 11
  avoided, or record which device observes which projects folder.
- **No way to see which lines were skipped** short of configuring logging. A `--verbose`
  flag that prints file, line and reason (never content) would help with a real folder.
- **The reader's bad-`seq` message quotes the value.** It is harmless for a log walle wrote,
  and a hand-edited log could put text there.
- **A 180,000-character body goes into the log whole.** The proposer and the hot tier read
  observations. I did not check how they handle a body that size: that is outside this
  task's criterion.
- **The first run over a long history holds the stream's lock through one `fsync` per
  observation.** This is from task 11, and it is now reachable from the command line.
- README does not mention `walle observe` yet.

**Cost:** pending.
