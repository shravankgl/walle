# Task 11: Transcript observer, part 2: one observation per completed turn

Issue #9. Branch `task/11`.

**Shipped:**
- `src/walle/observer.py` (new). It finds the turns in part 1's `parentUuid` graph and makes
  one `Observation` for each turn that has finished. It writes nothing: events come back with
  `seq` unset, for the stream writer.
  - `observe(transcripts, *, ts=None)` returns an `ObserveReport`: `turns` (each holding the
    prompt's uuid, the closing entry's uuid and the `Observation`), `pending` (prompts whose
    turn has not finished) and the `transcripts` it read, with their skips and repairs.
  - `observe_projects(projects_dir, *, ts=None)` lists, loads and observes. `projects_dir` is
    required, with no default (issue #9 comment, D40).
- `src/walle/transcript.py` (changed). These changes come from the facts on issue #9:
  - `transcript_files` also lists `<project>/<session>/subagents/*.jsonl`.
  - `load` hangs a subagent file's root (`parentUuid: null`, in a `subagents/` folder, with an
    `agentId`) under the trunk entry whose `toolUseResult.agentId` names that agent. That is
    the result of the `Task` call that started it. If no loaded entry names the agent, it
    stays a root and is reported as an `unanchored-agent` anomaly. An entry is never the
    anchor for its own agent.
  - `api-request` and `api-request-blob` lines are read with `raw` replaced by `{}`, so no
    payload is held anywhere after the read. A broken one is reported without its bytes.
  - A line of any type other than `user` or `assistant` that does not fit the entry shape is
    kept aside in `unthreaded` and is not a skip. A `user` or `assistant` line that does not
    fit is still skipped and reported, and so is a line with a `uuid` and no `type`.
- `tests/test_observer.py` (new): 26 tests, 42 with their parameters.
- `tests/test_transcript.py`: 9 new tests, and one changed (see *Verify*).
- `tests/fixtures/observer/-home-example-garden/` (new): one synthetic session file and one
  subagent file, described under *Verify*.

**Why:**
- **D56: the unit of observation is a completed turn, not a token.** An assistant entry that
  is still streaming (`stop_reason` still `null`) produces nothing, however many arrive. The
  message is read whole once the turn completes.
- **D93: `observed_at` beside `ts`.** `observed_at` is the closing entry's own timestamp.
  `ts` is when the log learns it: now, once per run, or whatever the caller passes.
- **D101: carry `version`, and never key on the session id.** Each observation's
  `source_version` is the Claude Code build that wrote the closing entry. Turns are found
  through the graph, so a session id that changes on every entry splits nothing, and a turn
  whose answer is in a resume is one turn.
- **D80, second half, in part:** a trailing tool call with no result is pending, not corrupt.
- **D40 and the issue's comment:** walle reads only the folder the person names.

**Verify:** PLAN §9: "N completed turns → N observations; token deltas produce nothing".
- `test_n_completed_turns_give_n_observations_and_token_deltas_give_nothing`, run for N in
  0, 1, 3 and 7, with 0, 1 or 5 streamed fragments before each answer, and a last turn still
  streaming. It checks for exactly N observations, exactly one pending turn, and the bodies.
- `test_fragments_alone_never_become_an_observation`: a prompt and three fragments give
  nothing. The closing entry then gives exactly one observation.
- The fixture, all synthetic (a made-up balcony garden, made-up uuids):
  - The trunk `b1…0b.jsonl` has three turns, with a different `sessionId` on each.
    - Turn 1 has a thinking fragment, then `end_turn`.
    - Turn 2 has an `attachment` inside the chain, a `Task` call, a tool result carrying
      `toolUseResult.agentId`, then `end_turn`.
    - Turn 3 has two fragments of one message and no stop reason: token deltas.
  - The file also has lines of the types the issue lists: `permission-mode`, `mode`,
    `ai-title`, `last-prompt` and `atis-latch`, plus an `api-request` (with a uuid) and an
    `api-request-blob` (without one), both carrying the string `CANARY-RAW-REQUEST-PAYLOAD`.
  - `b1…0b/subagents/agent-a7c0ffee00000001.jsonl` is the subagent turn 2 started. It has
    the trunk's session id, `isSidechain` on every entry, its own `agentId`, and
    `parentUuid: null` on its first entry.
  - Result: 2 observations, 1 pending, no skips, no anomalies. The canary is in no
    observation and in no loaded `raw`.
- Around it:
  - **pending:** a call waiting on its result, a prompt with no answer, and `tool_use`,
    `pause_turn` or an unknown stop reason.
  - **completing:** `end_turn`, `stop_sequence`, `max_tokens` and `refusal` complete a turn.
    With no stop reason at all, the person's next prompt completes the turn before it,
    unless that turn's last reply calls a tool.
  - **identity:** a session id that changes on every entry; two turns in one session id; a
    turn split across a resume; a rewound prompt, which is its own turn.
  - **prompts:** a tool result, an `isMeta` line and a sidechain line are not the person
    speaking. Image blocks become `[image]`, and thinking is left out.
  - **fields:** `observed_at`, `ts`, `source_version`, `source`, `actor`, no `seq`, no
    `caused_by`. Observations come in walk order, not file order.
  - **subagents:** discovery finds the file, the root hangs under the tool result, and the
    subagent adds no turn. The observations with and without its file are equal.
  - **explicit input:** neither `observe_projects` nor `transcript_files` has a default for
    `projects_dir`, and neither module mentions `expanduser`, `Path.home`, `os.environ` or
    `getenv`.
- **Test changed:** `test_transcript_files_finds_project_level_jsonl_only` became
  `test_transcript_files_finds_sessions_and_their_subagents_and_nothing_else`. The task
  requires this: it asserted that `subagents/` files are not listed, and the issue says that
  is where they live. It still checks that other nested folders, `meta.json` files and
  directories are not listed.
- I confirmed the tests failed before the code: `tests/test_observer.py` failed on collection
  (no `walle.observer`). On task 10's code, the changed discovery test and the new graft,
  ignored-type and api-request tests failed (7 failures). Two passed, and each had a reason:
  - `…_null_parent_outside_a_subagents_folder_is_not_grafted` guards against grafting too
    much, so it is meant to pass before and after.
  - `…_user_or_assistant_line_that_does_not_fit_is_still_skipped` passed for the wrong
    reason. My bad line used `"timestamp": 3`, which pydantic reads as an epoch time, so I
    changed it to a bad string (see *Noticed, not done*). It now guards the new
    ignored-type branch.

  The two stranger-pass tests each failed before their fix.
- **Mutation check:** I broke each of these rules in turn and confirmed a test fails:
  - any assistant entry completes a turn;
  - the search enters sidechains;
  - repeated blocks are not de-duplicated;
  - a tool call closes on the next prompt;
  - `isMeta` counts as a prompt;
  - `api-request` `raw` is kept;
  - no graft;
  - no subagent glob;
  - an agent anchors inside itself.
- Checks:
  ```
  uv run pytest              384 passed, 3 skipped
  uv run ruff check          All checks passed!
  uv run ruff format --check 46 files already formatted
  uv run pyright             0 errors, 0 warnings, 0 informations
  ```

**Two-runner walkthrough:** This task writes none of our logs, locks or derived files. It
reads two files that Claude Code C appends to: the trunk and the subagent's file. Here is C
against this observer R, during turn 2 of the fixture:
1. C writes the `Task` call (6) to the trunk and starts the subagent's file.
2. R reads the trunk, then the subagent file. Entry 7, the tool result naming the agent, is
   not written yet. So the subagent's root has no anchor: it is walked as a root and
   reported as `unanchored-agent`. Turn 2 has no closing entry, so it is pending. No
   observation is made.
3. C finishes the subagent, then writes 7 and 8 (`end_turn`) to the trunk.
4. R's next run anchors the subagent under 7 and closes turn 2 at 8: one observation.

The two files are read at different moments, so R can see the subagent further along than
the trunk, or behind it. Neither changes an observation. Whether a turn is complete, and its
body, come from the trunk's main thread alone. The subagent's sidechain is never entered. The
worst a race can do is leave a turn pending one run longer.

What step 2 reports is true but noisy: **every run during a synchronous subagent warns
`unanchored-agent`**. I left the warning in, because an agent that never gets anchored is
worth seeing, but see *Noticed, not done*.

Two observer runs at once each return the same observations, apart from `ts`, and neither
writes. When observations are appended, that changes. See *Choices made inside the task*.

**Stranger pass:** I re-read `observer.py` and the `transcript.py` diff as a stranger. They
are about 350 lines and touch IO and error paths. Findings:
- **A skip preview leaked a raw request.** A broken `api-request` line (not JSON, or the
  unterminated last line Claude Code is still writing) was reported with its first 80
  bytes, in `skips` and in a log warning. That is exactly the payload the issue says never
  to copy. Fixed: a line that looks like one (`"type": "api-request…`) is reported as
  "(an api-request line; its bytes are not shown)".
  `test_a_broken_raw_request_line_is_reported_without_its_bytes` failed before the fix.
- **A missing `type` was quietly accepted.** My first change put every line of a type other
  than user or assistant that did not fit into `unthreaded`, and that included a line with
  no `type` at all. An unknown type is not corruption, but a missing one is. Fixed: only a
  string type other than user or assistant is ignored.
  `test_a_line_with_a_uuid_and_no_type_is_still_skipped` failed before the fix.
- **A suspected crash that was not one.** I thought an offset with seconds (`+05:30:15`)
  would pass pydantic and then fail the log's Instant check inside `Observation`, stopping
  the run. I wrote the test and checked: pydantic rejects that offset, so the line is
  skipped at read time. Every timestamp pydantic accepts prints in a form the Instant check
  takes (I checked basic offsets, a space separator, nanoseconds and epoch numbers). I
  reverted the fix and the test rather than ship a fix for a bug that does not exist.
- I checked these and they hold:
  - There is no recursion. The searches are iterative, and the climb in `_path` always
    reaches the prompt, because `_closing` only returns entries below it.
  - Nothing writes.
  - No `except` hides an error. The one new `except` branch logs at debug and keeps the
    line in `unthreaded`.
  - `raw` is emptied before the entry is built, so the payload is never held in a returned
    object.
  - Bad message shapes (`message` not an object, `content` neither a string nor a list,
    blocks that are not objects) are read as empty, not raised.

**Review:** pending.

**Choices made inside the task:**
- **It returns observations and does not append them.** The criterion is about which turns
  become observations, and that is all here. Appending raises a question this task cannot
  settle alone. A second run must not observe a turn twice, and that needs a stable turn
  identity on the event. The `Observation` schema has nowhere to put the prompt's uuid. The
  other options are de-duplicating on the content (`observed_at`, `body`, `source_version`)
  or a schema change (a field plus a `v` bump under D88/D96). Choosing between them is a
  decision, so I left it for Shravan (see *Noticed, not done*), along with the lock that a
  read-check-append needs.
- **A turn starts at the person, not at any user line.** Tool results, `isMeta` lines and
  sidechain prompts are not the person speaking. Counting them would make N turns look like
  3N.
- **A subagent adds no turn of its own.** Its prompt is written by the main agent. Its work
  belongs to the turn that started it, and its result comes back into that turn as a tool
  result. Its text is left out of the parent's body too: a background subagent can still be
  writing after the parent turn ends, and the body should not change between runs.
- **Completion: a final stop reason, or failing that the person's next prompt.** This
  follows the API's semantics, with a fallback in case a build never writes a stop reason.
  That is assumption 1 below. An unknown stop reason counts as not complete: a new one
  delays an observation and never invents one.
- **The first completing entry in walk order closes the turn,** and the body is the path
  from the prompt to it. Branches off that path (the dead-end parallel tool-result siblings
  `dag.md` describes) do not reach the body.
- **The body has one line per block,** with a `user:` or `assistant:` prefix. Joining text
  blocks with nothing between them would run real paragraphs together, because each entry
  holds one whole block. Tool inputs and results are left out. They are where file contents,
  commands and secrets sit, and the proposer can ask for more later.
- **The graft uses `toolUseResult.agentId` only.** claude-code-log says that field exists
  only at trunk level. A nested agent (an agent started by an agent) therefore stays an
  unanchored root. Its entries are sidechain, so it changes no observation.

**Format assumptions still to check against real files:**
1. **`stop_reason`:** a finished reply's last assistant entry carries `end_turn` (or another
   final reason), and fragments carry `null`. If real builds write `null` everywhere, only
   the next-prompt fallback completes turns, and the last turn of every session stays
   pending.
2. **The spawn link:** the trunk tool result for a `Task` or `Agent` call carries
   `toolUseResult.agentId`, matching the subagent file's `agentId`.
3. **`isMeta: true`** marks Claude Code's own user lines (caveats, local command output).
   Slash-command wrappers (`<command-name>…`) carry no such flag and are read as prompts.
4. **One whole block per assistant entry.** A message's entries share `message.id`.
5. **Where `api-request` lines sit**, and whether they carry a `uuid`. The fixture has one of
   each, and both are handled.

**What to read first:** `_closing` in `observer.py`. It decides what "completed" means, and
so what N is. Then format assumption 1, because it is the one most likely to be wrong on a
real file. Then the graft in `transcript.load`, from `anchors` to `parent_of`.

**Noticed, not done:**
- **Appending observations, and doing it only once per turn.** Before the observer can write
  to the log, someone has to decide how a turn is identified across runs: a turn-id field
  on `Observation` (a schema change, so a `v` bump), or content-based de-duplication. That
  needs a decision. It also needs a lock held across read-check-append, so two observers
  cannot both append the same turn. That will need a two-runner walkthrough of its own.
- **The `unanchored-agent` warning fires on every run while a synchronous subagent is still
  running,** because its tool result is not written yet. Reading the sidecar
  `agent-<id>.meta.json` (`toolUseId`) would anchor it at the `Task` call instead. That
  would also anchor nested agents and spawns that were interrupted.
- **Interrupted turns.** `[Request interrupted by user]` is read as a prompt. The turn it
  cuts short closes on the fallback if its last reply had no tool call, and otherwise stays
  pending for good.
- **`timestamp` as a number** is accepted by task 10's `_EntryFields` as a Unix epoch time
  (pydantic lax mode). A strict `AwareDatetime` would reject it. That is task 10's
  validation, so I left it.
- **`logicalParentUuid` across `/compact`,** and claude-code-log's linearization ladder,
  still from task 10's list.
- **Tool-call / tool-result pair repair** (D80's second half). This task only treats a
  trailing call as pending. It synthesizes no placeholders.
- **No fixture file says it is synthetic**, because JSONL cannot hold a comment. The test
  module's docstring and this log say it. The generator script is not committed.

**Cost:** pending.
