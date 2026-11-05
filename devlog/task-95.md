# Task 95: the automation register and the WIP cap, folded from the log, dormant

Issue #95. Branch `task/p3-registry`, from `main` at `69125a4`. Blocked by: none. Phase 3 task
(R8). Dormant: nothing writes unless the owner sets `WALLE_AUTOMATIONS=on`.

**Shipped:**
- `src/walle/events.py` gains one type at `v = 1` (D96), and a closed verb set:
  - `AutomationVerb`: `add`, `retire`, `pin`, `unpin`. A closed set like `StepVerb`.
  - `Automation`: one change to the register. An `add` carries `name` (a path-safe token, the
    same rule as a node id) and `title`. `retire`, `pin` and `unpin` carry `target`, the ref of
    the add they act on, like `supersede.target`. `note` is free text for any verb. The
    validator holds each verb to exactly its own fields. There is no `pinned` or `active`
    field on purpose: both are what the fold says, never something an event declares.
- `src/walle/automations.py`, the module, about 530 lines with its docstring. The docstring
  states seven rules and the two-writer walkthrough.
  - The pure part: `fold_automations(records) -> Register` (entries in ref order of their add,
    plus `unapplied` with reasons), `refusal(register, event)` (the one check, shared by the
    writes and the fold), `full_message`, and `Register.suggestion` (the oldest unpinned active
    automation, by its add's `ts`, then ref).
  - `WIP_CAP = 7`, a constant (see the choices below).
  - The writes: `add` (optionally `retire=<name>` to swap one out in the same hold), `retire`,
    `pin`, `unpin`. All four run under `automations.lock`, read the log only after taking it,
    and refuse while the switch is off or the actor is a machine.
  - The read: `active`, which works while the switch is off.
  - The switch: `AUTOMATIONS_SWITCH` (`WALLE_AUTOMATIONS`), `SWITCH_ON`, `enabled(environ)`,
    the same shape as the inbox's.
- `src/walle/paths.py` gains `automations_lock_path`:
  `principals/p-<who>/automations.lock`, per principal, like `inbox.lock`.
- `tests/fixtures/v1/automations/`, the golden log for the new type at `v1` (D88, D96).
  `test_golden`'s coverage check required it. It is Ines's made-up household: three adds, a
  pin, a retire, a kernel-written add, a tombstone aimed at the pinned automation, a retire
  of the pinned one, a duplicate name, an unpin, a re-add of the retired name, and a pin.
  Its `__expected__.json` has 23 sentinels. The belief hash is the empty-state hash, because
  no automation event is a belief.
- `tests/test_automations.py`, new, 52 tests (many parametrized over the five machine
  actors).
- `tests/test_events.py`: `_one_of_each` gains an `Automation`. **The task requires this
  test edit.** `test_every_taxonomy_type_is_registered` asserts that list and `EVENT_TYPES`
  agree, which is what that test is for, and the round-trip test now covers the new type.
  Task 70 made the same edit for `ask` and `answer`.

**Why:**
- **D6:** restraint is enforced, not encouraged. There is a hard cap on active automations,
  starting at 7, and adding one past it means retiring one. That is also hard constraint 9.
  Here the cap is enforced twice. `add` refuses before writing, and the fold refuses an add
  past the cap that reached the log another way, so the cap holds even against a line the
  API never saw.
- **D114:** `pinned` is a human floor no automatic transition may cross. Retiring is never
  deleting (D8). So a pinned automation is never offered for retirement. A retire or a swap
  that names it is refused until a person unpins it, and the unpin is its own event in the
  log.
- **D72:** creating, updating or deleting an automation is a persistent-authority act, and so
  human-only. "Narrowing is floored along with broadening", so retire is human-only too. No
  machine actor can add, retire, pin or unpin. The machine actors are the inbox's
  `MACHINE_ACTORS`, imported so there is one list.
- **Hard constraints 2, 3 and 5:** the register is a fold over the log, with no file beside
  it. No model is involved, and nothing reads a clock: the suggestion orders by the add's
  `ts`, which is in the log.
- **D117 and the owner's dormant condition:** install is not enable. The writes are off unless
  `WALLE_AUTOMATIONS` is exactly `on` in the environment, which no event can reach. There is
  no CLI command, and nothing calls the writes.

**Schema version.** A new event type is a schema decision, so it is recorded here, as tasks
67 and 70 did. D96 versions each type separately. `automation` starts at `SCHEMA_VERSION = 1`
with no upcaster, and no existing type changes. The issue says the register is "folded from
the log", so it needs an event. §5.2's taxonomy has no type for it: `capability` is a version
of a skill, not its place in the active set. So I named one rather than overload another, and
did not flag the issue, because no D-number is contradicted. If the owner wants it named
differently, now is the cheap time, before a real log holds one.

**Verify:** *an eighth is refused and says which to retire; the rebuild test stays green;
`pinned` is honoured.*
- *An eighth is refused and says which to retire:*
  `test_an_eighth_is_refused_and_says_which_to_retire`. Seven are active, and the eighth
  raises `CapReachedError`. The message says "7 of 7", names the new automation, and names
  `'water-plants' (d-mbp:1)`, the oldest. `suggestion` is that entry, and the log is
  unchanged. Around it:
  `test_retiring_the_suggested_one_makes_room_for_the_eighth`,
  `test_an_add_that_retires_one_swaps_in_a_single_hold` (a retire then an add, with the
  add's `caused_by` naming the retire),
  `test_an_eighth_written_into_the_log_by_hand_is_reported_not_applied` (the fold also says
  which to retire), and `test_the_suggestion_is_the_oldest_unpinned_by_when_it_was_added`
  (by `ts`, not by ref: a second device's earlier add sorts later by ref).
- *The rebuild test stays green:* the whole suite passes, including `test_golden` over the
  new fixture (rebuild hash, incremental build, fold, sentinels, version coverage).
  `test_the_register_over_the_golden_log_is_the_one_it_shipped_with` checks that the register
  folded from a rebuilt index, from an incremental index and straight from the JSONL is the
  same, and pins it entry by entry.
- *`pinned` is honoured:* `test_a_pinned_automation_is_never_offered_for_retirement`,
  `test_when_every_active_automation_is_pinned_the_refusal_says_to_unpin_first`,
  `test_a_pinned_automation_cannot_be_retired_until_a_person_unpins_it`,
  `test_a_swap_naming_a_pinned_automation_is_refused_and_writes_nothing`,
  `test_a_retire_of_a_pinned_automation_written_into_the_log_is_reported`, and
  `test_no_tombstone_removes_an_automation`.
- *Dormant, as the owner asked:* `test_the_register_is_off_by_default` (all four writes
  raise, and nothing is created on disk), `test_only_the_one_spelling_turns_the_register_on`
  (seven wrong spellings), `test_reading_the_register_is_not_acting_and_works_while_off`,
  `test_nothing_in_the_log_can_turn_the_register_on`, and
  `test_the_cli_gains_no_automation_command`.
- *Human-only:* `test_no_machine_actor_can_add_retire_pin_or_unpin`,
  `test_a_machine_actor_is_refused_before_any_lock_or_folder_exists`, and
  `test_a_machine_written_automation_event_is_reported_not_applied`, each over all five
  machine actors.

Tests first: before the module existed, `tests/test_automations.py` failed at collection
(`ImportError: cannot import name 'automations' from 'walle'`). Then I mutated
`automations.py` eleven ways: the switch on by default, the fold ignoring the cap, the
suggestion offering pinned automations, a pinned retire allowed, a machine actor allowed in
the fold, a machine actor allowed in the API, a supersede honoured, the suggestion by ref
instead of `ts`, a duplicate name allowed, unpin doing nothing, and a swap that writes the
add twice instead of the retire. Ten were killed at once. "A machine actor allowed in the
API" survived, because `refusal` inside the lock catches the same thing. That early check
exists to refuse before any lock file or folder is created, so I added
`test_a_machine_actor_is_refused_before_any_lock_or_folder_exists`, which kills it.

```
uv run pytest               2952 passed, 30 skipped in 104.22s
uv run ruff check           All checks passed!
uv run ruff format --check  166 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 30 skips are the same as on `main`. The suite ran as an unprivileged user (uid 1000), and
nothing here assumes root.

**Two-runner walkthrough:** the walkthrough is written out in the module docstring, and
`test_two_runners_adding_the_seventh_at_once_give_one_seventh` runs it with two threads and a
barrier. With six active, writers A (`d-mbp`) and B (`d-phone`) each add a seventh at once.
A takes `automations.lock`, and B blocks there having read nothing. A reads every stream (each
under `LOCK_SH`), folds six active, appends `d-mbp:7` under the stream's `LOCK_EX` until
fsynced, and releases. B wakes, reads only now, folds seven active, writes nothing and raises
`CapReachedError` naming the one to retire. The log holds seven adds and the fold reports
nothing unapplied. Two devices that never shared a lock are
`test_two_devices_that_never_shared_a_lock_still_fold_to_seven`: both sevenths are in the
log, and the first in ref order is applied while the other is reported as past the cap, on
every machine. Lock order is `automations.lock`, then stream files (shared, released), then
one stream file (exclusive). Nothing takes `automations.lock` while holding another lock.
The register reads streams directly and never the index, so it takes no `index.lock`.

**Stranger pass:** done on the whole diff. What it found:
- *A swap that half-lands.* `add(retire=...)` appends the retire, then the add. If the second
  append raises (disk full, say), the retire is in the log and the add is not. The exception
  reaches the caller, and the log says exactly what happened: one retired, nothing added. No
  status lies. The order cannot be reversed, because with the register full the fold would
  refuse an add that comes before its retire. I left it, and say so here.
- *A stand-in ref.* The swap's effect is checked against a register with the retire applied
  under the stand-in ref `d-pending:1`. Only the add's refusal is read from that register,
  and it is thrown away, so the stand-in never reaches a caller or the log. There is a
  comment on the line.
- *An empty-register message.* `full_message` is only reached when the register is full, so
  `suggestion` is `None` only when every active automation is pinned, and the message says
  that rather than offering nothing silently.
- *Ordering by `ts`.* `ts` is always an RFC 3339 instant with an offset (the event type refuses
  anything else), so `datetime.fromisoformat` gives aware datetimes that compare correctly
  across offsets. A plain string comparison would not.

Looked for and not found: an ignored error (no `except` in the module), a lock held across
anything but one decision, a file handle left open (the lock file is closed by `with`), and a
read taken before the lock (every write folds inside `_exclusive`).

**Review:** independent review is the reviewer agent's job, on this PR, per the brief. I did
not run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **Supersede does not apply to automation events (rule 5).** A tombstone of an add would
  remove a pinned automation without the unpin D114 requires, and free a slot with no retire
  on record. So the fold reports it and changes nothing. The inbox makes the same kind of
  local choice for answers (its rule 6). Restoring a retired automation is a new add of the
  same name. #96 may want a dedicated restore, and it can add a verb then.
- **A pinned automation cannot be retired, even by a person, until it is unpinned.** D114 only
  bars *automatic* transitions, so a softer reading would let a person retire a pinned one
  directly. I took the stricter one so that "pinned is honoured" holds for the swap too: a
  person answering a full register cannot retire a pinned automation as a side effect. The
  cost is one extra event, the unpin.
- **The suggestion is the oldest unpinned, by when it was added.** "Least recently used" would
  be better, but nothing records use yet. #97 adds `outcome` events, and the suggestion can
  read them then.
- **`WIP_CAP` is a constant, not a parameter or an event.** A second caller with another
  number would fold the same log to a different register, and D6 says to "start at 7", so
  moving it is a decision for `CONVERSATION.md`.
- **The API takes names; the log stores refs.** A person thinks "retire bin-night". The event
  stores the add's ref, so a name reused after a retire never confuses two automations.
- **No CLI.** This follows the dormant rule and the inbox's precedent. A command is what the
  owner turns on after reading this.

**What to read first:** `refusal` and `fold_automations` in `src/walle/automations.py`. They
are the whole rule set in about 60 lines. Then rule 5 in the docstring, the one choice most
open to disagreement.

**Noticed, not done:**
- `ARCHITECTURE.md` §5.2's taxonomy table lists neither `ask`/`answer` nor `automation`. It is a
  CODEOWNERS path, so it is left for the owner.
- `MACHINE_ACTORS` lives in `walle.inbox`, and this module imports it from there. It belongs in
  `walle.events` beside the `ACTOR_*` constants. Task 70 noted the same thing.
- #96's deterministic pass moves automations to stale and archived "by timestamp" on its own.
  Rule 4 here refuses every machine-written automation event, per D72. #96 will need to say
  how an automatic transition is written: a separate verb the kernel may write, or a fold that
  derives staleness from timestamps without writing anything. D114 allows the first and D72
  forbids machine writes to automations, so that task may need a decision.
- An automation does not yet name what it runs (a kept workflow from `walle.builder`, or a
  mission). D114's "nothing a live automation's manifest names is retired" will need that
  link. An optional `source: Ref` on `add` would add it without a schema bump.

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
