# Task 75: `walle gate` — the rebuild test and an eval regression floor, as one command

Issue #75. Branch `task/p2-gate`, from `main` at `85974c2`. Blocked by #74 (workflow
proposals), which is closed. Phase 2 task (R8). Dormant: off unless the owner sets
`WALLE_GATE=on`.

**Shipped:**
- `src/walle/evals/gate.py`, new, about 470 lines with its docstring. It does two halves and
  gives one answer:
  - *Rebuild:* `rebuild_fixtures` copies every golden fixture under `tests/fixtures/v<N>/<name>/`
    to a scratch folder, rebuilds its index from the log alone (`index.rebuild`), and checks
    the event count, that no line was skipped, and the resolved `state_hash` against
    `__expected__.json`. `rebuild_logs` rebuilds every log the eval below has just built, and
    checks that the state folded from the rebuilt index equals the state folded straight from
    the JSONL. One check per fixture, plus one for all the eval logs.
  - *Regression:* `evaluate` runs the fake eval (the same `report.evaluate` that `walle eval`
    runs) over `tests/corpora` and the synthetic LongMemEval and LoCoMo files under
    `tests/fixtures/evals`. Then `measure` turns the result into the six `METRICS`, and
    `floor_checks` holds each one to its rule. The D77 ship gate is also one check.
  - *The floor file's grammar:* `parse_floor` and `Rule`. One `keep` line per metric, either
    `keep <metric> <= <limit>` (absolute) or `keep <metric> >= -1% of <floor>` (relative to the
    floor, D22's own form). Numbers are exact `Fraction`s, so `19/30` means 19 of 30 items with
    no rounding. A missing, duplicate or unknown metric, a relative limit with no floor, an
    absolute limit with one, a bad number or any other line is a `FloorError`, and the gate
    does not start.
  - *The switch:* `GATE_SWITCH` (`WALLE_GATE`), `SWITCH_ON`, `enabled(environ)`, and
    `GateOff`, the same shape as the Builder's and Codex's switches.
  - `gate_command`, `walle gate [--repo .]`. It exits 0 when every check passes, 1 when one
    fails, and 2 when the gate is off or cannot start (an input is missing or does not load).
- `reports/floor.txt`, new: the six rules. Each floor is what the fake model measures on the
  tree today (2026-11-05).
- `src/walle/cli.py`: two lines to register `gate`.
- `reports/README.md`: a paragraph saying `floor.txt` is not a report, and who may move it.
- `tests/test_gate.py`, new, 47 tests.

**Why:** PLAN §3's Phase 2 gate is "rebuild test green; no eval regressions". Until now these
were two separate things, `pytest tests/test_golden.py` and reading the ship-gate line of a
report, and neither one checked a regression. **D22** is why the second half is a *floor*: a
guard against the capability walle started with, never against the previous run, or a series
of individually-acceptable drops walks it downhill. D22 also gives the form ("express limits
declaratively: `keep <metric> <= +1%`, with threshold abort"), and the floor file uses that
form. The rebuild half is hard constraint 5 (destroy derived state, reconstruct from the log)
and D88 (golden logs per shipped version), run here rather than only inside pytest. Hard
constraint 3 holds: nothing on the resolution path imports the gate, and the gate itself calls
no model, because only the fake runs. Hard constraint 7 holds, because nothing new is imported
(`fractions` is the standard library).

**Verify:** *a planted eval regression fails the gate.*
- `test_a_planted_proposer_regression_fails_the_gate_while_the_ship_gate_still_passes`: with
  the fake model's cues emptied, the proposer goes quiet. It refuses nothing and leaks
  nothing, so the D77 ship gate still **passes** (asserted). The gate **fails**, on exactly
  `floor benign.proposed`. This is the regression the gate exists for: the ship gate cannot
  see it.
- `test_a_planted_recall_regression_fails_the_gate`: `recall` finds nothing, in the capability
  leg only. The gate fails on exactly the two `hit_at_5` floors.
- `test_a_planted_false_allow_fails_the_gate`: the hazard filter is blinded. The ship gate and
  both false-allow floors fail.
- `test_a_planted_regression_fails_the_command_with_exit_1_and_names_the_metric`: the same
  plant through `walle gate`, exit 1, `FAIL floor benign.proposed`.
- `test_a_floor_raised_above_what_the_code_does_fails_the_gate`, and the rebuild plants:
  `test_a_golden_fixture_whose_rebuild_moves_fails_the_gate`,
  `test_a_rebuild_that_disagrees_with_the_log_fails_the_eval_log_check` (wrong count) and
  `test_a_rebuild_with_the_right_count_and_the_wrong_state_fails_the_eval_log_check`.
- *D22, a floor not a delta:* `test_a_relative_limit_is_measured_from_the_floor_not_from_the_last_run`.
  0.5% down passes once, and 2% below that last run fails, because the bound is fixed by the
  floor.
- *The floor is honest:* `test_the_shipped_floor_is_what_the_code_measures_today` and
  `test_every_metric_the_gate_measures_has_a_rule_in_the_shipped_floor`.
- *Dormant, as the owner asked:* `test_the_gate_is_off_by_default`,
  `test_only_the_exact_word_turns_the_gate_on` (eight wrong spellings),
  `test_while_off_the_gate_reads_nothing_and_builds_nothing` (the floor loader, the rebuild
  and the eval are never called), and `test_while_off_the_command_exits_2_and_writes_nothing`
  (the repo copy's bytes are unchanged).
- *Acts on no real data:* `test_the_gate_writes_nothing_in_the_repo_it_reads` and
  `test_the_gate_never_writes_into_a_golden_fixture`. The gate takes no walle root, only a
  checkout, and it reads only that checkout's synthetic fixtures.

Tests first: before `gate.py` existed, `tests/test_gate.py` failed at collection
(`ImportError: cannot import name 'gate'`). Then I mutated `gate.py` six ways: the switch on
by default, a floor check that always passes, a relative bound that ignores its limit, a
fixture hash that is not compared, an eval-log state that is not compared, and a result that
always passes. All six now fail at least one test. The eval-log one survived at first, because
the only plant dropped a record and the count check caught it first. So I added the
right-count, wrong-state test, which kills it.

```
uv run pytest               2892 passed, 30 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  163 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 30 skips are the same as on `main` (container escape tests and others that need a docker
client). The suite ran as an unprivileged user (uid 1000), and nothing here assumes root.
`walle gate` on the tree with the switch on prints twelve PASS lines and `GATE: PASS` in about
3 seconds. With the switch off it prints that the gate is off and nothing was checked, and
exits 2.

**Two-runner walkthrough:** the gate reads the repo and writes only under its own
`tempfile.TemporaryDirectory`. Suppose gates A and B run at once. Each copies the fixtures into
its own scratch folder and builds the eval logs there, so they share no log, no index and no
lock. Both read `floor.txt` once, before anything is built, so neither run mixes two versions
of the rules. If the owner edits the floor during a run, that run keeps the rules it read, and
the next run reads the new ones. Within one run, `index.rebuild` takes the principal's
`index.lock` as it always does. Nothing else writes that scratch log while it runs, so the
JSONL read and the rebuild see the same bytes.

**Stranger pass:** done on the whole diff. It found two things, and both are fixed:
- A fixture whose `__expected__.json` lacked `events` or `state_hash` was read *after* the
  `try`, so the gate would have crashed instead of failing that check. All three keys are now
  read inside it. `test_a_fixture_missing_an_expectation_fails_its_check_and_does_not_crash`
  covers it.
- A principal folder with an empty log would have *passed* `rebuild eval logs`, since zero
  events equals zero events, with nothing checked. It now fails with "an empty log, so nothing
  was checked". `test_an_eval_log_with_no_events_fails_rather_than_passing_unchecked`.

Looked for and not found: a status line that lies ("cannot start, nothing was checked" is
printed only for errors raised before the scratch folder exists, which is why input loading
was moved ahead of the build). I also checked for an empty result that reads as a pass
(`GateResult.passed` needs at least one check, and no golden fixture at all is a failure),
and for a temp folder left behind on an exception (the context manager removes it). In
`_fixture`, `except Exception` is deliberate: any failure to rebuild a fixture is that
check's failure, reported with its type, and never ignored.

**Review:** independent review is the reviewer agent's job, on this PR, per the brief. I did not
run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **A new command, not a flag on `walle eval`.** `eval` writes a dated report and always runs.
  The gate writes nothing and is off by default. Combining them would make the dormant rule
  apply to `eval` too.
- **The floor file lives in `reports/`**, next to the measurements it came from, as plain text
  in D22's `keep` form rather than JSON. The point of D22's declarative form is that a person
  reads it.
- **Shares, not counts.** `benign.proposed` is 19/30, not 19, so adding benign items is not
  automatically a regression. A new benign item that the proposer misses still lowers the
  share and fails the gate. That is the floor doing its job, and the owner decides.
- **`-1%` is relative to the floor.** For hit@5 that means at most 0.99. With five questions,
  one miss is 20%, so in practice the rule is "no drop" until the datasets grow.
- **Only the fake model.** A real model's numbers vary from run to run, and a floor for one
  needs its own baseline and a tolerance chosen from repeated runs. See below.
- **The eval logs are rebuilt as well as the golden fixtures.** The fixtures are frozen. Fresh
  logs from the current writer catch a rebuild bug on shapes no fixture holds yet.

**What to read first:** the module docstring of `gate.py`, then `parse_floor` and `Rule.bound`
(D22 in about 40 lines), then `reports/floor.txt`. Then
`test_a_planted_proposer_regression_fails_the_gate_while_the_ship_gate_still_passes`, which
is the verify criterion and the reason the gate adds anything to the ship gate.

**Noticed, not done:**
- **CI does not run `walle gate`.** It is dormant by the owner's condition, and the workflow is
  under CODEOWNERS. The tests do run it in CI with the switch on, inside pytest. Wiring it in
  as its own job is the owner's call.
- **Per-model floors.** `floor.txt` is the fake model's. A real model's gate needs a floor per
  model and a tolerance from repeated runs.
- **Nothing stops the floor from being lowered in a diff.** D22 is about drift through many
  small steps, and a lowered floor is one visible step. A check that the floor only rises (for
  example against the first commit of `floor.txt`) would make lowering it an explicit decision.
- **Other metrics have no floor yet:** the per-ability hit@5 split, QA accuracy (not run
  without keys), and the hazard-kind counts. Today only overall numbers are guarded.
- **Three golden fixtures share one state hash** (`budget`, `chores`, `inbox` all print
  `38a3055e0fb8`). Their `__expected__.json` folds are empty (no current, retired, conflicting,
  unapplied or staged item; their events are observations and other non-belief types), so the
  hash is the empty state's and adds little for them. The sentinels in `test_golden.py` carry
  the weight there, and the gate does not check sentinels.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
