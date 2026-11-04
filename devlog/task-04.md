# Task 4: Stream reader

**Shipped:**
- `src/walle/reader.py`: reads one `(principal, device)` stream back. It returns the events
  in `seq` order as `Record`s (each with its own ref, line number and parsed `caused_by`),
  along with a list of `Skip`s for lines that were not loaded. `read_stream(root, principal,
  device)` resolves the path through `paths.py`. `read_file(path, stream)` reads a given
  file.
- `tests/test_reader.py`: the verify criterion plus 20 cases around it.
- `tests/fixtures/torn_tail.jsonl`: a synthetic golden stream. It holds five events (two
  observations, an assertion, a supersede and a second assertion about weekday dinners),
  written with the real `encode`. The fifth is cut in half with no trailing newline. No real
  data.

**Why:** D80. A crash mid-append leaves a fragment, and a bare `json.loads` over the file
would fail on every read after that: one bad line bricking a stream. The reader skips the
fragment deterministically, and **reports** every skip, both in the return value and as a
logged warning. It never repairs the file (hard constraint 2). Refs are `d-<device>:<seq>`
(D57). An event from a newer schema still halts by name (D111) rather than being skipped.

**Verify:** PLAN §9 says "fixture with a truncated last line loads N−1 events and reports
exactly one skip". The test for it is
`test_a_truncated_last_line_loads_n_minus_one_events_and_reports_one_skip`. It loads the
fixture: 5 lines, so 4 events with seqs `[1, 2, 3, 4]` and exactly one skip, on line 5, with
the warning naming `torn_tail.jsonl:5`. Before the reader existed, the test failed at
collection with `ModuleNotFoundError: No module named 'walle.reader'`.

```
uv run pytest               120 passed
uv run ruff check           All checks passed!
uv run ruff format --check  22 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**What counts as an event:** this is the part to check.
1. **An unterminated final line is torn**, and is skipped *without being parsed*. The writer
   takes the next `seq` from the last newline-terminated line, so a record that is complete
   except for its newline was never acknowledged, and its number will be reused. Whether the
   bytes happen to parse says nothing about whether the write finished.
2. **A line that isn't JSON is skipped.** After the writer steps over a tear, the fragment
   sits mid-file, so mid-file fragments have to be tolerated too, not only the last line.
   This follows task 3's rule: the reader attempts the parse rather than guessing from a `{`
   prefix.
3. **The same `seq` on two adjacent lines: the earlier one is dropped and reported.** This
   is the case in (1) after the writer has healed it. The unacknowledged line now ends in
   the writer's newline, and the next line reuses its seq. Keeping both would make one ref
   name two events. The rule only applies when the two lines are adjacent, because that is
   the only shape the writer produces.
4. **Everything else is fatal (`CorruptStreamError` with `file:line`):** valid JSON that
   isn't a valid event, a missing `seq`, a `seq` that goes backwards, or a repeated `seq`
   that isn't on the next line. None of these is an interrupted write, and skipping them
   would silently drop or renumber a real record.
5. **Gaps are allowed and aren't skips.** As the 2026-10-11 DEVLOG entry decided, an event
   lost between flush and fsync was never referenced.

**Two-runner walkthrough** (writer W and reader R on `d-mbp.jsonl`, which ends at 44):
1. W holds `LOCK_EX`, part-way through writing 45.
2. R opens the file and asks for `LOCK_SH`. It blocks, having read nothing.
3. W fsyncs and closes, which releases its lock.
4. R gets the lock and reads 44 and 45, both complete.
5. A second writer W2 now blocks until R closes.

R never sees half of an append that is still in flight. Without the shared lock, R would
report "an append was interrupted" about a write that was merely still happening, which is a
status that lies. If W *dies* at step 1, the OS drops its lock, R sees the fragment, and the
report is true. `test_a_read_waits_for_an_append_in_progress` holds an exclusive lock
mid-append and asserts the reader blocks, then gets both records and no skips. I re-ran it
with the `flock` line replaced by `pass` and it failed, so the test does depend on the lock.

**Stranger pass:** I re-read the reader cold after the checks went green. Findings:
- **Status messages overclaimed.** The skip reasons said "an append interrupted mid-write"
  as fact, but a mid-file non-JSON line can also come from a hand edit or a disk fault. They
  now say "most likely", and the duplicate-seq message describes the shape instead of
  asserting the cause.
- **Line splitting.** `encode` writes text unescaped (`ensure_ascii=False`), so U+2028 and
  U+0085 can sit raw inside a body. `str.splitlines` would cut a record there. The reader
  splits on the byte `b"\n"` only, and a test now pins that.
- **Lock scope:** held only for the single `read()`, and released by `close`, as the writer
  does. **Leaks:** none, the file is closed by `with`. **Ignored errors:** none. Every
  `except` either records a skip or re-raises with context, and `FutureSchemaError` is
  deliberately not caught.

**Review:** pending. Review has not run yet.

**Choices made inside the task:**
- **Seq order is checked, not sorted.** Within one stream, file order *is* seq order,
  because the writer appends under a lock. Sorting would hide a corrupt file instead of
  reporting it.
- **The reader takes `LOCK_SH`.** It costs a writer a wait of at most one read, and it makes
  "interrupted" true whenever it is reported. Like the writer, this means POSIX only.
- **A missing stream file raises `FileNotFoundError`** instead of returning an empty
  stream, so a misspelt principal can't read as someone with no history.
- **Whitespace-only lines are ignored without a skip.** They carry no data, and a torn heal
  can leave one (the writer's leading `\n` with nothing after it).
- **`read_file` validates the stream id with `make_ref(stream, 1)`.** That avoids importing
  the private `_check_stream` from `events.py`, and adding a public one would have been a
  change outside the task.

**What to read first:** `_parse` in `reader.py`, in particular rule 3 (the adjacent-duplicate
drop). It is the one place the reader discards a line that parsed as a valid event, and it
relies on how the writer heals. If the writer's healing ever changes, this rule has to change
with it.

**Noticed, not done:**
- The reader loads the whole file into memory. That's fine for Phase 0. A lifetime log may
  need a streaming read later, and the one-record lookahead in rule 3 is all it would need.
- If an unacknowledged line that rule 3 would drop comes from a *newer* schema, the reader
  raises `FutureSchemaError` before it knows the line will be dropped. That needs a crash on
  a newer build followed by an append from an older one. Left as is: halting is the safe
  direction.
- Skip warnings are logged as lines are read, so a read that later fails with
  `CorruptStreamError` has already logged warnings for its earlier skips.
- PLAN §9's status column still shows task 4 as next. PLAN.md is a CODEOWNERS path, and the
  issue carries the live status from task 4 on.
- Same caveat as the writer: `flock` does not serialise across hosts on NFS.

**Cost:** pending.
