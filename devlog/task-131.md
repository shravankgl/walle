# Task 131: Audit — leftovers across actors, answers and ordering

Issue #131: audit 2 findings 4.2, 5, 7 and 4.3. Branch `task/audit-leftovers`, from `main` at
`8af004b`. Author: walle-author (Opus 5.5). Every finding was written as a test first and
run against `8af004b` before any fix. All of them failed, with the cause the auditor described,
so none is reported as "did not reproduce". Nothing new is reachable without the switches that
already guard these paths (`WALLE_RUNGS`, `WALLE_INBOX`). No D-number changes.

**Shipped:**

- `src/walle/labels.py`: `RESERVED_ACTORS = MACHINE_ACTORS`, the one list in `walle.events`
  (issue #119), rather than a copy. The copy had missed `ACTOR_BUILDER`. `mcp_server` builds
  `_RESERVED_PRINCIPALS` from it, so `--principal builder` is now refused at start-up too.
  The five per-actor imports are gone (finding 4.2).
- `src/walle/rungs.py`, `settle`:
  - The `bound` check now also requires the item's ask to be timed at the pending's
    `asked_at`, compared as instants. An item with the same session, id and card, asked at
    another moment, is not the item `to_ask` made, so it is `MISMATCH`.
  - An answer timed before `asked_at` is `EARLY`. The check runs after the binding and before
    the outcome is read, which is the same order `resolve` uses for a typed reply (finding 5).
  - `reply_for` is now `_reply_for`, removed from `__all__`. Nothing in `src/` called it. It
    turned an outcome into a reply without looking at any inbox item, so it was a public path
    that skipped `settle`'s binding (finding 7, second half). The docstrings that named it were
    updated.
- `src/walle/staging.py`, `_still_staged`: "already kept as X" now names the first keep in
  `fold_order`, the same keep `walle.stats` counts. Before, it named the first in record
  order (finding 7).
- `src/walle/why.py`, `Ledger._proposal`: "kept as X, Y" lists keeps in `fold_order`, so the
  first one named is the one that decided the proposal. `fold_order` over the ledger's events
  is a `cached_property`, computed once per ledger and only when a proposal is explained
  (finding 7).
- `src/walle/cli.py`, `walle review`, edit: the new `_edit_body` helper. A one-line body is
  still edited at the prompt, where Enter keeps it. A body that spans lines opens in
  `click.edit` on the whole text. Closing the editor unsaved keeps the text. A text emptied in
  the editor writes nothing: the item stays staged and is counted as skipped, with a message
  saying why. If the editor cannot run, `click.edit` raises `ClickException`, which is now
  caught beside `Abort`, so the batch summary is printed before stopping (finding 4.3).
- `tests/test_audit_leftovers.py`, new, 13 tests:
  - three for 4.2: the auditor's reproduction, `RESERVED_ACTORS is MACHINE_ACTORS`, and the
    MCP server refusing `builder` without creating a folder;
  - two for 5: an approval on another device stamped before the ask, folded through the real
    inbox, refused as `EARLY` with nothing written, while the same item timed at the ask
    still settles; an ask re-timed is `MISMATCH`;
  - three for 7: one proposal kept on two devices, where ref order and time order disagree,
    checked through `stats`, `staging` and `why`; plus `reply_for` not being public;
  - five for 4.3: edit in the editor; editor closed unsaved; editor emptied; a one-line body
    still prompts and never opens the editor; the editor failing.
- `tests/test_rungs.py` and `tests/test_audit_approvals.py`, edited because the task requires
  it: three call sites renamed from `reply_for` to `_reply_for`. The assertions are unchanged.
  They still hold the unattended card and `INBOX_REPLIES` to each other.

**Why:**

- **D97, issue #119.** The Builder is a machine, so its words are never the principal's own.
  The one list exists so that a new machine actor is reserved everywhere the moment it is
  defined. `labels` had kept a private copy, which is the drift #119 was meant to end.
- **D62, D78.** A typed reply timed before its request is refused (`resolve`). The inbox path
  must not be weaker: `fold_inbox` folds every ask and then every answer, so across devices an
  answer stamped before its ask still resolves the item. `settle` is where rung 3 takes over
  from the inbox, so the check belongs there. Binding the ask's moment closes the matching
  hole on the item side.
- **Issue #120 / `walle.merge`.** A fold that applies events uses `fold_order`. "Which keep
  decided this proposal" is a fold question, and `stats` already answered it that way. That
  the two text surfaces disagreed with it was the bug.

**Verify:** the issue's criterion: a failing test for each finding first, then a refusal or
the right result, with nothing written on a refusal.

Before (the new tests against `src/` at `8af004b`): `11 failed, 1 passed` in
`tests/test_audit_leftovers.py`. The one that passed is the control: a one-line body still
prompts. It is kept so that the fix cannot move one-line edits into the editor. Each failure
was the auditor's: `'trusted' == 'untrusted'`; the reserved set lacking `builder`; `DID NOT
RAISE`; `Approved` where `EARLY` and `MISMATCH` were expected; `already kept as d-mbp:2`;
`kept as d-mbp:2, d-phone:1`; `reply_for` in `__all__`; and the multi-line edit aborting at the
one-line prompt. The editor-failure test was added after the fix. It would also fail at
`8af004b`, which never opens an editor.

After:

```
uv run pytest            4229 passed, 35 skipped
uv run ruff check        All checks passed!
uv run ruff format --check   208 files already formatted
uv run pyright           0 errors, 0 warnings, 0 informations
```

The tests do not assume root: the sandbox ran as uid 1000.

**Two-runner walkthrough:** the only change on a lock path is the message `_still_staged`
gives. Runners A (review on the laptop, `d-mbp`) and B (review on the phone, `d-phone`) share
one filesystem and both keep `d-mbp:1`. A takes `staging.lock`, re-reads through
`index.update`, finds it staged, appends `d-mbp:2` under the stream lock, and releases. B,
which was blocked on `staging.lock` having read nothing, now reads and finds `d-mbp:2` citing
it. `_still_staged` raises `already kept as d-mbp:2`, the only keep, and B writes nothing. On
two machines that do not share a filesystem, both keeps are written. Once the logs meet,
`stats`, `staging` and `why` now all name the one stamped first. Before this change, `stats`
named that one while the other two named the one with the smaller ref. `settle` and the `why`
ledger are pure over records already read, and take no lock.

**Stranger pass:** the diff is about 90 lines of `src/` and touches an error path (the CLI), so
the re-read was done.
- Found and fixed: `click.edit` raises `ClickException` when the editor fails. That would have
  left the loop without the summary line, the one place that says what was already written.
  It is now caught with `Abort`.
- Checked the status messages: the emptied-body message says "still staged", which is true,
  because nothing was appended.
- Checked that `_instant` in `settle` cannot raise on a stored event. `Event.ts` is an
  `Instant`, so it is validated with an offset at load, and `asked_at` is validated in
  `Pending`.
- Checked that `fold_order` cannot raise on these events, for the same reason.
- Considered and left: `_edit_body` returns `current` when the editor saves nothing, so an
  edit that only changes the summary still writes an edit. That matches the prompt path,
  where Enter keeps the body.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model, and it sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run
(R6 replaces it for agent work). There was no author-as-reviewer pass beyond the stranger pass
above.

**Choices made inside the task:**

- **The early check is in `settle`, not in `inbox.misfit` or `fold_inbox`.** The issue
  suggests `settle`. The inbox fold serves notifications and questions as well, and across
  devices with skewed clocks, rejecting an answer there would make it disappear from every
  inbox view. In `settle`, a lagging clock costs one re-ask, and only for a rung-3 approval.
  That fails closed the way `resolve` already does.
- **The emptied body is skipped, not asked again.** Asking again would reopen the editor on
  nothing, and in a test with a stubbed editor it would loop for ever. Skipping writes nothing
  and leaves the item for the next batch, which is how the prompt path treats an empty answer
  in spirit: nothing empty is ever remembered.
- **`_reply_for` stays, private, rather than being deleted.** Two existing tests use it to hold
  the card and the table together, and that seam is still worth testing. The issue offered
  "private, or bind it to the inbox item". Binding it would have duplicated `settle`.
- **`why` lists keeps in fold order, not D57 ref order.** `walle.merge` says a *listing* uses
  `ref_order`. But "kept as" names the deciding keep first, which is a fold answer, and the
  finding is that it must agree with `stats`. The trade-off: with a clock that ran backwards,
  the order could change if observations were later forgotten. Assertions are never removed
  by a snapshot, though, so the keeps keep their relative order.

**What to read first:** `rungs.settle`, the two added conditions and their order relative to
`MISMATCH` and `NOT_A_REPLY`. That is the one change on an approval path.

**Noticed, not done:**
- A principal folder that already exists named `p-builder` can no longer be served over MCP.
  None should exist, since the name was never meant to be usable, but nothing migrates or
  reports it.
- `inbox.fold_inbox` still lets an answer stamped before its ask resolve a question or a
  notification. Those carry no rung-3 meaning, so it is harmless today, but `walle inbox` will
  list such an item as resolved.
- `walle.merge`'s docstring says listings use `ref_order`. `why` now lists keeps by
  `fold_order`, deliberately (see *Choices*). The docstring could name that exception.

**Cost:** author (Opus 5.5, Hyperagent thread `cmw4bbvxf079g06adtxheoizl`): see Hyperagent's
usage for that thread. Reviewer: in its own log.
