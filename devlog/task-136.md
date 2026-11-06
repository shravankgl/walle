# Task 136: Audit — Google connectors: wider scopes, lost items, withheld mail

Issue #136: the Gmail and Google Calendar findings of the read-only audit. Branch
`task/audit-google`, created empty by the owner as the claim, from `main` at `fe60cb3`.
Author: walle-author-fable (Fable 5.1). Every finding was written as a test first and run
against `fe60cb3` before any fix, against the fakes in `tests/fake_gmail.py` and
`tests/fake_google_calendar.py` only: no real account, token, mail or calendar was anywhere
near this. One finding is not fixed and is flagged (`CONTACT`, below); one belongs to #137 and
is recorded, not touched. `proposer.py` and `hazards.py` are unchanged. No D-number changes.

**Shipped:**

- `src/walle/google_oauth.py`, new: what the two connectors share about a token, and nothing
  else. `wider_than(allowed, reply)` is the one check that a token-endpoint reply grants no
  scope beyond the ones asked for; both `_token_from` functions run it before a `Token` is
  built, so a sign-in or a refresh that comes back wider raises `AuthError` and nothing is
  written. `scrubbed(text, secrets)` replaces every secret, folds to one line and cuts; both
  `exchange_code` functions pass Google's error text through it with the client secret, the
  code and the PKCE verifier (the low on PR #65). The module names no scope and imports no
  network; a test holds both.
- `src/walle/gmail.py`:
  - `authorization_url` sends `include_granted_scopes=false`; `_token_from` runs
    `wider_than(SCOPES, data)` (finding: wider scopes, HIGH).
  - The first line of a body carries the query it was asked with, `Gmail message <id> in
    thread <tid> (query: label:urgent)`, and only when a query was given, so every existing
    body and fixture is unchanged. `GmailSource.covers(body)` reads it back, and the base
    folds the cursor over bodies asked with the same query (finding: one cursor across
    `--query`, HIGH).
  - `_html_to_text` escapes every `<` that opens no HTML element, comment, doctype or
    processing instruction before the parser sees it, as `google_calendar._html_to_text`
    does. The allowlist is the living standard's elements plus the obsolete ones a mail
    client still writes and Outlook's one-letter namespaces (`o:p`); `untrusted:`, `system`,
    `instructions` and the rest of the gate's boundary names are not on it, so they reach the
    log as text (finding: dropped markers, MEDIUM).
  - `GmailClient.get` refuses anything that is not `https`, on `gmail.googleapis.com`, with no
    port, under `/gmail/v1/users/me/`, with no `.` or `..` segment, before any request is
    made. `oauth2.googleapis.com` is no longer a host a `GET` with the bearer token may go to;
    the token endpoint is reached only by the two `POST`s. `urllib_transport` turns an
    `OSError` (a read timing out after the headers) into a `GmailError`. `sync_command`
    reports a `CorruptStreamError`, a `FutureSchemaError` or an `OSError` with
    `reader.without_content` (finding: wire and command, MEDIUM).
- `src/walle/google_calendar.py`:
  - `_granted_too_much` is gone; `_token_from` runs the shared `wider_than`. `exchange_code`
    scrubs as above.
  - An event with no `updated` of its own (a bare cancellation, which takes the list's
    `updated`) is handed to the base with `borrowed_time=True`, and the base matches it by
    body alone (finding: duplicated cancellations, HIGH).
  - `CalendarSource.covers(body)` reads the calendar id off the first line, which already
    named it; the base folds the cursor per calendar (finding: second `--calendar`, HIGH).
  - `sync_command` reports log errors without content, as Gmail's does.
- `src/walle/connector.py`: two additions to the base, each a fold over the log and nothing
  else (D92).
  - `Scoped`, an optional protocol with `covers(body) -> bool`. When a source has it,
    `fold_cursor`/`cursor_of` take it as `within` and fold `max(observed_at)` over the
    bodies it covers. The content fold that stops a duplicate is not narrowed. A source
    without `covers` is folded whole, as before.
  - `Item.borrowed_time`, default `False`. An item with it set is "already logged" when
    `(source, body, source_version)` match a logged observation, whatever either
    `observed_at` says; appending one adds its body key too, so two in one pull are one.
    The log carries no flag: the source knows which of its items borrowed a time.
- `src/walle/reader.py`: `without_content(exc)`, the function `cli._without_content` was. It
  belongs beside `CorruptStreamError`, whose message it knows how to split. `cli.py` now
  delegates to it, so there is one copy, and `cli.py` no longer imports `ValidationError`.
- Tests. `tests/test_google_oauth.py`, new, 6 tests: the check on a grant as asked, a reply
  without a scope, the extra scopes named sorted and cut, `scrubbed`, both connectors calling
  the one check and neither keeping its own, and the module's imports. `tests/test_gmail.py`,
  10 tests and one parametrised set (28 cases). `tests/test_google_calendar.py`, 6 tests.
  `tests/test_connector.py`, 2 tests, with a `ScopedSource`. `tests/test_reader.py`, 1 test.
  `_Canned` in `test_gmail.py` gained a `/slow` path that sends headers and holds the body.
  No existing test was edited.

**Why:**

- **D92, the cursor is a fold.** Both "lost items" findings are the same bug: one source
  name, two asks, one cursor. The fix keeps the cursor a fold over the log with no side
  store; it narrows the fold to what the log already says about each item, the calendar on
  the first line and now the query too. D92's own words, "a connector that needs more than a
  cursor is asking for state the log should hold as events", are why the query goes into
  the body rather than anywhere else. The content fold is untouched, so the duplicate rule is
  exactly what it was.
- **D93, `observed_at` is the source's time.** A bare cancellation has none, and the borrowed
  one moves. Matching it by its time was the bug; the body, which says "Google gave no
  time", is the item's identity. The flag lives on the `Item`, never on the event, so the
  schema and the log are unchanged.
- **D55, mail text is untrusted.** A forged envelope marker in an HTML-only mail reached the
  log as `<<>>`. The gate sees what the model would, so what the parser drops must be what
  a reader never sees (comments), and what a reader sees must survive (an unknown tag,
  written out).
- **D77, the boundary has an eval.** The e-mail items of the injection corpus are now
  rendered through the HTML path as well as the plain one; all 28 are flagged.
- **D18** is why `CONTACT` is flagged and not fixed (below).

**Verify:** the issue's criterion: a failing test for each finding first; nothing is written
to the log on a refusal; fix the class.

Before (the new tests against `src/` at `fe60cb3`): 18 failed, and `test_google_oauth.py`
did not collect (`ImportError`). Each failure was the auditor's: `KeyError:
'include_granted_scopes'`; `DID NOT RAISE AuthError` at sign-in and at refresh; the client
secret, code and verifier in the exchange error, with a line break; `render_message` not
taking a query and the body's first line not naming it; the receipt mail never appended after
the urgent one; `<<>>` in the body where the marker was; `DID NOT RAISE ValueError` for the
other Google host, the port, the other path and the `..` segment; a bare `TimeoutError` from
the transport; the corrupt line's body in the command's output; the bare cancellation
appended a second time when the calendar moved on; the team calendar asked with the primary's
cursor; no `covers`; and no `without_content`.

Cases that did **not** fail, kept as regression guards and said so here:

- `test_every_email_injection_item_sent_as_html_only_is_flagged_too` (28 cases) passed at
  `fe60cb3`. The `mail-forged-envelope` item is also caught by `deface`, which sees the
  opening `<<<` run; it was the closing marker alone that the parser ate, which the dedicated
  test holds.
- The calendar's `test_a_refresh_granted_a_wider_scope_is_refused_and_neither_file_nor_log_is_written`
  passed at `fe60cb3`: the calendar already had the check. It is kept because the check moved.

After:

```
uv run pytest                4373 passed, 35 skipped   (main at fe60cb3: 4320 passed, 35 skipped)
uv run ruff check            All checks passed!
uv run ruff format --check   216 files already formatted
uv run pyright               0 errors, 0 warnings, 0 informations
```

Run as uid 1000, not root. "Nothing is written on a refusal" is asserted directly: after a
wider grant at refresh the token file's bytes are unchanged, the stream file does not exist,
and the fake saw no API request; after a wider grant at sign-in through the command, the
token file does not exist.

**Two-runner walkthrough:** `connector.sync`'s order is unchanged: fold the cursor (shared
lock), pull, then take the exclusive lock, fold content, append. Two things changed inside
it.

*The scoped cursor.* A syncs `--calendar primary` and B syncs `--calendar team` into one
stream at once. Each folds its own cursor in step 1, over its own calendar's bodies, and asks
Google accordingly. Under the lock, each folds the content of the *whole* source and appends
what is missing. Neither's items match the other's (the first line differs), so each appends
its own, in whichever order the lock is taken. The cursor A folds next time is over
`primary` bodies only, B's over `team` only; what the other appended does not move it.

*The borrowed time.* A and B both sync `primary`; the calendar holds one bare cancellation
`gone`. Both pull it with `observed_at = <calendar updated>`. A takes the lock, finds no
`(google-calendar, <body>, calendar-v3)` in the stream, appends, releases. B takes the lock,
reads the stream A just wrote, builds `timeless` from it, finds the body key, appends
nothing and reports it already logged. Had the calendar changed between A's pull and B's,
B's `observed_at` would differ from A's; the body key does not, so the outcome is the same.
Before this change B would have appended it again with the later time.

**Stranger pass:** the diff is about 200 lines of `src/` and touches IO, an error path and
the base's dedup, so the re-read was done.

- Found and fixed: the first cut of `GmailClient.get`'s refusal lost the word "host", which an
  existing test matches on. The message now says both the host and the path; no test was
  edited.
- Found and removed: a cross-connector check duplicated in `test_gmail.py`; the one in
  `test_google_oauth.py` is stricter (it reads the AST) and stays.
- Checked `_FIRST_LINE` in both modules: `\S+` for the ids means a body whose id is empty or
  has a space is simply not covered, which makes the cursor earlier, never later. The
  calendar's group is non-greedy to the first line break and compared against
  `_one_line(calendar_id)`, the same function `render_event` wrote it with, cut included.
- Checked the order of `except` clauses in both `sync_command`s: `FileNotFoundError` (an
  `OSError`) is caught first, for the token file, before the log-error clause.
- Checked `urllib_transport`: `URLError` is an `OSError`, and is caught first so its `reason`
  is still what the message carries.
- Checked `_NOT_A_TAG` against the gate's boundary names: `untrusted`, `system`, `user`,
  `instructions`, `memory`, `document`, `context`, `prompt`, `human`, `assistant`,
  `tool_result`, `function_results`: none is an HTML element, so each is escaped. `<!` and
  `<?` are left to the parser, which drops comments, doctypes and CDATA; what a reader never
  sees, the model never sees.
- Considered and left: a `GET` to `oauth2.googleapis.com` was allowed before and is not now.
  Nothing in `gmail.py` made one; the token endpoint is only ever `POST`ed to.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model, and it sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run
(R6 replaces it for agent work). There was no author-as-reviewer pass beyond the stranger pass
above.

**Choices made inside the task:**

- **The query is in the body, and a message under two queries is two observations.** The
  issue offered this or "forbid changing `--query` without a full resync flag". The flag
  cannot be checked without knowing the last query, and under D92 the log is the only place
  to know it from, so the query had to be in the body either way. Once it is, the content
  fold tells the two apart, which is right: the log says what was asked. The cost is a
  message logged twice when two queries both match it, with first lines that differ.
- **A consequence for a stream synced before this change with a non-empty `--query`:** its
  bodies have no `(query: ...)` suffix, so they are not covered by that query's next sync,
  which starts from nothing and logs the slice again under the new first line. With no query
  given, old and new bodies are identical and nothing changes. The owner has not signed in
  yet (the issue says sign-in is tested after this merges), so no stream is affected today.
- **`borrowed_time` on the `Item`, not a stable `observed_at`.** The alternative was to give a
  bare cancellation a fixed time (an epoch, or the cursor). Both are a time the source never
  said (D93), and the body already says Google gave none. The flag keeps the log honest and
  the fix in the base, where the key is.
- **`covers` is an optional protocol, not a `Source` method.** `test_connector.py`'s
  `ListSource` and any future provider with one ask per name keep working unchanged; the base
  reads the method only when it is there.
- **`without_content` moved to `reader.py` rather than being copied.** The connectors could
  not import it from `cli.py` (which imports every observer) and should not carry a third
  copy. The reader writes the message, so the reader knows which half of it quotes the line.
- **A broad HTML allowlist for mail, not the calendar's short one.** Google's editor writes a
  dozen tags; a mail client writes `<center>`, `<font>`, `<o:p>` and the whole standard. With
  the calendar's list, every Outlook mail would carry `<o:p>` in its text. The rule is still
  "escape what is not a known tag"; only the list is longer.
- **Comments are dropped, not kept as text.** The calendar keeps them. A mail's conditional
  comments (`<!--[if mso]>`) would put a second copy of the layout into every corporate
  mail's text, and a comment is something no reader is shown, so there is nothing for the
  gate to see.

**What to read first:** `connector.sync`, the five changed lines around `timeless` and
`within`. They are the base's one change in behaviour and every other fix leans on them.
Then `gmail._NOT_A_TAG`, because an allowlist is where a miss would hide.

**Not done here, and why:**

- **`CONTACT` withholds every Gmail observation and every Calendar event with an organizer
  or attendee address (HIGH).** Reproduced: `hazards.flag` on a plain rendered mail is
  `('contact',)`, and on an event with an organizer `('contact',)`. Not fixed. The two
  fixes the auditor offers each change a settled decision: pseudonymising addresses in
  `render_message` moves D18's boundary from egress into the connector and puts tokens into
  the log before the mapping stream that D18 names exists; separating header metadata from
  the scanned text changes `hazards`/`proposer`, which #137 owns, or the event schema. The
  issue says to `flag` in that case and not to weaken the hazard check, so the issue carries
  `needs-shravan` with this reason, and the detector is untouched. The corpus tests set
  `CONTACT` aside already, as they did before.
- **Long lines forging a `user:` turn after `excerpts` slicing, and `person_lines` reading
  connector text as the person's words (HIGH).** Reproduced: a 2,000-character description
  followed by `user: ...` yields an excerpt whose first line is the forged turn, and
  `person_lines` on a calendar body returns every line. The fix is in `proposer.excerpts` and
  `FakeModel.person_lines`, which #137 is changing with the instruction to give connector
  text an explicit label. Labelling the bodies here as well would collide with that change
  on the same lines of both connectors; left to #137 and noted so it is not lost.

**Noticed, not done:**

- `gmail.py` and `google_calendar.py` still duplicate about 400 lines of wire, token, retry
  and sign-in code. `google_oauth.py` takes the two pieces this task needed shared; the rest
  is its own task, as #65 said.
- The calendar's `_html_to_text` keeps comments and `_TextOf` emits two breaks for `<br/>`
  (the nit on #65). Neither changed here.
- `_FIRST_LINE` is read back from a body the connector wrote; a body from an older
  `render_message` without the query suffix is treated as "no query", which is what it was.
- A `Source` whose `covers` covers nothing (a typo in `--calendar`) is asked from an empty
  cursor and fetches the whole calendar; the content fold then appends only what is new.
  Correct, and slow once.

**Cost:** author (Fable 5.1, Hyperagent thread `cmw4fur5902uz07ad4ujedguz`): see Hyperagent's
usage for that thread. Reviewer: in its own log.
