# Task 56: ACP client for a second host

Issue #56. Branch `task/p1-acp`, from `main` at `4a3c0c5`. Phase 1, D83's third surface.

**Shipped:**
- `src/walle/acp.py` (new). The client.
  - `Host.start(command, *, cwd, env, timeout, stderr)` spawns the host with `Popen` (a list,
    never a shell), starts one reader thread on its stdout, and sends `initialize` with
    `protocolVersion: 1`, no client capabilities, and `clientInfo` naming walle. A host that
    answers with any other version is closed and refused (`ProtocolError`).
  - `Host.new_session(cwd, *, memory, permission, timeout)` sends `session/new` with the
    absolute `cwd` and no MCP servers, and returns a `Session`.
  - `Session.prompt(text, *, timeout)` sends `session/prompt`. Until a prompt carrying the tier
    has been *answered*, the content blocks are `[tier, text]`; after that, `[text]`.
    `session/update` notifications that arrive during the wait are kept on the returned `Turn`,
    whose `text` is the joined `agent_message_chunk` texts. `Session.cancel()` sends the
    `session/cancel` notification.
  - `memory(root, principal)` reads the tier with `hook.snapshot(wait=False)`: `ready` with the
    hook's text, `busy` when a render holds `render.lock` (no wait, D115), `missing` with the
    reason otherwise. Never raises for the state of the tier.
  - `tier_block(text, *, embedded)` is `NOTE` on one line, then `envelope.wrap(text)`, in a
    `resource` block (`memory://hot-tier`, `text/markdown`) when the host advertised
    `promptCapabilities.embeddedContext`, else a `text` block.
  - `decline(params)` is the default answer to `session/request_permission`: `reject_once`,
    else `reject_always`, else `cancelled`. Any other request from the host is answered
    `-32601` method not found, since the client advertised nothing.
  - `Host.close()` is the one exit for the subprocess: stdin closed (EOF), `GRACE` seconds,
    `SIGTERM`, `GRACE` seconds, `SIGKILL`, and a `wait` at each step so it is reaped. Idempotent;
    called from `__exit__`, from `start` on any failure after the spawn, and before every
    `HostGone`, `Timeout` and `ProtocolError` is raised.
  - Errors: `HostError` (a JSON-RPC error reply; host alive, session usable), `HostGone`
    (stdout closed or already closed; carries `returncode`), `Timeout`, `ProtocolError`.
- `src/walle/cli.py`. A new `walle acp --root --principal [--cwd] [--timeout] -- COMMAND...`
  that reads one turn per stdin line and prints the host's text. With no tier it says so on
  stderr and runs anyway. An error reply is printed to stderr and the next line is sent; a dead
  host is a `ClickException`, exit 1, after `close`.
- `tests/fake_acp.py` (new). A fake ACP host: speaks the stdio transport, records every message
  it receives to a file, echoes the prompt's text blocks, and has twelve modes for the ways a
  host can misbehave (old version, error on initialize, error on the first prompt, dies, hangs
  and honours EOF, hangs deaf to EOF and `SIGTERM`, writes a non-message to stdout, asks for
  permission, asks for a file, floods stderr).
- `tests/test_acp.py` (new, 30 tests).
- `README.md`. A short "The ACP client" section, after the MCP server's.

**Why:** D83 lists three surfaces that put the hot tier in front of the model rather than
behind a tool it may not call (D54): the host hook (task 8), the MCP Resource (task 51), and
ACP injection, "when a second host matters". This is the third. One client gives memory to any
host that speaks the protocol, which today is Codex, Gemini CLI, Claude Code and others. D55 is
why the tier travels in an envelope here when it does not under the hook: the hook's
`additionalContext` is a slot the host frames as context, but a prompt block is the person's
own turn, and the model would read a stored "ignore your rules" as the person saying it. D90
names `session_start` as the hook event; the protocol has no such event, and a turn of its own
would be the "Remembering…" pull D54 diagnosed, so the first turn carries the tier. D115 is why
the tier is read without waiting and why a missing one is a session without memory, not an
error.

**Which spec.** Read from `agentclientprotocol.com` (overview, initialization, session setup,
prompt turn, content, transports, schema) and the schema file of the latest stable release,
`schema-v1.29.1`, published 2026-11-02, on the day of the task. The protocol version on the
wire is the integer `1`, moved only on breaking changes; capabilities carry everything else.
A `schema-v2.0.0-alpha.13` exists; it is alpha and not spoken here. The spec's baseline is that
every host accepts `text` and `resource_link` blocks, and `resource` (embedded) only with
`embeddedContext`, which is why the tier has two shapes.

**Not tested against a real host.** No Codex or Gemini CLI is available where this was built,
and none was run. Every test talks to `tests/fake_acp.py`, which was written from the spec by
the same author as the client, so it shares the author's reading of the spec. The first run
against a real host is the first thing to check; what is most likely to differ is the shape of
`session/update` and whether the host puts anything but messages on stdout.

**Verify:**
- *A fake ACP host receives the hot tier first.* `test_the_fake_host_receives_the_hot_tier_first`
  reads the fake's own record of what arrived: `initialize`, `session/new`, then a prompt whose
  first block is the tier and whose second is the person's text; the second prompt has no tier.
  `test_a_host_without_embedded_context_gets_the_tier_as_text` is the same with a host that
  advertised nothing, where the tier goes as `text` and the fake's echo shows it reached the
  turn.
- *A host failure leaves the session usable.* `test_an_error_reply_leaves_the_session_usable_
  and_the_tier_is_sent_again`: an error on the first prompt, then the same session answers the
  second, and the tier is sent again, because a prompt the host refused may never have been
  read. For failures that end the host — it dies mid-turn, hangs past the deadline, hangs deaf
  to EOF and `SIGTERM`, writes a non-message on stdout, names another version, refuses
  `initialize`, never answers `initialize` — a test each checks the error named, that the
  process is reaped (`returncode` set), its pipes closed, the reader thread gone, and by `ps`
  that no fake outlived the test. A further prompt on a dead host raises `HostGone` at once.
  `test_a_command_that_does_not_exist_raises_the_os_error_and_starts_nothing` covers the one
  failure before a spawn.
- *The hot tier stays inside its untrusted envelope where the protocol allows.*
  `test_the_tier_block_is_one_envelope_and_a_forged_marker_inside_it_is_defaced` unwraps the
  block strictly (exactly one envelope, matching ids, nothing before or after) and checks a
  forged closing marker planted in `rules.md` arrives as `[forged marker removed]` with the
  rest of the line intact. The first test checks the body is the hook's text, byte for byte.
- Written first and confirmed failing: collection failed with `ImportError: cannot import name
  'acp' from 'walle'`.
- Checks, on the final tree:
  - `uv run pytest`: `1467 passed, 10 skipped in 67.10s`
  - `uv run ruff check`: `All checks passed!`
  - `uv run ruff format --check`: `111 files already formatted`
  - `uv run pyright`: `0 errors, 0 warnings, 0 informations`
- No test needs root or changes a file mode. A missing tier is a folder never rendered; a busy
  one is a shared lock the test holds on `render.lock` in a folder it owns; a deaf host is the
  fake ignoring `SIGTERM` in its own process. The run was as uid 1000. The ACP tests take about
  nine seconds, most of it the two `GRACE` waits in the deaf-host test.

**Two-runner walkthrough:** the client C reading the tier and a renderer R, same principal.
1. R holds `render.lock` exclusively. C's `LOCK_SH|LOCK_NB` fails at once (`hook.Busy`); C
   reports `busy`, opens the session without memory, and does not wait. The next session reads
   R's render.
2. R releases. C's next `memory` call takes the shared lock, reads the four files under it, so
   never a new `identity.md` beside an old `learned.md`, releases, and builds the block from
   that text. Nothing of the protocol runs under the lock, and this module never takes
   `index.lock`, so it cannot meet the renderer's order in reverse.
3. Inside the client: one reader thread owns stdout, one lock orders writes, one lock keeps a
   single request in flight. `cancel` is a write only, so a turn can be cancelled from another
   thread while `prompt` waits. `close` from the waiting thread kills the host, which closes the
   reader's end of the pipe; the reader sees EOF, closes stdout itself, and ends.

**Stranger pass:** the diff is over 30 lines and is all IO, a thread and error paths. Found and
fixed:
- *A close that could block.* The first draft had `close` close stdout from the main thread
  after a bounded join of the reader. If a grandchild of the host still held the pipe, the
  reader would still be in `read`, and closing a buffered file under a blocked reader takes the
  same lock, so `close` would hang. Now the reader is the only thing that closes stdout, in its
  `finally`. `close` joins it for `GRACE` and moves on; the thread is a daemon for the case
  where a grandchild never lets go.
- *A half-turn with the host alive.* If the caller's `permission` callable raised, the first
  draft answered the host `cancelled` and re-raised. The host would then finish the turn and
  answer a request nobody was waiting for; the next `request` would drop that answer, file its
  updates on the wrong turn, and `delivered` would say the tier had not landed when it had. Now
  a raising callable closes the host before the exception propagates. The turn could not be
  accounted for, so the connection is not kept. `test_a_permission_callable_that_raises_ends_
  the_host_rather_than_a_half_turn`.
- *An answer lost at the deadline.* The loop checked the clock before looking at the queue, so
  a reply that arrived in the last instant was discarded and the host killed. Now the wait
  itself carries the remaining time, clamped at zero, and the queue is looked at once more
  before giving up.
- *Two requests on one queue.* Nothing stopped two threads from calling `request` at once, and
  each would have consumed the other's reply. One lock now keeps one request in flight; it is
  not held by `notify`, so `cancel` still goes through.
- *A spawn with no reader.* If the reader thread failed to start, the child was killed and
  reaped but both pipes stayed open. They are closed now. Pathological, and cheap.
- *A status that would lie.* `could not start X: None` when an `OSError` has no `strerror`; the
  command now falls back to the exception's type.
- Checked and left: `HostGone`'s message says "exited with status N" only after `close` has
  reaped the process, so the number is real; `close` suppresses `ProcessLookupError` from
  `terminate` and `kill` because the process ending between the wait and the signal is the
  outcome wanted, and `wait` still reaps it; an unknown response id is dropped silently, which
  after the permission fix can no longer happen in this module's own flows.

**Review:** none yet. The reviewer agent (a different vendor's model) reviews the PR head and
sets `agent-review`. The author does not set it.

**Choices made inside the task:**
- *The tier rides the first prompt, not a turn of its own.* A separate turn costs a model call
  and an answer nobody asked for, and is the pull pattern D54 rejected. A block in front of the
  person's words is read first and costs nothing extra.
- *Resent after an error, not after a timeout or a death.* An error reply means the host is
  alive and answered; whether its model read the blocks is unknown, so the tier goes again.
  After a death or a deadline the host is closed and the question does not arise.
- *`resource` when allowed, `text` otherwise.* The schema calls embedded resources "the
  preferred way to include context" and gives them a URI, which lets the tier be named
  `memory://hot-tier`, the same name the MCP Resource has. The envelope is identical in both
  shapes, so the protection does not depend on the host's capabilities.
- *A `NOTE` outside the envelope.* `recall` does the same (`DATA_NOTE`): one line walle wrote,
  saying what the enveloped block is, so the model has a reason to read it as memory rather
  than as a pasted document. walle's own sentence is not log text and does not need the
  envelope.
- *Permission is declined by default.* The protocol has no "ask the person" outcome a client
  without a person can give; the choices are to pick an option or cancel. Allowing anything
  would make walle the hand on the tool, which D62 and D72 keep with the person. A caller with a
  person in front of it passes its own callable.
- *No `SIGALRM`, no timer on the tier read.* The hook's 2-second budget exists because it runs
  as a hook with a host waiting; here walle is the client and nothing waits on it but the
  person, and the one blocking read (the lock) is made non-blocking instead.
- *Stderr inherited by default.* A pipe nobody drains stalls a chatty host; inheriting cannot.
  `test_a_host_that_logs_a_lot_on_stderr_does_not_stall_the_turn` passes a file instead and
  checks 300 KB went through.
- *`request` is public.* `session/load` and the rest are not wrapped; a caller that needs one
  can send it over the same connection rather than reaching into a private method.
- *A line per turn in `walle acp`.* The simplest shape that lets a person try it. A multi-line
  prompt is a later task, if the command is used at all once an editor speaks ACP to walle.

**What to read first:** `acp.py`'s module docstring, then `Host.close` and `Host._read`
together (who closes what, and why the reader owns stdout), then `Session.prompt` for the
`delivered` rule. `tests/fake_acp.py` is short and shows what the client is being held to.

**Noticed, not done:**
- The tier is not checked for staleness here, as the MCP Resource does (`hot_resource.read`).
  Doing so needs `index.update`, which folds the log; the hook does not do it either. A `stale`
  note on this surface would be honest and is its own small task.
- The tier has no size cap on this surface. The hook warns past Claude Code's 10,000-character
  `additionalContext` limit; ACP names no limit, and an index-mode render (D81) is bounded at
  about 8,000 characters anyway.
- `Turn.text` keeps `agent_message_chunk` only. Thoughts, tool calls and plans are on
  `Turn.updates` but nothing reads them.
- `walle acp` has no `session/load`, no cancellation on Ctrl-C mid-turn (the context manager
  closes the host instead), and no way to pass MCP servers to the host. A real editor-side
  client would want all three.
- A session ending is where D44's proposer would hang (`session_end` in D90's vocabulary). This
  client does nothing at close beyond ending the host.
- When a grandchild of the host holds the stdout pipe open, the reader thread outlives `close`.
  It is a daemon thread and holds no lock, but nothing reports it.

**Cost:** not visible to the author at writing time. Shravan can read it from Hyperagent for
this thread.
