# Task 70: the inbox for unattended asks — `ask` and `answer` events, a pure fold, dormant by default

Issue #70. Branch `task/p2-inbox`, from `main` at `6cf7b17`. Blocked by #67 (missions), which is
merged. Nothing here dispatches anything or runs by itself. This is the schema, the fold, the
lock-guarded writes and the switch, and the switch is off.

**Shipped:**
- `src/walle/events.py` gains two types, each at `v = 1` (D96):
  - `Ask`: one inbox item. `kind` (`approval`, `question`, `notification`), `session` and
    `tool_call_id` (the item's identity, D78's idempotency key), `body` (what the person reads),
    `choices` (a question only), `unattended` (how the person was reached), and optionally
    `mission` and `node` (which mission step is waiting, so a restart can rebuild the wait). The
    validator refuses choices on anything but a question, an empty or repeated choice, and a
    node without its mission. `CallId` holds session and tool-call ids to "no whitespace, no
    `/`", with mixed case allowed, because harness ids like `toolu_01AbC` are mixed case.
  - `Answer`: the resolution of one item. `ask` (a ref, like `supersede.target`), `outcome`
    (`approved`, `denied`, `answered`, `acknowledged`, `orphaned`), `value` (only and always for
    `answered`), and `surface` (where it was answered, for the record).
  - `AskKind` and `AnswerOutcome` are closed sets, like `Level` and `StepStatus`.
- `src/walle/inbox.py`, the module. Its docstring states seven rules and the two-responder
  walkthrough.
  - The pure part: `fold_inbox(records) -> InboxState` (items in ref order, plus `unapplied`
    with reasons), `misfit(ask, answer)` (the one check that decides whether an answer may
    resolve an item), `FITS`, `MACHINE_ACTORS` and `route(unattended=...)`.
  - The writes: `ask` (idempotent by call), `answer` (first responder wins, and the loser is
    told the winner's result), `orphan(alive=...)` (the kernel resolves items whose session has
    died). All three run under `inbox.lock` and refuse to run while the switch is off.
  - The read: `pending`.
- `src/walle/paths.py` gains `inbox_lock_path`: `principals/p-<who>/inbox.lock`, per principal
  like `staging.lock`.
- `tests/fixtures/v1/inbox/`, the golden log for both new types at `v1` (D88, D96). It is
  Lena's made-up morning: a two-node mission, an unattended approval that holds its
  `send-note` step, a question with choices, a notification orphaned by the kernel, a late
  deny, and one item still pending. Its `__expected__.json` has 25 sentinels. `test_golden`
  picks it up by directory, and its coverage check is what required it.
- `tests/test_inbox.py`, 56 tests: the issue's verify criteria, the dormant rule, what fits,
  the event shapes, purity, and the golden log's fold pinned item by item.
- `tests/test_events.py`: `_one_of_each` gains an `Ask` and an `Answer`. The task requires this
  edit. `test_every_taxonomy_type_is_registered` asserts that list and `EVENT_TYPES` agree,
  which is the purpose of that test, and the round-trip test now covers both new types too.

**Why:** D78 says async human attention is an inbox of approval, question and notification
items. Each is resolved exactly once, first responder wins, and items are idempotent by
`(session, tool_call_id)`, so answering from any surface is safe. The suspended run is persisted
so that a restart rebuilds the wait, and orphaned items resolve when their session dies.
"Unattended is a routing toggle, not a permission mode … Unattended runs never self-approve."
Here, "persisted" means *in the log*: the inbox is a fold, like the staging queue (D49), so it
passes the rebuild test and keeps no state outside the log (hard constraints 2 and 5). D63's
"the agent decides whether; configuration decides where" is why `Ask` has no recipient field.
D117's "install is not enable" is the reason for the switch, `WALLE_INBOX=on`, which uses the
same single spelling as `WALLE_MISSIONS`.

**Schema version.** A new event type is a schema decision, so it is written down here, as task
67 did. D96 versions each type separately, and the golden check requires a `v1/` log holding
each type at `v == 1`. Both types start at `SCHEMA_VERSION = 1` with no upcaster, and no
existing type changes. The types are D78's, named again in the owner-approved issue, so no new
decision was needed and the issue was not flagged.

**Verify:**
- *Two simultaneous answers give one result, with a two-runner test:*
  - `test_two_runners_answering_at_once_give_one_result`: two threads, released by one
    barrier, approve and deny at once from two devices' streams. Exactly one wins, both are
    told the same outcome, and the log holds one answer.
  - `test_a_second_responder_waits_and_is_told_the_first_answer`: a second process holds
    `inbox.lock` and approves. The denier waits at least 0.5 s, reads only then, writes
    nothing, and gets the approval back.
  - `test_two_answers_that_never_shared_a_lock_still_fold_to_one`: two devices, no shared
    lock. The first answer in ref order wins on every machine, the other is reported, and
    reversed input gives the same state.
- *An orphaned item resolves when its session dies:* `test_an_orphaned_item_resolves_when_its_session_dies`
  (only the dead session's item, by the kernel, as `orphaned`),
  `test_an_orphan_sweep_touches_nothing_already_resolved`,
  `test_an_orphaned_approval_can_no_longer_be_approved` and `test_only_the_kernel_orphans_an_item`.
- *Unattended mode never raises autonomy:*
  - `test_unattended_moves_where_the_person_is_reached_and_nothing_else`: for every kind,
    outcome and actor, `misfit` gives the same answer attended and unattended. Only `route`
    differs.
  - `test_no_machine_actor_can_approve_attended_or_not`: 10 cases, refused with nothing
    written.
  - `test_a_self_approval_written_into_the_log_is_reported_not_applied`: a line that bypassed
    `answer` still does not resolve the item.
- *Survives a restart:* `test_a_pending_item_survives_the_process_that_asked`. A child process
  asks and exits. The index is destroyed. A fresh call sees the item pending, with its mission
  and node. Re-asking the same call returns it unchanged, and answering resolves it.
- *Off by default:*
  - `test_the_inbox_is_off_by_default`: switch unset, so `ask`, `answer` and `orphan` all
    raise and nothing is created on disk.
  - `test_only_the_one_spelling_turns_the_inbox_on`
  - `test_nothing_in_the_log_can_turn_the_inbox_on`
  - `test_the_cli_gains_no_inbox_command`

Written first: with no `walle.inbox`, `tests/test_inbox.py` failed at collection
(`ImportError: cannot import name 'inbox' from 'walle'`). `test_golden`'s coverage check failed
on `ask v1` and `answer v1` until the fixture landed.

```
uv run pytest               2047 passed, 10 skipped in 80.61s
uv run ruff check           All checks passed!
uv run ruff format --check  132 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The tests do not assume root. They ran as uid 1000. Every write goes to `tmp_path`, and the
golden test copies the fixture and makes the copy writable first. The inbox and golden tests
pass with `tests/fixtures/v1/inbox` made read-only.

**Two-runner walkthrough:** in full in `inbox.py`'s docstring. In short, A and B answer
`d-mbp:2` at once, A approving on `d-mbp` and B denying on `d-phone`.
1. A takes `inbox.lock`, and B blocks having read nothing.
2. A reads every stream (each under `LOCK_SH`, released), folds, and finds the item pending.
3. A appends `d-mbp:3` under that stream's `LOCK_EX` until `fsync`, then releases the lock.
4. B wakes, and only now reads. It sees `d-mbp:3`, writes nothing, and returns
   `Resolution(won=False)` carrying A's approval.

A runner R asking meanwhile also waits on `inbox.lock`, because a new item must be checked
against existing ones under the same lock, or two re-issued calls would both write.

Lock order is `inbox.lock`, then shared stream locks (released), then one exclusive stream
lock. Nothing takes `inbox.lock` while holding another lock.

Across devices that do not share a filesystem, each device has its own lock, so both answers
can land. The fold's rule 2 gives one result everywhere once the logs meet: the first in ref
order, which is stream then seq, not wall-clock order. The other answer is reported as late.

**Stranger pass:** done on the whole diff (about 490 lines of new module, 135 in `events.py`,
three error paths). Found and fixed:
- `ask` returned the existing item for a repeated call even when it was re-asked as a different
  kind or for a different mission step. A caller asking a question would have been handed an
  approval and would wait on the wrong shape of answer, which is a status that lies. Now a
  `ValueError` names the existing item, and
  `test_the_same_call_asked_as_another_kind_is_refused` covers it. A changed `body` is still
  the same item, because a restarted run may render its prompt differently.
- Three messages read "a approval". They were reworded to "this approval" and "an inbox item".

Looked for and not found:
- An ignored error. Every refusal is raised before a write, and every reason the fold cannot
  apply something reaches `unapplied`.
- A lock leak. The lock is released by `close` in a `with` block, on any exception.
- A read before the lock. `answer`, `ask` and `orphan` all read inside `_exclusive`.
- A folder created for a misspelt principal. `answer` and `orphan` check the log exists before
  the lock file is made. `ask` creates the folder on purpose, as `remember` does.
- A path where `unattended` reaches the permission check. `misfit` never reads it, and the
  test runs all 105 combinations (3 kinds × 5 outcomes × 7 actors).

**Review:** independent review is the reviewer agent's job on this PR, per the brief. No
`scripts/review.sh` run, because the brief replaces it for agent work.

**Choices made inside the task:**
- **A lock and a fold, not the fold alone.** The fold alone already gives one result. Without
  the lock, though, two responders on one machine would both append and both believe they won.
  The lock makes the loser find out before writing. This is the same reasoning as
  `staging.lock`, and the same shape.
- **The loser gets the winner's result, not an exception.** "Two simultaneous answers give one
  result": both callers see the same outcome, and `won` says whose it was. A surface can then
  show "already approved by the app" instead of an error.
- **The fit check comes before the already-resolved check.** A machine actor's approval is
  refused even on a resolved item. A refused answer is refused regardless of timing, so the
  caller learns about the actual mistake.
- **Answers are final, and a superseded answer still stands (rule 6).** D8 says a supersede
  undoes. Reopening a resolved item, however, cannot recall what the released run already did,
  and "resolved exactly once" is D78's own phrase. A superseded *ask* is honoured: it withdraws
  the item. A reviewer may disagree here. If so, it is one `continue` in `fold_inbox`, plus
  this paragraph.
- **"A person" is any actor not in `MACHINE_ACTORS`.** Actors are an open string by design
  (events.py), so the inbox lists the five machine actors walle has rather than listing
  people. See *Noticed, not done*.
- **Session death is told, not detected.** `orphan(alive=...)` takes the caller's set of live
  sessions. There is no runner yet to own that knowledge, and polling PIDs here would put a
  guess about liveness inside a module whose answers come from the log.
- **No index.** The inbox reads the streams directly. Its answer must not depend on a derived
  file being current, and it saves taking `index.lock` inside `inbox.lock`.
- **The fixture is its own directory, `v1/inbox`.** Golden logs are frozen when they ship, so
  neither `maya` nor `chores` was changed.

**What to read first:** `inbox.py`'s module docstring (the seven rules and the walkthrough),
then `misfit` (about 15 lines, the whole permission story) and `answer` (the lock and the
order of checks). Then `test_unattended_moves_where_the_person_is_reached_and_nothing_else`
and `test_a_second_responder_waits_and_is_told_the_first_answer`.

**Noticed, not done:**
- **The actor rule is only as good as the actor string.** `MACHINE_ACTORS` covers today's five
  constants. A future machine actor, such as the Builder (#73) or the rung 3.5 reviewer (#71),
  that is not added there would count as a person. Those tasks should add their actor, or
  `events.py` should mark machine actors in one place both modules read.
- **`walle why` does not follow `Answer.ask` or `Ask.mission`**, for the same reason task 67
  noted for `Step.mission`: they are ref fields, not `caused_by`. The index's dangling-ref check
  does not see them either. The fold reports a dangling `ask` in `unapplied`.
- **No surface yet.** There is no `walle inbox` command, no notification and no Slack hookup. The
  dormant rule asks for this, and D63 says configuration decides where an ask goes. That
  configuration does not exist yet.
- **Nothing calls `orphan` at startup.** The runner that will own "which sessions are alive"
  does not exist. When it does, it should call `orphan(alive=<its own sessions>)` before
  dispatching, then rebuild waits from `pending`.
- **Ask text goes into the index's FTS** through `body`, like any other event's `body`. Fine
  for synthetic data. Once asks carry real text, it is searchable with `recall` like everything
  else, which is probably what is wanted.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
