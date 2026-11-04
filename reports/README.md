# reports

Dated, per-model eval reports (D77, D98), one file per model per date:
`<YYYY-MM-DD>-<model>.md`. Written whole by `walle eval`; do not edit one by hand. A report
that says `SHIP GATE: FAIL` is kept too: it is the record a regression needs.

Each report has the boundary numbers per corpus and leg (proposer, envelope), every failing
item by id and kind, never by text, and a capability section where LongMemEval and LoCoMo
get a number only if they ran. `tests/test_evals.py` fails if the latest fake report is not
what the current code produces.

- `2026-11-04-fake.md`: the fake model, offline. Gate PASS. No capability run.

A real model's report needs its key: `uv run --with 'aisuite[<provider>]' walle eval --model
<provider>:<model> [--longmemeval FILE] [--locomo FILE]`.
