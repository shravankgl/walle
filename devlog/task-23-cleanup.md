# Task 23: cleanup of low findings from the reviews of PRs 22, 26 and 30

Issue #23 and Shravan's comment on it. Branch `task/23-cleanup`, from `main` at `090d905`.
There were seven low findings, and all are fixed. None was skipped for overlap with an open
PR. When the task started, the only open PR was #36 (task 12). It changed `cli.py` and the
module docstring of `index.py`, and it added `ancestors()` after `load_records`. No item here
touches those lines. The `load_records` fix changes only the `_connect` line inside the
function. #36 merged during the task (`63fb514`). This diff applies cleanly on top of it, and
the four checks pass on the merged tree (487 passed, 3 skipped).

**Shipped:**
- `src/walle/resolver.py`, the three PR #22 findings:
  - **A date against an instant no longer depends on the instant's offset.** `_compare` used
    to compare a date with the instant's *written* calendar day. So one moment written as
    `2026-03-01T20:30:00+00:00` and as `2026-03-02T02:00:00+05:30` compared equal to each
    other but differently against the date `2026-03-01` (unknown in one case, later in the
    other). Now `_against_day` compares the instant's moment with the widest span the date
    can cover in any time zone, `[D 00:00 at UTC+14, D+1 00:00 at UTC-12)`, which is
    `[D-1 10:00Z, D+1 12:00Z)`. Before it the instant is earlier, after it the instant is
    later, and inside it the result is unknown. The result depends only on the moment, so
    two spellings of it always agree. The module docstring says the new rule.
  - **`closed_by` lists each closer once.** `closers` is a `dict[str, set[str]]`. Two
    contradiction events about the same pair, for example one written on each device before
    a sync, close the window once. `closed_by` is still sorted by ref.
  - **An empty or inverted window overlaps nothing.** `windows_overlap` returns `False` when
    either window is known to be empty, meaning `valid_from >= valid_to`. If emptiness is
    unknown (a date and an instant inside the span above), it is not treated as empty, and
    the existing rules decide. Rule 3 in the module docstring says so.
- `src/walle/index.py`, the two PR #26 findings:
  - **A disagreement found by `update` recreates the file.** `_open_index` now takes the cut
    and runs `_disagreement` itself. If the tracking records disagree with the log, it closes
    the connection, `_remove`s the file and its journals, and opens a new one. That is the
    same path as `full` and an unusable file. The in-place `DELETE FROM` and `_write`'s
    `wipe` parameter are gone, so every full rebuild now starts from an empty file.
  - **A connect-time failure in `load_records` is a `StaleIndexError`.** `_connect(mode="ro")`
    is in a `try` that catches `sqlite3.DatabaseError`, as `_unusable` does. Busy or locked
    is re-raised, and anything else becomes "the index file is unreadable (...). Run
    `walle rebuild`." Before, it escaped as a raw `sqlite3.OperationalError`.
- `src/walle/hook.py`, the two PR #30 findings:
  - **A blank required file counts as a missing one.** In `context()`, an empty or
    whitespace-only `identity.md`, `user.md` or `learned.md` raises `Silent` ("blank, so the
    hot tier is incomplete"). A blank `rules.md` is still skipped, as before. A render never
    writes a blank required file: `learned.md` with no beliefs still says "Nothing learned
    yet." So this cannot silence a real hot tier. The module docstring says it.
  - **`--measure` is decided by the parser.** After a successful parse, `measure` is
    `args.measure`. Before parsing, and so for a parse error, it comes from `_wants_measure`.
    That function reads argv the way the parser does: the token after `--root` or
    `--principal` is that option's value, and nothing after `--` is a flag. So
    `--principal --measure` is bad arguments and stays silent, while
    `--measure --root x` (no principal) still explains the error on stderr.
- Tests, in a new section at the end of each file, all synthetic (a made-up person's homes
  and fern):
  - `tests/test_resolver.py`: 4 new tests (6 cases).
  - `tests/test_index.py`: 2 new tests.
  - `tests/test_hook.py`: 2 new cases in `BROKEN_FOLDERS` and 3 new tests.

**Why:**
- **D93, rule 3 and rule 4.** A window holds the moments in `[valid_from, valid_to)`. An empty
  one holds none, so it cannot overlap a fact, and it must not close one. Before, an empty
  window `[2026-03-01, 2026-03-01)` "overlapped" an open window that started earlier, and it
  closed that window at 1 March. Comparison has to be a function of the moment for the fold to
  be order-independent: `_earliest` keeps the first of two times it cannot order, so a
  comparison that depends on the offset made the answer depend on ref order.
- **D103, "keep a date a date."** The date is never given a time zone. The unknown span is
  as wide as every real zone makes it, instead of borrowing the instant's offset.
- **D10, D96.** The index is derived. Every full rebuild takes one path, from an empty file,
  so a rebuild that `update` decides on does not depend on the old file's pages being sound
  enough to `DELETE` from. That is what `full=True` already did after PR #26.
- **D115, D102.** The hook either injects a complete hot tier or nothing. `--measure` is
  the debugging channel, so it must mean the flag and nothing else.

**Verify:** "each has a test that fails today and passes after the fix."

| Finding | Test | Before the fix |
|---|---|---|
| `_compare` | `test_one_moment_in_two_offsets_compares_the_same_against_a_date` | `None` vs `True` |
| `closers` | `test_two_contradictions_naming_one_pair_list_the_closer_once` | `('d-mbp:2', 'd-mbp:2')` |
| `Window` | `test_an_empty_or_inverted_window_overlaps_nothing[3 cases]` | `True is False` in each |
| `_disagreement` | `test_a_disagreement_found_by_update_recreates_the_file` | `leftover` table survived |
| `load_records` | `test_an_index_that_cannot_be_opened_is_a_stale_index_error` | raw `OperationalError` |
| blank file | `test_a_broken_hot_tier_emits_nothing_and_exits_zero[learned.md is blank, identity.md is empty]` | `user.md` etc. injected |
| `--measure` | `test_measure_as_an_option_value_is_not_the_measure_flag` | stderr not empty |

All 10 cases failed before the fix and pass after it. Three more tests guard what must not
change, and they pass both before and after:
`test_an_instant_far_from_a_date_is_still_ordered_against_it`,
`test_a_blank_rules_file_is_skipped_and_the_rest_injected` and
`test_measure_still_explains_bad_arguments`.

Two of the tests set up an unusual state on purpose. The `_disagreement` test plants an extra
table in the old file. Only a recreated file loses it, while `DELETE FROM` keeps it, and an
inode check would be unreliable because a freed inode can be reused at once. The
`load_records` test removes the file between the existence check and the open. That is a real
`SQLITE_CANTOPEN` at connect time. A `chmod 000` test would only work for a process without
`CAP_DAC_OVERRIDE`, and it would skip quietly everywhere else.

Checks, on the final tree (the branch after Shravan merged main into it, plus the fix
commit below). Run as uid 1000 with every capability dropped
(`setpriv --inh-caps=-all --ambient-caps=-all --bounding-set=-all`), so file modes are
enforced the way they are in CI:
```
uv run pytest              490 passed
uv run ruff check          All checks passed!
uv run ruff format --check 57 files already formatted
uv run pyright             0 errors, 0 warnings, 0 informations
```
With the capabilities this sandbox has by default, the result is 487 passed, 3 skipped. The 3
skips are the existing read-only-folder tests, which skip when the process can write a
`0o555` folder.

**CI failure on the first push, and the fix.** CI failed two of the new cases,
`[identity.md is empty]` and `[learned.md is blank]`, with `PermissionError`. Their helpers
wrote to a rendered file, and rendered files are `0444`. The sandbox runs as uid 1000 but
holds every capability, including `CAP_DAC_OVERRIDE`, so it ignored the mode and the cases
passed for me. I ran the first push's checks only that way, which was the mistake. The fix
is in the tests only: each helper now `chmod(0o644)`s the file before writing it, as
`_break_learned_bytes` already did. With capabilities dropped, both cases fail against main's
`hook.py` on the hook's output, not on a permission error, and pass with the fix. I re-ran
the whole suite with capabilities dropped to check my other new tests for the same
assumption, and all of them pass. The new resolver tests touch no files. The index tests
write `index.sqlite` and unlink inside folders the test owns. The blank `rules.md` test
writes a file the fixture created, not a rendered one. From now on I run the suite with
capabilities dropped before every push.

**Two-runner walkthrough** (`index.py`: the lock and the derived file):
- *Builder A runs `update` and finds a disagreement; reader B runs `load_records`.* A holds
  `index.lock` exclusive from before `_read_cut` until after `_rows`. Inside it, A opens the
  old file `rw`, runs `_disagreement`, closes the file, removes it, creates a new one, writes
  and commits. B takes `index.lock` shared. If B got there first, A waits, and B reads the
  whole old file. Otherwise B waits, and it reads the committed new file. B never sees the
  file missing, or new and empty, because both happen only under A's exclusive lock. This is
  the same as the `full` path that PR #26 tested.
- *Builders A and B both run `update`.* The exclusive lock serialises them. The second sees
  the new file, its tracking records agree with the log, and it adds only what was appended
  in between.
- *A stream writer appends during A's build.* Unchanged. The cut is taken under the stream
  locks, and anything appended after the cut is indexed by the next `update`.
- *Failure.* If A's write to the new file fails, the file is left absent, which is the
  existing behaviour for every fresh file
  (`test_a_write_that_fails_on_a_new_index_leaves_no_file`). The next `update` rebuilds it
  from the log. Before, a failed wipe rolled back and left the old, disagreeing file in place.
  Either way the next run rebuilds, so nothing is lost: the index is derived (D10).

**Stranger pass:** the diff is over 30 lines and touches IO, a lock and error paths, so I
re-read it as a stranger:
- *Leaks:* in `_open_index`, the disagreement connection is closed on every path: when
  `_disagreement` raises (`except BaseException`, re-raised), when it disagrees (closed before
  `_remove`, so the file is not removed while open), and when it agrees (returned to the
  caller's `closing`). A first draft used `finally` with `sys.exc_info()`, which was correct
  but hard to read. I replaced it with the plain form. In `load_records`, no connection
  exists on the error path.
- *Ignored errors:* none. Busy or locked is re-raised in `load_records`, so a timeout is not
  reported as a broken index, which matches `_check` and `_unusable`.
- *Lock scope:* unchanged. `_disagreement` still runs under the exclusive `index.lock`, now
  inside `_open_index` instead of after it.
- *Status messages that lie:* `IndexReport.full` and `reason` are what they were. The
  disagreement reason text is unchanged, as the existing disagreement tests confirm. The
  `load_records` message says "unreadable" for a file that vanished. That is slightly loose,
  but it carries SQLite's own reason ("unable to open database file") and gives the right
  remedy. It matches `_unusable`'s wording, so I kept it.
- *Resolver:* the new date and instant comparison never flips an answer. An answer the old
  rule gave as earlier or later is either the same now or unknown. This holds for any offset
  the log accepts, up to ±23:59. If the instant's offset is one a real zone uses (UTC-12 to
  UTC+14), every pair the old rule called unknown, meaning the same written day, is still
  unknown. An instant written with a larger offset, which no real zone uses, can now be
  ordered against a date the old rule called unknown. Because that instant falls outside the
  date's span in every real zone, the new order is correct. Unknown never closes a window (rule 5), so this cannot invent a
  closing. It can turn a closing into an open conflict for an instant and a date within about
  a day and a half of each other.
- *Hook:* the blank-file `Silent` is raised inside the `flock`'s `try`, so the `finally` still
  closes the lock descriptor.
- The PR #30 docstring reflow left a ragged line. Fixed.

**Review:** pending.

**Choices made inside the task:**
- *A span for every time zone, not the instant's UTC date.* Comparing against the instant's
  UTC day would also fix the inconsistency. But it quietly reads the date as a UTC date,
  which is the invented zone the docstring says not to invent (D103). It can also be
  confidently wrong: `2026-03-01T22:00:00-05:00` is 2 March in UTC but still 1 March where it
  was written. The span is a little more often unknown, and it is never wrong.
- *The day as a span, not its midnight as a point.* Treating a date bound as midnight in some
  unknown zone would give a narrower span (26 hours instead of 38), so more pairs would be
  ordered. But that would add answers the old rule never gave: an instant on the same day
  would become later. This task fixes an inconsistency and should not widen what the
  resolver claims to know.
- *An empty window is handled in `windows_overlap`, not refused by `Window` or `Assertion`.*
  Refusing it in `Window` would make `resolve` raise on a line the log already accepts. That
  would make a valid log unfoldable, against hard constraint 3, because the answer must exist
  for every log. Refusing it in the `Assertion` schema would change which lines the reader
  accepts, and that needs a decision. An empty-window assertion is still current, with its
  window as written. It just never conflicts with or closes anything.
- *`_wants_measure` knows the two value options by name.* It is a 10-line pre-scan, not a
  second parser. If an option with a value is ever added to `_parser`, it must be added here
  too, or its value could be read as the flag. The parse result decides whenever parsing
  succeeds, so the pre-scan only matters for the error message.

**What to read first:** `_against_day` and the "World time and precision" docstring in
`resolver.py`. It is the only change here that alters what the fold concludes, and the
choice of span is a judgment a reader might make differently. After that, read the new
`_open_index`, which is now the single place a full rebuild is decided.

**Noticed, not done:**
- `index.ancestors()`, from PR #36, copies `load_records`' old pattern: `_connect(mode="ro")`
  outside a `DatabaseError` guard. It needs the same guard. It is not in this issue's list,
  and it was another PR's code when this task started, so I left it alone.
- `Assertion` accepts `valid_from >= valid_to`. Whether the proposer or the schema should
  refuse one is a decision (D93, D103), so it is not made here.
- `_wants_measure` and `_parser` both list the hook's options. If the hook gains more
  options, one table could drive both.

**Cost:** pending.
