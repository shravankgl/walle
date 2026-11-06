# Task 144: D18 — pseudonymise at the egress boundary

Issue #144, written by the owner on 2026-11-06 after #136 found every real Gmail message and
most Calendar events withheld as `contact` because of their address headers. Branch
`task/d18-pseudonyms`, created empty by the owner as the claim, from `main` at `0f9cb81`
(#140's excerpt fix is in). Author: walle-author-fable (Fable 5.1). The verify criteria were
written as tests first and run before the module existed: collection failed on the import, and
the connector body run through the pipeline as it was came back withheld as `contact`. No
D-number changes; nothing here needed `flag`. **Off by default** (the owner's dormant rule):
`WALLE_PSEUDONYMS=on` is the only thing that turns it on, and a test proves the default.

**Shipped:**

- `src/walle/pseudonyms.py`, new. Three things, one module, because they are one boundary:
  - `Pseudonymiser(root, principal, device)`: `pseudonymise(text)` replaces mailboxes on
    `From`/`To`/`Cc`/`Organizer`/`Attendees` lines (display name and address together), the
    `Message-ID` value, bare addresses, phone numbers, street addresses and account numbers
    with tokens `PERSON_`/`MSGID_`/`PHONE_`/`ADDRESS_`/`ACCOUNT_` + ten hex characters.
    `reidentify(text)` puts the values back. `shred(token)` appends a tombstone (D12).
    `leaks(text)` says whether any mapped value is in a string. Its `repr` names the
    principal and device and nothing else.
  - The mapping: `principals/p-<name>/pseudonyms/`, a `key` (32 random bytes, `0600`) and one
    `d-<device>.jsonl` per device (`0600`, folder `0700`), beside the log, never in it, per
    principal (D28, D32). Lines are `Entry` records, `v: 1`, `op: map|shred`, appended under
    the file's exclusive lock and never rewritten (D18). Resolution is a pure fold: a `shred`
    wins over every `map`; two `map` lines that disagree resolve to nothing. Torn tails are
    stepped over and closed by the next append (D80).
  - `Egress(model, layer)`, the door. `complete(request)` checks the exact `request.prompt`
    the adapter will send: every item is enveloped under this request's marker and sits
    verbatim in the prompt; every item's inner text passes `hazards.flag` in full; what is
    left of the prompt with the items and their ids removed passes `hazards.flag` in full;
    with the layer on, no mapped value appears anywhere in the prompt. Any finding raises
    `Refused(kinds)` before the inner model is called. The message names kinds, never text.
- `src/walle/proposer.py`: `propose` builds the layer from the switch (`environ`, as the
  workflow switch is read) and the door around the model, and **every model call goes through
  `door.complete`**: the propose requests and the shadow judge's. `_scanned` pseudonymises the
  body first when the layer is on, then scans whole and per excerpt as before, so the scan is
  of the text that would go out. A door refusal on a propose request withholds every
  observation in it (reported by ref and kind, not marked seen, nothing written); on a judge
  request it records a `drop` per item with the kinds as the reason. `_candidates` applies
  `reidentify` to a reply's summary and body *after* every check on the model's own text, then
  re-runs the length and one-line rules, so the staged proposal holds the address and the log
  stays re-identifiable locally. Module docstring gains step 0 and the door.
- `src/walle/hazards.py`: `_EMAIL`, `_PHONE`, `_STREET` become public `EMAIL_PATTERN`,
  `PHONE_PATTERNS`, `STREET_PATTERN`, unchanged in content, so the layer replaces exactly the
  shapes the gate flags. No detector changed; every corpus item flags or passes as before.
- `tests/test_pseudonyms.py`, new, 36 tests. `tests/fixtures/pseudonyms/`: a golden mapping
  written by this build (`key`, two device streams including a `shred`, `__expected__.json`),
  frozen, with a test that the current build re-identifies it. The key in it is the plainly
  fake `0123456789abcdef` ×4, so nothing in the repo looks like a secret.

**Why:**

- **D18.** Names, addresses, account and phone numbers become stable local tokens before
  anything reaches a frontier model; the mapping never leaves the machine; the log stays
  re-identifiable locally. The token is an HMAC of the normalised value under a per-principal
  key, so stability needs no coordination: the same address is the same token on every run,
  after a rebuild (neither the key nor the mapping is derived state, and even with the
  mapping files deleted the tokens do not move), and on two devices sharing the profile
  folder. `PERSON_A` means the same person across years of log.
- **D12.** Shredding is a resolution rule here: a `shred` line makes a token opaque on every
  device, and the value is never written to the mapping again. What is *left to build* is
  said plainly in the module docstring: the shredded `map` line is still on disk in plain
  text, as is every observation the value came from. Crypto-shredding proper (a per-entry key
  destroyed with the entry) and D12's TTL compaction of the observations are the two halves
  not here. The record shape and the fold are ready for both.
- **D28, D32.** The mapping is inside the principal's folder and read by nothing that serves
  another principal. Isolation by the filesystem, as the log's is.
- **D55, D77.** The door checks the enveloped text as sent, and the gate's detectors run on
  what would go out. A miss anywhere upstream is withheld, never sent: with the layer on, an
  address the layer does not replace is still `contact` to the scan; a value the layer *did*
  map that reappears in the final text by any route is `mapping` to the door.
- **D88, D96.** The mapping is not an event type and the log never holds a line of it, so no
  event golden log changes. It is a versioned record with its own golden fixture, the shape
  the shadow log set (`ShadowVerdict`, task 18), so a later `v` must still load `v: 1`.

**Verify:** the issue's six criteria, each a test in `tests/test_pseudonyms.py`:

| criterion | test |
|---|---|
| the exact text sent holds no address; the check runs on that text | `test_on_connector_text_reaches_the_model_as_tokens_and_comes_back_re_identified` (every prompt, every item: no value, no display name, `hazards.flag` clean), `test_the_door_refuses_a_final_text_that_still_carries_a_hazard` (the door, on `request.prompt`, items and head), `test_every_model_call_in_the_proposer_goes_through_the_door` (counted at run time and read from the source: the only `.complete(` caller in `proposer.py` is `door`) |
| a synthetic Gmail message and Calendar event reach the fake model with tokens, re-identified back | the first test above: headers, `Message-ID`, a phone, a street, an account number all tokens; `(you, accepted)` and `and 2 more` kept; two proposals staged with the address back and no token |
| same token across runs, a rebuild, two devices | `test_the_same_address_is_the_same_token_across_runs_a_rebuild_and_two_devices` (also: mapping files deleted, another root differs), `test_v1_mapping_lines_still_load` |
| off, withheld exactly as today; default is off | `test_the_layer_is_off_by_default`, `test_off_contact_details_stay_withheld_exactly_as_today` (nothing written, no folder created) |
| a detector miss fails closed | `test_a_layer_that_misses_an_address_leaves_the_gate_to_withhold_it`, `test_a_door_refusal_in_propose_withholds_the_request_and_sends_nothing_from_it`, `test_the_door_refuses_a_mapped_value_however_it_got_into_the_final_text` (an account number no detector knows) |
| the mapping is never in a model call, a report, a log line or the terminal | `test_the_mapping_is_never_in_a_model_call_a_report_a_log_line_or_the_terminal`: every prompt, `repr(report)`, every log line (no token, so no pair), the shadow files, the layer's `repr`, and `walle propose` through the CLI with the switch on |

Before: `ImportError: cannot import name 'pseudonyms'` on collection of the new test file; and
the same synthetic Gmail body run through `propose` at `0f9cb81` with `WALLE_PSEUDONYMS=on`
in `environ` came back `withheld=(Withheld('d-mbp:1', ('contact',)),)`, `requests=0`: the
switch meant nothing and the mail never left, which is #136's finding.

After:

```
uv run pytest                 4366 passed, 35 skipped   (4380 with main at fa6daf9 merged: #141 adds 14)
uv run ruff check             All checks passed!
uv run ruff format --check    217 files already formatted (219 with main merged)
uv run pyright                0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the module docstring carries it in full. In short, runs A and B
on one device meeting the same new address: (1) both compute the same token with no file
read, a pure function of the key and the value; if neither has a key, each writes one under a
random temporary name and `link`s it as `key`, the second link fails and that run reads the
first's, so no reader ever sees a partial key; (2) both read the mapping (own file under a
shared lock that waits, other devices' files under one that does not, as `proposer._read_other`
does) and find nothing; (3) A takes the exclusive lock, re-reads under it, appends, `fsync`s,
releases; (4) B takes the lock, re-reads, finds A's line, appends nothing. Two devices append
to their own files and never contend; a token mapped on one re-identifies on the other once the
folder has synced, and until then stays a token locally, which is a value withheld, never one
sent. The proposer's own locks are untouched: `pseudonymise` runs before any request with no
proposer lock held, and `reidentify` runs on replies before `writer.locked()` is taken. The
test `test_two_writers_on_one_device_leave_one_line_and_one_token` walks A and B through it.

**Stranger pass:** 556 lines of new module on an IO, lock and egress path, so it was done.
- **Found and fixed:** the door's first shape scanned the whole prompt with every detector
  minus turn and boundary. The secret detector flattens lines across excerpt boundaries, so a
  benign sentence naming a password in one excerpt and the next envelope's hex marker read as
  one secret: 30 benign corpus items withheld with the layer off. The door now scans each
  item's inner text and, separately, the prompt with the items removed, so every character is
  scanned without joining two items into one sentence.
- **Found and fixed:** that residue carried the excerpt ids, and a device name such as
  `mbp-2024-work-laptop-xyz` is a 20-plus character run of letters and digits: the credential
  detector would have refused every request for that device. Ids are refs this build made
  from validated names, never source text; they are removed from the residue too, and
  `test_an_excerpt_id_from_a_long_device_name_is_not_a_credential_to_the_door` holds it.
- **Found and fixed:** `_append` parsed the file twice; once now. `_load_key` let a
  `UnicodeDecodeError` out of a non-ASCII key file; it is now the same "not a key this build
  wrote" with the path and nothing of the content.
- **Checked:** no error message, `repr` or report field carries a value or a token-value
  pair. `Refused` carries kinds. `Withheld` carries ref and kinds. The CLI prints `Withheld`
  as before.
- **Checked:** a refused propose request is not counted in `requests`, its observations are
  not marked seen (so they are scanned again next run), and `candidates` from other requests
  still stage. A refused judge request records drops only for items without a verdict.
- **Checked:** with the layer off, `Egress` is still the only path and its checks are a
  superset of what the pipeline already passed, so nothing the pipeline sent before is
  refused now: the full suite, the three corpora and the gate are unchanged.
- **Considered and left:** `leaks` is a substring test, so a short mapped account number that
  happens to occur inside a longer number in another observation refuses that request. The
  cost is a withheld proposal; the other side is a value sent.
- **Considered and left:** `pseudonymise` writes to the mapping before the hazard scan, so an
  observation withheld for a credential still gets its addresses mapped. The mapping is local
  and the entry is a token for an address that exists; harmless, and simpler than mapping in
  two passes.
- **Considered and left:** the judge's verdict reasons may carry tokens (the model answers
  about `PERSON_…`). They are shadow lines nobody acts on, and a token without the mapping is
  opaque; re-identifying them would put addresses into a file that never needed them.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model, and it sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run (R6
replaces it for agent work). No author-as-reviewer pass beyond the stranger pass above.

**Choices made inside the task:**

- **A keyed hash, not a counter.** `PERSON_A`, `PERSON_B` in order of appearance would be
  stable on one device and nowhere else: two devices meeting two people in different orders
  would disagree, and a lost mapping would renumber everything. An HMAC under a key in the
  profile folder is the same on every device that has the profile, after any rebuild, and
  without the mapping file at all. A plain hash would do the same but could be reversed by
  anyone with a list of addresses to try; the key stops that. The token is `KIND_` plus ten
  hex characters, chosen so no hazard detector matches one (a test holds this for every kind).
- **The mapping stores the normalised address, and the first spelling of everything else.**
  A display name beside an address goes with it and does not come back: the header said the
  name, the address is the identity, and a name is what D18 most wants kept off the wire.
  `write to ALICE@…` re-identifies as the lowercase address. A phone comes back as first
  written.
- **One door, used with the layer off as well.** The issue asks that every model call go
  through it and that the check run on the exact final text. Making the door conditional on
  the switch would leave two paths to audit. Off, it changes nothing observable and a test
  reads the source to keep `door` the only caller of `.complete`.
- **Account numbers are the layer's, not the gate's.** Adding a detector to `hazards` would
  change the gate's behaviour on the corpora and the eval floors, which is a reviewed change
  of its own. Here an account number becomes a token with the layer on, and a value the layer
  has mapped is caught by the door if it reappears; with the layer off, nothing about account
  numbers changes.
- **Free-text person names are not replaced.** The issue allows a stdlib approach if one
  handles them safely; none does. "Thanks, Alice" versus "Thanks, Monday" needs a list or a
  model, and a model before the egress check is the thing the boundary exists to prevent. The
  module docstring says so in a paragraph of its own.
- **Withheld whole on a door refusal.** A refused request may carry several observations;
  all are withheld, as a flagged observation is withheld whole. Sending the others would mean
  rebuilding the request around a hole the door found.

**What to read first:** `pseudonyms.Egress.check`, then `Pseudonymiser.pseudonymise` and
`_append`. Then the three places `proposer.py` touches the door: `propose`'s request loop,
`_scanned`'s first `if`, and `_judge`'s `try`.

**Noticed, not done:**
- The Curator (`walle.curator`) calls `model.complete` on its own path and does not go
  through `Egress` or the layer. The issue scopes the proposer; the Curator is the next model
  call to put behind the same door.
- The hot tier and `recall` (`walle.hot`, `walle.mcp_server`) hand assertions to the host
  model. An assertion that came from a re-identified proposal holds an address; that egress
  is the host harness's and D18 applies there too. Not touched here.
- `hazards` still has no account-number detector, so with the layer off an account number in
  an observation is sent, as today.
- `shred` is a resolution rule, not a deletion (above). Crypto-shredding of the mapping line
  and D12's TTL compaction are the two halves to build.
- `_HEADER` also handles `Bcc` and `Reply-To`, which no connector writes today; harmless.
- `walle propose` does not say whether the layer was on for a run. The owner who set the
  switch knows; a line in the report would still help a later reader.

**Cost:** author (Fable 5.1, Hyperagent thread `cmw4jffid04cz07adfubga0m8`): see Hyperagent's
usage for that thread. Reviewer: in its own log.
