# Task 5: Resolver fold

Issue #3. Branch `task/05`.

**Shipped:**
- `src/walle/resolver.py`: the fold. `resolve(records)` takes the `Record`s that
  `walle.reader` returns, from one stream or several chained together, and returns a
  `Resolution` with four parts:
  - `current`: live assertions as `Belief`s, each with its effective world-time `Window` and
    the refs that closed it.
  - `retired`: superseded assertions, with the supersedes that retired them and the
    `valid_to` those supersedes state.
  - `conflicts`: live contradictions whose sides overlap and neither starts later.
  - `unapplied`: supersedes and contradictions the fold could not apply, each with a reason.

  It also exports two pure helpers, `windows_overlap(a, b)` and `starts_later(a, b)`, and
  raises `ResolutionError` for a supersede cycle or for one ref naming two different events.
- `tests/test_resolver.py`: the three verify criteria (10 parametrised cases) plus 29 cases around them, including
  one integration test that writes two streams with `StreamWriter`, reads them back with
  `read_stream`, and folds them.

No other file changed. `resolver` was already listed in `RESOLUTION_ROOTS` in
`tests/test_constraints.py`, so the model and network check covers the new module with no
edit to that file.

**Why:**
- **D8.** State is a fold over the append-only log, and `supersede` is how anything is
  retracted. An event is *live* unless a live supersede targets it. Liveness is recursive, so
  superseding a supersede undoes a retraction without editing anything.
- **D93.** Assertions carry world time beside log time. Two assertions contradict only if
  their validity windows overlap. When they do and one starts later, the earlier window
  closes at the later fact's `valid_from`. The fold computes that close fresh every time and
  never rewrites the event.
- **D58.** A `contradiction` event names both sides in `caused_by`. That event is the fold's
  only source of candidates (see choices, below).
- **D103.** A date stays a date. The fold never widens one to midnight to compare it with an
  instant (see choices).
- **Hard constraint 3.** No IO, no clock, no model. The caller reads; the fold only folds.
  `walle.reader` is imported under `TYPE_CHECKING` only, so at runtime the resolver imports
  `walle.events`, `pydantic`, `dataclasses`, `datetime` and `typing`, and nothing that can
  touch a file. A test pins this on the source: the allowed runtime imports, and no call
  named `open`, `now`, `today`, `utcnow`, `now_instant`, `time` or `input`.

**Verify:** PLAN §9 says "pure-function tests: supersede hides; non-overlapping windows never
conflict; overlapping later-valid closes earlier".
- `test_supersede_hides`: a superseded assertion leaves `current`, appears in `retired` with
  its supersede's ref and `valid_to`, and leaves no conflict.
- `test_non_overlapping_windows_never_conflict`: five shapes, each checked both through
  `windows_overlap` in both argument orders and through a full fold with a contradiction
  event. The shapes are: touching, a gap, an open start, instants in different offsets that
  touch in UTC, and mixed precision on different days. Every case gives no conflict and no
  closed window.
- `test_overlapping_later_valid_closes_earlier`: four shapes, each written in both log
  orders. The earlier side's window becomes `[its start, later start)` with `closed_by` set.
  The later side is untouched, and the logged event's own `valid_to` is unchanged.

Before `resolver.py` existed, the test file failed at collection with
`ModuleNotFoundError: No module named 'walle.resolver'`. After the code went in I broke each
rule in turn: liveness always true, overlap never false, the close removed. Each break turned
its verify test red (1, 5 and 4 failures), so the tests depend on the rules and not only on
the shape of the output.

```
uv run pytest               167 passed
uv run ruff check           All checks passed!
uv run ruff format --check  26 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the resolver touches no log, lock or derived file, but its input
does. Each `read_stream` is a consistent snapshot under `LOCK_SH`, while reading two streams
is not atomic across them. Here is what that means with writer A on `d-mbp` and writer B on
`d-phone`, and a caller C that reads mbp first, then phone:
1. C reads `d-mbp` up to seq 3.
2. A appends `d-mbp:4`, an assertion.
3. B appends `d-phone:2`, a supersede targeting `d-mbp:4`.
4. C reads `d-phone`, including seq 2, and folds.

The supersede targets an event the fold was not given, so it lands in `unapplied` with
"targets d-mbp:4, which the fold was not given". That statement is true, and nothing is
hidden or shown wrongly. The next read sees both. The fold never guesses at an event it does
not have.

**Stranger pass:** the diff is over 30 lines, so I re-read `resolver.py` cold after the checks
went green. Findings:
- **A hand-built `Window` was unchecked.** `windows_overlap` and `starts_later` are public,
  and `datetime.fromisoformat` would take `20260301` or a space-separated instant and order
  it. `Window` now validates each value against the log's own `WorldTime` rule and refuses
  `Z` (it asks for `+00:00`). Assertions arrive already normalized, so this costs the fold
  nothing. There is a test for it.
- **A supersede whose target is itself** is allowed by the schema. The cycle check already
  caught it. A test now pins the message `cycle: d-mbp:2 -> d-mbp:2`.
- **A status message** said "names 1 assertions". It now matches the count.
- **Ignored errors:** none. No `except` anywhere in the module. The `assert isinstance(...)`
  lines only narrow types where the branch above already guarantees the type. **Lock scope
  and leaks:** none, the module holds no resource. **Recursion:** liveness is an iterative
  depth-first search, so a long supersede chain can't hit Python's recursion limit.
- My own purity test then failed on the new `pydantic` import. Pydantic validates and does
  no IO, and `walle.events` already imports it, so I added it to that test's allowlist. That
  test was written in this task; no pre-existing test was edited.

**Review:** pending.

**Choices made inside the task:**
- **Candidates come only from `contradiction` events.** Assertions have no subject or
  predicate field, so deciding that "Lives in Pune" and "Lives in Bangalore" are about the
  same thing is judgment, and judgment in the fold breaks constraint 3. D93 adopted
  Graphiti's rule because it is "pure once candidates are chosen". D58 makes the Curator the
  chooser, and the fold applies the rule. Two overlapping assertions with no contradiction
  event between them are not a conflict (a test pins this).
- **A closed window stays in `current`.** "Lived in Pune until March" is still a true belief.
  It is history in world time, not a retraction in log time. `current` means live in the log.
  Answering "true *now*" needs a date, and the fold can't read the clock, so a caller compares
  the windows against a date it passes in. The other reading, hiding closed beliefs, would
  make `supersede` and contradiction indistinguishable and would lose the bitemporal history
  D93 exists to keep.
- **Each contradiction is judged on the windows as written,** not on windows another
  contradiction already closed. That keeps the answer independent of input order (tested with
  20 shuffles over two streams). When several later facts close one, the earliest close wins
  (Pune, then Mumbai, then Bangalore).
- **A date and an instant on the same calendar day can't be ordered.** Ordering them would
  need a time zone for the date, which D103 says not to invent. An unknown order never closes
  a window and never counts as disjoint: it is reported as an open conflict. On different
  days the instant's *written* date is used.
- **A contradiction with a superseded side is moot, not unapplied.** D58 says a later
  supersede is how a contradiction gets resolved. A superseded contradiction is a dismissal.
- **Strict about what a contradiction names.** It must name exactly two assertions, and every
  ref it names must have been passed in. If an evidence ref is missing, the fold can't know
  whether it was a third assertion, so the contradiction is reported rather than applied.
  Observations alongside two assertions are fine.
- **`Retired.by` is a tuple, and `valid_to` is the earliest any live supersede states.** Two
  live retractions of one assertion are legal. I changed this API in my own new tests before
  the implementation existed.
- **The same record twice counts once; the same ref with different content raises.** The
  first allows passing one stream twice. The second is "one ref naming two events", the
  failure the log exists to prevent.

**What to read first:** `resolve()` from "Rules 2-5" down, together with `windows_overlap` and
`_compare`. Mixed date and instant precision is the part most likely to be wrong in a way the
tests don't show. The other part is the decision that candidates come only from
`contradiction` events. If Shravan reads D93 as asking the fold to find candidates itself,
that is a new decision, and this task would need `needs-shravan`.

**Noticed, not done:**
- `snapshot.forgets` (D89) is not honored. A snapshot does not hide the assertions it stands
  in for. Whether it should, or whether a snapshot only replaces observations in the hot
  tier, isn't settled by D89. It belongs with task 7 or a decision.
- Rule 5 conflicts are returned, not written. The fold can't append. Surfacing them (D30),
  and writing whatever event that produces, is a later task.
- Supersedes that disagree about `valid_to` are reduced to the earliest. The disagreement
  itself isn't reported.
- Dangling refs are reported in `unapplied` here. Task 6 is where a dangling `caused_by`
  fails the rebuild.
- `Resolution.belief` is a linear scan. Fine at Phase 0 sizes.
- PLAN §9's status column still shows task 4 as next. PLAN.md is a CODEOWNERS path.

**Cost:** pending.
