# Task 52: real model route, with ollama as the local tier

**Shipped:**

- `src/walle/models.py` (new): the model route. `ProviderModel("provider:model")` is the
  proposer's `Model` (`name`, `complete(request)`) plus `text(prompt)`, over one aisuite client
  built with a timeout and **no retries**. `resolve(name)` gives `FakeModel` for `fake` and
  otherwise builds the provider at once, so a missing library, unknown provider or missing key
  is a `ModelUnavailable` before any request. A failed call raises `ModelCallError` saying
  which failure it was: HTTP 429 (stopped, not retried, with `Retry-After` when numeric),
  another HTTP status, a timeout, no connection, or the exception's class name.
- `src/walle/proposer.py`: `ModelCallError` added beside `ModelReplyError`. The shadow phase
  now catches it too, so a failure while judging (after the proposals are durable) lands in
  `shadow_error` rather than raising. Docstrings updated: the adapter is no longer "a later
  task".
- `src/walle/cli.py`: `walle propose --model` takes `fake` or `provider:model` (checked for
  shape and length by a callback, exit 2 if wrong). The client is built only when a run
  proposes, so `--score` works with no provider. `ModelUnavailable`, `ModelCallError` and
  `ModelReplyError` all print `nothing written: <why>` and exit 1.
- `pyproject.toml`, `uv.lock`: `aisuite[ollama]>=0.2.0`, approved for this issue. The lock
  adds aisuite 0.2.0, openai 1.109.1, httpx, httpcore, jiter, distro, sniffio, tqdm.
- `tests/fake_provider.py` (new): a threaded HTTP server on `127.0.0.1:0` that speaks the
  OpenAI chat-completions wire. It stands in for ollama, openrouter and openai, and records
  every request (path, headers, body) as a witness.
- `tests/test_models.py` (new): 27 tests, the verify criterion and the cases around it.
- `tests/test_proposer.py`: `test_the_cli_refuses_an_unknown_model` used `anthropic:claude`
  as its unknown name. That is a well-formed model name now, so the test uses `claude`.
  **This test edit is required by the task**: the issue makes `provider:model` valid. The
  assertion (exit 2, `fake` named in the message) is unchanged.

**Why:**

- D87: one `provider:model` string picks the model and aisuite is the provider layer. Swapping
  the string changes no code. Only the first colon splits provider from model (aisuite's own
  `split(":", 1)`), so `openrouter:google/gemma-3-27b-it:free` reaches OpenRouter as
  `google/gemma-3-27b-it:free`, in the JSON body, never in the URL path.
- D16: the local tier is `ollama:<model>`. It needs no key, and the host comes from aisuite's
  own `OLLAMA_API_URL` (default `http://localhost:11434`).
- D86 (fail closed, through the proposer): a call that fails while proposing propagates before
  anything is written. The log, `seen.jsonl` and `verdicts.jsonl` are all untouched.
- D82: once proposals are durable nothing raises. A failure while judging is `shadow_error`.
- The issue's 429 rule: aisuite's ollama, openrouter and openai providers go through the
  OpenAI SDK, which **retries twice by default, 429 included**. `max_retries=0` turns that
  off, so a 429 is exactly one request. The judging loop stops at the first failure, so no
  further request follows either.

**Verify:** issue #52's criterion, in `tests/test_models.py`:

- *A local fake HTTP server stands in for each provider; swapping the string changes no
  code:* `test_each_route_reaches_its_fake_provider_and_proposes`, parametrized over
  `ollama:llama3.1:8b`, `openrouter:meta-llama/llama-3.3-8b-instruct:free` and
  `openai:gpt-4o-mini` through one helper, `_propose(root, name, base_url)`. Each stages a
  proposal and records a shadow verdict, and the fake saw the exact model name at
  `/v1/chat/completions`. The CLI end to end: `test_ollama_needs_no_key_and_is_found_by_its_own_variable`.
- *`openrouter:<author>/<model>:free` reaches the provider unchanged:*
  `test_an_openrouter_free_model_name_reaches_the_provider_unchanged`.
- *A provider failure or timeout proposes nothing and says why:* HTTP 503, a 0.3 s timeout
  against a 2 s delay, a closed port, a null-content reply and a reply with no choices. Each
  asserts the stream bytes are identical and neither shadow file exists. The CLI version
  checks the `nothing written: … HTTP 500` message and exit 1.
- *429 stops and says so:* `test_a_rate_limit_is_one_request_never_retried` (one request,
  message says rate limited, HTTP 429, not retried, retry after 41 s) and
  `test_a_rate_limit_while_judging_keeps_the_proposals_and_says_so`.
- *No key in logs or errors:* `test_no_key_appears_in_errors_or_logs_or_output`. The fake
  returns a 401 that quotes the `Authorization` header back. The test checks that the key
  reached the fake, and that it appears in none of `str`/`repr` of the error, the
  DEBUG-level captured logs (it asserts the openai SDK's own DEBUG records were captured, so
  the check is not vacuous) or the CLI's stdout and stderr. The error is raised `from None`.

Tests first: before `walle.models` existed, `tests/test_models.py` failed at collection
(`ImportError: cannot import name 'models' from 'walle'`). Two mutation checks after: removing
`max_retries: 0` fails 4 tests (503, timeout, both 429s). Appending the provider's exception
text to the error fails the key test.

No real key is used or read. An autouse fixture clears `OPENROUTER_API_KEY`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OLLAMA_API_URL`, `OR_SITE_URL`, `OR_APP_NAME` and
`ANTHROPIC_API_KEY`, then sets fake key-shaped strings where a provider refuses to start
without one. A real key in Shravan's shell is never read by the tests or sent anywhere.
Nothing depends on file permissions, so the tests mean the same whether or not they run as
root (this run was uid 1000).

Check output, on the commit's tree:

```
uv run pytest             1363 passed, 10 skipped in 42.45s
uv run ruff check         All checks passed!
uv run ruff format --check  93 files already formatted
uv run pyright            0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** not needed. Nothing here touches the log, a lock or a derived
file. The proposer's locking is unchanged: the model call still happens with no lock held
(its walkthrough, step 2). A call that fails there raises before step 3, so no writer takes a
lock and nothing is appended.

**Stranger pass:** the diff is over 30 lines and has error paths and IO (HTTP).

- *Status messages that lie.* The first wording was "timed out after N s". httpx's float
  timeout bounds each wait (connect, read, write), not the whole call. Changed to "timed out:
  no response for N s", and the module docstring says so.
- *A test that proved less than it said.* An automated rewrite to satisfy ruff's SIM117
  pulled the key test's CLI half inside `pytest.raises`, so it never ran (pyright caught it
  as possibly-unbound variables). Restructured. The test also now asserts that openai's DEBUG
  records were captured.
- *Ignored errors.* `text()` catches `Exception` around the one provider call and re-raises
  it as `ModelCallError`. Nothing is swallowed. `KeyboardInterrupt` is not caught.
- *Leaks.* The provider's error text, which is where a 401 would echo a key, is never
  included. Only status, duration or class name. `from None` keeps it out of tracebacks.
  Set-up messages from aisuite (missing key, unknown provider) are passed on after masking
  the value of any `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` variable; they name the variable,
  not its value. `base_url` is never in a message, since a URL can hold credentials.
- *Lock scope:* unchanged, as above.

**Review:** none yet. This is the author's log. The reviewer agent (a different vendor's
model) reviews the PR head and sets `agent-review`. The author does not set it.

**Choices made inside the task:**

- **Same shape as PR #48, separate module.** PR #48 (branch `task/19b-2`, open) adds
  `src/walle/evals/models.py` with a `ProviderModel(name, *, client=None)` over aisuite. I was
  told to reuse its shape and keep clear of its files until it merges. So `walle/models.py`
  keeps the same names and signature (`ProviderModel`, `ModelUnavailable`, `name`, `text`,
  `complete`, `_ensure_client`, `resolve`, the same `provider:model` regex). It only adds
  keyword-only `base_url` and `timeout`, plus the no-retry, timeout and error mapping the
  proposer route needs. Until both land there are two classes. The follow-up is to make
  `evals/models.py` import from `walle.models`.
- **`cli.py` is touched, though #48 also edits it.** The issue needs the proposer's
  `--model` to take the string. #48's hunks are the import at line ~21 and an
  `add_command` at the end of the file. This change is the `propose` command around line 480
  plus one import line three lines above #48's, so a three-way merge stays clean either way.
- **`aisuite[ollama]`, not bare `aisuite`.** Bare aisuite cannot reach any provider. Its
  ollama, openrouter and openai providers all import the `openai` SDK, which only the extras
  pull in, and aisuite names the `ollama` extra for exactly that. The allowlist check reads
  the name `aisuite`. It does mean `openai` is now in the lockfile as a transitive dependency.
- **Built eagerly through `ProviderFactory`.** aisuite builds a provider on first use, which
  would turn a missing key into a failure inside the run. `_ensure_client` builds it with
  `ProviderFactory.create_provider` and hands it to the client. That uses aisuite's factory
  and `Client.providers` attribute: public names, but not documented API. A future aisuite
  could break this, and the tests would show it.
- **Model name capped at 64 characters.** `ShadowVerdict.model` and `Seen.model` hold 64. A
  longer name would stage proposals and then fail every shadow write. It is refused at
  `--model` instead.
- **Providers whose SDK is not installed** (`anthropic:`, …) are `ModelUnavailable` naming
  the extra. They were not added: only `aisuite` was approved.

**What to read first:** `_why` and `ProviderModel._ensure_client` in `src/walle/models.py`.
They decide what a person is told when a call fails, and whether a 429 can ever be retried.
Then `test_no_key_appears_in_errors_or_logs_or_output`.

**Noticed, not done:**

- Real models often wrap JSON in a ```json fence. The proposer's `_parse_list` refuses that
  as "not JSON", and nothing is written. Fail closed is right, but free models may fail
  every run until the proposer strips a fence or asks for JSON mode. That is a proposer
  change, not part of this route.
- A failure on the second of several propose requests discards the first reply's
  candidates and records nothing as seen, so the next run re-sends those excerpts. That is
  right for fail closed. On a free tier with a daily cap it costs requests.
- `OPENAI_LOG=debug` in the environment makes the openai SDK print request options,
  including the prompt (log excerpts), to stderr. The SDK filters auth headers, so no key is
  printed, but the excerpts are. That is the user's opt-in, but worth a line in the README
  when it next changes (#48 is editing it now).
- Once #48 merges, `src/walle/evals/models.py` should import `ProviderModel` and
  `ModelUnavailable` from `walle.models`, leaving one adapter.

**Cost:** not visible to the author from inside the run. To be filled from Hyperagent.
