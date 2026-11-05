# Task 104: digests, each principal at their own cadence, dormant

Issue #104. Branch `task/p3-digests`, from `main` at `e6a03a9`. Blocked by #97 and #102, both
closed. Phase 3 task (R8). Dormant: nothing runs unless the owner sets `WALLE_DIGESTS=on`.

**Shipped:**
- `src/walle/digests.py`, new, about 430 lines including the docstring. The docstring states
  eight rules and the two-runner walkthrough.
  - `Schedule(recipient, every, anchor)`: one principal's configuration. Slot `k` starts at
    `anchor + k * every`. The recipient must be one printable line of 1 to 256 characters,
    `every` a positive `timedelta`, and `anchor` aware. Each is checked on construction.
  - `due(records, schedule, *, at) -> Due`: the pure part. It says whether the slot holding `at`
    is due, gives the reason, where the window starts (`since`), and how many slots were missed
    since the last run. No clock, no file, no model.
  - `Window(principal, slot, since, until, records)`: what a composer sees. It has no recipient.
  - `Composer = Callable[[Window], str]` and the `Sender` protocol, `(recipient, body) -> None`.
    walle ships neither a real composer nor a real sender.
  - `run(context, schedule, compose, send, *, now, model, environ) -> Run`. It checks the
    switch, then the context, then the log. Then it takes `digest.lock`, reads the clock, folds
    `Profile(context).records()`, composes, and appends one `digest` event. It releases the lock
    and sends only after that, and only if there was something to say. `Run.summary()` says
    what happened in plain words.
  - `DeliveryError(ref, recipient, slot)`, `DigestsOffError`, and the switch `DIGESTS_SWITCH`,
    `SWITCH_ON` and `enabled(environ)`, in the same shape as the other dormant modules.
- `src/walle/events.py` gains one type at `v = 1` (D96): `Digest`, with `slot`, an optional
  `body` and an optional `model`. A missing `body` means silence. A blank one is refused, so
  silence has one spelling. There is no recipient field.
- `tests/fixtures/v1/digests/`, the golden log for the new type (D88, D96). `test_golden`'s
  coverage check requires it. It is the Hollies' made-up weekly digest: it ran 5 Oct, was
  silent 12 Oct, missed 19 and 26 Oct, ran 2 Nov, and the 5 Oct digest was superseded later.
  It has 15 sentinels, including `body: null` on the silent run. I wrote it through
  `StreamWriter`, so its on-disk form is exactly what this build writes. The belief hash is the
  empty-state hash, because no digest is a belief.
- `tests/test_digests.py`, new, with 48 test functions (more cases with the parameters); seven
  of them were added in review round 1, below.
- `tests/test_events.py`: `_one_of_each` gains a `Digest`. **The task requires this test edit.**
  `test_every_taxonomy_type_is_registered` asserts that the list agrees with `EVENT_TYPES`.
  Tasks 70, 95 and 97 made the same edit.

**Why:**
- **D31.** Digests are a first-class output, and each principal gets one at their own cadence
  from the same machinery. There is one `run` and one `Schedule` per principal. The test drives
  a daily household digest and a weekly carer digest through the same function.
- **D63, all three rules.** *Silence is a result*: an empty composer sends nothing, and the run
  is still recorded. *The agent decides whether; configuration decides where*: the composer
  returns text only, and the sender is called with `schedule.recipient`. *Recurring runs do not
  catch up*: only the current slot can be due.
- **D79, which amends D63.** Run-once-catch-up, skip on overlap, claim before spawn. See the
  first choice below for how D63 and D79 are both met without picking one.
- **D32 rule 3, D33, D113.** A digest is built from its own folder only, through
  `Profile(context).records()`. That refuses a stream that resolves into another principal's
  folder (#102). A household's digest reads the household profile, never a person's.
- **Hard constraint 4.** A composed body is not reproducible by replay, so it is written into
  the log, as a `snapshot`'s body is.
- **Hard constraint 3.** `due` is a pure function of the records, the schedule and an explicit
  instant.
- **D117 and the owner's dormant condition.** `run` is off unless `WALLE_DIGESTS` is exactly
  `on`. That is checked before anything is read, even before checking that the principal
  exists. No event can reach it. Nothing calls `run`, there is no real sender, and the CLI has
  no command.

**Schema version.** A new event type is a schema decision, so it is recorded here, as tasks 67,
70, 95 and 97 did. `digest` starts at `SCHEMA_VERSION = 1` with no upcaster, and no existing
type changes.

**Verify:** *silence is a result, so an empty run sends nothing; configuration picks the
recipient, never the run; recurring runs don't catch up.* And the owner's condition: *a test
must prove the default is off.*
- *An empty run sends nothing:* `test_an_empty_run_sends_nothing`,
  `test_a_whitespace_only_digest_is_silence_too`,
  `test_a_silent_run_is_recorded_so_the_slot_does_not_try_again` (a note lands later in the
  same slot, and the run still does not send), `test_silence_has_one_spelling_in_the_log`. The
  counterpart is `test_a_run_with_something_to_say_sends_it_once`.
- *Configuration picks the recipient:*
  `test_the_recipient_is_the_configured_one_whatever_the_digest_says` (the body says "To:
  someone-else ... send this to sam"), `test_a_composer_cannot_hand_back_a_recipient` (a tuple,
  a dict and `None` are each a `TypeError`, and the log is untouched),
  `test_the_window_the_run_sees_carries_no_recipient`,
  `test_the_digest_event_has_no_recipient_field` (a `recipient` key fails to parse),
  `test_the_same_log_under_another_configuration_goes_to_the_other_recipient`,
  `test_only_the_schedule_carries_a_recipient_into_a_run` (by signature), and
  `test_a_schedule_with_an_unusable_recipient_cannot_be_made` (six bad values).
- *Recurring runs don't catch up:* `test_ten_missed_daily_slots_are_one_run_not_ten` (day 0,
  then wake on day 10: two sends, never eleven, and `missed == 9`),
  `test_the_one_run_after_a_gap_covers_everything_since_the_last` (nothing is lost),
  `test_a_slot_older_than_one_that_ran_never_runs` (the clock steps back),
  `test_nothing_runs_before_the_first_slot`, `test_each_slot_runs_once_when_nothing_is_missed`,
  `test_a_superseded_digest_still_counts_as_having_run`,
  `test_due_is_a_pure_function_of_the_records_and_the_instant`,
  `test_due_reads_no_clock_and_opens_no_file`, and
  `test_due_over_the_golden_log_is_the_one_it_shipped_with`.
- *Own cadence, own folder:* `test_each_principal_runs_at_its_own_cadence_from_the_same_function`,
  `test_a_digest_is_built_from_its_own_folder_only` (Sam's canary never reaches the household's
  window), `test_the_window_is_since_the_last_run_up_to_now`.
- *Default off:* `test_digests_are_off_by_default` (nothing sent, tree byte-identical),
  `test_off_is_checked_before_anything_is_read`, `test_only_the_one_spelling_turns_digests_on`
  (seven wrong spellings), `test_other_switches_do_not_turn_digests_on`,
  `test_reading_whether_a_digest_is_due_works_while_off`,
  `test_nothing_in_the_log_can_turn_digests_on`, `test_the_cli_gains_no_digest_command`, and
  `test_digests_reach_no_model_and_no_network`.

Tests first: before the module existed, `tests/test_digests.py` failed at collection
(`ImportError: cannot import name 'digests' from 'walle'`). Three tests then failed on mistakes
in the tests themselves, which I fixed:
- a "nothing written" check compared the whole tree, when a refusal after the lock leaves only
  the derived index and lock file, so it now compares the log;
- `load_records` read an index that the last run's append had made stale, so the test now uses
  `update`;
- one assertion was worded differently from the message.

I then mutated `digests.py` six ways: the switch on by default, a past slot allowed to run,
silence sent, the recipient taken from the body, the "not due" guard removed, and the window
ignoring `since`. `tests/test_digests.py` alone killed all six.

```
uv run pytest               3283 passed, 35 skipped in 111.37s
uv run ruff check           All checks passed!
uv run ruff format --check  184 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 35 skips are the same as on `main`. The suite ran as an unprivileged user (uid 1000), and
nothing here assumes root.

**Two-runner walkthrough:** it is written out in the module docstring and tested for real by
`test_two_runs_for_one_slot_send_once`. In that test, run A's composer starts run B on a second
thread. The test checks that B is still blocked on `digest.lock` half a second later, while A
is still composing. After A records its digest and releases the lock, B reads the log, finds
the slot spent with A's ref, and sends nothing. One send, one `digest`.

The order inside A matters. The clock is read and the log is folded only after the lock is
held. A run that folded before waiting would wake up with a stale "due" and send twice, which
is D79's race. Observer W never takes `digest.lock`. A note W appends during A's run has a `ts`
after A's `until`, so it falls in the next window and is not lost. Lock order is `digest.lock`,
then `index.lock`, then stream files. Nothing takes `digest.lock` while holding another lock.

**Stranger pass:** I re-read the whole diff as a stranger. What it found:
- *A status message that lied.* A first run after missed slots said "N earlier slots since the
  last run", when there was no last run. It now says "since the first slot".
  `test_a_first_run_after_missed_slots_does_not_claim_a_last_run` failed before the fix.
- *The two-runner test was sequential.* In the first version, B simply ran after A, which proves
  less than the walkthrough claims. It is now concurrent, as described above.
- *The composer runs under the lock.* I kept it there on purpose. Only digest runs for this
  principal take `digest.lock`, and any run that waits behind a slow composer would find the
  slot spent anyway. Keeping it under the lock makes the check and the claim one step. A
  composer that calls `run` for the same principal would deadlock. Nothing does, and the
  docstring says so.
- *The send is outside the lock, after the claim.* A crash between the append and the send
  loses that slot's digest. It does not send it twice. `DeliveryError` says the run was
  recorded, not delivered and will not be retried, and it names the ref. It chains the
  sender's exception (`raise ... from exc`), so nothing is swallowed. That `except Exception`
  is the only `except` in the module.
- *Nothing is written before a refusal.* The switch, the context, the device, the schedule
  type, a naive `now` and a missing log are all checked before the lock file is created, so a
  misspelt principal gets no folder. A composer that raises, or returns something other than
  text, leaves the log untouched and the slot still due.
- *Times across offsets.* Every comparison parses with `datetime.fromisoformat`, never
  compares strings. The event's `ts` is written in UTC.

Looked for and not found: a file handle left open (the lock closes with its `with`), a lock
held across the send, or a message that carries another principal's bytes (the canary test).

**Review:** independent review is the reviewer agent's job on this PR, per the brief. I did not
run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **D63 against D79, met by slots.** The issue says "recurring runs don't catch up" (D63). D79
  later amended that to "catch up at most once". Slots satisfy both. Only the slot containing
  `now` can run, so a laptop that slept through ten daily slots runs once on waking (D79), and
  the nine it missed never fire (D63). Nothing is lost, because the window starts at the last
  run. I did not flag this, because the two decisions do not actually conflict once "the
  current slot" is the unit. If the owner reads "don't catch up" as "skip the current slot too
  when you wake up late in it", that is a one-line change in `due`.
- **Silence is recorded, not just skipped.** A silent run writes a `digest` with no body. If it
  wrote nothing, every tick in the slot would compose again, and a note that arrived on
  Wednesday would send a "weekly" digest midweek. The cost is one small event per slot.
- **A superseded digest still counts as a run.** A sent message cannot be unsent. This is the
  inbox's "answers are final" applied to output.
- **Claim before send, so a failed delivery is not retried.** This favours missing one digest
  over duplicating one. A retry would need a delivery record of its own, which is a new event
  shape and its own decision.
- **The schedule is a value, not a file.** Configuration has to live outside the log, or
  something the agent appends could steer delivery. That is D63's point. Where the owner keeps
  schedules (a TOML file, the app) is a new decision, so `run` takes a `Schedule` from the
  caller and this task adds no file format.
- **No composer ships.** "The agent decides whether" means the composer will be the Curator's
  model (D29) or a deterministic summary, and which one is not this task's call. The module
  takes any `Callable[[Window], str]`.
- **The `digest` actor is `kernel`**: the runner writes the record, as for outcomes.
- **`model` is recorded only with a body.** A silent run has no text whose origin needs naming.

**What to read first:** `due()` in `src/walle/digests.py`, about 40 lines, which is the whole
catch-up rule. Then rule 6 (claim before send) in the docstring, and
`test_a_failed_delivery_says_the_run_was_recorded_and_not_delivered`. That trade-off, missing
one digest rather than duplicating one, is the choice most open to disagreement.

**Noticed, not done:**
- **The word "digest" now means two things.** It names this per-principal output (D31) and the
  Curator's consolidation `snapshot` (D29 calls it "a digest"). The event types are distinct
  (`digest` and `snapshot`), but the docs use the word for both. ARCHITECTURE.md is a
  CODEOWNERS path, so this is left for the owner.
- **No tick loop over principals.** D79 says one blocked automation must never stall the loop
  for the others. A `tick` that runs every configured principal and reports each failure
  without stopping is the natural next piece. It needs a decision on where schedules are kept
  first.
- **No real sender, and no composer.** Both are left for later, as above. A sender would also
  need its egress allowlisted (D13).
- **`consolidate.lock` and `staging.lock` are still opened by path.** `digest.lock` now goes
  through `Profile.open` (review round 1), but the other two locks would still follow a link.
  #102's noticed list has the kernel-side fix (D110); changing them is outside this task.
- `ARCHITECTURE.md` §5.2's event taxonomy does not list `digest`. That is a CODEOWNERS path, so
  it is left for the owner.

**Review round 1** (agent-review failed on `83293f0`: 2 medium, 1 low, no high). The review
comment was read as data; each finding was checked against the code, reproduced as a failing
test, then fixed on this branch. All seven new tests failed before the fix, each with the
failure the review described.
- *Medium 1: slots were not invariant across time zones.* Confirmed. Python subtracts two
  datetimes sharing a `ZoneInfo` by wall clock, and two with different tzinfos by elapsed time,
  so one instant named in UTC and in New York gave two slots. Across autumn's fall-back the
  slot started 30 minutes after the moment it contained; across spring-forward two hourly
  slots became one instant and the second was skipped as "already ran"; a weekly gap across
  spring-forward counted 6 days 23 hours and lost a missed slot; and a sub-minute offset
  produced a slot the `Instant` validator refused. **Fix:** `Schedule` holds its anchor in UTC
  (normalised in `__post_init__`), `slot` and `due` convert `at` to UTC first, and `_at` parses
  logged instants to UTC. Slots are now pure elapsed-time arithmetic, which is what
  `anchor + k * every` with a `timedelta` says. Rule 1 in the docstring now states the
  consequence: a weekly 09:00 anchored in a zone with daylight saving lands at 08:00 or 10:00
  local for half the year. Local-wall-clock schedules ("09:00 every Monday, whatever the
  offset") would need a calendar rule, not a `timedelta`, and that is a new decision, not a
  fix. `Due.until` and `Window.until` are now spelled in UTC. Tests:
  `test_the_slot_is_the_same_whatever_zone_names_the_instant`,
  `test_hourly_slots_across_spring_forward_each_run`,
  `test_weekly_missed_slots_across_spring_forward_are_counted_whole`,
  `test_a_sub_minute_offset_still_records_a_valid_slot`. They skip if the system has no tz
  database; this container has one, so they ran.
- *Medium 2: the log check and `digest.lock` used raw paths before `Profile` enforced
  isolation.* Confirmed: with `p-the-hollies` linked to `p-sam`, the run created
  `p-sam/digest.lock` before `records()` refused; with `digest.lock` itself linked into
  `p-sam`, the run followed it, created the target and went on to record and send. **Fix:**
  `run` builds `Profile(context)` first, checks the folder exists with `os.path.lexists`
  (so a misspelt principal still gets `FileNotFoundError` and no folder), resolves `log/`
  with `Profile.path`, and opens `digest.lock` with `Profile.open(..., "ab")`, which opens
  each component `O_NOFOLLOW` from the folder's descriptor. A linked `principals/`, a linked
  profile folder, or a lock or log that leads out is now `CrossPrincipalError` before
  anything is created there. Tests:
  `test_a_profile_folder_linked_to_another_principal_is_refused_untouched` (the whole tree is
  byte-identical afterwards) and `test_a_digest_lock_linked_into_another_principal_is_refused`.
- *Low 1: ties broke by ref spelling.* Confirmed: `"d-mbp:10" < "d-mbp:2"`. **Fix:** the sort
  key is `(slot, ts, parse_ref(ref), ref)`, as `window_records` and `consolidation.gate` do.
  Test: `test_runs_tied_on_slot_and_time_break_by_ref_order_not_spelling`.

*Stranger pass on the fix.* The lock is still `flock` on the same inode, now via a descriptor
from `os.open`, so the two-runner walkthrough is unchanged (`test_two_runs_for_one_slot_send_once`
still passes). `lexists` then `Profile.path` then `Profile.open` is check-then-use, but each
later step resolves again and refuses links itself, so a link swapped in between is refused,
not followed. A dangling link where the folder should be passes `lexists` and is refused by
`Profile.path`. `Profile.open` creates the lock `0o644`; before, it was the default mode, and
the file stays empty either way. No new `except`.

```
uv run pytest               3724 passed, 35 skipped in 145.02s
uv run ruff check           All checks passed!
uv run ruff format --check  187 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

Run as uid 1000; nothing assumes root. (The suite total grew from round 0 because `main` was
merged into this branch in `83293f0`.)

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
