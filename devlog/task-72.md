# Task 72: the container executor, dormant, driven through the standard `docker` CLI

Issue #72. Branch `task/p2-executor`, from `main` at `3cd1633` (task 68's merge). Phase 2's third
task (R8). Nothing in this build calls it: there is still no runner, and the switch is off. This
is the executor a runner will hand a step to once `admit` (task 68) has let it through, and the
exact shape of the container it gets.

**The escape attempts have not run.** This VM has no `docker` client and no daemon, so every
test in `tests/test_executor_escape.py` skipped here, with that reason. They need the owner's
Mac (OrbStack) before this merges — see *Verify* for the one command. Nothing below claims the
boundary holds in a real container; it claims the plan says what D68 asks for, and that the
tests which would prove it exist, skip honestly, and refuse to skip when told the daemon must be
there.

**Shipped:**
- `src/walle/executor.py`, new, about 370 lines, half of them docstring. Imports `os`, `re`,
  `secrets`, `shutil`, `subprocess` and the stdlib only; nothing from `walle`.
  - `Job`: workspace, command (a tuple, never a shell string of ours), image, `memory`, `cpus`,
    `pids`, `timeout_s`. Every field is checked at construction against the spelling the CLI
    documents, so nothing that the CLI could read as an option reaches the argv: an image must
    look like an image reference (`-rm`, `Alpine`, `a:b:c`, a trailing newline are refused), a
    memory cap like `512m`, cpus like a positive decimal, pids at least 1, the timeout finite
    and positive. `fullmatch`, not `match` — `$` accepts a trailing newline and `match` would
    have let `"x\n"` through; the test caught it.
  - `check_workspace(path)`: absolute, resolved, an existing directory, not `/`, not the home
    folder, no comma or newline (the `--mount` syntax cannot carry one). Resolving first means a
    symlink mounts what it points at, under that name, and the plan shows it.
  - `plan(job, token)`: the `docker run` argv, with no process started — `--rm --init`, a
    `--name` and a `--label walle.executor=<token>`, one `--mount type=bind` of the workspace at
    `/workspace` and `--workdir` there, `--read-only` with a `--tmpfs /tmp` that is `noexec,
    nosuid`, `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`
    and `--memory-swap` equal (the CLI's spelling of "no swap"), `--cpus`, `--pids-limit`, then
    the image and the command. No `--env`, `-e` or `--env-file`: the container's environment is
    empty unless the CLI is told otherwise, and it is not told. Deterministic for one token.
  - `enabled(environ)`: `WALLE_CONTAINER_EXECUTOR` is exactly `on`. Same rule as missions.
  - `run(job, *, environ, docker, token)`: refuses before any process starts unless `enabled`,
    and when the client cannot be found; runs the plan with the job's timeout; on a timeout the
    client is killed, and because killing the client does not stop the container, `docker rm -f
    walle-<token>` follows, and `Outcome.removed` says whether that worked — `False` with the
    daemon's words in `stderr`, never a silent "cleaned up". Exit 125 is the client failing on
    its own account (no daemon, no such image) and the command never ran, so that is a
    `Refusal` carrying the stderr, not an exit code attributed to the command.
  - `reap(token=None, *, docker)`: `docker ps --all --quiet --filter label=walle.executor[=token]`,
    then `docker rm --force` of what it lists. Needs no switch; raises `DockerError` when the
    daemon refuses, so a reap that did nothing is never reported as one that did.
  - `unavailable(docker)`: `docker info --format {{.ServerVersion}}`; the client's own words
    when it fails, `None` when a daemon answers. The escape tests' skip-or-fail reads it.
- `tests/fake_docker.py`: a fake client, like `tests/fake_acp.py`, run through `sys.executable`.
  Records every invocation as a JSON line; seven modes (`normal`, `fail-run`, `hang`,
  `hang-stuck`, `no-daemon`, `stale`, `stale-stuck`) so each path in the executor is exercised
  alone. Starts nothing, talks to nothing.
- `tests/test_executor.py`, 84 tests: the plan's every clause, the job's refusals, the client
  driven through the fake (argv equals the plan exactly; exit code and output carried; a hang
  killed and removed by name; a failed removal reported as such; a made token names the
  container; a missing client and a dead daemon are refusals), reap by label and by token,
  `unavailable` in three states, the dormant rule, and two tests that run pytest on the escape
  module in a subprocess with a stub `docker` on `PATH` that cannot reach a daemon: without
  `WALLE_REQUIRE_DOCKER` every test skips and the reason names the variable; with it set to
  `1`, they fail and say no daemon was reachable. A third pins the exact Mac command in the
  escape module's docstring.
- `tests/test_executor_escape.py`, 19 tests, **all skipped here**: writes outside the workspace
  (`/`, `/etc`, `/usr`, `/root`) refused and nothing on the host; `/tmp` writable but nothing in
  it runs; a sibling folder of the workspace with a synthetic secret unreachable by its host
  path, by `..`, and by grep over the container's folders; one bind mount in `mountinfo`;
  `CapEff`, `CapPrm` and `CapBnd` all zero; `mount` fails; `chown` on a writable `/tmp` file
  fails (only the missing `CAP_CHOWN` refuses that one); `NoNewPrivs: 1`; `lo` the only
  interface and a connection out fails; `memory.max`, `cpu.max` and `pids.max` read back as
  declared (cgroup v1 paths as fallback); a sixty-fork storm against a cap of twenty dies
  before its last line, with the fork named in stderr (see the second commit, below); a
  `sleep 60` under a 3 s timeout is killed, removed, and absent from `docker ps`; a container
  left under the label by the CLI directly, as a crashed parent would leave one, is found and
  removed by `reap`. One positive test — a write inside the workspace reaches the host — so the
  refusals are read as a boundary and not as a broken harness. The image is pulled once per
  module before the first job, so a slow first pull is not read as a timeout.

**Why:** D68 is the list — only the workspace mounted, capabilities dropped, `no-new-privileges`,
memory/CPU/PID caps, labelled so a crashed parent can reap — and `plan` is that list as argv.
D99 amended D68 to make the container the stronger opt-in beside the OS sandbox; the issue's
dormant rule (D117) says the same thing from the other side: off unless the owner turns it on,
outside the log. The two agree, so nothing was flagged. The owner's runtime note is why it is
the standard CLI and nothing OrbStack-specific: OrbStack, Docker Desktop and Docker Engine all
answer to the same client, and a fake client is enough to test everything but the kernel's
part. D62's "say it was refused" is why `Refusal` and `Outcome.removed` carry the daemon's words
rather than a summary. Hard constraint 7 holds: no dependency added, stdlib `subprocess` only.

**Verify:** *escape attempts fail in a real container.* The tests are
`tests/test_executor_escape.py`, listed above. On this VM:

```
uv run pytest tests/test_executor_escape.py -rs
19 skipped — no docker daemon reachable (docker client not found: 'docker'); the escape tests
did not run. On a machine with Docker: WALLE_REQUIRE_DOCKER=1 uv run pytest
tests/test_executor_escape.py -v  — WALLE_REQUIRE_DOCKER=1 makes this a failure.
```

The command for the owner's Mac, with OrbStack as the active `docker` context:

```
WALLE_REQUIRE_DOCKER=1 uv run pytest tests/test_executor_escape.py -v
```

With that variable set, no daemon is 19 failures that say so, not 19 skips — proven here by
`test_the_escape_tests_fail_rather_than_skip_when_docker_is_required`, which runs the module
under a stub `docker` that cannot connect. The verify criterion is therefore **not yet met**;
the PR carries `needs-shravan` for the Mac run.

*Off by default:* `test_the_executor_is_off_by_default` (switch unset, and an explicit empty
environment: `run` returns `Refusal("container executor is off")` and the fake recorded no
invocation), `test_only_the_one_spelling_turns_the_executor_on` (`1`, `true`, `yes`, `ON`,
`" on"`, `container` all refused), `test_nothing_the_agent_writes_can_open_the_executor` (the
module opens no file; `Job` has no `enabled` field; `run` takes no `enabled` argument),
`test_the_cli_gains_no_executor_command`.

Written first: with no `walle.executor`, both test modules failed at import. After the module
landed, one test was right and the code wrong (the trailing-newline image, above); two were
test-helper mistakes (a workspace made twice in one test).

```
uv run pytest               2158 passed, 29 skipped in 75.24s
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 29 skips are the 10 from before plus the 19 escape tests.

**Second commit, after CI ran on the first:** the `check` job failed at `uv run pytest` while
the three other steps passed, and the same commit was green here. I cannot read the CI log from
this environment (the Actions log endpoints are outside the agent's GitHub scope), so the cause
is inferred, not observed: GitHub's `ubuntu-latest` runners ship a Docker daemon, so the escape
tests most likely *ran* there rather than skipping — which the issue did not expect, and which
this VM cannot reproduce. Re-reading them as a stranger for anything that only breaks inside a
real container found one real bug and one race:

- **The fork-storm test could not have passed in any container.** It ran sixty `sleep &` against
  a cap of twenty and then `wait; cat pids.events`. But busybox `ash` ends a non-interactive
  script on a failed fork (`forkshell` → `ash_msg_and_raise_perror("can't fork")` → `exitshell`
  when not interactive), so the `cat` never ran and the exit was 2, not 0. The test now makes the
  shell the witness: the storm's last line, `echo storm-complete`, must *not* be reached, the
  exit must be non-zero, and stderr must name the fork. Without the cap all sixty would start
  and the script would finish, so the test still fails open if the cap is missing.
- **The two hang tests gave the fake 0.5 s** to start Python and write its record before the
  kill. On a cold CI runner that is a race; now 3 s.

If CI is red again after this commit, the log is the next thing to read, and it needs someone
who can — the owner, or a reviewer agent whose scope includes Actions logs. The escape tests
have still not been run by me, anywhere.

**Two-runner walkthrough:** the executor touches no log, lock or derived file. The shared thing
is the daemon. Writers A and B each `run` a job: each makes its own 16-hex token, so the two
containers have different names and labels and neither `rm -f` can hit the other's. `reap()`
with no token removes both, by design — it is the crashed-parent sweep, and a runner that wants
only its own calls `reap(token)`. Two parents racing on `reap()` both list the same ids; the
second `rm --force` fails on an id already gone and raises `DockerError` — a reap that reports
failure over containers that are in fact gone. Rare, loud, and the next `reap()` lists nothing;
preferable to swallowing the error and missing a real refusal.

**Stranger pass:** done on the whole diff (new module, two error paths, a subprocess). Found and
fixed before commit:
- Exit 125 from `docker run` was being reported as the command's exit code. It is the client's
  own failure and the command never started; now a `Refusal`.
- The fork-storm test measured with `ps | wc -l` *while* the PID cap was full, so `ps` itself
  could not fork and the test would have crashed on empty output. First rewritten to read
  `pids.events` after `wait`; then, after CI, rewritten again (second commit, above) because
  `ash` does not survive a failed fork at all.
- `grep -rl … /` would have matched the test's own command line in `/proc/*/cmdline`; now greps
  the container's folders, not `/proc`.
- `chown 1234 /etc/passwd` was refused by the read-only root, not by the dropped capability, so
  it tested the wrong thing; now `chown` on a file in the writable `/tmp`.
- `reap` took an `environ` it deleted on entry; removed.
- `Outcome.removed is False` read as "still running"; after a client killed mid-pull there is no
  container to remove and `rm` says so. The docstring now says "not confirmed removed".
Looked for and not found: a way for the token to reach the argv unchecked (`run` checks a given
one and `secrets.token_hex` makes a lowercase one); a shell anywhere (`shell=True` and
`os.system` are asserted absent); the host environment reaching the container (no `--env`);
a decode that could raise on a stray byte (`errors="replace"` throughout).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run; the brief replaces it for agent work.

**Choices made inside the task:**
- **`--read-only` and a `noexec,nosuid` `/tmp`, beyond D68's list.** D68 says "only the workspace
  mounted"; a writable root filesystem makes that a statement about *mounts* and not about where
  a write can land. Read-only root makes the workspace the only place a write can go and the
  only place one need be looked for, and it is what makes half the escape tests crisp. `/tmp` is
  there because real tools need one; `noexec` so it cannot become a staging area.
- **`--init`.** A `sh -c` child that forks leaves zombies without a reaper; `tini` as PID 1 is
  one CLI flag and standard.
- **No `--user`.** The process runs as the image's default user (root, in Alpine) with an empty
  capability set and a read-only root. Choosing a uid is a decision about file ownership in the
  owner's workspace (on Linux, files written as root in a bind mount are root's), which varies
  by host and is not in the issue. Noted below.
- **The timeout covers the image pull.** A first run against a missing image may be killed by a
  short timeout, and `removed` will then be `False` with "No such container". The alternative —
  a separate pull step — is more surface for one task. The escape tests pull once up front.
- **`WALLE_CONTAINER_EXECUTOR=on`, one spelling**, the same rule and the same reasoning as
  `WALLE_MISSIONS` (task 67): a stray value in a shell profile is not consent. A single
  `WALLE_EXECUTOR=container` was considered and dropped because it invites more spellings.
- **The fake client is a script, not a monkeypatch.** The executor is a `subprocess` caller; a
  patched `subprocess.run` would test the test. `tests/fake_acp.py` is the precedent.
- **Skip-or-fail is a fixture, not a module-level skip,** so with `WALLE_REQUIRE_DOCKER=1` each
  test *fails* by name rather than the module erroring at collection, and the summary line is
  unmistakable.
- **The orphan for the reap test is made with the CLI directly**, not through `run`, because
  `run` waits for its container and a crashed parent does not.
- **Not flagged:** D99 amends D68 but agrees with the issue; `--read-only` extends the issue's
  list without contradicting any D-number.

**What to read first:** `plan` in `executor.py` — about 40 lines, and the whole boundary. Then
`run`'s timeout branch and `_remove`. Then the fixture at the top of `test_executor_escape.py`,
which is the skip-or-fail rule, and `test_the_escape_tests_fail_rather_than_skip_when_docker_is_
required`, which proves it from outside.

**Noticed, not done:**
- **CI has a daemon, most likely.** GitHub's `ubuntu-latest` runners ship Docker, so the escape
  tests run there rather than skip, contrary to the issue's expectation (see the second commit).
  If they pass there, that is extra evidence from a Linux kernel; the owner's Mac run is still
  the one the issue asks for. If the owner would rather CI never start a container, a marker
  and a `-m "not escape"` in `ci.yml` is a one-line change — but `ci.yml` is not this task's.
- **The author agent cannot read CI logs.** Its GitHub scope has no Actions log tool, and the
  REST log endpoint needs a token. A red `check` is then a guess from the diff. Worth a
  `check-logs` subcommand in the `walle-github` skill, or the Actions toolset in the scope.
- **`--user`** (above): the file ownership story for a Linux host is open.
- **A `-v`/`--volume` for the srt profile's reads** (D99) is not here; this executor mounts one
  folder. A Builder step that needs a read-only view of a second path needs a second mount
  kind, declared from the manifest's `reads`.
- **Output is buffered in memory** (`capture_output`). A job that writes gigabytes to stdout
  before the timeout holds them all. A size cap, or streaming to a file in the workspace, when a
  runner exists.
- **The runner.** Nothing connects `admit` to `run`: which image, which caps, how the manifest's
  `reads`/`writes` become mounts, and how a `Step` records the `Outcome`. That is the next task.
- **PLAN §9's status column** is not updated; `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time; Hyperagent's figure for this thread to be
filled in by the owner.
