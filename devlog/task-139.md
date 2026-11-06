# Task 139: Audit — evals must not pass on nothing, and errors must not leak

Issue #139: read-only audit findings 4, 6 and 7 (Gemini 4 Argon at `9bd13c0`). Branch
`task/audit-evals`, from `main` at `fe60cb3`, created empty by the owner as the claim. Author:
walle-author-fable (Fable 5.1). Every finding was written as a test first and run against
`fe60cb3` before any fix. Two cases did **not** fail as the auditor described, and are said
so below rather than fixed around: an empty corpus *file* was already refused by the reader
before anything ran (exit 2, `no items`), and a model that errors on *every* reviewer prompt
already failed the gate, through the benign refusals. The gap was narrower and real: a model
that errors on exactly the hostile items only a model can judge got PASS. No D-number changes,
so `flag` was not used. Only fake and scripted models were run; no key, no network.

**Shipped:**

- `src/walle/evals/models.py`: the duplicate `ProviderModel` is gone. The module keeps the
  `Completer` protocol and re-exports `ProviderModel`, `ModelUnavailable` and `resolve` from
  `walle.models`, the adapter with a timeout, no retries, and scrubbed errors raised
  `from None`. `walle eval --model provider:model` now runs through it. Its docstring also
  said aisuite was not a dependency yet; it has been one since `pyproject.toml` gained
  `aisuite[ollama]`, so that is gone too (finding 6, HIGH).
- `src/walle/models.py`: `_redact` is now `redact(text, *urls)`, public, and masks the
  `user:password@` of any URL given to it as well as every secret-named environment value.
  `_ensure_client` passes `self._base_url` to it, and catches `Exception` after the three
  named classes: an SDK's own error type is a `ModelUnavailable` named by class, text masked,
  `from None` (finding 6, LOW).
- `src/walle/evals/reviewer.py`: `Outcome.unjudged`, true for a dangerous or injection item
  with `model_judges` and no verdict; `Result.unjudged`; `Result.gate` fails on every unjudged
  item by id and on every corpus with no items. `render` has an `unjudged` column and a line
  per reason, and the fake-model disclaimer only when `result.model == "fake"`, as
  `evals/report.py` already had it (finding 7, HIGH and MEDIUM).
- `src/walle/evals/boundary.py`: `Gate` gains `benign_proposed` and `missing` (the
  `corpus/leg` pairs with no outcome); `passed` needs at least one benign item proposed from
  and nothing missing. `IncompleteRun` is raised by `run_proposer` when `propose` reports a
  `shadow_error`: the proposals stand in the scratch log, the verdict reasons were never
  judged, and scoring them as clean would be the pass the issue names. `_check_recall` checks
  the whole transcript, defaced, not its first line (finding 7, HIGH, MEDIUM and LOW).
- `src/walle/evals/report.py`: the gate paragraph says how many benign items were proposed
  from and adds a bold paragraph when that is zero or when part of the eval did not run; the
  command catches `ModelCallError` and `IncompleteRun` beside `ModelReplyError`, exits 1 and
  writes no report, in one line with no traceback. `evals/gate.py`'s `ship gate` check says
  the same numbers, so a failing ship gate says why.
- `src/walle/acp.py`: `HostError.message` goes through `_said`: redacted with
  `walle.models.redact`, folded to one line, cut at `ERROR_CHARS` (400). Redacted first, so a
  secret the cut would split is masked whole. `_initialize` names an unexpected
  `protocolVersion` by type unless it is an integer. `session/update` is kept on a turn only
  when its `sessionId` is the one the request named. `memory` reads the manifest it used to
  drop and asks `hot.age` whether the render is the log's fold now: `Status.STALE` and
  `Status.UNCHECKED` carry the tier behind the same note the MCP Resource writes (finding 4,
  MEDIUM and LOW).
- `src/walle/hot.py`: the age check moved here from `hot_resource` as `Age`, `Aged`,
  `age()`, `STALE_HEAD` and `UNCHECKED_HEAD`, so a module that may not import the MCP server
  (`acp`) can use it. `hot_resource.read` calls it and re-exports the heads; its behaviour and
  its tests are unchanged.
- `reports/2026-11-06-fake.md`, new, and a bullet in `reports/README.md`: the gate paragraph's
  text changed, so the checked-in report was regenerated with `walle eval --date 2026-11-06`.
  The numbers are the 2026-11-05 numbers. It still proves only the harness. `floor.txt` is
  untouched: the floors are what walle started with, and nothing measured moved.
- Tests, 18 new: `test_evals.py` (gate over nothing; a corpus missing at one leg; a mute
  model; a shadow failure; a model that cannot be called, through the CLI; a recall cut after
  line one; one `ProviderModel`, shown by identity and by a client that raises quoting a
  key), `test_reviewer.py` (gate over nothing; a model that fails only on the judged items,
  three ways; the disclaimer), `test_models.py` (any constructor exception, and a URL with
  userinfo), `test_acp.py` (a verbose host error through the fake host; the same without a
  host; an odd version; an update from another session; a stale tier; an unchecked tier),
  `test_gate.py` (a proposer gone silent). `tests/fake_acp.py` gains three modes:
  `error-verbose`, `odd-version`, `other-session`.
- Tests edited because the task requires it, said here:
  - `test_evals.py::test_the_gate_counts_a_false_allow_and_the_report_says_fail`: its
    "passing" list had two outcomes, benign and dangerous at the proposer leg only. A gate
    with four `corpus/leg` pairs missing can no longer pass, so the list now holds every
    corpus at every leg. The false-allow half of the test is unchanged.
  - `test_gate.py::test_a_planted_proposer_regression_fails_the_gate_while_the_ship_gate_still_passes`:
    it emptied the fake's cues entirely, which is now a ship-gate failure too. It now keeps one
    cue, so the proposer proposes from some benign items and fewer than the floor, which is
    the regression the floor exists for and the ship gate still cannot see. The fully silent
    case is the new test beside it, asserting both checks fail.

**Why:**

- **D77.** The benign row of D77's table is "the proposer proposes something; nothing is
  refused". The gate had only the second half. A model that answers `[]` to everything
  satisfies the second half and says nothing about the system; so does a run over no items.
  The ship gate is the literal line D77 asks for, and a literal PASS on nothing is the thing
  that must not be written. `floor.txt` already had `benign.proposed` as a floor (D22), but
  the gate is off unless `WALLE_GATE=on`, and `walle eval` on its own must not lie.
- **D22.** Nothing here lowers a floor. The new checks are absolute (nothing missing, more
  than zero), beneath every floor, so a floor that passes still implies them.
- **D87.** aisuite is the provider layer and `walle.models` is the one place walle talks to
  it. A second adapter beside it, with the SDK's default retries on a 429 and the raw error on
  a 401, was the leak and the retry the module docstring says walle does not do.
- **D16.** The local tier is `ollama:` through the same adapter; a long first-token wait is
  why the timeout exists, and `walle eval` now has it.
- **D55.** A memory that comes back is one envelope, whole. Checking the first line let a
  recall that truncated a multi-line belief count as whole.

**Verify:** the issue's criterion: a failing test for each finding first; a refusal writes
nothing; the class is fixed.

Before, the new tests against `src/` at `fe60cb3`, each run and read:
- `boundary.gate(())` passed; `report.evaluate(root, {}, fake).boundary.gate.passed` was
  True; a mute model got `Gate(false_allows={..0}, benign_refused=0)` and PASS; a model whose
  judging raised `ModelCallError` got the same PASS; a model that raised on every call made
  the CLI exit 1 with a traceback (`result.exception` was the `ModelCallError`); a recall cut
  after line one was `enveloped` on every multi-line benign item; `models.ProviderModel is
  walle.models.ProviderModel` was False and the raising client's `RuntimeError` came through
  with the key in it.
- `reviewer_eval.run({}, fake).gate` was `Gate(passed=True, reasons=())`; the selective model
  got `refused_benign == 0`, `false_allows == 0`, four items with `verdict None`, and
  `Outcome` had no `unjudged`; the real-model render carried "A fake-model PASS proves".
- `ProviderFactory.create_provider` raising a class of its own came through as that class
  with the key and the URL's password in its text.
- The fake host's verbose error reached `str(HostError)` with its newlines, 5 KB and the
  `FAKE_ACP_TOKEN` value; the odd version reached `ProtocolError` at 5,133 characters; the
  stray session's chunk was in `turn.text`; `acp.Status` had no `STALE`.
- Did **not** fail, kept as regression tests and said here: `walle eval` on three empty
  corpus files exits 2 with `no items` before anything runs (`corpus.load`); a reviewer model
  that errors on *every* prompt fails the gate through ten benign refusals.

After:

```
uv run pytest                4338 passed, 35 skipped
uv run ruff check            All checks passed!
uv run ruff format --check   215 files already formatted
uv run pyright               0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the one new lock path is `acp.memory`, which now folds the log
after reading the tier. Client C and renderer R for one principal. (1) R holds `render.lock`
exclusively; C's non-blocking shared request fails at once, `busy`, as before. (2) R releases;
C takes the shared lock, reads the four files and the manifest under it, releases, and only
then calls `hot.age`, which takes `index.lock` to bring the index up to date. `render.lock` is
never held while `index.lock` is taken, so R's order (`render.lock`, then `index.lock`) cannot
meet its reverse; this is `hot_resource.read`'s order, unchanged by the move. (3) A render Y
lands between C's read and its fold: C says `stale` of the text it read, which is true of that
text, and the session carries the older render with the note in front. An append after C's
fold: C says current one event early; newer is the right way round. The eval harness writes
only under a scratch root it owns; `IncompleteRun` is raised after `propose` has returned, so
no lock is held when it is raised and the scratch log is left as `propose` left it.

**Stranger pass:** the diff is about 500 lines of `src/`, touches error paths and a lock
path, so the re-read was done.
- Found and fixed: `_said` first folded and cut, then redacted. A secret straddling the cut
  would have survived in half. It now redacts the raw text first.
- Found and fixed: `acp`'s two-runner walkthrough said "nothing here takes `index.lock`",
  which `memory` now does. The docstring says the new order and why it is safe.
- Checked: `hot.age` catches `Exception` around the fold, as `hot_resource` did, so
  `acp.memory` still never raises for the state of the log; a `ValueError` for an unsafe
  principal name is raised by `hook.snapshot` before any of this, as before.
- Checked: the status messages. `IncompleteRun`'s text is `ProposeReport.shadow_error`, the
  same text `walle propose` prints to stderr today: a `ModelCallError`'s scrubbed words, or a
  class name and message for an `OSError` or a pydantic `ValueError`. Not a new surface.
- Checked: `Outcome.unjudged` is also true for a `model_judges` item that a detector caught
  before the model (`model_asked False`). That is deliberate and the reason says "the model
  was not asked": the corpus claimed nothing structural catches it, so one of the two is
  wrong and the gate should say so. The fake run asks every one of them, so it passes.
- Checked: `ProtocolError` with a `bool` version prints `True`; harmless, and still no host
  text.
- Considered and left: a `session/update` with no `sessionId` during a prompt is dropped. The
  schema requires the field, and a host that omits it cannot be matched to a turn.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model and sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run (R6
replaces it for agent work). There was no author-as-reviewer pass beyond the stranger pass.

**Choices made inside the task:**

- **A shadow failure raises rather than records a gate failure.** The issue allowed either.
  `walle eval` already writes no report on a `ModelReplyError` while proposing (D86: half a
  run has no number); a judging failure after the proposals are durable is the same half run
  one step later, so it takes the same exit. A report with a FAIL and no verdict numbers would
  have been a record of nothing.
- **`benign_proposed >= 1` in the gate, not the floor's 19/30.** The gate is D77's literal
  line and runs on any corpora folder with any model; the floor is D22's and belongs to
  `walle gate` with `WALLE_GATE=on`. Putting the floor's number into the gate would have made
  a run over a different corpus fail for the wrong reason.
- **The age check moved to `hot.py`, not duplicated and not imported from `hot_resource`.**
  `acp` must not import the MCP server (its own test holds that), and two copies of the note
  would drift, which is what the issue's finding was about. `hot` already owns the manifest
  and its `state`, so the check of that state sits beside the writer of it.
- **`redact` became public rather than `acp` importing a private name.** It is one rule used
  from two modules now, and the module docstring says so.
- **`ERROR_CHARS = 400`.** A JSON-RPC error message is a sentence; 400 holds any honest one
  on one terminal line or two and cuts a dump.
- **A new dated report, not an edit of 2026-11-05.** `reports/README.md` says a report is
  written whole by `walle eval` and never edited by hand, and the date in the name is the day
  the code produced it.

**What to read first:** `evals/boundary.py`, `Gate.passed` and the `shadow_error` check in
`run_proposer`; then `evals/reviewer.py`, `Outcome.unjudged`, and the selective model in
`test_reviewer.py`, which is the realistic failure: a provider's content filter refusing
exactly the prompts that matter.

**Noticed, not done:**

- `evals/reviewer.py` has no checked-in report and no CLI; its gate is exercised only by
  tests. A dated report under `reports/` for the reviewer corpora would make a regression
  visible the way the boundary one is.
- `ProposeReport.shadow_error` carries `str(exc)` for `OSError` and pydantic `ValueError`,
  which can quote a path or a reply field. `walle propose` prints it to stderr today. A kind-
  only form would match what the rest of the module does with model text.
- `fake_acp.py`'s `error-verbose` mode reads `FAKE_ACP_TOKEN` from its environment to play a
  careless host; the test sets it. No real value is involved.

**Cost:** not read from Hyperagent during this run; author only, one thread.
