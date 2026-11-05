# Task 67: missions as DAGs — `mission` and `step` events, the pure fold, dormant by default

Issue #67. Branch `task/p2-missions`, from `main` at `c17fef6`. Phase 2's first task (R8). Nothing
here dispatches anything: this is the schema, the fold and the switch, and the switch is off.

**Shipped:**
- `src/walle/events.py` gains the two types §5.2 and D67 name, each at its own `v = 1` (D96):
  - `Mission`: `title`, `nodes` (at least one) and `links`, which is networkx's `node_link_data`
    without its metadata keys. A `Node` is `id`, `title`, its manifest (`reads`, `writes`,
    `egress` — D67 with D75), a `token_budget` (D22) and an optional `assignee`. A `Link` is
    `source -> target`: the source must be completed before the target is ready. The
    validator refuses, at construction and on load, a duplicate node id, a link to a node the
    mission lacks, a duplicate link and a cycle — the cycle is named in order in the error.
  - `Step`: a delta against one mission, with `mission` as a ref and a `verb`: `status`
    (`node`, `status`, optionally `cost_usd` and `tokens`), `pause`, `resume` (no fields),
    `inject` (`inject`, the new node; `after`, its predecessors; `before`, its successors) and
    `reassign` (`node`, `assignee`). The validator holds each verb to exactly its fields, from
    the `_STEP_FIELDS` table, so a pause cannot carry a status and a status cannot carry a node.
    `note` is free text for any verb.
  - `StepStatus` (`pending`, `running`, `completed`, `failed`) and `StepVerb`, closed sets like
    `Level`; `NodeId`, held to the stream-id rule (lowercase, no dots) because an id may one day
    be a path segment; `find_cycle`, iterative depth-first, shared by the validator and the fold.
- `src/walle/missions.py`, the fold. `fold_missions(records) -> Missions`: every live mission in
  ref order as a `MissionState` — nodes with status, predecessors, current assignee, spend and
  the step that injected them; links; `paused`; the applied step refs — plus `unapplied`, every
  live step it could not apply and why. `ready`, `running`, `outcome` and `spent` are properties
  of the state. `dag_hash` is the canonical-JSON SHA-256 of the resolved DAG (D66). `enabled`
  and `dispatchable` are the dormant switch (below).
- `src/walle/resolver.py` gains `live_events(records)`: events by ref and rule-1 liveness, the
  seam the mission fold starts from so a `supersede` means one thing everywhere. Twelve lines, no
  new import; the resolver's own import test still passes unchanged.
- `tests/fixtures/v1/chores/`, the golden log for both new types at `v1` (D88, D96): a made-up
  Sam's Saturday, one mission of four nodes, every verb once, a failed step undone by a
  supersede. Its `__expected__.json` has 31 sentinels over the new fields. `test_golden` picks it
  up by directory and its coverage check is what required it.
- `tests/test_missions.py`, 70 tests: the issue's four verify criteria, the dormant rule, the
  step shape per verb, the inject edge cases, purity, and the golden log's DAG pinned by hash.
- `tests/test_events.py`: `_one_of_each` gains a `Mission` and a `Step`. The task requires this
  edit: `test_every_taxonomy_type_is_registered` asserts that list and `EVENT_TYPES` agree, which
  is the point of the test, and the round-trip test now covers both new types for free.

**Why:** D67 says a mission is a DAG of steps in `node_link_data` shape, a step is ready when every
predecessor is completed — a pure function of node status, consistent with D8 — each node's
`reads`/`writes` is its manifest (§7 rule 2, with D75's `egress`), each step carries a
`token_budget` and `cost_usd` for D22, and pause / inject / reassign are the intervention verbs.
`step` events are "node-status deltas; the resolved DAG is a fold, never the in-place mutation
Arcturus does". That sentence is the shape of `missions.py`: the mission event is never touched,
every change is a step, and the DAG as it stands is computed from the log each time. D8's rule 1
is reused rather than re-implemented, so undoing a pause is a tombstone on the pause.

D117 is the dormant rule's reasoning: the agent may install, only a human enables. The switch is
`WALLE_MISSIONS=on` in the environment — one spelling, like an instant — because the environment
is the one place no event the agent appends and no file it writes under the root can reach.
`dispatchable(state)` returns nothing unless it is on and the mission is not paused; `ready` keeps
answering, because reading the DAG is not acting on it. The `walle` command gains no subcommand.

**Schema version.** A new event type is a schema decision (D120 says so of `proposal`), so this is
written down: D96 versions each type on its own and the golden check requires a `v1/` log holding
each type at `v == 1` — the rules already settle where a new type starts, and both arrive at
`SCHEMA_VERSION = 1` with no upcaster and no bump to any existing type. The types themselves are
D67's and §5.2's, named again in the owner-approved issue, so no new decision was needed and the
issue was not flagged.

**Verify:**
- *Folding the same log twice gives the same DAG:*
  `test_folding_the_same_log_twice_gives_the_same_dag` and
  `test_input_order_does_not_change_the_dag` (twenty shuffles across two streams, equal
  `Missions` and equal `dag_hash`).
- *A cycle is refused:* `test_a_mission_with_a_cycle_is_refused_as_an_event`, `..._self_link_...`,
  `..._long_cycle_is_named_in_order`, `..._cannot_enter_the_log_by_parsing_either` (the validator
  runs on load, so a hand-edited line is refused too), and
  `test_an_inject_that_would_close_a_cycle_is_reported_and_leaves_the_dag_alone`.
- *Pause, inject and reassign are events:* `test_pause_and_resume_are_events`,
  `test_inject_is_an_event_that_adds_a_node_and_its_links`, `test_reassign_is_an_event`, and
  `test_a_superseded_intervention_is_undone` for D8.
- *The rebuild test stays green:* `test_golden` passes over `v1/maya` and the new `v1/chores`,
  and `test_the_mission_fold_over_the_golden_log_is_the_one_it_shipped_with` folds the fixture
  three ways — the rebuild's records, the index, the raw JSONL — and pins the DAG hash.
- *Off by default:* `test_missions_are_off_by_default` (switch unset: `enabled()` is false and a
  mission with a ready node dispatches nothing), `test_only_the_one_spelling_turns_missions_on`,
  `test_nothing_in_the_log_can_turn_missions_on` (the module opens no file and neither event has
  an enable field), `test_the_cli_gains_no_mission_command`.

Written first: with no `Link` in `walle.events`, `tests/test_missions.py` failed at import. After
the module landed the only failures left were the two fixture tests, which the fixture fixed.

```
uv run pytest               1544 passed, 10 skipped in 61.18s
uv run ruff check           All checks passed!
uv run ruff format --check  112 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The tests do not assume root. The golden-log test copies the fixture to `tmp_path` and makes the
copy writable first, like `test_golden`; it passes with `tests/fixtures/v1/chores` set read-only.

**Two-runner walkthrough:** the fold opens nothing, so there is nothing to walk through on its
own. The mission events go through `StreamWriter` like any other, so its walkthrough (task 4)
holds. One mission-specific case is worth stating: writers A and B both append a `status` step
for the same node, A `completed`, B `failed`, to two streams. Neither sees the other's step at
write time. When both streams are folded, rule 2 applies — the last live delta *in ref order*
wins, which is stream order and then seq, not wall-clock order — and both folds, on both
machines, give the same answer once the logs meet (D57). Which answer is right is for D91's
recovery, by a further explicit step; the fold does not guess.

**Stranger pass:** done on the whole diff (about 620 lines of `src` across a new module and two touched ones, with two error paths).
Found and fixed:
- `find_cycle`'s first draft had a nonsense line in the "every successor explored" branch,
  left from an earlier shape of the loop; rewritten as pop, discard from the path set, mark done.
- An `inject` with `after=["a", "a"]` passed every check and would have added the same link
  twice, giving the node a duplicated predecessor. Now reported as "the same link twice";
  `test_an_inject_naming_one_predecessor_twice_is_reported`.
- The dormant rule did not say what a `status` delta does while the mission is paused. It is
  recorded — the kernel finishing a step it had already started is a fact, pause or not — and
  `dispatchable` still returns nothing. Written into rule 5 of the module docstring and tested
  by `test_a_status_delta_is_recorded_while_paused`.
- The golden-fixture test copied the fixture without making the copy writable, unlike
  `test_golden`; it would have failed on a read-only checkout for a reason unrelated to missions.
Looked for and not found: an ignored error (every "cannot apply" is a returned reason, and every
reason reaches `unapplied`); a status message that lies (`applied` lists exactly the steps that
changed state); a path where ref order and input order could differ (`live_events` builds a dict
by ref and the fold sorts it); float spend drift (summed in ref order, rounded to 6 places at
each add and at the total, so deterministic).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run; the brief replaces it for agent work.

**Choices made inside the task:**
- **One `step` type with a `verb`, not five event types.** D67 says "`step` events", singular
  type. Five types would be five discriminator values with mostly the same fields, and every fold
  would list all five. A per-verb field table in one validator keeps the shape closed without
  that.
- **`completed` is the only status that satisfies a successor.** D67's words. A `skipped` status
  was considered and dropped: it would either count as done (then it is a lie about the work) or
  not (then it is `failed` with a nicer name). A human who wants to move past a step marks it
  completed with a `note`, or injects a replacement.
- **The fold does not police transitions.** `completed` then `running` is accepted and the later
  one wins. The fold is a record of what was reported; D91's recovery decides what an odd sequence
  means, with another explicit step. Policing here would make a crashed kernel's half-written
  truth unreadable.
- **Pause is mission-wide, not per node.** The verb exists for the person who comes back to a
  forty-minute mission and wants everything to stop; per-node pausing is `reassign` to nobody, or
  a future verb if it is ever wanted.
- **An inject that would break the DAG is reported, not raised.** The resolver raises only when
  no true answer exists (a supersede cycle). An unapplyable inject has a true answer: the DAG
  without it. Raising would make the whole log unreadable over one bad intervention.
- **`Step.mission` is a ref field, not a `caused_by` entry.** It mirrors `supersede.target`: a
  pointer to what it changes, kept apart from provenance. Consequence: the index's dangling-ref
  check (D111) covers `caused_by` and so does not halt on a step whose mission is missing; the
  fold reports it under `unapplied` instead, which is the resolver's treatment of a dangling
  supersede target.
- **The environment variable, not a config file.** A file under the root is reachable by
  anything that writes there; a file under `~/.config` is reachable by a capability with that
  path in its `writes`. The environment is set by the person's shell and by nothing in walle.
  `enabled(environ)` takes a mapping so the tests never touch the real one except through
  `monkeypatch`, and so a runner can be handed a frozen environment at start.
- **`dag_hash` includes the mission event's content hash.** The resolved DAG is the plan plus
  what happened to it; two plans with the same node ids and different titles are two DAGs.
- **The fixture is a second directory, not more lines in `v1/maya`.** A golden log is frozen when
  it ships; appending to Maya's would change a log that is supposed to prove the old shape still
  loads unchanged.
- **The resolver gained a public seam rather than `missions.py` importing `_by_ref` and
  `_liveness`.** Private names across modules are a maintenance trap; `live_events` is the one
  function and it is documented as the seam.

**What to read first:** `missions.py`'s module docstring (the five rules, about 40 lines), then
`_Building.apply` and `_inject` (about 50 lines), which are the whole fold. Then `Step`'s
docstring and `_STEP_FIELDS` in `events.py`, for the per-verb shape. Then
`test_missions_are_off_by_default` and `test_nothing_in_the_log_can_turn_missions_on`.

**Noticed, not done:**
- **`walle why` does not reach a step's mission.** `why` walks `caused_by`, and `Step.mission` is
  a separate field, so `why d-mbp:5` on an inject shows its causes but not the plan it changed.
  Either `why` learns the field or steps conventionally also cite their mission in `caused_by`.
- **The index's FTS indexes `step.note` and not `mission.title`.** `_TEXT_FIELDS` is `summary`,
  `body`, `reason`, `note`. A later `recall` over missions would want the title and node titles.
- **Empty `after` and `before` are written on every step** (`exclude_none` keeps empty lists),
  as `caused_by: []` already is. Harmless and consistent, but noisy on a `pause`.
- **No `walle mission` command and no runner.** On purpose (dormant rule), but the next Phase 2
  task that dispatches will need: the manifest check before dispatch (§7 rule 2), the inbox for
  asks (D78), D91's unmatched-step recovery over `running`, and the container executor (D68).
  `dispatchable` is the one function it has to go through.
- **`token_budget` is declared, not enforced.** The fold sums `tokens` and `cost_usd`; nothing
  compares them to the budget yet, because nothing spends yet.
- **A mission with no nodes is refused** (`min_length=1`). If an empty plan that is filled by
  injects turns out to be wanted, that is a one-line change and a fixture.
- **PLAN §9's status column** is not updated; `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time; Hyperagent's figure for this thread to be
filled in by the owner.
