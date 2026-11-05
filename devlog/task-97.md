# Task 97: outcomes, and how many shipped automations still run at day 30, dormant

Issue #97. Branch `task/p3-survival`, from `main` at `b4efc53`. Blocked by: #95 (closed).
Phase 3 task (R8). Dormant: nothing records an outcome unless the owner sets
`WALLE_OUTCOMES=on`.

**Shipped:**
- `src/walle/events.py` gains one type at `v = 1` (D96), and a closed result set:
  - `OutcomeResult`: `ok`, `failed`. There is deliberately no `stopped` (see the choices below).
  - `Outcome`: one observed run. It carries `target` (the ref of an automation's add), `result`
    and an optional `note`. There is no `survived` or `active` field, because survival is what
    the fold says, never something one run declares.
- `src/walle/survival.py`, the module, about 300 lines with its docstring. The docstring states
  seven rules and the two-writer walkthrough.
  - The pure part: `survival(records, *, as_of) -> Survival`. `Survival` holds the `cohort` (a
    `Verdict` for each automation that has reached day 30, with `survived`, a `reason` and
    `last_ok`), `not_yet`, the outcomes it did not count in `unapplied` with reasons, `rate`,
    and `summary()` in plain words.
  - `SURVIVAL_DAYS = 30` and `RECENT_DAYS = 7`, both constants.
  - The read: `report(root, principal, as_of=None)`, which works while the switch is off.
  - The write: `record(root, principal, device, name, result, note=, actor=kernel)`, which is
    refused while the switch is off.
  - The switch: `OUTCOMES_SWITCH` (`WALLE_OUTCOMES`), `SWITCH_ON`, `enabled(environ)`, the
    same shape as the inbox's and the register's.
- `tests/fixtures/v1/outcomes/`, the golden log for the new type at `v1` (D88, D96).
  `test_golden`'s coverage check requires it. It is Ines's made-up household: four adds, ok and
  failed outcomes, a retire, a supersede of an outcome, an outcome that names another outcome,
  and a person-recorded outcome, with 18 sentinels. The belief hash is the empty-state hash,
  because no outcome is a belief.
- `tests/test_survival.py`, new, 36 tests.
- `tests/test_events.py`: `_one_of_each` gains an `Outcome`. **The task requires this test
  edit.** `test_every_taxonomy_type_is_registered` asserts that this list and `EVENT_TYPES`
  agree, and the round-trip test then covers the new type. Tasks 70 and 95 made the same edit.

**Why:**
- **D19:** capability events record outcomes, not just changes. An `outcome` is §5.2's
  taxonomy entry ("result observed against a capability or mission"), and here its target is
  an automation, the thing D20's second stage asks about.
- **D20 and PLAN §3:** the real gate is "30 days later: the automation is still in use", and
  Phase 3's metric is 30-day survival above 60%. `survival` is that number.
- **D6:** "Dead automations erode trust silently". So a stopped automation has to show up as a
  stop, and that is the issue's second criterion.
- **Hard constraints 3 and 5:** the report is a fold over the log and an explicit date. It reads
  no clock, opens no file, calls no model and keeps no state outside the log.
- **D72 does not reach this.** Recording a run does not create, change or delete an automation,
  so the runner (`kernel`) may write outcomes. That is why this task could be built while #96,
  which does change the register, was flagged.
- **D117 and the owner's dormant condition:** install is not enable. `record` is off unless
  `WALLE_OUTCOMES` is exactly `on` in the environment, which no event can reach. Turning the
  register on (`WALLE_AUTOMATIONS`) does not turn this on. There is no CLI command, and nothing
  calls `record`.

**Schema version.** A new event type is a schema decision, so it is recorded here, as tasks
67, 70 and 95 did. `outcome` starts at `SCHEMA_VERSION = 1` with no upcaster, and no existing
type changes. `target` is a plain `Ref`, not "an automation ref", so a later outcome against a
capability or a mission (§5.2) needs no bump. Which targets count is the reader's call.

**Verify:** *survival is computed from the log alone; a stopped automation counts against it.*
- *From the log alone:* `test_survival_is_computed_from_the_log_alone` checks that the same
  report comes out of the JSONL, a rebuilt index, an incremental index, the records shuffled
  and the records duplicated. `test_what_the_log_learns_after_as_of_changes_nothing` checks
  that a day-40 report is unchanged after a retire, an outcome and an add land on day 41.
  `test_the_report_has_no_clock_of_its_own` checks that a naive `as_of` is refused.
  `test_the_computation_reads_no_clock_and_opens_no_file` checks it at the AST level, and
  `test_survival_reaches_no_model_and_no_network` checks the import closure.
  `test_survival_over_the_golden_log_is_the_one_it_shipped_with` pins the fixture's report.
- *A stopped automation counts against it:* `test_a_stopped_automation_counts_against_survival`
  uses a household of four. One is retired on day 12 and one went silent after day 10. Both
  are in the cohort, both are `stopped` with a reason, and the rate is 2/4.
  `test_an_automation_that_only_failed_in_its_last_week_did_not_survive` covers failures, and
  `test_a_tombstoned_outcome_does_not_count` covers retracted runs. The boundaries are
  `test_the_window_is_the_week_up_to_day_30_inclusive` and
  `test_an_automation_retired_after_its_day_30_survived`.
- *Dormant, as the owner asked:* `test_recording_outcomes_is_off_by_default` (nothing is
  created on disk), `test_off_is_checked_before_the_log_is_read`,
  `test_only_the_one_spelling_turns_recording_on` (seven wrong spellings),
  `test_the_automations_switch_does_not_turn_recording_on`,
  `test_reading_the_report_is_not_acting_and_works_while_off`,
  `test_nothing_in_the_log_can_turn_recording_on`, and
  `test_the_cli_gains_no_outcome_or_survival_command`.

Tests first: before the module existed, `tests/test_survival.py` failed at collection
(`ImportError: cannot import name 'survival' from 'walle'`). I then mutated `survival.py` ten
ways: the switch on by default, no `as_of` filter, retire ignored, tombstones ignored, the
window's lower bound made inclusive, runs after day 30 counted, young automations counted,
stray outcomes counted, `failed` counted as `ok`, and a naive `as_of` accepted. All ten were
killed by `tests/test_survival.py` alone.

```
uv run pytest               2998 passed, 30 skipped in 124.28s
uv run ruff check           All checks passed!
uv run ruff format --check  170 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 30 skips are the same as on `main`. The suite ran as an unprivileged user (uid 1000), and
nothing here assumes root.

**Two-runner walkthrough:** this is written out in the module docstring. `record` takes no
lock beyond the stream's. Writer A (the runner, laptop) folds the register and sees
`bin-night` active. Writer B (a person, phone) retires it under `automations.lock`. Then A
appends its outcome under `d-mbp`'s `LOCK_EX`, fsynced. The log ends up holding an outcome a
moment after the retire, and no answer changes. A retire on or before day 30 has already
stopped the automation, whatever ran after it. A retire after day 30 means the outcome is
outside the week that counts. So a lock around read-and-append would buy no correctness, and
leaving `automations.lock` alone means the runner never waits on a person's register change.
The report reads streams directly (each under `LOCK_SH`) and never the index, so it takes no
`index.lock`.

**Stranger pass:** done on the whole diff. What it found:
- *Inconsistent instants in messages.* The verdict reasons used `datetime.isoformat()`, which
  drops `.000000`, while `last_ok` used the log's microsecond spelling. A reader grepping the
  log for the instant a reason quotes would miss it. Everything now goes through `_spell`.
  Fixed.
- *Ordering across offsets.* Every `ts` is an RFC 3339 instant with an offset, so
  `datetime.fromisoformat` gives aware datetimes that compare correctly. A string comparison
  would not. `as_of` must be aware too, or it is refused.
- *`events[entry.retired_ref]`.* This cannot `KeyError`: the register and `events` are folded
  from the same filtered records, so any retire the register applied is in `events`.
- *Status messages.* `summary()` says "unknown, not zero" for an empty cohort, because a rate of
  0% for "nothing old enough yet" would be a lie the gate report (#106) could repeat.

Looked for and not found: an ignored error (there is no `except` in the module), a file handle
left open, a lock held across anything, and a write before every refusal (the switch, the
result, the principal and the name are all checked before `append`).

**Review:** independent review is the reviewer agent's job on this PR, per the brief. I did not
run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **"Still running" means an `ok` run in the 7 days up to day 30, and not retired by then.**
  "In use" needs evidence, and the only evidence is a recorded run. Seven days is D114's
  reference interval, and the longest gap a weekly automation leaves. A monthly automation
  would fail this test. When one exists, the window can become per-automation (its cadence),
  which is a change to this module only.
- **No `stopped` result.** An automation that stopped writes nothing, so a `stopped` value would
  only be present when something remembered to write it. Silence is what gets counted. The
  issue's "a stopped automation counts against it" holds because silence and retirement both
  count against.
- **A failure in the last week does not by itself count against.** If there is also an `ok` run
  in that week, the automation is still running. I counted it as surviving rather than
  inventing a ratio.
- **A tombstone retracts an outcome here, unlike in the register.** A supersede of a run says the
  run did not happen as recorded, which is exactly what a supersede is for. The register's rule
  5 refuses tombstones because there they would bypass the retire and unpin rules. Nothing
  like that exists for a record of a run.
- **No lock in `record`** (see the walkthrough).
- **`report` defaults `as_of` to now; `survival` never does.** The clock is read in the IO
  wrapper only, so the computation stays a pure function of its arguments.

**What to read first:** `survival()` in `src/walle/survival.py`, about 70 lines, which is the
whole metric. Then rule 3 in the docstring, the definition of "still running", which is the
choice most open to disagreement.

**Noticed, not done:**
- #96 (rot detection) is flagged `needs-shravan`. Its automatic transitions conflict with D72's
  human-only floor for changing an automation. The flag on #96 gives the options. Branch
  `task/p3-rot` is claimed at `main` with no commits.
- `ARCHITECTURE.md` §5.2 lists `outcome` as written by the "Operator" against a `capability` or
  `mission`. Here the runner is `kernel` and the target is an automation's add. The table may
  want a line, but it is a CODEOWNERS path, so it is left for the owner.
- `walle.automations.Register.suggestion` still offers the oldest unpinned automation. With
  outcomes in the log it could offer the one with the oldest `ok` run instead (the task-95
  devlog anticipated this). That changes #95's behaviour, so it is not done here.
- `MACHINE_ACTORS` still lives in `walle.inbox` (noted by tasks 70 and 95).

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
