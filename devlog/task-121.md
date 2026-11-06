# Task 121: Audit — the Builder must run steps through the whole chain

Issue #121, findings 2.1 (high) and 2.2 (medium) of the cross-module audit. Branch
`task/audit-builder` (the owner's empty claim, reused as the issue comment says). Author:
walle-author-fable, in Hyperagent thread `cmw40wiot01iu07adsvodv4nr`.

**Shipped:**

- `src/walle/builder.py` — the fix. `run_step` now takes a `device` and runs a step through
  the whole chain or not at all: the register of active automations, then a mission folded
  from the log, then `missions.admit`, then the executor; and it records the mission and each
  step's `running` / `completed` / `failed` in the log, under a `missions.lock`. `_inside`
  asks `floors.hit` about every declared write, so a floored path is refused wherever a
  workflow is read (a model's reply, a staged body, a kept body). `_kept` requires the
  proposal a keep cites to be live; `workflow` asks `approved(records, source)` instead of
  only checking that the source ref is live. Module docstring: new rule 4, the three switches,
  and a two-runner walkthrough.
- `src/walle/automations.py` — `add(..., workflow=<kept ref>)` puts the kept workflow in the
  add's `caused_by` (beside the retire, when swapping), and `Entry.caused_by` carries it out
  of the fold. This is the binding `run_step` looks for. Nothing about the cap, the verbs or
  the refusals changed.
- `src/walle/paths.py` — `missions_lock_path`: `principals/p-<name>/missions.lock`, per
  principal like the other three locks.
- `tests/test_builder_chain.py` — new, 34 tests: both reproductions from the issue as
  written, one test per behaviour the fix adds, the dormant rule at this seam, and the seam
  tests that drive `builder`, `floors`, `missions`, `manifest`, `automations` and `executor`
  (the fake client) together.
- `tests/test_builder.py` — the task required edits (see *Choices*): `run_step` call sites
  gain `D`; `ALL_ON` gains `WALLE_MISSIONS=on`; the five tests that expect a step to run enrol
  the workflow in the register first (`_enrolled`); the executor-at-default test turns
  missions on so the refusal it pins is still the executor's; and the "no way to start a
  process" AST test now allows `executor.enabled` and `executor.Refusal` beside `Job` and
  `run`, with a comment saying why.

**Why:**

- D72 (the floor is under the manifest, in any mode) and D32 rule 2 (the manifest is checked
  before the touch): `missions.admit` already does both, but `run_step` never called it, so a
  kept workflow could write `kernel/init.py` in a container with the whole workspace mounted,
  and could run step 2 with step 1 never run. The issue asked that `admit` be the one door
  rather than its checks be copied, so #118's path fix (absolute and `..` writes in `admit`)
  applies to the Builder the day it lands, with no change here. The Builder calls `admit`
  with the step's own `reads` and `writes` as the touch; the manifest check is then trivially
  satisfied (a node covers its own declaration), and what bites is the floor, the switch, a
  pause, a breach and the order.
- D67 (missions are DAGs with manifests; a node is ready when every predecessor completed):
  step order can only hold if the log knows what ran. `run_step` is the only runner in this
  build, so it is the one that appends the mission and the step statuses — before the
  container starts (`running`) and after (`completed`/`failed`). D91's recovery reads
  `running` nodes as outcomes unknown; a step whose runner died mid-container is left exactly
  that way, and a second runner is refused ("already running") rather than racing it.
- D6 / hard constraint 9 (the WIP cap): a kept workflow that runs is an automation. The audit
  showed one that was never added under the cap, or was retired, still ran. Now an active
  register entry whose add names the kept workflow is required; a retired one refuses by
  name (D114).
- D117 (install is not enable; the grant is bound to what it was given for): a workflow is
  now also refused when the proposal its keep cites was tombstoned (2.2.1) and when its
  source is anything `approved` would refuse — an observation, an assertion nobody kept from
  a proposal, a superseded one (2.2.2). The question `build` asked is asked again at every
  run.
- D68 / D99: still one door to the executor, `executor.run`, with the Job unchanged. The
  mount is unchanged (see *Noticed, not done*).

**Verify:** "each reproduction becomes a failing test first, then passes as a refusal; one
test per seam drives the real modules together; nothing is written to the log on a refusal;
dormant by default." All 34 tests in `tests/test_builder_chain.py` were run against main at
d6a6c23 before the fix: all 34 failed, the two reproductions by accepting the floored draft
(`Draft(...)` where a `Refusal` was asserted) and by not raising. After the fix, with main at
dfbeb90 merged in:

```
$ uv run pytest
4050 passed, 35 skipped in 147.43s (0:02:27)
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
198 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

The 35 skips are the container escape tests and the Linux real-user tests, which need a
daemon or root; none is new. The tests that carry the criterion:

- `test_the_reproduction_from_the_audit_is_refused`, `test_the_reproduction_for_2_2_is_refused`
  — the issue's code, verbatim shape, now refused.
- `test_build_refuses_a_step_that_writes_a_floor_path` (7 paths, including one above a floor
  and one in the other case) and `test_a_kept_workflow_edited_to_write_a_floor_path_is_refused_at_every_later_stage`
  — the floor at every stage, nothing written, the client never asked.
- `test_run_step_asks_missions_admit_about_the_step_it_is_about_to_run` — a spy on
  `missions.admit` sees exactly one call with the node, `Touch(reads, writes)` of the step and
  the caller's environment, on a mission `caused_by` the kept workflow; what `admit` refuses,
  `run_step` returns in `admit`'s words.
- `test_step_two_cannot_run_before_step_one` — refused "not ready: waiting on write-plan",
  nothing appended; step 1 runs and the log holds the mission and `running`/`completed`; a
  finished step is "already completed"; step 2 runs; the mission folds to `done`.
- `test_a_workflow_not_in_the_register_does_not_run`, `test_a_retired_automation_does_not_run`
  — including an active automation bound to a *different* workflow not counting.
- `test_with_missions_off_a_kept_step_does_not_run_and_nothing_is_written` and
  `test_with_the_executor_off_nothing_is_written_either` — the dormant rule at this seam, and
  `WALLE_MISSIONS=1` is not `on`.
- `test_the_builder_and_the_mission_gate_refuse_the_same_floored_workflow[on|off]` — the seam
  itself: the same `WorkflowStep` refused by `missions.admit` in both modes and by the
  Builder's reader.
- `test_the_builder_has_exactly_one_door_to_the_executor_and_it_is_behind_admit` — source
  level: `missions.admit` and `executor.run` are each called once in the module, both in
  `run_step`, `admit` first.

**Two-runner walkthrough:** writers A (laptop, `d-mbp`) and B (phone, `d-phone`) each ask for
step 1 of the same kept workflow at the same moment.

1. A takes `missions.lock` (exclusive). B blocks here, having read nothing.
2. A calls `index.update` (the index lock, released inside) and folds: the workflow is live
   and kept from a live Builder proposal built from a live approval; the register has an
   active add naming it; no Builder mission `caused_by` it is open. `admit` says go on a fresh
   fold of the plan. The node is `pending`, not `running`. The executor is on.
3. A appends the mission (`d-mbp:N`), then `running` for step 1 (`d-mbp:N+1`), each under the
   stream's own exclusive lock, held until fsync. A releases `missions.lock`. The container
   starts.
4. B wakes and only now reads. The fold shows mission `d-mbp:N`, open, step 1 `running`.
   `admit` admits a running node (it is also the per-tool-call check), so `run_step` itself
   refuses: "already running". B appends nothing and starts nothing.
5. A's container exits 0. A appends `completed` with note `exit 0` — without `missions.lock`,
   because a status on a running node is recorded whatever else is happening (`missions` rule
   5) and nothing decides on it until the next hold of the lock.
6. Had A died between 3 and 5, step 1 stays `running`: step 2 is "not ready", step 1 is
   "already running", and no new mission opens because this one is still `open`. That is
   D91's recovery case, surfaced rather than papered over (see *Noticed, not done*).

Lock order: `missions.lock` → `index.lock` (inside `index.update`, released) → one stream's
lock per append. Nothing takes `missions.lock` while holding another; no cycle. The lock is
held for one decision, never across the container. Two devices that do not share a filesystem
can each open a mission for the same workflow; when the logs meet, `_mission_state` picks the
latest open one in ref order and the other stays open and unfinished — the same limit
`automations` documents for the cap.

**Stranger pass:** the diff is ~340 lines and touches the log, a lock and an error path, so a
second read was due. Found and changed:

- `except BaseException` after `executor.run` wrote `failed` with "before it ran". On a
  `KeyboardInterrupt` mid-run that is a lie — the container may be alive — and `failed` would
  hide the D91 case. Now `except Exception` only (the executor's `ValueError`s and an
  `OSError` from a client that cannot start all happen before a container exists), the note
  says `<Class> from the executor`, and an interrupt leaves the node `running`.
- The executor's refusal reason went into the step's note whole; for `CLIENT_FAILED` that is
  the daemon's stderr. Capped at `NOTE_CHARS` (200). The command's own stdout/stderr never
  reach the log; `test_a_failed_step_is_recorded_and_holds_its_successor` asserts `boom` is
  absent.
- The floor refusal read "the path under a floor path". Now "the path is under …".
- `workflow`'s new message said the source "no longer stands", wrong for a source that never
  was an approval. Now "does not stand (<why>); the workflow lapsed with it (D117)".

Looked for and not found: an ignored error (every `writer.append` result is used or the
exception propagates); a status message that lies (`executor.Refusal("container executor is
off")` is returned only after `executor.enabled` is false, and the string is pinned by the
pre-existing test); a leak (the lock file handle closes with the `with`; the container is the
executor's to remove, unchanged). One deliberate duplication remains: `run_step` asks
`executor.enabled` before appending, so a run with the executor off leaves no mission and no
step in the log; the executor still checks its own switch inside `run`.

**Review:** per the agent brief, review is the reviewer agent's on the PR, on a different
vendor's model; this entry records the author's own stranger pass only. No `scripts/review.sh`
run (R6 replaces it for agent work).

**Choices made inside the task:**

- *`run_step` writes to the log.* The alternative — fold a fresh mission every call and never
  record — makes step order a dead letter: step 2 could never run, because nothing would ever
  say step 1 completed. `missions` reserved exactly this for "a runner"; `run_step` is the
  only runner in the build, so it records. The cost is a required `device` argument, which is
  why every existing `run_step` call site changed.
- *Three switches.* `admit` checks `WALLE_MISSIONS`, so a step now needs `WALLE_BUILDER`,
  `WALLE_MISSIONS` and `WALLE_CONTAINER_EXECUTOR` all `on`. Not a fourth: the register's
  switch governs its writes, and reading it is not acting (its rule 7).
- *The binding is the add's `caused_by`.* An `Automation` has a name and a title, not a target
  workflow. `caused_by` already names the retire an add swapped with, so naming the kept
  workflow there is the same idiom, costs no schema change and no new verb, and the fold
  carries it out as `Entry.caused_by`. A looser binding — by name, or "any active automation
  will do" — would let one slot under the cap run every workflow.
- *A done or failed mission is spent.* The next `run_step` of step 1 opens a new mission; a
  retry of a failed Sunday is a new Sunday. The plan is never edited (D67), so there is no
  other way to run it again. A paused mission holds everything until a person resumes it.
- *A mission in the log that names the workflow but is not its plan is refused* rather than
  trusted or replaced — the runner's manifest would otherwise come from whatever was appended.
- *The floor refusal quotes no path.* `_inside` has never quoted the model's words; the floor
  message names the kind of floor and how it was hit, not the path.
- *Tests edited:* the AST test in `test_builder.py` pinned `executor` calls to `Job` and
  `run`; it now also allows `enabled` and `Refusal`, for the pre-check above. The brief asks
  that edits to a passing test be named: this is the one, and the comment in the test says
  why.
- *No change to `executor.py`.* Finding 2.1 notes the whole workspace is mounted read-write
  even for a step with `writes=()`. Mounting only declared paths changes the container's
  shape (D68 names "the workspace mounted" as the boundary) and needs the escape tests run on
  the owner's Mac, so it is not in this diff; it is listed below.

**What to read first:** `run_step` in `src/walle/builder.py` — the order of refusals, the
lock scope, and the three appends — then rule 4 and the walkthrough in the module docstring,
then `test_step_two_cannot_run_before_step_one` and `test_a_step_already_running_is_not_started_twice`.

**Noticed, not done:**

- The whole workspace is bind-mounted read-write whatever a step declares (issue 2.1, third
  bullet). Narrowing the mount to declared paths is an `executor.py` change under D68 and
  needs a daemon for the escape tests; a decision on how reads vs writes map to mount
  options (read-only mounts per `reads` path?) should come first.
- D91 recovery: a step left `running` by a runner that died holds its workflow forever. The
  log says so truthfully; nothing yet appends the `failed` that would release it.
  `missions.abort_step` is the shape of that event.
- `Workflow.mission` could name the automation entry in `caused_by` beside the kept workflow,
  for traceability from mission to slot. Left as is because `test_a_workflow_becomes_a_mission_whose_manifest_is_the_steps`
  pins `caused_by == [kept]` and nothing reads it yet.
- The manifest check in `admit` is trivially satisfied here (the touch is the declaration).
  It will bite when a runner checks actual tool calls against the node — the per-tool-call
  path `missions` rule 7 describes — which no runner does yet.

**Cost:** not visible to the author from inside the run; read it from the Hyperagent thread
`cmw40wiot01iu07adsvodv4nr` (author) and the reviewer's run.

---

## Round 2 — review of a5c2de8 (Gemini 4 Argon): 1 high, 2 medium, 1 low

Before pushing, the branch was checked for commits not mine: it held `f007016` (mine) and
`a5c2de8` (GitHub's merge of main, requested by me via `update-branch`). Nothing else.

**Failing tests first.** Fourteen new tests at the end of `tests/test_builder_chain.py`, all
run against the branch head before the fix: 13 failed, one (`_plain_workspace_still_runs`)
passed as the control it is.

### 1 [high] `automations.add(workflow=)` could poison the log

**Found.** `add` put whatever ref it was handed into `caused_by`. `index._dangling` refuses a
log with a cause no stream holds, and the log is append-only, so a typo (`d-mbp:999`), a ref
from another principal, or the add's own ref on an empty stream broke `index.update` and the
rebuild for good. Reproduced; the index raised `DanglingRefError` after one call.

**Fixed, twice.**
- `automations.add` checks, under `automations.lock` and from the same read the register is
  folded from, that `workflow` is a ref in this principal's log, and raises `RefusedError`
  before anything is written. The event's own ref is never in the log at decision time, so a
  self-reference is refused the same way.
- **Centralised at the append** (`src/walle/writer.py`): `LockedStream.append` — the one
  place a stream grows; `StreamWriter.append` delegates to it — now runs `_check_causes`
  before a byte is written. Every ref in `caused_by` is looked for: on this stream through
  the locked handle, on any other stream of the principal by reading its file. Missing refs
  raise `DanglingCauseError` (a `ValueError`) naming them. A cause on this stream with the
  seq about to be assigned is the event naming itself and is missing like any other.

  *Why the other streams are read without their lock:* taking `LOCK_SH` on `d-phone` while
  holding `LOCK_EX` on `d-mbp` deadlocks against a writer doing the same the other way. The
  lockless read is sound because the reader's rules (D80) already make it so: a line is a
  record only once its newline is on disk; the only line a concurrent writer can be mid-way
  through is the last, which parses as torn and is skipped. Append-only means what was there
  before the read stays; what was not there yet makes a cause missing, which refuses — fail
  closed. This is written into the writer's module docstring.

**Survey of every other writer that sets `caused_by`** (asked for by the owner):

| writer | cause comes from | dangling possible before this round? |
|---|---|---|
| `staging.keep/edit` (`_assertion`) | `item.ref`, read from the log under `staging.lock` | no |
| `builder.stage` | `draft.source`, which `approved(records, …)` found in the log | no — but only if the caller built the draft from the same principal's records; now the writer refuses otherwise |
| `builder.run_step` (mission, steps) | `flow.ref` from the log; the mission ref just written on this stream | no |
| `curator` (merge/fusion) | `pair.sides`, `fusion.sources` from the fold | no |
| `proposer` | `candidate.observation` from the records it read | no |
| `context._write` (edit) | `old_ref` from the ledger; `new_ref` just written on this stream | no |
| `missions.abort_step` | `spent_from` from the fold | no |
| `automations.add` with `retire=` | the retire just written on this stream | no |
| `automations.add` with `workflow=` | **a bare string from the caller** | **yes — the finding** |

So the hole was the one the reviewer found; everything else derives its cause from a record
it has in hand. The writer's check is still worth having: it turns "every caller got it
right" into "no caller can get it wrong", which is what an append-only log needs.
`Supersede.target`, `Step.mission` and `Automation.target` are not `caused_by` and are not
checked by `index._dangling`; a dangling one there is reported by its fold, not a broken
index, so they are left as they are (noted below).

**Tests edited because the task required it:** three pre-existing tests planted a dangling
or cyclic cause *through `StreamWriter`* to exercise the index's and `why`'s defence
(`test_index.py::test_a_dangling_caused_by_fails_the_build`,
`test_walle_rebuild_fails_loudly_on_a_dangling_ref`,
`test_why.py::test_a_cycle_in_caused_by_still_ends`). The writer now refuses exactly that,
so they plant the line by hand (`_append_raw` / `write_bytes`), as a hand-edited log would
hold it; the first also asserts the writer refuses. The defences they test are unchanged.
`_append_raw` gained a `mkdir` for the test that starts from an empty root.

New tests: the two refused adds with the index asserted green afterwards (`update` and
`rebuild`); the self-reference; `test_no_writer_can_append_a_cause_the_log_does_not_hold`
(both appends, four kinds of missing ref, nothing written, index green, then a cross-stream
cause and a same-stream cause accepted); and a source-level test that `paths.stream_path` is
spelled only by the reader, the writer and a profile's read-only property, and that both
appends in the writer go through `_check_causes`.

### 2 [medium] `_mission_state` dropped every mission that was not `open`

**Found.** `outcome == "open"` was the filter, so a paused mission after a failure, an
aborted one (D22's `abort_step` marks the node failed), and one with a node still running
beside a failed one were all ignored and a fresh mission opened; and step 2 of a mission
whose step 2 had failed was refused "not ready: waiting on write-plan" off a fresh fold — a
lie about a step that had already run.

**Fixed.** `_mission_state` takes the latest Builder mission for the workflow whatever its
state, and folds a fresh plan only when there is none, or when the latest is *settled*
(`_settled`: every node finished, not paused, not aborted, no node running) **and** the node
asked for is the workflow's first step — a new run starts at the start. Everything else is
`admit`'s to answer: "mission is paused", "aborted: …", "already failed", "already running".
`_same_plan` now ignores the two budget fields on a node: the Builder writes none, and a
budget a person added narrows spend without widening the manifest, so a budgeted copy of the
plan is still the plan and its breach is refused as a breach rather than as "not the plan".
Tests: one per case the reviewer listed, plus `_fresh_mission_opens_only_at_the_first_step`.

### 3 [medium] `run_step` never asked `floors.floored`

**Found.** `floors.hit` is lexical. `plans -> kernel` inside the workspace made
`plans/meals.md` a write under `kernel/`, and the step ran.

**Fixed.** `_linked_floor`, called after `admit` and before anything is appended: for each
declared write, `floors.floored(workspace, Op(WRITE, path))` — the shared answer from #123,
following every link and looking where the write lands — and then
`floors.floored(root, Op(WRITE, workspace/path))`, so a link *out* of the workspace into the
walle root's floors is caught too. An unresolvable path (a link loop) is refused (D86). The
refusal carries `floors`' own sentence, prefixed with which root it was found against. Tests:
links to `kernel`, `principals/p-maya/hot` and `SHARED` inside the workspace; a link out to
the real hot tier; a loop; and a control where a harmless link still runs.

Reads are not checked against the floor: the floor is under writes (D72), as before.

### 4 [low] merge main (#123, #125)

Merged `57e3a98`. One conflict, in `_inside`: #123 replaced the hand-rolled relative/`..`
check with `floors.segments`; this branch had added the floor check after it. Resolved by
keeping #123's `segments` and asking `floors.hit` on `"/".join(parts)` for a write. #125's
fold order change needed nothing here; the suite is green on it.

### How the round was pushed

The `walle-github` skill writes single-parent commits only, so the merge with main was
pushed as one flat commit on `a5c2de8` carrying the merged tree (my round-2 files and the
#123/#125 files alike), followed by `update-branch`, which records main as an ancestor. That
only works if GitHub's merge is clean, so it was simulated locally first: the first two
tries conflicted (`_inside`, then the import block, where #123 and this branch edited
adjacent lines). Resolved by keeping `_inside` byte-identical to #123's and moving the floor
check into its caller as `_floored_by_name`, and by taking the new imports through the
existing `from walle import …` line. The simulated merge then produced exactly the reference
tree the four checks ran on.

### Checks, round 2

```
$ uv run pytest
4188 passed, 35 skipped in 133.50s (0:02:13)
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
204 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

### Stranger pass, round 2

- `_check_causes` runs before the first-append `fsync` of the directory, so a refusal on a
  brand-new stream leaves no file content and no durable entry to explain.
- `read()` on the locked handle seeks to the end and back; `append` seeks to the end again
  before writing, so the position is not trusted across the check.
- `Node` is frozen; the aborted-mission test builds its budgeted plan with `model_copy`.
  That is what surfaced the `_same_plan` budget question above.
- `_linked_floor` is placed after `admit` so a lexical floor hit is still reported in
  `admit`'s words (one refusal, one voice), and before any append.

### Noticed, not done (round 2)

- `Supersede.target`, `Step.mission`, `Automation.target` are refs the writer does not check;
  their folds report a dangling one rather than fail. Checking them at the append would be
  the same six lines in `_check_causes`, but it changes what a tombstone of a not-yet-synced
  event means across devices, which is a decision, not a fix.
- The writer's own-stream check parses the whole stream on every append that has a cause.
  Fine for Phase 0 logs; a tail cache under the lock is the obvious next step if it shows.
