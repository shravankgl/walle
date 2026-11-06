# Task 129: Audit — the observer can lose, repeat or mislabel turns

Issue #129, the second read-only audit (Gemini 4 Argon at main `8af004b`), four findings on the
transcript observer. The issue said the findings were not independently re-run, so a failing test
came first for each. **All four failed on main before any fix**, so all four were fixed.

**Shipped:**

- `src/walle/transcript.py` — part 1 of the observer, the file reader and graph builder. Two
  repairs added to `load()` for a lost line inside a chain (finding 2.1), and a `growing` signal
  for a file whose torn last line is still being written (finding 2.3). `TranscriptFile` gains
  `links` and `growing`; `Transcripts` gains `growing`; `Anomaly.kind` gains `bridged`.
- `src/walle/observer.py` — part 2, turns and bodies. Rule 2 waits for a growing file
  (finding 2.3); `_body` reads a message's text snapshots once (finding 2.2) and writes the
  prompt as one `user:` block (finding 2.4).
- `tests/test_transcript.py` — the graph-level cases: an orphan behind a torn line, an orphan with
  no gap (stays a root), a known link through an unthreaded, a skipped and a payload line, two lost
  lines in a row, a lost root, and the growing/stale tail.
- `tests/test_observer.py` — the turn-level cases, one or more per finding, plus the idempotence
  run for 2.3 (pending → appended once → re-run appends nothing).
- `devlog/task-129.md` — this file.

Not touched: `hook.py`, `writer.py`, `reader.py` (#128 is in them), `hazards.py`, `cli.py`.

**Why:**

- **D56** — the unit of observation is a completed turn, never a token, and a message is stored
  whole. 2.2 wrote every streaming snapshot of a text block into the body; 2.3 observed a turn
  whose file was still being written. Both are the turn observed before it was whole.
- **D80** — a bad line never loses the rest of a file, and a trailing line being written is
  pending, not corrupt. 2.1 skipped the bad line correctly and then lost every turn below it
  anyway, because the next entry became an orphan root the observer never looks at. The repair
  keeps D80's promise one step further: the *chain* survives the bad line, not only the lines.
- **D92** — "already logged" is a fold over the log by content, with no cursor. That fold cannot
  tell a short observation of a turn from a longer one of the same turn, so the observer must not
  emit the short one. 2.3's fix is about keeping D92's content key sufficient.
- **D51, D40** — the source is the harness transcript, read from a folder the person names;
  nothing here changes that, and no default path was added (`test_no_observer_module_names_a_home_folder`
  still passes).
- No D-number was changed or contradicted, so `flag` was not used. D80 says an unterminated last
  line is pending; the new `TAIL_GRACE` refines *how long* a tear is read as a write in progress,
  and the skip's reason says which reading was made.

**Verify:** a failing test for each finding first, then the right result, and nothing written to
the log on a refusal (a pending turn).

| Finding | Test that failed on main | What it proves now |
|---|---|---|
| 2.1 corrupt line in a turn | `test_a_corrupt_line_inside_a_turn_does_not_lose_the_turn`, `test_an_orphan_behind_a_line_that_was_not_loaded_hangs_off_the_entry_before_it` | the turn is observed whole, closing at the reply below the gap; the `orphan` anomaly is still reported, now naming where the entry was hung |
| 2.1 unthreaded / skipped-but-parsed line | `test_a_line_kept_aside_or_skipped_inside_a_turn_still_links_the_chain[...]`, `test_a_known_link_through_a_line_that_is_not_an_entry_is_followed[...]`, `test_two_lost_lines_in_a_row_are_bridged_through_both`, `test_a_link_to_a_lost_root_makes_a_root_not_an_orphan` | the entry below is hung from the lost line's own parent (`bridged`), through several lost lines if needed; a payload line's content still reaches nothing |
| 2.1 guard | `test_an_orphan_with_no_gap_before_it_is_still_a_root`, `test_a_turn_after_a_corrupt_line_is_still_its_own_turn` | the guess is made only where a line was skipped, and the repair joins a chain, never two turns |
| 2.2 streaming prefixes | `test_a_growing_prefix_of_one_message_is_read_once_at_its_full_length`, `test_a_snapshot_before_a_tool_call_in_the_same_message_is_read_once` | the body holds the last snapshot only |
| 2.2 guard | `test_a_prefix_in_another_message_is_not_a_snapshot` (passed on main, kept as the fence) | two messages are never folded together |
| 2.3 torn tail being written | `test_a_final_reply_at_the_end_of_a_file_being_written_is_pending_until_the_tail_lands` | pending, stream not even created; once the tail lands, appended once; a third run appends nothing |
| 2.3 not forever | `test_a_torn_tail_nobody_is_writing_does_not_hold_a_finished_turn_back`, `test_a_file_with_a_fresh_torn_tail_is_growing_and_a_stale_one_is_not` | a tear older than `TAIL_GRACE` (300 s) holds nothing back, and its skip reason no longer says someone may be writing it |
| 2.3 scope | `test_a_growing_file_holds_back_only_the_turn_its_tail_can_reach` | rule 1 still closes a turn the person moved on from; another file's turn is untouched |
| 2.4 multi-line prompt + second block | `test_a_multi_line_prompt_with_a_second_block_is_one_user_block` | body is `user: Line 1\nLine 2\n[image]\n…` and `hazards.flag` returns `()` |

One existing expectation changed because the task required it:
`test_prompt_content_as_blocks_reads_text_and_marks_images` asserted the old per-block layout
`user: what is this?\nuser: [image]`; it now asserts `user: what is this?\n[image]`. Nothing else
in the suite moved. The auditor's own four reproductions were run on main (all four reproduced) and
after (all four give the expected result).

Checks, at the final commit:

```
uv run pytest               4234 passed, 35 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  206 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** Claude Code C appending to `s.jsonl`, the observer R reading it; the
stream lock story for two observers A and B is unchanged from the module docstring.

1. C has written `p1`, `a1` (`end_turn`) and is part-way through the next line, a continuation of
   the same turn. `mtime` is now.
2. R reads the file once: `p1`, `a1` end in a newline; the tail does not. `read_transcript` stats
   the file after the read: changed within 300 s, so `growing=True`, skip reason "pending, Claude
   Code may still be writing it". `Transcripts.growing = {s.jsonl: a1}`.
3. R's `_closing(p1)` walks `a1`, finds it in `open_ends`, sets `may_grow`. Rule 1 has no later
   prompt. Rule 2 is refused. `p1` is pending; `append_observations` with no turns does not take
   the lock, and does not create the stream. (Before: `a1` closed the turn and
   `user: q\nassistant: a` was appended.)
4. C finishes the line (`a2`, `end_turn`) and goes idle. R's next read: no tail, nothing growing,
   `a2` closes the turn, `user: q\nassistant: a\nassistant: and b` is appended once. A third run
   finds it in the fold and appends nothing. (Before: the second run's key differed from the
   first's, and the turn was in the log twice.)
5. If C instead dies in step 1: the tear stays. For 300 s R reads it as growing and `p1` stays
   pending, on every run. After that `read_transcript` sees `mtime` older than the grace,
   `growing=False`, the reason says "the file has not changed for over 300s, so nothing is writing
   it", and `a1` closes the turn: observed once, with what was written. A race in the other
   direction (R stats the file after C wrote more) only makes R read it as growing one more run,
   the safe side.
6. If C's tail is the person's *next prompt* rather than a continuation: once it lands, rule 1
   closes `p1` at `a1`, the same closing and body rule 2 would have produced; and had rule 2
   fired early, the content key would have matched. The hold costs one run of delay there and
   prevents a duplicate in the continuation case.

For finding 2.1 there is no second writer: the repair is a view over bytes already on disk, made
the same way on every read, so two observers see the same graph.

**Stranger pass:** the diff is over 30 lines and touches IO and an error path. Found and done:

- `before_gap` first counted every unthreaded line as a gap. A line kept aside *without* a uuid
  (`summary`, `queue-operation`) cannot be anyone's parent, and those are common mid-file, so an
  orphan whose parent was really in a deleted file would have been glued to an unrelated neighbour.
  Now only skipped lines count, and the scan uses `bisect` rather than a quadratic `any`.
- `_open_ends` first picked "the entry with the highest line in a growing path" from the kept
  entries. A replayed entry is kept from whichever file sorts first, so the growing file's last
  entry could have been attributed to another path and missed. `load()` now records the last entry
  from each `TranscriptFile`'s own order, in `Transcripts.growing`.
- `_changed_within` swallows `OSError` from `stat` and answers "not growing": a file deleted
  between read and stat has no writer. That branch is not a silent failure; the file's entries are
  still loaded and the next listing will not find it.
- Status messages: the skip reason for a stale tear no longer says Claude Code may be writing it.
  The `orphan` detail says where the entry was hung; `bridged` says how many lost lines were
  crossed and where the trail ended.
- Secrets: `links` holds two identifier strings per lost line, capped at 128 characters each, and
  for a payload line is read from the parsed object *before* `kept` is replaced by `{}`. The
  existing canary test over every string in `TranscriptFile` and `Transcripts` still passes, and
  the new payload-line test runs the same walk. No tool output, thinking, tool input or
  `api-request` content reaches a body: `_part` returns `None` for them as `_line` did.
- Lock scope: unchanged. `append_observations` still folds and appends under one `LOCK_EX`.

**Review:** to be done by the reviewer agent on the PR's head commit (a different vendor's model,
per the brief). Not reviewed by the author's own `scripts/review.sh` run, which the agent build
period replaces.

**Choices made inside the task:**

- *2.1, two repairs rather than one.* The auditor suggested bridging through unthreaded lines and
  attaching behind corrupt ones. Bridging is also applied to a skipped line that parsed (a user
  line with a bad timestamp): its link is as good as an unthreaded line's, and better than a
  guess. The attach guess is made only when a line was actually skipped between the orphan and
  the entry before it in the same file; with no gap, the orphan stays a root as before.
- *2.1, the limit of the guess.* If the unreadable line was the person's own prompt, the reply
  below it now joins the turn above instead of being lost. The prompt was unreadable either way,
  and the alternative (treat every gap as a possible prompt and close the turn above at the gap)
  would truncate the far more common case, a torn tool result, to a dangling `[tool: …]`. The
  anomaly names the gap so a reader can see it.
- *2.2, a prefix rule rather than "skip `stop_reason: null` entries".* The suggested fix would
  have removed disjoint fragments that `test_fragments_alone_never_become_an_observation` and the
  parametrised deltas test have read into the body since task 11, i.e. it would have needed those
  tests edited. A block that a later entry of the *same message* holds a longer version of is
  superseded; a disjoint piece is not. Both shapes now read correctly, and the rule is scoped to
  one message id so two replies that start alike are never merged. The cost: a genuine disjoint
  piece that happens to be a strict prefix of a later piece of the same message (`"the "` then
  `"the end"`) would be read once. That needs token-level fragments on disk, which nothing has
  shown.
- *2.3, an age rather than a flag alone.* "Pending while the tail is unterminated" alone would
  hold a crashed session's last turn back forever, which the issue's owner asked be checked.
  `TAIL_GRACE = 300` s: Claude Code writes a line in one `write`, so a tear minutes old is a
  crash. Too short a grace observes a partial turn during a long pause; too long delays a crashed
  turn by that much. Five minutes is well clear of the first and cheap on the second. A paused
  writer (laptop asleep mid-line) that resumes after the grace produces the pre-existing
  "second, longer observation" the module docstring already names as the content key's limit.
- *2.3, pending on the open end, not on the closing entry.* The tail hangs off the file's last
  entry, which may be a `system` or `attachment` line after the closing reply. A turn is held if
  its walk reaches that entry, not only if its closing is that entry.
- *2.4, fixed in the observer, not in `hazards`.* `forged_turn`'s docstring records the choice from
  PR #50 to flag this layout and accept the cost. Changing the gate to allow a `user:` label after
  an unlabelled line would re-open the forged-turn hole; writing the person's blocks as one block
  removes the cost without touching the gate. `proposer.person_lines` already reads unlabelled
  continuation lines as the person's. `hazards.py` is unchanged.

**What to read first:** `load()` in `transcript.py` from the `links` / `before_gap` comment to the
end of the `parent_of` loop, then `_closing` and `_body` in `observer.py`. The module docstrings
carry the reasoning for each.

**Noticed, not done:**

- `codex.py` writes `user: [image]` per attached image after the prompt's text
  (`tests/test_codex.py:485`), the layout finding 2.4 removes from this observer. A multi-line
  Codex prompt with an image would still be flagged `turn`. Same fix, separate module (#81's).
- `forged_turn`'s docstring still describes the multi-block layout as something the observer may
  write. True of `codex.py`, no longer of `observer.py`; a wording update when that module is next
  touched.
- The module docstring's "a turn that changes after it was observed gets a second, longer
  observation" remains the content key's limit. Carrying the prompt's `uuid` on the event (a schema
  change, D88/D96) would close it for good.
- `_LINK_LENGTH` (128) and `TAIL_GRACE` (300 s) are constants, not configuration. Nothing has asked
  for them to vary.

**Cost:** not available to the author from inside the run; to be filled from Hyperagent.
