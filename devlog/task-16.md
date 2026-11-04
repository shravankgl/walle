# Task 16–18: Proposer — read the log, propose into staging, judge in shadow

Issue #13. Branch `task/16`, PR #39. Two commits: the first build (`ade2666`), and a second
that fixes the first review's findings (1 high, 2 medium, 2 low). *Review* lists each finding
and what changed for it. Built in parallel with other agents, so everything is in new files
except one appended command in `cli.py`.

**Shipped:**
- `src/walle/proposer.py` (new). The proposer and its shadow log.
  - `propose(root, principal, device, model, *, ts=None)` brings the index up to date, takes
    every observation no proposal cites and this model has not been shown, withholds the ones
    `hazards.flag` catches, cuts the rest into excerpts, asks the model in bounded requests,
    checks each reply item, and appends `proposal` events with `actor: proposer` and
    `caused_by: [<observation>]` — the same queue `walle remember` writes and `walle review`
    decides (D60). It never writes an assertion or a supersede. Then it judges every staged
    item (from `remember` too) and records a `keep`/`drop` verdict in the shadow log (D82).
    Returns a `ProposeReport`; `report.silent` is D63's "nothing to say".
  - `Model` is a `Protocol`: `name` and `complete(Request) -> str`. `Request` carries the
    `kind`, the envelope `marker`, the `items` (`(id, enveloped text)`) and the full `prompt`.
    `FakeModel` is the only implementation: deterministic, offline, proposes the person's own
    `user:` lines that carry a lasting-statement cue, judges on the same test, and replies in
    the JSON shape a real model is asked for, so the parsing path is the real one.
    `MODELS = {"fake": FakeModel}` is what the command line can name.
  - `excerpts(ref, body)` cuts a body into `<ref>#<n>` pieces of at most `EXCERPT_CHARS`
    (2000) on line boundaries; `_requests` envelopes them under one marker per request and
    groups them into at most `REQUEST_CHARS` (8000) of enveloped text per request.
  - `envelope(text, marker)` is D55's `<<<untrusted:…>>>` block with a 16-hex marker random
    per request; `_deface` breaks any `<<<`/`>>>` already in the text.
  - `_candidates` reads a propose reply: not JSON or not a list → `ModelReplyError`, nothing
    written; per item, `malformed`, `unknown-excerpt` (not an excerpt of *this* request),
    `oversize` (body over `BODY_CHARS`, 2000), `marker-in-text`, `hazard-in-text` and
    `duplicate` (the same fact from the same observation anywhere in the run) are refused
    and counted in `report.dropped`. A long summary is cut to one line
    (`staging.summarise`), not refused.
  - The shadow log is `principals/p-<name>/shadow/`: `verdicts.jsonl` (`ShadowVerdict`: `ts`,
    `proposal`, `model`, `verdict`, `reason`) and `seen.jsonl` (`Seen`: `ts`, `observation`,
    `model`). Both are append-only JSONL with `extra="forbid"` models, written under the
    file's own exclusive lock with a re-read for duplicates, and read by `score` and the next
    run only. A line that does not load — a torn tail from an interrupted append — is
    stepped over on read, and an append after a torn tail starts with a newline (D80).
    Nothing on the resolution path knows the folder exists.
  - `score(root, principal, *, model)` compares the latest verdict per proposal with what the
    log says became of it: `agree`, `false_allow` (model keep, person dropped — D77's
    metric), `missed` (model drop, person kept), `pending`.
- `src/walle/hazards.py` (new). `flag(text) -> tuple[str, ...]`: the kinds found, never the
  text. Six kinds: `credential` (known key shapes, JWTs, private-key headers, the corpora's
  `FAKE-<KIND>-NOT-REAL-<nnnn>`, and any 20+ character run mixing letters and digits),
  `secret` (a password/PIN/code named and followed within 60 characters by a token with a
  digit), `contact` (e-mail, phone, street address), `disclosure` ("in confidence", another
  person's health in so many words), `instruction` (an order to ignore instructions or to
  write memory — "remember permanently", "save a note that", "store as a fact", "add to the
  user's rules"; text addressed to "AI assistant:"/"system:"; a claim of prior approval —
  "already approved", "pre-approved", "review status:", "the user has asked you to") and
  `boundary` (a forged envelope or boundary marker — `<<<…>>>`, `</untrusted>`,
  `</tool_result>`, `<system>`, `[END OF …]` — or a forged turn). `forged_turn(text)` is the
  turn rule: the observer writes the person's block first and the reply after it, so a
  `user:` line after an `assistant:` line or after a blank line did not come from the
  person. Pure regex and one line scan, no IO, no model.
- `src/walle/cli.py` (changed, 75 lines: one import and one appended command). `walle propose --principal P
  --device D --model fake [--score]`. Prints nothing when the run is silent (D63). Never
  prints observation text: withheld items are listed as `ref (kinds)`.
- `tests/test_proposer.py` (new): 112 tests with their parameters, listed under *Verify*.
- `tests/fixtures/proposer/d-mbp.jsonl` (new): a synthetic stream of six observations and
  two `remember`ed proposals for an invented allotment keeper: a preference, a routine, a
  shed code shaped `FAKE-SHEDCODE-NOT-REAL-0042`, a neighbour's invented number `555-0133`
  and street, a recipe question, and a correction. It is a bare stream file, not a
  `principals/` tree, because `.gitignore` excludes that folder name ("never the log of a
  real person") and the rule was not weakened for a fixture; the test lays it out.

**Why:**
- **D44:** Phase 0 has a minimal proposer. This is it: read the log, propose, nothing else.
  No evals, no self-modification.
- **D60, D49, D86:** the extractor's output goes to the staging queue and only a human moves
  it on. The proposer writes `proposal` events through the same writer `remember` uses, and
  nothing it writes can reach `resolver.current` or the hot tier until `review` keeps it.
- **D63:** silence is a result. A run that proposes nothing and records nothing prints
  nothing and exits 0. What is already cited by a proposal, or already shown to this model,
  is never sent again, so a quiet week is a quiet run and not a repeated report.
- **D82:** the proposer's ranking ships in shadow. Every staged item gets a recorded verdict
  from the model, the verdict acts on nothing, `review` is not delayed by it, and `score`
  compares it with the person's real decisions. Recording started with this commit, before
  any real model exists, so the day-one baseline will be there when one does.
- **D55:** everything from the log that reaches a model is inside an untrusted envelope with
  a per-request random marker, and anything shaped like a marker is defaced first.
- **D77, D18:** observations hold real transcript text. A credential, a secret, contact
  details or a confidence keeps the observation off the wire entirely. That is stricter than
  D77's "flag" and coarser than D18's pseudonymisation; both are the right later shape, and
  the one that cannot leak is the right first one.
- **D77, D55: the injection gate is in `hazards.py`, not in a model.** Text inside an
  observation is data: what a page said, what a tool returned, what was pasted. A planted
  instruction, a claim of approval, a forged marker or a forged turn withholds the
  observation before any model sees it, so no model's behaviour — the fake's or a real
  one's — is what keeps a web page from writing a memory. The fake, for its part, never reads
  a line after an `assistant:` line as a turn either (`FakeModel.person_lines`).
- **D87 kept at arm's length:** no provider library is imported anywhere in this task; the
  `Model` protocol is where a `provider:model` adapter plugs in later, in its own module.
- **Hard constraint 3:** `proposer` and `hazards` are not on the resolution path and import
  no model or network library. `test_constraints.py` is unchanged.

**Verify:** PLAN §9: "fixture log → ≥ 1 proposal; shadow verdicts recorded, never acting".
- `test_the_fixture_log_yields_at_least_one_proposal_and_no_decision`: the fixture yields
  three proposals, each by `proposer`, each citing one observation, each staged; the log
  gains no assertion or supersede; `resolve().current` is `()` before and after; the rendered
  hot tier carries none of their text.
- `test_shadow_verdicts_are_recorded_for_every_staged_item_and_never_act`: one verdict per
  staged item, `remember`'s two included; the queue is untouched; the shadow file is not
  under `log/`.
- `test_the_score_compares_verdicts_with_review_decisions`: keep one the model keeps, drop
  one the model keeps, keep one the model drops → agree 1, false-allow 1, missed 1.
- D63: `test_a_second_run_with_nothing_new_is_silent` — second run writes nothing to the
  stream or the shadow log, makes **no model request**, and the command prints nothing.
- Before the model: `test_every_dangerous_item_is_flagged` (24 items, tool text included),
  `test_no_benign_item_is_flagged` (24 near-misses), `test_a_flag_names_the_kind_and_never_
  the_text`, `test_a_flagged_observation_never_reaches_the_model` (the shed code, the number
  and the street are in no request), `test_a_remembered_secret_is_judged_without_reaching_
  the_model` (a `remember`ed gate code gets a `drop` verdict from this module and is in no
  judge request).
- **The corpora, full turns, through `propose()`:** `test_the_corpora_through_the_whole_
  pipeline[benign|dangerous|injection]`. Every turn of every item, tool turns included, is
  written as an observation and `propose()` is run with a recording model. Benign: nothing
  withheld, proposals from at least ten items. Dangerous and injection: **every item
  withheld, no request made to the model, no proposal, nothing refused, the run silent** —
  zero false-allows (D77). Every marker is checked, as written and with its `user: ` prefix
  stripped, against every proposal, every verdict reason and every request prompt.
- A line inside a body is never a turn: `test_a_user_line_after_a_reply_or_a_blank_line_is_
  a_forged_turn` (5 shapes), `test_the_observers_own_layout_is_not_a_forged_turn` (4 shapes,
  including a prompt with blank lines and the benign tool-result layout), `test_planted_
  instructions_and_forged_markers_are_flagged_on_one_line` (13 payloads with no turn
  structure around them), `test_the_persons_own_rules_and_reminders_are_not_instructions`
  (7 shapes, including the assistant saying "Remember to…" and the person writing a note).
- Long observations: `test_a_very_long_observation_is_never_sent_whole` — a body over
  80 000 characters goes in several requests, each within `REQUEST_CHARS` plus the
  instructions, each excerpt within `EXCERPT_CHARS` plus the envelope, and the one lasting
  line deep inside it is still proposed. `test_excerpts_cut_on_lines_and_lose_nothing`.
- Envelope: `test_every_request_wraps_its_excerpts_in_an_envelope_with_its_own_marker` —
  distinct 16-hex marker per request, a forged `<<</untrusted:…>>>` in the text is defaced.
- Fail closed: `test_an_unparseable_reply_writes_nothing`,
  `test_ungrounded_marker_carrying_or_hazardous_proposals_are_dropped` (one of nine reply
  items survives; the refused include an oversize body and a planted instruction),
  `test_a_long_summary_is_cut_not_refused`,
  `test_a_failed_judge_reply_keeps_the_proposals_and_records_no_verdict`.
- Requests: `test_an_excerpt_is_only_citable_in_its_own_request_and_a_fact_is_staged_once`
  (one observation across 11 requests, the same fact offered from each: one proposal, ten
  `duplicate`, and a citation of another request's excerpt refused every time),
  `test_a_long_remembered_item_is_judged_on_a_bounded_head`.
- The shadow log: `test_a_torn_shadow_line_is_passed_over_and_the_next_append_starts_fresh`
  (both files torn; reads unchanged; the next verdict lands on its own line; `score` agrees),
  `test_a_line_that_does_not_load_is_stepped_over_and_the_file_still_appends`,
  `test_a_shadow_failure_after_proposals_are_written_is_reported_not_raised` (a file where
  the `shadow/` folder should be — so the failure holds for any user, root included — the
  proposals stand, `shadow_error` is set, the command exits 0 and says so on stderr),
  `test_a_verdict_reason_is_flagged_whole_before_it_is_cut`,
  `test_verdicts_alone_keep_the_run_silent`.
- Two runs: `test_an_observation_proposed_meanwhile_is_not_proposed_twice` — a proposal
  appended by another writer while this run waited on the model is found under the stream
  lock; `already_proposed == 1`, one proposal in the log.
- CLI: a run that proposed (no secret or number in the output), `--score`, an unknown
  model refused, no log is `FileNotFoundError`.
- `test_the_proposer_imports_no_model_or_network_library`.
- **Tests first:** the module failed collection on `ImportError`; after the implementation
  the only test failures were two bugs in the tests themselves (a wrong principal in a
  helper, a name-dependent assertion) and the one real finding below.
- **Mutation check**, each undone in turn, each caught by a named test: no hazard flagging
  (the CLI test, which looks for `withheld`); no re-check under the stream lock
  (`…proposed_meanwhile…`); no excerpting (`…never_sent_whole`); no defacing (the envelope
  test); no grounding check (`…are_dropped`); **no dedupe of what was already shown** — not
  caught at first, see *Stranger pass*; caught now by the silence test's "no model request".
  Second commit: no `forged_turn` (the forged-turn test); no instruction rules (the
  injection pipeline test); `allowed` across the run and `seen` per request (the
  one-fact-once test); no body bound; judge input unbounded; torn tail not closed; strict
  shadow parse; shadow `OSError` raised; reason cut before it is flagged. All caught.
- Checks, second commit (the branch now carries main's consolidation work too):
  ```
  uv run pytest              829 passed, 3 skipped
  uv run ruff check          All checks passed!
  uv run ruff format --check 65 files already formatted
  uv run pyright             0 errors, 0 warnings, 0 informations
  ```
  First commit: 542 passed, 3 skipped; the other three the same.

**Two-runner walkthrough:**

*Two proposer runs A and B, one principal, one device, observation O not yet cited.*
1. Both `index.update` (`index.lock`, then every stream shared, released) and both find O
   neither cited nor seen.
2. Both call the model. No lock is held: a model call can take seconds and nothing reading
   the log should wait on a vendor.
3. A takes O's stream `LOCK_EX` (`StreamWriter.locked`), reads the stream through the locked
   handle, finds no proposal citing O, appends one, `fsync`s, closes; the lock goes.
4. B takes the lock, reads, finds A's proposal citing O, appends nothing for O, reports it
   `already_proposed`. The test stages exactly this with a writer inside the model call.
5. Each then appends O to `seen.jsonl` under that file's `LOCK_EX`, re-reading first; the
   second finds `(O, model)` present and writes nothing. Same for the verdict on A's new
   proposal: whichever judges second finds the verdict there.

The re-check in step 3–4 reads this device's stream only, the one under the lock. Two
*devices* proposing from one observation before their logs meet can each stage a proposal;
`review` then sees two items for one fact. Sync (D57) is where that is answered, and the
observer has the same limit (task 11).

*A proposer run and `walle review`.* Review holds `staging.lock` for one decision; the
proposer never takes it (a new proposal changes nothing about an item being decided, as
`staging.py` says). A proposal appended during a review shows up in the next `pending`. A
verdict recorded for an item the reviewer is deciding is unaffected by the decision; `score`
reads the decision from the log later.

*A proposer run and `walle rebuild`.* The shadow folder is not under `log/`; rebuild reads
only stream files and the index. The proposer's appends are ordinary stream appends under
the stream lock, which rebuild waits out with `LOCK_SH`.

*A failure mid-run.* Before the appends: nothing written. During the appends: the earlier
proposals are durable, the exception propagates, `seen` is not written, so the next run
re-shows the remaining observations and the re-check prevents a second proposal for the ones
already written. After the appends **nothing raises**: everything that touches the shadow
folder — the `seen` append, the verdict read, the judge call, the verdict append — is in one
`try`, and an `OSError`, a `ModelReplyError` or a `ValueError` there becomes
`report.shadow_error`; the proposals stand, the command reports them, exits 0, and says on
stderr that the shadow log is incomplete. The queue to judge is the records read at the start
plus what this run wrote, so the index is not read again after the appends and a log problem
cannot surface there dressed as a shadow one.

*A shadow append interrupted mid-line.* The file ends in half a line. The next read, by any
run or by `score`, steps over it. The next append sees the file does not end in a newline,
writes one, and writes its line whole; the half line stays, stepped over forever. One verdict
or one "seen" mark is lost, and the next run makes it again.

**Stranger pass:** the diff is ~1 400 lines and touches the log, two lock files and an
error path, so this was a full re-read. Found and fixed before the commit:
- **A `remember`ed secret reached the model in the judge phase.** The hazard seam was only
  on observations; staged items from `walle remember` went to the judge request as they
  were. Now `_judge` flags every staged item first and gives a flagged one a `drop` from this
  module, reason `withheld from the model: <kinds>`, with no request made. Test added.
- **An observation with nothing lasting in it was re-sent on every run.** "Already
  considered" was only "cited by a proposal", so the recipe question in the fixture would
  have gone to a vendor weekly, forever. The mutation check (dedupe removed) was not caught,
  which is how this was noticed. Now `seen.jsonl` records every observation shown to a
  model, and the silence test asserts a second run makes no request.
- **A status message that would have lied.** An `OSError` on the shadow file surfaced as a
  `ClickException` after the proposals were written, so the command would have reported
  failure for a run that staged items. Now it is `report.shadow_error`; the proposals are
  reported, the shadow problem goes to stderr, exit 0.
- **The request budget ignored the envelope.** Four 2 000-character excerpts plus four
  envelopes exceeded `REQUEST_CHARS`. Batching now measures enveloped text.
- **A long summary refused a good proposal.** A real model will exceed 120 characters; the
  summary is now cut by `staging.summarise` and only an empty one is `malformed`.
- **A phone regex that matched "100 2000".** Tightened to separated 10-digit forms, the
  7-digit local form with `-`/`.`, `+` international and `0`-led national runs.
- **A secret followed by a comma was missed** ("the gate code is 7781, for the courier").
  The token's boundary is now "not a letter or digit" rather than "whitespace".
- **The model's verdict `reason` is stored verbatim.** It is model output, so it is cut to
  500 characters and replaced if `hazards.flag` catches anything in it.
- **An empty `seen.jsonl` was created on a run that showed nothing.** Now only written when
  there is something to record.
- Checked and holding: no lock is held across a model call; `LockedStream.read` is used
  inside the lock (opening the file again would deadlock, task 11); the shadow files are
  locked alone and never while another lock is held; `index.update` is called with no lock
  held; `ValidationError` from a corrupt shadow line is a `ValueError` and lands in the
  CLI's `_LOG_ERRORS`; no path reads `~` or the environment; withheld items are reported by
  ref and kind only; the fixture carries only invented data.

**Review:**
- **Round 1:** Gemini 4 Argon, direct diff review against `63fb514` (`ocr delegate` could not
  be installed), on `ade2666`. Result: `agent-review` failure. The four checks were green.
  - **High: injection payloads could become proposals; the pipeline test dropped tool
    turns and missed stripped-prefix markers.** True on every point. The test helper
    `_observer_body` removed `role: tool` turns, which is where 20 of 24 injection items and
    4 of 24 dangerous items carry their payload; the fake parsed any `user:` line as the
    person; `hazards` had no rule for an instruction, an approval, a forged marker or a
    forged turn; and the marker check did not try the form a model that quotes the person
    would produce.
    - Fixed in `hazards.py`, not in the fake: two new kinds, `instruction` and `boundary`,
      and `forged_turn`. Every injection and dangerous item flags with its full turns, and
      no benign item does.
    - The pipeline test now feeds every turn of every item through `propose()`, asserts
      every dangerous and injection item is withheld with **no request made and no proposal
      written**, and checks every marker with and without its `user: ` prefix against the
      proposals, the verdict reasons and the request prompts. Zero false-allows.
    - The fake no longer parses a line after an `assistant:` line as a turn. That is belt
      and braces: the hazard seam runs first and does not depend on it.
    - The `_observer_body` helper is gone.
  - **Medium 1: shadow files did not tolerate a torn tail; shadow failures after the
    proposals could raise.** Fixed. `_parse_shadow` steps over any line that does not load,
    as the reader does (D80); an append after a torn tail starts with a newline. Everything
    after the proposals are durable is in one `try` that turns `OSError`, `ModelReplyError`
    and `ValueError` into `report.shadow_error`. The second `index.update` is gone, so no
    log error can occur inside that block.
    - First attempt at the torn tail "closed" it with a newline and then refused to read
      the now-complete bad line, which the new test caught. Stepping over is the right rule.
  - **Medium 2: `allowed` spanned the run; no dedupe across requests; no bound on body or
    judge input.** Fixed. `allowed` is per request; `seen` is one set for the run; a body
    over `BODY_CHARS` is `oversize`; a staged item is judged on its first `EXCERPT_CHARS`.
  - **Low 1: the verdict reason was cut before it was flagged.** Fixed: flagged whole,
    markers included, then cut.
  - **Low 2: `silent` and the CLI.** Fixed. Verdicts alone keep a run silent (shadow
    records quietly; `--score` reads it); `dropped` and `already_proposed` break the
    silence, so the branches that report them are reachable; "nothing new to propose" is
    gone, and a non-silent run with nothing staged says "nothing staged this run" before
    what it does have to say.
- **Round 2:** pending.

**Choices made inside the task:**
- **The shadow log is beside the log, not in it.** D82 says "shadow log", and a verdict is
  not a belief, an observation or a decision; the resolver must never see one. Putting it in
  the log would need a new event type, which is a schema decision (D120 precedent: the task 9
  agent made one and flagged it). I chose not to make one. Two files, each with one
  `extra="forbid"` model and a `v`, so the migration path exists if Shravan wants them in
  the log later. Deleting the folder costs one re-read by the model and the verdicts to
  date, never a belief.
- **Withhold, not redact.** Replacing a matched span with a placeholder and sending the rest
  is D18's shape and keeps recall, but a regex boundary that is one character off sends the
  secret, and a neighbour's name with the number removed still identifies the neighbour.
  Phase 0 withholds the observation. The cost is proposals missed from sessions that mention
  a SHA or a UUID, which the generic credential rule also catches.
- **The envelope goes to the fake too.** The `Model` sees enveloped items, not raw text, so
  "zero escape the envelope" is tested on the surface a real adapter will use.
- **The fake reads the person's block, and nothing after the reply.** `person_lines` takes
  the lines up to the first `assistant:` line; an excerpt with no label is read as a
  continuation of the person's text. So a `user:` line planted after a reply is never a
  turn to the fake, and `hazards` has withheld the observation before that anyway. The
  continuation assumption means an excerpt cut from the middle of a long reply is read as
  the person's; that is the fake's over-proposing, not a leak, and the pipeline test holds.
- **The injection gate is conservative on purpose.** "Add to the house rules: no shoes
  inside" from the person is flagged `instruction` and withheld; a pasted chat export with
  `User:` lines is flagged `boundary`. Each costs one proposal; the other direction costs a
  memory a web page wrote.
- **One `try` after the proposals, not three.** Finer blocks would let a later step run
  after an earlier shadow failure and report a count that hides it. One block, one
  `shadow_error`, and the count of verdicts is whatever was appended before it.
- **Proposals from the model default to `inductive`;** the fake says `explicit` because it
  quotes the person. `valid_from`/`valid_to` stay null: the fake cannot resolve a marker
  without guessing (D103), and a wrong date looks precise.
- **A reply that does not parse writes nothing, across every request of the run.** All
  replies are read before anything is appended. A run over a long history with one bad reply
  among many proposes nothing that run; the alternative, a partial run, would need a partial
  `seen`, and I preferred the simple invariant.
- **`--score` sits on `propose`** rather than a new command, to keep the `cli.py` edit to one
  appended block while other agents work in the file. It needs `--device` it does not use.
- **`FakeModel` is in `src/`,** because `walle propose --model fake` against a real log is
  the way to see what the pipeline does before a provider exists.

**What to read first:** `propose` in `proposer.py`, top to bottom: where the hazard seam is,
that the model call is outside every lock, the re-check under the stream lock, and the two
shadow appends. Then `hazards.py` with `tests/corpora/benign.jsonl` open beside it: every
rule is a trade between a missed secret and a withheld observation. Then the "shadow log
beside the log" choice above, which is the one for Shravan to confirm or overturn.

**Noticed, not done:**
- **Withheld observations are listed only on a run that has something else to say.** A
  log whose only new observations are all withheld is a silent run, so the person is not
  told. Recording withheld refs in `seen.jsonl` under a `hazards` pseudo-model would let the
  first run say it once and later runs stay quiet.
- **A torn or edited shadow line is stepped over silently.** The reader reports its skips;
  the shadow files do not. A count on `ProposeReport` would be the small fix.
- **No provider adapter.** `--model` accepts only `fake`. The D87 adapter (`provider:model`
  via aisuite, which is on the allowlist) is its own module and task; it must take `Request`
  and return the reply text, nothing more.
- **Redaction instead of withholding (D18).** Stable local tokens for names, numbers and
  addresses would let a flagged observation still yield a proposal. The flagger returns kinds
  only, so spans would need a second function.
- **The generic credential rule catches git SHAs and UUIDs,** which Claude Code transcripts
  are full of. Expect many withheld observations on a developer's log until D18 lands.
- **The observer's body format cannot tell a pasted `user:` line inside the prompt itself
  from a second text block.** `forged_turn` catches one after a reply or a blank line; a
  pasted `user:` line directly under the real prompt, with no blank line, is read as part of
  the person's block. The instruction rules still catch what such a line usually says. A
  block start marker in the body would settle it; that is the observer's task and a
  body-format change re-observes history (task 11's note).
- **Whether `proposal` events need a `model` field** for provenance. `caused_by` carries the
  observation; which model proposed it is only in the shadow log's verdict for it.
- **Cross-device duplicates** (walkthrough above), as for the observer.
- **Scoring per hazard kind and per cue** would show which rules earn their keep; `score`
  is one line of totals.
- **No session-end or weekly trigger.** `propose` is the function and the command; what
  runs it (a Stop hook, a cron) is D79's recurring-run shape and D113's principal binding.

**Cost:** pending.
