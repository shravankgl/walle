# Task 22: Consolidation: a snapshot that names what it forgets, and the gate that times it

Issue #14. Branch `task/22`. PLAN §9 tasks 22–23, built as one PR: the view, the forgets check,
the plan, the gate, and the locked write. The digest itself (the Curator's model call) is not
here and must never be reachable from here.

**Shipped:**
- `src/walle/consolidation.py` (new, pure). It opens no file, reads no clock and calls no model.
  - `view(records) -> View` is the log with what live snapshots forget taken out. `View.records`
    keeps the input order. `View.forgotten` lists the refs taken out, in ref order.
    `View.held` lists the refs a live snapshot forgets that the view kept anyway, with the
    snapshot's ref and the reason.
  - `forgettable(records)` gives every ref a snapshot could forget right now.
  - `check_forgets(records, forgets)` raises `SnapshotError` naming every problem at once:
    forgets nothing, a ref named twice, a malformed ref, a ref not in the log, a type that
    can't be forgotten, a ref some supersede or contradiction names, or a ref a live snapshot
    already forgets.
  - `plan_forgets(records)` picks the older half, by `observed_at` then ref, of the
    observations still in the view that nothing names (D89).
  - `gate(records, now, policy=None) -> Gate` applies D95. `Gate` carries `due`, `reasons`
    (one line per condition that fails, empty exactly when due), `idle_for`,
    `new_observations`, `last_run` and `since_last_run`. `Policy(idle, new, gap)` defaults
    to `MIN_IDLE` = 30 min, `MIN_NEW` = 20 and `MIN_GAP` = 12 h, and refuses negative
    durations and `new < 1`.
- `src/walle/snapshots.py` (new, IO). `write_snapshot(root, principal, device, *, forgets,
  body, model, now=None, policy=None) -> ref` takes `consolidate.lock`. Under it, it brings the
  index up to date, checks the gate, checks forgets, and appends one `snapshot` event
  (actor `curator`, forgets in ref order, `ts` = now). It raises `NotDueError` (which carries
  the `Gate`), `SnapshotError`, `FileNotFoundError` or `ValueError`/`TypeError`, always before
  anything is written. `consolidate_lock_path` lives here rather than in `paths.py` (see
  *Choices*).
- `tests/test_consolidation.py` (new): 32 test functions, 236 cases, 200 of them random-log
  seeds.
- `tests/test_snapshots.py` (new): 12 test functions, 13 cases, including a two-process lock
  test.
- No existing file changed. `cli.py`, `resolver.py`, `events.py` and `paths.py` are untouched.

**Why:**
- **D89: a snapshot names what it forgets.** `Snapshot.forgets` already existed from task 2.
  This task decides what it *means* to the fold. The issue's criterion is "folds
  identically", and `body` is a model's text that no fold can read. Together those settle it:
  a snapshot can only stand in for events the fold does not read for belief. Those are
  observations and earlier snapshots ("summaries get summarized"), and only when no
  `supersede` or `contradiction` looks them up. The view enforces this at fold time, so it
  holds for any snapshot anyone writes, not only the ones `check_forgets` approved.
- **Hard constraints 3 and 4.** The snapshot is an event in the log, never a rebuild artifact.
  The view and the gate are pure functions of the records, so they replay. Superseding a
  snapshot undoes it, the same rule-1 liveness the resolver uses (D8).
- **D95: idle, new, not too often.** All three must hold. `now` is an argument, so the gate
  never reads the clock. Activity is any event except a snapshot, so the Curator's own write
  can't reset the idle clock it is gated on. "New" means observations with `ts` later than
  the newest snapshot. "Last run" is the newest snapshot, live or not, because an undone run
  still ran.
- **D79, skip on overlap.** The second of two concurrent runs is refused by the gate after it
  gets the lock (see the walkthrough), so no separate rule is needed.

**Verify:** *snapshot event folds identically to the events it replaces.*
- `test_a_snapshot_folds_identically_to_the_events_it_replaces` builds a two-device log with
  every event type and plans a snapshot. It asserts `resolve(view(log + snapshot)) ==
  resolve(log)`, the same for `staged`, and the same for the log with the snapshot but no
  view.
- `test_any_snapshot_in_any_log_folds_identically[0..199]` uses 200 seeded random logs with
  snapshots that forget *anything*: assertions, supersedes, refs not in the log. In each one
  the view folds the same as the log. Two mutations were tried and reverted: removing the pin
  rule fails 172 of the 200 seeds, and letting the view hide any type fails 163 seeds plus 5
  named tests.
- `test_a_written_snapshot_folds_identically_on_disk` writes a real snapshot, destroys and
  rebuilds the index, and asserts the view's `state_hash` equals the log's from before the
  write (D66).
- Confirmed failing first: before the modules existed, both test files stopped at collection
  with `ModuleNotFoundError: No module named 'walle.consolidation'`.
- Checks on the final tree:
  ```
  uv run pytest              696 passed, 3 skipped in 23.63s   (447 before; +249 new cases)
  uv run ruff check          All checks passed!
  uv run ruff format --check 56 files already formatted
  uv run pyright             0 errors, 0 warnings, 0 informations
  ```

**Two-runner walkthrough:** Curator runs A and B for principal maya, both with the same
planned `forgets` and a digest. Observer W appends turns, and Curator C writes a
contradiction. The same walkthrough is in the `snapshots.py` docstring.
1. A takes `consolidate.lock`. B blocks, having read nothing and **not yet read the clock**.
2. A runs `index.update` (index.lock, then every stream shared, briefly), then folds: idle,
   45 new, 24 h since the last run, so it is due. The forgets pass the check. A appends to
   `d-srv.jsonl` under that stream's lock, which is held through `fsync`. Then A releases
   `consolidate.lock`.
3. W appends to `d-mbp.jsonl` under its own stream lock only. A new observation can't change
   what A's snapshot hides, so W never needs `consolidate.lock`.
4. B wakes, reads the clock, then the log. It sees A's snapshot (0.00 h since the last run),
   raises `NotDueError`, and writes nothing.
   `test_a_second_run_waits_for_the_lock_and_finds_the_first_runs_snapshot` stages this
   with a second process. It checks B is still blocked and has written nothing after 1 s,
   then that B reports "0.00 h since the last run (d-srv:1)".
5. C appends a contradiction naming an observation A forgot. C takes no consolidation lock.
   On every later fold the view holds that ref back in and reports it under `held`, so the
   fold stays identical. `test_a_contradiction_written_after_the_snapshot_pins_what_it_names`
   covers this.

Lock order: `consolidate.lock` → `index.lock` → stream files (shared, released) → one stream
(exclusive). Nothing else takes `consolidate.lock`, and nothing takes it while holding another
lock, so there is no cycle with `staging.lock` or the indexer. Not covered: two devices that
don't share a filesystem. Each takes its own lock, so the log may record two runs. The view is
still identical. Sync is deferred (D57).

**Stranger pass** (the diff is about 1,270 lines and touches IO and a lock). Found and fixed:
- **The clock was read before the lock.** With `now=None`, a run that waited behind another
  would be gated on a stale "now", and its `ts` could be earlier than the run it waited for.
  This is the writer's stale-seq bug in another form. The clock is now read inside the lock.
  `test_the_clock_is_read_after_the_lock` covers this.
- **A refusal whose numbers said it should pass.** The hours were rounded, so 11 h 59 m 59 s
  printed "12.00 h, needs 12.00 h". They are now floored, like the minutes. The gap test has
  a case one second short of the threshold.
- **A bare string as `forgets`** iterated its characters and failed on `'d'`. It is now a
  `TypeError` that says so.
- **The module docstring overstated its readers.** It said the Curator and proposer "read"
  the view. Nothing reads it yet, and the text now says that.
- Checked and left as is: every refusal happens before the append, so a raised error always
  means nothing was written. The lock file is opened `ab` and closed by the context manager,
  and the lock goes with it. `index.update` errors (dangling ref, future schema) propagate
  before any write. A principal with no log raises before the lock file is created, so no
  folder appears. `view` runs the resolver's own duplicate-ref check over *every* record
  before hiding anything, so a hidden ref can't mask a corrupt log
  (`test_one_ref_naming_two_events_is_still_refused`).

**Review:** pending.

**Choices made inside the task:**
- **What the view may hide is decided by the view, not trusted from the snapshot.** The
  alternative was "trust `forgets` because `check_forgets` approved it at write time". That
  breaks as soon as a later contradiction or supersede names a forgotten ref, and it depends
  on which writer wins a race. Deciding at fold time makes the criterion hold for every log,
  which the random-log test checks.
- **A snapshot can't forget a belief.** Letting it would need the snapshot to carry the folded
  state the fold could apply. That is a schema change (a v2 `snapshot` with an upcaster, D96)
  and a new decision, so it is not made here. Fusing or retiring beliefs goes through
  `assertion` + `supersede`, which have a reviewer (D49).
- **An assertion's `caused_by` doesn't pin its evidence.** The fold never looks those refs up,
  and `walle why` reads the index, which keeps the whole log. Pinning them would make most
  observations unforgettable, for no gain in correctness.
- **"Since the last run" uses `ts`.** The alternative was "not forgotten by any snapshot".
  Because D89 keeps the recent half verbatim, that would count the kept half again and
  re-trigger after 10 new observations instead of 20. The cost of `ts` is cross-device clock
  skew near the boundary, which can only move *when* a digest is written, never what is
  believed.
- **The gate is enforced inside `write_snapshot`, with no force option.** D89's hard trigger,
  a hot tier that no longer fits, is a different path and isn't built.
- **New files only, as asked for parallel work.** `consolidation.py` imports the resolver's
  private `_by_ref` and `_liveness` rather than adding public names to `resolver.py`. That way
  the view can never disagree with the fold about liveness or duplicates. It is a deliberate
  coupling, and it is listed below to undo. For the same reason `consolidate_lock_path` sits
  in `snapshots.py`, not `paths.py`. There is no CLI command: without the Curator's model a
  `walle consolidate` could only print the gate, so `cli.py` is untouched.
- **The pure module is checked by its own test** (`test_the_pure_module_reads_no_clock_file_or_model`).
  It allowlists imports and bans `.now`/`.today`/`.time`/`.utcnow`. I did not add the module
  to `RESOLUTION_ROOTS` in `tests/test_constraints.py`, which is CODEOWNERS-protected. Its
  imports (`events`, `resolver`, `reader`) are already inside that closure.

**What to read first:** the four numbered rules at the top of `consolidation.py`, then
`_why_not` and `view`. If the rule for what may be hidden is wrong, the fold stops being
identical. `test_any_snapshot_in_any_log_folds_identically` is the test that would notice.

**Noticed, not done:**
- Promote `resolver._by_ref` and `resolver._liveness` to public names, and move
  `consolidate_lock_path` into `paths.py`, once the parallel tasks touching those files have
  merged.
- Add `consolidation` to `RESOLUTION_ROOTS` in `tests/test_constraints.py` (needs Shravan,
  CODEOWNERS).
- `plan_forgets` considers observations only. Decided proposals are also fold-neutral when
  nothing targets them, but leaving them in costs nothing yet.
- Nothing reads the view yet. The proposer (tasks 16–18) and the Curator are its first
  readers.
- D89's hard trigger (the hot tier no longer fits, forcing a full reset) is unbuilt.
- The snapshot's `caused_by` is left empty. Whether it should repeat `forgets`, so `walle why`
  on a snapshot shows its sources, is a question for task 12's `why`.

**Cost:** pending.
