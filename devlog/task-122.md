# Task 122: Audit — conversation rules, its switch, and fold hygiene

Issue #122, audit findings 3.1, 4.1 and 5.3. Branch `task/audit-conversation`, created empty
by the owner as the claim and reused here as his comment on the issue says; from `main` at
`dfbeb90`, which already holds #124's approvals fix (`walle.speakers`, `Order` that cannot be
built for anyone but the principal). Author: walle-author-fable (Fable 5.1). Every
reproduction in the issue was re-run against `dfbeb90` first and held; each is now a test that
fails there and passes here as a refusal that writes nothing. Dormant: nothing new is reachable
without `WALLE_CONVERSATION=on` (and `WALLE_DIGESTS=on` for the window), and
`test_4_1_permit_is_off_by_default` proves the one entry point that had no switch now has one.

The three properties the owner asked for, and where each now lives:

1. **A speaker who is not this folder's owner is the lowest tier for what this folder loads,
   whatever role they hold elsewhere.** `conversation.spoke_at(principal, speaker, role)` is
   the rule; `turn` records it from `Conversation.principal` (no role is written for anyone
   else), and `fold` takes the principal and enforces it over every utterance already in the
   log, so an old `Utterance` claiming `role=operator` for another principal folds to
   `UNKNOWN` on rebuild (3.1).
2. **Every public entry point honours `WALLE_CONVERSATION`.** `permit` now calls
   `_require_on(environ)` as its first statement, like `open_conversation` and `turn`, and a
   source-level test holds all three to it (4.1).
3. **Both folds go through the resolver's live events.** `digests.window_records` keeps live
   records only, each once; `conversation.fold` and `_turns` see each utterance once and raise
   `ResolutionError` for one ref naming two events (5.3).

**Shipped:**

- `src/walle/conversation.py`:
  - `spoke_at(principal, speaker, role) -> Tier`, new and public: the speaker's role when
    they are the principal, `Tier.UNKNOWN` for anyone else. One function, so `turn`'s record
    and `fold`'s ranking cannot disagree.
  - `fold(records, *, principal)` and `floor(records, conversation, *, principal)`: the
    principal is keyword-only with no default. There is no fold without one, so no caller can
    rank another principal's role in this folder by forgetting to say whose folder it is.
  - `_utterances(records)`: every utterance through `resolver.live_events`, each ref once.
    `fold` and `_turns` both read from it. Liveness is deliberately **not** applied: a
    supersede is an event the agent can append, and nothing in the log may raise a floor or
    unsay a turn (D61, D117). The docstring says so.
  - `turn`: writes `role=None` for a speaker whose id is not `conversation.principal`.
    Another principal's role is a fact about their folder; this log keeps no facts about them
    (§7). The fold ranks the record the same way whatever it says, so the record is the honest
    one, not the enforcement.
  - `frame`: for a named speaker who is not the principal, says the role walle attached to
    them is theirs in their own folder, not here, and that here they hold no role. It no longer
    says "Their role here is operator", which was false.
  - `permit`: `_require_on(environ)` first, before `done` is looked at or `grants.check` is
    reached. Docstring and module docstring updated: the dormant rule now names all three
    entry points.
- `src/walle/digests.py`:
  - `window_records(records, since, until, *, include_since=False)`: materialises `records`
    (read twice), computes liveness with `resolver.live_events` over everything handed in — not
    only the window, so a fact forgotten after the slot closed is still gone by the time the
    digest is composed — and keeps `live[r.ref]` records inside the window. The supersede
    itself is in the window when its `ts` is: a digest may say a fact was withdrawn, not what
    it was.
  - `Due.first: bool`: no digest has ever run. `run` passes it as `include_since`, so a record
    stamped exactly at the schedule's anchor is in the first window (slot 0 starts there).
    Later windows start after the last run's `ts`, exclusive, as before — that run's own
    window already reached it.
  - Module docstring: rule 9, the window holds live events only, each once.
- `src/walle/events.py`: `Utterance.role` docstring says the role is the one attached *in this
  folder*: `None` for anyone who is not the principal. No schema change; the field was already
  optional.
- `tests/test_audit_conversation.py`, new: 22 test functions, 27 cases. Per finding: the
  reproduction, the cases around it, and one seam test that drives the real modules together
  (`conversation`+`profiles`+`index` for 3.1, parametrised over every non-principal speaker ×
  role; `conversation`+`grants` with real grant files for 4.1; `digests`+`resolver` through a
  real `run` for 5.3). Plus a source-level test that both folds import `live_events` and no
  private resolver name.
- `tests/test_conversation.py`, edited (the task requires it; see *Choices*). Eight tests
  changed: five swap `SAM_STAKEHOLDER` for a new `ADA_STAKEHOLDER` so they keep testing the
  by-role ratchet they were written for; `fold`/`floor` calls pass `principal=`; the grants
  test's `ada_again.conversation.floor` is now `Tier.UNKNOWN`; the golden-log test expects
  `garden` (where ravi, not ines, spoke) at `UNKNOWN`. **The fixture itself is unchanged**: the
  same JSONL now folds to a lower floor, which is the point of 3.1.

**Why:**

- **D61 / §7 / the owner's decision of 2026-11-05.** D61 says context ratchets to the lowest
  role that has spoken. Task 102 settled that nobody reads another principal's folder, not even
  an admin. A role is held in one profile; Sam's `operator` is Sam's folder's. In Ada's folder
  he holds nothing, so his tier here is the bottom, and Ada's `learned.md` and `user.md` unload
  the moment he speaks — exactly as they would for a guest. Before this, `authority` already
  knew Sam was not the operator here (he got a `Request`) while `fold` ranked him as one: the
  two halves of D61 disagreed with each other.
- **D117, the dormant rule.** `permit` is the one impure public function besides
  `open_conversation` and `turn` — it opens grant files via `grants.check`. With no switch of
  its own, `WALLE_GRANTS=on` alone activated it. Now the conversation's switch is checked
  first, and grants' switch decides the rest, as the module always said.
- **Hard constraint 3 and D8 (supersede, never delete).** `resolver.live_events` is the one
  liveness in the project; `walle.missions` already folds from it. A window that filtered by
  time alone handed forgotten facts to the composer, which then wrote them into a `digest`
  event (constraint 4 makes that permanent). A fold that iterated raw records counted a stream
  read twice as two. Both now start from the seam every other fold starts from.
- **D31 / D63 (rule 5 in `digests`).** `due` is untouched: a superseded `digest` still counts
  as a run, because a sent message cannot be unsent. Only what the *composer sees* changed.

**Verify:** the issue's criterion — each reproduction a failing test first, then a refusal
that writes nothing, plus one seam test per finding.

Before (tests at this commit, `src/` at `dfbeb90`): `25 failed, 2 passed` in
`tests/test_audit_conversation.py`. The two that passed are guards for properties main already
had (`test_5_3_a_tombstone_neither_raises_a_floor_nor_uncounts_a_turn`,
`test_3_1_the_principal_s_own_lower_roles_still_ratchet_by_role`), kept so the fix cannot
break them. Eight existing tests in `tests/test_conversation.py` also failed against the new
signature and rule.

After:

```
uv run pytest               4042 passed, 35 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  198 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** `fold` and `window_records` are pure over records already read;
neither takes a lock or opens a file. The IO paths are unchanged in order: `turn` appends under
the stream lock, then reads every stream back and folds; `digests.run` reads under
`digest.lock` and folds under it. Writers A (a turn on `d-mbp`) and B (a turn on `d-phone`) in
the same conversation: A appends, B appends; each then reads both streams and folds the same
two utterances, in either order, to the same floor — now through `_by_ref`, so if A's read
somehow returned the same record twice it would count once. If A's read-back were to hold two
different events under one ref (a corrupt copy), `fold` raises `ResolutionError` after A's
utterance is already in the log: fail closed, with the drop recorded, which is the same shape
as the existing `RuntimeError` on a read-back that lacks the turn. A digest run that reads a
supersede stamped after `until` (a `forget` that landed between the slot's end and the run)
still drops the target from the window; the next run's window does not re-include it, because
liveness is over the whole log every time.

**Stranger pass:** diff is ~140 lines over four files, touches an error path, so the re-read
was done. Found and fixed: (1) `window_records` iterated `records` twice — a generator would
have been empty on the second pass; it is now materialised with a comment saying why. (2) The
frame for a named non-principal said "They are operator in their own folder", a fact the kernel
has not verified (it attached a role for *this message*); reworded to say the role walle
attached is theirs in their own folder, not here. (3) A first draft of the window filter used
a walrus inside a comprehension; replaced with a named `inside` predicate. Considered and left:
`live_events` runs `_liveness` (cycle detection) even though `fold` ignores liveness — the cost
is linear and the alternative was importing the private `_by_ref`, which the seam test forbids
by design. Consequence noted: a supersede cycle in a principal's log now makes
`open_conversation` raise `ResolutionError`, as `resolve` already did; fail closed on a corrupt
log is the right answer and `turn`'s append-then-read ordering is unchanged.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model; it sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run (R6
replaces it for agent work). Nothing here is author-as-reviewer beyond the stranger pass above.

**Choices made inside the task:**

- **`fold` takes the principal, rather than `turn` alone writing `role=None`.** The owner's
  property is that an old `Utterance` cannot restore a higher tier on rebuild. Only the fold
  can enforce that over records that already exist, so the fold needs to know whose folder it
  folds. Keyword-only with no default so the call site reads `fold(records, principal=P)` and
  cannot pass the conversation id by mistake.
- **`turn` still records `role=None` for a non-principal** rather than keeping the attached
  role as data. The honest record of "the role the kernel attached in this folder" is none, and
  Ada's log should not carry a fact about Sam (§7). The `Utterance` schema is unchanged; the
  docstring now says what the field means.
- **Liveness is not applied to the conversation fold.** The issue suggested `_by_ref` for the
  fold and `live_events` for the window. Using `live_events` for both and ignoring `live` in
  the fold keeps one public seam; the docstring and
  `test_5_3_a_tombstone_neither_raises_a_floor_nor_uncounts_a_turn` pin the reason.
- **`FILES[Tier.ADMIN]` keeps `user.md`.** The issue asked to verify this given "an admin ends
  profiles and reads none". After 3.1 the `ADMIN` and `STAKEHOLDER` tiers are reachable only by
  the principal speaking in a lower role in their own folder — a non-principal admin is
  `UNKNOWN` — so `user.md` at `ADMIN` is the principal's own file about themself. No change,
  and no D-number touched.
- **The anchor edge in 5.3 is fixed by `Due.first`** rather than by changing `Due.since` to be
  inclusive everywhere: later windows start at the previous run's `ts`, which that run's window
  already included (`<= until`), so making every start inclusive would show the previous
  `digest` event — and anything stamped at that instant — twice.
- **Existing tests edited.** The brief allows it when the task requires it: the fold's
  signature changed, and five tests used Sam-as-stakeholder to exercise the by-role ratchet.
  They now use Ada-as-stakeholder and test the same thing; Sam's turns moved to the audit file.
  The golden fixture's bytes are untouched; only the expected floor for `garden` changed, which
  is the property under test.
- **#120's shared merge order (PR #125) is not on `main`** at the time of this commit
  (`mergeable_state: dirty`), so `window_records` still sorts by `parse_ref` as before. When
  #125 lands, the one-line swap to `merge.ref_order` belongs with it or with the next task that
  touches digests.
- **No D-number conflict found**, so `flag` was not used.

**What to read first:** `spoke_at` and `fold` in `conversation.py` (the rule and its
enforcement, ten lines), then `test_3_1_an_old_utterance_with_a_foreign_role_cannot_restore_a_tier_on_rebuild`
— it writes a pre-fix log by hand and folds it both ways, which is the whole of 3.1 in one
test. Then `window_records` and `test_5_3_seam_a_forgotten_note_is_not_in_the_digest_the_run_composes`.

**Noticed, not done:**

- `digests.window_records` sorts by `parse_ref`; PR #125 introduces `merge.ref_order` for
  exactly this. Swap when it lands.
- `conversation.Conversation.turns` counts superseded utterances. That is deliberate here (a
  turn happened) but nothing else reads `turns`; if a caller ever wants "turns still standing",
  it should be a separate count over `live`.
- `test_the_pure_parts_have_no_switch` and `test_the_fold_is_pure` in `test_conversation.py`
  list function names by hand; `spoke_at` is not in either list. It is pure and takes no
  environ; adding it would be a one-line test change outside this task's diff.
- The frame for a non-principal still names the role walle attached to them. Whether a frame
  should mention another principal's role at all (it is a fact about their folder) is a
  wording question for the owner, not a code one.

**Cost:** author (Fable 5.1, this thread) — see Hyperagent's usage for thread
`cmw426q4102eq08ad4kz49job`; reviewer — in its own log.
