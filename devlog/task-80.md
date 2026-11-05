# Task 80: optional Opik exporter, counts and timings only, dormant

**Shipped:**
- `src/walle/opik_export.py`: the exporter. A pure fold (`traces`) turns finished missions into
  one Opik trace per mission and one span per started step, using the token and dollar figures the
  mission fold already sums (D22). `scan` checks the exact bytes before they go out. `export` checks
  the switch, then the endpoint, asks Opik which traces it already has, and sends the rest: spans
  first, then the trace. It speaks Opik's REST API (`/v1/private/spans/batch`,
  `/v1/private/traces/batch`, `GET /v1/private/traces/{id}`) through the standard library.
- `tests/test_opik_export.py`: the verify criteria as tests, 56 of them.
- `tests/fake_opik.py`: a real HTTP server on `127.0.0.1` that answers those three calls the way
  Opik's local server does. It records every request with the body bytes exactly as received.
  It does not need the `opik` package.
- `pyproject.toml`: `[project.optional-dependencies] opik = ["opik>=1.8"]`, an extra and never in
  `dependencies`. `uv.lock` is regenerated to match. CI's `uv sync --group dev` does not install it.

**Why:**
- **D25, as the owner narrowed it on 2026-11-05.** Opik is optional and walle works the same
  without it. No module in the package imports `opik_export`, and a test checks that. The exporter
  never writes anything, and Opik only ever holds a read-only copy of the log.
- **D22.** The figures that leave walle are the ones the budget abort already reads:
  `NodeState.spent`, `MissionState.spent` and `spent_from`. That last field was added on main so
  that "a later exporter reads the per-step figures from" it, and this is that exporter.
- **D92.** Nothing keeps a cursor in a side file. Each run folds the log again to find what should
  be in Opik. Ids are derived from refs, so they are the same on every fold, and what Opik already
  has is found by asking it. See *Choices* for why this is the reading of D92 I took.
- **D117 and the dormant rule.** The exporter does nothing unless `WALLE_OPIK` is exactly `on`.
  When it is off, `export` returns before the fold, the endpoint check or any import of an HTTP
  client. `urllib.request` and `http.client` are imported inside the transport, and `opik` is never
  imported at all.

**Verify:** the issue's criteria, and the test for each:
- *Off: nothing imported or sent, and a test proves it.* `test_the_switch_is_off_in_a_fresh_environment`,
  `test_only_the_exact_word_turns_it_on` (`1`, `true`, `ON`, ` on` all stay off),
  `test_off_sends_nothing_even_with_an_endpoint_set` (the fake gets zero requests), and
  `test_off_imports_nothing`. That last one runs in a fresh interpreter: after the import and an
  export with the switch off, none of `opik`, `httpx`, `requests`, `urllib.request` or
  `http.client` is in `sys.modules`.
- *On: a fake Opik server receives each mission and step with its token count, cost and
  duration.* `test_a_finished_mission_and_its_steps_reach_the_server` checks every figure. The
  mission has 1050 tokens, $0.0135 and 420000 ms. Its two spans have 1000 and 50 tokens, $0.0125
  and $0.001, and 210500 ms and 120000 ms. `test_figures_come_from_the_golden_budget_log` runs on
  the v1 `budget` fixture from main and checks the result against `fold_missions`: 2300 tokens,
  $0.06, a failed outcome, and no span for the two nodes that never started.
- *Re-running sends no duplicates.* `test_rerunning_sends_no_duplicates`: the second run only
  asks, makes no POST, and the server holds one trace and two spans.
  `test_the_cursor_is_the_log_and_the_server_not_a_side_file` checks that no file anywhere under
  the tmp dir changes across two runs.
- *A failed or missing server never fails a mission or changes the log.*
  `test_a_missing_server_fails_nothing_and_leaves_the_log` points at a closed port: the run reports
  `unreachable`, the log files hash the same, and the fold is equal before and after.
  `test_a_failing_server_is_reported_and_retried_next_run` returns a 500 on spans or on traces: the
  mission goes into `failed`, no trace is stored without its spans, the log is unchanged, and the
  next healthy run sends it. `test_a_failed_existence_check_sends_nothing_for_that_mission` covers a
  failed check, and `test_a_redirect_is_not_followed` covers a redirect: the other server gets
  nothing.
- *Counts and timings only.* `test_no_text_reaches_the_server` scans what the fake received, path
  and body. None of the fixture's titles, notes, node ids or the memory's text appears, and every
  body passes `scan`. `test_a_payload_with_text_in_it_is_never_sent` monkeypatches the span builder
  to slip a title in. The run marks the mission failed with a scan reason, and the fake receives no
  POST. `test_the_scan_refuses_anything_but_counts_ids_and_times` feeds it free text, unknown keys,
  a bool, NaN, non-ASCII, non-JSON and a duplicated key, and each is refused.
- *Endpoint defaults to localhost; non-local refused unless explicitly set.*
  `test_the_endpoint_defaults_to_localhost` (`http://localhost:5173/api`),
  `test_a_local_endpoint_is_accepted` (localhost, 127.0.0.0/8, `[::1]`),
  `test_a_non_local_or_odd_endpoint_is_refused` (a remote host, `localhost.example.com`, 10/8,
  0.0.0.0, credentials, a query, `file:`, no scheme). Each of those also shows a spy transport got
  zero requests. `test_a_remote_endpoint_needs_its_host_named` and
  `test_a_named_remote_host_still_needs_https` cover the explicit opt-in.
- Test first: the file was written before the module, and it failed at collection with
  `ImportError: cannot import name 'opik_export'`.

Check output, on the final tree:
```
uv run pytest            2726 passed, 30 skipped in 108.08s
uv run ruff check        All checks passed!
uv run ruff format --check   158 files already formatted
uv run pyright           0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the exporter touches no lock, writes no file and appends nothing, so
the log cannot be affected. The remaining race is two exporters running at once against the same
Opik. Both fold the same log and get the same ids. Both `GET` the trace and get 404, then both
POST the same spans and the same trace with the same ids. The fake stores rows by id, so the second write
replaces the first. Opik's backend is meant to behave the same way, but that is not verified
here. If it does not, the worst case is two identical rows, and only when two exporters race.
One exporter run after another never re-sends. A writer
appending steps while the exporter runs is the reader's usual case: the exporter sees the log
before or after the append. If the mission was not finished in the copy it read, it waits for the
next run.

**Stranger pass:** done on the whole diff, which is over 30 lines and is network IO with error
paths. It found:
- **Duplicate keys hid text from the scan.** `json.loads` keeps the last of two equal keys, so
  `{"name": "<a title>", "name": "walle step"}` would have passed the scan with the title still on
  the wire. Fixed: an `object_pairs_hook` refuses a repeated key, and there is a test for it.
- **A status that lied.** A run where every mission failed reported `state="done"`. It is now
  `finished`, documented as "every mission was tried, see `failed`".
- **Ref order is not clock order across streams.** The trace's end was the last applied step in
  ref order, and refs sort by stream name first. It is now the latest step by time, and a span
  starts at its earliest status step. `test_times_are_by_clock_when_streams_interleave` covers it.
- Checked and left as is: `HTTPError` bodies are closed (`with error:`). Timeouts, refused
  connections and `http.client` protocol errors all become `Unreachable`, which ends the run
  without raising. `urllib`'s default redirect handling and environment proxies are both turned
  off, so the bytes go to the URL that was checked. The URL itself is checked a second time in
  `_send` against the three paths the module calls.

**Review:** independent review is the reviewer agent's, on this PR, per the brief. There was no
`scripts/review.sh` run, because the brief replaces it for agent work.

**Choices made inside the task:**
- **Standard-library HTTP, not the Opik SDK.** The extra is in `pyproject.toml` as asked, but the
  module does not import `opik`. There are two reasons. First, "scan what is actually sent" only
  holds if we hold the bytes: the SDK batches and serialises on a background thread, out of
  reach. Second, the tests have to run without the package. So the extra gives the owner Opik's
  own tools (`opik configure`, its health check) next to the exporter, but the exporter does not
  depend on it. If the owner wants the SDK to be the wire instead, that is a later change, and it
  would need its own way to scan.
- **The cursor reads D92 as "derived, never stored".** D92's connector cursor is
  `max(ts) over observation events`. The log does not record what was sent out, and making it do
  so would mean a new event type (D88) and an exporter that writes to the log, which the issue rules
  out ("never ... changes the log"). So what *should* be in Opik is folded from the log every run.
  Ids are a pure function of refs, and whether Opik has a trace is asked of Opik with one `GET`.
  There is no side file and no KV store. If the owner reads D92 more strictly, this is the place
  to disagree.
- **Only finished missions are sent, and once.** A mission is sent when its outcome is `done` or
  `failed`. Sending open missions would mean updating traces in place, and then "no duplicates"
  becomes "no stale copies". The cost: a mission whose outcome changes after it was exported (a
  supersede that undoes a failure, as in the `chores` fixture) is not re-sent. That is listed
  under *Noticed*.
- **No node ids and no titles, even though node ids are slugs.** A node id like `water-plants` is
  a person's words. Span names are fixed (`walle step`), and the span id hashes the mission ref and
  the node's *position*, or the inject step's ref, never the node id, because a hash of a short
  slug can be guessed back. Refs (`d-tab:2`) are sent, so the owner can get from a span to the
  events. A device id is the one part of a ref chosen by a person, and the owner picks it at setup.
- **A remote host is allowed only by name, and only over https.** "Unless the owner sets it
  explicitly" could mean "sets the URL". I read it as a second, separate act, so a stray URL in
  the environment is not enough: `WALLE_OPIK_REMOTE_HOST` must name the same host, and plain http
  to a remote host is refused even then.
- **Ids are UUID v7**, as Opik expects. The 48-bit time is the event's own `ts`, and the rest is
  SHA-256 of the key, so the same log always gives the same ids.
- **The project name is a slug.** It is owner-set, but it goes out on every row, so it is held to
  the same no-text rule.

**What to read first:** the module docstring's five rules, then `scan` and `_send`, because that
is where "counts and timings only" is enforced. After that, the D92 choice above, which is the
call most worth a second opinion.

**Noticed, not done:**
- A mission that changes after it was exported, for example an outcome flipped by a supersede or
  a step appended later, keeps its first copy in Opik. Re-sending would need a "what changed"
  check (for example comparing `dag_hash` with a value stored in Opik metadata) and an update
  call.
- There is no CLI command and no scheduler hook. Nothing calls `export` yet, which fits the
  dormant rule. Wiring it to a command is a separate task, once the owner has read this.
- Opik Cloud needs an API key and a workspace header. Self-hosted needs neither, so neither is
  sent. A remote self-hosted Opik behind auth would need the same.
- Opik's `usage` takes prompt and completion tokens separately, but a step event records only a
  total, so only `total_tokens` is sent.
- `CONVERSATION.md` has no appended entry for the 2026-11-05 narrowing of D25. The constraint test
  cites it, but the record does not hold it yet. That file is owner-only.

**Cost:** not available to the author at write time. Hyperagent's figure for this thread is to be
filled in by the owner.
