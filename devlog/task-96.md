# Task 96: rot detection, a derived label that writes nothing, dormant

Issue #96. Branch `task/p3-rot`. It was claimed at `main` (`b4efc53`) on 2026-11-05, then
flagged `needs-shravan`. The owner answered on 2026-11-06 and told me to reuse the branch after
merging main, so this is not a second claim. I developed on `main` at `e097fd5`. The author's
only write path cannot move a branch to main directly, so the branch got this log first, then
the PR, then GitHub's update-branch merged main in, and the code went on top of that merge. Blocked by:
#95 (closed). Builds on #97 (closed). Phase 3 task (R8). Dormant: nothing reads a log for labels
unless the owner sets `WALLE_ROT=on`.

**The owner's three decisions (comment on #96, 2026-11-06), which this implements:**
1. Derived status. `rot(register, records, now)` is a pure function of the log and an explicit
   `now`, and it writes nothing, so D72 holds. An automation labelled archived stays in the
   register until a person retires it. The WIP cap's refusal suggests archived ones first.
2. An optional `depends_on` on `add`, with no schema bump. Its refs must exist in this
   principal's log. walle never labels, or suggests retiring, something a live automation
   names.
3. Idle means no add, pin, unpin, restore or outcome. Stale at 30 days and archived at 90, both
   configurable. `pinned` is never labelled. `restore` is person-only and resets the idle
   clock.

**Shipped:**
- `src/walle/rot.py` (new, about 280 lines with its docstring). The docstring states seven
  rules.
  - The pure part: `rot(register, records, now, *, stale_after, archive_after) -> Rot`. `Rot`
    has one `Label` per active automation, in register order. Each label has a `status`
    (`active`, `stale` or `archived`), `last_activity`, `idle`, a `reason`, `pinned` and
    `held_by`. `Rot.offer` is what a full register offers: the archived automation idle the
    longest, then the stale one idle the longest.
  - `STALE_AFTER` (30 days) and `ARCHIVE_AFTER` (90 days) are the owner's defaults. Any call
    can override them.
  - The read: `labels(root, principal, now=None, ...)`. It refuses with `RotOffError` before
    reading anything unless `WALLE_ROT=on`.
  - The switch: `ROT_SWITCH`, `SWITCH_ON` and `enabled(environ)`, the same shape as the
    register's and the outcomes' switches.
- `src/walle/events.py`:
  - `AutomationVerb.RESTORE`.
  - `Automation.depends_on: list[Ref] | None`, allowed on `add` only. It must be non-empty and
    name each ref once, and it is left out of the JSON when absent.
  - `Automation` goes to **v2**, with the upcaster `_automation_v1_to_v2` (see *Schema version*
    below).
- `src/walle/automations.py`:
  - Rule 1 now has five verbs. New rule 7 covers dependencies. The old rule 7 (off by default)
    is now rule 8.
  - `Entry.depends_on`, plus `Register.held_by(ref)`, `.held` and `.offerable`.
    `Register.suggestion` now skips anything a live automation names.
  - `refusal(..., log=, ref=)` checks `depends_on` against the refs the log holds, and refuses
    an add that names itself. The writer and the fold both call it (rule 6 is unchanged).
  - `add(..., depends_on=)` checks under the lock, from the same read, before any write,
    including the retire half of a swap. `restore(...)` uses `_change`, so it is person-only
    and off with the register. While `WALLE_ROT` is also on, `_cap_offer` makes the cap
    refusal offer `rot`'s archived or stale automation.
- `tests/test_rot.py` (new, 55 tests).
- `tests/fixtures/v2/automations/`, the golden log for `automation` v2 (D88, D96). It is Ines's
  made-up household: seven adds, one with `depends_on`, a pin, a person's restore, a curator's
  restore (which the fold reports), outcomes at v1, and a tombstoned run, with 16 sentinels.
  `test_golden`'s coverage check requires a `v2/` log once the type is at v2. The belief hash is
  the empty-state hash, because no automation is a belief (as in `v1/automations`).
- `tests/test_events.py`, `test_every_event_carries_a_version`: **the task requires this test
  edit.** It asserted `v == SCHEMA_VERSION == 1` for every type, which is false as soon as any
  type is bumped. It now asserts 2 for `Automation` and 1 for every other type, so the check is
  no weaker.

**Why:**
- **D114:** timestamps move automations between active, stale and archived, with no model in
  the loop. `pinned` bypasses every automatic transition. Nothing a live automation references
  ages out. D8 says never delete, only archive, and keep it recoverable.
- **D72, and the owner's decision 1:** only a person changes the register, so the status is
  derived and never written. The labels are a fold over the log plus a `now` (hard
  constraints 3 and 5). They read no clock, open no file, call no model and keep no state.
- **D6:** the cap refusal is where the labels earn their keep. With rot on, it offers the
  archived automation first instead of the oldest by add.
- **D117 and the owner's dormant condition:** install is not enable. `labels` refuses unless
  `WALLE_ROT` is exactly `on`, and no event can reach that switch. `restore` and `depends_on`
  are register writes, so they are off unless `WALLE_AUTOMATIONS=on`. Turning the register on
  does not turn rot on. No CLI command exists, and nothing in the package calls `labels`.

**Schema version.** A new verb in a closed set is a version bump with an upcaster. That rule is
written on `AutomationVerb` itself (D88, D96), and `restore` is a new verb, so `automation` goes
from v1 to v2. This is the first bump any type has had. `depends_on` would not have needed one
on its own (decision 2), but it ships in the same version. The upcaster is the identity for a
real v1 line. It **refuses** a line that says `"v": 1` but carries `restore` or `depends_on`,
because no v1 build wrote that, and reading it as v1 would hide a forged or mangled line. An
event built in code has no `v` until it is validated, and the chain reads a missing `v` as 1,
so the strict check applies only to an explicit `"v": 1`. In practice, a build from before
this PR that meets a v2 line now halts with `FutureSchemaError` and names the version. Without
the bump it would fail with a confusing validation error on `restore`.

**Verify:** *never deletes, only archives and can be restored; `pinned` is never retired;
nothing a live automation depends on is retired.*
- *Never deletes; archived; restorable:* `test_rot_labels_by_idle_time_and_writes_nothing`
  compares every byte under the root before and after. `test_an_archived_automation_stays_in_the_register`
  shows it is still `active` in the register. `test_a_restore_brings_an_archived_automation_back_and_resets_the_clock`
  shows a restore makes it active the next day and stale 30 days later.
  `test_rot_has_no_way_to_retire_delete_or_write` checks at the AST level that there is no
  writer import, no append, unlink or rename, and no `retire`. Restore is person-only
  (`test_restore_is_person_only`, three machine actors, through the API and by a hand-written
  line), and a retired automation cannot be restored.
- *`pinned` is never labelled:* `test_a_pinned_automation_is_never_labelled` checks days 120,
  365 and 3650. `test_an_unpin_starts_the_clock_from_the_unpin` checks the unpin.
  `test_a_pin_or_unpin_the_register_refused_does_not_move_the_clock` checks refused pins and
  unpins.
- *Dependencies:* `test_what_a_live_automation_names_is_never_labelled` (day 400) and
  `test_once_the_one_that_names_it_is_retired_it_ages_like_any_other`.
  `test_the_wip_suggestion_never_offers_what_a_live_automation_names` checks with rot on and
  with it off. `test_depends_on_naming_nothing_in_the_log_is_refused_before_any_write` and
  `test_a_swap_with_a_bad_dependency_writes_neither_half` compare every byte under the root
  before and after. `test_a_hand_written_add_with_a_dangling_or_self_dependency_is_reported`
  covers the fold.
- *Pure, folded twice gives the same answer:* `test_folding_rot_twice_gives_the_same_answer`.
  It checks two calls, the records duplicated and shuffled with the register refolded from
  them, a rebuilt index and an incremental index. `test_events_after_now_do_not_move_the_clock`,
  `test_rot_has_no_clock_of_its_own` (a naive `now` is refused),
  `test_the_computation_reads_no_clock_and_opens_no_file` (AST) and
  `test_rot_reaches_no_model_and_no_network` (import closure) cover the rest.
- *Thresholds:* `test_the_thresholds_are_30_and_90_days_inclusive` (29.999 days, 30, 89.999
  and 90), `test_the_thresholds_are_configurable`, and
  `test_thresholds_that_cannot_order_the_labels_are_refused`.
- *Archived first:* `test_with_rot_on_the_cap_refusal_offers_archived_first`. With rot off, the
  refusal offers the oldest by add. With it on, it offers the archived one, and after that one
  is restored, the stale one.
- *Dormant, as the owner asked:* `test_rot_is_off_by_default` (nothing is created on disk),
  `test_off_is_checked_before_the_log_is_read` (the read is patched to fail),
  `test_only_the_one_spelling_turns_rot_on` (seven wrong spellings),
  `test_the_register_switch_does_not_turn_rot_on`, `test_restore_is_off_with_the_register`,
  `test_nothing_in_the_log_can_turn_rot_on`, `test_the_cli_gains_no_rot_or_restore_command`,
  and `test_nothing_in_the_package_calls_rot_on_its_own`, which checks that only
  `automations.py` imports `walle.rot`.
- *Schema:* `test_automation_is_at_v2_and_a_v1_line_still_loads`,
  `test_a_v1_line_cannot_carry_what_v1_never_had`, and `test_golden` over `v1/automations`
  (unchanged) and the new `v2/automations`.

Tests first: before the module existed, `tests/test_rot.py` failed at collection
(`ImportError: cannot import name 'rot' from 'walle'`). I then mutated the code 15 ways: the
pinned exemption removed, the held exemption removed, outcomes ignored, refused pins counted,
events after `now` counted, `>=` changed to `>` at the stale threshold, tombstoned outcomes
counted, the switch on by default, the `depends_on` existence check removed, the self-reference
check removed, `restore` taken off the clock, the strict upcaster disabled, stale offered before
archived, held entries made offerable, and the rot switch ignored in `_cap_offer`. All 15 were
killed by `test_rot.py`, `test_automations.py` and `test_survival.py`.

```
uv run pytest               4424 passed, 35 skipped in 143.11s
uv run ruff check           All checks passed!
uv run ruff format --check  220 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 35 skips are the same as on `main`. Every existing test in `test_automations.py` and
`test_survival.py` passes unchanged.

**Two-runner walkthrough:** `rot` and `labels` write nothing and take no lock beyond each
stream's `LOCK_SH`, so two readers see the same labels for the same log and `now`. The writes
here are `add(..., depends_on=)` and `restore`. Both run under `automations.lock` with the log
read only after the lock is taken, as in #95's walkthrough.
- Writer A adds `pay-rent` with `depends_on=[car-service's add]` from the laptop. At the same
  moment, writer B, on the phone, retires `car-service`.
  - If A takes the lock first, it reads the log, finds the ref, appends, and releases. B then
    retires `car-service`, which is a person's call and is not refused (rule 7). `pay-rent`
    still names a ref the log holds, and that ref now holds nothing, because only an active
    automation is labelled.
  - If B takes the lock first, A wakes and reads the log with the retire in it. The add's ref
    still exists, so A's add is allowed. `depends_on` names an event, not an active
    automation.
  - Either way, every step ends with a log that folds the same on both devices.
- Writer A restores `dog-walk` while writer B retires it. Whichever takes the lock second
  reads the first one's event. A restore after the retire is refused (the target is retired).
  A retire after the restore is applied, and the restore did nothing anyone can still see.

**Stranger pass:** done on the whole diff. What it found:
- *A status message that lied.* With a held automation older than the offered one, the refusal
  still said "the oldest unpinned is 'pay-rent'", but `car-service` was older and unpinned.
  It now says "the oldest not pinned or named by a live automation" whenever something is
  held. When nothing is held, the wording is exactly as before, so #95's tests are unchanged.
- *A fresh event read as v1.* The first version of the strict upcaster refused
  `Automation(verb=RESTORE, ...)` built in code, because a missing `v` reads as 1. Fixed: the
  check applies only to an explicit `"v": 1`, and there is a test for each side.
- *`_apply` treated every verb other than retire or pin as an unpin.* A restore would have
  silently unpinned its target. `RESTORE` now returns the register unchanged before that
  branch, and `test_a_pinned_automation_is_never_labelled` would catch a regression.
- *A clock that runs backwards.* An activity stamped before its automation's add (a device
  whose clock is behind) cannot move `last_activity` earlier than the add. An add stamped
  after `now` is idle for zero time, never negative.
- *`rot` given a register from other records* would read clocks it cannot see. It raises
  `ValueError` and does not guess.

Looked for and not found: an ignored error (there is no `except` in `rot.py`), a file handle
left open, a lock held across anything, and a write before any refusal. Every new refusal sits
inside `refusal` or before `writer.append`.

**Review:** independent review is the reviewer agent's job on this PR, per the brief. I did not
run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **`restore` is a verb on `automation`, which forces v2.** The alternatives were a new event
  type (a bigger schema change for one verb) or re-using `pin` and `unpin`, or a
  person-recorded `outcome` (both say something that did not happen). The cost is the first
  schema bump and one required test edit.
- **"Live" means active in the register**, so an automation labelled archived still holds what
  it names. Labels never feed back into the register, which keeps the rule a function of the
  register alone, and it can only label less, never more.
- **A person's `retire` of something a live automation names is not refused.** The owner's
  decision 2 says walle never *labels or suggests* it. Refusing a person would be a new
  restriction on human authority (D72). That is a decision for the owner, so it is listed
  below.
- **`depends_on` may name any event in the log**, not only an automation: a kept workflow or a
  fact is a fair dependency. Only an automation it names is held, because only automations
  are labelled.
- **`labels` is gated, though reading has not been gated before.** #95 and #97 let reading work
  while off. The owner's condition here says nothing may "act on real data" until turned on, and
  a rot pass over a real log is the thing that condition is about. The pure `rot` needs no
  switch, because it sees only the records it is handed.
- **The fold's cap message does not use rot.** The fold has no clock, so a message stored in
  `unapplied` cannot depend on one. Only the writer's refusal, which has the add's `ts`, offers
  archived first.
- **Idle time on a pinned automation keeps running.** It is ignored while pinned, so an unpin
  (itself activity) starts the clock fresh.

**What to read first:** `rot()` in `src/walle/rot.py`, about 80 lines, which is the whole pass.
Then *Schema version* above, because the first bump of any type is the decision most worth a
second look.

**Noticed, not done:**
- On a device that has not yet received the stream a `depends_on` ref lives in, the fold
  reports the add as unapplied until the logs meet. The writer cannot produce this (it saw the
  ref), and the logs converge, but until they do that device counts one fewer active
  automation.
- A hand-written pair of adds that name each other would hold each other forever while both
  are active. The writer cannot create a cycle, because a ref must exist before it is named.
  The fold could refuse a ref to a later event in merge order, at the cost of the clock-skew
  case above.
- Whether a person's retire of a depended-on automation should be refused, or should warn
  (see the choices above), is the owner's call.
- The owner reviews after 30 days of real use whether to amend D72 (decision 1).
  `CONVERSATION.md` has no entry for decisions 1 to 3 yet. It is a CODEOWNERS path, so the
  owner records it.
- `ARCHITECTURE.md` and `PLAN.md` do not mention `restore`, `depends_on` or the v2 bump. They
  are CODEOWNERS paths and are left for the owner.

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
