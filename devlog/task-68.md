# Task 68: the manifest check before dispatch, and per-step budgets that abort the mission

Issue #68. Branch `task/p2-manifest`, from `main` at `6cf7b17` (task 67's merge). Phase 2's
second task (R8). As with task 67, nothing here dispatches anything: this is the gate a runner
will have to come through, the breach the fold reports, and the event that records an abort.
The switch is still off.

**Shipped:**
- `src/walle/manifest.py`, new, 88 lines, pure. `Touch` is what a step is about to do: the paths
  it will read and write and the hosts it will send to. `undeclared(node, touch)` is every item
  the node's manifest does not cover, in touch order, each saying which list it is missing from
  (`"read 'principals/p-sam/log' is not in reads"`). `covers_path` and `covers_host` are the
  matching rules: a declared path covers itself and what is under it after `posixpath.normpath`
  on both sides, and nothing beside or above it, and a touched path that climbs out through `..`
  is inside nothing; a declared host covers exactly that host, lower-cased, no subdomains, no
  wildcard, no port. `reads` and `writes` are separate grants. Imports `posixpath` and nothing
  else at runtime.
- `src/walle/missions.py` gains rules 6 and 7 of the docstring and what implements them:
  - `Breach` (`node`, `kind` — `tokens` or `cost_usd` — `spent`, `budget`) with a `reason`
    sentence; `NodeState.breaches`, `MissionState.breaches` (node order), `MissionState.breach`
    (the first, the abort's reason) and `MissionState.aborted`. A breach is spend *strictly over*
    a declared budget; a node without a budget never breaches.
  - `NodeState.spent_from`: the refs of the live status steps that reported spend, in ref order.
    What an abort step cites, and the per-step figures a later exporter (#80) reads.
  - `_held(state, environ)`: why nothing in the mission may run — off, paused, aborted, in that
    order. `dispatchable` now goes through it, so a breached mission dispatches nothing.
  - `admit(state, node_id, touch, environ) -> Refusal | None`: the gate. Refuses with the first
    reason that holds: missions are off; paused; aborted (with the breach's reason); the node is
    unknown, already finished, or not ready (naming what it waits on); then every undeclared item
    of the touch, joined by `; `. A `running` node is admitted so the same call is the check at
    each tool call while a step runs.
  - `abort_step(state, breach, *, ts, actor=kernel)`: the `status failed` step a runner appends on
    a breach, the reason as its `note`, `caused_by` the status steps whose spend crossed the line.
    `ts` is an argument: the module still reads no clock.
- `src/walle/events.py`: `Node` gains `cost_budget_usd: float | None` (`gt=0`). Optional, default
  `None`, excluded from the canonical form when unset, so every v1 line written before this task
  loads and hashes exactly as before — `test_a_mission_without_cost_budgets_hashes_as_it_did_before`
  proves it on a line from the `chores` fixture, and task 67's `DAG_HASH` pin did not move. An
  optional field is not an incompatible change, so no schema bump and no upcaster (D96; `Event`'s
  own rule: "bumped when the shape changes incompatibly").
- `tests/fixtures/v1/budget/`, a second synthetic golden log for the mission types (D88, D96): a
  made-up Ira's Sunday, one mission of three nodes with both budgets declared, three `running`
  reports with `tokens` and `cost_usd`, the token budget crossed on the third, and the abort step
  exactly as `abort_step` writes it. 21 sentinels, including `cost_budget_usd` on two nodes and the
  abort's `note`. Like `chores`, its `.jsonl` is force-added past the `principals/` ignore rule.
- `tests/test_manifest.py`, 73 tests: the two verify criteria, the matching rules, the gate's
  order, the budget edge cases, the abort step, the dormant rule, purity, and the new fixture's
  breach and DAG pinned by hash.
- `tests/test_missions.py`: one line. The import allowlist for `missions.py` gains `walle.manifest`.
  The task requires this edit: the gate's matching rules live in their own module and `missions`
  imports it; the new module is held to the same purity test (`forbidden_in_closure`, its own
  import allowlist, no `os`, no clock) in `test_manifest.py`.

**Why:** D32 rule 2 — "the manifest is the permission grant" — and D67's "checkable per step
before dispatch" are a check, and until now nothing performed it. `admit` is that check, and it
runs on what the step is *about* to touch, so the refusal lands before the read, the write or the
request, never after. D75 is why `egress` is a host list checked the same way. D22 is the budget:
"express limits declaratively, with threshold abort" — the limit is on the node, the spend is in
the log, and the abort is read from the two, not from a flag a runner has to remember to set.
D117 is the dormant rule: the gate refuses everything, declared or not, while `WALLE_MISSIONS`
is not `on`, and a breach is reported on or off because reading the budget is not acting on it.

**Verify:**
- *A step touching anything undeclared is refused before it runs:*
  `test_a_step_touching_anything_undeclared_is_refused_before_it_runs` — a runner that starts a
  step only when `admit` returns nothing never starts this one, and the refusal names the path.
  Around it: `test_every_undeclared_item_is_in_the_refusal`, `test_a_running_node_may_keep_touching_
  what_it_declared`, `test_reads_and_writes_are_separate_grants`, and the two parametrized tables
  over paths (`..`, shared prefixes, absolute vs relative, case, empty) and hosts.
- *A budget breach aborts the mission and says why:*
  `test_a_token_budget_breach_aborts_the_mission_and_says_why` and `test_a_cost_budget_breach_...`
  — `breach` names node, amount and budget, `dispatchable` is empty, `admit` refuses with
  `aborted: <reason>`. `test_a_breach_on_one_branch_holds_the_whole_mission` is the "mission, not
  node" part; `test_the_abort_step_records_the_reason_in_the_log` folds the abort step back in.
- *Off by default:* `test_the_gate_refuses_everything_while_missions_are_off` (switch unset and
  explicit empty environment: a fully declared touch on a ready node is refused with "missions are
  off"), `test_only_the_one_spelling_opens_the_gate`, `test_nothing_in_the_log_can_open_the_gate`
  (no file, no `environ`, no enable field on `Node`), `test_the_cli_gains_no_manifest_or_budget_
  command`.
- *The rebuild stays green:* `test_golden` picks up `v1/budget` by directory (hash, fold, sentinels,
  incremental build, read-only fixture), and
  `test_the_breach_in_the_golden_log_is_the_one_it_shipped_with` folds it from the rebuild, the
  index and the raw lines, checks the breach, checks the abort step's note equals the fold's own
  reason, and pins the DAG hash.

Written first: with no `walle.manifest`, `tests/test_manifest.py` failed at import. Two of its
tests were wrong, not the code: one built a mission without links and expected a node to wait; one
appended the abort step on a second stream, where ref order (rule 2, `d-phone` before `d-tab`) put
it *before* the spend it answered, so the later `running` won — exactly task 67's two-runner note.
Both now build what they claim to.

```
uv run pytest               2062 passed, 10 skipped in 66.41s
uv run ruff check           All checks passed!
uv run ruff format --check  132 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the gate and the fold open nothing, so the walkthrough is about what
each writer *concludes*. Writers A and B run the same mission on two devices and both have node
`x` running with `token_budget` 2000. A appends `status running tokens=1500` to its stream; B,
not having seen A's line, appends `status running tokens=1500` to its own. Each folds its own log:
1500, under budget, no breach; each goes on. When the streams meet, both folds see 3000 > 2000, the
same breach, the same reason, and `admit` refuses both on the next tool call. Neither wrote an abort
step yet; whichever appends one first, the other's fold reads it and finds the mission already
`failed`; if both append one, two `failed` steps with the same note land, the later in ref order
wins, and both machines fold the same converged log to the same DAG. The budget was not enforced across the gap — it cannot be without a
lock across machines — but the moment the logs meet, the overrun is visible, attributed to both
steps by `spent_from`, and nothing further runs. That is D57's answer: the log converges, the
gate reads the converged log.

**Stranger pass:** done on the whole diff (about 270 lines of `src`, one new module, one error
path in `admit`). Found and fixed:
- `Breach.reason` formatted dollars with `.2f`, so a $0.0061 spend over a $0.005 budget would have
  read "spent $0.01 of its $0.01" — a status message that lies. Now `_dollars`: to the cent at
  least, to the millionth at most, never rounded into equality; tested with four cases.
Looked for and not found: an empty "waiting on" (a pending node not in `ready` always has at
least one predecessor not completed, by `ready`'s definition); a path where `admit` and
`dispatchable` disagree (both go through `_held`, and `admit` on a node `dispatchable` lists
passes the state checks); a float false-breach (spend is rounded to 6 places at each add, so
`0.05 + 0.05` is `0.1`, not `0.1000000001`, and exactly-the-budget is tested); a way a touch could
be normalized *into* a grant (`..` is resolved before comparing, and a result that starts with `..`
covers nothing); spend lost from the hash (`spent_from` is derived from steps `applied` already
lists, so `dag_hash` needs no change and task 67's pin stands).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run; the brief replaces it for agent work.

**Choices made inside the task:**
- **A cost budget field on `Node`, not a derived one.** The issue says each step has a token *and*
  a cost budget; D67 names `token_budget` and `cost_usd` "for D22's threshold abort". Task 67 read
  `cost_usd` as spend on status steps, and the fold sums it. A cost *limit* needs a place, and the
  node — beside `token_budget` — is the one D67 implies. Optional and additive, so no bump. Not
  flagged: this extends the owner's own issue and contradicts no D-number.
- **The abort is read from the log, and the abort step is a record of it.** The alternative — a
  `pause` step as the abort, and `dispatchable` trusting it — is fail-open: a runner that crashed
  between the breach and the append would resume dispatching. Here `breach` is a pure property of
  the fold, `dispatchable` and `admit` refuse on it whether or not any step was written, and
  `abort_step` is for the reader and for `walle why`. Consequence, stated in rule 6: a breached
  mission stays aborted; the only ways out are a tombstone on a spend step (rule 1, D8) or a new
  mission with a bigger budget, because the plan is never edited.
- **`failed`, not `pause`, for the abort step.** The step failed; that is the true status. A pause
  would say "held" about a mission that cannot be resumed, and a `resume` on it would then not
  resume — a status message that lies.
- **A breach holds every branch.** The issue says "aborts the mission". A sibling branch that keeps
  going would spend more on a plan the owner's limit already said to stop.
- **Strictly over, not at.** Spending exactly the budget is spending the budget.
- **`running` is admitted.** The manifest check "before dispatch" has to be the check at each tool
  call too, or a step declares nothing, starts, and touches what it likes. One function for both.
- **Exact hosts, no wildcard.** D75's point is that egress is the channel that matters most. A
  grant for `*.grocer.example` reads as one host and is many; the owner lists the hosts.
- **`reads` does not imply `writes` and `writes` does not imply `reads`.** Two lists, two grants,
  as D32 rule 2 wrote them. A step that does both declares both; the refusal says which is missing.
- **The refusal order is fixed and documented** (off, paused, aborted, node state, manifest), so a
  single refusal tells its reader which earlier reasons did not apply.
- **`spent_from` on `NodeState`, not in `dag_hash`.** It is provenance for the abort step and the
  exporter; it adds no information the hash does not already cover through `applied`.
- **A third fixture directory.** `chores` is frozen (task 67's reasoning holds), and a log that
  shows a breach from raw lines is the rebuild proof this task needs.

**What to read first:** `admit` and `_held` in `missions.py` (about 40 lines), then rule 6 and 7 of
the module docstring. Then `manifest.py`'s docstring — what a declaration covers is the whole
security argument. Then `test_a_step_touching_anything_undeclared_is_refused_before_it_runs`,
`test_a_breach_on_one_branch_holds_the_whole_mission`, and
`test_the_gate_refuses_everything_while_missions_are_off`.

**Noticed, not done:**
- **Per-step token and cost figures for #80.** They are in the log already: each `status` step's
  `tokens` and `cost_usd`, the node's `spent` and `spent_from` in the fold, and the breach's
  reason in the abort step's `note`. An exporter needs no new event; it needs `fold_missions` and
  `walle.reader`. Not built here, per the issue.
- **`Step` docstring says `tokens` and `cost_usd` are "what this step spent so far"; the fold sums
  them,** so in practice they are per-report deltas and both fixtures write them that way. One of
  the two sentences should change; the fold's reading is the one both fixtures rely on.
- **A declared path of `.` or an empty string covers nothing**, by design, but nothing tells the
  mission's author so at construction. `Node` could refuse them the way `NodeId` refuses dots.
- **No wall-clock budget.** D22 speaks of thresholds in general; a step that spends nothing and
  never ends is not caught here. D91's `running` recovery is the nearer home for that.
- **The gate takes paths as strings.** A runner will hand it paths relative to the walle root;
  nothing here pins which root, because nothing here has one. The container executor (D68) will.
- **`walle why` still does not follow `Step.mission`** (task 67's note); the abort step's
  `caused_by` now reaches the spend steps, so `why` on an abort shows the overrun.
- **PLAN §9's status column** is not updated; `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time; Hyperagent's figure for this thread to be
filled in by the owner.
