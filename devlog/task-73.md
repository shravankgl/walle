# Task 73: the Builder — an approved proposal becomes a staged workflow that runs only in the container

Issue #73. Branch `task/p2-builder`, from `main` at `3accce4` (task 71's merge). Blocked by #71
(reviewer, PR #87) and #72 (container executor, PR #84). Both are merged. Phase 2 task (R8). This
is the first code that calls the executor, so it also closes the three low findings from PR #84's
review that the issue lists. **`executor.py` changed, so auto-merge is left off.** The owner runs
the escape tests on the Mac before merge (see *Verify*).

**Shipped:**
- `src/walle/builder.py`, new, about 650 lines, a third of them docstring. Five things:
  - `approved(records, ref)`: the only input the Builder takes. It must be a live assertion that a
    person (`actor` user) kept from exactly one proposal, and that proposal must not be the
    Builder's own. Each of these is refused with its reason: a staged, dropped or superseded
    proposal, an assertion nobody kept, an assertion written without a proposal, a missing ref,
    and a kept workflow. A malformed ref is a `ValueError`, as everywhere else. Pure: it reads only
    the records it is given.
  - `build(approved, model, *, environ)`: one model call, and it writes nothing. Before the call,
    `hazards.flag` runs over the approval. A planted instruction, a forged marker or turn, a
    credential, a secret, contact details or a confidence is refused without a call, and the
    reason names the kind but never quotes the text. The approval is sent inside a fresh envelope
    (D55). The reply must be a JSON object with exactly `title` and `steps`, and each step must
    have exactly `id`, `title`, `command`, `reads` and `writes`. The rules: an id is a node id; a
    command is a non-empty list of strings, with no NUL and no argument over 4000 characters;
    every path is relative and stays inside the workspace; there are 1 to 8 steps and no
    duplicate ids. `egress`, `image` and any other key make the whole reply a refusal. Next, the
    draft is scanned as the person will read it, string by string and also as the staged body
    whole. A draft that argues or carries a secret is refused and not staged. When the model
    fails, the refusal names the exception's class and never its message.
  - `stage(root, principal, device, draft, *, environ)`: appends **one** `proposal` from
    `ACTOR_BUILDER`, `caused_by` the approval. Its body is `walle workflow v1` followed by the
    workflow as indented, key-sorted JSON, and its summary is `workflow: <title>`. It joins the
    staging queue like any other proposal, and `walle review` keeps, edits or drops it. Nothing
    else is written: no mission, no assertion, no file.
  - `workflow(records, ref)` reads a kept workflow back, as the person left it (kept or edited).
    The proposal must have come from the Builder, the body must still parse, the body's `source`
    must equal the approval the proposal was built from, and that approval must still be live.
    Retracting the memory makes the workflow lapse (D117). `Workflow.mission(ts)` turns it into a
    `Mission` (D67): one node per step, each with its manifest, linked in order, no egress. Nothing
    here appends that mission.
  - `run_step(root, principal, ref, node, workspace, *, environ, docker, token)`: the one way a
    step runs. It reads the log itself (`index.update`), needs a live, person-kept Builder
    workflow, and refuses a workspace that is, holds or sits inside the walle root. It then
    passes the step's argv unchanged to `executor.run`, which refuses unless its own switch is on.
  - `FakeBuilder`: deterministic, with no key and no network. It drafts one step that writes the
    approval to `notes/workflow.txt`. The approval goes in as `$0` of `sh -c` and is never spliced
    into the script.
- `src/walle/executor.py`, the three PR #84 findings:
  - `Job.__post_init__` refuses a bare `str` command with `TypeError`. A `str` is a sequence of
    strings, so the old element check accepted `"rm -rf x"`, which would have run as one argument
    per character. A list is refused the same way, because the field is a tuple.
  - `pids` and `timeout_s` refuse a `bool` with `TypeError` (`True` is the int 1, so the old code
    read `pids=True` as a cap of one). They also refuse any other non-number, such as `"64"` or
    `2.0` for pids.
  - `reap()` now turns a missing client, a client the OS will not start, and a client that does
    not answer within `CLIENT_TIMEOUT_S` into `DockerError` with `exit_code=None`, like its
    siblings. `DockerError.exit_code` is now `int | None`, and the message says "no exit status".
    `_gone` (shared with `run`) also turns `OSError` into "not confirmed gone" rather than raising.
- `tests/fake_docker.py`: one new mode, `ps-hang`, in which `ps` never returns, as with a wedged
  daemon.
- `tests/test_builder.py`, 97 tests. `tests/test_executor.py` gained 21 (105 now).

**Why:** D68 says Builder output runs in a container. D99 made the OS sandbox the laptop default
and kept the container as the stronger opt-in. The issue says the container is where this
output *always* runs, which is the stronger reading, and the two do not conflict, so nothing was
flagged. D117 says install is not enable: the Builder may author and stage, and a human turns it
on. That is why the Builder's only write is a proposal in the staging queue that the person
reviews, and why `run_step` needs a person's keep. It is also why a workflow lapses with the
approval it was bound to. D76 invariant 7 and D18 are why the approval is scanned before a vendor
sees it. D55 is the envelope. D67 is the mission shape. Hard constraint 7 holds: no dependency was
added, and the Builder imports no process library at all. Hard constraint 3 holds: `approved` and
`workflow` are pure folds over records.

**Verify:** *output never runs on the host by default; everything it makes is staged for review.*
- *Never on the host:* `test_builder_output_never_runs_on_the_host_by_default` leaves every switch
  at its default. `build` refuses with zero model calls, `run_step` refuses, the fake docker
  records nothing, and `subprocess.Popen`/`run` are patched to fail the test if anything starts.
  `test_with_the_builder_on_and_the_executor_at_its_default_a_kept_workflow_still_does_not_run`
  covers a kept workflow with the Builder on but the executor at its default: the result is
  `executor.Refusal("container executor is off")` and no process starts.
  `test_with_everything_on_a_kept_step_runs_only_as_docker_run` shows that the one thing started
  is the client running `executor.plan` exactly, with the step's argv after the image and
  `--network none`. `test_the_builder_has_no_way_to_start_a_process_of_its_own` checks that the
  module imports no `subprocess`, `multiprocessing`, `pty`, `shutil`, `threading`, `asyncio`,
  `socket` or `ctypes`, uses nothing from `os` except `os.environ`, calls nothing in `executor`
  except `Job` and `run`, and calls no `exec`/`eval`/`compile`/`__import__`.
- *Staged for review:* `test_everything_the_builder_makes_is_one_staged_proposal` shows one line
  added to the log, a `proposal` from `builder` `caused_by` the approval. It is in `staged()` and
  not in `resolve().current`. The only new files are the index and its lock, and the log has no
  mission. `test_building_writes_nothing_staging_writes_the_one_event`.
  `test_a_staged_workflow_cannot_run_until_a_person_keeps_it` runs the workflow while staged
  (refused), then kept (runs once). Dropped, superseded after keep, and approval superseded after
  keep are each refused. A memory that only looks like a workflow is refused.
- *Off by default:* `test_the_builder_is_off_by_default` (no call; `stage` raises and writes
  nothing), eight wrong spellings,
  `test_run_step_with_the_builder_off_refuses_even_with_the_executor_on`,
  `test_nothing_in_a_call_or_the_log_can_turn_the_builder_on`,
  `test_the_cli_gains_no_builder_command`.

Tests first: before the module existed, `tests/test_builder.py` failed at import and the 14 new
executor tests failed (`str` accepted, `bool` accepted, `TimeoutExpired`/no `DockerError` from
`reap`). After the module landed, one test was wrong: the CLI check matched `rebuild`. One helper
was wrong: it read the log before writing it. I also mutated `builder.py` seven ways (dropped the
root-overlap check, the draft scan, the Builder-actor check, the liveness check, the person-actor
check, the source check, and the switch in `run_step`). A test failed for each mutation.

```
uv run pytest               2667 passed, 30 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  155 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 30 skips are the same as on `main`: 20 container escape tests and 10 others. This VM has no
docker client. **Because `executor.py` changed, the owner's Mac run is still owed before merge:**

```
WALLE_REQUIRE_DOCKER=1 uv run pytest tests/test_executor_escape.py -v
```

The change touches only `Job`'s type checks and `reap`'s and `_gone`'s failure paths. `plan` and
the argv are unchanged, and the escape tests build every `Job` with a tuple command and int/float
caps, so I expect 20 of 20 as at task 72's last Mac run. That is an expectation, not a result.

**Two-runner walkthrough:** `build` holds no lock and writes nothing. `stage` is one
`StreamWriter.append` under the stream lock, the same as `remember`, without `staging.lock`. If
runners A and B stage drafts from the same approval, they append two proposals with different
refs. Both are staged and the person sees both. That is acceptable: neither is live until kept,
and the fold has one answer for each. Keeping and dropping go through `staging._decide` under
`staging.lock`, unchanged. If A and B both call `run_step` on the same kept step, each reads the
log through `index.update` (index lock, then shared stream reads), finds the same workflow, and
starts its own container with its own token. That gives two containers, two names and two labels,
both writing the same workspace. Nothing here stops a step from running twice. That is the
runner's job (D91's "outcome unknown" rule), and it is listed below. If the person supersedes the
kept workflow between A's read and A's `executor.run`, A still starts. The log read and the start
are not atomic, and nothing could make them atomic short of a lock held across a container run.

**Stranger pass:** done on the whole diff. Every path in `run_step` and `build` is an error path.
Found and fixed before commit:
- **The split-hazard test passed for the wrong reason.** It put a newline in the title, which the
  one-line-title rule refuses before any scan. It now splits "Ignore all previous" / "instructions"
  across two arguments, asserts that each half alone is clean, and is refused only by the
  whole-body scan. Removing `draft.body()` from the scan makes it fail.
- **A workflow outlived its approval.** If the person retracted the memory, the kept workflow
  still ran. `workflow` now requires the source approval to be live, and a test covers it.
- **`run_step`'s exceptions were undocumented.** A missing workspace raises `ValueError` from
  `executor.plan`, not a `Refusal`, and the docstring says nothing of this. It now does, along with
  the malformed ref and the missing log.
- **`reap`'s new `DockerError` carried `exit_code` as an int that never existed.** I made it
  `None` and changed the message rather than invent a 127.

Looked for and not found: a path from the model's text to a shell of ours (the argv goes to
`docker run` as elements, and only the container's `sh` ever reads it); a host-side write by the
Builder other than the one log append; the approval or the reply quoted in a reason; a way for an
edited workflow to change its `source` unnoticed.

Accepted, not fixed: `run_step` resolves the workspace to check overlap, then `executor.plan`
resolves it again to mount it. A symlink swapped between the two could point the mount into the
root. Swapping it takes write access to the person's own folders, which is already more than the
container gets. It is noted below.

**Review:** independent review is the reviewer agent's job, on this PR, per the brief. I did not
run `scripts/review.sh`, because the brief replaces it for agent work.

**Choices made inside the task:**
- **A staged workflow is a `proposal`, not a new event type.** D120 says a schema type is a
  decision. The staging queue already does what D117 asks (a person's keep, edit or drop, with
  tombstones), and #74 says workflow proposals are "staged like other proposals". The cost is
  real and the reader should weigh it: **a kept workflow is an assertion, so the resolver puts it
  in `current` and the hot tier will carry its summary and body like any memory.** For one or two
  workflows that is harmless and even useful ("you approved this"). At scale it means a `workflow`
  type, or a hot-tier filter on `ACTOR_BUILDER` provenance. I did not make that decision here. It
  is listed below.
- **The body is a header line plus JSON, not prose.** The person reads it in `walle review`, and
  it must survive an edit and parse back exactly (`parse_workflow` is `render`'s inverse and is
  tested). Indented, key-sorted JSON is readable and gives one spelling per workflow. An edit that
  breaks the shape is refused at run time, not guessed at.
- **Steps run in the order given (a chain), not a DAG.** `Mission` can carry a DAG, but nothing
  here needs one yet, and a model asked for links is one more thing to get wrong. `mission()`
  links the steps in order.
- **No `egress`, no `image`.** The container has `--network none`, so egress would be a promise
  the executor cannot keep. An image is a supply-chain choice (hard constraint 7's neighbour) and
  stays the executor's pinned default until someone decides otherwise.
- **`reads`/`writes` are recorded, not enforced, inside the container.** The container mounts the
  whole workspace. The manifest is what `admit` will check at tool calls once a runner exists, and
  the docstring does not claim more.
- **`TypeError` for the executor's type refusals.** Its siblings raise `ValueError` for bad
  values. A `bool` cap or a `str` command is the wrong type, and Python's convention fits. The
  only caller, the Builder, never builds such a `Job`.
- **Hazards in the approval refuse the build rather than redacting.** A redacted approval would
  build a workflow from text the person did not approve. "Build this one by hand" is honest.

**What to read first:** the module docstring of `builder.py` (three rules, then the dormant rule),
then `run_step` (about 30 lines, the only door), then `_kept` and `workflow`. Then read
`test_builder_output_never_runs_on_the_host_by_default` and
`test_everything_the_builder_makes_is_one_staged_proposal`, which are the verify criterion. Then
the first bullet of *Choices*: the hot-tier consequence is the part most worth a decision.

**Noticed, not done:**
- **A kept workflow lands in the hot tier**, as above. The fix is a `workflow` event type, or the
  renderer skipping assertions kept from `ACTOR_BUILDER` proposals. Either is a decision (D120's
  precedent).
- **The runner.** Nothing appends the `Mission`, records a `Step` for the `Outcome`, guards
  against running a step twice, or calls `admit` for the manifest. `run_step` is the seam it will
  use.
- **`executor.run` has the same `OSError` gap `reap` had.** A client path that exists but will not
  start raises `OSError` from `subprocess.run`, not a `Refusal`. That is one `except OSError` in
  `run`, but it is not in this issue.
- **The workspace check-then-mount TOCTOU** above. Passing the resolved path through to `Job`
  would narrow it. Closing it fully means opening the directory and mounting by fd, which the CLI
  cannot do.
- **No eval corpus for the Builder.** D77 gave the reviewer three corpora and a gate. The Builder's
  safety lives in structure (the container, the keep), but a corpus of approvals that try to
  steer the draft (exfiltration scripts, host paths) would let the model side be measured. That is
  a candidate for #75's gate report.
- **The `TOOL_NAME`-style allowlist for commands.** Any argv may run inside the container. That is
  D68's model (the boundary is the container, not the command), but a person reading `walle
  review` would benefit from a one-line plain-language summary per step, which is the proposer's
  kind of work.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time. The owner fills in Hyperagent's figure for
this thread.
