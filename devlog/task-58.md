# Task 58: live Gmail connector, read-only, on the connector base

Issue #58. Branch `task/p1-gmail`, from `main` at `2933a5f` (the connector base, #54). Phase 1's
first live provider (R8). Built against a local fake of the Gmail API only: no real account, token
or mail was anywhere near this run, and nothing here can reach one. The owner signs in and tests
on his own Mac.

**Shipped:**
- `src/walle/gmail.py`, the connector. One module, five parts:
  - *The wire.* `Request`, `Response`, a `Transport` protocol (a callable from one to the other),
    and `urllib_transport`, the real one from the standard library. It follows no redirect, so a
    token is never carried to a host Google did not name, and sends plain `http` only to the
    loopback (the sign-in redirect needs that). `GmailClient` refuses any URL whose host is not
    `gmail.googleapis.com` or `oauth2.googleapis.com` before a request is made.
  - *The token.* `Token`, a Pydantic model with `extra="forbid"` and `SecretStr` for every
    secret (so `repr` and `str` show none), and `TokenStore`, one private file (`0600`, in a
    `0700` folder) replaced whole. `default_token_path()` is `$XDG_CONFIG_HOME/walle/gmail-token.json`
    or `~/.config/walle/gmail-token.json`. `sync_gmail` refuses a token path under the walle root.
  - *The client.* `GmailClient.get` is the only verb: `GET`, with the bearer token. An access
    token past its expiry is refreshed before the first call; one Gmail rejects with `401` is
    refreshed once and the call retried; a second `401` is an `AuthError`. `429`, a `403` whose
    reason is a rate limit, and `5xx` are retried with `Retry-After` or `1, 2, 4, 8...` seconds,
    up to `max_attempts` in all, then `RateLimited` or `ApiError`. `sleep` and `now` are
    injectable, so the tests show the backoff without waiting for it. Every error message is
    scrubbed of every secret the client has held.
  - *The source.* `GmailSource` implements `connector.Source`: `name = "gmail"`, and `pull`
    turns the cursor into Gmail's `after:` term (less an overlap), lists threads page by page,
    fetches each in full, and renders each message to an `Item`. `render_message` is pure and
    exported: headers that matter on one line each, the message's own text with quoted earlier
    messages removed, attachments noted by type and size and never fetched, a cut at `MAX_TEXT`.
  - *Sign-in and the command.* `sign_in` is the installed-app flow on a loopback port with PKCE:
    open the consent page, take one redirect, check `state`, exchange the code. `python -m
    walle.gmail sign-in` and `python -m walle.gmail sync` are a small click group inside the
    module. The `walle` command gains nothing here, on purpose (see *Choices*).
- `tests/fake_gmail.py`, the fake: `users.threads.list` with paging and `q`, `users.threads.get`
  with `format=full`, the token endpoint for refresh and code exchange, scripted rate limits and
  failures, an expiring token, and an adversarial mode where error bodies quote the
  `Authorization` header back. It is also a witness: it records every request, answers anything
  but a `GET` on the API with `405`, and raises if asked for an attachment.
- `tests/test_gmail.py`, 79 tests: the issue's verify criteria, the cases around them, the
  stdlib transport against a loopback `http.server`, sign-in with a stand-in browser, the command,
  and every e-mail item of the injection corpus rendered as the connector renders a message.
- `tests/corpora/generate.py` and the regenerated `tests/corpora/injection.jsonl`: seven e-mail
  templates, 28 items (`mail-subject-instruction`, `mail-sender-name`, `mail-quoted-reply`,
  `mail-hidden-html`, `mail-false-approval`, `mail-forged-envelope`, `mail-forged-turn`).
- `tests/test_hazards.py`: the `ALONE` map says which detector must catch each new template on
  its own, and the recorded list of payloads caught by their frame gains the four e-mail items
  whose payloads already sat on it. This is a test edited to pass, and the task requires it: the
  test's own docstring says a change to that list is a change someone chose, and this is it.

**Why:** D92 and D93 are the base's, and the connector inherits them: nothing is stored between
runs but the log, the cursor is `max(observed_at)` folded by `source`, and `observed_at` is
Gmail's `internalDate`, the moment Gmail received the message, in the log's one spelling. The
`Date` header is kept as text because a reader wants it and not used as time because the sender
wrote it. Gmail lists a *thread* when any message in it matches `after:`, then hands back every
message in the thread, so a long conversation re-arrives on every sync; the base's content fold
appends only the new replies, and a test holds that a re-run with the overlap is byte-identical.

D55 and D77 are why the second half of this task is a corpus. Every byte a mail contains was
written by someone who is not the principal, and some of it was written for the model. The
connector treats it that way: the body it produces opens with its own line, never a turn label,
so any `user:` line in a mail is a forged turn to `walle.hazards`; header values are folded onto
one line, so none can end the header block or open a turn; HTML is reduced to text, so a
`display:none` block is as visible as the rest. The seven corpus shapes are the routes one mail
offers for a planted instruction, and each is caught by an existing detector alone, with no
change to `hazards.py`.

The scope is the issue's hard line, and it is held three ways: `SCOPES` is the `gmail.readonly`
scope and nothing else; the module's source names no other scope and contains the string
`googleapis.com/auth/` exactly once; and after a sign-in, a refresh and a full sync against the
fake, every request to the API was a `GET` and every `POST` went to the token endpoint. There is
no code path that could send, modify, label, trash or draft a mail, because nothing here knows
those endpoints exist.

No Google client library: `tests/test_constraints.py` allows four runtime dependencies (D87,
constraint 7) and Google's is not one. The connector needs three request shapes and a refresh,
which `urllib` covers; the `Transport` seam is what lets the whole connector run against the fake
with nothing special-cased for tests.

**Verify:** the issue's criteria, each a test that failed before the module existed (the file
failed at collection with `No module named 'walle.gmail'`; the corpus tests failed on an empty
template set).
- *threads*: `test_a_thread_becomes_one_item_per_message_in_the_source_clock_order` (a
  three-message thread and a single mail become four items, oldest first, each thread fetched
  once with `format=full`).
- *quoted replies*: `test_a_quoted_reply_is_dropped_and_the_new_words_are_kept`,
  `test_bottom_posted_text_after_a_quote_survives`,
  `test_an_attribution_line_that_wraps_is_still_a_quote_header`,
  `test_an_outlook_style_quote_is_cut_from_its_separator`.
- *headers*: `test_the_headers_that_matter_are_kept_and_the_routing_headers_are_not`,
  `test_a_header_cannot_break_onto_a_second_line_or_open_a_turn`,
  `test_the_body_never_opens_with_a_turn_label_so_any_user_line_in_it_is_forged`.
- *paging*: `test_paging_follows_every_page_token_until_there_is_none` (seven threads, pages of
  three, tokens `None`, `page-3`, `page-6`), and
  `test_a_page_that_fails_after_others_succeeded_appends_nothing`.
- *an expired token*: `test_a_token_past_its_expiry_is_refreshed_before_the_first_call_and_saved`,
  `test_a_token_the_server_rejects_is_refreshed_once_and_the_call_retried`,
  `test_a_token_still_rejected_after_a_refresh_raises_and_appends_nothing`,
  `test_a_revoked_refresh_token_raises_an_auth_error_that_names_no_token`.
- *rate limits*: `test_a_rate_limit_is_waited_out_using_retry_after_or_a_backoff` (sleeps of
  `3, 2, 4`), `test_a_rate_limit_that_never_lifts_gives_up_and_appends_nothing` (five attempts,
  four sleeps, nothing appended), `test_a_403_that_is_not_a_rate_limit_is_not_retried`.
- *attachments are never read*: `test_attachments_are_noted_and_never_fetched` (a PDF, an image,
  an attached `.txt`: noted by type and size, no id, no filename, no request naming
  `attachments`) and `test_an_inline_attachment_body_with_data_is_not_read_either`.
- *injection corpus extended, zero false-allows*: the 28 new items run through
  `test_hazards.py` (each caught by its own detector alone), `test_proposer.py`'s
  `test_the_corpora_through_the_whole_pipeline` (every item withheld before the model, no marker
  in any proposal), and `test_gmail.py`'s
  `test_every_email_injection_item_rendered_as_the_connector_renders_it_is_flagged` (each item,
  as a real message through `render_message`, is flagged, or, for the quoted-reply shape,
  removed before it reaches the log).
- *no send or modify scope is ever requested*:
  `test_the_one_scope_is_read_only_and_the_module_names_no_other`,
  `test_sign_in_asks_for_the_read_only_scope_and_nothing_else`,
  `test_every_api_request_is_a_get_and_the_only_posts_go_to_the_token_endpoint`.
- *tokens outside the log and the repo*:
  `test_no_token_reaches_the_stream_and_nothing_but_the_stream_is_written_under_root`,
  `test_the_token_file_is_private_and_replaced_whole`,
  `test_sync_gmail_refuses_a_token_path_under_the_root`,
  `test_a_401_that_echoes_the_token_does_not_leak_it_through_an_error`.

Checks, on the final tree (`uv run`, Python 3.12, as an unprivileged user, uid 1000):
- `uv run pytest`: 1336 passed, 10 skipped (1229 passed, 10 skipped on `main`; 79 new in
  `test_gmail.py`, 28 new corpus items through the existing parametrised tests).
- `uv run ruff check`: All checks passed!
- `uv run ruff format --check`: 89 files already formatted.
- `uv run pyright`: 0 errors, 0 warnings, 0 informations.

The tests do not assume root: every file is under `tmp_path`, the token file's `0600` and its
folder's `0700` are checked as the test user, and the only sockets are loopback servers on port
0. No test reads the network, the home folder or an environment variable except the one that
sets `HOME` and `XDG_CONFIG_HOME` under `tmp_path` to check the default token path.

**Two-runner walkthrough:** the connector touches the log only through `connector.sync`, whose
walkthrough (task 54) stands unchanged: the pull is outside the lock, everything is validated
before the lock, the fold that decides what to append is the one under the lock. What this task
adds is the token file, and two runs can refresh at once:

1. A and B both start with the same expired access token. Both `POST` a refresh with the same
   refresh token. Google honours both (a refresh token is reusable); A gets access token A', B
   gets B'. Both are valid.
2. A writes its token file (temp file, `fsync`, `os.replace`); B writes its own. One replaces
   the other whole; there is no torn file, because `os.replace` is atomic. Whichever lands last
   wins, and both A' and B' work until they expire, so neither run fails.
3. Each run then syncs under the stream's lock as the base describes. The log sees two syncs
   serialised; the second appends nothing.

A refresh token that Google rotates (it does not, by default, for installed apps, but the code
handles a reply that carries one) is the one case where last-writer-wins could keep the older of
two: both replies carry a new refresh token, and the file keeps one of them. Google keeps the
earlier one valid too, so the next run still refreshes. Recorded, not solved; a lock on the token
file is the fix if it ever bites.

**Stranger pass:** done; the diff is large and touches IO, sockets, an error path on every call
and a file with a secret in it. Found and fixed:
- `sign_in` handled exactly one request. A real browser asks for `/favicon.ico` as well, and had
  that arrived first the flow would have reported a timeout with nobody timed out. Now the
  handler answers anything without `code` or `error` with `404` and keeps waiting until the
  deadline; `test_sign_in_is_not_fooled_by_a_favicon_request_before_the_redirect`.
- `get` refreshed proactively for an expired token and would then refresh *again* on a `401` in
  the same call. The docstring promised one refresh per call; now it is one, whichever way it
  was triggered; `test_a_token_refreshed_for_expiry_and_then_refused_is_not_refreshed_again`.
- `Token.expired` crashed on an `expires_at` it could not parse. Unknown is now expired: one
  refresh, never a traceback; `test_an_unreadable_expiry_counts_as_expired_rather_than_crashing`.
- `exchange_code` built a placeholder `Token` with `refresh_token="pending"` to reuse a helper. A
  reader would stop on that word. The helper now takes the parts it needs.
- `urllib_transport` read an `HTTPError` without closing it. Now in a `with`.
- Checked and left: `TokenStore.load` reports a bad file by *field names only*, never by the
  Pydantic message, because that message quotes the input and the input is the secret. Error
  messages built from API bodies pass through `_scrub`, and the fake's adversarial mode (error
  bodies echo the `Authorization` header) proves it. The `Retry-After` value is capped at 60
  seconds and the backoff at 60, so a hostile header cannot park a sync. `_thread_ids` refuses
  a page token it has already seen, so a looping API cannot pull forever. Every socket and file
  handle is in a `with` or a `finally`. Nothing is swallowed: the only broad `except` is the
  cleanup in `TokenStore.save`, which re-raises.
- Status messages: the command prints `appended N, already logged M; cursor X over K
  observations`, and each number is what it says. `SyncReport.after` is the fold at the moment
  the lock went (task 54's wording), not a promise about now.

**Review:** none before the PR. Under R6/R8 the reviewer agent reviews this PR's head and sets
`agent-review`; the author never sets it. The author did not run `scripts/review.sh`.

**Choices made inside the task:**
- **`source = "gmail"`, not `"mail"`.** The base's docstring says `mail` as an example. The
  cursor is folded by this name forever, and a second mail provider with its own ids, clocks and
  quoting rules should not share a cursor with this one. Changing it later is a new source with a
  fresh cursor and a one-time overlap, not a migration, so the cheaper name was chosen now.
- **No `walle sync` subcommand in `cli.py`.** `cli.py` is shared with #57 (calendar), which may be
  built at the same time; two agents editing the same import block and command list is a merge
  conflict for Shravan to untangle. The command lives inside the module as `python -m
  walle.gmail` and is one line to wire into `walle` once both connectors have landed.
- **Quoted replies are removed, not kept.** A thread of ten replies would otherwise log the
  first mail ten times, once per quote, and every planted instruction in a quote would be
  re-observed on every reply. The cost: a reply that comments inline between `>` lines loses the
  lines it was answering. Bottom-posted text survives. The quoted-reply corpus item shows the
  other side of this: the planted order in the quote never reaches the log at all.
- **Attachments are noted by type and size only.** Not the filename: a filename is text the
  sender chose, and `trip-form.pdf` carries nothing a reader needs that `application/pdf, 48213
  bytes` does not. Not the attachment id: it is a handle to the one thing this connector must
  never fetch, and nothing downstream should have it.
- **Headers kept: From, To, Cc, Subject, Date, Message-ID.** `Received`, `Delivered-To` and the
  `X-` headers are routing and software, not what the mail said. `Message-ID` is kept because it
  is the stable identity a later `source_id` (task 54's *Noticed*) would be built from.
- **An overlap of one hour on `after:`.** `internalDate` is Gmail's own clock, so late arrivals
  are rare; the overlap covers clock skew between Gmail's search index and `internalDate`, and
  the base throws away what it already holds. Configurable.
- **`MAX_TEXT = 16000` characters.** A newsletter can run to hundreds of kilobytes of text; the
  log should not. The cut is noted in the body.
- **Plain `urllib`, a `Transport` seam, no `requests`.** Explained under *Why*. The seam is also
  why the fake can be a witness: it sees exactly what Google would see.
- **The token file holds the client secret too.** Google's installed-app flow needs it on every
  refresh, and two files for one sign-in is one more thing to keep outside the repo. The file is
  `0600`; the client secret of an installed app is, in Google's own words, not a secret.
- **PKCE and `prompt=consent`.** PKCE because the redirect is on a loopback anyone on the
  machine could race; `consent` because without it Google omits the refresh token on a second
  sign-in and the owner would be left with a token that works for an hour.

**What to read first:** `render_message` and `_strip_quotes`, which decide what a mail becomes;
then `GmailClient.get`, which is the whole retry and refresh story in one loop; then
`test_every_api_request_is_a_get_and_the_only_posts_go_to_the_token_endpoint` and
`test_attachments_are_noted_and_never_fetched`, which are the two promises the issue cares most
about, held by the fake as a witness.

**Noticed, not done:**
- **Every mail observation is withheld from the proposer today.** `hazards.CONTACT` flags any
  e-mail address, and every rendered message has one in `From:`. So the proposer, which withholds
  anything flagged, will propose nothing from Gmail until D18's pseudonymisation (or a narrower
  contact rule for the connector's own header block) exists. The connector is correct and the
  log is right; the loop from mail to memory is not closed yet. This is the first thing Shravan
  should decide on after this lands.
- A `source_id` on `Observation` (task 54's note) would let a message be keyed by `Message-ID`
  instead of by content, so an edited draft or a re-rendered body supersedes rather than sits
  beside. Schema bump; needs Shravan.
- The sign-in does not use Google's `loopback` port registration beyond "any port": the OAuth
  client in the owner's Cloud project must be of the *Desktop* type, which allows any loopback
  port. Worth one line in a README when the owner sets it up.
- `walle sync` in `cli.py`, once #57 has landed.
- Labels (`labelIds`) are read but not rendered. A reader might want `[INBOX]`/`[SENT]`; a
  sender cannot forge them, so they are the one trustworthy thing in a message besides
  `internalDate`. Left out to keep the body to what the issue names.
- A benign e-mail item for the corpus would show a keyword filter cannot refuse all mail, but
  the contact detector fires on any address, so it cannot be written until the point above is
  settled.

**Cost:** not visible to the author agent from inside the run. Shravan can read it from
Hyperagent.
