# Task 128: Audit — an interrupted append can permanently damage the log

Issue #128, audit findings 3.1, 3.2, 3.3 (with Audit 2's finding 6), 4.1, and the auditor's
walkthrough of `_check_causes`. Branch `task/audit-log`, created empty by the owner as the claim
and reused here as his comment on the issue says; from `main` at `8af004b`. Author:
walle-author-fable (Fable 5.1). Every reproduction in the issue was re-run against `8af004b`
first and **all of them held**; each is now a test that fails there and passes here. One half
of one finding (3.2's reader shape) reproduces but is not fixed, deliberately — see *Choices*.

The property the owner asked for, in one sentence: **no sequence of crashes, full disks or torn
writes leaves a stream that cannot be appended to or a log that will not rebuild, and an
unacknowledged event never comes back as acknowledged.** The reader's rules are unchanged. The
writer's two rules changed, and the two modules are now documented as one protocol.

**Shipped:**

- `src/walle/writer.py`:
  - `TORN_MARK = b"~"`, new and public. A heal now begins with this byte, then a newline, then
    the record — never with a bare newline. The byte is one no JSON value can end in and is not
    whitespace, so a torn line stays unparseable however the heal is then cut: after the mark
    alone (still a torn tail), after the newline (a complete non-JSON line the reader skips), or
    inside the record (another torn tail, healed the same way). This closes 3.2's second half,
    the resurrection: before, a heal cut after its first byte turned a complete-but-for-its-
    newline record into a terminated one, the reader loaded it, and the next writer built on
    its `seq`.
  - `_last_record(f, size, path)` replaces `_tail`. It reads backwards from the end and steps
    back past blank lines and complete lines that are not JSON — the reader's rule (D80) — to
    the last line that is a record, widening the window until it finds one or reaches byte
    zero. A heal cut after its newline leaves a non-JSON line *last*; the old `_tail` handed it
    to `_seq_of`, which raised `CorruptLogError` on every append thereafter while the reader
    loaded the stream fine (3.1). It warns once when it had to step past something.
  - `_seq_of(record, path)` takes the decoded line (decoded once, not twice). It still refuses,
    with `CorruptLogError`, valid JSON that has no usable `seq` — the reader halts on the same
    line with `CorruptStreamError`, so the two agree on what is fatal.
  - `_plan(f, size, path, event) -> (payload, seq)`: the one place the heal and the next `seq`
    are decided, reading only. `LockedStream.append` calls it and writes the bytes; the
    exhaustive tests call it over a `BytesIO`, so the model under test is the code that runs.
  - `StreamWriter.append`: when the event has causes and the stream file does not exist yet,
    the causes are checked *before* `locked()` creates the directory and file (3.3, Audit 2's
    6). A stream that does not exist holds nothing, so any cause on it is missing. The check
    under the lock still runs afterwards; this one only decides whether anything gets created.
  - `_refuse_dangling(event, stream, path, *, held_here)`: the cause check, pulled out of
    `LockedStream._check_causes` so the pre-create path and the locked path are one function.
  - `_refs_of`: the lockless sibling read now `fsync`s the sibling through the descriptor it
    read from, after the read and before the parse. What we are about to cite is on disk before
    our citation can be. No lock is taken, so the deadlock the lockless read exists to avoid
    stays avoided.
  - Module docstring: a new section, *How a tear is healed, byte by byte*, states the two writer
    rules and why; the `_check_causes` section walks writers A and B through the un-`fsync`ed
    sibling read in five numbered steps (the walkthrough the owner asked for).
  - `CorruptLogError` docstring: now says what it is for (JSON with no usable `seq`) and what it
    is no longer for (a non-JSON line, which is a fragment).
- `src/walle/reader.py`: docstring only. Rule three (a complete record with no newline shares
  its `seq` with the very next line) now says it is the shape a heal left *before* this change,
  kept for those streams (D88), and why adjacency is still required. No code changed.
- `src/walle/events.py`: `Event._upcast` refuses `v` that is a `bool` or below 1 with
  `v must be a JSON integer >= 1`, the rule `reader._record` already applied to `seq` (4.1).
  Before, `true` passed as 1 and `0` or `false` blamed a missing upcaster.
- `tests/test_audit_log.py`, new: 29 test functions, 31 cases. Per finding: the reproduction,
  the cases around it, one seam test through the real `index` (rebuild and update after a double
  crash; no phantom stream listed after a refusal). For the sibling read: the sibling's inode is
  `fsync`ed while our file is still at its old size; a sibling record without its newline is
  refused; two writers citing each other's streams, one holding a session open, both finish.
  Then the exhaustive section: an append cut at every byte offset, then a heal at every offset
  after every one of those, then a heal of a heal at every offset, a complete record cut only at
  its newline then healed at every offset, a torn write that leaves NUL bytes, and a test that
  runs the real `StreamWriter` on a real file at every depth-one cut and at every cut of a heal
  (a wrapper that fails `write` with `ENOSPC` after *n* bytes, which is what a full disk does)
  and checks the bytes equal the model's. Every state is checked against the real reader
  (loads; records are exactly the acknowledged events) and the real planner (next `seq` is one
  past the last record).
- `tests/test_writer.py`, edited (the task requires it; see *Choices*): the `unparseable` case
  left `test_a_corrupt_complete_line_refuses_rather_than_guesses` and became
  `test_a_complete_line_that_is_not_json_is_stepped_past_as_the_reader_skips_it`; a
  `not-an-object` case (`[1]`) joined the refusals. The file's header table says the new rule.

**Why:**

- **D80 — a bad line must never brick a stream.** 3.1 is D80's own failure mode arriving by a
  side door: the reader tolerated the line, the writer did not, and the result was a stream that
  loads and can never grow — forever, because nothing truncates (hard constraint 2). The fix is
  not tolerance in the writer; it is the writer using the reader's definition of a record.
- **D8 / hard constraint 2 — append, never mutate.** The heal still leaves every torn byte where
  it is. `TORN_MARK` is appended to the torn line, not written over it; the exhaustive tests and
  `test_3_2_the_torn_record_s_bytes_are_kept_not_erased` hold that.
- **D88 — old events must always load.** The reader's code is untouched, so every golden log
  under `tests/fixtures/v1/` replays unchanged (`test_golden.py` is in the suite), the
  `torn_tail.jsonl` fixture loads as before, and a stream healed by the old writer (bare newline,
  adjacent repeated `seq`) loads and is continued correctly
  (`test_3_2_a_log_healed_by_the_old_writer_still_loads`). I also ran `main`'s `reader.py` over
  a file the new writer healed twice: same three records, same two skips.
- **D111 — the fold refuses a dangling ref.** The auditor's race: B reads A's record from the
  page cache before A's `fsync`, cites it, `fsync`s its own stream, and power goes. `d-phone:1`
  is durable; `d-mbp:45` is not; `index.update` raises `DanglingRefError` for the rest of the
  log's life. `fsync` on the sibling's descriptor flushes every dirty page of that inode, so
  the cause is durable before our write begins. Also 3.3: `DanglingCauseError` says it is raised
  before anything is written, and now that includes the file and directory.
- **D10 — the file is for a person with `cat`.** `~` rather than NUL for the mark: a NUL byte
  makes `grep` call the whole stream binary.

**Verify:** the issue's criterion — each reproduction a failing test first, then the right
result, nothing written on a refusal.

Before (`tests/test_audit_log.py` at this commit, `src/` at `8af004b`): `23 failed, 8 passed`.
The eight that pass on `main` are guards for properties it already had (JSON with a bad `seq`
still refuses; torn bytes are kept; the 3.2.1 shape halts; an old-writer heal loads; a cause
that exists creates the stream; a refusal on an existing stream writes nothing; `v: 1` and a
missing `v` load; two writers citing each other do not deadlock). One existing test failed on
the new writer: `test_a_corrupt_complete_line_refuses_rather_than_guesses[unparseable]`, which
asserted exactly 3.1's bug.

The exhaustive tests cannot run against `main` (no `_plan`), so I ran the same model with
`main`'s `_tail`/`_seq_of` and bare-newline heal: of ~34,600 depth-two states, **33,306 raise
`CorruptLogError`** (every heal cut after its newline and before its own) and **183 resurrect
the unacknowledged record** (a record cut at its newline, then the heal cut anywhere after its
first byte). The new writer passes every one.

After:

```
uv run pytest               4247 passed, 35 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  207 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** this task is one. Three are in the code; the first two are in
`writer.py`'s module docstring and summarised here.

*A and B on one stream, A dies mid-append.* 1. A holds `LOCK_EX`, writes part of seq 45, dies;
the kernel drops its lock. 2. B wakes, seeks to the end, finds no trailing newline: torn.
3. B steps back past the fragment (it is not newline-terminated, so it is not a line) to 44,
plans seq 45, and writes `~`, `\n`, then its record, flushes, `fsync`s. If B dies after the
`~`: the tail is `<fragment>~`, torn, and C heals it the same way. If B dies after the `\n`: the
last complete line is `<fragment>~`, not JSON; the reader skips it, and C steps back past it to
44 and writes 45 *without* a heal prefix, since the file ends in a newline. If A's fragment was a
whole record but its newline: `<record>~` is not JSON either, in every one of those states, so
A's 45 is never loaded and B's 45 is the only 45. 4. The reader, taking `LOCK_SH`, sees either
nothing of B or all of B.

*A on `d-mbp`, B on `d-phone` citing A.* 1. A holds `LOCK_EX(d-mbp)`, writes 45, flushes; not yet
`fsync`ed. 2. B holds `LOCK_EX(d-phone)`, opens `d-mbp` read-only without its lock, reads: 45 is
there, newline-terminated. 3. B `fsync`s that descriptor; 45 is on disk when it returns. 4. B
writes `d-phone:1` citing `d-mbp:45`, `fsync`s. 5. Power is lost at any point: there is no
state with `d-phone:1` on disk and `d-mbp:45` not. If in step 2 A was still mid-write, B sees a
torn tail, 45 is not a record, B raises `DanglingCauseError` with nothing written. Neither
writer ever asks for the other's lock, so neither waits on the other; `flock` does not gate
`fsync`.

*A refused on a stream that does not exist, B creating it.* 1. A's `append` with a cause sees no
file, checks the causes, is refused: nothing created. 2. Meanwhile B's `locked()` creates the
directory and file and writes seq 1. 3. If A's `exists()` ran before B's create and A was *not*
refused, A proceeds into `locked()`, blocks on B's lock, and checks its causes again under the
lock against what B wrote. The pre-create check only ever decides whether to create; the locked
check decides whether to write.

**Stranger pass:** the diff is ~230 lines over three source files and touches IO, locking and
every error path in the writer, so the re-read was done. Found and fixed: (1) the last line was
JSON-decoded twice, once to find it and once for its `seq` — a snapshot body is large; it is
now decoded once and `_NO_RECORD` is a sentinel because `null` is a JSON line too. (2) The
`passed` counter for the warning accumulated across widening passes, which rescan the same
lines; it is now per pass. (3) The test helper that patches `open` undid itself in a roundabout
way; it now uses `monkeypatch.context()`. Considered and left: the pre-create `exists()` check
is a read outside the lock, which the file's own comments warn against — it is sound because
it only decides whether to *create*, and the authoritative check runs under the lock
regardless; the comment says so. Also left: `_refs_of` `fsync`s the sibling even when the
caller is only pre-checking and will not write (a stream that does not yet exist) — one
`fsync` on a first append with a cross-stream cause, for one code path instead of two.
Checked for status messages that lie: the torn warning now says "marking it", the
`CorruptLogError` message says the reader halts on the same line, and `DanglingCauseError`'s
"before anything is written" is now true of the file and directory as well.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model; it sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run (R6
replaces it for agent work). Nothing here is author-as-reviewer beyond the stranger pass.

**Choices made inside the task:**

- **3.2's reader half is not fixed, and the issue's test for it is kept as a halt.** The issue
  asked the reader to accept a repeated `seq` with blank or non-JSON lines between. The shape
  reproduces as a byte string and halts, but no heal produces it: the old heal wrote the newline
  and the next record in one write, so a terminated unacknowledged record and the record that
  reuses its `seq` are always adjacent (a heal cut between them leaves the file ending in a
  newline, and the *next* writer then takes `seq` from the resurrected line — the bug fixed
  here, which produces no repeat); and the new heal never terminates an unacknowledged record
  as JSON at all. The exhaustive tests are the evidence that no reachable state halts. What is
  left is a hand-edited log, and one ref naming two events is what the reader exists to refuse;
  loosening that check would trade a real corruption detector for an unreachable case. The
  fix for 3.2 is where the bug is, in the writer. `test_3_2_1_the_issue_s_reader_shape_still_
  halts_as_corruption` pins this with the reasoning in its docstring; if the owner would rather
  have the reader accept it, that is a one-condition change in `_parse`.
- **A mark byte, not a reader rule, and not a format change.** No bytes on disk can tell
  `R1\nJ2\n` with J2 acknowledged from the same bytes with J2's heal torn after one byte, so the
  resurrection cannot be fixed on the reader's side; the heal had to write something a bare
  newline is not. I read the owner's instruction — any format change to what is on disk changes
  a D-number, so `flag` it — and did not flag, on this reasoning: the on-disk contract (D80, the
  reader's docstring) is *lines; a JSON line is a record; a non-JSON line is a fragment the
  reader skips*. A stepped-over fragment was always arbitrary non-JSON bytes; it now ends in
  `~`. The reader's code is unchanged, and `main`'s reader loads a file the new writer healed
  with the same records and skips. If the owner reads D80 as pinning the heal's bytes rather
  than the reader's rule, this is the paragraph to disagree with, and the one-line change is
  `TORN_MARK`.
- **`~`, one byte.** Any non-whitespace byte that cannot end a JSON value works; one byte means
  a heal has exactly three cut classes (none, mark, mark and newline) instead of one per byte of
  a longer marker, and a multi-byte marker could not be a prefix of valid JSON either way but
  would need the same argument for every prefix. `~` over NUL for `grep` (D10).
  `test_3_2_a_heal_s_first_byte_cannot_end_a_json_line` checks the byte against every cut of a
  real record and the hostile shapes (`1`, `true`, `"s"`, `[1]`, `{}`).
- **The writer steps past non-JSON lines rather than refusing them.** The alternative — keep
  `CorruptLogError` for a non-JSON last line — is the bug. The writer's job is to agree with the
  reader about what the stream holds; the reader's rules are the contract (D80). What stays
  fatal in both is valid JSON with no usable `seq`: no interrupted append produces that.
- **The existing test was edited.** The brief allows it when the task requires it:
  `test_a_corrupt_complete_line_refuses_rather_than_guesses[unparseable]` asserted the
  behaviour 3.1 identifies as the bug. The case moved to a test of its own that says why, and
  the other three refusals still hold, plus `[1]`.
- **`fsync` on the sibling, not a lock.** `LOCK_SH` on the sibling while holding `LOCK_EX` is
  the deadlock the module already rules out. `fsync` needs no lock and does exactly the one
  thing the race needs: make what we read durable before we write. The cost is one `fsync` per
  append with a cross-stream cause, and it only does work when the sibling's writer has not yet
  done its own.
- **Exhaustive at depths one and two over every offset, depth three over every offset of the
  heal and the heal of the heal with the first tear fixed mid-record.** The heal's bytes depend
  on the earlier state only through "torn or not" and "last `seq`", so a third free dimension
  would re-run the same heal shapes 184 times. The two large tests take about five seconds
  each: ~34,000 states each, every one parsed by the real reader twice. I judged that worth it
  for the log; if the suite's time matters more, the depth-two test's inner `_check(s2 + p3)` is
  the half to drop.
- **No D-number conflict found**, so `flag` was not used. The paragraph above says where I
  would be wrong if I am.

**What to read first:** `writer.py`'s module docstring, section *How a tear is healed, byte by
byte* (the two rules and why both are needed), then `_last_record` and `_plan` (forty lines),
then `test_every_byte_offset_a_heal_can_be_cut_at_after_every_cut_append` and the model
comment above it in `tests/test_audit_log.py` — that test is the whole property. Then the
five-step walkthrough under *A cause must already be in the log* and `_refs_of`.

**Noticed, not done:**

- `tests/test_reader.py::test_an_unacknowledged_record_does_not_share_a_ref_after_healing`
  still passes, but its docstring describes the old heal ("the unacknowledged line is
  newline-terminated and the next append reuses its seq"). Under the new heal the line ends in
  `~` and is skipped as non-JSON. The assertions are right; the prose is stale.
- `reader._parse` reports a `~`-terminated line as "not JSON — most likely an append
  interrupted mid-write". True, and a more specific reason ("a tear the next writer marked")
  would help a person reading `walle` output; it is a message change in the reader, outside
  this task's diff.
- `LockedStream.read()` and `_refs_of` read whole files for a cause check. For a long-lived
  stream that is a full parse per append with causes; the index already knows every ref and
  could answer this, but a writer that depends on a derived file inverts constraint 5.
- `StreamWriter.locked()` still creates the file for session callers (`observer`, `proposer`,
  `curator`, …) before they decide to append. They read first by design, so an empty stream is
  what they would leave if they appended nothing; whether that is wanted is a separate question.
- macOS `fsync` does not reach the platter without `F_FULLFSYNC`. That is true of every `fsync`
  in the project already and is a durability-level decision for the owner, not this task.

**Cost:** author (Fable 5.1, this thread) — see Hyperagent's usage for thread
`cmw4ae3z001s106ad4javbpkv`; reviewer — in its own log.
