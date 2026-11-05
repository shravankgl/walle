# Task 55: Curator, propose-only

Issue #55. Branch `task/p1-curator`, from `main` at `a2965a8` (the model route, #52/#62).

**Shipped:**

- `src/walle/curator.py` (new): the Curator in propose-only mode. `curate(root, principal,
  device, model)` brings the index up to date, folds the log with the resolver, and shows the
  **current assertions** to a model. From the reply it writes exactly two kinds of event:
  - a `proposal` into the staging queue for each **consolidation** (two or more current
    assertions that say one thing, fused into one belief), `actor=curator`, `caused_by` naming
    every source. Same queue as `walle remember` and the proposer; `walle review` decides it.
  - a `contradiction` for each pair of current assertions that cannot both hold, `caused_by`
    naming both, with the model's one-line note (D58).
  `FakeCurator` is the deterministic, offline fake (`--model fake`). `resolve_model(name)`
  gives the fake, or `provider:model` through `walle.models`, after `refuse_local` has refused
  the local tier. `_append` is the one place an event is written, and it refuses any type not
  in `WRITES = {proposal, contradiction}`.
- `src/walle/cli.py`: `walle curate --principal --device --model`. `--model` is checked for
  shape (the proposer's rule) and then for the local tier: `ollama:…` is a usage error, exit 2,
  naming D24. Prints what it staged and recorded, what it withheld (by ref and kind), what it
  refused; a run with nothing to say prints nothing (D63).
- `src/walle/proposer.py`: `Request.kind` gains `"curate"`. Two lines. The Curator reuses the
  proposer's `Request`, `envelope`, `excerpts`, `_marked`, `_parse_list` and `_read_other`
  rather than copying them, so the hazard and envelope path is the same code, not similar code.
- `tests/fixtures/curator/d-mbp.jsonl` (new): nine synthetic events written by this build's
  `StreamWriter`. Two current assertions that contradict (dates day first / month first), two
  that say one thing (Thursday coaching), one carrying an invented code shaped
  `FAKE-SHEDCODE-NOT-REAL-0042`, one retired by a supersede that would otherwise contradict the
  Thursday rule, one lone belief, one observation. Made-up person, made-up allotment.
- `tests/test_curator.py` (new): 28 tests, the verify criterion and the cases around it.

**Why:**

- D29: fusion is the Curator's job, and it happens with maximum context, over the whole set of
  current beliefs. In propose-only mode the fusion is a *proposal*, not a snapshot: the person
  keeps or drops it, and nothing it fuses is retired by the Curator.
- D58: `contradiction` is an event, `caused_by: [seqA, seqB]`, written by the Curator, resolved
  or not by a later `supersede`. The resolver already folds it (rule 5: an open conflict when
  neither side carries world time, which is what the fixture shows; rule 4: closes the earlier
  window when both do, D93). The Curator records; it never resolves.
- D24: the Curator is frontier-class and pushed toward diversity. `DIVERSITY_PROMPT` is in
  every request's instructions, before any data. The local tier (`ollama:`, D16 and D87's
  spelling) is refused in two places: `curate` itself, before the log is opened, so a model
  object built around a local name is refused however it got there; and the command's
  `--model` callback, as a usage error.
- D49, D60, D86: nothing reaches the hot tier without a human yes. The Curator's only write
  that touches belief is the queue, and the queue is not belief.
- D55, D77, through the proposer's code: every assertion is flagged whole by `hazards.flag`
  before any cut and withheld by ref and kind; what is sent is the head of one excerpt inside
  an envelope with a marker random per request, anything marker-shaped defaced first; the
  reply is data, and a consolidation carrying a hazard or a marker is refused, a note carrying
  one is replaced by `NOTE_WITHHELD`.
- D63: a second run is silent. A set a proposal already names (live, kept or dropped) and a
  pair a contradiction already names (live or superseded) are passed over before the model's
  reply is read, so nothing is written twice and nothing is said about it.

**Verify:** issue #55's criterion, in `tests/test_curator.py`:

- *A fixture with a planted contradiction is found:*
  `test_the_planted_contradiction_is_found_and_recorded_once`. One `contradiction`, actor
  `curator`, `caused_by` = {`d-mbp:2`, `d-mbp:3`}, and the resolver reports exactly that pair in
  `conflicts`.
- *Every output is staged, never kept:* `test_every_output_is_staged_and_nothing_is_kept`.
  The new events are exactly the report's proposals and contradictions, all `actor=curator`;
  the log's assertions, snapshots (none) and supersedes are unchanged; every proposal is in
  `staged()`; the resolved `current` refs and assertions are identical before and after.
- *The diversity prompt is present:* `test_the_diversity_prompt_is_in_every_request`, in the
  instructions and in every request's prompt, before the first item.
- *A local model string is refused:* `test_a_local_model_is_refused_before_anything_is_read`
  (three spellings including uppercase, through `curate` and `resolve_model`; the stream's
  bytes are unchanged and no index was built) and
  `test_the_command_refuses_a_local_model_as_a_usage_error` (exit 2, D24 in the message).
- *All text goes through the same hazard checks as the proposer:*
  `test_assertions_go_through_the_proposers_hazard_checks` (a credential and a planted
  instruction are withheld by ref and kind, never appear in any prompt, every item sits in
  this request's envelope and defaces to itself) and
  `test_a_reply_goes_through_the_same_checks_as_a_proposers_reply` (`hazard-in-text`,
  `marker-in-text`, `oversize`; a note with a phone number is withheld).
- *No code path writes an assertion:*
  `test_the_module_can_build_no_event_but_a_proposal_or_a_contradiction` reads the module's
  source: no `Assertion`, `Snapshot` or `Supersede` is called or imported, nothing from
  `walle.staging` but the three text cleaners, `WRITES` is exactly the two.
  `test_a_reply_that_asks_for_anything_else_is_refused` (a reply of kind `assertion`, `keep`,
  `snapshot`, `supersede`: all `malformed`, log unchanged) and
  `test_appending_anything_else_is_refused_by_the_writer_guard` (`_append` refuses an
  `Assertion` under the lock).

Tests first: before `walle.curator` existed, `tests/test_curator.py` failed at collection
(`ImportError: cannot import name 'curator' from 'walle'`). Mutation checks after, each run
against the finished tests and each caught by a named test: removing the diversity prompt
from the instructions; removing `refuse_local` from `curate`; dropping the input hazard check;
dropping the reply hazard check; dropping the note hazard check; removing the `_append` type
guard; removing the grounding check (`refs <= shown`); removing the re-read under the lock;
showing retired assertions as well as current ones.

Nothing depends on file permissions or on who runs the tests; this run was uid 1000.

Check output, on the commit's tree:

```
uv run pytest               1391 passed, 10 skipped in 46.79s
uv run ruff check           All checks passed!
uv run ruff format --check  96 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** it touches the log. Runs A and B for one principal, both starting
with the pair (X, Y) unnamed and the fusion of (U, V) unproposed.

1. Both bring the index up to date (`index.update`, its own lock, shared for a moment) and
   both fold the same current assertions.
2. Both call the model. No lock is held: a model call can take seconds.
3. A takes its stream's exclusive lock (`StreamWriter.locked`), reads every stream again
   (`_named_now`: this one through the locked handle, the others with the proposer's
   `_read_other`, a shared lock that does not wait), finds (X, Y) unnamed and {U, V}
   unproposed, appends a contradiction and a proposal, `fsync`s, releases.
4. B takes its lock, reads every stream again, finds A's contradiction naming {X, Y} and A's
   proposal naming {U, V}, appends nothing, and reports `already = 2`.

`test_a_pair_named_meanwhile_is_not_written_twice` plays B's part with A as a run on another
device, started from inside the model call: one contradiction and one proposal in the log,
`already == 2`. When the shared lock on another device's stream is refused, the file is read
as it is and D80's reader passes over a torn tail, so the most that can be missed is the
record being appended at that moment: two devices writing before their logs meet (D57) can
each name a pair once, and the resolver treats two contradictions about one pair as one.

**Stranger pass:** the diff is over 30 lines and touches the log, a lock and error paths.

- *Nagging.* The first `_named` passed over a *superseded* contradiction, so a pair the person
  had undone ("both are true") would be named again on every run. That is the agent D30 says
  people switch off. Now a pair once named, live or not, is never named again, like a dropped
  proposal. `test_an_undone_contradiction_is_not_named_again` covers it.
- *Ref order.* `caused_by` was sorted lexically, which puts `d-mbp:10` before `d-mbp:2`. Now
  sorted by `parse_ref`, (stream, seq), the order the rest of the log uses.
- *Possibly-unbound variables.* The first `_findings` carried `summary` and `body` across an
  `if/elif` and pyright said so. Split into `_fusion` and `_pair`, each returning a whole
  finding or raising `ValueError`, and `_findings` only grounds and filters.
- *Ignored errors.* `model.complete` is not wrapped: `ModelCallError` and `ModelReplyError`
  propagate before the lock is taken, so nothing is written (tests: HTTP 503, non-JSON, a JSON
  object instead of a list). One malformed item in a list drops that item, not the list.
- *Lock scope.* The stream lock is held for the re-read and the appends only. No model call,
  no index update under it.
- *Leaks.* `Withheld` carries the ref and the kinds, never the text; the command prints kinds.
  A note is cut to one line of 500 after the hazard check, not before, so a hazard past
  character 500 is still a hazard.
- *Status messages.* "recorded 1 contradiction" is followed by "recorded, not resolved", so
  nobody reads it as the Curator having decided. A run that only refused or only found
  `already` says "nothing written this run" rather than nothing.
- *What the check does not cover.* If `_append` failed on the second of several appends, the
  first stands (it is durable) and the exception carries no report of it. The proposer has
  the same shape. The next run finds the first in `_named` and does not repeat it.

**Review:** none yet. This is the author's log. The reviewer agent (a different vendor's
model) reviews the PR head and sets `agent-review`. The author does not set it.

**Choices made inside the task:**

- **A consolidation is a proposal, not a snapshot.** The issue says every output is staged.
  A `snapshot` cannot be staged, and a propose-only Curator is not yet trusted to forget. So
  the fused belief goes to the queue with `caused_by` naming its sources, and D29/D89's digest
  stays with `walle.consolidation`'s gate and plan for a later, trusted mode.
- **A contradiction is written, not staged.** The issue says the Curator *records*
  contradiction events and D58 says the Curator writes them. It is a record, not a belief:
  it adds nothing to `current`, and the fold applies it only while live, so `walle forget
  <ref>` undoes it entirely. The one effect it can have on a belief is D93's: when both sides
  carry world time and one starts later, the earlier window closes. The fixture's pair has no
  world time, so it is an open conflict (rule 5).
- **Current assertions only.** Not observations, not staged proposals, not retired beliefs.
  The fixture's `d-mbp:7` would contradict the Thursday rule if read; it is retired and is not
  shown (`test_an_observation_is_never_read_as_an_assertion`).
- **`level` of a fusion.** From the reply when given, else the weakest of its sources
  (explicit < deductive < inductive, D94). `valid_from`/`valid_to` are left null: world time
  is not guessed (D103).
- **Grounding within one request.** A finding must cite items shown in the *same* request,
  the proposer's rule. So a pair split across two requests (more than `REQUEST_CHARS` of
  beliefs) is never found. See *Noticed*.
- **`refuse_local` by provider name, case-insensitive.** `LOCAL_PROVIDERS = {"ollama"}` is
  D16/D87's local tier. Another local provider would be added there, which is a decision.
- **`Request.kind` extended rather than a Curator `Request` of its own.** `ProviderModel.
  complete` takes a `proposer.Request`; a second type would mean a second adapter.
- **Two reads of the index.** `_named` on the records read at the start (so a known set is
  passed over before the model is asked) and `_named_now` under the lock (so a set written
  meanwhile is passed over too). The first is for silence, the second for correctness.

**What to read first:** `_findings` and `_append` in `src/walle/curator.py`: what a reply is
allowed to become, and the one gate every write goes through. Then
`test_every_output_is_staged_and_nothing_is_kept`.

**Noticed, not done:**

- A kept consolidation does not retire the beliefs it fused: after `walle review` keeps
  `d-mbp:11`, `d-mbp:4`, `d-mbp:5` and the new assertion are all current, and the hot tier
  says the Thursday rule three ways. The review needs a way to say "keep this, and supersede
  these", or a trusted Curator mode writes the supersedes. Either is a decision.
- A pair of beliefs more than `REQUEST_CHARS` apart is never compared. A profile with many
  beliefs needs either a bigger request or a second pass that pairs across requests.
- The Curator does not use D95's gate (`walle.consolidation.gate`) for its cadence; `walle
  curate` runs when asked. Wiring the gate in is a small follow-up once the trusted mode exists.
- A real model's reply wrapped in a ```json fence is "not JSON" and writes nothing, the same as
  the proposer (devlog/task-52.md).
- PR #48 (eval corpora and the dated report) is still open. This task changes neither the
  corpora nor the detectors, so no eval report would need regenerating; if a later change to
  `hazards.py` lands after #48, the checked-in report must be regenerated with it.
- `walle.models.resolve` still returns the proposer's `FakeModel` for `fake`; the Curator has
  its own `resolve_model`. One `resolve(name, kind)` would be tidier.

**Cost:** not visible to the author from inside the run. To be filled from Hyperagent.
