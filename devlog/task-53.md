# Task 53: acceptance-rate report

Issue #53. Branch `task/p1-stats`, from `main` at `c17fef6` (the Curator, #64).

**Shipped:**

- `src/walle/stats.py` (new): the Phase 1 gate metric as a fold over the log. `count(records)`
  is pure (no IO, no model, any order gives the same answer) and returns `Counts(kept, edited,
  dropped, staged)`, integers only, with `proposals`, `accepted` and `decided` derived from
  them. `stats(root, principal)` brings the index up to date and counts what it committed.
  `percent(accepted, decided)` is the rate as a whole number, halves rounded up.
- `src/walle/cli.py`: `walle stats --principal [--root]`. Prints the four counts and
  `acceptance: A of D decided (P%)`, or `nothing decided yet` when D is 0. Skipped log lines
  are reported on stderr as a count, with a note that whatever was on them is not counted.
  Errors go through `_without_content`, the observer's helper, so a line pydantic refuses is
  named by file and line, never quoted.
- `tests/fixtures/stats/d-mbp.jsonl`, `d-phone.jsonl` (new): 22 synthetic events across two
  devices, built from this repo's event models with fixed timestamps. Nine proposals cover
  every edge the fold has to get right; the table is in the test's docstring.
- `tests/test_stats.py` (new): 19 tests, from 12 functions and one rounding test with seven
  cases.

**Why:**

- PLAN §3, Phase 1 gate: "proposal acceptance rate is respectable". PLAN §1's metric table
  defines it as observer proposals the user accepts. This is that number.
- D49: review is keep / edit / drop. Those are the three counts, plus what is still waiting.
- D120 and `Proposal`'s docstring say how the log records each: an assertion whose
  `caused_by` names the proposal is a keep or an edit; a live supersede of it is a drop;
  anything else is staged. `count` reads it exactly that way and reuses `resolver.staged` and
  `resolver.retired_refs`, so `walle review` and `walle stats` cannot disagree about what is
  in the queue.
- Hard constraint 5: the counts come from the records, and the index only delivers them. A
  full rebuild, a deleted index and a garbage index all give byte-identical output.
- Hard constraint 3 in spirit: no model call anywhere on this path.

**Verify:** issue #53's criterion, in `tests/test_stats.py`:

- *Counts on a fixture log match a hand count:* `test_counts_on_the_fixture_match_the_hand_count`
  compares the whole stdout with the hand count (9 proposals: kept 4, edited 2, dropped 1,
  still staged 2; 6 of 7 decided, 86%). `test_the_fold_matches_the_hand_count` checks `count`
  directly.
- *The output comes from the log alone, so a rebuilt index gives the same numbers:*
  `test_a_rebuilt_index_gives_the_same_numbers` (after `rebuild`, after deleting the index,
  after overwriting it with garbage: identical exit code, stdout and stderr) and
  `test_the_numbers_are_the_streams_folded_with_no_index` (records read straight from the
  stream files with `read_file`, before any index exists, give the same counts, in either
  order). `test_an_index_behind_the_log_is_brought_up_to_date_first` appends a proposal after
  the index was built and sees it counted.
- *Prints counts only, never proposal text:*
  `test_only_counts_are_printed_never_what_anything_says` collects every body, summary and
  reason in the fixture, and every word over four letters of every body and summary, and finds
  none in stdout or stderr; no ref is printed either. Plus the two error-path tests below.

Written first and run before `stats.py` existed: collection failed with `ImportError: cannot
import name 'stats' from 'walle'`.

Check output on the final tree:

```
uv run pytest            1484 passed, 10 skipped
uv run ruff check        All checks passed!
uv run ruff format --check   113 files already formatted
uv run pyright           0 errors, 0 warnings, 0 informations
```

No test depends on file permissions or the user it runs as: everything is under `tmp_path`,
and nothing uses `chmod`, read-only folders or `geteuid`, so the suite gives the same answer
as root.

**Two-runner walkthrough:** `stats` writes nothing to the log. It touches one derived file, the
index, through `index.update`, the same call `walle review` and `walle propose` make. Runner A
(`stats`) and writer B (`remember`) at once: A takes `index.lock`, then every stream file
shared for a moment while it reads a cut; B's append needs its stream lock exclusive, so it
either lands before A's cut (A counts it) or after (A does not, and the next `stats` does).
A counts `report.records`, the rows its own build committed, read before it released
`index.lock`, so a third runner's later build cannot leak into A's numbers halfway. Two `stats`
runs at once serialise on `index.lock`; the second finds nothing new and counts the same rows.
Neither takes `staging.lock`, so a count never waits on a review.

**Stranger pass:** the diff is over 30 lines and has an error path, so it was re-read cold.

- The skipped-line path and the corrupt-line path were untested, and both are where proposal
  text could leak: `Skip` carries a preview of the raw bytes, and pydantic quotes the value it
  refuses. Added `test_a_torn_line_is_reported_as_uncounted_without_its_text` (exit 0, counts
  unchanged, stderr says one line was skipped, the torn text is absent) and
  `test_a_line_that_does_not_validate_fails_without_quoting_it` (exit 1, the file is named, the
  text is not). Both passed against the code as written: the command prints the skip count,
  never the preview, and uses `_without_content`.
- Status messages: the skip note says `walle rebuild` names the lines; it does
  (`rebuild_command` prints each skip). "N proposals in the log" counts proposals the reader
  could read, and the skip note says so when that is not all of them.
- No ignored errors: every `_LOG_ERRORS` case becomes a `ClickException`, exit 1. No path
  answers zero for a missing log: `index.update` raises `FileNotFoundError` before creating
  anything, tested by `test_no_log_is_an_error_not_zero` (the principal folder is not created).
- No locks of its own and no files opened directly, so nothing to leak.
- My own leak test first failed on the word "dropped", from walle's tombstone reason
  "dropped in review", which is also an output label. The word-level check now covers bodies
  and summaries; whole reasons are still checked. Said here because it is a test change made
  during the task.

**Review:** none before the PR. Under R6/R8 the reviewer agent reviews this PR's head and sets
`agent-review`; the author never sets it. The author did not run `scripts/review.sh`.

**Choices made inside the task:**

- **A forgotten keep is still a keep.** The metric is what review said, which is what
  measures the proposer. Counting only beliefs still current would mix review quality with
  later life changes.
- **An edit that changed nothing is a keep.** `walle review`'s edit with Enter twice writes the
  same assertion a keep writes; the log cannot tell them apart, so the text decides.
- **A proposal kept on one device and dropped on another is kept.** That is the resolver's
  rule ("an assertion decides"), and `staging._still_staged` says the same.
- **An undone drop is staged,** because superseding the tombstone puts it back in the queue.
- **Edited counts as accepted** in the rate. The person said yes and fixed the wording; that is
  the proposer being useful. Kept and edited are printed separately for anyone who disagrees.
- **Rounding half up with integers.** `round` would make 1 of 8 into 12%; 13% is what a person
  would write.
- **No per-actor split** (proposer versus curator versus `remember`). The issue asks for kept,
  edited and dropped; a split is a later question, and it would need a decision on whether
  `remember` proposals count toward the gate at all (see below).
- **The `stats` command sits at the end of `cli.py`, after the evals line.** Main gained the
  `acp` command (#66) in the slot after `observe` where `stats` first went, and the PR
  conflicted. Moving `stats` below the evals keeps the two additions apart, so main merges in
  cleanly. The code is unchanged; all four checks pass on the merged tree.

**What to read first:** `count` in `src/walle/stats.py` and the table at the top of
`tests/test_stats.py`. If a row in that table looks wrong to you, the metric is wrong.

**Noticed, not done:**

- PLAN §1 defines the metric as *Observer* proposals accepted. `walle stats` counts every
  proposal, including ones a person typed with `walle remember`, which are almost always kept
  and would flatter the rate. Filtering by `actor` is a one-line change but chooses what the
  gate measures, which is a decision for Shravan, not this task.
- `proposer.score` computes its own keep/drop sets from the log rather than sharing a fold with
  `stats`. They agree today; a shared helper would keep them agreeing.

**Cost:** not visible to the author agent from inside the run. Shravan can read it from
Hyperagent.
