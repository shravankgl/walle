# Task 69: the approval floors — files that govern permission cannot be written in any mode

Issue #69. Branch `task/p2-floors`, from `main` at `e29426c` (task 70's merge; started on `3cd1633`,
task 68's, and rebased when the claim showed main had moved). Phase 2's third task (R8). Like
tasks 67 and 68, nothing here dispatches, writes or runs anything: this is the check a runner has
to ask before every write, the refusal the gate gives, and the proof that the refusal is the same
whether missions are off or on. The switch is still off.

**Shipped:**
- `src/walle/floors.py`, new, about 300 lines, most of it the docstring. Three things:
  - `PROTECTED`, the floor table (D72, D109, `ARCHITECTURE.md` §7): `kernel/`, `shared/`,
    `principals/*/grants`, `principals/*/keys`, `principals/*/hot`. Each entry says what it is and
    which decisions put it there. `*` is any principal — the agent for one may not write
    another's either (hard constraint 6), and a folder that is not a principal's gets no benefit
    of the doubt.
  - `hit(relative)`, pure: a root-relative path's relation to the table — it **is** an entry, is
    **under** one, or is **above** one (a rename or delete of a parent takes the protected file
    with it, so parents are floored up to and including the root) — or `None`. Segments are
    compared after `posixpath.normpath` and without regard to case; a shared prefix is not
    containment; the names only count where the layout puts them. Absolute paths and paths that
    climb out through `..` get `None`: without a root there is no answer by name.
  - `floored(root, op)`, with a root and a filesystem. `Op` is one write of one `Kind` — `write`,
    `delete`, `rename` (both ends), `link` (the name made and the thing it names). For each end
    it asks `hit` three times: about the path as given (made absolute against the root and
    folded), about every symlink met on the way (walked one component at a time, as
    `sandbox._symlink_hops` does), and about where it lands, dangling links included. Any one
    floors it. A path that cannot be resolved — a link loop, a component that cannot be looked
    at, an embedded NUL — is refused as `unresolvable`. The only filesystem calls are `os.lstat`
    and `os.readlink`; nothing is written. (The first commit used `os.path.realpath` and asked
    twice; the review round below is what changed it.)
- `src/walle/missions.py`: rule 8 of the docstring, and three lines in `admit`: every path in
  `touch.writes` is asked of `hit` **first** — before the switch, before paused, before the node's
  state, before the manifest — so a declared write to a floored path gets the same refusal, word
  for word, with the switch unset, empty, misspelled or `on`.
- `tests/test_floors.py`, 123 tests after the review round: the table pinned; every floor path by name, under, above,
  however spelled; every floor path on disk against every kind of write; the rename and link ends
  named in the refusal; the symlink cases the issue asks for and the ones around them; what passes;
  the sandbox profile; the gate in every mode; nothing can lift it; the dormant rule; the `Op`'s own
  checks; purity.
- `tests/test_missions.py`: one line. The import allowlist for `missions.py` gains `walle.floors`.
  The task requires it: the gate imports the floor. `floors.py` is held to its own allowlist in
  `test_floors.py` (`os`, `posixpath`, `stat` and the standard dataclass/enum/typing set; no model,
  no network, no clock, no `subprocess`, no `walle.*`).
- `tests/test_manifest.py`: two tests from task 68 changed their *example* undeclared write.
  `test_every_undeclared_item_is_in_the_refusal` used `principals/p-ira/keys` and
  `test_a_running_node_may_keep_touching_what_it_declared` used `shared/recipes` — both now floored,
  so the floor speaks before the manifest and the expected sentence no longer matches. Each now uses
  `principals/p-ira/notes`, which the fixture node declares for reading and not writing, so each
  still tests what it says: every undeclared item named; a running node held to its manifest. The
  task requires this change and nothing else in those tests moved.

**Why:** D72 — *"files that govern permission are unwritable in any mode"* — is the one floor
openworker found by being burned: approve one ordinary-looking command, it appends to the rule
file, every later session is more permissive. The point of calling it a floor is that it is not a
property of a sandbox, a rung or a switch; so `admit` checks it before the switch, and `floored`
checks it with no switch to consult. D109 widened the set from "files that govern permission" to
"anything the system loads on its own", and named the set for walle; the table is that list, minus
the entries that have no path yet (below). D32 rule 2 and D67 make the manifest the permission
grant; D72 is the floor under the grant, which is why a node that *declares* `writes:
["principals/p-sam/hot/rules.md"]` is refused anyway — a plan cannot grant what no mode allows.
D86 is why an unresolvable path is refused rather than guessed. D117 is the dormant rule: the floor
is a check, it acts on nothing, and what the switch governs stays off.

**Verify:**
- *A test for every floor path in every mode:* `test_every_floor_path_is_refused_by_every_kind_of_write`
  (5 paths × 4 kinds, each two-ended kind tried from either end) and
  `test_under_and_above_every_floor_path_is_refused_on_disk`, on a synthetic tenant in `tmp_path`;
  `test_every_floor_path_is_refused_at_the_gate` and
  `test_a_declared_write_to_a_floor_path_is_refused_in_every_mode` (switch unset, `{}`, `"1"`,
  `on`: one `Refusal`, compared whole) through `admit`; `test_the_sandbox_writes_only_where_no_floor_is`
  for the `srt` profile — its one writable folder is not floored and every floor path is outside it.
- *Including through symlinks:* a link from outside the root into `hot/rules.md`; a link inside the
  root under an innocent name (`notes -> hot`), which `hit` cannot see and `floored` does; a
  dangling link whose target would be created under `keys/`; `grants/` itself replaced by a link
  pointing out of the root, caught by name; the root reached through a link, in both directions; a
  link loop; a link that cannot be looked at (staged with `lstat`, since this sandbox does not
  honour a `000` folder); a relative path that climbs out of the root and back in.
- *Including through renames:* `hot` renamed away; `scratch` renamed onto `hot`; the principal's
  folder and `principals/` itself renamed; a rename whose source is a link to `keys/`. The refusal
  names which end hit (`rename 'principals/p-sam/hot'` vs `rename to 'principals/p-sam/hot'`).
- *Off by default:* `test_an_ordinary_declared_write_is_still_refused_while_missions_are_off`
  (the floor refuses less than the switch, never more); `test_the_floors_module_never_writes_starts_
  nothing_and_reads_no_clock` (AST: no `open`, `write_*`, `unlink`, `rename`, `chmod`, `symlink`,
  `mkdir`, `run`, `Popen`; import allowlist); `test_the_cli_gains_no_floor_command`;
  `test_nothing_in_the_environment_the_log_or_a_call_can_lift_the_floor` (no `environ`, no switch
  name, no lifting field on `Node`, no `allow`/`force`/`override` parameter on either function).

Written first: with no `walle.floors`, `tests/test_floors.py` failed at import. One test was wrong,
not the code: `test_the_table_is_exactly_these` demanded a D-number on every entry, and the kernel's
reason is hard constraint 1. Two of task 68's tests broke for the reason above and were changed as
described.

```
uv run pytest               2251 passed, 10 skipped in 85.42s
uv run ruff check           All checks passed!
uv run ruff format --check  138 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** the floor opens no file for writing and takes no lock, so there is no
derived file to walk through; what is worth walking is a writer and a mover. Runner A is about to
write `principals/p-sam/notes/plan.md`; person H, between A's check and A's write, replaces
`notes/` with a link to `hot/`. A asked `floored` before the link existed and got `None`; A's write
then lands in `hot/plan.md`. The check is a check, not an atomic write: closing that window needs
the executor to open with `O_NOFOLLOW` on every component or to write through a directory
descriptor, which is D68's container executor's job, not this module's. What this module
guarantees is narrower and stated: at the moment it is asked, it answers for the tree as it is,
and it is asked again at every tool call (`admit` on a `running` node), so a link that appears
between two calls is seen at the second. Two runners asking at once read the same tree and get the
same answer; neither changes it.

**Stranger pass:** done on the whole diff (about 150 lines of `src` that are not docstring, one new
module, every path of it an error path). Found and fixed:
- `os.path.realpath` raises `ValueError`, not `OSError`, for an embedded NUL, so the fail-closed
  branch missed it; caught, and tested.
- `realpath` treats a component it cannot `lstat` (a folder without search permission) as a plain
  name and carries on, and `os.path.islink` swallows the same error, so the first version's
  loop-detection walk would have called such a path resolved — and the refusal text already claimed
  otherwise, a status message that lied. Fixed then with a direct `lstat`; after the review round
  the component walk is the only resolver and the same rule holds: missing is fine (what would be
  made), anything else is unknown and refused. Tested with a patched `lstat`.
Looked for and not found: a way to reach a protected name that neither layer sees (every spelling
goes through `normpath` and lower-casing; every link through the component walk (after the review round); the one
gap is the window above, which is the executor's); a false floor on the agent's own writes (the
log, the index, `plans/`, `hotel/`, `keystone`, a path outside the root, all tested clear); an
`Op` that reaches the check with a missing or extra end (refused at construction); a difference
between `admit` and `floored` on the same relative path (`admit` calls the same `hit`).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run; the brief replaces it for agent work.

**Review round one (head `b63461a`, Gemini 4 Argon, direct review of all six files): two medium,
one low, all fixed on the same branch in a second commit.** Each was a real gap, and each got its
failing test before the fix:

1. *Medium — `floored` asked only about the raw path and the fully resolved one.* A protected
   folder that is itself a link pointing out of the root was caught only when reached by its own
   name. Reached through another hop (`scratch/g -> ../grants`), through a `..` climb-out and
   back (`../walle/principals/p-sam/grants`), across a linked root (`via -> root`, in either
   direction, which is macOS's `/var`), or with a relative root and an absolute path, the first
   check saw an innocent name and the second saw a path outside the root. Four tests, one per
   case, all failing before. Fix: `_walk` resolves one component at a time from the real root and
   returns every link met, each named as itself with its folder already resolved; every hop that
   *is* or is *under* a floor path refuses (a hop *above* one does not — it is read on the way
   through, not written, and a principal folder kept on another disk must still append to its
   log; `test_a_principal_folder_that_is_a_link_does_not_floor_its_log`). Roots and the as-given
   path are made absolute with `abspath` so the lexical check agrees across spellings. One thing
   the reviewer's suggestion would have missed, found while doing it: folding `..` lexically
   *before* the walk hides `scratch/L/../x` with `L -> ../keys/sub`, which the OS reads as
   `keys/x`. So the by-name check folds and the walk does not; `_walk` applies `..` to the
   resolved folder, as the kernel does (`test_a_dot_dot_after_a_link_is_read_as_the_os_reads_it`).
2. *Medium — a `link`'s relative `to` was read from the root.* `os.symlink(to, path)` reads a
   relative `to` from `dirname(path)`, so the very links the symlink tests plant
   (`notes -> hot`, `scratch/g -> ../keys/grocer.key`) passed `floored`. Fix: a relative `to` is
   also walked from the link's folder, that folder resolved first (`alias -> scratch`, then
   `alias/k -> ../keys`), and still from the root, which is what a hard link's `to` means. Three
   tests failing before, one control that an ordinary relative target still passes.
3. *Low — `_relative` compared the root prefix case-sensitively while `hit` folds case.* Now it
   folds too, and returns the original slice. One test.

Fail-closed was kept throughout: a hop that cannot be `lstat`ed or `readlink`ed, a folder with a
file in the way, a NUL, or more than `HOP_LIMIT` (40) links is `unresolvable` and refused. The
`realpath`-based `_real` is gone; the walk is the one resolver, so the loop and permission cases
from the first stranger pass are now covered by the same code path as the new ones. The devlog's
first-commit descriptions above were corrected rather than left to lie. The branch was not behind
`main` (`e29426c`) at push time, so no merge from main was needed.

```
uv run pytest               2262 passed, 10 skipped in 84.38s
uv run ruff check           All checks passed!
uv run ruff format --check  139 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Choices made inside the task:**
- **Parents are floored, for every kind.** The issue says "including renames", and the rename that
  matters most is of the parent: `mv principals/p-sam principals/p-x` moves `rules.md` without ever
  naming it. Once a parent is floored for rename and delete, letting `write` (chmod, utime) through
  on the same parent would be a distinction the agent has no legitimate use for and the reader
  would have to remember. One rule: is, under, above.
- **Case-insensitive matching, everywhere.** On APFS and NTFS `HOT/rules.md` *is* `hot/rules.md`,
  and the host loads the lower-case name. On a case-sensitive filesystem the upper-case path is a
  different, empty folder nothing legitimate lives in. Refusing both costs nothing and removes a
  per-platform answer.
- **Two layers, both kept.** Name-only misses `notes -> hot`; resolution-only misses `hot -> /tmp/x`,
  because the host reads by name. Each test that proves one layer necessary asserts the other
  layer's blind spot (`hit(...) is None`, `found.resolved is None`).
- **The floor before the switch in `admit`.** D72 says "in any mode", and the cheapest proof is that
  the refusal does not mention the mode. It also gives the plan's author the useful reason: a
  floored write means the plan is wrong, where "missions are off" or "paused" is transient. The
  fixed order rule from task 68 is kept and extended, not reordered.
- **`hit` answers nothing about absolute paths.** `admit` has no root (task 68's note: "nothing here
  pins which root"), so an absolute write in a touch passes the name check and falls to the
  manifest; a runner with a root asks `floored`, which handles it. Said in both docstrings rather
  than guessed at with a substring match that would floor `/home/x/notes/kernel/`.
- **Writes only.** Reading `rules.md` is the hot tier's purpose; D109's credential-read rule is a
  different rule with a different matcher and is not this task.
- **No `extra` paths parameter.** A host's hook config outside the root (for Claude Code,
  `~/.claude/settings.json`) is a real floor path with no place in this table; a parameter to add
  it would be a second source of truth for the floor. Left for the decision below.
- **`Op`, not a `Touch`.** `Touch` has reads, writes and egress and no notion of a rename's two
  ends; the floor needs both ends. The runner will build `Op`s from what it is about to do and a
  `Touch` from the same; `admit` sees the `Touch`, `floored` the `Op`s.

**What to read first:** `PROTECTED` and `hit` in `floors.py` (about 40 lines), then the "Two layers"
section of its docstring, then `_walk`, `_check` and `floored`. Then
`test_a_declared_write_to_a_floor_path_is_refused_in_every_mode`,
`test_a_link_inside_the_root_under_an_innocent_name_is_refused` and
`test_a_floor_path_replaced_by_a_link_pointing_out_is_still_refused`, which between them are the
argument for two layers.

**Noticed, not done:**
- **Three floor paths D72 and D109 name have no path in the layout yet:** the watch file (D108), the
  host's hook configuration (outside the root; `~/.claude/settings.json` in the README's install),
  and D72's Phase 2 third floor — files inside a *workspace* that execute later (`.git/hooks/`, CI
  configs, editor task files), which needs a workspace root the layout does not have. Each is one
  line in `PROTECTED` when its location is decided; none is decided here (R6: agents make no new
  decisions). `trust` events are events, not files; the writer is their gate, when the type exists.
- **A plan that declares a floored write is refused only when the step reaches the gate.** Nothing
  tells the mission's author at planning time. The `Mission` validator could refuse such a node by
  name, but that changes what the schema accepts and is a D96 question.
- **The check-then-write window** (walkthrough above) is the executor's to close, with
  `O_NOFOLLOW` or a directory descriptor; D68's container executor is where that belongs.
- **`admit` takes paths as strings and `floored` a root;** when a runner exists, the gate call and
  the floor call should come from one place so a step cannot be admitted without both.
- **PLAN §9's status column** is not updated; `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time; Hyperagent's figure for this thread to be
filled in by the owner.
