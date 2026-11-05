# Task 57: live Google Calendar connector, read-only, on the connector base

Issue #57. Branch `task/p1-calendar`, from `main` at `2d0960e`. Phase 1's second live provider
(R8), built on `src/walle/connector.py` (task 54) to the pattern `src/walle/gmail.py` set (#61)
for sign-in, tokens, retries and the fake. Built against a local fake of the Calendar API only:
no real account, token or calendar was anywhere near this run, and nothing here can reach one.
The owner signs in and tests on his own Mac.

**Shipped:**
- `src/walle/google_calendar.py`, the connector. One module, five parts, laid out like
  `gmail.py` so the two read side by side:
  - *The wire.* `Request`, `Response`, a `Transport` protocol, and `urllib_transport`, the real
    one from the standard library: no redirects followed, plain `http` only to the loopback, and
    every network failure (a read timeout and a reset included) raised as a `CalendarError`.
  - *The token.* `Token` (Pydantic, `extra="forbid"`, `SecretStr` for every secret) and
    `TokenStore`, one private file (`0600` in a `0700` folder) replaced whole. The default path
    is `$XDG_CONFIG_HOME/walle/google-calendar-token.json`, apart from Gmail's.
    `sync_calendar` refuses a token path under the walle root.
  - *The client.* `CalendarClient.get` is the only verb. It sends the bearer token only to
    `https://www.googleapis.com/calendar/v3/...`: another host, another Google API on the same
    host, a `..` segment or the token endpoint are all refused before any request. Refresh before
    an expired token is used, refresh once on a `401`, then `AuthError`; `429`, a rate-limit
    `403` and `5xx` retried with `Retry-After` (at most 60 s) or `1, 2, 4, 8...` up to
    `max_attempts`, then `RateLimited` or `ApiError`. Error messages are scrubbed of every secret
    the client has held. A token-endpoint reply that grants more than `SCOPES` is refused, at
    sign-in and at refresh.
  - *The source.* `CalendarSource` implements `connector.Source`: `name = "google-calendar"`.
    `pull` asks `events.list` on one calendar (`primary` unless named) with
    `singleEvents=false`, `showDeleted=true`, and `updatedMin` from the cursor less an overlap,
    page by page. `render_event` is pure and exported: it turns one event into
    `(observed_at, body)`.
  - *Sign-in and the command.* `sign_in` is the installed-app flow on a loopback port with PKCE,
    `include_granted_scopes=false`. `python -m walle.google_calendar sign-in` and `... sync
    [--calendar ID]`, a small click group inside the module, like Gmail's.
- `tests/fake_google_calendar.py`, the fake: `events.list` with paging, `updatedMin`,
  `showDeleted` and `singleEvents` following Google's documented rules, `410
  updatedMinTooLongAgo` for a cursor it calls too old, deleted events that come back bare (only
  `id`), the token endpoint for refresh and code exchange, scripted rate limits, failures and a
  failing page, an expiring token, and an adversarial mode where error bodies quote the
  `Authorization` header back. It is also a witness: it records every request, answers anything
  but a `GET` with `405`, refuses `singleEvents=true`, and raises if asked for anything but
  `events.list`.
- `tests/test_google_calendar.py`, 74 test functions, 353 tests with the parametrised ones:
  the issue's verify criteria, the cases around them, the stdlib transport against a loopback
  `http.server`, sign-in with a stand-in browser that also asks for a favicon, the command, and
  every item of the injection corpus planted in each of five event fields.

No corpus, detector or eval-report file changed: the existing injection corpus, planted in event
fields, is caught by the existing detectors as they are (see *Verify*), so
`reports/2026-11-04-fake.md` is still what the code produces and `generate.py --check` is clean.
No existing file changed at all.

**Why:**
- *D92: the cursor is a fold over the log.* `observed_at` is the event's `updated`, the moment
  the calendar last changed it. The cursor, `max(observed_at)`, is then exactly what
  `updatedMin` asks for. Nothing is stored between runs but the log. That rules out Google's own
  `syncToken`, which is the right tool for incremental sync but is a side store by another name.
  When Google refuses an old `updatedMin` (`410 updatedMinTooLongAgo`), the source asks once for
  everything and the base's content fold appends only what is new. The base's docstring calls
  the cursor advice, and this is the case where the advice is refused.
- *D93: two clocks.* `updated` is the source's time for the item. When the event *happens* is
  world time and stays in the body as text: it would be an assertion's `valid_from` one day,
  never an observation's `observed_at`. An event edited after it was logged comes back with a
  new `updated`, so it is a second observation beside the first (append, never mutate). A
  cancellation is the same: the event's last state is "cancelled", logged beside what came
  before.
- *D55: event text is untrusted.* Anyone with the owner's address can put an event on the
  calendar: depending on the owner's settings, Google adds an invitation before it is accepted. So everything a stranger writes
  is treated as a stranger's: title, description, location, organizer and guest names. The
  body opens with the connector's own line. Every field but the description is folded onto one
  line, so none can open a turn or start the text block. An HTML description is reduced to
  text with hidden parts kept, and every `<` that does not open a real HTML tag is kept as
  text. Attachments and video-call links are counted by type and never followed.
- *Read-only.* `SCOPES` is `calendar.events.readonly`, the narrowest scope that reads events.
  `calendar.readonly` would also read calendar settings and the calendar list, and nothing here
  needs either.

**Verify:** each criterion is a test that failed before the module existed. The new file failed
at collection with `ModuleNotFoundError: No module named 'walle.google_calendar'`.
- *recurring events*: `test_a_recurring_series_is_one_item_with_its_rule_and_is_never_expanded`
  (one item with its `RRULE` and `EXDATE`, `singleEvents=false` on every request),
  `test_a_moved_instance_is_its_own_item_naming_its_series_and_original_time`,
  `test_a_cancelled_instance_says_which_occurrence_is_off`.
- *time zones*: `test_an_event_in_another_zone_keeps_its_own_time_and_gains_utc`,
  `test_a_time_without_an_offset_is_read_in_its_zone_across_a_daylight_saving_change` (London
  09:00 is 08:00 UTC in July and 09:00 in December),
  `test_a_floating_time_with_no_zone_of_its_own_takes_the_calendar_zone`,
  `test_ends_in_two_zones_are_each_named`,
  `test_a_floating_time_at_a_clock_change_gets_no_invented_utc_time`,
  `test_an_unknown_zone_is_said_plainly_and_does_not_crash_or_invent_a_utc_time`,
  `test_all_day_events_are_dates_and_the_exclusive_end_is_not_shown_as_a_day`,
  `test_an_updated_time_in_another_offset_is_spelled_the_log_way`,
  `test_a_cursor_in_another_offset_is_sent_as_the_same_moment_in_utc`.
- *cancelled events*: `test_a_cancelled_one_off_is_an_item_and_deleted_events_are_asked_for`,
  `test_a_cancelled_event_google_sent_without_an_updated_time_takes_the_calendar_change`,
  `test_cancelling_a_logged_event_appends_the_cancellation_beside_it`.
- *paging*: `test_paging_follows_every_page_token_until_there_is_none` (seven events, pages of
  three: `None`, `page-3`, `page-6`, and the same question on every page),
  `test_a_page_token_handed_back_twice_is_refused_rather_than_looped_on`.
- *an expired token*: `test_a_token_past_its_expiry_is_refreshed_before_the_first_call_and_saved`,
  `test_a_token_the_server_rejects_is_refreshed_once_and_the_call_retried`,
  `test_an_expiry_on_a_later_page_is_refreshed_and_the_page_retried`,
  `test_a_token_still_rejected_after_a_refresh_raises_and_appends_nothing`,
  `test_a_revoked_refresh_token_raises_an_auth_error_that_names_no_token`.
- *rate limits*: `test_a_rate_limit_is_waited_out_using_retry_after_or_a_backoff` (sleeps of
  `3, 2, 4`), `test_a_hostile_retry_after_cannot_park_a_sync` (a day becomes 60 s),
  `test_a_rate_limit_that_never_lifts_gives_up_and_appends_nothing`,
  `test_a_403_that_is_not_a_rate_limit_is_not_retried`,
  `test_a_transient_server_error_is_retried_and_then_given_up_on`.
- *event text is treated as untrusted*:
  `test_every_injection_item_planted_in_any_event_field_is_flagged`. Every one of the 56
  injection-corpus items, in each of title, description, location, organizer name and guest
  name, makes 280 cases. In each, the markers come through `render_event` intact and the
  proposer's gate flags the body, with `contact` set aside because the test's own addresses
  would trip it. Zero false-allows. Around it:
  `test_a_field_cannot_break_onto_a_second_line_or_open_a_turn`,
  `test_the_body_never_opens_with_a_turn_label_so_any_user_line_in_it_is_forged`,
  `test_an_html_description_is_reduced_to_text_with_hidden_text_kept_in_sight`,
  `test_a_forged_marker_or_a_comment_inside_html_is_text_and_reaches_the_gate`,
  `test_attachments_and_conference_links_are_noted_and_never_followed`.
- *a failed sync appends nothing partial*:
  `test_a_page_that_fails_after_others_succeeded_appends_nothing` (pages one and two succeed,
  page three fails on every retry, no stream file),
  `test_a_failed_sync_after_a_good_one_leaves_the_stream_byte_identical`,
  `test_a_live_event_without_an_updated_time_is_refused_and_nothing_is_appended`, and every
  token and rate-limit test above that ends `appends_nothing`.
- *no write scope is ever requested*:
  `test_the_one_scope_is_read_only_and_the_module_names_no_other` (every `googleapis.com/auth/`
  string in the source is the one read-only scope, and there is one),
  `test_sign_in_asks_for_the_read_only_scope_and_nothing_else`,
  `test_a_token_endpoint_granting_a_wider_scope_is_refused`,
  `test_every_api_request_is_a_get_to_events_list_and_the_only_posts_go_to_the_token_endpoint`,
  `test_the_client_has_no_verb_but_get`.
- *tokens outside the log and the repo*:
  `test_no_token_reaches_the_stream_and_nothing_but_the_stream_is_written_under_root`,
  `test_the_token_file_is_private_and_replaced_whole`,
  `test_a_bad_token_file_is_refused_by_field_name_never_by_value`,
  `test_sync_calendar_refuses_a_token_path_under_the_root`,
  `test_the_client_sends_the_token_to_the_calendar_api_and_nowhere_else`.

Checks, on the final tree (`uv run`, Python 3.12.16, as an unprivileged user, uid 1000):
- `uv run pytest`: 1770 passed, 10 skipped (1417 passed, 10 skipped on `main` at `2d0960e`;
  353 new).
- `uv run ruff check`: All checks passed!
- `uv run ruff format --check`: 107 files already formatted.
- `uv run pyright`: 0 errors, 0 warnings, 0 informations.

`main` moved to `4a3c0c5` (#63, the hot-tier resource) during the task, touching none of these
files. The four checks on that `main` with these four files added: 1790 passed, 10 skipped;
ruff clean; 111 files already formatted; pyright 0 errors.

The tests do not assume root. Every file is under `tmp_path`. The token file's `0600` and its
folder's `0700` are checked as the test user. The only sockets are loopback servers on port 0,
and one connect to `127.0.0.1:9` that is meant to be refused. No test reads the network or the
home folder. The only environment variables touched are `HOME` and `XDG_CONFIG_HOME`, set under
`tmp_path` to check the default token path.

**Two-runner walkthrough:** the connector touches the log only through `connector.sync`, whose
walkthrough (task 54) stands unchanged. The pull is outside the lock, everything is validated
before the lock, and the fold that decides what to append is the one under the lock. Two things
here are new:
1. *The token file*, exactly as in task 58. A and B both refresh an expired token. Google
   honours both, and each run writes its file with temp file, `fsync`, `os.replace`. One
   replaces the other whole, no file is torn, and both access tokens work until they expire.
   Neither run fails.
2. *The `410` fallback.* A and B fold the same cursor, and Google calls it too old for both.
   Each asks once for the full listing and gets the same events. A takes the lock, folds,
   appends the few that are new, and releases. B takes the lock, folds, finds them all, and
   appends nothing. A full listing is a bigger pull, not a different write.

**Stranger pass:** done; the diff is over 2800 lines and touches IO, sockets, an error path on
every call, and a file with a secret in it. Found and fixed:
- *A forged marker vanished inside HTML.* The first "is this HTML?" check took anything shaped
  like a tag, so a description holding `<<<untrusted:...>>>` was parsed as HTML. The parser
  then dropped the marker as an unknown tag, and the gate never saw the forgery. The
  280-case corpus test caught it, on `forged-close` in the description. Now only real HTML tags
  make a description HTML, and every other `<` is escaped before parsing so it survives as
  text. An HTML comment is kept as text for the same reason: hidden text stays in sight.
  Regression: `test_a_forged_marker_or_a_comment_inside_html_is_text_and_reaches_the_gate`.
- *A confident wrong UTC time at a clock change.* A floating 01:30 on 2026-11-01 in New York
  happens twice. The first version picked the earlier one, and showed a one-hour event as two
  hours in UTC. Now a time that is ambiguous or nonexistent in its zone gets no UTC line, and
  its label says why: `test_a_floating_time_at_a_clock_change_gets_no_invented_utc_time`.
  Google sends an offset with almost every time, so this only bites floating times, but the
  line would have lied.
- *An empty `When:` line.* An event whose `start` held neither a date nor a time printed
  `When: ` with nothing after it. Now there is no line: `test_an_event_with_no_usable_start_has_no_when_line`.
- *A negative overlap was accepted.* It would move `updatedMin` past the cursor and skip
  changes silently. Now it is refused: `test_a_negative_overlap_or_a_bad_page_size_is_refused`.
- *The transport let a read timeout escape.* A timeout or a reset mid-reply is an `OSError`,
  not a `URLError`, and would have reached the command as a traceback. Now every network
  failure is a `CalendarError`: `test_an_unreachable_host_is_a_calendar_error_not_a_traceback`.
  `gmail.py` has the same gap (see *Noticed*).
- Checked and left: `TokenStore.load` reports a bad file by field name, never by value. Error
  bodies pass through `_scrub`, and the fake's echo mode proves it. `Retry-After` and the
  backoff are capped at 60 s, and a repeated page token is refused, so neither a hostile header
  nor a looping API can park a sync. A zone name is the sender's text, so it goes to `zoneinfo`,
  which refuses path-shaped keys, and every lookup error is caught as "not known here". Every
  socket and file handle is in a `with` or a `finally`. The only broad `except` is the cleanup in
  `TokenStore.save`, which re-raises. The command prints `appended N, already logged M; cursor X
  over K observations`, and each number is what it says.

**Review:** none before the PR. Under R6/R8 the reviewer agent reviews this PR's head and sets
`agent-review`; the author never sets it. The author did not run `scripts/review.sh`.

**Choices made inside the task:**
- **Not expanding recurring events (`singleEvents=false`).** Expanding would write one
  observation per occurrence. A weekly lesson with no end date means hundreds of copies of one
  fact, all sharing one `updated`. Instead the series is one event carrying its rule, and each
  moved or cancelled occurrence is its own event naming the series and the time it replaces.
  That is "one observation per event" read literally. The cost: a reader who wants "what is on
  next Tuesday" has to apply the rule. That is a reader's job, not the log's.
- **`observed_at` is `updated`, not the start time.** Explained under *Why*. The start is in the
  body, in the calendar's own words and in UTC.
- **A deleted event with no `updated` takes the list's `updated`.** Google promises only `id`
  on a deleted event. The calendar's own last-change time is the source's clock at or after the
  deletion, and the body says it was used. The cost: the list's `updated` moves whenever
  anything on the calendar changes. So such an event re-listed inside the overlap window can be
  logged twice, the same cancellation with two times. Once the cursor passes it, it stops. A
  live event with no `updated` is a broken reply, and that sync is refused whole.
- **`source = "google-calendar"`, not `"calendar"`.** For Gmail's reason: the cursor is folded
  by this name forever, and another calendar provider should not share it. The module is
  `google_calendar.py`, not `calendar.py`, so it never sits beside the standard library's
  `calendar` under one name.
- **`calendar.events.readonly`, and a separate sign-in from Gmail.** One grant per connector,
  each the narrowest that works. Revoking one does not break the other, and neither token can
  do the other's job. The cost: the owner signs in twice.
- **A wider grant is refused, not trimmed.** If Google's reply lists a scope beyond `SCOPES`,
  the token is not kept, and the message tells the owner to remove walle's access and sign in
  again. A token that could write, sitting in a file, is the thing the issue forbids, whoever
  asked for it.
- **One calendar per sync, `primary` by default.** Listing the owner's calendars needs
  `calendarList`, which needs a wider scope. Another calendar is `--calendar ID`, but every
  calendar shares the `google-calendar` cursor. That is fine for one calendar. With two, it is
  not safe: a sync of calendar B asks from A's cursor and can miss B's changes older than it.
  So use one calendar per principal until *Noticed* is settled.
- **`showDeleted=true` from the first sync.** Without it, a series' cancelled occurrences are
  invisible, and the series reads as if every week happens. The cost: the first sync also logs
  old cancellations.
- **Self-contained, not importing from `gmail.py`.** The wire, the token, the client and the
  sign-in are close copies of Gmail's. Importing them would have meant either private names
  across modules, or messages that say "Gmail" in a calendar error, which lies. Extracting a
  shared module means changing `gmail.py`, which is another task. Recorded in *Noticed*, first.
- **No `walle sync` in `cli.py`.** Both connectors have now landed, or will when this merges,
  but wiring them is a change to the shared command, and to Gmail's surface. It is its own task.

**What to read first:** `render_event` and `_when`/`_point`, which decide what an event becomes
and are where a wrong time would come from. Then `CalendarSource.pull`, which holds the cursor,
the `410` fallback and paging in thirty lines. Then
`test_every_injection_item_planted_in_any_event_field_is_flagged` and
`test_every_api_request_is_a_get_to_events_list_and_the_only_posts_go_to_the_token_endpoint`,
the two promises the issue cares most about, held by the fake as a witness.

**Noticed, not done:**
- **`gmail.py` and `google_calendar.py` share roughly 400 lines** of wire, token, client and
  sign-in, nearly line for line. A `walle/google_oauth.py` that both import, with the provider
  name as a parameter, would make the next fix one fix. It touches `gmail.py` and its tests, so
  it is its own task.
- **Two fixes made here belong in `gmail.py` too:** `urllib_transport` lets a read timeout
  escape as a raw `OSError`, and the sign-in neither sends `include_granted_scopes=false` nor
  refuses a wider grant.
- **Every calendar observation will be withheld from the proposer today,** for the reason task
  58 gave for mail. `hazards.CONTACT` flags any address, and almost every event names an
  organizer by address. Until D18's pseudonymisation, or a contact rule that knows the
  connector's own `Organizer:`/`Attendees:` lines, the loop from calendar to memory is not
  closed. Same decision as for Gmail; one answer covers both.
- **Calendar-shaped corpus items.** The existing injection corpus planted in event fields is
  caught everywhere (280 cases), so nothing was added. Items written as invitations would make
  the eval report speak about calendars by name. That is a corpus change and a report
  regeneration, for Shravan to want or not.
- **A cursor per calendar.** Syncing two calendars shares one cursor, as *Choices* says. A
  `source` of `google-calendar:<id>` would give each its own; it is a naming decision that
  lasts forever, so it is not made here.
- **`source_id` on `Observation`** (task 54's note, again). Keyed by event id, an edited event
  would supersede its earlier observation instead of sitting beside it. Schema bump; needs
  Shravan.
- **`walle sync` in `cli.py`,** for both connectors.
- The OAuth client in the owner's Cloud project must be of the *Desktop* type, for any loopback
  port, and needs the Calendar API enabled. Worth one line in a README when he sets it up.

**Cost:** not visible to the author agent from inside the run. Shravan can read it from
Hyperagent.
