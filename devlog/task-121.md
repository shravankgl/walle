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
