# Task 13 (security follow-up): labels, un-enveloped errors, forged markers, path leaks

Issue #47, seven findings from a second-vendor read of PR #41. Branch `task/13-security`,
from `main` at `43d182a`. One commit. Built by the walle-author agent (Claude Fable 5.1).
`proposer.py` and `hazards.py` are untouched: #46 is changing them. `mcp_server.py` is
changed away from `main()`, which #42 touches.

**Shipped:**
- `src/walle/mcp_server.py`:
  - **Item 1.** A `why` step is labelled over every event its block quotes, not only the
    step's own closure. `_memory` takes `refs`; `why_result` passes the step and
    `Ledger.cited(step)`. A fact Maya typed, retired by a supersede the proposer wrote, is
    now `untrusted`: the block carries the proposer's reason.
  - **Item 2.** `index.CorruptIndexError` joins `_QUOTES_LOG`. The `except` for that tuple
    comes before `_SAID` on purpose and says so: the new error is a `StaleIndexError`,
    which `_SAID` names.
  - **Item 4.** `why_result` reads in `why.explain`'s order: `_freshen` (bring the index up
    to date, or learn the profile is read-only), then `index.ancestors`, **then**
    `index.load_records` for the statuses. `recall` keeps `_records` (update, or the index
    as last built). The module docstring has a new section, "Which state `why` describes".
  - **Item 5.** `_describe` hides paths for every error, not only `OSError`: the server's
    root (resolved) becomes `<root>`, and in a message said plainly any other absolute path
    becomes `<path>`. A path under the root keeps its tail, so "`<root>/principals/p-maya/
    index.sqlite`: not a walle index" still tells the person which file. An error that
    quotes the log is now returned as `ToolResult(is_error=True)` whose one block is
    enveloped, carries `_meta.ifc` `{untrusted, private}`, and is cut to `ERROR_TEXT_LIMIT`
    (1000) characters; `ToolError` carries text only and could not be labelled. Quoted log
    text gets the root replacement only, never the pattern (see the stranger pass).
  - A `FileNotFoundError` raised with one message no longer reads as "no log for this
    principal" whatever it said. `load_records`'s "no index yet. Run `walle rebuild`" was
    being reported as "no log": a status message that lied, found while fixing item 5.
  - **Item 7.** A `recall` block is now `summary: …`, then `valid: from … until …` when the
    belief's window (the fold's, so a window a later fact closed shows closed) has either
    end, then the body. `remember` refuses a text past `REMEMBER_TEXT_LIMIT` (2000, which is
    `RECALL_TEXT_LIMIT`: what `recall` shows whole) and refuses a text or summary that
    `hazards.flag` finds something in, naming the kinds and never the text (D77). Both
    before anything is written.
  - `ACTOR` is now `events.ACTOR_MCP`; `_RESERVED_PRINCIPALS` is `labels.RESERVED_ACTORS`
    plus `user`.
- `src/walle/envelope.py` (**item 3**):
  - Horizontal whitespace may sit between the opening brackets (`<< </untrusted…>>`,
    reproduced in the issue). `_BLANK` is every `str.isspace` character except the line
    breaks, as a character class and a set, and a test proves the split loses none.
  - A line break ends a would-be marker. `_skippable` no longer accepts `\n` (or `\r`,
    `\u2028`, …), so `In C, bit shift is <<` followed by a line starting `untrusted` is left
    exactly as it was (reproduced in the issue).
  - The gap set is every Unicode 15 format character (category Cf), written out by range,
    plus the zero-width marks that are not Cf (combining grapheme joiner, variation
    selectors) and the Hangul fillers. `_NAME_RE` allows gap characters between the letters
    of `untrusted`, so `un\u200btrusted` is found. The set is a literal, not computed from
    `unicodedata` at import (1.1 million lookups on every server start); a test holds it to
    the running Python's Cf category, so a newer Unicode fails loudly rather than quietly.
- `src/walle/index.py` (**item 2**): `CorruptIndexError(StaleIndexError)`, raised by the one
  place a row is parsed (`_parsed`), which `_rows` (behind `load_records` and `update`) and
  `ancestors` share. It names the file and the ref and says to rebuild. `FutureSchemaError`
  is not a `ValueError` and passes through unchanged, as before. `_rows` takes the path so
  the message can name it.
- `src/walle/why.py` (**item 1**): `Ledger.cited(ref)`: the refs a status was built from
  (the supersedes that retired an assertion and the assertions they named as replacements,
  the closers of a window, the keep or drop that decided a proposal). `status` and `cited`
  come from one `_account`, so they cannot drift apart. `walle why`'s output is unchanged.
- `src/walle/labels.py` (**item 6**): `is_principals_own` is false for any `Observation` and
  for any actor in `RESERVED_ACTORS`, whatever the principal is called. `ifc_all(events,
  refs, principal)`: most restrictive over several refs; a `ValueError` for none.
- `src/walle/events.py`: `ACTOR_MCP = "mcp"`, beside the other actor names.
- Tests, in a new section at the end of each file, all synthetic (Maya's kitchen, ferns,
  tomatoes): `tests/test_mcp_server.py` (+11 tests, 14 cases), `tests/test_envelope.py` (+4
  tests, 21 cases, and 4 new hostile inputs in the linear-time test), `tests/test_labels.py`
  (+3, 7 cases), `tests/test_index.py` (+2), `tests/test_why.py` (+1). 49 new cases; 983
  pass, 5 skip (the chmod tests skip only for root, and this run was not root, so they ran).

**Why:**
- **D84, D97.** A label is a claim about everything under it. `ifc(step_ref)` never looked at
  the supersede whose reason the block quoted, so the claim was false for exactly the text
  an attacker controls. The fix widens the closure to what the block quotes; it does not
  judge the text (D97: computed, never assigned).
- **D55.** Nothing from the log leaves un-enveloped. Pydantic's `ValidationError` on an
  index row carried the row in `input_value`, and `_SAID` showed it plainly because
  `ValidationError` is a `ValueError`. Naming the failure at the source (`CorruptIndexError`)
  is better than teaching the server to tell one `ValueError` from another.
- **D55 again, item 3.** The envelope is the one defence a host without label support has.
  Three ways past rule 1 are closed; the one false positive the issue named (`<<` then a new
  line) is opened. Defacing stays linear; the hostile-input test has four new cases.
- **D77** for `remember`: the host model's words are exactly what the proposer's gate exists
  for, and until now they bypassed it.
- **D81, D93** for `recall`: the summary is the index-mode line, and the window is the part
  of a fact a model most often needs and could not see.

**Verify:** the issue's criterion, each item a test that fails today:
- Item 1: `test_a_retirement_reason_from_a_non_principal_makes_the_step_untrusted` (and the
  control, `…by_the_person_keeps_the_step_trusted`); `test_the_ledger_says_which_events_a_
  status_quotes`.
- Item 2: `test_a_corrupt_index_row_arrives_enveloped_labelled_and_bounded[recall|why]`,
  `test_an_error_from_the_index_query_is_enveloped_too` (the `ancestors` path alone),
  `test_a_row_the_index_cannot_parse_is_a_corrupt_index_error`, `…_not_json_…`.
- Item 3: `test_more_forged_marker_shapes_are_defaced` (14 shapes: the space between
  brackets, tabs, a no-break space, ideographic and em spaces, zero-width space / word joiner
  / soft hyphen / ZWJ / ZWNJ / BOM / Arabic letter mark / isolate / tag character / CGJ /
  variation selector inside or around the name), `test_a_line_break_ends_a_would_be_marker`
  (5 cases, the `<<` newline case left untouched), `test_the_gap_set_covers_every_format_
  character`, `test_every_space_is_either_blank_or_a_line_break`.
- Item 4: `test_why_reads_the_records_after_the_chain` (the walkthrough below, staged).
- Item 5: `test_no_error_names_a_host_path` (five error types, one from outside the root),
  `test_a_missing_index_on_a_read_only_profile_says_no_index_not_no_log`, and the bound and
  label asserted in the item 2 tests.
- Item 6: `test_an_observation_is_not_the_principals_own_whoever_wrote_it`,
  `test_a_reserved_actor_is_not_the_principals_own_even_as_the_principal` (five actors),
  `test_ifc_all_is_the_most_restrictive_over_several_refs`.
- Item 7: `test_recall_carries_the_summary_and_the_validity_window`, `test_remember_refuses_
  text_past_the_bound_and_writes_nothing`, `test_remember_refuses_hazardous_text_by_kind_
  and_writes_nothing` (secret, instruction, contact), `test_remember_checks_the_summary_too`.
- Before the fix: 31 of them failed and `test_labels.py` failed at collection
  (`RESERVED_ACTORS` did not exist). Five of the new cases already passed on `main`
  (`<\u2068<</untrusted…>>>`, which rule 2 catches; the trusted control; three index tests
  that are not new) and are kept as regression cases.

Check output:
```
uv run pytest              983 passed, 5 skipped
uv run ruff check          All checks passed!
uv run ruff format --check 75 files already formatted
uv run pyright             0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough** (item 4, the read order in `why`). Reader A is the MCP server
answering `why(d-mbp:6)`, a staged proposal. Writer B is `walle review` keeping it. Each of
A's reads takes `index.lock` on its own and releases it; nothing is held across reads.

1. A calls `index.update` (`index.lock` exclusive, every stream shared, then SQLite). The
   index holds `d-mbp:6`, staged. A releases everything. On a read-only profile this step
   raises `PermissionError` instead, A keeps a sentence saying so, and goes on.
2. A calls `index.ancestors` (`index.lock` shared). The chain is `[(d-mbp:6, 0)]`. Released.
3. B keeps `d-mbp:6`: under `staging.lock` it runs `index.update`, sees the proposal staged,
   appends `d-mbp:7` (`assertion`, `caused_by: [d-mbp:6]`) under the stream lock, releases
   `staging.lock`. Something — B itself, a hook, the next `recall` — runs `index.update`,
   and the index now holds `d-mbp:7`.
4. A calls `index.load_records` (`index.lock` shared). It reads every row: `d-mbp:6` **and**
   `d-mbp:7`, because the index only grows and A reads it after step 3. Released.
5. A folds the rows: `d-mbp:6` is `kept as d-mbp:7`. A's answer has one step, and its status
   is true now.

The same five steps with the old order (rows before chain): A would read the rows at step 2,
before B, and the chain at step 4, after B. Asked about `d-mbp:7` instead, A's chain would
hold `d-mbp:7` and `d-mbp:6`, its rows neither keep; the answer would show the assertion that
kept the proposal and call the proposal `staged` in the next block. One answer, two states.
`test_why_reads_the_records_after_the_chain` stages step 3 inside a wrapper around
`index.ancestors`, so B lands exactly between A's reads, and asserts `kept as`.

If B lands between steps 1 and 2 instead, A sees the keep in both reads. If B lands after
step 4, A answers for the moment before the keep, which was true when A read it. Neither is
a contradiction. A rebuild between steps 2 and 4 that *shrank* the index (a lost stream tail)
could drop a chain event from the rows; `why_result` still adds the chain's own events to the
map it labels over, so no step is left without an event, and the status for a vanished ref is
`None`, said as nothing rather than as something.

The other lock-adjacent change is `CorruptIndexError` from `_rows`. In `_build` it is raised
under `index.lock` and inside `contextlib.closing(conn)`: the connection closes and the lock
goes with the file handle, as for any other exception there. In `load_records` the same. In
`ancestors` rows are fetched under the lock and parsed after it, as before.

**Stranger pass:** the diff is 850 lines and touches an error path, a lock order and
untrusted input, so the whole of it was re-read cold. Found and fixed:
- **Narrowing whitespace to space and tab was a regression.** The first version of item 3
  replaced `char.isspace()` with `" \t"`, which would have let `<<\u00a0/untrusted:…>>>` (a
  no-break space) through, when `main` catches it. `_BLANK` is now every `isspace` character
  minus the line breaks, and `test_every_space_is_either_blank_or_a_line_break` compares the
  two sets to `str.isspace` over all of Unicode so the split cannot lose one again. Two
  cases were added to the forged-shapes test (no-break space, ideographic and em spaces).
- **Hiding paths by pattern inside quoted log text broke defacing.** The first version ran
  `_PATH_RE` on every message. On a corrupt row holding `<<</untrusted:ID>>>`, `/untrusted`
  matched as a path and became `<path>`, the word was gone before `wrap` looked for it, the
  envelope's rule 1 no longer applied, and the planted id reached the model. Quoted log text
  now gets the exact root replacement only; the pattern runs only on messages said plainly.
  The test asserts the id is absent.
- **`<root>` then `<path>`.** After the root was replaced, the pattern ate the tail
  (`<root><path>: not a walle index`). The pattern now runs first with a replacement function
  that keeps the tail of a path under the root. A root of `/` is never replaced.
- **A refusal that named the wrong thing.** The hazard refusal said "the text carries" when
  the hazard was in the summary; it now says "the text or its summary".
- **`\x1f`** is whitespace to Python and was in neither set; it is a line break now.
- Checked and left: `ifc_all` on a ref missing from the map raises `KeyError`, which
  `_off_loop` would say as "no event …"; in `why_result` every cited ref comes from the fold
  over the same rows that build the map, so it cannot happen. `ERROR_TEXT_LIMIT` is applied
  before `wrap`, so a cut can land inside a forged marker; what is left is at most a bracket
  run, which rule 2 breaks. `_hide_paths` calls `root.resolve()` on an error path; one
  `realpath` per error is fine.

**Review:** pending.

**Choices made inside the task:**
- `CorruptIndexError` subclasses `StaleIndexError` rather than standing alone, so
  `walle why`, the one command that catches `StaleIndexError`, already prints "Run `walle
  rebuild`" for it without touching `cli.py` (which #45 edits). The cost is the ordering rule
  in `_off_loop`, stated in a comment beside `_QUOTES_LOG`.
- The label over a `why` step covers the step **and** every ref its status names, including
  the closers of a window and the keep of a proposal, not only supersedes with a reason. A
  status built from an event's fields quotes that event, even when the field is a validated
  ref or a date; widening can only make a label more restrictive, which is the safe
  direction, and the rule "what the status names" is one a reader can check.
- `recall`'s extra lines are inside the envelope, prefixed `summary:` and `valid:`. The
  summary is log text and may not sit outside (D55). The window is validated, but putting it
  in the head line would have changed the head's shape, which every test and the eval
  harness in #45 read as a bare ref. Five existing `recall` tests compared the whole body
  and now compare the part after those lines (`recalled()` in the test file); the task
  required that change.
- `REMEMBER_TEXT_LIMIT` is `RECALL_TEXT_LIMIT`. A body `recall` would have to cut is not a
  memory, and one number is easier to reason about than two.
- The fuzz oracle in `test_envelope.py` (the rule as one regex, which the linear scanner must
  agree with) was rewritten to state the new rule. The test's job is unchanged; its
  statement of rule 1 had to change with the rule, and it diverged first on exactly the
  `<<` newline case the issue describes.
- The gap set is a literal with a test against `unicodedata`, not a computed set. If Python
  ships a newer Unicode with a new Cf character the test fails, which is the behaviour
  wanted: the list is extended by hand and the change is visible in a diff.
- `_RESERVED_PRINCIPALS` is kept in the server even though the label no longer needs it: a
  principal named `observer` would still make the log hard to read.

**What to read first:** `_hide_paths` and `_describe` in `mcp_server.py` (where the stranger
pass found two bugs), then `_forged_spans` with `_BLANK_SET` and `_BETWEEN_BRACKETS` in
`envelope.py`, then the "Which state `why` describes" section of the server's docstring
against `why_result`.

**Noticed, not done:**
- Every CLI command but `walle why` lets `StaleIndexError` through as a traceback, so a
  `CorruptIndexError` from `walle rebuild` without `--full`, `recall`'s CLI cousins or
  `review` would show as a traceback saying to run `walle rebuild`. Arguably `update` should start over on a row it
  cannot parse, the way it does for a tracking record it disagrees with. `cli.py` is #45's,
  and the change to `update` is a design choice for a separate issue.
- `_PATH_RE` would turn a `https://…` documentation URL in a plainly-said message into
  `https:/<path>`. No plainly-said message carries one today (pydantic's do, and those are
  quoted log text, which the pattern does not touch).
- `hazards.py`'s `_BOUNDARY` rule for `<<<` does not know the gap characters or the
  `<< <` spacing that `envelope.py` now does. #46 owns that file.
- The eval harness in #45 reads `recall` bodies; once both land, its `_check_recall` should
  be read against the new `summary:` and `valid:` lines.

**Cost:** pending.
