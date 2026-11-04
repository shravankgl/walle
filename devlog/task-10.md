# Task 10: Transcript observer, part 1: the parentUuid DAG and its walk

Issue #8. Branch `task/10`.

**Shipped:**
- `src/walle/transcript.py` (new). It reads Claude Code's transcript files and returns their
  entries in graph order. It writes nothing; part 2 (task 11) turns turns into events.
  - `transcript_files(projects_dir)` lists `<projects_dir>/*/*.jsonl` (regular files only,
    sorted). Nothing deeper, so `<session>/subagents/agent-*.jsonl` is not included. A missing
    directory gives `[]`.
  - `read_transcript(path)` reads one file into `TranscriptEntry` values: the fields D101
    names (`uuid`, `parentUuid`, `type`, `timestamp`, `isSidechain`, `sessionId`, `cwd`,
    `version`, `agentId`), the path and line, and the whole parsed line as `raw` for part 2.
    Lines with no `uuid` (`summary`, `queue-operation`) go to `unthreaded`. Bad lines go to
    `skips` and are logged.
  - `load(paths)` reads every file and builds one graph over all of them, because a resume
    puts a child in one file and its parent in another. It removes duplicate uuids, repairs
    what cannot be walked (an orphan, an entry that is its own parent, a cycle) and reports
    each repair in `anomalies`.
  - `Transcripts.walk()` gives every entry once, depth first. A parent comes before its
    children, and each branch is walked whole before its next sibling.
- `tests/test_transcript.py` (new): 20 tests.
- `tests/fixtures/transcripts/-home-example-trip/` (new): two synthetic session files, an
  original session and a resume of it, described under *Verify*.

**Why:**
- **D51.** Phase 0 observes the harness transcript. This is the reading half of that.
- **D101, rule 1: order is the `parentUuid` graph, not timestamps.** Timestamps only put
  siblings and roots in order among themselves. A chain is never reordered by the clock.
- **D101, rule 2: carry `version`.** Every entry carries the build that wrote it (D96's era
  marker). Part 2 copies it onto each observation.
- **D101, rule 3: the session id changes on every turn.** It is carried on each entry and
  nothing keys on it. Duplicates are found by `uuid`, and the resume is found through
  `parentUuid`, not the session id.
- **D80, transcript side.** Skip bad lines and keep the recoverable history. This is looser
  than `reader.py`, which halts on a complete line it cannot read. That strictness protects
  our own log, where a skipped line would lose a ref. Here the file is Claude Code's.

**Verify:** PLAN §9: "fixture transcript with a resume and a sidechain → entries in DAG order,
not timestamp order".
- `test_a_resume_and_a_sidechain_walk_in_dag_order_not_timestamp_order`. The fixture:
  - Session S1 (`9f…01.jsonl`, version 2.0.1): entries 1 → 2 → 3 → 4, and a `summary` line.
  - Session S2 (`1a…02.jsonl`, version 2.0.2) resumes from 4. Its file name sorts *before*
    S1's on purpose. It starts with a replayed copy of 4 (same uuid, S2's session id). Then
    5 → 6, where 6 starts a background `Task`. The subagent's entries (7 → 9 → 11 → 12, all
    `isSidechain: true`) hang off 6, and the main thread carries on through 8 → 10 → 13 → 14.
    The two run at the same time, so their timestamps interleave. There is a
    `queue-operation` line too.
  - The walk is `1 2 3 4 5 6 7 9 11 12 8 10 13 14`. Sorting by timestamp gives `1 … 14`, and
    the test asserts that the two differ. It also checks that every parent comes before its
    children and that nothing is walked twice.
- Around it:
  - **load order:** the files passed in either order walk the same.
  - **resume:** the replayed 4 is kept once, from S1's file, and the copy is reported. 5's
    session id differs from 4's, and the chain still holds with one root.
  - **sidechain:** 7, 9, 11, 12 come together, right after 6, with `agent_id` carried.
  - **version, cwd and raw** are carried on each entry.
  - **unthreaded:** the `summary` and `queue-operation` lines are kept aside. They are not
    walked and not counted as skips.
  - **clock skew:** a child stamped before its parent still walks after it.
  - **rewind:** a fork walks each branch whole, the earliest branch first, even when the
    first branch's tail is later than the second branch's head.
  - **orphan, self-parent, cycle:** each becomes walkable and is reported. A ring is cut on
    the ring itself, not on an entry hanging off it.
  - **duplicates** that disagree about their parent report both parents.
  - **bad lines:** lines that are not JSON, not an object, not UTF-8, or not a valid entry
    (uuid not a string, a bad timestamp, `isSidechain: "yes"`) are skipped and reported by
    `file:line`, and the good lines around them load. An unterminated last line is pending,
    even when it parses.
  - **a file that vanished** between listing and loading is reported, and the rest load.
  - **discovery** finds `*/*.jsonl` only.
- I confirmed the tests failed before the module existed (collection error, no
  `walle.transcript`). The two cases added during the task were each confirmed failing
  against the code before their fix (see *Stranger pass*).
- Checks:
  ```
  uv run pytest              331 passed, 3 skipped
  uv run ruff check          All checks passed!
  uv run ruff format --check 43 files already formatted
  uv run pyright             0 errors, 0 warnings, 0 informations
  ```

**Two-runner walkthrough:** This task touches none of our logs, locks or derived files. But it
reads a file another process is appending to, so here are Claude Code C and this reader R:
1. C has written lines 1–9 and is part-way through line 10.
2. R reads the whole file in one `read_bytes`. Lines 1–9 end in a newline; line 10 does not.
3. R returns 1–9 and reports line 10 as "unterminated last line: pending".
4. C finishes 10. R's next read returns 1–10.

There is no lock, and none is needed: R never writes, and C only appends. If C dies in step 1,
line 10 is reported on every read, which is true. If C rewrote a file in place instead of
appending, R could read a mix of old and new bytes. That is format assumption 7 below.

**Stranger pass:** I re-read `transcript.py` as a stranger. It is IO plus graph repair, and
about 420 lines. Findings:
- **Cycle cut in the wrong place.** The first version cut the earliest entry nobody could
  reach. That entry might only hang off a ring rather than sit on it. It would be reported as
  a cycle, and the ring would be cut on the next pass anyway. Fixed: the code now climbs
  parents until one repeats and cuts the ring at its earliest member.
  `test_a_cycle_is_cut_on_the_ring_not_on_an_entry_hanging_off_it` fails on the old code.
- **One vanished file lost every file.** Claude Code deletes old transcripts on its own
  schedule, so a file can go between `transcript_files` and `load`. `FileNotFoundError` then
  stopped the whole load. Fixed: it is now reported as a skip with line 0, and the rest load.
  Any other `OSError` is still raised, because an unreadable file must not look empty.
  `read_transcript` on one named path still raises. The test fails on the old code.
- **Lax bool.** Pydantic's lax mode would read `"isSidechain": "yes"` as `True`. That field
  is now strict, and the line is skipped and reported.
- **A docstring that lied.** It said the duplicate winner was the file whose *first* entry is
  earliest. The code uses the file's earliest timestamp. I fixed the wording.
- I checked these and they hold: there is no recursion (the walk and the reachability check
  are iterative, so a deep chain cannot overflow the stack); every repair is logged and
  returned; nothing writes; and no `except` swallows anything without a skip.
- **A duplicate that agrees is returned but not logged.** Every resume produces them, so a
  warning each time would be noise. One that disagrees is logged.

**Review:** pending.

**Choices made inside the task:**
- **`extra="ignore"` on the entry model, not D88's `extra="forbid"`.** D88 is for our own
  events. A field added in a Claude Code release must not make every line unreadable. Only
  D101's fields are validated, and everything else stays in `raw`.
- **Sibling order: sidechain first, then timestamp, then file and line.** A subagent hanging
  off an entry is work that entry started, so it reads before the main thread's continuation.
  claude-code-log also orders siblings by timestamp ("timestamps are … used only to merge
  parallel chain segments, order sibling sessions").
- **No linearization ladder.** claude-code-log treats several multi-child shapes as
  recording artifacts and flattens them (compaction replays, parallel tool flows,
  continuation forks). Here every multi-child entry is walked as a plain fork. That is still
  DAG order, which is what this task's criterion asks for. Whether a fork is a rewind or an
  artifact decides which turns are "completed", so it belongs with part 2.
- **The duplicate winner** is the copy from the file with the earliest timestamp. This is
  claude-code-log's "earliest session" rule, and the result does not depend on the order
  paths are passed in.
- **The whole file is read at once.** It is simple and enough for part 1. Part 2 may want to
  read only what was appended since the last run.

**Format assumptions still to check against real files.** The fixtures are synthetic. They
are built from D101 and REFERRALS.md Gap 2 (claude-code-log `models.py`, `dev-docs/dag.md`),
not from a real transcript. Each of these should be checked against Shravan's own files
before part 2 depends on it:
1. **Layout.** Transcripts are `~/.claude/projects/<project>/<session>.jsonl`, one JSON
   object per line, UTF-8, `\n`-terminated.
2. **Subagent placement.** The fixture puts the sidechain inline in the session file, with
   the subagent's first entry's `parentUuid` pointing at the entry that started it. claude-code-log's
   `dag.md` says newer builds write subagents to `<session>/subagents/agent-*.jsonl`, with
   `parentUuid: null` on the first entry, linked back through `agentId`. Those files are
   outside the issue's `*/*.jsonl` glob. If that is the real shape, a subagent shows up as a
   separate root, or not at all. Which build writes which shape is unknown.
3. **Resume.** A resume writes a new file with a new `sessionId`. Its first new entry's
   `parentUuid` points into the earlier file. It may replay earlier entries with the same
   `uuid`.
4. **Timestamps** are ISO 8601 with `Z` or an offset. A naive timestamp is rejected, and its
   line is skipped.
5. **Lines without `uuid`** are only things like `summary` (with `leafUuid`) and
   `queue-operation`. Every conversational line has a `uuid`. Other types that do carry a
   `uuid` (`system`, `progress`, `attachment`, …) are walked as ordinary entries.
6. **Field types.** `isSidechain` is a real boolean and `version` is present on every entry.
   `version` is optional here, so an entry without it is still walked, with `None`.
7. **Writes are append-only.** Claude Code never rewrites a transcript file in place.
8. **`/compact`.** It writes a `system` / `compact_boundary` entry with `parentUuid: null`
   and a `logicalParentUuid`. Here that becomes a separate root, placed by timestamp, and it
   is not linked to the entry it continues.
9. **Compaction replays** (new uuids, same parent and timestamp) and **parallel tool-call
   siblings** really occur, as `dag.md` describes. Here they are walked as forks.

**What to read first:** `load()`, from the duplicate pass to the cycle loop, and the
sibling-order key `position`. Those decide the order that part 2 builds turns from. Then the
nine format assumptions above, because they are where this most likely breaks on a real file.

**Noticed, not done:**
- **Subagent files** under `<session>/subagents/` and the `agentId` link back to the
  spawning entry (assumption 2). This needs either a widened glob or a decision on scope.
- **`logicalParentUuid` across `/compact`** (assumption 8).
- **claude-code-log's linearization ladder** for recording artifacts (assumption 9). This is
  probably needed before task 11 can count completed turns honestly.
- **Tool-call / tool-result pair repair** (D80's second half). Pairs live in message
  content, so this belongs to part 2.
- **Incremental reads**, from an offset saved per file, for the observer's repeated runs.
- **No file under `tests/fixtures/transcripts/` says it is synthetic**, because JSONL cannot
  hold a comment. The test module's docstring and this log say it.

**Cost:** pending.
