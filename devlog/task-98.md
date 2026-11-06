# Task 98: the model maintenance pass, suggestions only, dormant

Issue #98. Branch `task/p3-maintenance`, claimed at `main` (`bece9bc`) on 2026-11-06. Blocked
by #96 (closed). Phase 3 task (R8). Dormant: nothing reads a log or asks a model unless the
owner sets both `WALLE_MAINTENANCE=on` and `WALLE_ROT=on`.

**Shipped:**
- `src/walle/maintenance.py` (new, about 410 lines with its docstring). The docstring states
  seven rules.
  - `suggest(register, records, now, model, *, stale_after, archive_after) -> Suggestions`.
    It runs `walle.rot.rot` first. Then it shows the model every *offerable* active
    automation (not pinned, not held by a live `depends_on`), with its name, title and rot
    label, and reads back two kinds of suggestion. `retire` names one automation, and
    `consolidate` names two or more and gives the title of the one that would replace them.
    It returns `Suggestions` with the rot labels unchanged (`.rot`), `.retire`,
    `.consolidate`, `.withheld` (ref and hazard kinds), `.dropped` (one reason per refused
    reply item), `.failures` (one error class per request with no readable reply) and
    `.requests`.
  - `suggestions(root, principal, model, *, now=None, ...)`. This is the read. It refuses
    with `MaintenanceOffError` before reading anything unless both switches are on.
  - `MAINTENANCE_SWITCH`, `SWITCH_ON` and `enabled(environ)`, the same shape as every other
    switch.
  - `INSTRUCTIONS`, `TITLE_CHARS` (200, an add's own limit), `REASON_CHARS` (300) and
    `REASON_WITHHELD`.
- `src/walle/proposer.py`: `Request.kind` gains `"maintain"`, beside `"curate"`. It is one
  line plus its docstring. There is no behaviour change.
- `tests/test_maintenance.py` (new, 51 tests).
- `tests/test_rot.py`, `test_nothing_in_the_package_calls_rot_on_its_own`: **the task
  requires this test edit.** It asserted that only `automations.py` imports `walle.rot`. A
  pass "on top of the deterministic pass" has to call `rot`, so the list is now
  `["automations.py", "maintenance.py"]`. The check is no weaker. Any third importer still
  fails it, and `test_nothing_in_the_package_calls_the_pass_on_its_own` checks that no module
  imports `walle.maintenance`.

**Why:**
- **D114:** maintenance runs at two speeds. The deterministic pass always runs, with no
  model in it (`walle.rot`, #96). The model pass is separate, opt-in and off by default.
  `pinned` bypasses every automatic transition, and nothing a live automation names is
  retired on usage grounds. This module never shows either kind to the model, so it can
  never suggest one. D114 also says background work uses its own model client and never
  touches a live session's prompt cache. The model is an argument here, and the module keeps
  no client and no session.
- **D72, and the owner's decision 1 on #96:** only a person changes the register, and the
  rot status is derived and writes nothing. This pass follows the same line. A suggestion is
  a value returned to the caller, never an event. "Nothing changes without approval" holds
  because the only way a suggestion takes effect is a person running `automations.retire`
  (or `add`) themselves, and the register refuses a machine actor doing so (rule 4 there).
- **D55 and the proposer's hazard rules:** an automation's title is a person's free text, so
  it is handled exactly as the Curator handles a belief. It is flagged whole and withheld by
  ref and kind, enveloped under a per-request marker, and sent through `pseudonyms.Egress`,
  the one door.
- **D117 and the owner's dormant condition:** install is not enable. Two switches are
  needed, and neither turns the other on. No event can reach either. There is no CLI
  command, and nothing calls the pass.

**Verify:** *off by default; it only proposes, and nothing changes without approval; the
deterministic pass runs without it.*
- *Off by default:* `test_the_pass_is_off_by_default` (nothing is created on disk),
  `test_off_is_checked_before_the_log_is_read` (the read is patched to fail, with no switch,
  rot only, and maintenance only), `test_the_rot_switch_alone_does_not_turn_it_on`,
  `test_it_needs_the_deterministic_pass_turned_on_too`, `test_only_the_one_spelling_turns_it_on`
  (seven wrong spellings), `test_nothing_in_the_log_can_turn_it_on`,
  `test_the_cli_gains_no_maintenance_command` and
  `test_nothing_in_the_package_calls_the_pass_on_its_own`. Every off test hands it a model
  that fails the test if it is asked.
- *Only proposes:* `test_the_pass_writes_nothing` compares every byte under the root before
  and after `suggest` and `suggestions`, and the folded register too.
  `test_the_module_has_no_way_to_write` works at the AST level. It checks for no writer,
  inbox, staging, index, builder or executor import, and that the only names taken from
  `walle.automations` are `Entry`, `Register` and `fold_automations`. It also checks that
  nothing calls `add`, `retire`, `pin`, `unpin`, `restore`, `ask` or `answer`, and that every
  `.append` or `.extend` goes to one of the module's own local lists.
- *Nothing changes without approval:* `test_nothing_changes_until_a_person_retires_it`. The
  suggested automation stays active. The curator actor retiring it is refused, and it stays
  active. A person's retire takes it out, and the next pass no longer suggests it.
- *The deterministic pass runs without it:* `test_the_labels_are_rot_s_whatever_the_model_does`
  compares `.rot` with `rot.rot(...)` for five models: the fake, `[]`, a reply that is not
  JSON, a reply that is not a list, and a model that raises.
  `test_a_model_that_fails_leaves_the_deterministic_labels`,
  `test_rot_labels_with_the_model_pass_off_and_absent` and
  `test_rot_imports_nothing_of_the_model_pass` (`rot.py` and `automations.py` import nothing
  of it, and `rot`'s import closure still reaches no model) cover the rest.
- *Around it:* pinned and held automations are never shown, by ref or by name
  (`test_pinned_and_held_automations_are_never_shown`). A reply naming either, or a made-up
  ref, is dropped as `unknown-item`. Eleven malformed, oversize, hazardous or marker-carrying
  reply items are each dropped with their reason. Duplicates are dropped, including the same
  set named in another order. A reason with a hazard or marker is replaced by
  `REASON_WITHHELD`, and every reason is cut to one line. A title with a contact detail is
  withheld and its text is never in any prompt. A title shaped like a marker is a `boundary`
  hazard, so it is withheld whole. When the door refuses, nothing is sent and every item is
  withheld. Requests split at `REQUEST_CHARS` with a fresh marker each. With nothing
  offerable, the model is not asked. A naive `now`, and thresholds with no order, are refused
  before the model is asked.

Tests first: before the module existed, `tests/test_maintenance.py` failed at collection
(`ImportError: cannot import name 'maintenance' from 'walle'`). I then mutated the code 11
ways: show every active automation instead of only offerable ones, switch on by default,
skip the rot-switch check, skip the grounding check, call the model directly instead of
through the door, skip the title hazard check, skip the item hazard check, skip the
duplicate check, pass a hazardous reason through, re-raise a model failure, and accept a
retire naming two items. All 11 were killed by `tests/test_maintenance.py`.

```
uv run pytest               4574 passed, 35 skipped in 152.87s
uv run ruff check           All checks passed!
uv run ruff format --check  228 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 35 skips are the same as on `main`. No test assumes root. The suite ran as uid 1000.

**Two-runner walkthrough:** there is no writer, so there is nothing to walk through.
`suggestions` reads each stream under its own `LOCK_SH` for a moment, as `rot.labels` does,
and holds no lock while the model is asked. Suppose two passes run at once. If no automation
event lands between their reads, they see the same labels. Their suggestions may differ,
because a model's answer is not reproducible, and that is acceptable because nothing is
written from either. If a person retires an automation between pass A's read and their own
look at A's output, A's suggestion goes stale. Acting on it then fails safely: `retire` by
name finds no active automation of that name and is refused.

**Stranger pass:** done on the whole diff. What it found:
- *An error path that was not stated.* Errors from the model other than `ModelCallError`
  and `ModelReplyError` (for example an adapter's `ModelUnavailable`) propagate. That is
  right, because a misconfigured model is not a failed request, but the docstring did not
  say so. It does now, and it says nothing was written either way.
- *A title test that tested the wrong thing.* My first marker test expected the marker to be
  defaced in the prompt. In fact `hazards.flag` already reports a marker-shaped title as
  `boundary`, so it is withheld whole and never reaches the envelope. The test now asserts
  that stricter behaviour.
- *A test fixture past the WIP cap.* Two tests added an eighth automation to a seven-strong
  household, and the fold rightly refused it. Those tests now start from a small log.

Looked for and not found: an ignored error (every `except` either records the failure or
withholds the items, and names the case), a file handle left open, a lock held across the
model call, and a status message that claims more than happened. Failures are recorded by
error class only, never by text, so no provider text or key can come back through them.

**Review:** independent review is the reviewer agent's job on this PR, per the brief. I did
not run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **Suggestions are returned, not written.** The alternatives were an inbox `ask` per
  suggestion, or a new event type. Either would make a model's unreproducible output a fact
  in the log, and an approved `ask` would need an executor to carry out the retire, which is
  a new decision about machine authority over the register (D72). Returning a value keeps
  the pass on the same line as rot's decision 1, where the status is derived and writes
  nothing. Persisting suggestions for a person to answer later is listed below.
- **Two switches, not one.** The pass reads the same log the deterministic pass does, and
  #96 gated that read behind `WALLE_ROT`. Turning on the model pass should not quietly
  turn on rot reading, so both are required, and the refusal names the missing one.
- **Pinned and held automations are not shown at all**, rather than shown as context and
  then refused if named. Not showing them is the simplest way to make sure they are never
  suggested. The cost is that the model cannot say "X duplicates the pinned Y".
- **The local tier is not refused.** The Curator refuses `ollama:` for D24's diversity
  reasons. Nothing in D114 asks for that here, and a local model is the more private choice
  for reading a person's automation titles.
- **No fake model in the package.** The Curator ships `FakeCurator` because `walle curate
  --model fake` needs one. There is no CLI command here, so the fake lives in the test file.
- **"Rewrite" is left out.** ARCHITECTURE's line says the model pass "consolidates and
  rewrites". The issue says consolidation and retirement, so that is what is built.

**What to read first:** `suggest()` in `src/walle/maintenance.py`, about 90 lines, which is
the whole pass. Then `_check`, which is every rule a reply item must pass.

**Noticed, not done:**
- There is nowhere for a person to see suggestions yet: no CLI command, and nothing calls
  the pass. If the owner wants them to persist, the natural places are an inbox
  `notification`, or `ask`s with an executor, and either is a decision for
  `CONVERSATION.md`.
- A consolidation is a suggestion only. Carrying one out is an `add` of the new title plus
  retires of the old ones. With the register full, the add needs `retire=` for one of them,
  so a person does it in several steps. A helper for that would be a register write and
  belongs with the register.
- The Curator calls `model.complete` directly rather than through `pseudonyms.Egress`, while
  the proposer and this pass use the door. That is worth aligning, separately.
- `ARCHITECTURE.md` and `PLAN.md` do not mention `walle.maintenance` or
  `WALLE_MAINTENANCE`. They are CODEOWNERS paths and are left for the owner.

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
