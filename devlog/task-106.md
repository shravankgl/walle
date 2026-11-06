# Task 106: Phase 3 gate report — survival, retirements and permission counts as one command

Issue #106. Branch `task/p3-gate3`, from `main` at `e546e7b` (task 98). Phase 3 (R8). Dormant:
the report reads a log only when the owner sets `WALLE_GATE3=on` and `WALLE_ROT=on`.

**Shipped:**
- `src/walle/gate3.py`, new. The Phase 3 gate as one report, from one principal's log.
  - `gate_report(records, *, as_of) -> GateReport`: a pure function of the records and an
    explicit, timezone-aware date. It cuts the records at `as_of`, then folds them four ways and
    puts the results side by side: the register (`fold_automations`), survival
    (`walle.survival.survival`), rot labels (`walle.rot.rot`) and the inbox (`fold_inbox`).
  - `GateReport` holds: the `Survival` and `Rot` results unchanged; `active` against `cap` (D6's
    7); `retirements` (name, add ref, retire ref, retire ts); `dead`; `permissions`;
    `not_counted` (how many events each fold could not apply); `above_bar`; and `gate`, the gate
    line.
  - **Dead** means still active in the register but not running: survival says it stopped by
    its day 30, or rot labels it stale or archived. Each `Dead` carries the reasons from both
    sources. A retired automation is a retirement, not dead, because a person already acted.
  - **Permission counts** (`Permissions`) are the approval items in the inbox as of `as_of`:
    asked, approved, denied, orphaned and pending, how many sessions asked, and the most asked
    in one session.
  - `above(survived, cohort)`: is the rate strictly above `GATE_BAR = Fraction(3, 5)`, compared
    exactly. `None` when nothing was measured.
  - `render(report)`: the report as lines for a person. Names, refs, timestamps and counts only.
  - `report(root, principal, *, as_of=None, environ=None)`: reads the log under each stream's
    `LOCK_SH`, not the index, and only once both switches are on. `Gate3OffError` comes before
    anything is read. `FileNotFoundError` for a principal with no log.
  - `gate3_command`: `walle gate3 --root R --principal P [--as-of 2026-12-01T09:00:00+05:30]`.
    Exit 0 when the report printed; 2 when it is off, `--as-of` has no timezone, there is no
    log, or the log does not read.
- `src/walle/cli.py`: two lines that register `walle gate3`.
- `src/walle/rot.py`: docstring only. Rule 7 said the cap refusal was the only other caller of
  `rot`. That was already untrue (maintenance calls it) and this task adds another caller, so it
  now names all three and the switch each one holds.
- `tests/test_gate3.py`, 24 test functions (30 runs with parametrisation), every log synthetic
  and under `tmp_path`.
- `tests/test_rot.py`: **one test changed because the task requires it.**
  `test_nothing_in_the_package_calls_rot_on_its_own` pins the exact list of modules that import
  `walle.rot`. `gate3.py` joins `automations.py` and `maintenance.py` in that list, and the
  docstring says why. The guard is not loosened: any other importer still fails it. A new guard
  in `test_gate3.py` pins `cli.py` as the only module that imports `walle.gate3`.

**Why:**
- PLAN §3 gives Phase 3 one gate: *30-day survival > 60%*. The issue asks for that number
  beside retirements and permission counts, as one command.
- D6: restraint is enforced, and dead automations erode trust silently. So the report counts
  active automations against the cap and names, one by one, every active automation that is not
  running. Survival alone misses one that survived its day 30 and died later. Rot (D114) catches
  that one, so both feed `dead`.
- D22: guard against initial capability, not the last run. Survival has no first real
  measurement yet, so there is no floor to hold it to. The report measures and does not
  pretend: it reads no floor and writes none. Once a real first measurement exists, a survival
  floor would go in the same form as `reports/floor.txt` (Noticed, not done).
- The issue's verify text, *"the report says the 30-day gate is unproven until real use"*, is
  rule 2. The code cannot tell a log of real use from a planted one, so `gate` never says passed.
  It gives the measured rate and which side of the bar it falls on, and leaves the call to the
  owner. With nothing at day 30 it says *unknown, not zero*, as task 97's devlog warned this
  report must.
- D117 and the owner's dormant condition: the switches are in the environment, outside the log.

**Verify:**
- *A planted dead automation shows up:* `test_a_planted_dead_automation_shows_up`. The synthetic
  household has three automations added on day 0. `school-run` runs to day 5 and then goes
  silent. As of day 45 it is the only entry in `report.dead`, with a survival reason ("day 30")
  and a rot reason ("stale"), and `render` names it. Around it:
  `test_a_running_or_retired_automation_is_not_dead` (the retired `bin-night` is in
  `retirements`, not `dead`), `test_a_dead_automation_rot_alone_sees_shows_up` (survived day 30,
  dead by day 70), `test_a_young_automation_gone_quiet_is_not_dead_yet`.
- *The report says the 30-day gate is unproven until real use:*
  `test_with_nothing_at_day_30_the_gate_is_unproven` ("unknown, not zero"),
  `test_even_above_the_bar_the_gate_is_unproven` (100% on the log, still unproven, never
  "passed"), `test_below_the_bar_says_so_and_is_still_unproven` (1 of 3, 33.3%),
  `test_the_bar_is_more_than_sixty_percent_not_sixty` (3 of 5 is not above).
- *Default off (the owner's condition):* `test_the_report_is_off_by_default` (nothing created),
  `test_off_is_checked_before_the_log_is_read` (five environments: neither switch, either one
  alone, wrong spellings; `_read` patched to fail), `test_the_command_is_off_by_default` (exit
  2, says nothing was read, tree unchanged).
- Also covered: permission counts; the WIP cap; `as_of` as a cut (later events change nothing);
  purity under reordering and duplicates; a naive `as_of` refused; render prints no titles or ask
  bodies; read-only (tree hash unchanged); the command's exit 2 paths; no model or network in
  the import closure; no writer imported.

Written first: before the module existed, `tests/test_gate3.py` failed at import (`cannot import
name 'gate3'`). Once it existed, `test_rot.py`'s importer guard failed as expected and was
updated as described above.

```
uv run pytest           4619 passed, 35 skipped in 167.58s
uv run ruff check       All checks passed!
uv run ruff format --check   232 files already formatted
uv run pyright          0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** not needed: no writer, no lock of its own, no derived file. `report`
reads each stream under its own `LOCK_SH`, as `survival.report` and `rot.labels` do. A writer
appending meanwhile either lands before a stream is read or after. Either way the report is
correct for the records it read, and an event stamped after `as_of` is cut, so a moment's
report does not change as the log grows past it.

**Stranger pass:** done on the whole of `gate3.py`, with the error paths and the messages in
mind.
- *A message that read as a contradiction.* A rate of 60.4% printed as "60%, above the bar of
  more than 60%". Rates now print with one decimal, beside the exact "n of m". Fixed. The
  comparison was already exact (`Fraction`).
- *The two lines disagreed:* the survival line said 33% and the gate line 33.3%. Both use one
  decimal now. Fixed.
- *`events[e.retired_ref]`* cannot `KeyError`: the register and `events` are folded from the
  same cut records. `rot` raises if the register holds an add not in its records. It cannot
  here, for the same reason.
- *Error messages:* the off and bad-`--as-of` paths say "nothing was read", which is true
  because both fire before `_read`. A read error that happens partway says "nothing was
  reported", not "nothing was read", because some streams may have been read. The message goes
  through `without_content`, so a refused line's text is not echoed.
- Looked for and not found: an ignored error (every `except` ends in exit 2 with the reason), an
  open file handle (`read_file` owns its handles), a lock held across anything, a write.

**Review:** not run before merge. Under the agent build period (R6, R8) the reviewer agent reads
the PR's head commit and sets `agent-review`. This entry is what it and Shravan read.

**Choices made inside the task:**
- **What "permission counts" means.** The issue gives three words. I read them as the rung-3
  approvals the runs asked a person for: the inbox's `approval` items and how each resolved,
  with the most in one session. That is the number PLAN §6 says approval fatigue shows in
  (openworker's ~15 per run, D73), and it is a fold over the same log. Two readings were not
  taken: the D110 OS-user permissions check (#105), which needs declared owners and judges disk
  layout, and stays its own command, `walle profiles owners`; and standing grants in `grants/`,
  which is outside the log, read through the permission engine's own door, and kernel territory
  (constraint 1). If Shravan meant either of these, it is an addition to this report, not a
  rewrite.
- **Two switches, not one.** The dead column labels rot from the same log, and
  `walle.maintenance` set the precedent of needing `WALLE_ROT` as well as its own switch.
  Reading needs neither the register's nor the outcomes' switch: reading is not acting, as
  `survival.report` already holds.
- **No "gate failed" exit code.** `walle gate` exits 1 on a failed check. Here, by rule 2, code
  gives no gate verdict, so 0 means "the report printed" whichever side of the bar the rate is.
  An exit 1 below the bar would make a script treat a planted log's number as a verdict.
- **Its own module, not `walle.evals`.** This is a fold over a person's log, not an eval over
  fixtures, and `evals` imports the proposer.
- **Names but not titles.** Survival's own `summary()` prints names, so names are already what
  walle shows. Titles, retire notes and ask bodies are a person's free text, and stay out, as in
  `walle stats`.

**What to read first:** `gate_report` in `gate3.py`, especially the `dead` loop (the D6 point),
and the `gate` property, which is rule 2 in code. Then `test_even_above_the_bar_the_gate_is_unproven`.

**Noticed, not done:**
- **A survival floor (D22).** Once the owner has a first real 30-day measurement, it could go in
  a floor file in `walle gate`'s form (`keep survival.rate >= -X% of <first>`). There is nothing
  real to anchor it to yet, and choosing X is a decision.
- **`walle.rot`'s docstring was already stale** before this task: it did not mention
  `maintenance` as a caller. It is corrected here only because this task adds a caller to the
  same sentence.
- **`fold_inbox` takes no `as_of`.** This report cuts the records itself. A second caller that
  wants the inbox as of a date would repeat the cut. If a third appears, it belongs in the fold.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not visible to the author at the time of writing. See Hyperagent's record of this
thread (author side only).
