# Task 11: Transcript observer, part 2: one observation per completed turn

Issue #9. Branch `task/11`, PR #34. Two commits: the first build (`adc8012`), and a second
that fixes the first review's findings. The first review found 2 high, 1 medium and 1 low.
*Review* lists each finding and what changed for it.

**Shipped:**
- `src/walle/observer.py` (new). It finds the turns in part 1's `parentUuid` graph, makes one
  `Observation` per completed turn, and appends each one the log does not already hold.
  - `observe(transcripts, *, ts=None)` returns an `ObserveReport`: `turns` (each with the
    prompt's uuid, the closing entry's uuid and the `Observation`), `pending` (prompts whose
    turn has not finished) and the `transcripts` it read, with their skips and repairs. It
    writes nothing.
  - `append_observations(report, writer)` takes the stream's lock, folds the stream into the
    set of transcript observations it already holds (D92), and appends each turn not in it.
    It returns an `AppendReport`: `appended` (with their `seq`) and `already_logged`. With
    nothing completed, it does not touch the stream, or create it.
  - `observe_projects(projects_dir, *, ts=None)` lists, loads and observes. `projects_dir` is
    required, with no default (issue #9 comment, D40).
- `src/walle/writer.py` (changed). `StreamWriter.locked()` holds the stream's `LOCK_EX`
  across a read and any number of appends, and yields a `LockedStream` with `read()` and
  `append()`. `append()` is now one locked session with one append. Its code is unchanged,
  only moved into `LockedStream.append`, and every existing writer test still passes.
  `read()` parses through the locked handle with `reader.parse_stream`. Opening the file
  again for `reader.read_file` would ask for `LOCK_SH` on a second open file and wait for
  this lock forever.
- `src/walle/transcript.py` (changed):
  - `transcript_files` also lists `<project>/<session>/subagents/*.jsonl`.
  - `load` hangs a subagent file's root under the **trunk** entry whose
    `toolUseResult.agentId` names that agent. Sidechain entries are never anchors. If nothing
    names the agent, the root is reported as an `unanchored-agent` anomaly.
  - `api-request` and `api-request-blob` lines are read with `raw` replaced by `{}`.
  - **`LineSkip` carries no transcript content:** `path`, `line`, `offset`, `length` and
    `reason` only. The 80-byte `preview` is gone, and so is the attempt to spot payload
    lines by pattern.
  - A line of any type other than `user` or `assistant` that does not fit the entry shape is
    ignored, kept aside in `unthreaded`. A `user` or `assistant` line that does not fit is
    still skipped, and so is a line with a `uuid` and no `type`.
- `tests/test_observer.py` (new): 35 tests, 52 with their parameters.
- `tests/test_transcript.py`: 11 new tests, and one changed (see *Verify*).
- `tests/test_writer.py`: 2 new tests for the locked session.
- `tests/fixtures/observer/-home-example-garden/` (new): one synthetic session file and one
  subagent file, described under *Verify*.

**Why:**
- **D56: the unit of observation is a completed turn, not a token.** An assistant entry that
  is still streaming (`stop_reason` still `null`) produces nothing, however many arrive.
- **D92: what is already logged is a fold over the log.** It is rebuilt from the stream on
  every run, with no cursor file and no second store. D92's example cursor is
  `max(ts) over observations`, and it does not fit transcripts. `ts` is when we read a turn,
  not when it happened. And turns from several files and resumes complete out of
  transcript order, so a high-water mark would drop a late completion. So the fold is the
  set of turns already logged, not a mark.
- **D93: `observed_at` beside `ts`.** `observed_at` is the closing entry's own timestamp.
  `ts` is when the log learns it.
- **D101: carry `version`, and never key on the session id.** Each observation's
  `source_version` is the build that wrote the closing entry. Turns are found through the
  graph, and "already logged" matches `(source, observed_at, body, source_version)`: no
  session id, and no `ts`.
- **D80, second half, in part:** a trailing tool call with no result is pending, not corrupt.
  A call cut short because the person spoke again is not trailing.
- **D40 and the issue's comment:** walle reads only the folder the person names.

**Verify:** PLAN §9: "N completed turns → N observations; token deltas produce nothing".
- `test_n_completed_turns_give_n_observations_and_token_deltas_give_nothing`, for N in 0, 1,
  3 and 7, with 0, 1 or 5 streamed fragments before each answer, and a last turn still
  streaming.
- `test_fragments_alone_never_become_an_observation`.
- **Appended once:** `test_completed_turns_are_appended_with_seqs_and_a_rerun_appends_nothing`.
  The fixture appends seqs 1 and 2. A second run, with a new `ts`, appends nothing and
  leaves the file byte-identical.
- **Two processes:** `test_two_processes_at_once_append_each_turn_once` starts two
  processes on the fixture and one stream. Each slows its fold by one second. Result: one
  process appends 2 and the other 0, seqs are `[1, 2]`, and there are 2 distinct bodies.
  With the fold moved outside the lock, this test fails: both processes append both turns.
- Around appending:
  - a turn that completes between runs is appended by the later run;
  - rewriting every session id between runs appends nothing;
  - an observation from another source does not count as logged;
  - with nothing completed, no stream file is created;
  - `locked()` reads and appends under one lock, and another process's append waits for it.
- **A turn runs to the next real prompt** (review, medium):
  - **Shape 1:** thinking and text in separate entries of one message, both `end_turn`. The
    text is kept, with and without a next prompt.
  - **Shape 2:** a `max_tokens` reply, then an `isMeta` line or an `attachment`, then more
    reply. The turn closes at the last reply, and the body has both.
  - A final reply followed by an `isMeta` line, with nothing after it yet, is pending. More
    is coming.
- **No payload bytes** (review, high 2):
  - `test_a_truncated_blob_line_with_its_type_last_leaves_no_payload_byte_anywhere`. The
    line is `{"sessionId":"s","blob":"CANARY-…-and-more`, cut off before `"type"`. It is
    tested mid-file and as the unterminated last line, in a `subagents/` file. The test
    walks every string reachable from the `TranscriptFile` and the `Transcripts`, including
    every `raw`, plus the captured log at DEBUG. No payload byte is in any of them.
  - `test_a_skip_carries_where_the_line_is_and_none_of_its_bytes`. `LineSkip` has exactly
    `path, line, offset, length, reason`, and `offset`/`length` slice the file back to the
    line.
- **Sidechain anchor** (review, low): a sidechain entry naming agent `ag2` earlier than the
  trunk's spawn result does not win. `ag2` hangs under the trunk entry.
- The fixture, all synthetic (a made-up balcony garden, made-up uuids), is unchanged:
  - three turns, with a different `sessionId` on each;
  - a subagent file the second turn started;
  - each ignored entry type the issue lists;
  - `api-request` lines carrying a canary string.

  It gives 2 observations and 1 pending turn, with no skips and no anomalies.
- **Tests changed**, because the task or the review requires it:
  - The discovery test asserted that `subagents/` files are not listed. It is now
    `test_transcript_files_finds_sessions_and_their_subagents_and_nothing_else` (first
    commit).
  - `test_a_next_prompt_does_not_close_a_turn_whose_last_reply_calls_a_tool` is now
    `test_the_next_prompt_ends_a_turn_even_when_its_last_reply_called_a_tool`. The review
    instruction is that a turn runs until the person's next real prompt.
  - `test_a_broken_raw_request_line_is_reported_without_its_bytes` checked the old
    redacted `preview`, which no longer exists. The two tests above replace it.
- **Tests first:** every new test failed before its fix, each for the expected reason:
  - the append tests on a missing `append_observations`;
  - the locked-session tests on a missing `locked`;
  - the skip tests on `preview` and on the leak;
  - the anchor test on the old anchor;
  - the five turn-shape tests on the old `_closing`.
- **Mutation check:** I undid each rule in turn and confirmed a test fails:
  - the fold moved outside the lock;
  - no de-duplication;
  - the first final reply closes the turn;
  - a meta line after a final reply does not make it pending;
  - a tool call blocks the next prompt from closing;
  - the stream is locked even with nothing to append;
  - sidechain entries can anchor.

  Only one mutation was not caught: dropping the `source == "transcript"` filter in the
  fold. That is because the match key already contains `source`, so the filter is
  redundant. The from-another-source test passes either way.
- Checks:
  ```
  uv run pytest              398 passed, 3 skipped
  uv run ruff check          All checks passed!
  uv run ruff format --check 47 files already formatted
  uv run pyright             0 errors, 0 warnings, 0 informations
  ```

**Two-runner walkthrough:**

*Two observer runs, A and B, appending to the same stream `d-mbp.jsonl`.* Both have read the
same transcripts and both found completed turns T1 and T2. The stream ends at seq 40, and T1
is already in it from yesterday.
1. A opens the stream (`a+b`) and takes `LOCK_EX`.
2. B opens the stream and asks for `LOCK_EX`. It blocks, having read nothing of the stream.
   Its view of the transcripts may be newer or older than A's. That is fine: whatever it
   observed is checked against the stream once it holds the lock.
3. A reads the whole stream through its locked handle. The fold finds T1 and not T2.
4. A skips T1 (`already_logged`) and appends T2 as seq 41. It `fsync`s, then closes, and
   the lock goes with the close.
5. B gets the lock and reads the stream: T1, and T2 at 41. It appends nothing, and reports
   both as `already_logged`.

If B's transcripts showed a turn T3 that A's did not (it completed between the two reads), B
appends T3 as seq 42 in step 5. Either order gives each turn once. If A dies in step 4
before T2's `fsync`, its lock drops. B then finds no T2 (or a torn fragment, which the reader
skips and the writer steps over) and appends T2 itself.

Without the lock around step 3, A and B would both fold the stream at seq 40, both find T2
missing, and both append it: two refs for one turn. The two-process test shows this; it
fails when the fold is moved out of the lock.

A reader (the index, `walle rebuild`) asking for `LOCK_SH` during steps 3–4 waits until A
closes, then sees T2 whole.

*Claude Code C appending to a transcript, and this observer R reading it.* Nothing changes
from the first commit. R never writes a transcript, so no lock is needed. A line C has not
finished is pending. A turn whose closing entry is not written yet is pending, and it is
appended by a later run. During a synchronous subagent, R may read the subagent's file before
the trunk's spawn result exists. Then the root is reported `unanchored-agent` for that run.
The turn is unaffected, because sidechains are never part of a turn's body or end.

**Stranger pass:**

*First commit:*
- **A preview leak.** A broken `api-request` line's first 80 bytes went into the skip.
- **A missing `type` accepted.** A line with a `uuid` and no `type` was quietly ignored.
- **An offset with seconds.** I suspected it would stop the run. It did not (pydantic
  rejects such offsets), so I reverted that fix.

*Second commit*, a re-read of `observer.py`, the `writer.py` diff and the `transcript.py`
diff. They touch the log, a lock and an error path:
- **Lock scope.** The fold and every append are inside the one `with writer.locked()` block.
  Nothing inside it opens the stream again: `LockedStream.read` uses the locked handle,
  because `read_file` would deadlock. An exception anywhere in the block closes the file,
  and the lock goes with it.
- **No stream created for nothing.** Opening `a+b` creates the file. With no completed turns,
  `append_observations` returns before locking, so a run over an empty folder leaves no
  empty stream behind. A test covers it.
- **A partial failure.** If an append raises (a full disk, say), the appends before it are
  durable and the exception propagates, so no `AppendReport` claims anything. The next run's
  fold finds them and appends only the rest. Ordering inside `LockedStream.append` is
  unchanged from `append`: the directory `fsync` comes before the write, and the record is
  encoded before anything is written.
- **Status messages.** `already_logged` also covers a turn identical in content to one
  appended earlier in the same call. I reworded its docstring so it does not claim the
  stream held it before the run.
- **Reasons in skips.** Nothing in a reason comes from the line itself. The reasons are the
  JSON decoder's `msg` (it never includes the document), "not UTF-8", the JSON type name,
  and pydantic's field names and messages, which carry no input values. The skip is logged
  with line number, byte length and reason only. The ignored-type branch logs the `type`
  string at DEBUG, and nothing else.
- **Lock held through many `fsync`s.** The first run over a long history appends one event
  per turn, each `fsync`ed, all under one lock. Readers of that stream wait meanwhile. That
  is correct, but slow for thousands of turns; see *Noticed, not done*.
- I checked these and they hold:
  - `append_observations` appends in walk order;
  - `observed_at` is stable across runs (the same `isoformat` of the same timestamp);
  - no new path reads `~` or the environment;
  - the `subagents/` graft still cannot form a cycle, because only a root (`parentUuid`
    null) is re-parented, and only onto a trunk entry, which is not inside its own file.

**Review:**
- **Round 1:** Gemini 4 Argon, direct diff review against `8b2a132` (`ocr delegate` was not
  installable), on `adc8012`. Result: `agent-review` failure.
  - **High 1:** observations were never appended, and nothing de-duplicated on a re-run.
    - Fixed. `append_observations` appends through `StreamWriter`, under the writer's lock,
      after a fold over the stream (D92). It matches on content and never on the session id.
    - Added: the two-process test, the re-run and session-id tests, and the walkthrough
      above.
    - I did not stop for a schema decision. The content match needs no new field. Its one
      limit (a turn that grows after being observed) is under *Noticed, not done*.
  - **High 2:** a truncated `api-request`/`api-request-blob` line, with `"type"` after the
    payload, leaked bytes into `LineSkip.preview`.
    - Fixed by removing previews from transcript skips entirely, rather than detecting
      payload lines better. `LineSkip` is now `path, line, offset, length, reason`.
    - The test covers a truncated blob line with `type` last, mid-file and at the tail.
  - **Medium:** `_closing` stopped at the first final reply and dropped the rest of the turn.
    - Fixed. A turn runs to the person's next real prompt.
    - With no next prompt, the turn closes at the last user or assistant line in the turn,
      if that line is a final reply. It is pending if a non-person user line follows that
      reply.
    - Tested for both shapes in the finding.
  - **Low:** the subagent anchor loop did not exclude sidechain entries, as its comment said.
    - Fixed. Only trunk entries anchor, and a test covers it.
- **Round 2:** pending.

**Choices made inside the task:**
- **Matching on content, not a turn id.** The `Observation` schema has no field for the
  prompt's uuid, and adding one is a `v` bump with an upcaster (D88, D96). The content key
  `(source, observed_at, body, source_version)` makes a re-run over unchanged transcripts
  append nothing, which is what the review asked for, with no schema change. Its limit is
  under *Noticed, not done*.
- **The fold covers this device's stream only,** the one under the lock. The transcripts are
  on this machine and its stream is this device's (D57). Folding other devices' streams as
  well would mean taking their `LOCK_SH` while holding this `LOCK_EX`. Two observers on two
  device ids would then deadlock, each holding its own exclusive lock and waiting for the
  other's shared one.
- **Rule 2 waits on a non-person user line.** After a final reply, a tool result or an
  `isMeta` line means the harness is about to continue (a Stop hook, for one). Waiting makes
  it less likely that a turn is observed and then grows. Lines of other types after the
  reply (`attachment`, `system`) do not hold the turn open.
- **A cut-off tool call closes when the person speaks again.** It is not pending: D80's
  pending is for trailing calls, and this one is not trailing. The first commit kept such
  turns pending forever. That is now gone from *Noticed, not done*.
- **The rest is as in the first commit:**
  - a turn starts at the person, not at any user line;
  - a subagent adds no turn, and its text stays out of the parent's body;
  - the body has one line per block, without thinking, tool inputs or tool results;
  - an unknown stop reason means not complete;
  - the graft uses `toolUseResult.agentId`.

**Format assumptions still to check against real files:**
1. **`stop_reason`:** a finished reply's last assistant entry carries `end_turn` (or another
   final reason), and fragments carry `null`. If real builds write `null` everywhere, only
   the next prompt completes turns, and the last turn of every session stays pending.
2. **The spawn link:** the trunk tool result for a `Task` or `Agent` call carries
   `toolUseResult.agentId`, matching the subagent file's `agentId`.
3. **`isMeta: true`** marks Claude Code's own user lines (caveats, hook feedback, local
   command output). Slash-command wrappers carry no such flag and are read as prompts.
4. **One whole block per assistant entry,** with a message's entries sharing `message.id`.
5. **Where `api-request` lines sit**, and whether they carry a `uuid`. Both cases are handled.

**What to read first:** `append_observations` and the walkthrough above, because they are
where a bug would put two refs on one turn. Then `_closing`'s two rules, because they decide
what N is. Then format assumption 1.

**Noticed, not done:**
- **A turn that grows after it was observed gets a second, longer observation.** The content
  match cannot tell "more of T" from "a new turn". A turn id on the event would, and that is
  a schema change (D88, D96) for Shravan to decide. Rule 2 narrows the window, but a split
  message whose later entries arrive after a run (shape 1, mid-write) can still do it.
- **Changing how `_body` renders re-observes all of history,** because the body is part of
  the match key. Any later change to the body format needs a migration plan, or a turn id.
- **No CLI yet.** Nothing calls `append_observations` from `walle`. A `walle observe
  --projects DIR --principal P --device D` command, with the folder required, is the
  obvious next step and not part of this task's criterion.
- **One `fsync` per observation under one lock.** A first run over a long history holds the
  stream's lock for all of it. Batching the appends into one write would need a writer API
  for several events at once.
- **The fold reads the whole stream every run.** It is fine at Phase 0 sizes. Later it could
  come from the SQLite index, which is a derived view of the same fold.
- **The `unanchored-agent` warning fires on every run while a synchronous subagent is still
  running.** Reading the sidecar `agent-<id>.meta.json` (`toolUseId`) would anchor it at the
  call instead, and would also anchor nested agents and spawns that were interrupted.
- **Interrupted turns.** `[Request interrupted by user]` is still read as a prompt.
- **`timestamp` as a number** is accepted by task 10's `_EntryFields` as a Unix epoch time.
- **`logicalParentUuid` across `/compact`,** and claude-code-log's linearization ladder,
  still from task 10's list.
- **Tool-call / tool-result pair repair** (D80's second half). No placeholders are
  synthesized.
- **No fixture file says it is synthetic**, because JSONL cannot hold a comment. The test
  module's docstring and this log say it.

**Cost:** pending.
