# Task 6: SQLite index, tracking record, `walle rebuild`

Issue #4. Branch `task/06`.

**Shipped:**
- `src/walle/index.py`: the index. One `index.sqlite` per principal, inside their folder,
  with four tables, all derived from the JSONL beside it:
  - `events`: one row per event (ref, stream, seq, type, v, ts, actor, content hash, source
    line, and the event as JSON so a fold can run off the index).
  - `edges`: `(src, dst, 'caused_by')`. `src` is the event, `dst` the event it came from.
  - `events_fts`: FTS5 over each event's summary, body, reason or note.
  - `streams`: the tracking record, `(stream, last_seq, last_hash)`.

  It exports `update` (incremental, the normal path), `rebuild` (from nothing),
  `load_records` (the index's rows as the `Record`s the resolver folds), `state_hash` (the
  D66 hash of a `Resolution`), `IndexReport`, `DanglingRefError` and `StaleIndexError`.
- `src/walle/cli.py`: `walle rebuild --principal NAME [--root DIR] [--full]`. Incremental by
  default. It prints what it did, why when it started over, any skipped torn lines, and the
  resolved-state hash. Exit 1 with the message on a refused build.
- `src/walle/paths.py`: `index_path` and `index_lock_path`, beside the existing path helpers,
  so the index's location is decided once and goes through the same name validation.
- `tests/test_index.py`: the four verify criteria plus 26 cases around them (30 in all).

`index` was already in `RESOLUTION_ROOTS` in `tests/test_constraints.py`, so the
no-model-on-the-resolution-path check covers the new module with no edit to that file.

**Why:**
- **D10, hard constraint 5.** The log is canonical; the index is a cache that can be
  deleted at any time. The design follows from that. There is nothing to migrate: a file
  from another index schema, or one that will not open, is deleted and rebuilt. When the
  index and the log disagree, the log wins and the report says the index was rebuilt.
- **D59.** Provenance edges live in the same SQLite file, rebuilt from `caused_by`, with an
  index on `dst` so the recursive `why` query in task 12 has something to walk. `why` itself
  is task 12 and is not here.
- **D66.** `state_hash` is SHA-256 over canonical JSON (sorted keys, compact, ASCII) of
  everything the fold decides: current beliefs with their effective windows and closers,
  retired assertions and what retired them, open conflicts, and unapplied events. Events
  enter by content hash, so a changed belief changes the hash and a rewritten row does not.
- **D96.** The tracking record lets `update` read in only the seqs after `last_seq` per
  stream. A full rebuild from nothing remains the test.
- **D111.** An event whose `v` is newer than this build halts the build. The reader already
  raises `FutureSchemaError` naming the event type, its `v` and the `v` this build knows;
  the index adds a note with the file and this build's walle version. A `caused_by` naming a
  seq no stream holds fails with `DanglingRefError`, which lists every `src -> dst` pair. Both
  are checked on the log cut **before the index file is opened**, so a refused build leaves
  the index byte-for-byte as it was.

**Verify:** PLAN §9 says "destroy the index → rebuild → resolved-state hash equal (D66);
incremental rebuild picks up only new seqs; an event whose `v` exceeds this build halts the
fold by name; a dangling `caused_by` ref fails the rebuild".
- `test_destroy_rebuild_gives_the_same_resolved_state_hash`: two streams with cross-stream
  provenance, a supersede and a contradiction. The test calls update, hashes the state from
  the index (and checks it equals the hash from the log alone), deletes `index.sqlite`, runs
  rebuild, and checks the hash is equal. Next to it: an index built in two steps hashes the
  same as one built from nothing. The hash changes when a belief changes. And the hash sees
  an index row whose content drifted under the same ref.
- `test_incremental_update_picks_up_only_new_seqs`: 3 events, then 2 more on one stream
  and 1 on a new stream. The second update reports exactly
  `("d-mbp:4", "d-mbp:5", "d-phone:1")`, a third reports `()`, and the tracking table reads
  `{d-mbp: 5, d-phone: 1}`. A companion test marks an old row by hand and shows the update
  never rewrites it.
- `test_a_future_schema_version_halts_the_build_by_name[update|rebuild]`: a hand-appended
  `assertion` with `v: 2`. The build raises `FutureSchemaError`. The message has
  `assertion`, `v2` and `v1`, the note has the stream path and "the index was not changed",
  and the index file is byte-identical afterwards.
- `test_a_dangling_caused_by_fails_the_build[update|rebuild × d-mbp:99|d-ghost:1]`: a
  missing seq in a known stream, and a stream that does not exist. `DanglingRefError.dangling
  == (("d-phone:1", missing),)`, and the index file is byte-identical afterwards.

Before `index.py` existed the file failed at collection with `ModuleNotFoundError: No module
named 'walle.index'`. After the code went in, I broke each rule in turn and ran the file:

| Break | Result |
|---|---|
| dangling check removed | 5 failed |
| incremental ignores the tracking record | 8 failed |
| tracking record never checked against the log | 3 failed |
| stream locks not held across the read | 1 failed (the race test) |
| no file note on a future-v halt | 2 failed |
| state hash ignores assertion content | 1 failed (added after this first survived) |
| no cleanup of a half-made new index file | 1 failed (added after this first survived) |
| explicit `ROLLBACK` removed | 0 failed. Closing the connection rolls back anyway; the explicit `ROLLBACK` is a second safeguard |

The race test passed 20 runs out of 20.

```
uv run pytest               197 passed in 2.07s
uv run ruff check           All checks passed!
uv run ruff format --check  29 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** this touches the log (it reads every stream under a lock) and a
derived file (it writes `index.sqlite`), so both pairs are walked. The same walkthrough is in
the module docstring.

*Writers against the builder.* Writer A appends to `d-mbp`, writer B to `d-phone`, and
builder R runs `update`. A appends `d-mbp:4`; B, having seen it, appends `d-phone:2` with
`caused_by: [d-mbp:4]`.
1. R takes `index.lock` exclusively.
2. R lists `log/`, takes `LOCK_SH` on every stream file at once, and lists again. If a stream
   appeared in between, it drops the locks and retries (5 attempts, then it says so and
   indexes nothing).
3. While R holds the shared locks, A's `LOCK_EX` on `d-mbp` blocks and so does B's on
   `d-phone`. R reads every stream. Either `d-mbp:4` was already on disk, and R sees it, or
   it was not, and then B cannot have written `d-phone:2` yet. R's view is a single cut, so
   any cause it is missing really is missing.
4. R drops the stream locks, and A and B continue. R checks the cut for dangling refs and
   future versions without opening the index.
5. R writes the new rows and tracking records in one SQLite transaction. A reader running
   `load_records` sees the index before or after the commit, never in between.

What fails without step 2: R reads `d-mbp` to seq 3, A and B append, and R reads `d-phone`
including seq 2. The build then fails on a "dangling" `d-mbp:4` that exists, a status that
lies. `test_a_cause_and_effect_written_mid_read_never_look_dangling` stages exactly that with
a thread. With the locks it sees neither event, and the next update sees both. With the locks
removed it fails.

*Two builders.* R1 and R2 both run `update` on one principal.
1. R1 takes `index.lock`; R2 blocks on it, having read nothing.
2. R1 reads the cut, writes, commits, and releases.
3. R2 wakes, and only now reads the log and then the tracking records. The log only grows,
   so R2's cut contains R1's. R1's tracking records match it, and R2 adds only what came after.

The order in step 3 matters. If R2 read the log *before* waiting, R1 could commit a
`last_seq` newer than R2's view. R2 would then see "the index has seq N and the log does
not", and rebuild from nothing for no reason. Taking `index.lock` first rules that out.

Lock order is always `index.lock`, then the streams (shared), then SQLite. Writers hold one
stream lock and nothing else, so there is no cycle.

**Stranger pass:** the diff is ~1,100 lines and touches IO, locking and error paths. After the
checks went green I re-read `index.py` and the command cold. Findings:
- **Busy was treated as broken.** `_unusable` caught every `sqlite3.DatabaseError`, which
  includes `OperationalError: database is locked`. An index that was only locked (say, a
  reader recovering a hot journal) would be called "unreadable", deleted and rebuilt, and
  `load_records` would have said "run walle rebuild". Fixed: `SQLITE_BUSY` and `SQLITE_LOCKED`
  now propagate. `test_a_busy_index_is_an_error_not_a_broken_one` holds an `EXCLUSIVE` lock
  from another connection. It failed before the fix ("DID NOT RAISE") and passes after,
  and the index file is unchanged.
- **Two untested paths.** The state-hash-content and fresh-file-cleanup breaks above survived
  the first test run. Each now has a test.
- **Ignored errors.** One deliberate suppression: a `ROLLBACK` that itself fails is
  suppressed so it cannot replace the error that caused it. The comment says why, and close
  rolls back regardless. `INSERT OR IGNORE` on `edges` only collapses a cause repeated in one
  `caused_by` list, which the schema allows. Nothing else is swallowed.
- **Leaks.** Every connection is in `contextlib.closing`, and the stream file handles are in
  an `ExitStack`. On the retry path in `_read_cut`, the `continue` leaves the `with`, so
  those handles are closed before the next attempt.
- **Lock scope.** Stream locks are held only for the read, not while SQLite writes, so
  writers wait for one read of the log and no longer.
- **A failed write on a new index leaves no file.** Otherwise the next run would report "the
  index file is empty" when the truth was "no index yet". There is a test for it.
- **Deleting an index also deletes its `-journal`.** A hot journal left beside a new file of
  the same name would be "rolled back" into it on the next open. This is commented in
  `_remove`; I did not stage it as a test.

**Review:** pending.

**Choices made inside the task:**
- **The log is checked before the index is touched.** The cut is the superset of what the
  index will hold after the update, so the dangling and future-v checks run in Python on the
  cut. SQL over the index would see the index's old rows plus the new ones, which is the same
  set but only once the tracking check has passed. Checking first is what makes "the index
  was not changed" true without relying on a rollback.
- **Only the last tracked event is checked.** `update` checks that the log still holds the
  tracked `last_seq` with the same content hash, and does not re-hash every earlier row. That
  catches the real hazard, a lost un-fsynced tail whose seq the writer then reuses
  (`writer.py`'s walkthrough). Middle-of-file edits break hard constraint 2. The full rebuild
  is what catches those, which is why it remains the test.
- **Incremental falls back to full on its own, and says why.** The index is a cache. Refusing
  and asking the user to run `--full` would make them do a step that carries no risk.
  `IndexReport.reason` and the command's output state the reason, for example
  `d-mbp: seq 3 in the log is not the event the index has`.
- **In-place transaction, not temp file plus rename.** ARCHITECTURE §5.4 says derived files
  are rewritten atomically via temp path then rename. For a SQLite file, a transaction gives
  readers the same before-or-after guarantee. Renaming a new file over one that another
  connection has open, with a possible hot journal beside it, is the riskier of the two. A
  file that cannot be opened at all is deleted (with its journal) and created fresh, under
  `index.lock`.
- **A stray `*.jsonl` in `log/` is refused, not skipped.** A stream the index cannot name would
  be silently left out of every fold. Non-`.jsonl` files (`.DS_Store`) are ignored.
- **Where the file lives.** I chose `principals/p-<name>/index.sqlite` beside `log/`.
  ARCHITECTURE §8 requires the index to be per profile and inside the principal's folder, but
  names no file. `.gitignore` already covers `principals/`.
- **Edges carry `caused_by` only.** D59 and D111 both say `caused_by`. A supersede's `target`
  and a snapshot's `forgets` are also refs. See noticed, below.
- **No `search` function.** FTS5 is built and tested with raw SQL. The query API belongs to
  `recall`, a later task.

**What to read first:** the module docstring's two-runner section, then `_read_cut` and the
order of operations in `_build` (lock, cut, checks, open, tracking, write). Holding every
stream's shared lock at once is what makes the dangling check trustworthy, and it depends on
a property worth checking: a second `LOCK_SH` from this process (inside `read_file`) on a file
it already share-locks is granted even when a writer is queued for `LOCK_EX`. That holds for
`flock` on Linux and macOS. The race test exercises it on Linux only.

**Noticed, not done:**
- `supersede.target` and `snapshot.forgets` are refs that could dangle too. The resolver
  reports a missing supersede target as `unapplied`. D111 names only `caused_by`, so failing
  the build on those would need a decision.
- A `FutureSchemaError` note names the file, not the line. Adding the line belongs in
  `reader._record`, which this task did not change.
- A torn line shows twice in `walle rebuild` output: once through the reader's `logging`
  warning (Python's last-resort handler, since the CLI configures no logging) and once from
  the command. Configuring CLI logging is its own small decision.
- `update` re-reads every stream file in full, because the reader has no seek-from-offset.
  It is O(log size) per update, which is fine at Phase 0 sizes. A byte offset in the tracking
  record would fix it.
- Writers wait while the builder reads the whole log. That is milliseconds now, but worth
  measuring before the hook path (task 8) appends during a build.
- `load_records` does not say whether the index is behind the log. A caller that needs fresh
  state should call `update` first.
- CI does not yet run the rebuild test against golden logs from prior schema versions
  (D88, `tests/baselines/vN/`). None have shipped yet.

**Cost:** pending.
