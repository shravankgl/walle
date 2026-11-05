# Task 93: the reviewer fails closed on JSON nested past the recursion limit

Issue #93. Branch `task/reviewer-recursion`, from `main` at `69125a4` (task 92's merge). The one
low finding left on PR #87's review, filed as its own issue after the Builder run that first
carried the fix lost the claim race on #73 (PR #88 landed from a scheduled run instead). The fix
and its two tests are re-applied here against current `main`; nothing else from that run is in
this diff.

**A note on the label.** #93 carries `queued`, not `agent-ready`. The owner set that on purpose
so the schedule could not claim it, and authorised this run directly in the thread; the brief's
label rule is his, and this is his instruction. Recorded here so the exception is visible.

**Shipped:**
- `src/walle/reviewer.py`, five lines. `json.loads` raises `RecursionError` — not a
  `ValueError` — on input nested past the interpreter's limit, and both call sites caught
  `ValueError` only. `_parse(reply)` now reads such a reply as "not JSON": `review` returns
  `ask`, verdict `None`, provenance `user`, reason "malformed reply: not JSON", to the person,
  exactly as a fenced or truncated reply does. `_texts(arguments)` now returns the arguments as
  written when they are nested past the limit: they are not JSON the reviewer can look inside,
  so the hazard scan runs over the text itself (and over the whole shown block, as before), and
  the model is then asked on that text alone. Invariant 3's docstring names the case.
- `tests/test_reviewer.py`, 2 tests, both failing on `main` with `RecursionError` before the fix:
  - `test_invariant_3_a_deeply_nested_reply_reaches_the_human_like_any_malformed_one`: a reply
    of 100 000 nested brackets is `ask`, no verdict, provenance `user`, "malformed" in the
    reason, and the reply is not quoted.
  - `test_invariant_7_deeply_nested_arguments_are_scanned_as_text_not_raised_on`: arguments of
    the same shape give `_texts` of exactly one text, the model is called once and `review`
    returns; the same arguments with an arguing suffix are `deny` without a call.

**Why:** D76 invariant 3, fail closed: the only path to `go` is a well-formed `allow`, and
*anything* else reaches the person — a `RecursionError` escaping `review` is the one shape that
reached nobody. D86: an unreadable reply is a halt to the person, never a tool error. The
arguments side is invariant 7: what is scanned must be what is shown, and a decode that raises
would have stopped the scan before the block was checked.

**Verify:** *a reply nested past the interpreter's limit reaches the human as malformed, like
any other malformed reply; arguments nested past the limit are scanned as text, and a hazard in
them is still denied before any model call.* The two tests above, one per sentence.

```
uv run pytest               2894 passed, 30 skipped in 107.58s
uv run ruff check           All checks passed!
uv run ruff format --check  164 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 30 skips are the same as on `main` (the escape tests; no daemon here). `executor.py` is
untouched, so no Mac run is needed and auto-merge is on.

**Two-runner walkthrough:** not applicable; the reviewer touches no log, lock or derived file.

**Stranger pass:** done, though the diff is under thirty lines. Checked that `RecursionError`
is the only non-`ValueError` `json.loads` can raise on a `str` (it is; `TypeError` needs a
non-string, which `_parse` refuses first and `Action.arguments` is typed as `str`). Checked
that catching it in `_texts` does not weaken the scan: the arguments as written are always the
first element, and `scanned` also includes the shown block, so a hazard in the text is seen
twice as before. Checked the test does not depend on the exact recursion limit: 100 000 levels
is two orders above CPython's default and above anything a test runner sets.

**Review:** independent review is the reviewer agent's, on this PR, per the brief.

**Choices made inside the task:**
- **Catch, not raise the limit.** `sys.setrecursionlimit` is process-wide and a model reply is
  not a reason to change it; refusing is the rule everywhere else in this module.
- **Arguments past the limit are scanned, not denied.** They are the agent's own text, not the
  model's; a shape the decoder cannot follow is not by itself evidence of arguing. The model
  sees them inside the envelope and judges them; the hazard scan still runs. Denying on depth
  alone would make the detector a matter of length, which the proposer's docstring warns
  against.
- **Nothing else from the Builder patch.** The owner asked for the reviewer fix alone; the
  Builder shipped as #88.

**What to read first:** the two `except` lines, then the second test's last two assertions.

**Noticed, not done:**
- **The same gap exists in `walle.builder` (#88, two call sites at `json.loads` of the reply
  and of a staged body) and `walle.proposer` (one, the reply).** Each catches `ValueError`
  only. Checked, not fixed: outside this issue. The same two-token change applies to each.

**Cost:** not available to the author at write time; Hyperagent's figure for this thread to be
filled in by the owner.
