# Task 9: walle remember and walle review

Issue #7. Branch `task/09`.

**Shipped:**
- `src/walle/events.py`: a new event type, `proposal`. It is one item in the staging queue: a
  belief waiting for a human yes. It has `body`, `summary` (1 to 120 characters, written when
  the item is proposed), `level`, and an optional `valid_from` / `valid_to`. It is registered
  in `AnyEvent` and `EVENT_TYPES` at `v` 1.
- `src/walle/resolver.py`: two new pure functions beside `resolve`.
  - `staged(records)` is the staging queue as a fold. A proposal is staged while it is live
    (rule 1, so a drop is a `supersede`) and no `assertion` names it in `caused_by`.
  - `retired_by(records, ref)` returns the live supersedes that target any event. A review
    uses it to say which tombstone dropped a proposal.
  - `resolve` itself is unchanged. It only ever folds `assertion` events into `current`, so a
    proposal cannot get there.
- `src/walle/staging.py` (new) holds the IO side.
  - `remember` appends one `proposal`.
  - `pending` updates the index and returns `staged(...)`.
  - `keep`, `edit` and `drop` each check, under `staging.lock`, that the item is still staged
    and then append one event. Keep and edit append an `assertion` with `caused_by: [proposal]`.
    Drop appends a `supersede` targeting the proposal, with reason `dropped in review`.
  - `summarise` is the default summary rule. `NotStagedError` is raised for a ref that is not
    a staged proposal, before anything is written.
- `src/walle/paths.py`: `staging_lock_path`, which is `principals/p-<name>/staging.lock`.
- `src/walle/cli.py`: two commands.
  - `walle remember --principal P --device D [--summary S] TEXT` stages the text and prints
    its ref, with "Not in the hot tier until you keep it".
  - `walle review --principal P --device D` is the batch (D49). It shows each staged item and
    asks keep / edit / drop / skip / quit. Each answer is written as soon as it is given. The
    command ends with a count line and a pointer to `walle render`.
- `tests/test_staging.py` (new): 23 tests, 28 cases. `tests/test_resolver.py`: 6 fold tests.
  `tests/test_events.py`: a `Proposal` added to `_one_of_each`, so the round-trip test covers
  it and `test_every_taxonomy_type_is_registered` still holds. **That is an edit to an
  existing test, and the task requires it.** That test exists to fail when a type is added
  quietly, and adding one is what this task does. Nothing was removed or loosened. All
  fixtures are synthetic: a made-up Maya's basil, coriander and rasam.

**Why:**
- **D49.** Observations append freely, but an assertion is injected into every future
  context, so it gets a human yes. The review is a batch: "keep / edit / drop".
- **D60.** "Staging queue: is D49's review batch. Nothing leaves it without a human yes." A
  kept assertion starts at confidence 0.3 with evidence count 1, which are D60's starting
  numbers and already the model defaults.
- **D81.** Every assertion carries a one-line `summary` "from the moment it is proposed".
  So the proposal carries it too, and a keep copies it.
- **D86** (named in ARCHITECTURE §9, not in the issue): "`remember` only stages, the hot tier
  is rebuilt only after review". `remember` writes a `proposal` and nothing else.
- **Hard constraint 2.** "No deletes — use `supersede` tombstones." A drop is a `supersede`.
  The proposal's line stays in the file byte for byte, and superseding the tombstone stages
  the item again.
- **Hard constraints 3 and 5.** The queue is a fold in `resolver.py`, which
  `test_constraints.py` already keeps free of model imports. It is not a separate file, so
  the rebuild test covers it.

**Verify:** the criterion from PLAN §9, part by part:
- ***Staged item absent from the hot tier until approved:***
  `test_a_staged_item_is_absent_from_the_hot_tier_until_approved`. `walle remember` runs
  through the CLI, then a real `hot.render`. `learned.md` does not contain the text,
  `resolve(...).current` is empty, and `pending` lists the ref. Then `walle review` gets `k`
  and the render runs again. Now `learned.md` has the text, and the one current belief cites
  the proposal, has actor `user`, and nothing is left staged.
- ***Drop leaves a tombstone, not a deletion:*** `test_a_drop_leaves_a_tombstone_not_a_deletion`.
  The test takes the stream file's bytes before `walle review` gets `d`. Afterwards:
  - the file starts with exactly those bytes and is longer;
  - the proposal is still readable at its ref, with its body unchanged;
  - there is one `supersede`, targeting it, with actor `user`, and no assertion;
  - nothing is staged, `learned.md` does not contain it, and the CLI said "dropped d-mbp:1 …
    it stays in the log".
- Around it:
  - **edit:** writes the edited summary and body, cites the proposal, and leaves the
    proposal's own text in the log. Enter keeps the current text. An overlong summary is
    asked for again.
  - **keep** copies `valid_from`, `level`, body and summary, at D60's 0.3 / 1.
  - **skip** leaves an item staged, **quit** stops the batch, and items are offered in ref
    order.
  - Input ending part-way prints what is already written ("kept 1, … 1 not reached") before
    it stops.
  - **Decided once.**
    - A decided item is not offered again.
    - `keep` or `drop` on a kept or dropped item raises `NotStagedError` ("already kept as
      d-mbp:2", "already dropped by d-mbp:2") and the file does not change size.
    - A ref that is not a proposal, or that does not exist, is refused the same way.
  - **Two reviewers:** `test_a_second_reviewer_waits_and_finds_the_item_decided`, below.
  - **Folds** (`test_resolver.py`):
    - a proposal is never current;
    - an assertion citing it decides it;
    - superseding the tombstone stages it again;
    - forgetting a kept belief does not put its proposal back in the queue;
    - the order of the input does not matter.
  - **Rebuild:** after `index.rebuild` from nothing, the queue is the same.
  - **Cross-device:** a review on `d-mbp` of a proposal on `d-phone` cites across streams.
  - **`remember` input:**
    - the default summary is the first non-blank line, whitespace collapsed, cut to 120 with
      `…`, and a given `--summary` is folded onto one line;
    - an empty or whitespace-only text is refused, and so is a summary that is blank or over
      120. So is an unsafe device (`../x`). Each writes nothing, and an unsafe name does not
      even create the folder;
    - `walle review` for a principal with no log is an error, not "nothing staged".
- **The tests were written first.** Before any code, collection failed with
  `ImportError: cannot import name 'Proposal' from 'walle.events'` (test_events, test_resolver)
  and `cannot import name 'staging' from 'walle'` (test_staging). Once the tests passed, I
  broke the code on purpose, one change at a time, to check that the key tests can fail:
  - `remember` writes an `Assertion` instead of a `Proposal`: 8+ tests fail, including the
    hot-tier criterion.
  - `staged` ignores liveness: the tombstone criterion fails, along with 3 others.
  - `staged` ignores assertions that cite a proposal: the hot-tier criterion, decided-once,
    rebuild and two fold tests fail.
  - The "still staged?" check moved before `staging.lock`: the two-reviewer test fails.
  - `flock` removed from `staging.lock`: the two-reviewer test fails.
  - `drop` appends a keep instead of a supersede: the tombstone criterion and 2 others fail.
  - I re-ran `test_staging.py` three more times to check for flakes, and all passed.
- The four checks, on the final tree:

```
$ uv run pytest
311 passed, 3 skipped in 18.52s
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
41 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

  The 3 skips predate this task (`test_index.py`'s read-only-folder tests, which skip in this
  sandbox). `test_staging.py` has none.

A real run, in a temp folder:

```
$ walle remember --principal maya --device mbp "Maya keeps the basil on the east windowsill."
maya: staged d-mbp:1: Maya keeps the basil on the east windowsill.
Not in the hot tier until you keep it: run `walle review`.
$ walle remember --principal maya --device mbp "Maya dislikes coriander."
$ printf 'k\nd\n' | walle review --principal maya --device mbp
[1/2] d-mbp:1, staged 2026-11-04 by user … kept as d-mbp:3
[2/2] d-mbp:2, staged 2026-11-04 by user … dropped d-mbp:2: d-mbp:4 retires it; it stays in the log
maya: kept 1, edited 0, dropped 1, skipped 0.
Run `walle render` to put what you kept in the hot tier.
$ cat principals/p-maya/log/d-mbp.jsonl      # four lines: proposal, proposal, assertion, supersede
```

**Two-runner walkthrough:** this task appends to the log, takes a new lock and updates the
index. The same text is in `staging.py`'s docstring. Reviewers A and B for maya are both
deciding `d-mbp:4`, a staged proposal. A keeps it and B drops it. Writer W runs
`walle remember` meanwhile.

1. A takes `staging.lock` exclusively. B blocks here, having read nothing.
2. A runs `index.update` (`index.lock`, then every stream file shared, for a moment) and folds
   the records it committed. `d-mbp:4` is staged.
3. W appends `d-mbp:5`, a new proposal. W needs only its stream lock, which A no longer holds.
   A new proposal changes nothing about `d-mbp:4`, so `remember` does not take
   `staging.lock`.
4. A appends its assertion (`caused_by: [d-mbp:4]`) as `d-mbp:6` under the stream lock, which
   the writer holds until the bytes are `fsync`ed. A then releases `staging.lock`.
5. B wakes, **and only now** reads the log. It sees `d-mbp:6` citing `d-mbp:4`, raises
   `NotStagedError("d-mbp:4 was already kept as d-mbp:6")` having written nothing, and
   `walle review` prints that and moves on to its next item.

Had B checked before waiting for the lock, it would wake with a stale answer and append a drop
beside A's keep. `test_a_second_reviewer_waits_and_finds_the_item_decided` stages this. A
second process takes `staging.lock`, signals, sleeps 1 s, appends a keep on `d-phone` and
releases. The test's `drop` must wait at least 0.5 s, then raise "already kept as
d-phone:1", and no supersede may be written. Moving the check above the lock, or removing the
`flock`, makes it fail.

Even in the bad case the fold gives one answer: an assertion citing a proposal decides it,
and a supersede beside it changes nothing. But a log that contradicts itself is a bug to
prevent, not to fold around.

A renderer R running during all this takes `render.lock`, then `index.lock`, and sees either
the log before `d-mbp:6` (no belief) or after it (the belief). It never sees a proposal in
`current`, whatever it reads.

The lock is held for one decision, never across a prompt. Lock order:
- `staging.lock`, then `index.lock`, then stream files (shared), all released;
- then one stream file (exclusive).

The renderer and the indexer never take `staging.lock`, and nothing takes it while holding
another lock, so no cycle exists with task 6's or task 7's order.

**Stranger pass:** done because the diff is over 30 lines and is IO, a lock and several error
paths. I re-read `staging.py` and the two commands cold. Found and fixed:
- **A status line that miscounted.** The first draft counted an item as reviewed when the
  answer was read, before the decision was written. If input ended in the middle of an edit,
  or the append failed, "N not reached" was one short, so the report said an item was dealt
  with when nothing had been written for it. The count now goes up only once the outcome is
  known. `test_input_ending_mid_batch_reports_what_was_written` covers the EOF case.
- **No report on EOF.** Click turns EOF or Ctrl-C at a prompt into `Abort` and prints only
  "Aborted!". A person who answered three items would then not know whether they were saved.
  The command now prints the count line first, and then aborts with exit 1.
- **The `--device` help** said "decisions are appended to it", which is wrong for `remember`.
  It now says "what this command writes goes there".
- **Cross-module use of private helpers.** The CLI's prompt validation called
  `_clean_summary` and `_clean_body`. They are now public (`clean_summary`, `clean_body`), so
  the prompt and the API apply one rule.
- Looked at and left alone:
  - **Nothing is written before validation.** In `remember` and `_decide` the names, the
    text, the summary, the world times and the ref are all checked before any lock or file.
    `StreamWriter` validates in its constructor, and `log_dir` is checked before
    `staging.lock` is created, so a misspelt principal gets no folder (tested for `../x`).
  - **The lock cannot leak.** `staging.lock` is a context manager over an `open`. The lock
    goes with the close on every path, exceptions included.
  - **No error is ignored.** Every `except` in `review` either reports and re-raises, or (for
    `NotStagedError`) prints the reason and counts it under "already decided elsewhere".
  - **`index.update` runs inside `staging.lock`.** On a big log a full rebuild can hold the
    lock for a while. That only makes a second reviewer wait, which is correct.
  - **The decision path refolds the records three times** (`staged`, `retired_by`, the
    kept-by scan), and only to build an error message. That is linear in the log, and it is
    fine for Phase 0.

**Review:** pending.

**Choices made inside the task:** each is mine, and any of them can be overridden.
- **The staging queue is in the log, as a new `proposal` event type.** This is the choice
  most worth checking, because it adds a type that §5.2's taxonomy does not list. The
  reasons:
  - The verify criterion says a drop leaves a tombstone. Under hard constraint 2 a tombstone
    is a `supersede`, and a `supersede` needs a ref to target, so the staged item has to be
    an event.
  - A separate staging file beside the log would be state outside the log (constraint 5), and
    "removing" an item from it would be the deletion the criterion forbids.
  - It is its own type, not an `assertion` with a "pending" flag. That way nothing that folds
    assertions (the resolver, the renderer, the index's state hash, a future `recall`) can
    leak a staged item by forgetting to check a flag.

  I read this as implementing D60's staging queue and the task row, not as a new decision. If
  Shravan reads it as one, it belongs in `CONVERSATION.md` (CODEOWNERS), and the type can be
  renamed before any real log holds it.
- **A keep is one event, not two.** Keep and edit append only the assertion. The proposal
  counts as decided because the assertion cites it. An alternative would also supersede the
  proposal, but that is two appends, and a crash between them would leave a belief plus an
  item still staged, offered again and kept twice.
- **Any assertion that cites a proposal decides it, live or not.** Otherwise a later
  `walle forget` of the belief (task 12) would put the proposal back in the review batch.
- **Drop is undone by superseding the tombstone,** which is rule 1, unchanged. No command
  does that yet.
- **Proposal fields.** It has `level`, `valid_from` and `valid_to`, and the CLI sets none of
  them. The proposer (tasks 16–18) and D103's temporal markers will need them, and adding them
  later would be a schema bump. `confidence` and `evidence_count` are left out, because they
  describe a belief. `about` (D94) is left out, because it is Phase 3 and needs a manifest.
- **The default summary is the first line, cut to 120, not a model's paraphrase.** Nothing on
  this path may call a model, and the reviewer sees the summary and can edit it.
- **`--device` is required** on both commands. Nothing else in the CLI has a device default
  yet, and a hostname often does not fit the name rule.
- **The review does not render.** It ends with "Run `walle render` …". ARCHITECTURE says the
  hot tier is rebuilt only *after* review, not *by* it. Coupling the two would make
  `review`'s exit code depend on task 7's kept-edit handling.
- **The edit prompt is single-line.** Enter keeps the current text, and a new body is typed
  on one line. Multi-line editing in `$EDITOR` is what `walle context` (task 12) is for.
- **`walle review` updates the index** (through `index.update`), like `walle render` does.
  It reads the log fresh, so it never offers an item that was decided while the index was
  behind.

**What to read first:** `_decide` and `_still_staged` in `staging.py` (about 40 lines): the
lock, the re-read after the lock, and what is refused. Then `staged` in `resolver.py` (12
lines), which is the whole definition of "staged". Then the `Proposal` docstring in
`events.py`, for the event-type choice above.

**Noticed, not done:**
- **FTS indexes proposal text.** `index.py`'s `events_fts` covers every event's body and
  summary, so a future `recall` (tasks 13–14) that searches FTS without checking the type
  would return staged and dropped proposals as if they were beliefs. `recall` should fold
  first, or filter to current assertions.
- **D107's last step can now be wired.** The renderer keeps an out-of-band edit but does not
  stage it (`hot.py`'s docstring). `staging.remember` with `actor=curator` and the diff as the
  body is the obvious hook, and it is task 12's or a follow-up's.
- **Two devices that do not share a filesystem** can each decide the same proposal before
  their logs meet. Sync is deferred (D57). When it lands, the fold already gives one answer
  (a keep wins over a drop), but the log would hold both.
- **An older build reading a log with a `proposal` in it** fails with a validation error, not
  D111's named halt, because the type is unknown rather than its `v` being newer. No release
  exists yet, so no older build is out there. Task 24's golden logs should include one.
- **The review shows an item before re-checking it.** If another review decided it while this
  one was prompting, the person answers and is then told "already decided". A re-check before
  each prompt would save the question, but it costs an index update per item.
- **PLAN §9's status column** is not updated. `PLAN.md` is under CODEOWNERS.

**Cost:** pending.
