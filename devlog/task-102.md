# Task 102: household profiles — a second principal, roles, a shared folder, and no read path across

Issue #102. Branch `task/p3-profiles`, from `main` at `a0a715f` (task 94's reviewer fix). Phase 3
(R8). Dormant: nothing that writes runs unless the owner sets `WALLE_PROFILES=on`; the parts that
*refuse* have no switch at all.

The owner's decision of 2026-11-05 is the spec: **nobody reads another principal's folder, not
even an admin. Only the folder's owner can share, and an admin can delete a profile but not read
it.** The task is to make that a shape of the code rather than a check in it.

**Shipped:**
- `src/walle/profiles.py`, new.
  - `Context` and `bind(root, principal, device=, role=)`: who a job acts for (D113). Validated
    whole on construction: names through `paths.check_name`, an empty or whitespace principal is
    `EmptyContextError`, never a default. `Context.writer()` needs a bound device. `run(context,
    job)` and `spawn(context, job)` are the one helper D113 asks for: both refuse `None`, and
    refuse a string that merely looks like a principal, *before* the job's first line. There is
    no `current()`, no context variable, no `DEFAULT_*`, and the module reads the environment for
    exactly one thing, the switch — a test checks this by AST.
  - `Profile(context)`: everything a run may touch of its own principal. Its constructor takes a
    `Context` and nothing else (checked by signature). `path(relative)` refuses, with
    `CrossPrincipalError`, anything absolute, empty, or climbing out by name, then walks the path
    one component at a time following every symlink by hand (the walk `floors.py` uses), and
    refuses anywhere the walk stands that is not inside the profile's real folder or an ancestor
    passed through on the way down an absolute link: a link to another principal's file, a link
    to their folder, one's own `hot/` replaced by a link, a dangling link whose target would be
    *created* in another folder, a link loop (unresolvable, so refused), and the profile folder
    itself replaced by a link. `open` is `open` on `path`. `records()` is the index of this one
    folder, and it passes `log/` and every stream file through `path` first, so a stream that is
    a link into another folder is refused before the index reads a byte. `resolution()` is the
    fold of `records()`. The role is on the context and `path` does not read it.
  - `Role`: D27's three, `operator`, `stakeholder`, `admin`. Not stored: the kernel attaches a
    role to a context per message (D61, ARCHITECTURE §7), and this module keeps no roster.
  - `add_profile(context, principal)` and `delete_profile(context, target)`: the admin's two
    powers, both whole-folder, both behind the switch. Add makes `principals/p-<name>/log`, empty,
    and never reuses an existing folder. Delete removes the folder by name through file
    descriptors — the folder opened `O_NOFOLLOW|O_DIRECTORY`, each child folder likewise before it
    is entered, `unlink` and `rmdir` relative to the descriptor — so it **opens no file** and
    cannot be steered through a link swapped in under it. A profile folder that is itself a link
    is refused (`ELOOP`), so nothing behind the link is touched. Returns counts by name.
  - `publish(context, source, kind)`: copies one capability file from the publisher's **own**
    folder to `shared/<kind>/`. The source goes through `Profile.path`, so there is no call that
    publishes someone else's file, whatever the role — that is how "only the owner can share" is
    structural. Kinds are `skills`, `mcp`, `hooks` (D33); a source that is data by place
    (`log/`, `hot/`, `grants/`, `keys/`, the index and its lock) or by shape (`.jsonl`,
    `.sqlite`, `.sqlite-wal`, `.sqlite-shm`, `.lock`) is `SharedDataError`. A name already in
    `shared/` is never replaced (hard constraint 2). `shared/` or `shared/<kind>` as a link is
    refused, and the source is opened `O_NOFOLLOW` so the open agrees with the check.
  - `Shared(root)`: knows capability folders only; it has no notion of a log or a memory under
    `shared/`. `audit_shared(root)` lists, by name, anything under `shared/` that D33 does not
    allow: a top-level entry that is not a kind, any link, any data-shaped file. `os.walk` with
    `followlinks=False`, so a link in `shared/` into a profile is reported, never entered.
  - The dormant switch: `enabled`, `SWITCH = "WALLE_PROFILES"`, `SWITCH_ON = "on"`,
    `ProfilesOffError`, `OFF_MESSAGE`, the same one-spelling rule as the other dormant modules.
- `src/walle/cli.py`: `walle profiles add|delete|publish`. `add` and `delete` take `--as <who>`
  and `--role`; `publish` takes `--principal`, `--kind` and a source path. Each refuses while off
  (exit 1, says how to turn it on, touches nothing). No `read`, `show` or `cat` command exists.
- `tests/test_profiles.py`, 59 tests (one of them eleven spellings by name, every role). Every root is a `tmp_path`; the principals are a made-up
  Ada and Sam; the canary is a string that reads as not real.

**Why:**
- D32 made D28 a filesystem fact for one principal. With a second one, the holes are the ones
  D113 and the floors review named: a job that forgot who it is, and a path that *resolves* into
  another folder even though it is *spelled* inside one's own. `Context` closes the first,
  `Profile.path` the second.
- D113, in its own words: the default profile is the most likely wrong answer, not the safest
  one. So a missing context raises, and nothing in the module can supply one.
- D33: `shared/` is the how, never the what. `Shared` cannot even spell a data path there, and
  `publish` refuses data by place and by shape rather than trusting the caller's label.
- D27: three roles. The owner's decision narrows what admin means here to adding and ending a
  profile. Nothing a role could unlock for reading exists, so there is nothing to misconfigure.
- D32 rule 5: deleting a person is deleting a folder. Done without a read, so the one person
  who may end a profile still cannot see it.
- D117: the switch is in the environment, outside the log, where nothing the agent appends can
  reach it.

**Verify:**
- *Fusion can't cross principals:*
  `test_a_curate_run_for_one_principal_fuses_only_that_principal_s_beliefs` — Ada's pair fuses
  under the fake Curator; Sam holds a third that would fuse with them if anything put the two
  folders side by side. Every staged proposal's `caused_by` is within Ada's refs and Sam's marker
  word never reaches Ada's queue; Sam's log stays one event with nothing staged.
  `test_a_profile_s_records_are_its_own_folder_s_and_nothing_else` — `Profile.records` is one
  folder's index, equal to `index.update` for that principal.
  `test_nothing_in_the_curator_takes_two_principals` — by signature, no public function in
  `curator` or `profiles` takes two.
- *A job with an empty context fails closed:*
  `test_a_job_started_with_no_context_raises_before_it_runs` (`run(None)`, `spawn(None)`, the job
  never runs), `test_a_context_with_no_principal_cannot_be_made` (`""`, `" "`, `"\n"`, and unsafe
  names), `test_a_wrong_object_in_place_of_a_context_is_refused` (a bare principal name, a tuple,
  a dict, an object), `test_a_job_that_writes_needs_a_bound_stream`,
  `test_a_spawned_job_runs_with_the_context_it_was_given_and_no_other`.
- *One principal's run can't read another's folder:*
  `test_no_path_reaches_another_principal_s_folder_by_name` (eleven spellings, every role),
  `test_no_path_reaches_another_principal_s_folder_through_a_link` (nine links, every role; the
  canary never appears in a message), `test_a_principal_folder_that_is_itself_a_link_is_refused`,
  `test_a_stream_file_that_is_a_link_into_another_folder_is_never_indexed`,
  `test_a_profile_s_paths_are_all_inside_its_folder`.
- *An admin role gets no read path into another principal's folder:*
  `test_an_admin_s_profile_is_its_own_folder_like_any_other`,
  `test_no_function_takes_a_role_and_another_principal_to_read` (by signature: only the two
  whole-folder powers take a context and another name; `path` and `open` have no role
  parameter), `test_an_admin_deletes_a_profile_without_opening_a_file` (`builtins.open`,
  `Path.open`, `Path.read_bytes`, `Path.read_text` and `os.read` all patched to fail; the delete
  completes; a link out of the deleted folder is removed as a link and its target is untouched),
  `test_only_an_admin_deletes_and_only_a_principal_s_folder`,
  `test_deleting_a_profile_whose_folder_is_a_link_removes_nothing_behind_it`.
- *Only the owner shares; shared/ holds capabilities only:*
  `test_publication_reads_only_the_publisher_s_own_folder` (every role, by name and through a
  link; the canary never reaches `shared/`), `test_publication_never_replaces_what_is_already_shared`,
  `test_data_shaped_files_cannot_be_published` (nine sources),
  `test_only_capability_kinds_are_accepted`,
  `test_the_audit_names_anything_in_shared_that_is_not_a_capability`,
  `test_publishing_through_a_shared_folder_that_is_a_link_is_refused`,
  `test_a_source_swapped_for_a_link_after_the_check_is_not_copied`.
- *No ambient principal:* `test_the_module_takes_no_principal_from_the_environment_or_a_context_variable`
  (AST: no `contextvars`, no `getenv`/`home`/`expanduser`/`getcwd`/`getlogin`/`getuid`, and the
  one `env.get` reads `SWITCH`), `test_no_default_principal_root_or_device_exists`,
  `test_nothing_the_profiles_module_imports_reaches_a_model`.
- *Dormant, default off:* `test_profiles_are_off_by_default`, `test_only_the_one_spelling_turns_it_on`
  (nine other spellings), `test_the_process_environment_is_read_when_none_is_given`,
  `test_while_off_add_delete_and_publish_touch_nothing` (the tree is byte-identical after),
  `test_isolation_has_no_switch` (the refusing parts take no `environ`),
  `test_the_command_refuses_while_off_and_creates_nothing`,
  `test_the_command_adds_deletes_and_publishes_when_on`.

Written first: with no `walle.profiles`, `tests/test_profiles.py` failed at import. After the
module, four tests failed on the tests themselves: one assumed Ada's and Sam's refs differ (they
do not; see *Choices*), one asserted a `p-` prefixed name is rejected (it is not, and that rule
is `paths.py`'s to make), one expected `delete_profile` alone to take a second name (`add_profile`
does too, and should), and the CLI tests ran before the command existed. The stranger pass added
four more tests, each failing before its fix.

```
uv run pytest               2953 passed, 30 skipped in 123.47s
uv run ruff check           All checks passed!
uv run ruff format --check  168 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** this task adds no new lock and no new derived file; its touches of
the log go through `StreamWriter` and `index.update`, whose walkthroughs stand.
- *Job J for Ada and job K for Sam, at once.* Each was handed its own `Context` by its caller.
  J's `Profile.path` resolves inside `p-ada`, K's inside `p-sam`; neither can spell the other's
  folder, so there is nothing for them to contend over. Two jobs for *Ada* contend as any two
  writers to Ada's log do today: the stream lock, then the index lock.
- *Admin A deleting Sam's profile while job K runs for Sam.* A opens `p-sam` by descriptor and
  unlinks by name. K's open stream file keeps its inode until K closes it; K's next append writes
  to an unlinked file and is lost with it, which is what deleting a person means (D32 rule 5).
  K's next `index.update` finds no `log/` and raises `FileNotFoundError`, as it does today for a
  misspelt principal. Nothing of A's walk reads K's bytes. If K is mid-`records()` when A
  unlinks a stream, `path` sees `FileNotFoundError` on `lstat` and treats the name as "would be
  created" — still inside the folder, so allowed — and the index then reports the missing file.
  A folder A has half-emptied is a folder with fewer streams, never one with another's.
- *Publisher P and a second publisher Q of the same name.* Both pass the `exists` check; the open
  is `"xb"`, so exactly one `O_EXCL` create wins and the other gets `FileExistsError` with
  nothing written. Nothing in `shared/` is ever replaced.

**Stranger pass:** done on the whole diff — IO, links and error paths throughout. Found and fixed:
- **Deletion recursed by path.** The first version listed a folder, then recursed into each
  child by name. A child folder replaced by a link between the listing and the descent would be
  followed, and the delete would run inside whatever it pointed at — Ada's folder, say. Rewritten
  to open the profile folder and every child folder `O_NOFOLLOW|O_DIRECTORY` and operate relative
  to the descriptor; a folder that has become a link is unlinked as a link. Same shape as
  `shutil.rmtree`'s safe path, written out so the counts are honest and nothing is opened for
  reading.
- **`records()` checked the folder and not the streams.** `index.update` reads `log/*.jsonl`
  by path; a stream file that is a symlink to Sam's stream would have been indexed as Ada's.
  Now `log/` and each entry in it go through `path` first.
  `test_a_stream_file_that_is_a_link_into_another_folder_is_never_indexed` failed before this.
- **`publish` would write through a `shared/` that is a link.** `shared/` or `shared/skills`
  pointing into `p-sam` turned a publication into a write to Sam's folder. Both are refused as
  links now. `test_publishing_through_a_shared_folder_that_is_a_link_is_refused` failed before.
- **The source was opened by name after the name was checked.** A swap between check and open
  is the oldest race there is. The source is now opened `O_NOFOLLOW`, so a name that has become
  a link fails at the open. `test_a_source_swapped_for_a_link_after_the_check_is_not_copied`
  stages the swap inside `Profile.path` and failed before.
- **`spawn` made daemon threads.** A daemon thread the interpreter kills mid-append is how a torn
  tail happens. Default is now non-daemon; the caller may ask otherwise.
- **The deleted-counts could lie.** A listed folder that had become a regular file (`ENOTDIR`)
  was counted as a link. It is now `lstat`ed and counted as what it is.
- Looked for and not found: an ignored error (every `OSError` either maps to a named refusal or
  propagates), a lock held across a model call (no model, no lock), a message that carries file
  contents (refusals carry the name given, never bytes; the canary tests check).

**Review:** not run before merge. Under the agent build period (R6, R8) the reviewer agent reads
the PR's head commit and sets `agent-review`; this entry is what it and Shravan read.

**Choices made inside the task:**
- **A ref does not name a principal.** `d-mbp:1` is Ada's first event and Sam's first event.
  That is by design (D57: refs are per-folder) and it is one more reason two folders' records
  must never be in one collection: the fold would see one ref naming two events, which this
  project calls its worst failure. The fusion test checks by ref *and* by a marker word for this
  reason, and the entry says so in a comment.
- **Roles are not stored.** D27 names three roles; nothing yet says where a household's roster
  lives, and inventing a file for it would be a new decision (and, under hard constraint 2, a
  new append-only format). The role rides on the `Context`, attached per message by whoever
  builds it (D61). The only thing a role unlocks here is adding or ending a profile.
- **Passing through an ancestor is allowed; standing anywhere else is not.** An absolute symlink
  inside Ada's folder to another place inside Ada's folder walks down from `/` through the root
  and `principals/`. Those are ancestors of the profile folder, read as directory lookups only,
  and ARCHITECTURE §7 already has them as passable-not-listable. A sibling principal, `shared/`,
  `kernel/` or anywhere outside the root is refused wherever the walk stands, not only where it
  lands — `../p-sam/../p-ada/log` reaches Ada's own log and is still refused, because it passed
  through Sam's.
- **The issue against D32 rule 2.** D32 allows cross-folder reads "only inside a declared
  workflow whose manifest lists the paths". The owner's rule is stricter: none. A stricter rule
  is inside "only", so this is a narrowing, not a conflict, and nothing was flagged. If a later
  task wants a manifest-granted cross-folder read, it is that task's decision to raise, and
  `Profile.path` is the one place it would have to be opened — there is no second door.
- **Isolation has no switch, by design.** The dormant rule covers what writes: add, delete,
  publish, and the command. `Context`, `Profile`, `run` and `spawn` take no environment because
  a refusal the owner has to turn on is a policy, and the issue asked for a shape. The same
  argument `floors.py` makes.
- **`publish` takes any role.** "Only the folder's owner can share" is about *whose* folder, not
  which role; an operator publishes their own skill. What stops an admin publishing Sam's file is
  that the source resolves inside the admin's own folder and nowhere else.
- **No `read` command.** There is nothing for one, and adding one would be the first door.

**What to read first:** `Profile.path` and `_walk` in `profiles.py`, then
`test_no_path_reaches_another_principal_s_folder_through_a_link` — the nine links are the whole
argument. Then `_remove_tree` and `test_an_admin_deletes_a_profile_without_opening_a_file`.

**Noticed, not done:**
- **Check-then-use remains a race for `Profile.open`.** `path` checks the name, then `open`
  opens it; a link swapped in between is followed, because a link that stays inside the folder
  is allowed by name. `publish` closes this for its own open with `O_NOFOLLOW` because a
  capability is never a link, but `open` cannot without refusing in-folder links. D110's OS user
  per principal is the real fix: a kernel refusal needs no check to race.
- **Hard links are invisible to a path walk.** A hard link inside Ada's folder to Sam's stream
  is one inode under two names; `lstat` sees a regular file. Linux's `protected_hardlinks` stops
  this across users once D110 lands; in a single-user install nothing here can tell. Noted so
  nobody reads the symlink tests as covering it.
- **Nothing calls `run`/`spawn` yet.** The observer, proposer, consolidation and index rebuild
  still take `(root, principal)` directly, which is explicit but not the one helper D113 asks
  for. Moving them onto `Context` is one mechanical task; it touches every background entry
  point and is not this task.
- **`records()` lists `log/` with `os.listdir`, then `index.update` lists it again.** Two
  listings a moment apart; a stream added between them is indexed without the link check. Fail-
  open on a race, fail-closed otherwise. A `Profile`-aware `index.update` that takes the checked
  paths would close it; that is a change to the resolution path and belongs to its own task.
- **The curator still takes `(root, principal)`.** `test_nothing_in_the_curator_takes_two_principals`
  pins the shape by signature; the stronger version, where `curate` takes a `Profile`, is the
  same mechanical task as the one above.
- `Shared` has no audit command; `audit_shared` is a function only. A `walle profiles audit`
  command is a few lines when wanted.

**Cost:** author run on Hyperagent, Fable 5.1; the dollar figure is on the Hyperagent side and
not visible from here. Reviewer cost to be added by the reviewer.
