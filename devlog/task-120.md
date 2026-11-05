# Task 120: folds merge streams by time, through one shared order

Issue #120, audit findings 5.1 and 5.2. Branch `task/audit-folds`, created empty by the owner as
the claim and reused here as his comment on the issue says; from `main` at `834c6d8` (task 115).
Nothing here adds a write path or a switch: the register, missions and outcomes stay off by
default exactly as before, and their existing dormant tests still prove it.

**The finding, in one line:** every fold that applies events one after another sorted them by
ref, `(stream, seq)`, so every event the laptop ever wrote was applied before the first event
the phone wrote. A retire from the laptop of an automation added on the phone found nothing to
retire; at the cap the oldest automation vanished and its runs were reported as naming
nothing; a pause from the laptop was folded before the resume it came after; a status from the
laptop for a node the phone injected was reported as naming a node the mission did not have.

**Shipped:**
- `src/walle/merge.py`, new, 30 lines of code under a long docstring. The one place a fold's
  order comes from, with two functions because folds need two different things:
  - `fold_order(events)` — the order a fold **applies** events in. A k-way merge of the
    streams: each stream in its own `seq` order, streams interleaved by `ts` compared as
    instants in UTC, a tie at the same instant ordered `(stream, seq)`. Implemented as a heap
    of stream heads; a stream's next event is eligible only once its previous one is out.
  - `ref_order(refs)` — the order a fold **lists** a set of refs in: D57's `(stream, seq)`,
    once each. The resolver's order (below, *Choices made*).
  - `merge_order({ref: ts})` is the merge itself; `fold_order` is it over events.
- `src/walle/automations.py`, `missions.py`, `inbox.py`, `survival.py`, `stats.py`: each
  `sorted(events, key=parse_ref)` becomes `fold_order(events)`. No other logic changes. The
  module docstrings that said "ref order" now say what the order is and point at `walle.merge`;
  `automations.py`'s and `inbox.py`'s "what this does not cover" paragraphs are rewritten,
  since what they described as a limit was the bug.
- `src/walle/resolver.py`: `_ref_key` and its five sorts become `ref_order`. Same order as
  before; it now comes from the shared module, with the reason stated in `resolve`'s docstring.
- `src/walle/opik_export.py`: two comments. The code already took a span's start as the
  earliest report and the trace's end as the latest stamp, so no negative duration was
  possible on `main` at `834c6d8` (5.2's third point does not reproduce there; the exporter
  has used `min` for a span's start and `max` for the trace's end since it shipped in #90).
  What *was* wrong there is that a node started on
  the phone and completed on the laptop folded to `running`, so the mission never finished and
  nothing was exported at all. That is the missions fix, and the new test pins it at the seam.
- `tests/test_merge.py`, new, 18 test functions (25 with parametrisation): every rule in the
  docstring, the two limits, and source checks that every applying fold imports `fold_order`,
  the resolver imports `ref_order`, none sorts events by ref on its own, and `merge.py` reads
  no clock and opens no file.
- 13 new tests across `tests/test_automations.py`, `test_missions.py`, `test_survival.py`,
  `test_opik_export.py`: the audit's reproductions, one seam test per seam through the real
  modules, ties, a backwards clock, and determinism (folded twice, reversed, duplicated,
  shuffled over ten seeds).
- `tests/test_resolver.py`, `tests/test_missions.py`: the import allowlists of the two purity
  tests gain `walle.merge`, with a comment. **This is a test edited to make it pass, and the
  task requires it:** a shared order every fold imports cannot exist without those two folds
  importing it. `walle.merge` is held to the same bar by `test_merge` (no clock, no file) and by
  `test_constraints`' closure check, which follows the resolver's imports through it.

**Why:**
- D57: `seq` is scoped per stream and the log is device-mergeable by union, with no conflicts
  because nothing is edited. All of that is kept. What D57 names as the merge key, `(stream,
  seq)`, stays the order within a stream (`seq` is the writer's order under the lock; it is the
  stream's order by construction) and the order of last resort when two streams tie. What
  changes is only how two streams interleave: by the stamp each event carries, which is the
  log's own `ts` (D93: "when the log learned something"), not a clock the fold reads. The
  issue's own suggested fix, and three places that already sorted this way (`Register.
  suggestion`, `hot.compose`, `context.open_view`), read D57 the same way. I did not flag it:
  a union of streams has no interleave of its own, and nothing in D57's reasoning — append-only,
  no conflicts, no migration later — depends on alphabetical stream names.
- D8 and hard constraint 3: the merge is a pure function of the refs and stamps it is given.
  No clock, no file, no model. Rebuild is deterministic; the same log folded twice is the same
  answer; the same records in any order are the same answer. Tested in every fold.
- D67: task 67's walkthrough said the last delta "in ref order wins, which is stream order and
  then seq, not wall-clock order". That was a choice inside that task, recorded as a known
  limit, and this task replaces it: the last delta by time wins across devices, by seq within
  one. Which answer is *right* when two devices disagree is still D91's recovery, by a further
  explicit step; the fold still does not guess.

**Verify:** each reproduction a failing test first, then passing; one seam test per seam;
nothing written on a refusal. Before the fix, nine tests failed for the finding's reason:
`test_a_swap_from_the_laptop_of_an_automation_added_on_the_phone_holds`,
`test_what_a_write_returns_is_what_the_next_read_folds`,
`test_events_on_two_devices_apply_in_time_order` (automations);
`test_a_pause_from_the_laptop_after_a_resume_from_the_phone_holds`,
`test_a_status_from_the_laptop_for_a_node_the_phone_injected_applies`,
`test_the_last_delta_is_the_last_by_time_across_devices` (missions);
`test_a_node_started_on_the_phone_and_finished_on_the_laptop_is_one_span` (opik_export);
`test_a_swap_from_the_laptop_keeps_the_phones_automation_and_its_runs` (survival, the issue's
reproduction verbatim through `automations.add`, `survival.record`, `survival.report`); and
`test_merge.py` as a whole, which could not import. The tie and backwards-clock tests passed
before the fix by coincidence (stream order happened to agree) and pin the stated rule.
Seams driven through the real modules: `automations.add` → `fold_automations` → `active`;
`automations` + `survival.record` → `survival.report`; `fold_missions` → `dispatchable`;
`fold_missions` → `opik_export.traces` → `span_payload`. The golden logs still replay
(`tests/test_golden.py` and the per-module golden tests unchanged and green).

```
uv run pytest                3998 passed, 35 skipped in 114.33s
uv run ruff check            All checks passed!
uv run ruff format --check   195 files already formatted
uv run pyright               0 errors, 0 warnings, 0 informations
```

**Ties and a clock that ran backwards (the stated behaviour, each with a test):**
- *Tie across streams:* same instant on two streams → `(stream, seq)`, D57's order, so it is
  still one answer. With two sevenths stamped alike, the laptop's is applied and the phone's
  reported (`test_the_same_instant_on_two_devices_falls_back_to_stream_order`, in both
  automations and missions). Two spellings of one moment (`04:30+00:00`, `10:00+05:30`) tie.
- *A clock that steps back on one device:* that device's own order never changes — seq 2 is
  applied after seq 1 whatever the stamps say — and no other device's events move relative to
  each other. The later seq with the earlier stamp waits behind its predecessor and is
  interleaved from there. The phone adds `a-1` at 10:10, steps back, adds `a-2` at 09:50 and
  retires `a-1` at 09:55: everything applies, where a plain sort by stamp would have applied
  the retire before the add it names and reported it
  (`test_a_clock_that_steps_back_on_one_device_never_reorders_that_device`; the missions twin
  keeps `completed` after a `running` stamped later). Determinism is tested over reversed,
  duplicated and shuffled input in the same tests.
- *A clock that is ahead or behind on one device* moves that device's events relative to the
  other device's, deterministically; a retire stamped before the add it names is reported, as
  it always was. The order does not guess at causality (*Noticed, not done*).
- *When every clock is monotone* the merge is exactly the sort by `(instant, stream, seq)`
  (`test_with_monotone_clocks_the_merge_is_the_plain_sort_by_instant_stream_seq`, fifty random
  logs). One stream always merges to its own ref order, which is why no single-device golden
  log moved.

**Two-runner walkthrough:** the merge opens nothing, so the writers' walkthroughs in
`automations.py` and `inbox.py` hold unchanged. The case this task changes is two devices that
never shared a lock. A adds `gym-class` on the phone at 10:00 (`d-phone:1`). B, on the laptop,
retires it at 10:10 (`d-mbp:7`) after the logs met. Before: on every machine the fold applied
`d-mbp:7` before `d-phone:1`, reported the retire as naming nothing, and kept `gym-class`
active; `automations.retire` had returned it as retired, so the write and the next read
disagreed. After: every machine applies `d-phone:1` at 10:00 then `d-mbp:7` at 10:10, and the
register B's `retire` returned is the register A's next `active` reads. Two sevenths before
the logs met, A's at 10:00 and B's at 10:01: A's is applied and B's reported, on both machines
— the earlier by stamp, where before it was the earlier by stream name. Same stamp to the
microsecond: the stream that sorts first, on both machines.

**Stranger pass:** done on the whole diff (60 lines changed across seven `src` modules, mostly
docstrings, plus the new module; it touches every fold, which is the error path of the whole
log). Found:
- The first draft of `test_merge` asserted that `hot.compose`, `context.open_view` and
  `Register.suggestion` also use the merge. Switching them broke
  `test_a_summary_cannot_forge_an_index_line` in `test_hot`, which was right to break: they sort *beliefs*
  for display by a per-belief key, not events for application, and a per-item key is what a
  display wants (*Choices made*). Reverted; the test was mine and went.
- The first draft had the resolver iterate in `fold_order`. Dozens of seeds of
  `test_any_snapshot_in_any_log_folds_identically` failed, and the failure is a theorem, not a
  bug: see *Choices made*. The resolver lists in `ref_order`.
- `opik_export`'s span `end` is the step that set the status, `times[-1]` in merge order;
  `start` is `min(times)`. `end >= start` always holds, so `duration_ms` cannot be negative,
  and I did not take 5.2's suggestion of `max(times)` for `end`: with a backwards clock that
  would stamp the span's end from a step that did not set its status.
- `ref_order` deduplicates; `Conflict.sides` was built from a set before, so nothing changes,
  and a contradiction naming one ref twice still counts one side.
- One full run showed `tests/test_evals.py::test_no_marker_reaches_the_wire_a_proposal_or_a_
  verdict` failing once; it passed on the next three full runs and five isolated runs, and it
  is not in this diff's closure (the proposer boundary, `tmp_path`). Noted below, not chased.

**Review:** none before merge; the reviewer agent reviews the PR's head per the brief.

**Choices made inside the task:**
- **A merge, not a sort by `(ts, ref)`.** The issue suggests `(datetime.fromisoformat(ts),
  parse_ref(ref))`. That is a per-event key, which is simpler, and it is wrong for one device:
  when its clock steps back, a plain sort applies that device's own events out of `seq` order,
  so a retire can precede the add it names on one machine with one stream. `seq` is the
  stream's order by construction (D57); a clock is only what the device believed. The merge
  keeps `seq` within a stream and uses stamps only to interleave streams. The existing
  `test_times_are_by_clock_when_streams_interleave` in `test_opik_export` already encoded this
  (a `completed` stamped before its `running`, on one stream, is still completed); a plain sort
  would have broken it.
- **Two orders, named, in one module.** Rule 1 (seq within a stream) ties an event to its
  predecessor; rule 2 (time across streams) ties it to its stamp. When a clock ran backwards
  the two disagree, and then *no* order can also be stable under taking events away: forget
  the phone's seq 1 (stamped 10:10) and its seq 2 (stamped 09:50) moves from after 10:10 to
  09:50. `test_consolidation`'s random logs — observations stamped anywhere in ten hours,
  everything else at minute zero — are full of exactly this, and `view` must leave the
  resolver's answer identical after a snapshot forgets observations. The folds that apply
  events run over whole streams and never see a subset; the resolver applies nothing in
  sequence (a supersede and a contradiction name their refs — "That is what makes the answer
  independent of input order", its own docstring) and only lists, so it lists in the order
  that is stable: D57's. Both orders live in `walle.merge`, with the limit written down and
  tested (`test_taking_an_event_away_can_move_its_streams_later_events_when_a_clock_ran_back`).
  A reader who wants "one order" has one for applying and one for listing, and the module
  says which is which and why.
- **Display sorts left alone.** `Register.suggestion` ("oldest unpinned by the `ts` of its
  add"), `hot.compose` and `context.open_view` ("oldest first by when the log learned it")
  sort the fold's *output* by a per-item key. They are not folds, they were already by time,
  and a per-item key is stable under subsetting, which a rendered file wants. The audit cited
  them as the precedent, not the problem.
- **The resolver's tie-break for `_earliest`** ("when two cannot be ordered the one met first
  is kept") and `Retired.by` are listing orders and stay in `ref_order`. No behaviour change.
- **No causal tie-break.** A device whose clock is ahead still has its retire of another
  device's add applied before the add if its stamp is earlier; the fold reports it. An order
  that also respected "an event never precedes a ref it names" would fix that for `target`,
  `mission` and `caused_by`, but it is a different rule with its own failure mode (a cycle in a
  hand-written log) and a decision Shravan has not made. Noted below.
- **`stats` and `inbox` moved too**, though the issue names neither: both apply events in
  sequence (first ask wins, first citing assertion decides keep versus edit), and the request
  was one order for every fold. Their behaviour on one stream is unchanged.

**What to read first:** `src/walle/merge.py`'s docstring, then
`test_a_clock_that_steps_back_on_one_device_never_reorders_that_device` in
`tests/test_automations.py` for why it is a merge and not a sort, then
`test_taking_an_event_away_can_move_its_streams_later_events_when_a_clock_ran_back` in
`tests/test_merge.py` for why the resolver lists differently.

**Noticed, not done:**
- **`ARCHITECTURE.md` §5.1 and D57's text** say the merge is "ordered by `(stream, seq)`".
  Both are under CODEOWNERS. This task keeps that as the within-stream and tie order and
  interleaves streams by stamp; if Shravan wants the record to say so, an R-note on D57 and a
  sentence in §5.1 are the change. Not flagged, because nothing in D57's reasoning depends on
  the interleave and the owner's own audit asked for this fix; flagging is one comment away if
  he reads it otherwise.
- **A causal tie-break** (above). Would need a decision.
- **`survival.survival`'s `as_of` filter** keeps records with `ts <= as_of`, by stamp. With a
  backwards clock, the records kept need not be a prefix of their stream, so a report as of a
  moment inside the step can see seq 2 without seq 1. Pre-existing, by design of task 97; the
  merge does not make it worse.
- **`walle.mcp_server`'s recall ranking** sorts by score, then `ts`, then ref. Not a fold, not
  changed, and the one remaining `(ts, parse_ref)`-shaped sort outside the display ones.
- **`tests/test_evals.py::test_no_marker_reaches_the_wire_a_proposal_or_a_verdict`** failed
  once in one full run and never again (three full runs, five isolated). Outside this diff;
  worth a look if it recurs.
- **`PLAN.md` §9** is under CODEOWNERS and not updated.

**Cost:** pending from Hyperagent.
