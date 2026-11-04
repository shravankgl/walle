# Task 12: walle forget, walle why and walle context

Issue #10. Branch `task/12`. One commit.

**Shipped:**
- `src/walle/index.py` (changed): `ancestors(root, principal, ref)`. It is a `WITH RECURSIVE`
  query over `edges`. It returns `ref` at depth 0 and every event its `caused_by` reaches,
  each once at its shortest depth, ordered by depth and then ref. It reads the index only,
  under `index.lock` shared, the same way `load_records` does. It raises `KeyError` for a ref
  the index does not hold. A depth bound (the event count) ends a cycle that a hand-edited log
  could hold. The module docstring's "``why`` (task 12)" now names `ancestors`.
- `src/walle/why.py` (new):
  - `explain(root, principal, ref)` brings the index up to date, runs `ancestors`, and gives
    each step a status from the fold.
  - An assertion's status is `current`, `forgotten by <supersede>` or `edited by <supersede>,
    replaced by <assertion>`, with the day the supersede was logged and its `valid_to` when it
    states one.
  - A proposal's status is `staged`, `kept as <assertion>` or `dropped by <supersede>`.
  - `Ledger` is that fold, kept so the status of any ref can be asked. `context` uses it too,
    to say why it refused.
- `src/walle/context.py` (new):
  - `forget(...)` appends one `supersede` targeting a current assertion, with optional
    `valid_to` and `reason`. It runs under `staging.lock`, after re-reading the log.
    `NotCurrentError` says what the ref is instead: missing, not an assertion (with a pointer
    to `walle review` for a proposal), or already forgotten or edited, and by what.
  - `open_view` builds the live view: every current assertion as `### [#ref] summary` and its
    text, oldest first, under a comment preamble.
  - `parse` reads a view back. `plan` diffs a save against the view it came from.
  - `apply` checks the save, takes `staging.lock`, re-reads the log, and refuses the whole save
    with `StaleViewError` if any fact it touches is no longer current. Otherwise it appends the
    events. `PartialWriteError` names what reached the log if an append fails part-way.
- `src/walle/cli.py` (changed): three commands.
  - `walle forget --principal P --device D [--valid-to T] [--reason R] REF`.
  - `walle why --principal P REF` prints the chain, indented by depth, one status per step and
    a one-line gist.
  - `walle context --principal P --device D` opens the view in `$EDITOR` with `click.edit`,
    applies the save, and prints what it wrote. A refused save is kept in
    `principals/p-<name>/context-unsaved-*.md` (mode 0600, inside the principal's folder, hard
    constraint 6), and the command says where.
  - The render's "kept your edit" message no longer says `walle context` is "not built yet".
- `tests/test_why.py` (new): 8 tests. `tests/test_context.py` (new): 13 tests, 19 cases with
  their parameters. All data is synthetic: a made-up Maya's basil, dinner and fern.

**Why:**
- **D59.** "`why` is a recursive query." The `edges (src, dst, kind)` table task 6 built is
  queried with `WITH RECURSIVE`. There is no new artifact and no new dependency (networkx was
  allowed and is not needed). Like the rest of the index, it is rebuilt from the log.
- **D57.** Refs are `d-<device>:<seq>`, so the chain crosses streams with no special case. The
  verify test's chain runs through both `d-mbp` and `d-phone`.
- **D93.** A retraction has a log time and a world time. `forget --valid-to` puts the world
  time on the `supersede`. The resolver already reads it into `Retired.valid_to`, and `why`
  prints it.
- **D103.** `valid_to` is never guessed: `forget` sets it only when the person gives it, and it
  must be a date or an RFC 3339 instant. An edit through `context` carries the old fact's
  `valid_from` and `valid_to` over unchanged rather than inventing new ones.
- **D107.** "`walle context` opens the live view in `$EDITOR`, diffs what comes back, and
  appends the events that diff implies. The log stays the only writer of state." The view is
  built from the fold, never read out of `learned.md`, which is derived.
- **Hard constraint 2.** Forget, edit and remove are all `supersede`s. Every test that writes
  checks that the stream file still starts with its old bytes.
- **Hard constraint 3.** `why` reads the resolver; nothing here calls a model.
- **Hard constraint 5.** The verify test deletes the index after a context edit, rebuilds,
  and compares `state_hash`.

**Verify:** the criterion from PLAN §9, part by part.
- ***`why` returns the full `caused_by` chain:*** `test_why_returns_the_full_caused_by_chain`.
  - The log has two streams, a diamond (an observation cited at depth 1 and again at depth 3)
    and a bystander event outside the chain.
  - The answer is exactly `[(restated,0), (obs_a,1), (kept,1), (proposal,2), (obs_b,3)]`,
    without the bystander.
  - `test_the_chain_comes_from_the_index_edges` shows the chain is the index's answer: an
    event appended after the last build is a `KeyError` to `ancestors` until `explain`
    updates the index.
  - `test_a_cycle_in_caused_by_still_ends` checks a hand-made two-event cycle.
- ***`forget` hides the fact from the render and `why` still explains it:***
  `test_forget_hides_the_fact_from_the_render_and_why_still_explains_it`.
  - Two facts are kept through `staging`, then `hot.render` runs: the basil is in
    `learned.md`. Then `walle forget` runs through the CLI.
  - The stream file starts with its old bytes and is longer.
  - A real render no longer has the basil and still has the dinner.
  - The assertion is still in the log at its ref, with one `supersede` targeting it, actor
    `user`.
  - `why.explain(ref)` puts the forgotten assertion first, with a status naming the supersede,
    and then the proposal it was kept from.
- ***An edit through `context` appends events and survives a rebuild:***
  `test_an_edit_through_context_appends_events_and_survives_a_rebuild`.
  - One save edits an entry, deletes one and adds one.
  - The file only grew. What was written is, in order, `Assertion, Supersede, Supersede,
    Assertion`.
  - The new text is current and cites the old ref. The removed fact is retired. The added one
    is current, by `user`.
  - Then the index file is deleted and rebuilt: `state_hash` is equal.
  - A render shows the new text and neither old one, and `why` on the old ref says "edited by
    …, replaced by <new ref>".
- Around it: `forget --valid-to` reaches `Retired.valid_to`, and forget refuses (writing
  nothing) a forgotten ref, a proposal, a missing ref and an unparseable world time. The view
  round-trips, including body lines that look like headers or escaped headers. An unchanged
  save writes nothing. A cancelled or emptied editor writes nothing. Five kinds of bad save
  are each refused whole, even with a valid edit beside them. A stale view is refused, and the
  CLI keeps the refused text in a file.
- Failing first: written before the code, the two files failed at import (`cannot import name
  'context'`). With the implementation stashed and the tests kept, 16 fail.
- Checks:
  - `uv run pytest`: 474 passed, 3 skipped.
  - `uv run ruff check`: All checks passed!
  - `uv run ruff format --check`: 55 files already formatted.
  - `uv run pyright`: 0 errors, 0 warnings, 0 informations.
- By hand, through the installed `walle`: remember and keep two facts. Run `context` with
  `EDITOR` set to a `sed` script that changes one. Run `forget --valid-to 2026-10-01` on the
  other. `why` on each prints the expected statuses. A second `forget` exits 1 with "already
  forgotten". `render` shows one assertion. `rebuild --full` and an incremental `rebuild` print
  the same resolved state.

**Two-runner walkthrough:** this touches the log, `staging.lock` and the index. It is also in
`context.py`'s docstring, and `why.py` says which state its answer describes.

*Person A runs `walle context` and edits `d-mbp:6`. Person B runs `walle forget d-mbp:6`.
Writer W appends observations throughout.*
1. A updates the index, folds it and opens the editor, **with no lock**. Holding
   `staging.lock` across an editor session would stall every review and forget for as long as
   the editor is open.
2. B takes `staging.lock`, updates the index, folds it and finds `d-mbp:6` current. B appends
   `supersede d-mbp:6` as `d-mbp:9` under its stream lock, `fsync`ed, then releases
   `staging.lock`.
3. W appends `d-mbp:10`. W needs only its stream lock and changes no assertion.
4. A saves. `apply` parses and checks the save with no lock and no IO. Then it takes
   `staging.lock` and only now re-reads the log. `d-mbp:6` is no longer current, so A raises
   `StaleViewError` naming `d-mbp:9`, having written nothing. The CLI keeps A's text in a file.
- If B arrives while A holds the lock in step 4, B waits, re-reads, and finds `d-mbp:6`
  already retired by A's edit. B refuses with "already edited by …, replaced by …".
- Had A trusted its step-1 view, it would append a replacement for a fact B had just retired,
  and the fact would come back under a new ref.
- `test_an_apply_that_waits_on_another_decision_reads_the_log_after` stages step 4 with a
  second process. It holds `staging.lock` for 0.8 s and writes the supersede. The apply waits at
  least 0.5 s and refuses, and no edit citing the ref is in the log.
- Facts the save does not touch are not checked. A fact kept meanwhile was not in A's view, so
  A's save neither edits nor removes it.

*Two forgets of the same ref, A and B:* the same shape as task 9's two reviewers. A takes
`staging.lock` and appends. B waits, re-reads, and raises `NotCurrentError("… already
forgotten by …")`. The log never holds two retractions from one intent.

*One save, crash part-way:* the events are written under one hold of the stream lock, each
`fsync`ed before the next. Each edit writes its assertion first and then its supersede. A
crash between the two leaves both texts current: a duplicate the next `context` shows, never
a lost fact. `PartialWriteError` lists what was written if an append raises.

*`why` while W appends:* `explain` runs `update`, then `ancestors`, then `load_records`, each
under its own `index.lock`. `caused_by` never changes, so the chain is fixed once `ref` exists.
The rows read last are a superset of what the query saw, because the index only grows, so
every step has its event. A status can be an append or two newer than the chain, which is
the right way round.

Lock order is `staging.lock`, then `index.lock`, then stream files (shared), released, then
one stream file (exclusive). That is `staging.py`'s order. `why` takes no `staging.lock`.
Nothing takes `staging.lock` while holding another lock, so no cycle exists.

**Stranger pass:** I re-read the diff as someone else's code (it is over 30 lines, and it
touches IO, locks and error paths).
- Found and fixed: `walle why` did not catch `StaleIndexError`. `ancestors` and `load_records`
  can raise it, so a stale index printed a traceback. It is now said plainly.
- Found and fixed: a status that lied. My first CLI draft said "Nothing was written" for any
  error from `apply`, including an `OSError` on the third of four appends. `PartialWriteError`
  now carries the refs already written, and the CLI says "nothing was written" only for
  errors raised before the first append.
- Found and fixed: `StaleViewError`'s own text also said "Nothing was written", so the CLI
  printed it twice. It now says it once, in the CLI.
- Found and fixed: the view was ordered by the `ts` string, which misorders instants with
  different UTC offsets. It now parses `ts`, as `hot._newness` does.
- Checked, nothing to change:
  - Locks are held only around a re-read and its appends, never across the editor.
  - `_keep_unsaved`'s file descriptor is owned by the `with`.
  - `mkstemp` makes the unsaved file 0600, which is right for personal text.
  - The CTE ends on a cycle (tested).
  - The escape rule round-trips any line (tested with both header-like forms and a plain
    backslash).
- Left as is: a Ctrl-C between two appends of one save is not wrapped in `PartialWriteError`.
  Wrapping `KeyboardInterrupt` would change what Ctrl-C means. The prefix it leaves is the
  same as a crash's, which is safe (above).

**Review:** pending.

**Choices made inside the task:**
- **`[#new]` writes an assertion, not a proposal.** D107 lists what `context` appends ("an
  `assertion` edit, a `supersede`, a `rules` write"), and the person typing into their own
  editor is D49's human yes. D86 ("`remember` only stages") is about the path an agent can
  call, which this is not: `context` needs a terminal and `$EDITOR`. If Shravan wants adds to
  stage, it is a one-line change in `_write`.
- **An empty save cancels.** It is never "forget everything". A crashed editor and a deliberate
  wipe look the same. Deleting every entry while keeping the comment preamble does forget
  everything, because that file is plainly intentional.
- **The edit's supersede cites the new assertion** (`caused_by: [new]`). That is how `why`
  tells "edited, replaced by X" from "forgotten" without reading `reason`, which is free text.
- **An edit keeps level, confidence, evidence count, world time and `about`.** A reworded fact
  is the same belief. Resetting confidence to 0.3 would punish someone for fixing a typo.
- **`forget` refuses anything that is not a current assertion**, rather than superseding any
  ref. Proposals have `walle review`'s drop. Superseding a supersede (an undo) is a real
  operation, but it deserves its own command and its own decision, not a side door here.
- **The stale-view check is per ref, not a whole-state hash.** A whole-state hash would refuse
  every save made while the observer was running.
- **`why` shows ancestors only.** The criterion is "the full `caused_by` chain". What a fact
  later caused (descendants) is a different question, left for when one needs it.

**What to read first:** `context.py`'s docstring and `apply` / `_write`, the only code here
that appends more than one event per decision. Then `index._ANCESTORS`: the `UNION` on `(ref,
depth)` plus the depth bound is what keeps a cyclic hand-edited log from hanging `why`.

**Noticed, not done:**
- D107 also has `context` doing "a `rules` write". `rules.md` is human-only and not in the log,
  so it needs its own decision about whether a rules change becomes an event.
- D107's last step (a render that finds an out-of-band edit stages it as a proposal) is still
  not built. Task 7's log already says so.
- `hot.py`'s module docstring still says `walle context` "will" be how an edit is made
  properly. It is now built. The user-facing message in `cli.py` is fixed; the docstring is
  left for whoever next touches `hot.py`.
- `walle context` cannot edit `valid_from` / `valid_to`. D103 wants them editable "in the review
  batch", which `walle review` does not do yet either.
- Undoing a forget (superseding the supersede) works in the resolver but has no command.

**Cost:** pending.
