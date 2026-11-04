# Task p1-connector: connector base with cursors folded from the log

Issue #54. Branch `task/p1-connector`, from `main` at `d8beef5`. Phase 1's first task (R8):
the interface every observation source shares, so that #57 (calendar) and #58 (mail) add
only what is specific to their provider. Nothing in this diff names a provider.

**Shipped:**
- `src/walle/connector.py`, the base. Four names a provider module uses:
  - `Item` — what a source observed: `observed_at` (its own clock), `body`, optional
    `source_version`. Smaller than `Observation` on purpose: the base supplies `ts`, `actor`
    and `source`, so a source cannot set `caused_by` or `actor` and cannot forge provenance.
  - `Cursor` — where a source got to, folded from the log: `observed_at` (the max of the
    source's own times, the "since" a source wants), `ts` (the max append time), `events`
    (how many the stream holds). Nothing stores one.
  - `Source` — a `Protocol` with a `name` and `pull(cursor) -> Iterable[Item]`. Runtime
    checkable, so a fake made of a list is a source.
  - `sync(source, writer, *, ts=None) -> SyncReport` — fold the cursor, ask the source,
    validate everything it returns, then under the stream's lock fold what is already there
    and append only what is missing. `SyncReport` carries `before`, `appended`,
    `already_logged` and `after`.

  Two pure functions beside them: `cursor_of(contents, source)` folds one stream's parsed
  contents, and `fold_cursor(root, principal, device, source)` reads the stream and folds it
  (a missing stream is an empty cursor).
- `tests/test_connector.py`, 24 tests: the three verify criteria, the boundary cases around
  them, the "failed pull appends nothing" property that #57 and #58 both verify, and two
  guards on the base itself (its import graph, and no home folder or environment read).

**Why:** D92 says the cursor is a fold over the log and not a store: the last observed
timestamp per source is the max over that source's observation events, rebuildable, with no
second store. This module is that sentence as code, plus the one thing D92 does not say and a
real source forces: a cursor alone does not stop a duplicate. A source asked for "since
10:00" hands the 10:00 item back; two runs at once both start from the same cursor. So the
base also folds the *content* of what the stream holds and appends only what is missing,
under the lock. That is the same shape `observer.append_observations` already has (D92 again,
from task 11's review), lifted so every source gets it.

D93 decides which clock the cursor is on. D92 was written when an event had one timestamp;
D93 then split `ts` (when the log learned it) from `observed_at` (the source's own time).
D92's own words for the cursor, "the last timestamp of the retrieved dataset", are the
source's time, so the cursor is `max(observed_at)`. `max(ts)` is the time of our last read and
tells a source nothing about what to fetch. Both are on the `Cursor`, and the test that pins
the fold checks `observed_at`. I read this as applying D93 to D92, not as changing D92; if
Shravan reads it the other way, the swap is one line and one test.

The cursor is advice to the source, not a filter on what comes back: an item older than the
cursor is appended if the log lacks it. A calendar event created today for last week, or a
mail delivered late, is new even though it is "before" the cursor. The observer's docstring
makes the same argument for transcript turns.

**Verify:** the issue's three criteria, each a test written before the module existed. With
no `walle.connector` the file failed at collection; the three were then run against the
module as it was built.
- *A re-run adds no duplicates*:
  `test_a_rerun_appends_nothing_and_leaves_the_stream_byte_identical` syncs the same two
  items twice (the second run with a new `ts`) and compares the stream's bytes. Around it,
  `test_a_source_that_returns_the_boundary_item_again_adds_nothing`,
  `test_a_late_item_older_than_the_cursor_is_still_appended_once`,
  `test_the_same_item_twice_in_one_pull_is_appended_once` and
  `test_an_identical_item_from_another_source_does_not_count_as_logged`.
- *Deleting the index loses no cursor*: `test_deleting_the_index_loses_no_cursor` syncs,
  builds the index, folds the cursor, deletes `index.sqlite`, folds again and gets the same
  cursor, then syncs again and is asked with it. `test_the_cursor_never_reads_the_index` lets
  the index lag the log and then overwrites it with garbage: the cursor does not move.
  `test_the_connector_module_imports_neither_the_index_nor_sqlite` makes the reason
  structural.
- *Two simultaneous runners append each item once*:
  `test_two_processes_at_once_append_each_item_once` starts two interpreters that sync the
  same three items into one stream. The content fold under the lock is slowed by a second in
  each child, so if it ran outside the lock both would read an empty stream and both would
  append all three. Result: one child appends 3, the other 0; the stream holds seqs 1, 2, 3
  with three distinct bodies. Processes, not threads, for the reason `test_writer.py` gives:
  `flock` serialises between processes, and threads in one interpreter share a file
  description.

Checks, on the final tree (`uv run`, Python 3.12.16, as an unprivileged user, uid 1000):
- `uv run pytest`: 1229 passed, 10 skipped (1205 passed, 10 skipped on `main`; 24 new).
- `uv run ruff check`: All checks passed!
- `uv run ruff format --check`: 85 files already formatted.
- `uv run pyright`: 0 errors, 0 warnings, 0 informations.

The tests do not assume root: every file is under `tmp_path`, nothing changes modes or
ownership, and the index-garbage test writes a file the test user owns.

**Two-runner walkthrough:** the module touches the log and the stream's lock, so this is
required. Writers A and B both run "sync source S" at once. The stream holds nothing from S.

1. A and B each fold the cursor from the stream, outside the lock (`read_stream`, a short
   `LOCK_SH`). Both see "nothing yet". Both call `pull` and both get the same three items.
   Each builds its three `Observation`s, validated, in memory. Neither has written anything.
2. A takes the stream's `LOCK_EX` (`StreamWriter.locked`) and reads the stream through the
   locked handle: no item from S is there.
3. B asks for the lock and blocks. Nothing B read in step 1 is used to decide what to append;
   the cursor was only what B asked the source with.
4. A appends the three, each `fsync`ed in turn, and closes. The lock goes.
5. B wakes, reads the stream through its own locked handle, and finds the three with A's seqs.
   B appends nothing and reports all three as `already_logged`. B's `after` cursor equals A's.

Had B decided from step 1's read, the stream would hold six events, two refs for each item.
The fold that decides what to append is the one under the lock, and only that one.

A dies in step 4 after the first item: that item is durable (the writer `fsync`s before
returning) and the other two were never acknowledged. B, or the next run, folds the stream,
finds the one, and appends the other two. Nothing is appended twice. The log is partial
between the crash and the next run; it is append-only, so there is no transaction to roll
back, and "a failed pull appends nothing" is a promise about the *source* failing (step 1),
not about the writer.

A syncs S while B is the transcript observer on the same stream: one lock per stream, so
their appends serialise in arrival order. Each folds its own `source` under the lock, so
neither sees the other's items as its own.

A reader (the index builder) runs during step 4: it takes `LOCK_SH` on the stream and waits
for A's `LOCK_EX`, then reads a stream that ends on a complete line. The index's own
walkthrough covers the rest.

**Stranger pass:** done; the diff is well over 30 lines and touches IO, a lock and error
paths. Found:
- `SyncReport.after` was documented as "the cursor as the next run will fold it". That is a
  status that can lie: another writer can append after the lock goes. Reworded to what it is,
  the fold at the moment the lock went.
- The fold re-parsed the running maximum on every event, an instant parse per event per
  comparison. Harmless at Phase 1 sizes, but needless; it now keeps the parsed moment beside
  the string it came from. The string the log holds is what the cursor reports, so a source
  gets back a spelling it will recognise.
- `fold_cursor` catches `FileNotFoundError` and returns an empty cursor. The reader refuses a
  missing stream on purpose, so I checked the swallow was narrow and justified: a first sync
  has no stream yet, and `StreamWriter` creates it under the same root regardless. Only that
  one error is caught, and the docstring now says why.
- Lock scope: the pull is outside the lock, deliberately, because a source is network IO
  measured in seconds and the lock is shared with the hook, the CLI and the observer.
  `test_a_sync_does_not_hold_the_stream_lock_while_pulling` pins it, with a bounded join so
  a regression fails instead of hanging.
- Error on append mid-batch (disk full): earlier appends are durable, the exception
  propagates, and the caller gets no report for the partial batch. The next run heals it.
  Documented in the module; not a bug, but a reader should know the report is all-or-nothing
  while the log is not.
- Nothing ignored, nothing left open: every file handle is in a `with`, and the only `try`
  is the one above.

**Review:** none before the PR. Under R6/R8 the reviewer agent reviews this PR's head and
sets `agent-review`; the author never sets it. The author did not run `scripts/review.sh`.

**Choices made inside the task:**
- **`max(observed_at)`, not `max(ts)`.** Explained under *Why*. Both are on the `Cursor`.
- **Content match, not an item id.** `Observation` has no field for a source's own id, and
  adding one is a schema bump (D88, D96) with an upcaster and a `v2` golden log — a decision,
  not a base task. The content key is `(source, observed_at, body, source_version)`, the
  observer's key. An item whose text changes after it was observed gets a second observation,
  which is right: the thing observed changed. See *Noticed*.
- **The source is asked outside the lock, and everything it returns is materialised and
  validated before the lock.** A source that raises mid-page, or returns an item the schema
  refuses, appends nothing, not even the items before the failure. #57 and #58 both verify
  "a failed sync appends nothing partial"; it comes from here, so they do not each build it.
- **No `walle sync` command.** There is no source to sync yet. The CLI entry belongs with
  the first provider.
- **`Source` is a `Protocol`, not an ABC.** A provider module should not have to inherit
  from the base to be one; a fake made of a list is a source, which is what the tests want
  and what #57's and #58's fakes of their APIs will want.
- **`Item` carries no `actor` and no `caused_by`.** The base writes `actor=observer` on every
  observation. A connector is an observer; letting it name another actor would let a mail
  claim to be the user.
- **The base knows nothing about trust.** Item text is untrusted (D55), and the envelope
  that says so is applied where text is recalled, not where it is logged. Nothing in the base
  reads a body.

**What to read first:** the module docstring's *Order of operations* and *Two runners*, then
`sync`, then `test_two_processes_at_once_append_each_item_once` and
`test_a_source_that_fails_mid_pull_appends_nothing_not_even_the_items_before`. Together
they are the two guarantees a provider module inherits.

**Noticed, not done:**
- A stable per-source item id (`source_id`) on `Observation` would let a changed item
  supersede its earlier observation instead of sitting beside it, and would make the content
  fold a set of ids instead of bodies. It is a schema bump with an upcaster and a `v2`
  golden log, so it needs Shravan. The base's key is chosen so that adding it later changes
  `_key` and nothing else.
- The content fold holds every key for the source in memory, like the observer's. Fine for
  Phase 1 volumes; a very large mailbox would want the fold bounded by the cursor, which is
  safe only once late arrivals are ruled out or keyed by id.
- The `gate` job's regex in `.github/workflows/ci.yml` requires `devlog/task-[0-9]+...md`.
  This task's log is `devlog/task-p1-connector.md`, the name the issue gives, which the regex
  does not match. `.github/` is a CODEOWNERS path, so the fix is Shravan's: either widen the
  regex (`task-[0-9a-z]+[a-z0-9-]*\.md`) or rename the Phase 1 logs. Flagged on #54.
- `uv` is not on the agent image; this run installed it and Python 3.12 before the checks.

**Cost:** not visible to the author agent from inside the run. Shravan can read it from
Hyperagent.
