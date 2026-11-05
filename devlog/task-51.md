# Task 51: hot tier as an MCP Resource

Issue #51. Branch `task/p1-resource`, from `main` at `1a86b01`. Phase 1, D83's second surface.

**Shipped:**
- `src/walle/hot_resource.py` (new). The resource and the watcher.
  - `read(root, principal)` gives the resource's text and a `Status`: `current`, `stale`,
    `unchecked` or `missing`. The text comes from `hook.snapshot`, the hook's own reader, so it
    is the hook's text byte for byte. When the tier is not current, a first paragraph says why.
    A read never raises.
  - `Watch` is one poll task per server. The server's lifespan starts and stops it. Each look
    hashes the tier and `rendered.json`, taking a *non-blocking* shared `render.lock`. When the
    hash moves, it sends `notifications/resources/updated` for `memory://hot-tier` on both
    wires. On the 2026-07-28 wire it publishes to a `SubscriptionBus` that the SDK's
    `ListenHandler` serves (`subscriptions/listen`). On the 2025-era handshake wire it uses
    `resources/subscribe` / `resources/unsubscribe` and tells each subscribed session directly.
  - `publish(server, watch, hide)` registers the resource (`text/markdown`) and the three
    request handlers on the FastMCP server.
- `src/walle/hook.py`. `context()` now delegates to a private `_tier()`. A new `snapshot(root,
  principal, *, wait=True)` returns the same text plus the bytes of `rendered.json`, read under
  the same shared lock. With `wait=False` it raises a new `Busy` (a `Silent`) instead of
  blocking while a render runs. Also a fifth copied name, `MANIFEST`. The hook's own path is
  unchanged: same blocking lock, no manifest read, no new imports (the cold-start import test
  still passes).
- `src/walle/mcp_server.py`. `build_server` builds a `Watch`, passes `lifespan=watch.lifespan`
  and calls `hot_resource.publish`, with `_hide_paths` bound to the root so no host path
  reaches the model. It takes a new keyword `poll`, which tests set low. The docstring no longer
  says "the hot tier is not here".
- `tests/test_hot_resource.py` (new, 20 tests).

**Why:** D54 says the hot tier is put in front of the model, never left behind a tool the
model may not call. D83 lists three surfaces for that: host hook (task 8), **MCP Resource**,
then ACP injection. A resource is loaded by the host, not chosen by the model, so it keeps D54.
It is not a fourth tool, and D102's `tools/list` budget is untouched (tested). The resource
reuses the hook's reader, so the two surfaces cannot drift. D66's `state` hash in
`rendered.json` is what lets a read say "stale" without a model and without re-rendering (hard
constraint 3). D115's rule that memory never blocks a session is why the watcher's first look
never waits on `render.lock`.

**Verify:**
- *A test client reads the resource and receives the update.*
  `test_a_listener_is_told_on_every_render` covers the 2026-07-28 `subscriptions/listen` wire,
  which FastMCP's in-memory client negotiates by default.
  `test_a_2025_subscriber_is_told_on_every_render` covers `Client(mode="legacy")` with
  `resources/subscribe`, starting from no tier at all. Each test does two renders and expects
  two notifications. After each notification it reads the resource back and compares it with
  the installed hook.
- *The text matches what the hook emits.* `test_the_resource_is_exactly_what_the_hook_emits`
  runs the real `walle-hook` console script with a `SessionStart` payload and compares its
  `additionalContext` with the resource text, `rules.md` included.
- *A read on a missing or stale hot tier says so and does not raise.*
  `test_a_missing_hot_tier_says_so_and_does_not_raise`, `test_no_log_at_all_is_missing_too...`,
  `test_an_incomplete_hot_tier_is_missing_not_half_served`,
  `test_a_stale_hot_tier_says_so_and_still_carries_the_render`, and
  `test_a_log_that_will_not_fold_is_unchecked_not_an_error`. The last one plants an instruction
  in a corrupt log line and checks that the note never quotes it.
- Written first and confirmed failing: collection failed with `ImportError: cannot import name
  'hot_resource'`, and `hook.snapshot` did not exist.
- Checks, on the final tree:
  - `uv run pytest`: `1356 passed, 10 skipped`
  - `uv run ruff check`: `All checks passed!`
  - `uv run ruff format --check`: `93 files already formatted`
  - `uv run pyright`: `0 errors, 0 warnings, 0 informations`
- No test needs root or changes a file mode to make something fail. The incomplete tier is made
  by unlinking a file in a folder the test owns. The run was as uid 1000.

**Two-runner walkthrough:** watcher W (or a read R) and renderer X, same principal.
1. X holds `render.lock` exclusively. W's `LOCK_SH|LOCK_NB` fails at once (`hook.Busy`), and W
   skips that look without counting it as a change. R blocks, as the hook does, so R never sees
   a new `identity.md` beside an old `learned.md`.
2. X releases. W's next look gets the shared lock and reads four files and the manifest. X
   rewrote the manifest (it does so whenever the fold moved, even if no file did), so the hash
   moved and W notifies.
3. R reads under the shared lock. It then releases the lock and only after that folds the log
   (`index.update`, which takes `index.lock`) to check the age. So this module never holds
   `render.lock` and `index.lock` at once, and cannot meet the renderer's order in reverse.
   - If a second render Y lands between R's read and R's fold, R calls the text it read
     `stale`. That is true of that text. W then sees Y's manifest and notifies, and R's
     re-read is current.
   - If an append lands after R's fold, R calls the text current one event early. Newer is the
     right way round.
4. The baseline: the lifespan takes W's first hash before the server answers anything, so
   nothing a client could have seen gets folded into the starting point. If a render holds the
   lock then, the first successful look becomes the baseline and is not announced. Nobody had
   connected yet, and a read waits for the lock, so nobody can hold the older text.

**Stranger pass:** the diff is over 30 lines and touches IO, a lock and an error path. Found
and fixed:
- *Race in the first draft.* The baseline was taken inside the task, so it could land after the
  change under test, or during a render, which returned `None` and caused a spurious publish.
  This showed up as two flaky tests. Fixed by taking the baseline in the lifespan.
- *That fix blocked startup.* It first retried while a render held the lock, so a hung render
  would stop the server from starting at all. Now it takes one non-blocking look; see step 4.
  `test_the_server_starts_while_a_render_holds_the_lock`.
- *Watcher died silently.* It caught only `OSError`. Any other exception would end the task,
  and every later render would go unannounced with nothing to say so. Now one failed look is a
  state, hashed by exception type. A lasting failure is announced once. The watcher keeps going.
  `test_a_look_that_fails_does_not_end_the_watching`.
- *Read could raise.* A `flock` failure other than "busy" (e.g. `ENOLCK` on a network disk)
  would have made a read raise. It now reads as `missing`, with the reason.
- *Status that lied.* A `rendered.json` that is a symlink, a FIFO or oversized made the whole
  snapshot fail, so a whole tier was reported as `missing`. It now reads as an unreadable
  record (`unchecked`), and the tier is served.
  `test_a_manifest_walle_did_not_write_is_unchecked_not_missing`.
- *Note too specific.* The stale note said "events have been added". The state can also move
  for other reasons (a retraction, a new build's fold), so it now says the log has changed.
- Checked and left: a 2025-era session that disconnects without a send ever failing stays in
  `Watch.sessions` until the server stops. On stdio that is one session for the server's whole
  life, so the leak is bounded. Over HTTP it would grow (see *Noticed*). The broad `except
  Exception` around the age check is deliberate: failing to fold is "could not check", never a
  failed read. The note names only the exception type, because a corrupt line's message quotes
  log text and the hot tier is not enveloped.

**Review:** none yet. The reviewer agent (a different vendor's model) reviews the PR head and
sets `agent-review`. The author does not set it.

**Choices made inside the task:**
- *Poll, not inotify or a callback from the renderer.* inotify is Linux-only, and the renderer
  runs in another process that has no channel to the server. A look is five small reads under a
  shared lock, once a second (`POLL_SECONDS`).
- *"Every render" means every render that changes what a read returns.* A render of an
  unchanged log writes no byte (R1), so nothing is sent
  (`test_a_render_that_changes_nothing_sends_nothing`). An edit to `rules.md` changes what the
  hook would emit, so it is sent (`test_an_edit_made_outside_walle_is_an_update_too`).
- *Both protocol eras.* FastMCP 4's client speaks 2026-07-28 and only gets updates through
  `subscriptions/listen`. Most hosts still speak the handshake era and `resources/subscribe`.
  Serving only one would leave one side with a resource that never updates. FastMCP has no
  public hook for either, so the handlers go on `server._mcp_server` with the SDK's public
  `add_request_handler` (the one private attribute, marked for pyright).
- *Stale is served with a note, not withheld.* An old memory with a warning beats none. Missing
  or incomplete is not served at all, because half a tier has framing that points at text that
  is not there (the hook's rule).
- *Reading the manifest in the hook module.* It has to be read under the same shared lock as
  the files, and `hook.py` owns that lock. Taking a second `flock` from another module would be
  two lock acquisitions that could straddle a render.

**What to read first:** `hot_resource.py`'s module docstring, then `Watch.run` and
`Watch.lifespan`, which hold the baseline logic the stranger pass changed twice. Then
`hook._tier`, the one place both surfaces read from.

**Noticed, not done:**
- When `rendered.json` lists a file under `kept` (an out-of-band edit, D107), the resource
  serves the person's edit and calls it `current`, exactly as the hook does. Neither surface
  says so. A one-line note on both would be honest, but it changes the hook's output and is its
  own task.
- The log growing without a render is not announced. The read says `stale`, but no
  notification tells a host to re-read. Announcing it would mean folding the log on every poll.
- `Watch.sessions` keeps 2025-era sessions until unsubscribe, a failed send, or server stop.
  Fine on stdio. An HTTP transport would want a session-closed hook to drop them.
- `build_server` now starts a poll task per connection lifespan. If a future transport serves
  many principals from one process, that would be one task per principal.

**Cost:** not visible to the author at writing time. Shravan can read it from Hyperagent for
this thread.
