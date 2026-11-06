# reports

Dated, per-model eval reports (D77, D98), one file per model per date:
`<YYYY-MM-DD>-<model>.md`. Written whole by `walle eval`; do not edit one by hand. A report
that says `SHIP GATE: FAIL` is kept too: it is the record a regression needs.

Each report has the boundary numbers per corpus and leg (proposer, envelope), every failing
item by id and kind, never by text, and a capability section where LongMemEval and LoCoMo
get a number only if they ran. `tests/test_evals.py` fails if the latest fake report is not
what the current code produces.

- `2026-11-04-fake.md`: the fake model, offline. Gate PASS. No capability run. **A fake-model
  PASS proves only that the harness works**; it says nothing about a real model, and the
  detectors' known gaps are open in #46.
- `2026-11-05-fake.md`: the fake model again, written twice that day. First after issue #59 (a
  passphrase with its words joined by hyphens, dots or underscores), when three dangerous items
  and one benign near-miss were added. Then regenerated after issue #74 (workflow proposals):
  the proposer leg now runs with `WALLE_WORKFLOW_PROPOSALS=on`, twelve workflow-aimed injection
  items and three benign chores were added, and the benign chores are proposed as workflows.
  Gate PASS, zero false-allows; the same caveat applies. The first version is in git history.
- `2026-11-06-fake.md`: the fake model, written twice that day. First after issue #137 (forged
  turns through the proposer's excerpts). The benign `tool-result` items are now laid out as
  every source writes a turn, one `user:` block then the tool's text and the reply, so the
  person's "remember" cue sits in their own block and two more benign items are proposed from:
  21 of 30, up from 19. The floor in `floor.txt` is raised to match. Then regenerated after
  issue #139 (a gate that did not run must fail), with the same numbers: the gate line now also
  says how many benign items were proposed from, because zero is a FAIL since that issue: a
  model that answers nothing to everything refuses nothing and proves nothing. A report also
  names any corpus or leg with no outcome, and `walle eval` writes no report when the model
  gave no reply or the proposer's shadow judging failed. Gate PASS, zero false-allows; the same
  caveat applies.

`floor.txt` is not a report. It is the regression floor `walle gate` holds the fake eval to
(D22, #75): one `keep` rule per metric, measured against what walle started with, never the
last run. The gate reads it and never writes it. Raising a floor is a reviewed change, and
lowering one is a decision. The gate is off unless `WALLE_GATE=on`.

A real model's report needs its key: `uv run --with 'aisuite[<provider>]' walle eval --model
<provider>:<model> [--longmemeval FILE] [--locomo FILE]`.
