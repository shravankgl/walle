# Task 6 follow-up: index reader race and rebuild recovery

Issue #25, from the independent review of PR #24. Branch `task/06-fix`.

**Shipped:**
- `src/walle/index.py`, the five findings:
  - **Readers lock.** `load_records` holds `index.lock` *shared* for the whole read (new
    `_shared`), so it waits out a builder and never sees the empty file a fresh build passes
    through. Readers share the lock, so they don't wait for each other.
  - **`rebuild` starts a fresh file.** `_open_index(path, full=True)` deletes the old file
    (and its journal) and creates an empty one. It no longer reuses the old file by deleting
    its rows, so damage anywhere in the old file can't stop the recovery command. `_check`
    (split out of `_unusable`) now also requires every table, not only `meta`, so `update`
    and `load_records` also refuse an index with a dropped table instead of crashing on
    `no such table`.
  - **One lock per stream.** `_read_cut` reads each stream through the handle it already
    holds `LOCK_SH` on, and parses the bytes with the new `reader.parse_stream`. It no
    longer goes through `read_file`, which opened the file a second time and asked for a
    second shared lock.
  - **Read-only reads.** `_connect(path, mode=...)` takes `rwc`, `rw` or `ro`. Readers use
    `ro`, and `_shared` opens the lock file `O_RDONLY`. It creates the lock file only if it
    can, and reads unlocked if it can't (see Choices).
  - **The report carries its rows.** `IndexReport.records` holds the rows this build
    committed, read back before `index.lock` is released. `events` is now `len(records)`.
- `src/walle/reader.py`: `parse_stream(data, path, stream)`, which parses bytes the caller
  read under its own lock. `read_file` is unchanged.
- `src/walle/cli.py`: `walle rebuild` hashes `report.records` and no longer calls
  `load_records` after the build. The printed hash and the printed counts now describe the
  same build.
- `tests/test_index.py`: 10 new tests (19 cases), plus one hook moved in an existing test
  (see Choices).
- The module docstring's walkthrough now covers a reader as well as builders, and explains
  why one lock per stream matters.

**Why:**
- **Hard constraint 5, D10.** The log is the truth and the index is a cache, so the command
  that rebuilds the cache must always work while the log is readable. Before this change,
  `rebuild --full` read the old file to empty it. That made the recovery path depend on the
  thing it is supposed to recover. Now it never reads the old rows.
- **D66.** The rebuild test compares resolved-state hashes. The CLI's hash has to be the
  hash of the index this build left, not of whatever the index holds a moment later, or a
  user comparing hashes is comparing two different builds.
- **D96.** The tracking record and the incremental path are unchanged. Only the full path
  and the readers changed.
- The task-6 docstring promised readers would see "before or after the commit, never in
  between". Before this change that was only true for incremental builds; now it holds for
  all of them.

**Verify:** the issue's criterion has four parts. Each part has a test:
- *A concurrent reader during a fresh build gets the committed state or waits, never
  `StaleIndexError` (a two-process test).*
  `test_a_reader_during_a_fresh_build_gets_committed_state_or_waits[no index|garbage index|full rebuild]`.
  A child Python process runs `update` (or `rebuild`) with `_open_index` wrapped to sleep
  1 s after the fresh file exists. The parent waits for its signal file and then calls
  `load_records`. The result must be the before state or the after state. On main the first
  two cases fail with `StaleIndexError: ... the index file is empty`. The `full rebuild` case
  passed on main, because main's full rebuild never emptied the file; it guards the new
  delete-and-recreate path. It fails if the reader lock is removed.
- *`walle rebuild --full` succeeds on an index with a dropped table and on one corrupted past
  `meta`, with the resolved-state hash equal to the log's.*
  `test_walle_rebuild_full_recovers_a_dropped_table[events|edges|streams|events_fts]` and
  `test_walle_rebuild_full_recovers_an_index_corrupted_past_meta`. Both run the real command
  and check that `resolved state: <hash of the log alone>` is in its output. The corruption
  helper overwrites the back half of a 66-event index's pages. It first checks its own
  premise: `meta` still reads and `events` raises `DatabaseError`. On main, every case exited
  1 with `no such table: ...` or `database disk image is malformed`.
  `test_an_index_missing_a_table_is_not_trusted[×4]` covers the same damage on the
  `update`/`load_records` path.
- *A stream is locked only once per read.* `test_a_stream_is_locked_only_once_per_read`
  wraps `fcntl.flock`, counts `LOCK_SH` per stream inode across `update`, `update`,
  `rebuild`, and expects exactly one per stream. On main the count was two per stream.
- *Read paths work on a read-only index file.* `test_read_paths_open_the_index_read_only`
  checks every connection `load_records` opens is `?mode=ro`; on main they were `mode=rw`.
  `test_read_paths_work_on_a_read_only_profile[lock file present|absent]` makes the folder
  0o555 and the files 0o444, then folds the index. These two passed on main, because SQLite
  quietly falls back to read-only when `rw` can't write. They guard the new reader lock,
  which would otherwise need write access. They fail if `_shared` opens the lock `O_RDWR`.
- The nit: `test_walle_rebuild_prints_the_hash_of_what_it_built` lets a second builder
  index a new assertion right after the first build returns. On main the output said
  `6 events` next to the hash of 7. A first draft of this test passed on main for the wrong
  reason: it added an *observation*, which the D66 hash ignores. I changed it to an
  assertion and it failed as it should.

Then I broke each fix in turn and ran the suite with Linux capabilities dropped (see
Stranger pass):

| Break | Result |
|---|---|
| `load_records` takes no `index.lock` | 3 failed |
| `rebuild` reuses a usable file | 3 failed |
| `_read_cut` goes back through `read_file` | 4 failed |
| readers open `mode=rw` | 1 failed |
| CLI hashes `load_records` again | 1 failed |
| `_check` ignores missing tables | 4 failed |
| `_shared` opens the lock `O_RDWR` | 1 failed |
| report's records taken from the log cut, not read back from the index | 0 failed at first; added `test_walle_rebuild_prints_the_index_hash_so_drift_shows`, now 1 failed |

Both race tests (`fresh_build`, `mid_read`) passed 20 runs out of 20, twice.

```
uv run pytest               213 passed, 3 skipped in 5.50s
                            (216 passed in 5.46s with capabilities dropped; see below)
uv run ruff check           All checks passed!
uv run ruff format --check  30 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 3 skips are the read-only-folder tests. This sandbox's user holds every Linux
capability, including `CAP_DAC_OVERRIDE`, so it can write a 0o555 folder and the tests'
premise is false. Each test tries a real write first and skips with that reason if the write
succeeds; it doesn't pass without testing anything. Under
`setpriv --inh-caps=-all --bounding-set=-all` (an ordinary user, like CI's `runner` and a
Mac) all 216 pass. CI runs on ubuntu-latest as a non-root user, so the tests run there
rather than skipping.

**Two-runner walkthrough:** this touches a lock and a derived file. The module docstring
has the same text.

*Reader Q against builder R, fresh index (none yet, unusable, or `rebuild`).*
1. R takes `index.lock` exclusive. Q calls `load_records` and asks for `index.lock`
   shared. Q blocks; it hasn't looked at `index.sqlite` yet.
2. R reads the log cut under the stream locks, checks it, deletes the old file and its
   journal, and creates an empty file. On main, Q could open it at this point, find no
   tables and raise `StaleIndexError: the index file is empty. Run walle rebuild`, while a
   rebuild was already running. Now Q is still waiting.
3. R writes and commits, reads its rows back into `report.records`, and closes the file.
4. R drops `index.lock`. Q gets it, opens the file `mode=ro`, checks the schema, reads
   every row and drops the lock. Q sees R's committed state.

The other order: Q takes the shared lock first, then R asks for exclusive and waits. Q reads
the old index (a usable one, or it raises `StaleIndexError` because the old file really is
unusable, which is true), and drops the lock. Only then can R delete the old file. Q never
has a file open while R deletes or empties it. Two readers Q1 and Q2 both hold the lock
shared and don't wait for each other.

*Builder R against writer A on one stream (the double lock).*
1. R holds `LOCK_SH` on `d-mbp.jsonl` through handle h1.
2. A asks for `LOCK_EX` on the same file and waits.
3. On main, R then opened the file again as h2 and asked for another `LOCK_SH`. On a kernel
   that queues new shared requests behind a waiting exclusive one, h2 waits for A, A waits
   for h1, and h1 is released only after h2 finishes. That's a deadlock. Linux grants h2 at
   once, which is why the task-6 race test never showed it. The issue warns not to rely on
   that, since the main platform is macOS.
4. Now R reads through h1. There is no second request, so whatever order the kernel uses
   doesn't matter.

*Two builders R1 and R2, CLI output.* R1's `walle rebuild` commits 6 events and reads them
back while still holding `index.lock`. R2 then indexes a seventh. R1's CLI hashes the 6 rows
it read back, which is the build it reported. On main it re-read the index and hashed R2's
7, next to R1's "6 events".

Lock order is unchanged: `index.lock`, then streams (shared), then SQLite. Readers take
`index.lock` shared and then SQLite. `flock` locks belong to an open file, not to a process,
so code that holds `index.lock` must not call `load_records`, because the second open would
wait for the first. Nothing does that now (the CLI used to call `load_records` only after
`build` returned). The docstring says so.

**Stranger pass:** the diff is ~450 lines and touches IO, locking and error paths. After
the checks were green I re-read it cold. Findings:
- **A traceback that misleads (fixed).** The first `_shared` yielded inside its
  `except OSError` block when the lock file couldn't be created. Any exception from the
  caller's body, such as `FileNotFoundError: no index yet`, would then show as "During
  handling of the above exception (PermissionError), another exception occurred". That
  wrongly suggests the permission error caused it. I moved the open into
  `_open_for_shared_lock`, which returns `None`, and the yield is now outside any handler.
  `test_a_reader_that_cannot_lock_reports_its_own_error` checks `__context__ is None`. It
  failed against the earlier shape and passes now.
- **Stale documentation (fixed).** The module docstring's step 5 said a full build was one
  transaction over the existing file, and it had no reader runner. Both are rewritten.
- **The permission tests prove nothing here (handled).** See the skip note above. Without the
  write probe, two of them would have failed in this sandbox for a reason unrelated to the
  code.
- **Ignored errors.** `_open_for_shared_lock` swallows only `EACCES`, `EPERM`, `EROFS` and
  `ENOENT` when *creating* the lock file, which are the cases where a builder couldn't create
  it either. Everything else propagates. A busy index still propagates from `_check`, as in
  task 6, so `rebuild` doesn't delete a file another connection holds locked.
- **Leaks.** The lock fd is closed in a `finally`. The stream handles are in the existing
  `ExitStack`. The reader's connection is in `contextlib.closing`.
- **Lock scope.** Readers hold `index.lock` only while reading rows. Writers to the log
  never take `index.lock`, so readers never block appends.

**Review:** pending.

**Choices made inside the task:**
- **`rebuild` deletes, it doesn't build beside and rename.** A temp file plus `rename`
  would keep the old index until the new one commits. But the old file's hot journal then
  has to be removed in a separate step, and a crash between the two steps leaves either a
  journal beside the wrong file or an old file without its journal. Deleting and recreating
  uses the path task 6 already tests (`_remove` takes the journal too). The cost is that a
  `rebuild` that fails partway leaves no index. The next `update` reports "no index yet"
  and builds one, and the log has lost nothing.
- **A stray non-walle connection.** Someone with `sqlite3 index.sqlite` open but idle keeps
  the old inode after `rebuild` unlinks it. Their later reads see the old file. Walle's own
  readers all go through `index.lock`, so this only affects people poking at the cache by
  hand.
- **Readers may create `index.lock`.** A reader on a writable folder with no lock file
  creates an empty one, exactly as a builder would. On a read-only folder it can't, and it
  reads unlocked. That's safe when the builder sees the same folder, because the builder
  must create the file first. A builder running as a *different* user with write access,
  while this reader can't write, is the one gap. It only exists before the first build ever,
  or after someone deletes `index.lock` by hand. Task 15 should check this against how it
  actually makes a profile read-only.
- **`_check` looks at the schema, not the pages.** `PRAGMA quick_check` would also find
  damage past `meta`, but it is O(file) on every read. `rebuild` no longer needs it, so I
  left it out (see Noticed).
- **`IndexReport.records`, not `IndexReport.state`.** Putting the hash in the report would
  make `index.py` import and run the resolver. Its `TYPE_CHECKING` comment says the index
  stores records and does not fold them, and I kept that.
- **One existing test edited, because the task required it.**
  `test_a_cause_and_effect_written_mid_read_never_look_dangling` stages its race by wrapping
  `index_module.read_file`. Fixing the double lock means the index no longer calls
  `read_file`, so the hook moved to `index_module.parse_stream`. That runs at the same
  moment: all stream locks held, after one stream and before the next. The assertions are
  unchanged, and it still fails when the stream locks are removed.

**What to read first:** the reader section of the module docstring, then `_shared` and
`_open_for_shared_lock`. The claim to check is the one in `_shared`'s docstring: reading
unlocked when the lock file can't be created is safe. Then `_open_index`: three lines decide
when the old file is kept and when it is deleted.

**Noticed, not done:**
- `update` (not `--full`) on an index damaged past `meta` still raises a raw
  `sqlite3.DatabaseError`, and so does `load_records`. `walle rebuild` prints it as a
  traceback rather than "run `walle rebuild --full`". Catching it needs a decision on whether
  `update` should fall back to a full rebuild on any non-busy `DatabaseError`, or refuse and
  point to `--full`.
- `update` now reads every row back to fill `report.records`: a second O(index) pass on top
  of the O(log) re-read noted in task 6. That's fine at Phase 0 sizes; worth measuring before
  the hook path (task 8) calls `update` per event. An option such as `records=False`, or a
  lazy report, would fix it.
- A reader on a read-only folder with a hot journal beside the index gets
  `StaleIndexError: the index file is unreadable (...)`. That is true but vague: the next
  writable `update` rolls the journal back. Not staged as a test.

**Cost:** pending.
