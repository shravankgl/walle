# Task 130: Audit — container links, index links and the WIP cap

Issue #130, the second read-only audit's three findings. Branch `task/audit-chain2`, created
empty by the owner as the claim and reused here as his comment on the issue says; from `main`
at `8af004b`. Author: walle-author-fable (Fable 5.1). The findings were not independently
re-run by the auditor, so each was written as a test first against `8af004b`: **all three
reproduce** — 28 of the 37 cases in `tests/test_audit_chain2.py` failed on main's `src/`,
and the 9 that passed are guards for properties main already had, kept so the fix cannot
break them. Each finding is now a refusal that writes nothing, or the right result.

The three properties, and where each now lives:

1. **A link in the workspace is read as the container will read it.** `floors.escape(root,
   path)` walks a declared write from the resolved workspace and refuses any link whose
   target is absolute, any `..` that climbs above the workspace (even if the path comes back
   in), any walk that cannot finish, and any landing not strictly inside. `builder._contained`
   asks it for every declared write, after `_linked_floor` has had its say. And `run_step`
   calls `executor.check_workspace` before `missions.lock` is taken, so a relative or missing
   workspace is a `ValueError` with nothing in the log.
2. **Every file the index touches goes through the profile's resolve, plain.**
   `profiles.INDEX_FILES` is `index.sqlite`, `index.lock` and the SQLite sidecars
   (`index.SIDECARS`); `Profile.records` resolves each and refuses any that met a link —
   inside the folder or out. `walle.index` holds the same line for callers that reach it
   without a `Profile`: the lock is opened `O_NOFOLLOW` (`_open_lock`), and `_plain` refuses a
   linked `index.sqlite` or sidecar before anything is opened or removed, in the builder and
   both readers.
3. **One add binds one workflow.** `automations.refusal`, the one check the writes and the
   fold share (rule 6), allows at most one ref in an add's `caused_by` that is not a retire
   the register holds, beside at most one retire. A line naming eight workflows is reported
   like an eighth add, never applied, so `builder._automation` finds none of them in the
   register.

**Shipped:**

- `src/walle/floors.py`: `escape(root, path) -> str | None`, new and public. Its own walk,
  not `_walk`, because `_walk` follows an absolute target to where *this* process finds it —
  the right answer for a floor and the wrong one for a container. Same filesystem calls as
  the rest of the module: `lstat` and `readlink`, nothing written. Module docstring gains a
  paragraph naming it as the third reader of a path here.
- `src/walle/builder.py`: `_contained(workspace, writes)` calls `floors.escape` per declared
  write and names the write in the refusal. `run_step` resolves the workspace through
  `executor.check_workspace` before `_overlaps` and before the lock; `_contained` runs after
  `_linked_floor`, before the running-node check. Docstrings (module rule 4, `_linked_floor`,
  `run_step`) say what each check answers and what raises before the lock versus after the
  appends. The builder still uses `os` only for `environ`.
- `src/walle/profiles.py`: `INDEX_FILES`; `Profile.records` resolves each and requires
  `Resolved.plain`. Module docstring (Fusion) names the rule.
- `src/walle/index.py`: `SIDECARS` (one list, used by `_remove` and by profiles);
  `_plain(path)` in `_build`, `load_records` and `ancestors`; `_open_lock` maps `ELOOP` to a
  `ValueError` naming the lock; `_exclusive` and `_open_for_shared_lock` open through it with
  `O_NOFOLLOW`. `_exclusive` now closes its descriptor in a `finally`, as the `with` did.
- `src/walle/automations.py`: the two new refusals in `refusal` for an add; rule 2 of the
  module docstring says so.
- `tests/test_audit_chain2.py`, new: 26 test functions, 40 cases. Per finding: the
  reproduction as the auditor wrote it, the cases around it, a seam test that drives the real
  modules together (`builder`+`floors`+`executor` through a symlinked workspace and the fake
  docker client; `digests.run` through `Profile.records` with a linked index; the real
  `automations.add` swap path and `builder._automation`), and a source-level test that pins
  where the rule lives (`check_workspace` before `missions_lock_path`; one `os.open` of the
  lock, every flag set `O_NOFOLLOW`; `caused_by` counted only in `refusal`, `_apply`, `add`).
- `tests/test_builder.py`, edited (the task requires it; see *Choices*): the pinned list of
  `executor.*` calls in `builder.py` gains `check_workspace`.

**Why:**

- **D68 (the container boundary) with D72 (the floor).** `executor.plan` mounts the workspace
  at `/workspace`; the floor's walk resolves on the host. Those are two filesystems with one
  shared folder, and a link is a string each resolves for itself. The only strings both read
  the same are relative ones that never leave the shared folder, so that is the only shape a
  step may write through. Refusing the rest costs nothing legitimate: a step that needs a link
  elsewhere needs something the container cannot have.
- **D86 (fail closed at the seam).** A workspace the executor would refuse was already
  refused — after `Mission` and `Step(RUNNING)` reached the log. The seam is the lock; the
  check now sits before it.
- **D28 / D32 / hard constraint 6.** `Profile.records` is the handle fusion reads through so
  that "never crosses principals" is a filesystem fact. It resolved the log and left the index
  — the file that holds the same facts, derived — to `index.update`, which followed any link.
  Sam's rows in Ada's digest is exactly the leak D32 made physical to prevent. The index-side
  checks are there because `index.update` is also called directly (the builder, the CLI).
- **D6 / hard constraint 9.** The cap is on automations; a workflow runs only while an active
  automation names it (#121). One add naming eight workflows is eight automations in one slot.
  The rule belongs in `refusal` because rule 6 says the writes and the fold share one check,
  and the line that matters is the one the API cannot write — a second device, a hand-written
  line — which only the fold sees.

**Verify:** the issue's criterion — a failing test for each finding first, then a refusal or
the right result, nothing written to the log on a refusal.

Before (tests at this commit, `src/` and `tests/test_builder.py` at `8af004b`):
`28 failed, 9 passed` in `tests/test_audit_chain2.py`. Every reproduction failed: finding 1's
`plans -> /workspace/kernel` ran the container; the relative workspace left a mission and a
`running` step; finding 2's linked `index.sqlite` returned Sam's rows and wrote Ada's event
into Sam's file, and the linked `index.lock` created a file in Sam's `hot/`; finding 3's add
with eight refs was active and `_automation` let every ref run. (The `-journal` sidecar case
failed differently on main: SQLite raised `OperationalError` through the dangling link.)

After:

```
uv run pytest               4255 passed, 35 skipped
uv run ruff check           All checks passed!
uv run ruff format --check  207 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

`executor.py` and `tests/test_executor_escape.py` are untouched, so no daemon run is needed
and auto-merge is on.

**Two-runner walkthrough:**

- *`run_step`, writers A and B on the same step.* Unchanged in order: both call
  `check_workspace` (a `stat`, no lock), then A takes `missions.lock`; B blocks. A reads the
  index, decides, runs `_linked_floor` and `_contained` (lstat/readlink on the workspace, no
  lock, no write), appends `Mission` and `running`, releases. B wakes, re-reads, finds the node
  `running`, refuses. A workspace that fails `check_workspace` now raises before either takes
  the lock, so `missions.lock` is not even created for it. A link that appears in the workspace
  *after* `_contained` and before the container starts is the same race `_linked_floor` already
  had; the container is the enforcement of record there (D68), this is the gate.
- *`Profile.records`, A building while B links.* A resolves `log/`, each stream and the five
  index names (lstat/readlink, no lock), then `index.update` takes `index.lock` and `_plain`
  re-checks `index.sqlite` and the sidecars under it. B replacing `index.sqlite` with a link
  between A's resolve and A's lock is caught by `_plain`; after A's lock, A holds the file open
  by descriptor and B's link changes a name, not A's file; the next build sees the link and
  refuses. A link at `index.lock` itself is refused by `O_NOFOLLOW` at the open — there is no
  window where the lock is taken through it.
- *`index.update` and `load_records`, the existing dance.* `_exclusive` and `_shared` take the
  same lock in the same order as before; only how the descriptor is opened changed. A reader
  that cannot create the lock still reads unlocked, as before; a reader that finds a link
  there raises instead, which is new and deliberate.
- *`automations.add`, A and B adding at once.* Unchanged: `refusal` is pure and runs under
  `automations.lock` on each side; the new checks read only the event and the register in hand.

**Stranger pass:** diff is ~210 lines over five source files, touches IO and lock scope, so
the re-read was done. Found and fixed: (1) a first draft of the walk lived in `builder.py`
with its own `lstat`/`readlink`; `test_the_builder_has_no_way_to_start_a_process_of_its_own`
pins the builder to `os.environ` only, and the walk belongs with the other path reader anyway
— it moved to `floors.escape`. (2) The first draft refused a `..` that reached the workspace
itself, which broke `plans -> sub/../notes`, a relative link that stays inside; the rule is
*above* the workspace, and the landing check keeps "strictly inside". (3) `os.path.islink`
swallows `OSError`, so the "cannot be resolved" branch was dead; the walk uses `lstat` like
`_walk`, with `FileNotFoundError` meaning "not there yet" and any other error a refusal.
(4) `_plain` in `index.py` first checked only `index.sqlite`; a linked `-journal` is where
SQLite writes by name, so the sidecars are checked too, with a test. Considered and left: the
`_exclusive` descriptor is now closed in `finally` rather than by a `with` — same behaviour,
stated.

**Review:** per `.github/agent-brief.md`, the reviewer is a separate agent on another vendor's
model; it sets `agent-review` on the PR's head commit. `scripts/review.sh` was not run (R6
replaces it for agent work). Nothing here is author-as-reviewer beyond the stranger pass above.

**Choices made inside the task:**

- **Refuse every absolute link, not only ones that land somewhere bad.** The auditor's
  suggestion was to refuse hops that are absolute or land outside, "as `Profile.resolve`
  does"; the owner's framing is to reason from what the container sees. An absolute target
  that the host resolves to somewhere inside the workspace (`plans -> <host ws>/notes`) is a
  path that does not exist in the container at all — so it is refused too, and tested.
- **`floors.escape`, not a change to `_walk` or a reuse of it.** `_walk` answers the floor's
  question (where does the host land it) correctly; the container's question is a different
  one with a different answer for the same link. Two small walks that each say what they
  answer beat one with a mode flag.
- **`_linked_floor` still runs first, unchanged.** It is what refuses `plans -> kernel` as
  "floored (D72)", which existing tests and the mission gate's wording rely on; `_contained`
  then catches what the host cannot see. Order is stated in the docstrings.
- **`check_workspace` raises; it does not become a `Refusal`.** The docstring contract for a
  caller's mistake (malformed ref, unsafe name) is `ValueError` before any lock; a bad
  workspace is the same kind of thing. The root-owned check (`owner_user`) still happens in
  `executor.plan`, after the appends, and the docstring says so — it needs the resolved folder's
  owner and is the executor's, not the runner's; see *Noticed*.
- **The index-side checks exist beside the profile's.** The issue's suggestion was both; the
  owner's instruction was the profile. `index.update` is called directly by `builder.run_step`
  and the CLI, so without `_plain` and `O_NOFOLLOW` a linked index would still be followed on
  those paths. The errors there are `ValueError` (the module's existing shape for "a file we
  cannot name"), not `CrossPrincipalError`, which lives in `profiles` and imports `index`.
- **Finding 3 counts refs by what the register knows,** not by event type: `refusal` is pure
  over a register and an event and has no records. A ref that is some entry's `retired_ref` is
  a swap; anything else is a workflow. Two retires are refused as well — a swap is one for one.
- **Existing test edited.** `test_the_builder_has_no_way_to_start_a_process_of_its_own` pins
  the exact set of `executor.*` calls in `builder.py`; adding `check_workspace` is the task.
  The comment there says why it is safe (a `stat`, no process).
- **No D-number conflict found,** so `flag` was not used. Nothing here changes a decision;
  each is an enforcement of one already made.

**What to read first:** `floors.escape` (sixty lines with its docstring; the whole of finding 1's rule) and
`test_1_a_relative_link_that_leaves_the_workspace_is_refused_even_if_it_comes_back` — the
`../ws/notes` case is the one that shows why "lands inside on the host" is not enough. Then
`Profile.records` and `test_2_index_sqlite_linked_to_another_principal_s_index_is_refused_untouched`,
which builds the exact state the auditor described (matching stream heads) and checks Sam's
file byte for byte.

**Noticed, not done:**

- `executor.owner_user` (the root-owned refusal) still runs after `Mission` and `running` are
  appended, so a root-owned workspace leaves a `failed` step in the log. Moving it before the
  lock is a one-line call in `run_step` and a docstring change; left because the issue named
  `check_workspace` and the test that pins the executor call list would grow again.
- `executor.check_workspace` resolves and `_contained` walks from the resolved folder, but the
  host's `realpath` and the container's view can still differ for a *bind mount inside* the
  workspace (a second mount, not a link). Nothing in the layout does that today; worth a line
  in `executor`'s docstring when it matters.
- `index._open_for_shared_lock`'s unlocked fallback (`_CANNOT_CREATE`) now sits beside a
  `ValueError` for a link. A reader on a read-only folder with a *link* at `index.lock` gets the
  error, not the fallback; that is the right order, but the docstring of `_shared` could say so.
- `tests/test_automations.py` has `test_the_fold_is_pure`-style name lists in other test files
  for other modules; `floors.escape` is not in `test_floors.py`'s signature checks
  (`test_nothing_in_the_environment_the_log_or_a_call_can_lift_the_floor` iterates `floored`
  and `hit` only). Adding it is a one-line test change outside this diff.
- #128 is changing `writer.py` and `reader.py`; neither is touched here, as asked.

**Cost:** author (Fable 5.1, this thread) — see Hyperagent's usage for thread
`cmw4ae4tg02ky07ad2uvpg835`; reviewer — in its own log.
