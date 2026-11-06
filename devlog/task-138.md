# Task 138: Audit — Codex observer turns and dedupe

Issue #138, a read-only audit (Gemini 4 Argon at main `9bd13c0`) with four findings on
`src/walle/codex.py`. The issue said the findings were not re-run, so a failing test came first
for each. **Every case in the issue failed on main before any fix**, so every case was fixed.
`observer.py`, `transcript.py` and `hazards.py` are untouched, as the issue asked. Built from
Codex's public source at `openai/codex` commit
[`388aee4b`](https://github.com/openai/codex/tree/388aee4b26226778701a7fa749354c44f74b4394/codex-rs),
the commit #81 read; still unverified against a real file, and the module docstring still says so.

**Shipped:**

- `src/walle/codex.py` — the Codex rollout reader and turn folder.
  - The person's message is one block: `user:` on its first line, continuation lines and
    `[image]` lines unlabelled (finding 1.1, the layout #129's finding 2.4 gave `observer.py`).
  - A `user_message` that arrives after a turn has been answered closes the turn so far and opens
    the next segment of the same turn (finding 1.2). `_Turn.segment` counts the pieces.
  - `turn_aborted` finishes the turn it ends (finding 3.3).
  - An implicit turn still open at the end of a file is complete once the file is quiet: not
    changed for `TAIL_GRACE` seconds, the constant imported from `transcript.py` (finding 2).
    `RolloutFile.growing` carries the signal; `_changed_within` reads `mtime` after the read.
  - A turn id is matched only within a lineage: `session_meta.id` followed through
    `forked_from_id` to the oldest ancestor named (finding 3.1). `RolloutFile.thread_id` and
    `.forked_from` hold the two ids for this purpose only; `_lineage` does the walk.
  - A fork whose ancestor could not be read this run has its turns marked
    `ObservedTurn.orphan_fork`; `append_observations` treats the copied prefix, matched by body
    against Codex observations already in the log, as already logged (finding 3.2).
  - A tool's name is folded onto its one `assistant:` line; a whitespace-only prompt with no image
    is nobody speaking; a whitespace-only reply is dropped like an empty one (finding 4).
- `tests/test_codex.py` — eleven tests under *issue #138*, one or more per finding plus a fence
  for each fix, and one expectation changed (below).
- `devlog/task-138.md` — this file.

**Why:**

- **D92** — what is already logged is a fold over the log by content: `(source, observed_at,
  body, source_version)`. That fold cannot tell a short observation of a turn from a longer one
  (3.3: a line after `turn_aborted` grew the turn), nor a fork's fresh-stamped copy from the
  original (3.2: the copy matches nothing by key once the parent is unreadable). Both fixes keep
  D92's key sufficient by never emitting the second observation, rather than adding a cursor or a
  second store.
- **D101** — no session id and no turn id reaches an observation; that still holds. The thread id
  and `forked_from_id` now live on the in-memory `RolloutFile`, scoping the one thing a turn id was
  ever used for, a fork's copy counted once (3.1). `test_the_session_id_is_never_a_key` still
  passes, and the `model_dump_json()` of every observation is checked for the id.
- **D56** — a turn is observed whole, once complete. Finding 2 was a turn that could never be
  complete: in a markerless file only the next prompt cuts a turn, and the last has none. The rule
  added is the one task 129 gave the Claude Code observer for a torn tail: quiet for `TAIL_GRACE`
  means nothing is writing. Finding 1.2 is D56's "turn" read as the Claude Code observer reads it:
  a prompt after a reply is the person moving on (its rule 1).
- **D55, D77** — Codex text meets the same hazard checks. Findings 1.1 and 1.2 were bodies the
  observer itself laid out so that `hazards.forged_turn` had to flag them (`user:` after an
  unlabelled line, `user:` after `assistant:`), withholding a genuine turn from the proposer. The
  layout is fixed in the observer; the gate is unchanged, as #129 chose.
- No D-number was changed or contradicted, so `flag` was not used. Finding 3.2's complete fix,
  reading `.jsonl.zst`, needs zstd, which is outside the allowlist in `tests/test_constraints.py`
  (Python is 3.12; the standard library gains zstd only in 3.14). That is a decision for the
  owner, recorded under *Noticed, not done*; the fix shipped needs no dependency.

**Verify:** a failing test for each finding first; nothing is written to the log on a refusal.

| Finding | Test that failed on main | What it proves now |
|---|---|---|
| 1.1 multi-line prompt with an image | `test_a_multi_line_prompt_with_an_image_is_one_user_block_and_not_a_forged_turn` | body is `user: Look at this screenshot:\nWhat does it mean?\n[image]\nassistant: …` and `hazards.flag` returns `()` |
| 1.2 follow-up after a tool call | `test_a_follow_up_after_a_tool_call_in_a_running_turn_is_its_own_turn` | two observations, each unflagged, each with its own `observed_at`; no duplicate, nothing pending |
| 2 last turn of a markerless file | `test_the_last_turn_of_a_markerless_file_is_observed_once_the_file_is_quiet` | pending while fresh (nothing appended for it); after `TAIL_GRACE` appended once; a third run appends nothing and the stream is byte-identical |
| 2 fence | `test_a_quiet_file_does_not_close_an_explicit_turn_that_is_still_open` | an explicit turn with no `task_complete` stays pending however quiet the file |
| 3.1 two sessions, one turn id | `test_two_sessions_that_reuse_a_turn_id_are_both_observed` | both observed, `duplicates == ()` |
| 3.1 fence | `test_a_fork_of_a_fork_still_counts_a_copied_turn_once` (passed on main, kept as the fence) | a copy carried through two forks is still the root's turn, kept from the root's file |
| 3.2 parent compressed after the fork | `test_a_forks_copy_is_not_appended_again_once_the_parent_is_compressed` | with the parent now `.jsonl.zst`, the second run appends nothing, reports both turns already logged, counts one skipped file |
| 3.2 fence | `test_a_new_turn_in_a_fork_whose_parent_is_gone_is_still_observed` (failed on main too: the copy was appended again beside the new turn) | only the copied prefix is matched; the fork's own turn after it is appended |
| 3.2 stranger-pass fence | `test_a_fork_with_no_id_of_its_own_still_knows_its_parent_is_unread` | a fork naming only its parent is still a fork of an unread parent |
| 3.3 line after `turn_aborted` | `test_a_record_after_turn_aborted_does_not_reopen_the_turn` | the aborted turn is observed as it stood; a late reply appends nothing and leaves nothing pending |
| 4 tool name, blank text | `test_a_tool_name_is_one_line_and_blank_text_is_not_speech` | `[tool: run tests]` on one line; a turn whose only prompt is blank is not observed; a blank reply is dropped |

For finding 2 the test as written errors on main at `codex.TAIL_GRACE` before the assertion, so
the behaviour was also reproduced directly: a markerless file with `mtime` 600 s old still gave
`['user: q1\nassistant: a1']` and one pending turn. All other cases failed on main at the
assertion the table names.

One existing expectation changed because the task required it:
`test_images_tools_and_empty_replies` asserted the old per-image layout
`user: what is this\nuser: [image]\nuser: [image]`; it now asserts
`user: what is this\n[image]\nuser: [image]` (the second `user:` is a second message). Nothing else
in the suite moved.

Checks, at the final commit:

```
uv run pytest               4331 passed, 35 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  214 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

After review round 1 (below), at the second commit:

```
uv run pytest               4344 passed, 35 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  216 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** Codex C appending to a rollout, this reader R; two Rs are serialised by
the stream's lock as before (fold and append under one `locked()`; the `bodies` set and the
`copying` set are built under it too).

1. *Finding 2.* C, an older build, has written `q1 a1 q2 a2` with no turn markers. R reads the
   file, then stats it: changed within 300 s, `growing=True`. `q1` is cut by `q2` and appended;
   `q2` is open, implicit, in a growing file: pending. Nothing is written for it.
2. C writes `q3` and `a3`. R's next read: `q2` is cut by `q3`, appended once with `user: q2\nassistant:
   a2`; `q3` is pending. (Had R observed `q2` in step 1, this run would have had the same key:
   the cut body and the open body are the same when nothing more joined it. Had C appended a
   second reply to `q2` between the runs, the key would differ: the pending hold prevents that
   duplicate, as task 129's hold does.)
3. C exits. For 300 s R reads the file as growing and `q3` stays pending. After that `growing=False`,
   `q3` is `settled`, appended once; every later run finds it in the fold and appends nothing.
4. If C pauses mid-turn for longer than the grace (a long tool call with no line written) R observes
   the turn early, and a later reply makes a second, longer observation. The same limit task 129
   accepted for the Claude Code observer, for the same reason: five minutes is well clear of a
   write in progress and cheap on a crash.
5. *Finding 3.3.* C writes `started q a aborted`. R observes `user: q\nassistant: a`. C (or anything)
   appends a reply to the file. R's next run: the aborted turn was finished at the abort; the late
   reply joins a further segment of that turn, nobody spoke in it; nothing is observed, nothing
   appended, nothing pending.
6. *Finding 3.2.* R observes parent P (turn `p1`, version 0.60.0) and fork F (copy of `p1`,
   fresh-stamped, version 0.61.0, deduplicated by turn id within the lineage; own turn `f1`). The log
   holds `p1` from P and `f1` from F. Codex compresses P. R's next run: P is a counted skip; F's
   chain ends at an id no file has, so F's turns are `orphan_fork`. In file order, the copy's body is
   in the log: already logged, prefix still open; `f1`'s key is in the log: already logged. Nothing
   appended. Had F gained a turn `f2`, its body not in the log: the prefix closes there and `f2` is
   appended; anything after it is the fork's own regardless of body.

**Stranger pass:** the diff is over 30 lines and touches IO (a `stat`) and the dedupe path. Found
and done:

- `_lineage` seeded its `seen` set with `thread_id or forked_from`. A fork whose `session_meta` had
  no `id` of its own but named a parent then skipped the walk and reported the parent readable, so
  its copy would have been appended again once the parent was compressed. Fixed; the walk now
  starts from the file's own id only, and `test_a_fork_with_no_id_of_its_own_still_knows_its_parent_is_unread`
  is the fence.
- `_changed_within` swallows `OSError` from `stat` and answers "not growing": a file gone between
  the read and the stat has no writer, and what was read is observed. Not a silent failure: the
  file's records were read in full and the next listing will not find it.
- Lock scope: unchanged. `append_observations` folds, builds its two sets, and appends under one
  lock. The body set is a derived view of the same read.
- Status messages: no skip reason changed. `DuplicateTurn.turn_id` is still the Codex id.
  `PendingTurn` is reported for an open implicit turn in a growing file, as before.
- `_fold`'s follow-up segment reuses the Codex turn id, so `task_complete` and `turn_aborted` still
  find the turn they name; the earlier segment is already finished with `cut=True`.
- Secrets: the two ids kept on `RolloutFile` are Codex's thread ids, not content, and the existing
  canary tests over records and skips still pass. The `model_dump_json()` check in
  `test_the_session_id_is_never_a_key` guards the observation.
- The purity test (`test_the_module_imports_no_model_or_network_library`) still passes: the grace
  comes from `walle.transcript`, and time is read through `datetime`, so no new top-level import.

**Review:** Gemini 4 Argon, direct diff review (the `ocr` registry was unreachable from its
sandbox), on head `0331fd2` (my commit `e6d78fe` plus a merge of `main` at `0f9cb81`, #140, that I
did not make; I confirmed its tree equals a fresh merge of `main` into `e6d78fe` before building on
it). `agent-review` failed: 2 medium, 1 low, no high. Each finding got a failing test on `0331fd2`
first, then a fix on the same branch:

| # | Finding | Test that failed on `0331fd2` | Fix |
|---|---|---|---|
| M1 | A whitespace-only `user_message` with no image still cut an implicit turn or split an explicit one before `if block:` ran, so the reply after it landed in an unspoken segment and was dropped | `test_a_blank_message_does_not_cut_or_split_a_turn` (an explicit turn loses `assistant: done`; a markerless file loses `q1 -> a1`) | `_record` returns `None` for it, as it does for an empty reply, so no such record reaches `_fold` at all; the `if block:` guard in `_fold` is gone, since the record now always holds speech |
| M2 | In `append_observations`, an exact-key match did not discard the file from `copying`, so an orphan fork's prefix stayed open across the fork's own logged turns and a later turn repeating a logged body was dropped | `test_a_forks_own_logged_turn_closes_the_copied_prefix` | an exact key is a turn observed from this fork on an earlier run (a copy carries fresh timestamps, so it never matches by key): `copying.discard` there too |
| L1 | `thread_rolled_back` sliced `finished` by segment, not by Codex turn, so after a split turn `num_turns=1` left the earlier segment observed; the implicit turn a late post-abort line opened counted as a turn of its own | `test_a_rollback_counts_codex_turns_not_segments` (a split turn, an aborted turn with a late reply, and `num_turns=2`) | `_rollback` counts consecutive segments with one turn id as one turn; `abort()` leaves a further segment of the aborted turn open (same id, `segment + 1`) instead of letting a late line open an implicit turn, which also follows Codex, whose reducer keeps writing under the aborted turn |

The first-round tests all still pass; `test_a_record_after_turn_aborted_does_not_reopen_the_turn`
holds with the tail segment (nobody spoke in it, so it is neither observed nor pending). Full
checks at the second commit are above. Not reviewed by the author's own `scripts/review.sh` run,
which the agent build period replaces.

**Choices made inside the task:**

- *1.1, one block per message, not one block per turn.* Two `user_message`s before any reply are
  two `user:` blocks, as `observer.py` writes two prompt entries. A second message after a
  multi-line or image-bearing first one is therefore still the layout `forged_turn` flags, the
  cost PR #50 and #129 chose to keep rather than open the gate. Folding every message before the
  reply into one block would have hidden where one message ended, and changed
  `test_a_message_while_a_turn_runs_belongs_to_that_turn` for no finding in the issue.
- *1.2, split rather than relabel.* The auditor offered splitting the turn or writing the follow-up
  without a `user:` label. Unlabelled, the person's words would read as the reply's continuation
  (`proposer.person_lines` stops at the first `assistant:`), which misattributes them. Splitting
  matches what the Claude Code observer does with the same conversation, which is the module's
  stated goal. The segment index is in the dedupe key so a fork's copy of a split turn is still
  counted once per piece.
- *2, implicit turns only.* An explicit turn open at the end of a quiet file (Codex killed mid-turn,
  no `turn_aborted` written) stays pending. Its marker exists and may still come; closing it on
  quiet alone would observe a turn Codex itself has not ended. Noted below as a separate question.
- *2, the grace imported, not copied.* `TAIL_GRACE` is one number for both observers; if it moves,
  both move. `codex.py` still changes nothing in `transcript.py`.
- *3.1, lineage by `forked_from_id`, not "include the timestamp and content in the key".* A fork's
  copy carries fresh timestamps and the fork's version (`recorder.rs` stamps each line as it is
  written), so content would never match a copy; the turn id is the only thing the copy keeps, and
  the lineage is the only scope in which it means the same turn.
- *3.2, a body-matched prefix, not "exclude `source_version` from the key".* For the same reason:
  the copy's `observed_at` differs too, so the key would not match with or without the version. The
  prefix rule is a guess, stated as one in the docstring, bounded to forks whose ancestor is
  unreadable and to the leading run of turns whose body is already in the log. Its cost (a fork's
  first new turn dropped if its body is exactly a logged turn) is the trade against the issue's
  duplicate. Reading the compressed file is the complete fix and needs an allowlist decision.
- *3.3, finish on abort, not "keep pending until the next turn".* Pending would hold a Ctrl-C'd turn
  back until the person spoke again, possibly never. Codex's reducer leaves the turn in progress only
  so a UI can show it; for an observation the abort is the end.
- *Round 1, blank message dropped in `_record`, not guarded in `_fold`.* The reviewer offered either.
  Dropping it where the empty reply is dropped keeps the rule in one place, and `_fold` then has no
  prompt record that is not speech, so the `spoke` flag needs no condition.
- *Round 1, an abort tail rather than an implicit turn.* Letting a late line open an implicit turn was
  the one place the fold made a turn Codex would not have, and the rollback count paid for it. The
  tail segment is what Codex's reducer does (items after `turn_aborted` stay in that turn). A late
  reply alone leaves it unspoken, so it is neither observed nor pending. Only if the person then
  speaks in it, it is answered, and it is ended does it become an observation of its own, which is
  what Codex itself records in that case; a real Codex opens the next prompt with `task_started`.
- *4, a whitespace-only reply dropped.* Codex drops only `is_empty()`. A reply of spaces would add an
  `assistant:` line with nothing after it; dropping it is the same observation minus a blank line.

**What to read first:** `_fold` in `codex.py` (the prompt branch and the `aborted` branch), then
`_lineage`, then the `copying` loop at the end of `append_observations`. The module docstring's
new *Forks, and a turn counted once* section carries the reasoning and the source citations.

**Noticed, not done:**

- Reading `.jsonl.zst` would replace the body-matched prefix guess with a real match by turn id.
  Needs zstd (not in the allowlist; stdlib only from Python 3.14). An owner's decision.
- An explicit turn left open in a quiet file (Codex killed before `turn_aborted`) is pending forever,
  the same shape as finding 2 one step over. Whether a quiet file should stand in for the marker is a
  D56 question, not taken here.
- A markerless file whose last turn has a prompt and no reply is pending forever, as a Claude Code
  prompt with no reply is. Nothing to observe either way.
- `forged_turn`'s docstring in `hazards.py` still says the observer may write a multi-block prompt;
  neither observer does now. A wording change when that module is next touched (#129 noted it too).
- `ForkPersistence::Referenced` forks (`history_base`, newer Codex) do not copy history at all; their
  files start with the fork's own turns, so the prefix rule stops at the first turn. Unverified.
- `RolloutFile.growing` defaults to `False` for a file built by hand (every reader sets it).

**Cost:** not available to the author from inside the run; to be filled from Hyperagent.
