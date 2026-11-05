# Task 116: the Builder refuses what the comparison found

Issue #116. Branch `task/builder-hardening`, from `main` at `834c6d8` (task 115's merge). The
owner made the branch empty as the claim and assigned the task to this agent in a comment on
the issue; it was at `main`'s head when work began, so there was nothing to merge in. Phase 2's
Builder landed as PR #88 from a scheduled run; this thread had built the same task
independently and, asked afterwards for a read-only comparison, found five places where the
merged module was weaker. This task ports those guards into `main`'s design — the staged
`proposal` event, `run_step` as the one door — and nothing of the other implementation's
staged-files approach. `executor.py` is untouched, so no Mac run is needed and auto-merge is on.

**Shipped:**
- `src/walle/builder.py`, about +95/−20 inside `_parse_reply`, `_read`, `_read_step`,
  `_inside` and `parse_workflow`, plus four constants and three helpers:
  1. **Deep nesting fails closed.** Both `json.loads` sites catch `(ValueError, RecursionError)`.
     A reply nested past the interpreter's limit is "not JSON" from `build`; a kept body nested
     past it is "not JSON after the first line" from `parse_workflow`, so `run_step` refuses
     rather than raising. The same two-token fix as #93 in the reviewer.
  2. **A lone surrogate is refused at `build`.** `_utf8` walks every string in the decoded
     reply, keys included, and `_parse_reply` refuses with "holds text that cannot be written
     as UTF-8" before `_read` looks at a key. Before, `"\udc80"` passed every type check,
     `build` returned a `Draft`, and `stage` raised `pydantic.ValidationError` from
     `Proposal(...)` — an exception the docstring did not list, two calls later.
  3. **Key names are counted, not quoted.** `_keys` names missing keys (this module's words)
     and counts unexpected ones (the model's): `keys missing ['steps'], 1 unexpected`.
  4. **Bounds.** `COMMAND_PARTS = 64` words per command, `PATH_COUNT = 64` paths per `reads`
     and per `writes`, `PATH_CHARS = 255` per path, and `BODY_CHARS = 60_000` on the rendered
     staged body as the backstop (`_read` renders with a placeholder source and measures; the
     real source is a ref of at most 84 characters, so the staged body is within a few bytes
     of what was measured). Before, one step with 200 000 one-letter words staged a 3.2 MB
     line in the log.
  5. **A title holds no line break of any kind.** `_one_line` refuses every character
     `str.splitlines` breaks on — `\r`, `\x0b`, `\x0c`, `\x1c`–`\x1f`, `\x85`, `\u2028`,
     `\u2029` — not only `\n`, for the reply's title and each step's. The summary is
     `workflow: <title>` and D81 says one line.
- `tests/test_builder.py`, 22 new tests under one `#116` heading, one or more per case, each
  asserting the refusal and, where the test has a log, that no line was written: the deep
  reply (and the deep kept body through `run_step`, with the fake client recording nothing);
  six places a surrogate can sit, including a key; two invented keys with a nonce that must not
  appear in the reason, and one that missing keys are still named; five replies past a bound
  and one exactly at the bounds that is accepted; five line-break characters in both titles.

**Why:** D86 and D76 invariant 3 — a reply the module cannot read is a refusal to the person,
never an exception out of `build` or `run_step`; a `RecursionError` was the one shape that
escaped. D18 and D77 — a refusal must not carry the model's words, and a key name is the
model's words. D81 — the summary is one line, and `\r` is a line. D55 — unchanged; the envelope
already held. The bounds are the proposer's "a cut that hid a hazard would make the gate a
matter of length" turned around: a body the log cannot reasonably hold is refused, not
trimmed. The dormant rule is untouched: every new check sits after `enabled`, and
`test_the_builder_is_off_by_default` still shows no call and no write.

**Verify:** *a failing test for each case first, with nothing written to the log on any
refusal.* Each test was run against `main`'s logic before the fix (with the four new constants
stubbed so the module imported): 23 parametrised cases failed — five raised `RecursionError`
or let the surrogate through to a `Draft`, the rest returned a `Draft` where a `Refusal` was
required or quoted the nonce. The two baseline tests (`missing_keys_are_still_named`,
`a_reply_at_the_bounds_is_accepted`) passed before and after, as they should. One test-helper
mistake found by the first run: `_approval()` itself appends two lines (the proposal and the
keep), so the line count is taken after it.

```
uv run pytest               3982 passed, 35 skipped in 125.72s
uv run ruff check           All checks passed!
uv run ruff format --check  193 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 35 skips are the same as on `main`.

**Two-runner walkthrough:** not applicable; nothing here changes a write, a lock or a derived
file. Every change is a refusal before `stage` is reached.

**Stranger pass:** done on the diff (two error paths, one new walk over untrusted data).
Checked that `_utf8` runs before `_keys`, so a surrogate in a key name is "cannot be written as
UTF-8" and not a counted unexpected key — either is a refusal, but the first is the true
reason. Checked that `_one_line` keeps the three distinct messages the existing `MALFORMED`
tests match on ("not one line of text", "longer than", and the new "holds a line break").
Checked that `BODY_CHARS` cannot be gamed by the `source` field: `_read` renders with a
placeholder of fixed length and the real ref is bounded by `_REF_RE`. Checked that `_utf8`'s
walk is iterative, so a reply that is deep but under the recursion limit cannot recurse here.
Looked for and not found: any path where a reason string interpolates model text (ids are
`NodeId`-validated before they appear in a reason; counts and our own key names otherwise).

**Review:** independent review is the reviewer agent's, on this PR, per the brief.

**Choices made inside the task:**
- **`BODY_CHARS = 60_000` and `COMMAND_PARTS = 64`, not larger.** Eight steps of a few
  4 000-character arguments is a real workflow; sixty-four words is more than any `sh -c`
  needs. A workflow that needs more is a mission to design, which is what `MAX_STEPS`'s own
  docstring says.
- **Refuse the surrogate, not strip it.** Stripping would change what the model said before
  the person sees it; refusing says a reply the log cannot hold is not a workflow.
- **Count unexpected keys rather than name a sanitized form.** A sanitized key is still the
  model's choice of word; the count tells the person as much as they need to re-ask.
- **`_LINE_BREAKS` is `str.splitlines`' set**, spelled out rather than computed, so a reader
  can see it and a future Python cannot change it silently.
- **Nothing from the other implementation's design** — no staged files, no `after` DAG, no
  per-step `file`. The issue asked for the guards; the design is #88's.

**What to read first:** `_parse_reply` and the three helpers under it (`_utf8`, `_keys`,
`_one_line`); then the `#116` test section.

**Noticed, not done:**
- `walle.proposer._parse_list` has the same `ValueError`-only catch around `json.loads` of a
  model reply (`proposer.py:512`). Outside this issue; the same two-token change.
- `executor.run` still has the `OSError` gap #88's devlog noted (a client that exists but
  cannot be started raises rather than refusing); `reap` and `_remove` were fixed, `run` was
  not. Outside this issue and it touches `executor.py`, which would need a Mac run.

**Cost:** not available to the author at write time; Hyperagent's figure for this thread to be
filled in by the owner.
