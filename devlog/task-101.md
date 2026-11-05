# Task 101: rung 3 and rung 4 protocols

Issue #101. Branch `task/p3-rungs`, created empty by the owner as the claim and reused here as
the owner's comment on the issue says; from `main` at `03f1745` (PR #111, matching). Phase 3
(R8). Dormant: nothing is asked, resolved or bounded unless the owner sets `WALLE_RUNGS=on`.

**Shipped:**
- `src/walle/rungs.py`, new. Rung 3 and rung 4 as protocols, on top of #99's `grants.Call`
  and `Scope`, #100's `matching.Rule` and `match`, the inbox's `Ask`, and the grants floor.
  - *Rung 3.* `ask(ledger, call, id=, session=, request=, ts=)` puts the exact `Call` into a
    `Pending` under its id and returns a new `Ledger`. `Pending.card()` names the command
    verbatim and the exact reply strings; `Pending.answers()` is `#id approve`, `#id always`,
    `#id no`, with `always` omitted for a destructive call (D116); `Pending.buttons()` makes
    `Button`s whose payload *is* their label (D62). `parse(text)` is one `fullmatch` of
    `#<id> <word>`: no fold, no trim, no second word. `resolve(ledger, reply, ts=)` returns
    `Approved(ledger, pending, scope)`, `Denied` or `Refused(kind, reason, ledger)`, and
    settles the item once: a second reply is `REPLAYED`; a reply more than fifteen minutes
    after the ask is `EXPIRED` and settles it as expired; a reply timed before the ask is
    `EARLY`; a `Button` whose payload differs from its label is `BUTTON`, refused before the
    payload is read; a second `ask` under a live or settled id with another command, session
    or request is `TAKEN`. `Approved.call` is the very `Call` object the person saw and
    `Approved.permits(call)` compares the call to run against it byte for byte, destructive
    flag included. `SCOPE_FOR` maps `approve` to `Scope.REQUEST` and `always` to
    `Scope.ALWAYS`, so an `Approved` goes to `grants.grant` as it is.
  - *The inbox.* `to_ask(pending, unattended=)` builds the inbox `Ask` (kind approval, the id
    as `tool_call_id`, the card as the body, timed at the ask, actor kernel) for the caller
    to hand to `walle.inbox.ask`. `reply_for(pending, outcome)` turns an inbox outcome back
    into the reply it stands for: approved is `#id approve`, denied is `#id no`, anything
    else is `None`. No inbox outcome is `always`.
  - *Rung 4.* `Standing.parse("tool: pattern*")` and `Envelope.parse(text)`: one rule per
    line, the pattern a `matching.Rule`, so a bare `*` and every shape the matcher refuses
    (separator, quote, executor, inline code, redirect, glob) cannot be a line, and one bad
    line is no envelope, with the line number in the error. The tool is a dotted identifier,
    so `*: …` is refused too. `load_envelope(root, principal)` reads
    `principals/p-<name>/grants/envelope.txt` — under the floor, human-edited, never written
    here; a link is not read; an unreadable file is reported by its error class. `bounded
    (envelope, call)` refuses off, a problem string, and any destructive call (D116), then
    runs the call's arguments through `matching.match` with the rules for its tool: split on
    every separator, every part justified (D74). It needs `WALLE_MATCHING=on` as well, and
    says so when it is not.
  - Reasons are fixed text. None echoes the reply, the id or the command.
- `tests/test_rungs.py`, 170 tests. Every id, command, session and timestamp is invented;
  nothing is run; every root is a `tmp_path`.

**Why:**
- D62: rung 3's answers are `#id approve` / `#id always` / `#id no`, matched exactly; an
  approval covers that command, once, within fifteen minutes; only the literal word `always`
  creates a standing permission; a button's payload is exactly the text it stands for. Rung
  4's envelope is `tool: pattern*` with a bare `*` rejected.
- D73: the two yeses are the first and last of the grantable scopes; what `Approved.scope`
  says is what `grants.grant` takes, so this module adds no fourth notion of "how long".
- D116: a destructive call is approved once, never always — the option is not drawn, and the
  word is refused if typed; and no envelope rule bounds a destructive call.
- D74: a compound command inside the envelope is split and each part justified, through the
  matcher as it is, not a copy of it.
- D72: the envelope lives under `grants/`, which the floors already protect, and nothing in
  this module writes a file.
- D117: install is not enable. Off by default, one spelling turns it on, and tests prove it.
- The owner's instructions for this task: approval text is the attack surface, so exact match
  only and a keen reply creates nothing; resolve the command once and execute that same
  string; test replay, expiry, a changed command under the same id, case and whitespace
  variants of `always`, and a button whose payload differs from its label.

**Verify:**
- *Only the literal word `always` makes a standing rule:*
  `test_only_the_literal_word_always_makes_a_standing_rule` (`#c-1 always` →
  `Scope.ALWAYS`), `test_a_reply_that_merely_sounds_keen_creates_nothing` (43 variants —
  `Always`, `ALWAYS`, trailing and leading space, two spaces, a tab, `\n`, `\r\n`, `!`, `.`,
  `always always`, `approve always`, `always, please`, an accent, a zero-width space, a
  Cyrillic `а`, a no-break space, `#c-1always`, `# c-1`, `#C-1`, no `#`, bare `always`,
  "yes, always!", `yes`, `ok`, `y`, `approved`, `Approve`, `nope`, empty — each refused,
  the ledger unchanged, the item still pending for the one exact reply),
  `test_parse_is_exact` (of the 43, only the two that are well-formed replies to *another*
  id parse at all, and they resolve to nothing), `test_parse_accepts_exactly_three_words`,
  `test_an_inbox_answer_can_approve_once_or_deny_and_never_always` (every `AnswerOutcome`),
  `test_always_is_not_offered_for_a_destructive_call_and_is_refused_if_said`,
  `test_an_approved_always_goes_into_grants_as_the_same_call` (end to end with #99: the rule
  covers the exact call in a later session and not a near miss).
- *A bare `*` is refused:* `test_a_bare_star_is_refused_as_a_pattern`,
  `test_a_bare_star_and_every_rule_the_matcher_refuses_cannot_be_an_envelope_line` (28
  lines: `*`, `**`, ` *`, `* `, empty, `*: …`, a detached or glued star inside a word,
  `sudo*`, `xargs rm*`, a pipe, `&&`, `bash -c*`, `python3 -c*`, `find . -exec*`, a quote,
  `$HOME`, a redirect, no colon, no space, two spaces, a leading space, a trailing tab — each
  refused alone and as line 2 of a file), `test_an_envelope_with_one_bad_line_is_no_envelope`.
- *A replayed or expired approval fails:* `test_a_replayed_approval_fails` (`approve`,
  `always` and `no` after an `approve`), `test_a_replayed_always_fails_too`,
  `test_an_approval_within_fifteen_minutes_stands_and_one_after_does_not` (at 0:00 and 15:00
  it stands; at 15:00.000001 and 16:00 it is expired),
  `test_an_expired_item_is_settled_and_stays_expired` (a later reply back inside the window
  is still refused; the id cannot be asked again), `test_a_reply_from_before_the_ask_is_refused`,
  `test_an_expired_item_does_not_take_the_others_with_it`, `test_the_window_is_fifteen_minutes_exactly`.
- *What is approved is what runs:* `test_what_is_approved_is_what_runs` (`Approved.call is
  pending.call`; a trailing space, a doubled space, a case change, an appended command, the
  destructive flag and another tool are each not permitted),
  `test_a_changed_command_under_the_same_id_is_refused` (the second ask is `TAKEN`, the
  first stands, approving the id approves the first),
  `test_a_settled_id_is_never_reused_even_for_another_command`,
  `test_the_same_id_from_another_session_or_request_is_refused`.
- *A button is its label:* `test_a_buttons_payload_is_exactly_its_label`,
  `test_a_button_whose_payload_differs_from_its_label_creates_nothing` (six pairs, including a
  trailing space and a case change in the payload).
- *Rung 4 is bounded:* `test_a_bounded_call_is_justified_part_by_part` (`git status && git
  diff --stat` passes under two rules; `git status && rm -rf /` is refused naming part 2 and
  not the text), `test_a_rule_for_one_tool_does_not_cover_another` (`Shell`, `sh`, a near
  miss, an empty envelope), `test_a_destructive_call_has_no_standing_yes`,
  `test_bounded_needs_matching_on_too`, `test_the_envelope_file_is_read_from_under_the_floor`
  (`floors.hit` on the path), `test_a_missing_envelope_is_empty_and_a_broken_one_is_a_problem`,
  `test_an_envelope_that_is_a_link_or_a_folder_is_not_read`, `test_nothing_in_the_module_writes_a_file`.
- *Dormant, default off:* `test_rungs_are_off_by_default` (switch unset and an empty
  mapping: `ask` raises `RungsOffError`, `resolve` refuses `OFF` with a pending item in hand,
  `bounded` refuses `OFF` with a matching envelope and the matcher on),
  `test_only_the_one_spelling_turns_rungs_on`, `test_while_off_nothing_is_parsed_or_matched`
  (`parse` and `matching.match` patched to fail if called),
  `test_the_module_starts_nothing_runs_nothing_and_reads_no_clock`.

Written first: with no `walle.rungs`, `tests/test_rungs.py` failed at import. After the module,
ten tests failed, each on my own test code, and each was a lesson about the matcher rather than
a bug in the module: `shell: git *` is a valid prefix rule in #100 (one space before the star is
allowed), so it left the refusal list; `Shell: git status` is a rule for another tool, not an
error, so it moved to the tool test; `~` is opaque to the matcher, so the destructive example
became `/tmp/allotment/old`; `Justified` carries `text` and `words`, not `part`; and the inbox
`Ask` needs `ts` and `actor`, now the ask time and `kernel`. Then three planted weakenings,
each reverted: folding and trimming the reply in `parse` failed 25 tests; removing the expiry
check failed 3; making the switch default to on failed 2.

```
uv run pytest               3825 passed, 35 skipped in 110.81s
uv run ruff check           All checks passed!
uv run ruff format --check  187 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the ledger is in memory and immutable: `ask` and `resolve` return
a new one, and two kernels hold two ledgers that share nothing. The durable form of an
unattended item is the inbox's, whose own walkthrough (`walle/inbox.py`) covers two responders;
this module only builds the `Ask` and reads the `Answer` back as a reply. The one file touched
is `grants/envelope.txt`, read and never written. Reader R and a person P editing it: R reads
whole text once; if P's editor has replaced the file, R sees either the old whole file or the
new whole file; if P's editor truncates and rewrites in place, R may see a prefix — whole lines
parse to a *narrower* envelope, which is safe, and a cut line parses to nothing at all (fail
closed), which is reported by its line number. No lock, because nothing here writes.

**Stranger pass:** done on the whole diff, since the module is one long error path. Found and
fixed:
- **Same id, same command, another session.** `ask` first treated "same id and same call,
  still pending" as the same item regardless of who asked. That would have let a reply in
  session B approve a call pending in session A, if the two shared an id. Now the item is the
  same only when the call, the session *and* the request are the same; anything else is
  `TAKEN`. A test holds it.
- **A tool name validated by building a throwaway `Call`.** `Standing` now checks the tool
  against `grants.TOOL_NAME_RE` and `TOOL_NAME_CHARS` directly, so the check reads as what it
  is.
- **Dead generality.** `_require` had grown a tuple-of-types branch nothing used; removed.

Looked for and not found: a path where `Approved.call` is anything but the `Pending`'s own
object (it is a property on the pending); a reason that echoes the reply or the command (every
reason is a literal, and the keen-reply test asserts the text is absent); a way for `always`
to come out of `reply_for` (the two branches return `approve` and `no`); a normalisation
before `fullmatch` (there is none; the planted `strip().lower()` failed 25 tests); an expiry
that reopens (an expired item is settled, and a settled item is never pending again).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run, because the brief replaces it for agent work.

**Choices made inside the task:**
- **D62 "once" and D73 "this request" are not in conflict here.** D73 refines D62: `approve`
  resolves the one ask once (this module), and the scope it hands to `grants.grant` is
  `request`, which #99 clears at the run boundary. The ask is used once; what the dispatcher
  then remembers for the rest of the request is #99's decision, not a second one made here.
  So no `flag`.
- **Exactly fifteen minutes stands; a microsecond more does not.** "Within fifteen minutes"
  read as `elapsed <= 15m`. The boundary is tested on both sides.
- **Expiry settles the item.** The alternative — leave it pending and refuse each late reply —
  would let a clock that went backwards approve it later. Settled as expired, with its own
  `How`, so the reason can still say "expired" rather than "replayed".
- **A trailing newline is not a reply.** Chat surfaces sometimes append one. The instruction
  is exact match only, and a surface that strips its own newline before sending is a surface
  doing its job; one that cannot gets a refusal that says what a reply looks like. If this is
  most of the fatigue, it is one decision to record, not a change to make here.
- **The envelope is `grants/envelope.txt`, `tool: pattern*` per line, D62's syntax verbatim.**
  D100's richer rule file (justification, `match`/`not_match` examples, `host_executable`) is
  its own task and will supersede this format; a plain-text list is what D62 names and what
  this issue asks for. Under `grants/` because the floor is already there (D72).
- **Rung 4 matches through `walle.matching` for every tool, not just a shell.** The
  alternative is a per-tool matcher this task has no decision for. The cost is that a
  non-shell tool whose arguments carry a quote or a `$` cannot be inside an envelope yet;
  that is fail closed, and D100 is where per-tool matching belongs.
- **`bounded` needs both switches.** It would be wrong for this module to turn the matcher
  on by passing its own environment; the owner turns each on. The refusal names the switch.
- **The `Ask` is built, not written.** `to_ask` returns the event and the caller hands it to
  `walle.inbox.ask`, which owns the lock and the switch. One writer per file.
- **Ids are not case-folded.** `#C-1` is a well-formed reply to an id that does not exist,
  and is `UNKNOWN`. Harness ids are case-sensitive (`walle.events.CallId`).

**What to read first:** the module docstring, then `parse` (five lines) and `resolve` (the
order of refusals is the protocol), then `ask`'s same-item check. In the tests, `KEEN` is the
list of replies that must create nothing, and `test_what_is_approved_is_what_runs`.

**Noticed, not done:**
- **Wiring.** Nothing calls `ask`, `resolve` or `bounded` yet. The dispatcher's order — exact
  grant (`grants.check`), then envelope (`bounded`), then ask the person (`ask`) — and where
  the ledger lives in the kernel are the wiring task.
- **D100's rule file** replaces `envelope.txt` when it lands: justification, load-time
  examples, `host_executable`, a `forbidden` decision that names the alternative.
- **The ledger is not in the log.** An attended ask that the kernel dies holding is gone with
  the process, which is the right default (nobody is waiting on a dead run), but a kernel
  that wants attended asks to survive a restart would route them through the inbox too.
- **`Refused.ledger` defaults to an empty ledger for rung 4 refusals**, where there is no
  ledger. A separate rung 4 refusal type would be cleaner; left as one type so a caller has
  one `Refused` to match on.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time. Hyperagent's figure for this thread is to
be filled in by the owner.
