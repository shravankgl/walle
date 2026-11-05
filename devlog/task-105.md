# Task 105: OS user permissions check — a read-only check, and a Linux test with real users

Issue #105. Branch `task/p3-osusers`, created empty by the owner as the claim and reused here as
his comment on the issue says; from `main` at `3d384ec` (task 102). Phase 3 (R8). Dormant: the
check runs only when the owner sets `WALLE_OSUSERS=on`.

The owner's decision of 2026-11-05 is the spec: **build a permissions check and a Linux test
only.** Nothing shipped creates a user, changes an owner or a mode, or calls `sudo`, `dscl` or
`useradd`. Making the users and handing each folder to its user stays a step the owner runs
himself, if a second person ever uses walle. The test may make throwaway users where it can
(GitHub's Linux runner), must always remove them, and skips — saying why — where it cannot.

**Shipped:**
- `src/walle/osusers.py`, new. The read-only check that D110's layout is in place.
  - `Owner(uid, gid)` and `Owner.parse("user[:group]")`: who a principal's folder must belong
    to. Numbers or names; a name is looked up in the system's user and group databases (`pwd`,
    `grp`, read-only) and a bare user takes its primary group. Owners are **declared, never
    guessed**: the module does not decide who *should* own a folder.
  - `check(root, owners, *, shared=None, environ=None) -> Report`. `lstat`s the root,
    `principals/`, every declared principal's folder and everything in it (`os.walk`,
    `followlinks=False`), and `shared/` when a household gid is given. Three rules, each a mask
    of bits a place must not have: a profile and all its contents `0o077` (nothing for group or
    others); `principals/` and the root `0o066` (pass through, never list or write); `shared/`
    `0o027` (the household group reads, nobody else; nobody but each file's owner writes). A
    link is judged by owner only (Linux reports `0o777` for every link). `principals/` or a
    profile folder that is itself a link, a declared principal with no folder (or a file where
    the folder should be), and a name in `principals/` nobody declared are findings too.
  - `Finding(path, problem, expected, actual, principal)`: the path relative to the root, which
    of `owner | group | mode | link | missing | undeclared`, and what was expected and found as
    short strings (`uid 1001`, `drwxr-xr-x`). Never a byte of a file, never where a link points.
  - `Report(findings, unlisted, checked)`, `Report.clean`. **A folder the check could not list
    is reported as unlisted and the report is not clean.** Run as one principal, the check sees
    every top folder's owner and mode and reports every other folder's inside as unlisted; run
    as root, it sees everything. A check that did not look does not say the household is fine.
  - The switch: `SWITCH = "WALLE_OSUSERS"`, `SWITCH_ON = "on"`, `enabled`, `OsUsersOffError`,
    `OFF_MESSAGE`, one spelling, as the other dormant modules. `REQUIRE_OS_USERS =
    "WALLE_REQUIRE_OS_USERS"`, the constant the Linux test reads; the module itself never does.
- `src/walle/paths.py`: `PRINCIPALS_DIRNAME = "principals"`, used by `principal_dir` and now by
  the check, so the one string lives in one place. No behaviour change.
- `src/walle/cli.py`: `walle profiles owners --root R --owner ada=walle-ada --owner sam=1002:1002
  [--shared-group walle]`. Prints one line per finding and per unlisted folder, then a summary.
  Exit 0 when clean, 1 when there is a finding or something was not listed, 2 for a bad
  `--owner`. Refuses while off with the message that says how to turn it on.
- `tests/test_osusers.py`, 37 test functions (49 with parametrisation), every root a `tmp_path`
  the test process owns; the wrong owner is this process's uid plus one. `tests/test_osusers_isolation.py`, 5 tests with real
  users, below.

**Why:**
- D110: each principal's folder is owned by its own OS user and group, so a process running as
  one principal cannot read another's — the kernel refuses, not a resolver. D32 made isolation a
  layout; task 102 made it a shape of the code; this makes it checkable at the OS level, which is
  where D110 says the Phase 3 guarantee lives. The parents "can be passed through but not
  listed" and "shared paths belong to a common group" are D110's words, and are the two other
  masks.
- The owner's narrowing of D110 for this task — check and test only, no user creation — is a
  narrowing, not a conflict, so nothing was flagged. D110's trusted-command `sudo` rule is a
  Phase 1 connector mechanism and is not touched.
- D86 / "status messages that lie": `unlisted` exists because the obvious implementation (skip
  what you cannot read) would report a household clean after looking at half of it.
- D117: the switch is in the environment, outside the log, where nothing the agent appends can
  reach it.

**Verify:**
- *The check reports a folder with the wrong owner or mode:*
  `test_a_folder_owned_by_another_uid_is_a_finding` (owner and group, every path under the
  folder, the other principal untouched), `test_a_wrong_group_alone_is_a_group_finding`,
  `test_any_permission_for_group_or_others_inside_a_profile_is_a_finding` (five modes, folder
  and file), `test_a_listable_or_writable_principals_folder_is_a_finding` (and the root),
  `test_a_profile_folder_that_is_a_link_is_a_finding_and_is_not_entered`,
  `test_a_principals_folder_that_is_a_link_is_a_finding`,
  `test_a_declared_principal_with_no_folder_is_missing`,
  `test_a_folder_with_no_declared_owner_is_undeclared`,
  `test_shared_is_checked_for_the_household_group_when_one_is_given`,
  `test_findings_are_sorted_by_path_and_carry_no_file_contents`.
- *A Linux CI test with real users proves one principal's process can't read another's folder:*
  `tests/test_osusers_isolation.py`. The fixture makes two system users (`wt<pid>a`, `wt<pid>b`,
  no home, `/bin/false`), a household under `/tmp` (pytest's `tmp_path` sits under a `0o700`
  folder the users could not pass through), hands each folder to its user, and removes users and
  folder in a `finally`. Then:
  `test_the_laid_out_household_is_clean_and_each_user_reads_only_their_own` — the check, run as
  root, is clean; `sudo -u ada cat` reads Ada's stream and is refused Sam's (`Permission denied`,
  the canary absent from stdout); `ls` of Sam's folder and of `principals/` are refused; and
  the other way round.
  `test_the_check_reports_the_mode_that_lets_a_neighbour_read` — Sam's folder and stream are
  loosened to `0o755`/`0o644`; the check names exactly those three paths as `mode`; Ada can now
  `cat` Sam's stream, so **the finding and the leak are the same fact**; restored, both go away.
  `test_the_check_reports_a_folder_handed_to_the_wrong_user` — Sam's folder `chown`ed to Ada:
  `owner` and `group` findings on it, and Ada can list it.
  `test_an_unprivileged_check_sees_the_top_folders_and_says_what_it_could_not_list` — the test
  user's own check: no findings, both insides unlisted, not clean; owners swapped, four findings.
  `test_a_throwaway_user_is_removed_again` — the teardown on its own.
- *No code path calls `sudo`, `dscl` or `useradd` outside the test setup:*
  `test_the_module_spawns_nothing_and_changes_no_owner_mode_or_user` (AST: `osusers.py` imports
  no `subprocess`/`pty`/`multiprocessing`/`asyncio`, calls none of 49 `os` functions that spawn,
  exec, fork, chown, chmod, set an id or write, and no non-docstring string mentions the
  commands), `test_no_shipped_module_spawns_sudo_dscl_or_useradd` (every module under
  `src/walle/`: no spawn call whose arguments contain the three words),
  `test_only_the_isolation_test_names_the_three_commands` (no other test file does),
  `test_the_check_opens_no_file` (`builtins.open`, `os.open`, `os.read`, `Path.open/read_*`
  patched to fail; the check completes with findings), `test_the_check_changes_nothing`
  (`chmod`/`chown`/`unlink`/`rename` patched to fail; every mode the same after).
- *Dormant, default off:* `test_os_user_checks_are_off_by_default`,
  `test_only_the_one_spelling_turns_it_on` (nine spellings),
  `test_while_off_the_check_looks_at_nothing` (`lstat`/`stat`/`listdir`/`scandir`/`walk`
  patched to fail; `OsUsersOffError` first), `test_the_command_refuses_while_off_and_reports_when_on`.
- *A skip cannot pass as a run:* `test_the_isolation_tests_skip_with_the_reason_when_no_user_can_be_made`
  (a subprocess pytest with an empty `PATH`: skipped, names `useradd`, says
  `WALLE_REQUIRE_OS_USERS=1`), `test_the_isolation_tests_fail_rather_than_skip_when_users_are_required`
  (same with the variable set: fails, "proves nothing"),
  `test_the_isolation_module_removes_every_user_it_makes` (by AST: `_remove_user` in a `finally`).

Written first: with no `walle.osusers`, both test files failed at import. After the module, two
tests failed on the tests themselves (click exits 2, not 1, for a bad option; the module-scoped
`household` fixture ran before the function-scoped skip, so the skip is module-scoped now), and
one on the module: `os.walk`'s `onerror` was never reached in this sandbox because the process
holds `CAP_DAC_READ_SEARCH` and can list a `0o000` folder — the skip probe now *tries* the
listing rather than asking `os.access`, which says no while `scandir` succeeds. A mutation check
(`PRIVATE_MASK` set to `0o007`) failed three tests and was reverted.

```
uv run pytest               3177 passed, 35 skipped in 146.94s
uv run ruff check           All checks passed!
uv run ruff format --check  177 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

Of the 35 skips, 4 are this task's and are the capability quirk above (this sandbox lists other
users' folders). With the capabilities dropped (`setpriv --inh-caps=-all --ambient-caps=-all`),
all 49 pure tests and all 5 isolation tests pass with no skip — the conditions of GitHub's Linux
runner, which has passwordless `sudo` and no such capabilities, so the isolation tests **run under
the existing `ci.yml`** without a change to it. The real-user tests were also run here with
`WALLE_REQUIRE_OS_USERS=1`: 4 passed, 1 skipped for the quirk, no user left behind (`getent
passwd` shows none).

**Two-runner walkthrough:** the check takes no lock, writes nothing and reads no log; it
`lstat`s. *Check C running while writer W appends to Sam's stream:* C sees the stream's owner
and mode, which W's append does not change; W sees nothing of C. *Check C while admin A
deletes Sam's profile (task 102):* an entry gone between the listing and the `lstat` is skipped
(`FileNotFoundError` → nothing to judge); a folder gone before its listing is `unlisted`
(`os.walk`'s `onerror`), so the report is not clean, which is right: the household was changing
under it. Nothing C does can affect A or W.

**Stranger pass:** done on the whole diff — IO and error paths throughout. Found and fixed:
- **A `PermissionError` on the root's own `lstat` would have blamed the root's parent**, a
  path outside the root that `relative_to` cannot express (a `ValueError` where a report was
  owed). The root is now blamed as itself.
- **A declared principal whose "folder" is a regular file** was judged by the private mask and
  silently passed `os.walk`. It is now a `missing` finding with `actual = "not a folder"`;
  likewise `principals/` and `shared/`.
- **Teardown could leave files in `/tmp`** if the `chown` back failed: `rmtree` with
  `ignore_errors` would have swallowed it. A second attempt with `rm -rf` under `sudo` follows.
  **`_remove_user` on a user that was never made** would have asserted inside a `finally` and
  masked the real error; it now returns.
- **The skip probe lied** under capabilities (above). Tried, not inferred.
- Looked for and not found: an ignored error (every `OSError` is a finding, an unlisted folder,
  or propagates), a message carrying bytes (findings carry names, modes and ids; the canary test
  checks), a write (the AST test enumerates them), a way for the check to call itself clean
  without having looked.

**Review:** not run before merge. Under the agent build period (R6, R8) the reviewer agent reads
the PR's head commit and sets `agent-review`; this entry is what it and Shravan read.

**Choices made inside the task:**
- **Group bits are forbidden inside a profile, not merely checked against the principal's
  group.** D110 gives each principal their own group, so `0o750` with that group would leak
  nothing today. It leaks the day a second account joins that group, and "nothing for group or
  others" is a rule a reader checks at a glance. Strict, and said so in the module.
- **Owners are an argument, not a lookup.** The obvious alternative — derive `walle-<name>` and
  look it up — invents a user-naming convention, which is the owner's to decide when he makes
  the users. `Owner.parse` only turns what he typed into numbers.
- **The clean assertion in the Linux test runs the check as root.** Only root can list two other
  users' private folders, and the clean verdict needs every folder listed. It is the same code,
  run as the one person D110 lets; the unprivileged run is tested too, and reports the insides as
  unlisted rather than clean. This is a `sudo` in the test file, not in shipped code.
- **`shared/` is judged only when a group is given.** A Phase 0 household has no household
  group; judging `shared/` against nothing would be a guess.
- **The command lives under `walle profiles`** (`owners`), beside task 102's `add|delete|publish`,
  not as a top-level command: it is a question about profiles.
- **`PRINCIPALS_DIRNAME` was added to `paths.py`** rather than hard-coding `"principals"` a
  second time. One line, no behaviour change; `paths.py` is not a CODEOWNERS path.

**What to read first:** `_Check._judge` and the three masks at the top of `osusers.py` — the
whole rule is there. Then `test_the_check_reports_the_mode_that_lets_a_neighbour_read`, where
the finding and the kernel's refusal are shown to be the same fact.

**Noticed, not done:**
- Task 102's `Profile.open` check-then-open gap and the hard-link gap are what D110's users
  close; this task checks the users are in place and does not touch `profiles.py`.
- `principals/` with `0o711` lets a principal `stat` a sibling's folder by guessing the name
  (owner and mode, no contents). Pithagoras hashes the names; walle's `p-<name>` are readable.
  A decision, not a task.
- Nothing yet runs this check on a schedule or at startup. Under the dormant rule it should not,
  until the owner turns it on after reading it.
- `Owner.parse` accepts a numeric uid with no such account. Deliberate (an owner may pre-assign
  ids), but a `--strict` that requires the account to exist would be cheap.

**Cost:** not visible to the author at the time of writing; from Hyperagent's record of this
thread, author side only.
