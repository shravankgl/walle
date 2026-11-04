# DEVLOG

One entry per shipped task (D104). Newest last. Each entry: what shipped, the verify criterion
and its result, what is next, and anything noticed but deliberately not done. The task numbers
are `PLAN.md` §9's. From task 4, each task has its own file in [devlog/](devlog/) (R6).

---

## 2026-09-19 — Day 0: decisions, git

**Shipped:** D104–D106 (cadence, name, toolchain); `CLAUDE.md` rewritten for implementation;
`PLAN.md` §9 task list with verify criteria for tasks 1–30; this file; git initialized with the
design documents as the first commit, personal identity set repo-locally.

**Verify:** `git log` shows one commit; `git config user.email` in this repo is the personal
address. ✅

**Next:** Task 1 — repo scaffold.

**Noticed, not done:** `README.md` still says "no code yet" and "see PLAN.md §4"; it gets
rewritten at task 25 when there are numbers to put in it, not before.

## 2026-09-19 — Task 1: repo scaffold

**Shipped:** `pyproject.toml` (`walle` 0.0.1, MIT, py ≥ 3.12, `src/` layout, runtime deps
`click` + `pydantic` only, ruff/pyright/pytest config inline), `src/walle/{__init__,cli}.py`
(`walle --version`, a click group every later task adds to), `tests/test_smoke.py`,
`.pre-commit-config.yaml` (ruff lint + format, whitespace, EOF, yaml, large files),
`.github/workflows/ci.yml` (uv → ruff check → ruff format --check → pyright → pytest on 3.12),
`LICENSE`, `.python-version`.

**Verify:** `uv run pytest` — 2 passed. `ruff check` clean, `ruff format --check` clean,
`pyright` 0 errors. `uv run walle --version` → `walle, version 0.0.1`. ✅ CI green on
`github.com/shravankgl/walle` (private), run 35396079098, 18 s. ✅

**Next:** Task 2 — the event schema.

**Noticed, not done:** ruff format reports "10 files" for 3 Python files — a counter quirk,
verified with `--verbose` that only the three real paths are formatted. `uv.lock` is committed
(reproducible installs); `.venv/` is not.

## 2026-10-10 — pithagoras re-read (not a §9 task)

**Shipped:** D107–D112 from re-reading pithagoras at 412 commits past the Session 2 pass
(5b67cc9 → f52db60). Four new docs there: `sandbox.md`, `memory.md`, `agents.md`,
`upgrading.md`. `REFERRALS.md` gained a re-read entry and a "re-read the moving ones" rule;
`ARCHITECTURE.md` §4, §6, §7, §9 and `PLAN.md` §9 tasks 6, 7, 12 updated.

**The one that matters:** D107. Our rendered hot tier is a derived artifact (D10) that D50
promised was editable — a direct edit would have been silently overwritten by the next render.
pithagoras hit the same problem twice in one release (its memory notes, its `MEMORY.md`) and
both times added a stale-copy check. Fixed here before it was written: renders are `0444`,
`walle context` is the editor and appends events, and a render that finds the file changed
preserves the edit and reports it. Tasks 7 and 12 carry the verify criteria.

**Verify:** no code changed; every D-number cited in the three synthesized documents resolves
to a decision in `CONVERSATION.md`; `uv run pytest` still 2 passed, ruff/pyright clean. ✅

**Next:** Task 2 — the event schema.

**Noticed, not done:** pithagoras relicensed to Apache-2.0; MIT stands (D105), reasoning in
the Session 8 rejected table. Their `upgrading.md` is five pages of migration machinery we do
not need because the log is canonical and the index rebuilds — that comparison belongs in the
Phase 0 article, not in the docs.

## 2026-10-10 — hermes-agent + Herald-OS review (not a §9 task)

**Shipped:** D113–D117. `REFERRALS.md` batch 5; `ARCHITECTURE.md` §4, §5.5, §9, §11 and
`PLAN.md` §9 task 8 + the risks table updated.

**The one that matters:** D113. Hermes binds every background job to its caller's context
through one helper, because *"a worker started with an empty context silently lands on the
default profile."* Our Observation loop, proposer, consolidation and index rebuild are all
background work that acts for a principal — and D32's folders do nothing about a thread that
forgot which principal it is. One helper, bind at spawn, raise on an empty context. It costs
nothing now, in a single-principal install, and is unfixable later by inspection.

Also: D114 two-speed maintenance with `pinned` as a human floor (hermes `curator.py`),
D115 a deadline on the session-start hook, D116 destructive actions never get "always"
(Herald ADR-005), D117 install-is-not-enable and a widened manifest lapses its grant
(Herald ADR-019).

**Verify:** no code changed; every D-number cited in the synthesized documents resolves in
`CONVERSATION.md`; `uv run pytest` 2 passed, ruff/pyright clean. ✅

**Next:** Task 2 — the event schema.

**Noticed, not done:** Hermes is the closest built thing to the full thesis and runs Honcho
for user modeling — worth naming in the article as prior art rather than pretending the space
is empty. Herald-OS's `LANDSCAPE.md` names eight more agent-OS projects; surveyed, declined,
recorded in REFERRALS so they are not re-evaluated.

## 2026-10-10 — D118: language decision

**Shipped:** D118 — Python for Phase 0, Tauri rejected (no GUI until Phase 4), the hook is the
first Rust artifact after M1. First measured numbers in the project: CLI cold start with
pydantic + click ~360 ms, warm ~30 ms, against a 2 s hook budget — which killed the latency
argument for Rust now.

**Verify:** `uv run pytest` 2 passed; ruff, pyright clean. Numbers reproducible with
`/usr/bin/time -p uv run walle --version`. ✅

**Next:** Task 2 — the event schema. No more reviews, no more decisions unless code forces one.

**Noticed, not done:** the 360 ms cold start is a `uv run` number and includes uv's own
resolution. The figure that matters for D115 is the installed entry point measured the way the
hook will actually be invoked — measure it properly at task 8, not before.

## 2026-10-10 — Task 2: the event schema

**Shipped:** `src/walle/events.py` (~290 lines) and `tests/test_events.py` (36 tests).
`Ref` as a validated `d-<device>:<seq>` string; `Instant` / `WorldTime`; `Level`; the `Event`
base with `v`, `seq`, `ts`, `actor`, `caused_by` and the permanent upcast chain; the five
types task 2 covers — `observation`, `assertion`, `supersede`, `snapshot`, `contradiction`;
a discriminated union behind `parse_event`; `canonical_json` and `content_hash`.
Implements D57, D58, D66, D85, D88, D89, D93, D94, D96, D103, D111.

**Verify** (PLAN §9 task 2): round-trip per type through a real JSON string ✅; hash stable
across field order ✅; unknown field rejected ✅; `v` on every event ✅; malformed and
traversal-shaped refs rejected ✅; an event from a newer build halts by name ✅.
`uv run pytest` 36 passed; ruff check, ruff format, pyright all clean. ✅

**Two bugs the tests caught before the commit:**

1. `FutureSchemaError` inherited from `ValueError`, so pydantic folded it into a
   `ValidationError` and, under a discriminated union, it arrived as one entry in a list of
   per-member failures — indistinguishable from a typo. D111 asks for a halt that names the
   version. It now inherits from `Exception` so it propagates.
2. The upcaster in the test did `{**d, "name": d.pop("title", "")}`. The splat runs first, so
   `title` survived and `extra="forbid"` rejected the event the upcaster existed to rescue.
   A removed field has to be dropped explicitly — noted in the test, because every real
   upcaster will have the same shape.

**Author-blind pass found one real defect:** `datetime.fromisoformat` and `date.fromisoformat`
accept far more than RFC 3339 — `20261010T143702+0000`, `2026-10-10 14:37:02+00:00`,
`20260301`, `2026-W09-1`. Parsing alone would have let the same moment into the log under
several spellings: several hashes, several things to grep for, in a file whose whole claim is
that you can read it with `cat`. Shape is now gated by regex before parsing, `Z` normalises to
`+00:00`, and sub-second precision survives because it is a real difference rather than a
spelling. Also from the pass: `seq` gained `ge=1`, `summary` gained `min_length=1` (an empty
one renders a blank index line, the single thing it exists for), and `Level` now documents that
adding a member is a breaking change needing a version bump.

**Decisions taken inside the task, worth knowing:**
- `ts` is a *string*, not a `datetime` — the JSONL is canonical and a string round-trips
  byte-identically, which the content hash depends on.
- `content_hash` excludes `seq`: it identifies content, not position, so the same event
  appended twice has one hash and the rebuild test can see the duplicate.
- `canonical_json` (sorted, compact, ASCII-escaped, for hashing) and the file format
  (readable, with `seq`) are deliberately two different things.
- `actor` is an open string while `level` is an enum: an unknown actor from a later build must
  still load, whereas the three levels drive behaviour.
- The payload is flattened into each typed subclass rather than nested under a `payload` key
  as §5.1's generic example draws it — a discriminated union is what makes the strictness work.

**Next:** Task 3 — the stream writer. Append, fsync, per-stream seq, path validation, and a
cross-process lock. First task with a two-runner walkthrough in its entry.

**Noticed, not done:** no `now()` helper yet — the writer needs one in task 3, and putting it
here would have been scope. §5.2's other event types (utterance, capability, mission, trust,
feedback, outcome) belong to phases that do not exist; adding them now would be guessing at
fields with nothing to check the guess against.

## 2026-10-10 — Task 3: the stream writer

**Shipped:** `src/walle/paths.py` (layout + name validation) and `src/walle/writer.py`
(`StreamWriter.append`, `encode`, `CorruptLogError`), plus `now_instant()` in `events.py`.
49 new tests. Implements D10, D57, D80, D85, and hard constraint 2.

**Verify** (PLAN §9 task 3): four concurrent writer *processes* × 25 appends → seqs exactly
1..100, no gaps, no duplicates, every line parses ✅; a traversal, uppercase or over-long name
is rejected before anything is opened ✅. `uv run pytest` 85 passed; ruff, format, pyright
clean. ✅

**The race test has teeth.** Commenting out the single `flock` line makes it fail; restoring
it makes it pass; five consecutive runs are stable. A concurrency test that passes without the
lock proves nothing, so this was checked rather than assumed.

**Two-runner walkthrough** (coding rule 2), also carried in the module docstring. A and B both
append to a stream ending at seq 44:

1. Both open `a+b`. Opening is not ordering — neither has read anything.
2. A takes the `flock`; B blocks inside it, having read nothing.
3. A seeks, reads the last complete line (44), writes 45, flushes, `fsync`s.
4. A closes; the kernel drops the lock.
5. B wakes and *only now* seeks and reads. It sees 45 and writes 46.

Steps 3 and 5 are the whole thing: a writer that computed `seq` before blocking would wake
with a stale number and write a duplicate — not a lost event, but one ref naming two events
for the rest of the log's life. If A dies between 3 and 4, B finds a line with no newline,
leaves those bytes alone, starts on a fresh line, and takes `seq` from the last *complete*
line.

**Author-blind pass found three real defects, all fixed before the commit:**

1. **Validation after the durable write.** `append` re-parsed the record to satisfy a wider
   return type — so a validation failure would have raised at a caller whose event was already
   on disk. A status that contradicts the file is the worst bug a log can have. Now generic in
   the event type; no re-parse.
2. **Directory fsync outside the lock.** Done after release, there was a window where another
   writer opened the file, saw it exists, skipped the directory fsync, fsynced only its own
   data and returned claiming durability — while the entry its data hangs off was still only
   in cache. Moved inside the lock.
3. **A stray newline on an error path.** The torn-tail newline was written before `encode`
   ran; an exception in between would have left a blank line. Encoding is now hoisted above
   every write (coding rule 7).

**Decisions taken inside the task:**
- A torn tail is **stepped over, not truncated**. Truncating is tidier and is what "append,
  never mutate" forbids; those bytes are evidence a crash happened there, and nothing ever
  depended on them because the append was never acknowledged.
- A corrupt *complete* line is fatal (`CorruptLogError`), not repaired: the next `seq` comes
  from it, and guessing risks reusing a number.
- The file keeps `ensure_ascii=False` so text stays greppable in its own script; the hash form
  keeps `ensure_ascii=True`. Two functions, as task 2 set up.
- `flock` does not serialise across hosts on NFS. Stated in the module docstring rather than
  discovered later.

**Next:** Task 4 — the stream reader: iterate in seq order, tolerate a torn tail and *report*
it, parse refs.

**Noticed, not done:** `_tail` doubles its window without a ceiling, so a single pathological
record (a 100 MB body) would be read whole into memory. The record is the problem in that case,
not the read; revisit if a body limit ever lands. A test caught me filtering log lines by
`startswith(b"{")` — a truncated record starts with `{` too, so the reader must *attempt the
parse* rather than guess from a prefix. That rule belongs to task 4.

## 2026-10-10 — Pre-commit review (not a §9 task)

**Shipped:** `scripts/review.sh` and D119. `alibaba/open-code-review` installed globally
(`@alibaba-group/open-code-review`, v1.12.13). `CLAUDE.md` gained the ritual step; `.review/`
is gitignored.

**What it is:** Alibaba's internal code-review assistant, open-sourced — 45.7k stars,
Apache-2.0, Go, pushed today. Verified via the GitHub API before installing (REFERRALS step 0).
Its architecture is D114's two-speed shape independently: deterministic pipelines decide which
files are reviewed and which rules apply; the agent only does dynamic judgement.

**Supply chain, stated accurately:** npm's `allowScripts` policy blocked the package's
postinstall, and nothing was overridden to work around it. The binary arrives as the optional
platform package `@alibaba-group/ocr-darwin-arm64` under registry integrity hashes; the
postinstall is only a fallback for when that package cannot install. `ocr --version` works.

**Verify:** missing tool → exit 2 ✅; configured-but-failing model → exit 2 with the tool's
stderr, no silent fallback ✅; unconfigured → delegation mode, exit 0 ✅; file selection on the
task 3 commit picked the 3 source files ✅.

**Running the ritual on the ritual found a real defect in it.** The first version probed for a
model with `ocr review --preview && ocr review ...`. But `--preview` succeeds *without* a
provider, so the real gate was the second command failing — it worked by accident. Worse: a
configured model failing transiently would have fallen back and printed "no model configured",
a status that lies about what happened. Now there is an explicit `have_model` check against
the three sources `ocr` itself names, and configured-but-failing exits 2.

Also fixed in the same pass: `mapfile` does not exist in bash 3.2, which is what macOS ships,
so file extraction is a NUL-delimited list through `xargs` instead of an array.

**Two modes, and the DEVLOG must not blur them.** Delegation needs no key and buys the
deterministic half — file selection that does not cut corners, a curated ruleset — but **not
independence**: the author is the reviewer, which is the one thing coding rule 4 asks for. A
configured model is the real thing. Entries from here on say which mode produced them.

**Next:** Task 4 — the stream reader.

**Noticed, not done:** `ocr` excludes `tests/` by default (`default_path`), which is wrong for
this project — the tests carry the verify criteria (D69) and a bug in one is a bug. Changing it
means supplying a custom `rule.json`, which risks losing their curated ruleset; left alone for
now, and the author-blind pass still covers tests. Also: git 2.39.5 is below `ocr`'s minimum of
2.41, which it warns about on every run — `brew install git` when convenient.

## 2026-10-11 — Claude Code plugin, and what its first review found

**Shipped:** `open-code-review` installed as a Claude Code plugin (`/plugin marketplace add
alibaba/open-code-review`), giving `/review` and `/delegate-review`. R4 logged, amending D119:
`/delegate-review` is the ritual; `scripts/review.sh` keeps the non-interactive and
configured-model paths because it holds the "a review that did not run exits 2" property.

**This is what "configured with Claude Code" means here:** in delegation mode Claude Code *is*
the reviewing model, on the existing subscription — no API key, no local model, officially
supported by the tool. What was not done: pointing `ocr` at Claude Code's subscription
credentials via `ANTHROPIC_AUTH_TOKEN`. That auth is for Claude Code, not for driving
third-party tools, and extracting it is the credential handling D109 forbids.

**First real review — on the task 3 writer — found a genuine defect.** `_fsync_dir` ran
*after* the record was written:

- If it raised (EIO, metadata ENOSPC), the exception reached a caller whose event was already
  written and fsynced — a status contradicting the file, which `writer.py`'s own docstring
  calls the worst bug a log can have. A caller retrying would append the event twice.
- A crash between the data fsync and the directory fsync left durably written bytes in a
  directory entry that was not durable — precisely what `_fsync_dir` exists to prevent.

Fixed by ordering it first: the entry is made durable before the data that depends on it, so
a failure there truthfully means nothing was written. New test
`test_a_failed_directory_fsync_leaves_no_record_behind` asserts the file is empty after a
simulated metadata failure.

Worth noting it was yesterday's author-blind pass that *moved* this call inside the lock and
still read past the ordering. A second reading with someone else's checklist caught it.

**Verify:** 86 passed; ruff, format, pyright clean. The race test still fails with the `flock`
line removed after the reorder — re-checked, not assumed. ✅

**Next:** Task 4 — the stream reader.

**Noticed, not done:** `ocr` still excludes `tests/` (`default_path`), so the review never saw
the test files; the author-blind pass covers them. Gaps in `seq` remain possible if a process
dies after flush but before fsync — the event is lost, nothing referenced it, and refs stay
unambiguous, so gaps are acceptable where duplicates are not. Said here so it is a decision
rather than a surprise.

## 2026-10-11 — An independent reviewer, and the two bugs it found

**Shipped:** `scripts/review.sh` rewritten so the reviewer is **Gemini 3.1 Pro via the
Antigravity CLI** (`agy --print`), with `ocr delegate` still doing file selection and rules.
R5 logged. Two real defects fixed, with regression tests. 95 tests green.

**This is the first time the project has had a reader that did not write the code.** Coding
rule 4 has asked for one since day one; delegation mode could not give it.

**Bug 1 — `$` matches before a trailing newline in Python.** Five validators used `^...$`
with `.match`, so `check_name("maya\n")` returned `"maya\n"` and built the directory
`p-maya\n`. Worse: `"d-mbp:1\n"` was a **valid ref**. A ref with a trailing newline prints
identically to one without and compares unequal — one event nameable by two refs nobody could
distinguish by eye, which is exactly the ambiguity `writer.py`'s docstring calls the worst
failure in the project. Timestamps had it too, so two visually identical instants could hash
differently. All five now use `fullmatch`.

**Bug 2 — a TOCTOU on `created`, in yesterday's fix.** `open(..., "a+b")` creates the file
*before* the lock is taken. So writer A's `open()` can make writer B's pre-lock `exists()`
return True; B then wins the lock, writes the first record, skips `_fsync_dir` and returns
claiming durability on a directory entry nobody made durable. Yesterday's reasoning — "the
creator fsyncs inside the lock, so by the time anyone else holds it the entry is durable" —
assumed creation happens under the lock, and `open()` breaks that. Newness is now decided
inside the lock from `size == 0`.

Both were missed by: my author-blind pass on task 3, the delegated review yesterday, and my
own re-read when moving the fsync. The independent reader found both in one run.

**Two operational facts, learned the hard way:**
- `agy -p` needs the prompt attached (`-p="..."`) and does not read stdin, so reviews run per
  file. Fine for our commit sizes.
- Without an explicit *do not use any tools* instruction, the reviewer reaches for a command
  to explore the repo, headless mode cannot prompt for that permission, the call is
  auto-denied, and it returns **nothing** — which the first version of the script reported as
  "reviewed 3/3". That is the exact failure the script exists to prevent, in the script
  itself. Empty output is now a failed review. Granting the permission instead would let a
  reviewer run arbitrary commands here; handing it the diff is the better trade.

**Verify:** 95 passed; ruff, format, pyright clean; the race test still fails with the `flock`
line removed. Review cost: ~2 minutes and three model calls for a three-file commit. ✅

**Next:** Task 4 — the stream reader.

**Correction to yesterday's entry:** it said the first review "found a genuine defect". In
delegation mode `ocr` performs no analysis — the *ritual* found it, by forcing a structured
re-read. Today's entry is the first where a reviewer other than the author actually found
something.
