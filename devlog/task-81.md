# Task 81: a Codex CLI transcript observer, built from public source

Issue #81. Branch `task/p2-codex`, from `main` at `e29426c` (task 70's inbox merge). Phase 2
(R8). Dormant: nothing runs unless the owner sets `WALLE_CODEX=on` and names a folder.

**The format is unverified.** The owner has no real Codex sessions. Every format claim below
was read from Codex's public source at `openai/codex` commit
[`388aee4b`](https://github.com/openai/codex/tree/388aee4b26226778701a7fa749354c44f74b4394/codex-rs)
(2026-11-05) and has not been checked against a real file. The module docstring repeats this
and cites each claim.

**Shipped:**
- `src/walle/codex.py`, new. Reads Codex rollouts into the same `Observation` events the Claude
  Code observer writes, so everything downstream (proposer, hazard checks, labels) stays
  harness-agnostic.
  - `enabled(environ)`, `SWITCH = "WALLE_CODEX"`, `CodexOffError`: the dormant switch. Off unless
    the variable is exactly `on`, the same one-spelling rule as `missions.enabled`.
    `observe_sessions` and `append_observations` raise before reading or writing anything while off.
  - `rollout_files(sessions_dir)`: `rollout-*.jsonl` under the folder the caller names, plus the
    `.jsonl.zst` files, which are listed so they can be counted. There is no default folder and
    nothing builds a home-directory path.
  - `read_rollout(path)` / `read_or_skip(path)`: one pass over the bytes. Each line is reduced on
    the spot to a `RolloutRecord` (kind, time, turn id, the person's text, a reply's text, a tool's
    name). Everything else on the line is dropped, so tool inputs, tool outputs, reasoning, injected
    context and `base_instructions` never exist past the parser. Bad lines become `RolloutSkip`
    (line, offset, length, a fixed reason) with no bytes from the line. A changed format, a
    compressed file or a vanished file becomes one whole-file skip with line 0.
  - `observe(files, ts)`: Codex's own turn reducer, copied (see *Why*), then one observation per
    completed turn. A fork's copy of a turn, matched by Codex turn id, counts once.
  - `append_observations(report, writer)`: the D92 fold and append under the stream lock, as in
    `observer.append_observations`, but counting only `source == "codex"`.
- `src/walle/cli.py`: `walle observe-codex --root --principal --device --sessions-dir`. Refuses
  while off (exit 1, says how to turn it on, creates nothing). `--sessions-dir` is required, with
  no default and no envvar. Prints counts only: turns seen, appended, already logged, pending,
  skipped lines, skipped files, duplicate turns.
- `tests/fixtures/codex/`: a synthetic bakery session twice over, as a Codex rollout under
  `sessions/2026/11/02/` and as the equivalent Claude Code session under `claude/`. The rollout
  carries canary strings in every place that must never be read: tool arguments, tool output,
  reasoning, `<environment_context>`, `base_instructions`.
- `tests/test_codex.py`, 50 tests.

**Why:**
- D51 observes the harness transcript itself. Codex's rollout is that transcript for Codex. D101's
  rules carry over: order is the file's order and Codex's own turn markers, the session id is
  never a key, and `source_version` is the Codex build (`session_meta.cli_version`).
- D56: only a completed turn is observed. "Completed" uses Codex's markers (`task_complete`,
  `turn_aborted`) plus Claude Code's rule 1 (the person moved on).
- D92: no cursor file. What's already logged is a fold over the stream, rebuilt each run.
- D55 and D77: Codex text gets the same hazard flags and labels because it is an `Observation`
  like any other. The proposer reads every observation whatever its source, and `labels` marks
  every observation untrusted. Two tests prove this rather than assume it.
- D40: no default path. The owner names the folder each time.
- The turn rules copy `ThreadHistoryBuilder`
  (`codex-rs/app-server-protocol/src/protocol/thread_history.rs`), Codex's own reducer from
  rollout to turns. Copying the vendor's reducer is less of a guess than inventing one.

**Verify:**
- *Synthetic rollout fixtures become the same observations as an equivalent Claude Code session:*
  `test_a_codex_rollout_gives_the_same_observations_as_the_equivalent_claude_code_session`.
  `observe_sessions` on the bakery rollout and `observe_projects` on the bakery Claude session give
  equal `(type, actor, ts, observed_at, body)` for both completed turns, and one pending turn each.
  `source` differs (`codex` against `transcript`) and so does `source_version`. That is
  deliberate (see *Choices*).
- *Re-running adds no duplicates (D92):* `test_a_second_run_appends_nothing` (byte-identical
  stream), `test_a_turn_that_completes_later_is_appended_once_then`,
  `test_two_runs_at_once_append_each_turn_once` (two threads, two writers, one lock),
  `test_the_command_appends_then_appends_nothing`.
- *Malformed lines, unknown record types and a changed format version are skipped and counted,
  with no transcript bytes in the skip record:*
  `test_malformed_lines_are_skipped_counted_and_the_rest_still_read` (eight kinds of bad line, each
  with a `SECRET` marker, none in any `repr(skip)`),
  `test_a_changed_format_version_skips_the_whole_file_and_counts_it` (`paginated`, an unknown
  string, a number, null), `test_a_compressed_rollout_is_counted_not_read`,
  `test_a_file_that_vanishes_before_it_is_read_is_counted`.
- *Only counts are printed:* `test_the_output_is_counts_and_holds_no_transcript_text`. Every stdout
  line must match the counts pattern, and the `SECRET` marker is in the folder name, the file name,
  the prompt, the reply, a skipped line and a `history_mode`.
- *Rules:* `test_observe_sessions_has_no_default_folder`, `test_the_module_never_names_the_home_folder`
  (AST: no `home`, `expanduser`, `.codex` or `HOME` in code),
  `test_the_sessions_dir_option_is_required_with_no_default_and_no_envvar`,
  `test_tool_output_tool_input_reasoning_and_request_bodies_never_reach_an_observation`,
  `test_codex_text_meets_the_same_hazard_flags_and_labels_as_claude_code_text`,
  `test_the_proposer_withholds_codex_text_with_a_hazard`,
  `test_the_hook_and_the_claude_code_observer_do_not_reach_codex`.
- *Dormant, default off:* `test_the_codex_observer_is_off_by_default`,
  `test_only_the_one_spelling_turns_it_on`, `test_while_off_it_reads_nothing_and_writes_nothing`
  (the readers are patched to fail if called), `test_while_off_the_command_refuses_before_touching_anything`.

Written first: with no `walle.codex`, `tests/test_codex.py` failed at import. Then four tests
failed on my own test code, not on the module: two unpacked an `AppendReport` as a tuple, and two
called `read_stream` with the wrong arguments. The stranger pass added one more test, which failed
before its fix (below).

```
uv run pytest               2189 passed, 10 skipped in 68.96s
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** two kinds of pair.
- *Codex C writing a rollout, this reader R.* C has written lines 1-9 and is part-way through
  line 10. R reads the file once with `read_bytes`. Line 10 has no newline, so it is a pending
  skip, counted, and its turn stays pending. C finishes line 10, and R's next run reads it whole
  and observes the turn. R never writes a rollout. C only appends (`JsonlWriter` writes a line then
  flushes), so R never sees a rewritten middle. If Codex's compression worker replaces the file
  with `.jsonl.zst` between `rollout_files` and the read, the plain file is gone: it becomes a
  "vanished" whole-file skip, and the next run counts it as a compressed file.
- *Two `observe-codex` runs A and B on one device.* Both read the rollouts and find turn T. A takes
  the stream's `LOCK_EX`, folds the stream (no Codex observation with T's key), appends T, fsyncs
  and releases. B, blocked on the lock, then folds the stream, finds T's key, and appends nothing.
  This is the same sequence as `observer.py`'s walkthrough, because it is the same lock and the
  same content key. On two devices, A and B write different streams. Both may append T, and the
  merged log holds two observations of T. That is the same limit as the Claude Code observer, and
  the same content key makes the pair recognisable.

**Stranger pass:** done on the whole diff, which is over 30 lines and is IO plus error paths.
Found and fixed:
- **A timestamp Python parses but the log refuses could sink the whole run.**
  `datetime.fromisoformat` accepts a seconds offset (`+05:30:15`). The observation's `observed_at`
  would then fail the `Instant` validator inside `observe()`, which is a `ValueError` for the whole
  run, not a skipped line. That breaks D80. Times are now converted to UTC as they are read (Codex
  writes UTC anyway), and a time that overflows on conversion is a counted skip.
  `test_an_odd_offset_or_an_impossible_time_cannot_sink_the_run` failed before the fix.

Looked for and not found:
- A skip reason built from line content. Every reason is a literal, and the unknown type and the
  `history_mode` value are never echoed. One test plants the marker in both.
- A stream touched with nothing to append. It isn't touched, and a test checks the file is not created.
- The switch checked after a read. It is checked before `rollout_files` and before the lock. The
  CLI checks it before constructing the writer.
- A lock held across the rollout reads. It isn't: reading is done before `append_observations`
  takes the lock, as in `observer.py`.
- A status count that lies. "turns seen" is observed plus pending, as for Claude Code. Dropped
  empty turns and duplicates are not "seen" turns, and duplicates have their own line.

**Review:** independent review is the reviewer agent's, on this PR, per the brief. There was no
`scripts/review.sh` run, because the brief replaces it for agent work.

**Choices made inside the task:**
- **`history_mode` is the format version.** The issue asks that "a changed format version" be
  skipped and counted, and Codex rollouts carry no format number. `history_mode` is what Codex
  itself switches persistence on (`policy.rs`), so it is the honest marker. `legacy` (or absent)
  is read. `paginated`, or anything unknown, skips the whole file. I chose not to flag this: the
  public source is clear on what each mode writes. Reading `paginated` files (via `item_completed`
  turn items) is a follow-up, not a guess to make now.
- **`source = "codex"`, not `"transcript"`.** "The same observations" is taken as the same turns,
  bodies, times and actor. Keeping the harness in `source` lets each observer's D92 fold count
  only its own, so neither can hide the other's turns (`test_claude_code_observations_in_the_same_
  stream_are_left_alone`). It also changes nothing in what the Claude Code observer does. If the
  owner wants one source for both, it is a one-word change plus a fold decision.
- **The person's words come from `event_msg.user_message`, not `response_item` messages.**
  User-role `response_item` messages also carry Codex's injected `<environment_context>` and
  instructions, which are not the person. Assistant `response_item` messages repeat the
  `agent_message` text. Reading only the events gives one clean copy of each.
- **Tools by name only**, as in Claude Code's `[tool: <name>]`. A call with no name field
  (`local_shell_call`, `web_search_call`) is named by its kind. One with an empty name is `unnamed`.
- **An unknown `event_msg` or `response_item` kind is passed over, not counted.** Codex has about
  ninety event kinds and adds more. Counting them would make "skipped lines" noise and hide real
  breakage. Unknown *record* types (the thirteen top-level kinds) are counted, as the issue asks.
- **Compressed rollouts are counted, not read.** zstd needs a dependency outside the allowlist.
- **The CLI refuses while off instead of printing zero counts.** Printing zeros would read as
  "nothing to observe", which would be a status message that lies.

**What to read first:** the module docstring of `codex.py` (the format claims and their sources),
then `_record` and `_fold` (about 120 lines: what is kept and the turn rules), then
`test_a_codex_rollout_gives_the_same_observations_as_the_equivalent_claude_code_session` and
`test_malformed_lines_are_skipped_counted_and_the_rest_still_read`. When the owner has a real
Codex session, running `WALLE_CODEX=on walle observe-codex --sessions-dir <copy of sessions>` on a
scratch root and comparing the counts is the first real check.

**Noticed, not done:**
- **`paginated` rollouts** (`item_completed` with `TurnItem::UserMessage` / `AgentMessage`) are
  skipped whole. Newer Codex threads may default to it. Supporting it is its own task, ideally
  against a real file.
- **A rolled-back turn already appended on an earlier run stays in the log.** Append-only means it
  cannot be withdrawn here. A `supersede` from the observer would be the honest answer, and that is
  a decision, not a fix.
- **A fork's copy of a turn is counted once within a run, by turn id. Across runs**, if the fork
  file appears later with copied lines re-timestamped, its copy has a different `observed_at` and
  would be appended. Whether Codex re-timestamps copied history is not stated plainly in the source.
- **A tool name with a newline in it** would put a second line into the body. This is the same as
  Claude Code's `[tool: <name>]`. The proposer's forged-turn hazard flags such a body before any
  model sees it. Parity was kept rather than diverging silently.
- **`rglob` and symlinked folders inside the sessions folder:** behaviour follows Python 3.12's
  `pathlib` and was not pinned by a test.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.
- **Issue #80 (Opik exporter)** was flagged `needs-shravan` in this same run. It needs
  `tests/test_constraints.py` (CODEOWNERS) to refuse `opik` imports. An empty branch
  `task/p2-opik` was created before that was found, and the issue comment says so.

**Cost:** not available to the author at write time. Hyperagent's figure for this thread is to be
filled in by the owner.
