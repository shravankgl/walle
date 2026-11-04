# Task 7: Hot-tier renderer

Issue #5. Branch `task/07`.

**Shipped:**
- `src/walle/hot.py` is the new renderer. It has two halves:
  - **`compose(principal, resolution)` is pure.** It takes the resolver's fold and returns the
    text of `identity.md`, `user.md` and `learned.md`, plus the mode. It reads nothing,
    writes nothing and reads no clock, so the same fold always gives the same bytes.
  - **`render(root, principal)` does the IO.** It holds `hot/render.lock`, brings the index up
    to date (`index.update`), folds it, composes, and writes the files `0444` through a temp
    file in the same folder, then renames. It records each file's SHA-256 in
    `hot/rendered.json`. A file whose bytes walle did not write is kept and reported, never
    rendered over.
  - The constants `FILES`, `ORDER`, `FULL_LIMIT = 8000` and `KEEP_FULL = 10`, and a
    `RenderReport` that says per file what happened: `written`, `unchanged` or `preserved`.
- `src/walle/paths.py`: `hot_dir(root, principal)` is `principals/p-<name>/hot/`.
- `src/walle/cli.py`: `walle render --root --principal`. It prints the mode, the counts, which
  files were written or unchanged, whether `rules.md` exists, and the resolved-state hash. It
  exits 1 with an explanation when it kept an edit, and with a plain `Error:` (not a
  traceback) on an `OSError`.
- `tests/test_hot.py`: 24 tests (25 cases). Every fixture is synthetic: a made-up person's
  dinners, fern and basil.

**Why:**
- **D50, D64.** The hot tier is a file you can open, and each file opens by saying what it is
  and what to do with it. Every rendered file starts with a `<!-- walle hot tier: … -->` line
  (its name, that it is derived and read-only, and where edits go), then a second-person
  framing ("Read them as things you already know, not as notes about somebody else"). That
  sentence is the fix for pithagoras's SOUL.md failure.
- **D81.** The split is identity / user / rules / learned. `rules.md` is human-only, and the
  renderer never opens, writes, chmods, renames or creates it. It only reports whether it
  exists (`os.path.lexists`). `identity.md` tells the model that rules win on conflict, and
  `ORDER` puts rules above learned for the hook (task 8). Index mode: when the full render is
  over 8,000 characters, the newest ten assertions stay in full and the rest become
  `[#ref] summary` lines. A note tells the model to fetch with `recall` before acting on a
  one-line entry.
- **D107.** The files are derived (D10), so they are written `0444` and their hash is recorded.
  If a file's bytes are not ones the manifest vouches for, a human edited it: the render keeps
  the edit byte for byte, reports it (`RenderReport.preserved`, `kept` in the manifest, exit 1
  from the CLI), and renders over it on no later run either until the person deletes it.
  "Never a silent clobber, never a silent discard."
- **R1.** In index mode the one-line entries come first, oldest first, and the full entries
  last. A new assertion therefore changes the end of `learned.md`, and the start stays a stable
  prefix. A render that changes nothing rewrites nothing: inodes, the manifest included, stay
  the same.

**Verify:** the criterion from PLAN §9, part by part:
- *Over-threshold fixture flips to index mode:*
  `test_over_the_threshold_flips_to_index_mode`. 30 assertions with long bodies. The newest
  ten bodies are present, the other 20 appear only as `[#d-mbp:N] Plant note NN` lines, and
  the `recall` note is there. `test_the_threshold_is_on_the_full_render` pins the boundary:
  `limit=size` is full and `limit=size-1` is index.
- *Under-threshold renders in full:* `test_under_the_threshold_every_assertion_renders_in_full`.
- *`rules` untouched by the renderer:* `test_rules_is_untouched_by_the_renderer`. `rules.md` is
  written `0644` with a fixed mtime, and the test renders twice with a new assertion between.
  Bytes, inode, mode, `mtime_ns` **and `ctime_ns`** are unchanged. `ctime` catches a chmod or
  a rename over the file, not just a write. `test_rules_is_never_created_by_the_renderer`
  covers the absent case.
- *A render over an out-of-band edit preserves the edit and reports it:*
  `test_a_render_over_an_out_of_band_edit_keeps_the_edit_and_reports_it`. Render, edit
  `learned.md`, append an assertion, render again. The file is byte-identical to the edit,
  `preserved` names it with the edit's hash, the other two files still render, the next render
  reports it again, and deleting the file lets the render write it fresh.
  `test_the_cli_reports_a_kept_edit_and_exits_non_zero` covers the CLI side.
  `test_an_edit_saved_by_replacing_the_file_is_kept` covers editors that save by renaming.
  `test_a_file_walle_never_wrote_is_not_clobbered` and
  `test_a_symlink_in_place_of_a_rendered_file_is_not_followed` cover the rest.
- *Rendered `0444`, hash recorded:* `test_rendered_files_are_0444_and_their_hashes_are_recorded`.
- **The tests were written first.** On the branch before `hot.py` existed, collection failed
  with `ModuleNotFoundError: No module named 'walle.hot'`. Once the code passed, I broke it on
  purpose to check that each key test can fail:
  - Removing the `flock` on `render.lock` fails the two-renderer test.
  - Rendering over unvouched bytes fails the four edit-preservation tests.
  - Dropping the first manifest write fails `crash … [after the files]`.
  - Never entering index mode fails the three index tests.
  - Removing `OSError` from the CLI's catch fails the CLI error test.
  - A no-op text change fails nothing.
- The four checks, run on the final tree:

```
$ uv run pytest
238 passed, 3 skipped in 8.16s
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
33 files already formatted
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

  The 3 skips predate this task. They are `test_index.py`'s read-only-folder tests, which skip
  in this sandbox because its filesystem lets a `0o555` folder be written. `test_hot.py` has
  no skips. Its read-only-folder test injects a `PermissionError` instead of relying on chmod,
  so it runs everywhere.

**Two-runner walkthrough:** this writes derived files and takes locks. The same text is in the
module docstring. Renderers A and B for one principal, writer W on `d-mbp`, and a person H with
an editor.

1. A takes `hot/render.lock` (exclusive). B blocks there, **having read nothing**.
2. A clears any temp files a killed render left (safe: no live render can own one while A holds
   the lock), then runs `index.update`, which takes `index.lock` and every stream lock for a
   moment and releases them. A has the log up to `d-mbp:3` and folds it.
3. W appends `d-mbp:4`. It needs only its stream lock, which A has released, so W does not
   wait for the render. A's render does not include `d-mbp:4`.
4. A reads `rendered.json` and each file, and all three are bytes it vouched for. It writes the
   manifest vouching for the old **and** new hashes, renames the files into place, then writes
   the manifest vouching for the new hashes only, and releases the lock.
5. B wakes and only now reads the log, which includes `d-mbp:4` because the log only grows.
   B finds A's files, which A's manifest vouches for, and renders over them. What is left is
   B's render of the newer log, and neither reported the other as a human edit.

If B read the log before waiting for the lock, it could wake holding an older fold than A's
and replace A's newer files with stale ones. `test_a_second_renderer_waits_and_renders_the_newer_log`
stages exactly that. A second process composes, signals and sleeps 1.5 s inside the lock. The
parent appends and renders. The test asserts the final file has the new assertion and nobody
reported an edit. It fails with the lock removed.

H takes no lock, because editors know nothing about it. If H saves before A's step 4, A sees
unvouched bytes and keeps them. If H saves after step 4, the next render does. **One window is
left open:** H saving between A reading the file in step 4 and A's `os.replace` loses H's edit.
That window is microseconds, and closing it would need a lock the editor takes. The `0444`
mode is what makes it unlikely: H has to chmod first, or use an editor that forces the write.

Crash safety: files and manifest cannot change in one step, hence the two manifest writes. A
render killed after the first manifest leaves old files that the manifest still vouches for. A
render killed after the files leaves new files that the first manifest already vouches for.
`test_a_crash_mid_render_is_not_mistaken_for_an_edit[×2]` kills a render at each point with
`KeyboardInterrupt` and checks the next render reports nothing.

Lock order is `render.lock`, then `index.lock`, then stream files. Nothing takes `render.lock`
while holding the others, so there is no cycle. `render` does not call `load_records`: it folds
`IndexReport.records`, so it never opens `index.lock` twice.

macOS: everything used is POSIX and present on Darwin. That covers `fcntl.flock`, `os.replace`
within one folder (atomic on APFS and HFS+), `tempfile.mkstemp(dir=…)`, `os.fchmod`,
`O_NOFOLLOW`, `O_CLOEXEC` and fsync of a directory fd. Nothing Linux-only is used: no
`O_TMPFILE`, no `renameat2`/`RENAME_NOREPLACE`, no `/proc`, no `linkat` tricks. Renaming over
a `0444` file depends on the folder's permissions, not the file's, on both systems. I could
not run the tests on macOS here; CI is Ubuntu.

**Stranger pass:** done because the diff is over 30 lines and touches IO, a lock and error
paths. I re-read `hot.py` and `cli.py` cold. Found and fixed:
- **A status that lied.** When `learned.md` was kept, the CLI's headline still read "rendered
  the hot tier … 4 assertions", although the file on disk held the person's edit, not those
  four. The headline is now "hot tier for N assertions", and stdout gets
  `kept as edited, not this render: learned.md` in addition to the stderr explanation.
  The CLI test asserts both.
- **A stale manifest.** The first draft rewrote `rendered.json` only when a file changed, so a
  fold that changed the state hash without changing any text (for example a new unapplied
  supersede) left a manifest whose `state` described an older log. Now the manifest is
  compared with what is on disk and rewritten when anything differs.
- **The manifest could be read as if every file held the render.** It now carries
  `kept: [...]`, so the hook (task 8) can tell a kept edit from this render.
- **An ignored error class.** A read-only `hot/` folder or a full disk raised a raw
  `PermissionError` traceback from the CLI. `OSError` is now caught and shown as
  `Error: …`. The first version of that test used `chmod 0o555` and was silently skipped in
  this sandbox, so I rewrote it to inject the error.
- Leaks: the temp file is unlinked on any failure, including `KeyboardInterrupt`
  (`test_a_failed_write_leaves_the_old_file_and_no_temp`). Leftovers from a `SIGKILL` are
  cleared under the lock on the next render. Every fd is closed by a context manager or
  `finally`.
- Looked at and left alone:
  - If `_fsync_dir` fails after `os.replace`, the CLI reports an error although the new file
    is in place. That is truthful about durability, and the first manifest already vouches
    for those bytes, so the next render is clean.
  - A failure partway through the file loop leaves a mix, for example `identity.md` new and
    `learned.md` old. Each file is still whole, and the next render completes it. See
    *Noticed*.

**Review:** pending.

**Choices made inside the task:** each is mine, and any of them can be overridden.
- **Where the files live: `principals/p-<name>/hot/`.** The issue names the files but not the
  folder. Inside the principal folder because they hold that person's facts (hard constraint
  6). `rules.md` is in the same folder so the hook reads one place.
- **What goes in identity and user.** The schema has no persona or profile event yet; the
  D60 field registry isn't built. So `identity.md` and `user.md` carry only framing: who the
  model is, who it works for, how the four files rank. All current assertions go in
  `learned.md`. I considered splitting by `level` (explicit means user, inferred means
  learned) and rejected it: that would be a new meaning for D94's field, which is a decision,
  not a render detail.
- **A kept edit stays in place, rather than being moved aside so the render can proceed.**
  Moving it aside would keep the hot tier matching the log, but the person would open
  `learned.md` and find their edit gone, which looks exactly like a clobber. Staying in place
  means the hook injects the edited text until it is dealt with, and the CLI says so. D107's
  "stages it as a pending proposal (D49)" needs task 9's staging queue, so it is not done
  here (see *Noticed*).
- **A file with no manifest entry is kept unless it already equals the render.** Walle cannot
  prove it wrote that file, so it may be the person's own. The cost is a false report after
  the manifest is lost; the alternative cost is a lost file.
- **The threshold is measured on all three rendered files, in characters, in full mode.** D81
  says "~8k chars" of the hot tier. `rules.md` is not counted, because a person's own rules
  growing should not change how walle shows what it learned.
- **"Newest" means log time (`ts`), tie-broken by ref.** World time (`valid_from`) is about
  the fact, not about when walle learned it.
- **The manifest is written `0444` too.** It is derived like the rest. A person who edits it
  to vouch for their own edit can, deliberately.
- `hot.py` is not added to `RESOLUTION_ROOTS` in `tests/test_constraints.py`. It consumes the
  fold and is not imported by it. The constraint test still covers everything `hot.py`
  imports from the resolution path.

**What to read first:** the `render` function in `hot.py`, about 70 lines. It covers the
two-phase manifest, `_judge`'s three outcomes, and why a kept file keeps its old vouched
hashes. Then the docstring's two-runner walkthrough, and whether you accept the one open
window. Then the three framing texts (`_identity`, `_user`, `_learned`). Their wording is
what the model reads every session, and D64 says wording is the feature.

**Noticed, not done:**
- **D107's staging step.** A kept edit should become a pending proposal in the D49 review
  batch. That needs task 9's staging queue, and then `walle review` can offer the diff. Until
  then the person deletes the file to discard their edit.
- **Cross-file consistency for the hook.** Each file is replaced atomically, but the set is
  not. A hook (task 8) reading during a render, or after a render that failed mid-loop, can
  see a new `identity.md` beside an old `learned.md`. Today the identity and user texts barely
  change, so this is harmless. Task 8 may want to check `rendered.json` hashes against the
  files, or the renderer could later write a versioned folder and swap one symlink.
- **Durability on macOS.** `os.fsync` on Darwin does not flush the drive's cache; that takes
  `fcntl(F_FULLFSYNC)`. The stream writer has the same limit. A derived file can always be
  re-rendered, so it matters more for the writer than here.
- **Bodies are rendered as written.** A summary is collapsed to one line, so it cannot forge an
  index entry (`test_a_summary_cannot_forge_an_index_line`). A body can still contain a line
  that looks like `### [#d-mbp:1] …`. Bodies are human-approved (D49), so I left it.
- **A `hot/` that is a symlink is followed.** Only the files inside are checked with `lstat`.
- `identity.md` and `user.md` should gain content once the schema has somewhere for persona
  and profile facts (D60's field registry).

**Cost:** pending.
