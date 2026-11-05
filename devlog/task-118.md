# Task 118: path checks disagree across floors, manifest, missions and grants — one answer

Issue #118, audit findings 1.1, 1.2 and 1.4 (high, high, medium). Branch `task/audit-paths`,
created empty by the owner as the claim and reused here as his comment on the issue says; from
`main` at `834c6d8`. Decisions D32, D72, D109, with D86 for every refusal.

This is the fourth time this class has come up: two modules answering the same question about a
path two ways. So the task was not three patches but one function in one place, read by every
gate, and a test per seam that drives the real modules together so the sides cannot drift again.
Every reproduction in the issue held on `main` (re-run here first) and is a refusal now; nothing
is written on a refusal; every refusal holds with every switch off.

**Shipped:**
- `src/walle/floors.py` — the one answer.
  - `segments(path)` is public (was `_segments`): the one spelling of a path by name, or `None`
    when there is no answer by name — absolute, climbing out through `..`, or holding a NUL.
    Pure. The manifest and the builder read a path through it.
  - `hit(path)` no longer returns `None` for a path it cannot answer about. It returns
    `UNANSWERABLE`, a `Hit` of the new `How` value `"unanswerable"`, whose `reason` is a
    refusal citing D72 and D86. `None` now means exactly one thing: clear.
  - `_check` no longer skips a hop whose hit is `above` a floor path. A linked `principals/` or
    `principals/p-<name>` is refused (finding 1.2), and the refusal names the link: `Floored`
    gains `through: bool` and `Hit.reason` gains `through=`, so the sentence reads *passes
    through a link at …, which stands above …; a folder above a floor path is physical, never a
    link (D32, D72)* rather than the lie *resolves to*.
- `src/walle/manifest.py` — `_normalized` is `floors.segments` joined back: absolute paths and
  NULs now cover nothing and are covered by nothing. `covers_path("/", "/anything")` was `True`
  because `"/".rstrip("/") + "/"` is `"/"` (finding 1.1); it is `False`.
- `src/walle/missions.py` — no code change in `admit`; it already asked `hit`, and `hit` now
  answers. Docstring rule 8 and `admit`'s say so. The reproduction's `admit(... writes=["/"])`
  is refused before the switch, with the floor's sentence, not "missions are off".
- `src/walle/reviewer.py` — `_unanswerable` deleted; `review` asks `hit` and nothing else, so
  the gate and the reviewer give the same sentence for the same path (the seam test asserts
  equality). `posixpath` import gone with it.
- `src/walle/builder.py` — `_inside` reads `floors.segments` for relative/climbing/NUL and keeps
  only what is the shell's business (`~`, a backslash, padding). `posixpath` import gone.
- `src/walle/profiles.py` — `Resolved.plain` (the name landed exactly on itself, compared
  case-folded) and `Profile.open(..., plain=True)`, which refuses a name that is not plain with
  `CrossPrincipalError` before opening. Decided once on the one `Resolved`, acted on with
  `_open_below` and no second resolution: a link anywhere on the way, inside the folder
  included, is refused. The ordinary `open` is unchanged.
- `src/walle/grants.py` — every rule-file read and write goes through that door (finding 1.4).
  `_load(profile, name, model)` and `_append(profile, rule)` take a `Profile`; `_exclusive`
  opens `always.lock` through it too (a linked lock file used to be followed). New public
  `read_plain(root, principal, name)` for `rungs`. `grant` catches `CrossPrincipalError` beside
  `OSError` and degrades to session with the class in the note (D73). Two small truth-keepers:
  `_require_principals` makes a root with no `principals/` a `FileNotFoundError` rather than the
  profile's "cannot be resolved", and `_append` names a file where `grants/` should be
  `NotADirectoryError` — so neither is reported as a link.
- `src/walle/rungs.py` — `load_envelope` reads through `grants.read_plain`; `os.path.islink`
  on the leaf is gone. The module still contains no `open(`, no `mkdir`, no write mode.
- `tests/test_audit_paths.py`, new: the three reproductions as written in the issue, a seam test
  for each, and the nothing-written and switch-off checks. 12 functions, 54 with parametrisation.
- Unit tests beside the modules: `test_floors` (the flipped linked-principal test, `hit` on
  unanswerable paths), `test_manifest` (absolute and `/` cover nothing), `test_builder`
  (`_inside` agrees with `segments`), `test_profiles` (`plain`), `test_grants` (watcher follows
  the new `_append` signature), `test_reviewer` (the round-one test reads the new answer).

**Why:**
- D72 / D109: the floor is the floor in every mode and at every gate. Finding 1.1 was two gates
  reading `hit`'s `None` two ways — the reviewer (PR #87) grew a private `_unanswerable`, the
  mission gate never did. Moving "no answer" into `hit` itself removes the second reader.
- D86: no answer is a refusal. A NUL is now unanswerable by name too, before any `lstat` could
  raise, which is the same answer `floored` used to reach by a longer road.
- D32, hard constraint 6: profiles are physical. `floors` let `principals/p-sam` be a link so a
  profile could live on another disk; `profiles` and `osusers` refuse exactly that. Refusing is
  the safe default and matches D32, so no D-number changes and nothing was flagged. The
  alternative in the issue — track the remaining segments below an `above` hop against the
  pattern — would keep a design the rest of the code already rejects, and would still leave a
  second walker to drift.
- D32 rule 2, D72: a principal's `grants/` is theirs. `Profile` already was the one place that
  resolves a name from `principals/p-<name>` down and opens with `O_NOFOLLOW`; the grants and
  the rungs now use it instead of a leaf-only `islink`. The grants' own stricter rule — a rule
  file is a *plain* file, even a link that stays inside is not read — is now `plain=True`,
  one flag on the one door, rather than a check beside it.
- D73: a grant that cannot be saved degrades to session, never to nothing; a linked folder is
  one more way it cannot be saved, and the note says so by class.

**Verify:** each reproduction a failing test first, then a refusal; one seam test per finding;
nothing written on a refusal; dormant stays dormant.
- 1.1: `test_1_1_the_floor_answers_unanswerable_not_none` (7 paths), `test_1_1_an_unanswerable_declaration_covers_nothing`
  (12 pairs, `("/", "/principals/p-sam/hot/rules.md")` among them),
  `test_1_1_the_gate_and_the_reviewer_refuse_the_same_write_for_the_same_reason` — the seam:
  the node declares `/`, `admit` refuses in every mode with `hit`'s sentence and never with
  "missions are off", `review` blocks in every standing with the same sentence, zero model
  calls. `test_1_1_a_root_relative_write_the_node_declared_is_still_admitted` pins that nothing
  legitimate narrowed.
- 1.2: `test_1_2_a_write_through_a_linked_principal_folder_is_floored` (the issue's five
  operations: `notes -> hot` on the other disk, `scratch/up -> ..` into `hot/` and `grants/`,
  the delete of the folder, and the log), `test_1_2_the_floor_and_the_profile_give_the_same_answer_to_a_linked_folder`
  — the seam: `Profile.path` and `floored` both refuse a linked `p-sam`, and both refuse a linked
  `principals/`. `test_1_2_a_real_principal_folder_is_still_not_floored`.
- 1.4: `test_1_4_a_linked_grants_folder_grants_nothing_and_is_written_nothing` (on: `Refused`
  with the link named, `grant(ALWAYS)` degrades with `CrossPrincipalError` in the note; off:
  `Refused` with no problems, `GrantsOffError`; both: sam's folder holds exactly its two files
  after, ada's exactly the link), `test_1_4_a_linked_grants_folder_is_no_envelope`,
  `test_1_4_a_linked_principal_folder_is_refused_the_same_way` (`p-ada -> p-sam`),
  `test_1_4_the_grants_and_the_profile_give_the_same_answer` — the seam: what `Profile.path`
  refuses, `check` and `load_envelope` refuse; what it resolves, they read (sam's own config
  allows, sam's envelope parses). `test_1_4_a_rule_file_that_is_a_link_inside_the_folder_is_still_not_read`.
- Dormant: the two `EVERYTHING_OFF` cases above; `floors` has no switch and the existing
  `test_nothing_in_the_environment_the_log_or_a_call_can_lift_the_floor` still inspects
  `floored` and `hit` by signature and by AST.

Written first. Before any source change: `tests/test_audit_paths.py` failed at collection
(`floors.segments` did not exist) and the 20 new or changed unit tests failed — `hit` returned
`None`, `covers_path("/", …)` was `True`, `_inside` had no shared answer, `Resolved` had no
`plain`. After the source change everything passed except three in `test_grants.py`, two of
which were the code lying and were fixed in the code (below), and one which watched
`_append`'s old signature.

```
uv run pytest               4033 passed, 35 skipped in 129.14s (0:02:09)
uv run ruff check           All checks passed!
uv run ruff format --check  194 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

`main` moved to `d6a6c23` (#117, builder hardening) while this was being written, and the
PR reported a conflict in `builder._inside`: #117 added a length cap and its own
`posixpath.isabs(...) or "\x00" in path` line, this task moved exactly those two tests into
`floors.segments`. Resolved by keeping #117's length cap and this task's `segments` call, and #117's new tests
pass against it. The `posixpath.isabs(...) or "\x00" in path` line stays as #117 left it:
`segments` makes the same two tests one line later, so the line is redundant, but changing
it puts this task's hunk directly against #117's insertion and git calls that a conflict,
and the only tool this agent may push with makes single-parent commits — no merge commit, no
force. Keeping the line is what lets the branch merge; it is listed under *Noticed*. `main`
merged in, the four checks again:

```
uv run pytest               4055 passed, 35 skipped in 128.32s (0:02:08)
uv run ruff check           All checks passed!
uv run ruff format --check  196 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

The issue's own reproduction script, run against the branch: `covers_path("/", …)` is `False`;
`admit` returns the unanswerable refusal on and off; `review` is `blocked`; the four `floored`
calls return `Floored(how="above", through=True, resolved=…/principals/p-sam)`; `grants.check`
for ada is `Refused` naming the link, `load_envelope` is the problem string, `grant(ALWAYS)`
degrades, and `sam/grants/always.jsonl` does not exist afterwards.

**Tests edited to pass, and why the task required it:**
- `test_floors.py::test_a_principal_folder_that_is_a_link_does_not_floor_its_log` pinned the
  behaviour finding 1.2 reverses. Renamed `…_floors_everything_under_it` and inverted, with the
  issue's `notes -> hot` layout added and the `through` sentence asserted.
- `test_floors.py::test_by_name_cannot_tell_for_a_path_that_leaves_or_is_absolute`,
  `…::test_a_relative_path_that_climbs_out_and_back_in_is_refused`,
  `test_reviewer.py::test_round_one_a_write_path_the_floor_cannot_answer_about_is_blocked` each
  asserted `hit(path) is None` — the exact ambiguity 1.1 removes. They now assert
  `how == "unanswerable"`; the reviewer test keeps its `"cannot be answered"` check, which the
  new sentence satisfies.
- `test_floors.py::test_a_path_that_cannot_be_a_path_is_refused_not_raised`: a NUL path is
  `unanswerable` by name now, before `floored`'s walk could call it `unresolvable`. Still a
  refusal; the assertion names the new `how`.
- `test_manifest.py`: `("/tmp/walle", "/tmp/walle/out.txt")` moved from the covers list to the
  does-not-cover list (1.1's fix is exactly that absolute covers nothing); the module's allowed
  import set gains `walle.floors`.
- `test_grants.py::test_nothing_in_the_module_writes_the_config_file`: the watcher takes
  `_append`'s new `(profile, rule)` signature and records the same path it did.

**Two-runner walkthrough** (the always file, under its lock; the module docstring carries it):
A resolves `grants/`, makes it if missing, `lstat`s it as a folder, opens `always.lock` through
`Profile.open(plain=True)` (resolve → plain check → `O_NOFOLLOW` open relative to the folder's
descriptor) and takes `flock(LOCK_EX)`. B blocks there, having opened only its own lock
descriptor. A opens `always.jsonl` the same way, appends one line, `fsync`s, closes, releases.
B wakes, opens the same way, appends after A's line, `fsync`s, releases. Two whole lines in
either order. A reader (`check`) takes no lock, as before. If a link appears at `grants/` or at
the file between any two of A's steps: `mkdir` sees `FileExistsError` and goes on, `lstat`
finds a link and raises `NotADirectoryError`, or `Profile.open`'s resolve lands elsewhere and
`plain` is false, or `_open_below` meets it with `O_NOFOLLOW` — each is an exception, the
grant degrades to session, nothing is written anywhere. `test_two_kernels_saving_always_at_once_leave_two_whole_lines`
still runs eight threads through this path.

**Stranger pass** (the diff is ~370 lines and touches IO and error paths):
- *Status messages that lie, found twice and fixed in code, not tests.* With `Profile` doing the
  resolving, a root with no `principals/` at all surfaced as `CrossPrincipalError` ("cannot be
  resolved"), which `_load` would have reported as *a link*; and a plain file where `grants/`
  should be hit `_open_below`'s `ENOTDIR → CrossPrincipalError` mapping ("a link now stands
  where a folder was"), also not true. `_require_principals` and the `lstat` in `_append` name
  each for what it is (`FileNotFoundError`, `NotADirectoryError`), so the existing tests on
  those notes pass unchanged and the person reads the right class.
- *A contradiction left in the floors docstring:* the bullet still said a hop above "does not
  count" one sentence before saying it is refused. Rewritten.
- *Duplicate check:* `_append` and `_open_plain` each tested for a missing `principals/` with
  different filenames in the error. One helper now.
- *Check-then-use:* `_append` resolves `grants/` once for the `mkdir`, then `Profile.open`
  resolves again for the lock and the file. The first resolve only decides where `mkdir` goes;
  every open re-decides on its own `Resolved` and acts on that with no further resolution, so a
  link that appears between is refused, not followed (walkthrough above). `read_plain`/`_load`
  have one resolve and one open.
- *Ignored errors:* `contextlib.suppress(FileExistsError)` around `mkdir` is the only suppressed
  error, and the `lstat` right after re-judges what is there.
- *Scope of the hop change:* dropping `found.how != "above"` can only newly refuse hops at
  `principals` or `principals/<x>` — the only names `above` matches — plus the root, which a
  hop cannot be (the root is already resolved). Checked by reading `_against` and `_relative`.
- *What else called `hit` and relied on `None` for unanswerable:* only `missions.admit` and
  `reviewer.review` (grep); `floored` folds against the root before asking, so it never passes
  an absolute or climbing path to `hit` — except a NUL, now caught earlier (test updated).

**Review:** no independent reviewer ran before this PR; under R6 the reviewer agent reads the PR
head on GitHub. The author-blind pass above is the author's.

**Choices made inside the task:**
- *Refuse a linked principal folder rather than track segments across the hop.* The issue
  offered both; the owner's steer and D32 pick refusing. It is also the smaller change and
  removes a second link-walker rather than growing one. A household that wants a profile on
  another disk mounts it there; it does not link to it.
- *`segments` + `hit` rather than making the manifest and builder call `hit`.* The manifest
  and the workspace rule need "is this a path by name", not "is it floored" — a declared write
  of `principals/p-sam/hot` is still *covered* by the manifest; the floor refuses it first. So
  the shared primitive is the spelling, and `hit` is the one floor verdict built on it.
- *`plain=True` on `Profile.open` rather than a wrapper in `grants`.* A wrapper that resolves,
  checks, then calls `open` (which resolves again) has a window in which a link placed between
  the two resolutions is followed. The flag puts the check on the one `Resolved` that is opened.
- *`rungs` imports `CrossPrincipalError` from `profiles` and reads via `grants.read_plain`*
  because `test_rungs` forbids `open(` and `mkdir` in the module's source, which is a good rule
  to keep.
- *`grants.check` while off still constructs a `Profile`.* `bind` validates the name and
  resolves the root path (readlink, no open), the same cost `grants_dir` had minus the
  readlink; the "no file is opened while off" promise holds and the off-case test proves it.
- *`sandbox.py` untouched* (it needs a Mac run). `floors._walk`, which `sandbox` is documented
  against, is unchanged; only `_check`'s reading of the hops changed.

**What to read first:** `floors.hit` and `segments` (fifteen lines, the whole of 1.1), then
`_check`'s hop loop (one condition, the whole of 1.2), then `profiles.Resolved.plain` and
`grants._append` (1.4), then `tests/test_audit_paths.py` top to bottom — the three seam tests
are the contract.

**Noticed, not done:**
- Builder workflow paths are workspace-relative, but a `Mission` built from them is checked by
  `missions.admit` with `floors.hit`, which reads root-relative names. `run_step` refuses a
  workspace inside the root, so no floor path can be reached, but a workspace folder named
  `kernel` would be refused by name for no reason. A `Node` could carry where its paths are
  relative to.
- `grants.check` on a root with no `principals/` folder used to report no rules and no problem;
  now `_require_principals` keeps that (`FileNotFoundError → ()`). But a `principals/` that is
  a regular file is reported through the link sentence ("or its folder cannot be resolved"
  was added so the sentence is not false). A broken root deserves its own words.
- `builder._inside` keeps its own `posixpath.isabs`/NUL line in front of `segments` (above):
  one redundant line, there only so the branch merges with #117 without a merge commit. Drop
  it, and `import posixpath`, in the next builder task.
- `profiles._open_below` maps `ENOTDIR` to "a link now stands where a folder was", which is
  also what a plain file there produces. Worth a `NotADirectoryError` branch of its own.
- `Context.__post_init__` resolves the root with `Path.resolve()`, following links in the root
  path itself. Every module agrees on that today; it is one more place a linked root is decided.
- The one-line `_unanswerable` in `reviewer` was added in PR #87 alone; a grep for other private
  path helpers (`_inside`, `_normalized`, `_segments`) is what found the rest. A constraint test
  that no module but `floors` calls `posixpath.normpath` or `isabs` on a manifest path would
  stop the fifth occurrence. Not added: it is a new rule, and this task fixes a class, not
  the policy.

**Cost:** not visible to the author from inside this run; the owner reads it from Hyperagent.
