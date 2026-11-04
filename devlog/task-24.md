# Task 24: golden logs under tests/fixtures/v1/ with __expected__ sentinels; compat check in CI

Issue #15. Branch `task/24`, from `main` at `9cc5bf0`. Test-only: no file under `src/` changes.

**Shipped:**
- `tests/fixtures/v1/maya/`, the first golden log. It is a walle root holding
  `principals/p-maya/log/d-mbp.jsonl` (10 events) and `d-phone.jsonl` (9 events), written once
  by this build's `StreamWriter` and frozen. The household is made up, so the data is
  synthetic. It holds every event type at v1: observation, assertion, proposal, supersede,
  snapshot and contradiction. It also exercises every rule of the fold:
  - Pune (`d-mbp:2`) is closed at 2026-09-01 by Bangalore (`d-phone:1`) through a
    contradiction (rule 4).
  - A proposal is kept with an edit (`d-mbp:3` becomes `d-mbp:4`), and another is dropped by
    a supersede (`d-mbp:5`, `d-mbp:6`).
  - A retraction is undone by superseding the supersede (`d-mbp:7` to `d-mbp:9`), so the
    belief is current again.
  - A snapshot names what it forgets.
  - An assertion `about` someone else is retired with a world-time `valid_to`
    (`d-phone:3`, `d-phone:4`).
  - Two walk times with no world time stay an open conflict (rule 5).
  - A contradiction naming one assertion and an observation lands in `unapplied`.
  - One proposal is still staged (`d-phone:9`).

  The timestamps carry a `+05:30` offset and `observed_at` carries `+00:00`, so the offset
  survives as written.
- `tests/fixtures/v1/maya/__expected__.json`, which holds:
  - `principal` and `events` (19);
  - `state_hash`, the hash of the resolved state (`ebf47084…1b05`);
  - `fold`, the refs in `current`, `retired`, `conflicts`, `unapplied` and `staged`;
  - 28 `sentinels`, mapping dotted paths to values (`"d-mbp:10.forgets.1": "d-mbp:3"`). They
    cover every optional field the fixture sets. None of them pins `v` (see Review).
- `tests/test_golden.py`, the compatibility check. It finds every `tests/fixtures/v<N>/*/`
  with an `__expected__.json` and copies each to `tmp_path`, so the repo copy is never written.
  It then checks:
  - a full rebuild equals `state_hash`, read three ways: the report's records, `load_records`,
    and the JSONL read with no index;
  - an incremental build equals it too;
  - the fold's refs equal `fold`;
  - every sentinel holds after the rebuild.

  It also guards the set of fixtures:
  - at least one exists;
  - each holds only its log and `__expected__.json`, with no index or lock;
  - every event in `vN/` carries a raw on-disk `v` from 1 to `min(N, its type's
    SCHEMA_VERSION)`, and at least one is at `N` (`version_failures`);
  - every type in `EVENT_TYPES` has a fixture under `vK/` holding it at raw `v == K`, for
    every `K` from 1 to its `SCHEMA_VERSION` (`coverage_gaps`). A schema bump that ships
    without a frozen log of the new shape fails here.
  - no sentinel pins `v`.

  Versions are read from the raw JSON lines (`_raw`), not from loaded events, because the
  upcast chain rewrites `v` to the current version on load.

**Why:** D88 says old events must always load. It asks for golden logs per shipped schema
version, sentinels mapping dotted paths to values so that *silent* data loss is caught and not
only load failures, and a CI check that replays every fixture. D96 keeps all of that, and
makes the version a per-type `v` with an upcaster chain. A golden log is what lets a v2
upcaster prove that v1 events still read the same. The hash comes from D66 and the rebuild from
hard constraint 5. CI already runs `uv run pytest` in the `check` job, so the compatibility
check runs on every PR and every push to main without touching `.github/` (which is a
CODEOWNERS path).

**Verify:** "rebuild over every fixture equals its hash" is
`test_rebuild_over_every_fixture_equals_its_hash`, parametrized over every fixture. I wrote it
first and confirmed it failed: with no fixtures, `test_there_are_golden_fixtures` and the type
coverage test failed, and the parametrized cases had nothing to run. Two tests show the checker
catches what it is for:
- `test_a_field_lost_silently_is_caught_by_a_sentinel_and_not_by_the_hash` removes
  `source_version` from the observation in a copy. The log still loads and the hash is
  *unchanged*, because the hash covers assertions only. The sentinel fails and names the path.
  This is the case D88 asks sentinels for.
- `test_a_changed_belief_moves_the_hash` moves a `valid_from`, and the hash changes.

`test_a_sentinel_that_names_nothing_is_a_failure_not_a_pass` checks that a bad path fails
instead of passing quietly.

Checks, on the final tree:
- `uv run pytest`: 949 passed, 5 skipped (after the merge of main, 946 passed before the
  review fixes; 3 new tests, 15 in `test_golden.py`).
- `uv run ruff check`: All checks passed!
- `uv run ruff format --check`: 77 files already formatted.
- `uv run pyright`: 0 errors, 0 warnings, 0 informations.

The tests do not assume root. They also pass with the fixture tree made read-only
(`chmod -R a-w`), because the copy is made the current user's to write.

**Two-runner walkthrough:** not needed. The task adds no writer, lock or derived file. Each
test builds an index in its own `tmp_path` copy, with one builder per copy. The index's own
two-runner behaviour is covered by `test_index.py`.

**Stranger pass:** done, because the diff is over 30 lines. It found three things:
- `copytree` keeps the checkout's file modes. With a read-only checkout, the rebuild would
  have failed for a reason unrelated to compatibility. Fixed: the copy is made user-writable,
  and I checked this with the fixture tree set read-only.
- List indexes in sentinel paths used `str.isdigit`, which accepts non-ASCII digits. Fixed:
  they must be ASCII digits.
- My first expected message for the dropped-field test said "missing". The checker compares
  loaded events, where a dropped optional field reads as its default (`None`), so the real
  message is "None after the rebuild". I kept the checker's behaviour, which matches its
  docstring, and corrected the test. "missing" is for a path that resolves to nothing at all.

The sentinel check compares type as well as value, so `True` does not pass for `1`, and a
float does not pass for an int.

**Review:** none before the PR. Under R6 the reviewer agent reviews this PR's head and sets
`agent-review`. The author does not run `scripts/review.sh` and never sets that status.

*Round 1* (`walle-reviewer`, Gemini, direct diff review of `31fd32e`, since `ocr` could not
install): `agent-review` failure, two medium findings, no high. Both were right.
- **Medium 1: the version checks read `v` after the upcast.** `_from_log` goes through
  `parse_event`, and `Event._upcast` rewrites `v` to `SCHEMA_VERSION`. On the first bump the
  frozen `v1/` log would have read as v2, so the old tests failed in two ways. I confirmed
  both by running the old file with `proposal` bumped. The directory test failed, and the
  coverage test reported `proposal v1 under tests/fixtures/v1/` as missing, which is false.
  The old directory rule (`v == N` for every event) also made a `v2/` log citing any unbumped
  type impossible. Fixed: `_raw` reads the JSON lines as written. `version_failures` allows
  raw `1 <= v <= min(N, SCHEMA_VERSION)` with at least one event at `N`. `coverage_gaps`
  counts raw versions.
- **Medium 2: the sentinel `"d-phone:9.v": 1` pinned the post-upcast version.** It would
  have forced an edit to the frozen `__expected__.json` on a proposal bump. Fixed: replaced
  with `"d-phone:9.valid_from": "2026-10-01"`. The fixture test now refuses any sentinel
  whose field is `v`. This changes `__expected__.json` before the fixture has ever merged,
  so nothing frozen was edited.
- **New tests.** These simulate a later build by monkeypatching `proposal` to
  `SCHEMA_VERSION = 2` with a no-op upcaster:
  - `test_a_schema_bump_needs_no_edit_to_the_v1_fixture` checks that the `v1/maya` log loads
    as v2, the raw version check passes, and every sentinel, the fold and the hash hold. I
    wrote it first. Before the sentinel fix it failed with
    `d-phone:9.v: 2 after the rebuild, expected 1`.
  - `test_a_schema_bump_without_a_v2_golden_log_is_caught` checks that exactly
    `proposal v2 under tests/fixtures/v2/` is reported.
  - `test_a_vn_fixture_may_hold_older_versions_but_not_newer_ones` uses a `v2/` copy in
    `tmp_path`. With only v1 events it reports `no event at v2`. With one event at v2 whose
    type is still v1, it reports that the event is newer than the build.

Round 1 stranger pass: `_raw` fails loudly with `KeyError` or `JSONDecodeError` on a line
with no `seq` or a torn line. That is right for a golden log, which the rebuild test already
requires to have no skips. The monkeypatch is undone after each test, and the full suite
passes with the bump tests in it.

**Choices made inside the task:**
- **`tests/fixtures/v1/`, not D88's `tests/baselines/vN/`.** PLAN §9 and the issue name
  `tests/fixtures/v1/`, and CLAUDE.md's layout puts golden logs in `tests/fixtures/`. PLAN is
  the newer synthesized view of the same decision, and the directory name does not change what
  D88 asks for. I did not treat this as disagreeing with D88.
- **What `vN` means, given D96's per-type versions.** A fixture under `vN/` holds events up
  to `v == N`, with at least one at `N`. Older events are allowed because a v2 event can cite
  a v1 one of another type. Every type needs a fixture holding it at `v == K` under `vK/`
  for every version `K` it has had. (Before review round 1, every event had to be exactly
  `v == N`; see Review.) Another option was a global "release N" counter, but walle has no
  tagged release yet (see below). This reading is the one a test can enforce today.
- **The checker lives in the test file, not in `src/`.** Nothing in the product needs it, and
  putting it in `src/walle` would ship test code in the wheel.
- **No generator script is committed.** A golden log is written once and never regenerated.
  A generator in the repo invites "regenerate the fixture until the test passes", which is the
  exact failure the fixture exists to catch. The v1 log was written once, through
  `StreamWriter`, from a throwaway script. Before writing `__expected__.json`, I derived the
  fold by hand, and the computed fold matched it.
- **Sentinels are checked on events loaded back from the index**, not on the JSONL. That way a
  field lost by the upcast chain *or* by the index's encode/parse is caught.

**What to read first:** `test_a_field_lost_silently_is_caught_by_a_sentinel_and_not_by_the_hash`,
then `sentinel_failures`. Together they are the argument for D88's sentinels. After that,
the type and version coverage test, which is what makes a future v2 bump carry a v1 log.

**Noticed, not done:**
- D88's last bullet, "CI loads the last tagged release's fixtures", cannot be done yet: the
  repo has no tags. Once releases are tagged (task 25–26, packaging), the check could also
  check out the previous tag's `tests/fixtures/` and replay them through the current build.
  That needs a `.github/workflows/ci.yml` change, which is a CODEOWNERS path, so it is for
  Shravan.
- Nothing stops someone from editing a golden log together with its `__expected__.json`. A
  checksum file would be just as editable. The protection is that `tests/fixtures/v*/` changes
  are visible in review. A CODEOWNERS entry for `tests/fixtures/v*/` would make that explicit.
- **A bump of `assertion` will move every golden `state_hash`, even with a no-op
  upcaster.** I checked this with a probe. `content_hash` hashes `canonical_json`, which
  includes `v`, and `state_hash` hashes current and retired assertions by content hash. So
  the first `assertion` upcaster breaks
  `test_rebuild_over_every_fixture_equals_its_hash` on `v1/maya` unless the frozen hash is
  edited. That is the same problem as medium 2, but in `src/walle/events.py` (D66), not in
  this task's tests. There are two possible fixes: exclude `v` from the content hash, or
  hash the upcast form and accept that the hash moves on a bump. Either one changes D66's
  hash and every stored hash, so it needs a decision from Shravan. It is not done here.
- D88's rule that test names carry the last version where a structure existed only bites at
  the first upcaster. There is nothing to name yet.

**Cost:** not visible to the author agent from inside the run. Shravan can read it from
Hyperagent.
