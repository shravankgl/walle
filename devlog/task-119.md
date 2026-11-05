# Task 119: Audit — approvals can settle for the wrong command or the wrong person

Issue #119, audit findings 1.3, 1.5, 3.2 and 3.3. Branch `task/audit-approvals`, created
empty by the owner as the claim and reused here as his comment on the issue says; from `main`
at `834c6d8`, the commit the audit's line numbers refer to. Author: walle-author-fable
(Fable 5.1). Every reproduction in the issue was re-run against `834c6d8` first and held; each
is now a test that fails there and passes here as a refusal that writes nothing. Dormant: every
new path sits behind the switch its module already had (`WALLE_RUNGS`, `WALLE_INBOX`,
`WALLE_GRANTS`), and `test_the_new_paths_are_off_by_default` proves it.

The three properties the owner asked for, and where each now lives:

1. **An approval binds to the exact command and is never returned for a different one.**
   `inbox.ask` (1.3) and `rungs.settle` (1.5) both compare the text the person read, byte for
   byte, before handing anything back.
2. **Only an `Order` from the folder's owner can settle an approval or write a grant, enforced
   in the signature.** `rungs.resolve`, `rungs.settle` and `grants.grant` take `by` as a
   required keyword-only argument with no default, and `Order` cannot be constructed for anyone
   but the principal speaking as the operator (3.3).
3. **Every machine actor, including the Builder, is listed in one place and can never act as a
   person.** `walle.events.MACHINE_ACTORS`, checked against every `ACTOR_*` the package defines
   (3.2).

**Shipped:**

- `src/walle/speakers.py`, new. `Speaker`, `Tier`, `Order`, `Request` and `Authority`, moved
  from `walle.conversation` unchanged except for one thing: `Order.__post_init__` raises
  `ValueError` unless `speaker.id == principal and speaker.role is Role.OPERATOR`. The move is
  what makes finding 3.3 fixable in the signature: `conversation` imports `grants`, so `grants`
  could not import `Order` from `conversation` without a cycle. `conversation` re-exports all
  five names, so `conversation.Speaker`, `conversation.Order` and the rest are the same objects
  and every existing import still works. `conversation.authority` stays where it was and is
  unchanged: it is the one place that decides, and `Order` now refuses to exist when that
  decision would have gone the other way.
- `src/walle/events.py`: `ACTOR_BUILDER = "builder"` beside the other actors, and
  `MACHINE_ACTORS` — observer, proposer, curator, kernel, MCP, Builder — defined here, once.
- `src/walle/inbox.py`:
  - Rule 1 is now *an item is `(session, tool_call_id)`, and it says one thing*. `ask` returns
    the existing item only when kind, body, choices, mission and node all match (`_text`);
    otherwise `ValueError`, before anything is written, whether the existing item is pending or
    already answered. Same text after an answer still returns the answered item: that is D78's
    restart, a run reading its answer back. `unattended` and the asking actor are not part of
    the text — routing is not permission (rule 5).
  - `ask_event(root, principal, device, event)`, new: stores an `Ask` the caller already
    built, keeping its `ts` and `actor`. `ask` now builds its event and calls it, so there is
    one code path under the lock. This is what `rungs.to_ask` was documented as feeding and
    could not (1.5, point 1).
  - `MACHINE_ACTORS` is imported from `events` and re-exported; an `__all__` was added so the
    re-export is deliberate rather than an unused import.
- `src/walle/automations.py`: imports `MACHINE_ACTORS` from `events` instead of `inbox`. No
  behaviour change beyond the Builder now being refused.
- `src/walle/builder.py`: `ACTOR_BUILDER` is imported from `events` and kept in `__all__`.
- `src/walle/rungs.py`:
  - `Ledger` carries `principal` (`Ledger.start(principal)`): a ledger is one kernel's, and a
    kernel is one principal's (hard constraint 6). Validated with `paths.check_name`.
  - `resolve(ledger, reply, *, by, ts, environ)`: `by` is an `Order` or `Request`. Anything
    else is `TypeError`; a `Request`, or an `Order` for another principal, is
    `Refusal.NOT_AN_ORDER` before the reply is read, with fixed text that never names the
    speaker.
  - `INBOX_REPLIES`, one table: `approved → approve`, `denied → no`. `reply_for` reads it;
    `Pending.answers(via_inbox=True)` and `Pending.card(via_inbox=True)` offer exactly its
    words, so the unattended card no longer says `#id always` (1.5, point 2). The card's
    instruction line also changes for the inbox: it names the inbox's rule (first answer
    settles it), not the fifteen-minute window. `to_ask` uses the inbox card.
  - `settle(ledger, item, *, by, environ)`, new: takes the resolved `inbox.Item` itself.
    Order of checks: off; `by`; unknown id; replayed or expired; mismatch (not an approval, or
    another session, or a body that is not byte-for-byte `found.card(via_inbox=True)`, or
    choices); not a reply (pending, orphaned, or an answer `inbox.misfit` rejects, such as a
    machine actor's); then `Denied` or `Approved(scope=Scope.REQUEST)`. There is no time
    window on this path (1.5, point 3) — see *Choices made*.
  - `Refusal.NOT_AN_ORDER` and `Refusal.MISMATCH`. `Refused.ledger` is `Ledger | None`: it
    was a meaningless empty `Ledger()` for rung 4 refusals, which can no longer be built
    without a principal, so `bounded` leaves it `None`.
- `src/walle/grants.py`: `grant(live, call, scope, *, by, call_id, root, ts, environ)`. The
  `principal` parameter is gone; the folder written is `by.principal`'s, and a `Request` is
  `NotGrantableError` after the type checks and before any file is opened. `check` keeps
  `principal`: reading is gated by `conversation.permit`, which already passes only an
  `Order`'s.
- `src/walle/conversation.py`: imports the moved types; its docstring says where they live
  and why. `Speaker.tier` moved with `Speaker`, which is why `Tier` moved too.
- `tests/test_audit_approvals.py`, new, 32 tests: the four reproductions, the seam tests,
  the dormant proof, and the structural checks described under *Verify*.
- Existing tests edited because the task changes signatures (the brief asks that this be said):
  - `tests/test_rungs.py`: `EMPTY = Ledger.start(P)`; every `resolve` call passes
    `by=ORDER`, the owner's; three `Ledger(...)` constructions gain the principal; the grants
    call passes `by=ORDER`; the unattended-card assertion compares against
    `card(via_inbox=True)`; four lines narrow `Refused.ledger` from `Ledger | None` for
    pyright (two by asserting, two by resolving against the ledger they already held).
  - `tests/test_grants.py`: `_grant` and every direct `grant` call pass `by=ORDER` instead of
    `principal=P`. `test_a_bad_principal_is_refused_before_anything` now builds an `Order` for
    `Lena` — a valid speaker id that is not a valid principal name — and shows `grant` still
    refuses it before writing.
  - `tests/test_conversation.py`: the two `grants.grant` calls pass `by=ada.authority`, the
    turn's own `Order`.
  - `tests/test_inbox.py`: `test_asking_again_with_the_same_call_is_the_same_item` asserted
    the exact behaviour 1.3 removes — a different body returned the first item. It now re-asks
    with the same text and a different route, and a new test beside it asserts the other body
    is refused and the log is unchanged. This is the one place a test's meaning changed rather
    than its calling convention.

**Why:**

- D62: *the request names the exact command … matched exactly*. 1.3 broke this at the inbox:
  the match was on `(session, tool_call_id, kind, mission, node)` and never on what the person
  read. 1.5 broke it between modules: an inbox approval was turned back into a reply string and
  matched by id alone. Both now compare the text.
- D61 / D27: *capability follows the speaker*; admin and stakeholder do not operate. 3.3 is the
  gap between `conversation.permit`, which checked this for *reading* a grant, and
  `rungs.resolve` / `grants.grant`, which checked nothing for *settling* or *writing* one. The
  fix follows the issue's suggestion to require an `Order`, and goes one step further: an
  `Order` for the wrong person cannot be built, so the signature is the whole check.
- D72: persistent-authority acts are human-only, and inbox rule 4 / automations rule 4 say no
  machine actor resolves or changes the register. 3.2 is a list that was not kept in one
  place. It is now, and the test walks every module's AST for an `ACTOR_*` assignment so the
  next one cannot be added anywhere else.
- D73: `settle` hands back `Scope.REQUEST` for an inbox approval and nothing wider — the inbox
  has no word `always`, and `INBOX_REPLIES` is where that fact is written down.
- D78: the inbox is *resolved exactly once, idempotent by `(session, tool_call_id)`*, and *the
  suspended run is persisted so a restart rebuilds the wait*. The restart case is why the same
  text after an answer still returns the item; D78 says nothing about returning an item for
  other words, and D62 says not to.
- No D-number was found in conflict with the issue, so nothing was flagged. The one judgement
  call — the window — is below.

**Verify:** the issue's criterion: *each reproduction becomes a failing test first, then
passes as a refusal; one test per seam that drives the real modules together; nothing is
written to the log on a refusal; the code stays off by default and a test proves it.*

- 1.3: `test_an_approved_item_is_never_handed_back_for_a_different_command` is the issue's
  script through `reviewer.review`, `reviewer.escalate`, `inbox.answer`, then the second
  escalate: `ValueError`, the log's events unchanged, the one item still the benign one.
  Three more cover pending-vs-settled, same-text-after-answer, and choices.
- 1.5: `test_the_unattended_card_offers_only_what_the_inbox_can_carry` (the card),
  `test_the_one_table_drives_reply_for_and_the_inbox_fits` (`set(INBOX_REPLIES) ==
  inbox.FITS[APPROVAL]`), `test_the_ask_rungs_builds_is_the_ask_the_inbox_stores`
  (`to_ask → inbox.ask_event`, stored verbatim), and
  `test_an_inbox_approval_settles_the_pending_after_a_forty_minute_wait`: the inbox answer is
  stamped now, which is more than fifteen minutes after the pending's `T0`, and `settle`
  approves while `resolve` with the same gap still expires. Plus denial, orphaned, mismatch,
  replay and the unfitting-answer case.
- 3.2: `test_the_builder_cannot_approve_or_change_the_register` is the issue's script, both
  halves refused, log unchanged. `test_every_machine_actor_is_listed_once_in_events` is the
  structural check; `test_no_machine_actor_fits_any_answer_a_person_gives` is parametrised over
  the list.
- 3.3: `test_a_guests_always_settles_nothing` is the issue's script; then Sam, Ada-as-admin
  and Sam's own `Order` against Ada's ledger, typed and through the inbox;
  `test_an_order_cannot_be_built_for_anyone_but_the_principal_as_operator`;
  `test_a_request_writes_no_grant` checks no file appears;
  `test_by_is_required_and_keyword_only_everywhere_a_person_settles_or_grants` inspects the
  three signatures.
- Dormant: `test_the_new_paths_are_off_by_default` — `ask_event` raises `InboxOffError` with
  the switch unset and with an empty environ, and no log folder is created; `settle` is
  `Refusal.OFF` with the ledger unchanged; `grant` raises `GrantsOffError`.

Before the fix (on `834c6d8`), the four scripts from the issue were run as written and all
held (`1.3 reproduced: True` … `3.3 reproduced: True`), and `tests/test_audit_approvals.py`
failed there — at collection, since `walle.speakers` and the `by=` / `Ledger.start(P)`
signatures it is written against do not exist on that commit. The first-assertion failures of
the reproduction tests themselves were confirmed while building: 1.3 returned the approved
item, 1.5's card carried `always` and `resolve` expired, 3.2's answer won, 3.3 approved.

Checks, on the final tree:

```
$ uv run pytest
3992 passed, 35 skipped in 108.77s (0:01:48)
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
195 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** `inbox.ask_event` takes the same `inbox.lock` `ask` took, reads
after taking it, and appends under the stream's own lock; the fold-then-decide-then-append
shape is unchanged, and `ask` is now a thin wrapper that builds the event first. Runner A
asks `(s-1, c-1)` with text X; runner B asks `(s-1, c-1)` with text Y at the same moment.
A takes the lock, folds, finds nothing, appends X. B wakes, folds, finds A's item, compares
text: Y ≠ X, raises `ValueError`, writes nothing. Had B compared before waiting for the lock it
would have appended Y and the fold would report it as a repeat (rule 1's fold half, unchanged)
— the lock prevents the contradictory log rather than folding around it, as before.
`rungs.settle` and `rungs.resolve` touch no file and no lock: a `Ledger` is immutable and in
memory, and two kernels never share one. `grants.grant` is unchanged in its locking; the only
difference is which folder the lock lives in — `by.principal`'s, which the caller can no longer
spell differently from the speaker.

**Stranger pass:** done on the whole source diff (227 lines in `rungs.py` alone, and every
file touches an error path).

- Found: `settle`'s docstring listed mismatch before replay; the code checks replay first,
  because a `Settled` has no call to compare against. Docstring corrected to the code's order.
- Found: `settle` trusted `item.answer` to be a person's because `fold_inbox` never builds an
  item otherwise. A hand-built `Item` with the kernel's `approved` would have settled. It now
  runs `inbox.misfit` on the pair and refuses as not-a-reply;
  `test_an_item_whose_answer_does_not_fit_it_settles_nothing` covers it.
- Found: `w is not ALWAYS` compared string identity. Works for the module's own constants;
  changed to `!=` so it does not depend on that.
- Checked and left: every refusal reason is fixed text (`NOT_AN_ORDER` names neither the
  speaker nor the reply; `MISMATCH` does not echo the body); `grant` raises for a `Request`
  before `grants_dir` is computed, so no folder is touched; `ask_event`'s `ValueError` message
  quotes the existing item's kind, mission and node as before and not the new body.
- Checked and left: the `Refused.ledger` optional. Only `bounded` produces `None`; every
  rung 3 path passes the ledger. The alternative — a second refusal type for rung 4 — would
  have changed more tests for no safety gain.

**Review:** not yet run at the time of writing; the reviewer agent sets `agent-review` on the
PR's head commit and its findings land as a PR comment. Nothing here was reviewed by the
author posing as reviewer.

**Choices made inside the task:**

- *No time window on the inbox path.* The issue offered two fixes: enforce fifteen minutes in
  `inbox.misfit`, or document how a forty-minute wait settles. Enforcing it would make the
  inbox useless for exactly what D78 built it for — a mission that waits for a person who is
  away (D67) — and would need the fold to read a clock, which it may not (constraint 3). D62's
  window guards a *typed reply* that arrives in a channel where the command may already be
  stale and the reply may be a stray; an inbox item shows the exact command at the moment of
  answering, is resolved once, and is orphaned when its session dies, so a late yes cannot
  reach a run that is not waiting for it. `settle` therefore applies the inbox's lifetime rule
  and `resolve` keeps the window; the module docstring says so. This reads as a clarification
  of D62's scope, not a conflict, so it was not flagged — but it is the one line in this task
  the owner may want to overrule, and it is a two-line change if he does (`settle` can compare
  `item.answer.ts` to `found.asked_at` the way `resolve` does).
- *Text, not command, in `inbox.ask`.* The `Ask` event has no command field, and adding one is
  a schema change (D88). The body is what the person read and approved, so comparing it is
  D62's rule applied as written. The cost: `reviewer.escalate` puts the reviewer's reason in
  the body, so a restarted run whose real model words the reason differently will get
  `ValueError` on re-ask instead of its answer. That is fail-closed and the caller can use a
  new `tool_call_id`; it is listed below as something to decide later.
- *Ledger-level principal, not `Pending`-level.* A kernel is one principal's; putting the name
  on the ledger means `ask` needs no new argument and every pending in it is covered.
- *`by: Authority`, refusing `Request` inside, rather than `by: Order` only.* A caller can then
  pass `turn.authority` straight through and never decide who may answer. The user-facing
  property — only an `Order` settles — holds either way; this way the module, not the caller,
  says no.
- *`Tier` moved with `Speaker`.* `Speaker.tier` is used by `test_conversation`; keeping it
  meant `Tier` had to be importable from `speakers`. Re-exported, so nothing else moved.
- *An `__all__` in `inbox`.* Added so that re-exporting `MACHINE_ACTORS` is explicit; it lists
  the module's existing public names and adds nothing new except `ask_event`.

**What to read first:** `speakers.Order.__post_init__` (the whole of 3.3 is that one
condition), then `rungs.settle` and the `bound` expression in it, then `inbox._text`.

**Noticed, not done:**

- `inbox.answer` does not check that `actor` is the folder's principal: any non-machine string
  resolves an item (the default is the generic `"user"`). Identity at the inbox is the
  surface's job today; `rungs.settle` compensates by requiring the kernel to pass an `Order`
  when it reads the item back, but the inbox itself still has no notion of *whose* yes it
  recorded. Worth its own decision.
- `reviewer.escalate`'s body carries the model's reason; see *Choices made*. If the restart
  case matters with a real reviewer, the body could be split into a fixed command line and a
  free note, with only the command compared — but that is a schema or a convention decision.
- `tests/test_curator.py::test_a_reply_goes_through_the_same_checks_as_a_proposers_reply` is
  flaky: it asserts `"555" not in json.dumps(event)` and the event's `ts` microseconds can
  contain `555` (seen once in this run: `…33.182555+00:00`). Not touched.
- `CONVERSATION.md` could record the inbox-window reading of D62 as an R-entry if the owner
  agrees with it; a CODEOWNERS path, so not touched here.

**Cost:** not visible to the author from inside the run; the owner's Hyperagent usage page has
the author's figure, and the reviewer's is added when the review runs.
