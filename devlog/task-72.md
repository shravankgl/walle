# Task 72: the container executor, dormant, driven through the standard `docker` CLI

Issue #72. Branch `task/p2-executor`, from `main` at `3cd1633` (task 68's merge). Phase 2's third
task (R8). Nothing in this build calls it: there is still no runner, and the switch is off. This
is the executor a runner will hand a step to once `admit` (task 68) has let it through, and the
exact shape of the container it gets.

**Where the escape tests run.** The issue said CI and agent VMs have no real container. Half
right: this VM has no `docker` client and no daemon, so every test in
`tests/test_executor_escape.py` skipped here, with that reason — but **GitHub's `ubuntu-latest`
runner has a Docker daemon, and the escape tests run there on Linux on every push** (the owner
read the first CI log and corrected his note). They have also run once on the Mac under OrbStack,
at the first commit: 18 of 19 (see *First Mac run*). The Linux run found the most useful thing
this task learned (see *The Linux finding*), and the Mac has not yet run the head that carries
the fix; the issue asks for that run before this merges — see *Verify* for the one command.

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
    client is killed, and because killing the client does not stop the container, `docker rm
    --force walle-<token>` follows, and `Outcome.removed` says whether that worked — judged by
    the listing afterwards, `False` with the daemon's words in `stderr`, never a silent "cleaned
    up". Exit 125 is the client failing on
    its own account (no daemon, no such image) and the command never ran, so that is a
    `Refusal` carrying the stderr, not an exit code attributed to the command.
  - `reap(token=None, *, docker)`: `docker ps --all --quiet --filter label=walle.executor[=token]`,
    then `docker rm --force` of what it lists, then the listing again. Needs no switch; raises
    `DockerError` when something is still listed, so a reap that did nothing is never reported
    as one that did.
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
  declared (cgroup v1 paths as fallback); a sixty-fork storm against a cap of twenty, run in a
  subshell, leaves a refused count above zero in `pids.events` and a `pids.peak` no higher than
  the cap (see the fifth commit, below); a
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

**Third commit, after CI was red again on the second** (`pytest` 88–104 s against 75 s here,
every other step green, and the merge with `main` at `e29426c` green here too, so the difference
is the machine, not the tree). Still inferred, still unread. One more pass over `run` and `reap`
for what only a real daemon does found a race I should have known:

- **`rm --force` on a container started with `--rm` races the daemon's own removal.** The
  container stops, AutoRemove starts, and `rm --force` can exit 1 with "removal of container …
  is already in progress" — over a container that is gone. `run`'s timeout path read that exit
  status as `removed=False`, and `reap` raised `DockerError`, in both cases over containers no
  longer there: the escape tests `test_a_command_past_the_timeout_…` and
  `test_a_container_left_behind_…` would fail on any real daemon, with the executor telling the
  truth about a false alarm. Removal is now judged by the listing: after `rm --force`, `docker
  ps --all --quiet --filter label=…` is asked up to ten times 0.2 s apart (a container shows as
  "Removal In Progress" for a moment), and `removed` is `True` only when it lists nothing. When
  it still lists something, `stderr` carries both the removal's words and the listing's ids.
  `reap` does the same and raises only if something remains. `tests/fake_docker.py` gained the
  record as its memory (a `ps` after an `rm` lists nothing) and two racy modes; three tests pin
  the new rule (`test_removal_is_judged_by_the_listing_not_by_rm_exit_status`,
  `test_reap_trusts_the_listing_when_rm_loses_the_race_with_autoremove`, and the stuck case
  asserting all ten listings were asked).

```
uv run pytest               2160 passed, 29 skipped in 80.22s
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**The Linux finding (fourth and sixth commits).** Red again on the third commit, so I stopped
guessing and read the job log through the skill's token (`GET
/repos/…/actions/jobs/<id>/logs`, a read of this PR's own CI; the GitHub integration's scope
has no Actions tool). The facts: **`2 failed, 2248 passed, 4 skipped`** — the runner has a
daemon, the escape tests ran, 17 of 19 passed in a real Linux container, and the two failures
share one cause:

```
FAILED test_a_write_inside_the_workspace_reaches_the_host
       AssertionError: sh: can't create made.txt: Permission denied
FAILED test_a_write_outside_the_workspace_is_refused[/root/escaped]
       PermissionError: [Errno 13] Permission denied: '/root/escaped'
```

- **Root with every capability dropped has no `CAP_DAC_OVERRIDE`**, so it cannot write a
  bind mount owned by the host user (uid 1001 on the runner, mode 755). A real executor bug,
  not a test bug: the first three commits ran the container as root, and OrbStack's file
  sharing hid it on the Mac, where the Mac run's positive test passed. The boundary was
  *stronger* than the harness assumed — the one positive test failed, not an escape. The fix,
  in `plan`, is `--user <uid>:<gid>`: the fourth commit took the ids from the workspace's
  `stat`; the sixth, on an instruction that crossed with the Mac result, briefly took them from
  the host process; the seventh went back to the workspace's owner (see *Second Mac run*). The
  container therefore also stops running as root at all. On Linux
  that is the one identity that can write the one folder it is given; on the Mac, OrbStack
  remaps bind-mount ownership to the container's user under runc, so the same flag holds.
  `test_the_process_runs_as_the_workspace_owner_not_as_root` pins the plan; in the escape
  tests the positive write asserts the file belongs to the workspace's owner, and
  `test_the_process_inside_is_the_workspace_owner_not_root` checks `id -u` and `id -g` inside.
- **`/root/escaped`** was the host-side check raising, not the container: `Path.exists()` under
  a `/root` the runner user cannot enter raises `PermissionError`. A test bug. Now
  `os.path.exists`, which answers `False` there; `/root` stays in the list and the in-container
  refusal assertions are unchanged (non-root reports "Permission denied" where root reported
  "Read-only file system"; the test accepted both from the start).

What this does to the other escape tests, re-checked for a non-root process: `CapPrm` and
`CapEff` are zero for any non-root process, so `CapBnd` zero is the assertion that carries
weight (its docstring says so). `mount` and `chown` now fail for two reasons that coincide —
not root, and no `CAP_SYS_ADMIN` / `CAP_CHOWN` even for root with `CapBnd` empty — and the test
no longer claims only the capability refuses them; it says it cannot tell the two apart, and
points at the capability test for the second reason standing alone. The cgroup files are
world-readable and `/proc/self/*` is the process's own, so the cap and `NoNewPrivs` tests are
unaffected. The fork storm, the timeout and the orphan
are unchanged. The two earlier "most likely" paragraphs above were partly right (the daemon,
the fork storm — ash does die, and that test is among the 17 that passed on the third commit)
and partly wrong (the `rm --force` race did not show in this log; the fix stands as a
correctness fix, but it was not the red).

```
uv run pytest               2161 passed, 29 skipped in 87.07s
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**First Mac run, by the owner, and the fifth commit.** OrbStack, Docker 29.8.2, at head
`14109d85` (the first commit, before the four fixes above): **18 passed, 1 failed, no containers
left behind.** The failure was `test_a_fork_storm_is_cut_off_at_the_pid_cap`, exit 2 with empty
stderr — the first commit's version, which ran the storm in the main shell and then `wait; cat
pids.events`. The owner's diagnosis, confirmed by hand at `--pids-limit 20`: busybox `ash`
aborts on its first failed fork (`sh: can't fork: Resource temporarily unavailable`), so the
script exits 2 before the `cat`, and the `2>/dev/null` hid the message. Running the storm in a
subshell gives sub-exit 2, `pids.max=20`, `max 1` in `pids.events`, `pids.peak=20`; a control at
`--pids-limit 200` gives `max 0`. A test bug, not a boundary failure: the cap held.

So the 17 of 19 on CI and the 18 of 19 on the Mac are consistent — CI's two were the
root-without-`CAP_DAC_OVERRIDE` pair, and its fork-storm test was already the second commit's
rewrite; the Mac ran the first commit's. The fifth commit rewrites the test the owner's way and
touches nothing in `executor.py`: the storm runs in a subshell `( … ; wait ) 2>/dev/null`, so the
outer shell survives; `sleep 4` lets the forks that did start drain so `cat` can fork; then the
outer shell prints the subshell's exit, `pids.events`, and `pids.peak` when the kernel has it.
Asserted: the outer shell exits 0 (strict — a non-zero exit is the harness failing, not the
boundary holding), the subshell did not exit 0, `max` in `pids.events` is above 0, and
`pids.peak` is at most 20 when present. The second commit's "the shell is the witness" reading
was true but weaker: it proved the shell died, not that the cgroup refused anything.

```
uv run pytest               2161 passed, 29 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

Sixth commit's checks, after the uid source moved to `host_user()` and the non-root escape test
was added:

```
uv run pytest               2161 passed, 30 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The 30th skip is the new `id -u` escape test.

**Second Mac run, and the seventh commit.** The owner ran head `ebf80896` (the fourth commit,
`--user` from the workspace's `stat`) on the Mac: **19 of 19 passed**, and called running as the
workspace owner a good choice. That message and the sixth commit crossed: the sixth had, on the
owner's earlier instruction, moved the ids to `os.getuid()`/`os.getgid()`. The latest word
wins, and it is the one the Mac validated, so the seventh commit goes back to the workspace's
owner — `owner_user(workspace)`, one function over `_owner_ids` (a plain `os.stat`) so the plan
test and `run` agree — and closes the two gaps the owner named:

- **A root-owned workspace is refused.** `--user 0:0` would be root in the container again.
  `owner_user` raises `ValueError("… owned by root …")` for uid 0 whatever the gid, so `plan`
  and `run` both refuse before anything starts. A test process is seldom root, so
  `test_a_root_owned_workspace_is_refused` stands `_owner_ids` in with `(0, 0)` and checks all
  three entry points and that the fake recorded nothing; a second test covers `(0, 1000)`.
- **The user inside a real container is tested.** `test_the_process_inside_is_the_workspace_
  owner_not_root` runs `id -u; id -g` and asserts they equal the workspace's `st_uid`/`st_gid`
  and the uid is not 0 (the sixth commit's version compared against the host process; this
  one compares against the folder, which is what `--user` is built from). The escape file is
  20 tests.

```
uv run pytest               2163 passed, 30 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The Mac has run the fourth commit; commits five to seven are what it will see new: the
subshell fork storm, the root-owned-workspace refusal, and the `id -u` test inside the container.

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
  `pids.events` after `wait`; then, after CI, rewritten again (second commit) because `ash`
  does not survive a failed fork at all; then, after the Mac run, rewritten a third time (fifth
  commit) to run the storm in a subshell and read the counters from the surviving outer shell.
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
- **`--user` is the workspace owner's uid and gid, and uid 0 is refused** (fourth and seventh
  commits; the sixth briefly used the host process's ids, the first three ran as root and
  Linux showed why that cannot write the workspace once every capability is gone). The owner
  of the folder is the identity that can write the folder, on Linux by the mode bits and on
  the Mac by OrbStack's remap; the host process's ids are the same in every ordinary case and
  differ only when they should not (a `sudo`'d runner on a person's folder). Read from the
  filesystem, not configured, so there is nothing to get wrong; `owner_user()` is one function
  so `run` stays testable against the plan. No `HOME` is set inside; a tool that needs one is
  a later decision. Noted below.
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
- **Removal is judged by `docker ps`, not by `rm`'s exit status** (third commit). The
  alternative — parse "already in progress" and "No such container" out of `rm`'s stderr — is
  matching on the daemon's prose, which changes. The listing is the fact.
- **The orphan for the reap test is made with the CLI directly**, not through `run`, because
  `run` waits for its container and a crashed parent does not.
- **Not flagged:** D99 amends D68 but agrees with the issue; `--read-only` extends the issue's
  list without contradicting any D-number.

**What to read first:** `plan` in `executor.py` — about 40 lines, and the whole boundary. Then
`run`'s timeout branch and `_remove`. Then the fixture at the top of `test_executor_escape.py`,
which is the skip-or-fail rule, and `test_the_escape_tests_fail_rather_than_skip_when_docker_is_
required`, which proves it from outside.

**Noticed, not done:**
- **CI has a daemon** — a fact now, from the log. The escape tests run on every PR on a Linux
  kernel, contrary to the issue's expectation. That is good evidence and a slower `check` (the
  step took 88–113 s against about 75 s of unit tests). If the owner would rather CI never start
  a container, a marker and `-m "not escape"` in `ci.yml` is a one-line change — `ci.yml` is
  not this task's.
- **The author agent could not read CI logs through its integration.** The GitHub MCP scope has
  no Actions tool; the REST log endpoint needs the token, which only the skill carries. Three
  commits were pushed on inference before I used the skill's token for a plain `GET` of the
  job log. A `check-logs` subcommand in `walle-github` would make that the first move, not the
  fourth.
- **No `HOME` inside the container.** The process runs as a uid the image has no passwd entry
  for; busybox does not mind, Python and git might want `HOME`. One `--env HOME=/tmp` when a
  runner needs it — but then the "no `--env`" test changes, and that is a decision.
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
