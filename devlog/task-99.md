# Task 99: grant scopes — this request, this session, always, config

Issue #99. Branch `task/p3-grants`, from `main` at `a0a715f` (PR #94, the reviewer's recursion
fix). Phase 3 (R8). Dormant: nothing is granted and every check refuses unless the owner sets
`WALLE_GRANTS=on`.

**Shipped:**
- `src/walle/grants.py`, new. The permission engine's answer to "the person said yes — to what,
  and for how long?"
  - `Scope`: `request` < `session` < `always` < `config` (D73). `offered(call)` is the one list
    of what a person may be offered: the three grantable scopes for an ordinary call,
    `request` alone for a destructive one (D116). `config` is never offered.
  - `Call(tool, arguments, destructive)`: the call as it will be applied. Types are checked in
    `__post_init__`, once: a non-string tool or arguments, a non-bool flag, are `TypeError`; a
    tool name outside `TOOL_NAME_RE` (the reviewer's shape, held equal by a test) or a NUL in
    the arguments is `ValueError`.
  - `key_for(call, call_id)`: the one key function, called by `grant` and by `check`. Exact
    bytes of tool and arguments plus the destructive flag; for a destructive call, the call id
    too, so the grant matches that one call and nothing afterwards (D116's per-call `rule_key`).
  - `Live(session, request, grants)`: the in-memory grants and where the run is. Immutable.
    `next_request()` is the run boundary and drops every `request` grant; `start()` is a new
    session with nothing. `Grant.covers` compares the session, and for a request grant the
    request, so a stale grant left in a `Live` by a caller who forgot the boundary still does
    not apply.
  - `grant(...)`: off → `GrantsOffError`; wrong type or shape → `TypeError` / `ValueError`;
    scope not offered → `NotGrantableError`; all before anything is read or written. `request`
    and `session` go into memory. `always` is appended to `principals/p-<name>/grants/always.jsonl`
    under `always.lock`; on any `OSError` it goes into memory at `session` scope and
    `Granted.note` says so, naming the error class, never its message (D73).
  - `check(...)`: types first, then the switch, then both rule files are read (every time, so a
    broken file is reported the first time anyone looks), then memory narrowest first, then the
    files. A destructive call is never covered by a file. Any file that does not parse whole —
    a line that is not JSON or not a rule, a link, a newer `v`, an unreadable file — contributes
    nothing and is named in `problems` on both `Allowed` and `Refused`.
  - `ConfigRule` / `AlwaysRule`: strict pydantic, `extra="forbid"`, `v` at least 1 and at most
    `KNOWN_V`. The always line also records `given_at`, `session`, `request`.
- `tests/test_grants.py`, 93 tests.

**Why:**
- D62: only the literal `always` writes a standing rule; an approval covers the exact command.
- D73: the four scopes, `request` as the default cleared at the run boundary, `session` as the
  fatigue answer, degrade to session with the card saying so, config as the human-edited ceiling.
- D116: a destructive action is approved once; the option to say `always` is not offered, and the
  key such an approval writes is per call so it matches nothing afterwards.
- D72: `grants/` is kernel-written and floored for the agent. The floor table already names it;
  a test pins both file names to it. Config is read here and written nowhere here.
- D75: the destructive flag is part of the key, so a declaration that tightens is not undone by
  an earlier grant.
- D86 / D111: fail closed on anything unresolvable, including a rule from a newer build.
- D117: install is not enable. Off by default, one spelling turns it on, and a test proves it.

**Verify:**
- *A request grant ends at the run boundary:*
  `test_a_request_grant_ends_at_the_run_boundary` (allowed before `next_request`, refused after,
  same session), `test_a_request_grant_covers_the_exact_call_for_the_rest_of_the_request`,
  `test_a_stale_request_grant_left_in_memory_still_does_not_apply`,
  `test_a_session_grant_outlives_the_request_and_not_the_session`.
- *An `always` that can't be saved drops to session and says so:*
  `test_an_always_that_cannot_be_saved_drops_to_session_and_says_so` (a file where the folder
  should be: `asked` is always, `scope` is session, the note says "could not be saved" and
  names the class, the session grant survives the next request, nothing durable exists),
  `test_an_always_for_a_principal_with_no_folder_degrades_and_makes_none`,
  `test_a_read_only_grants_folder_degrades_too` (skipped where the account ignores folder
  permissions, as this sandbox does), `test_an_always_file_that_is_a_link_is_not_written_through`,
  `test_the_note_names_the_error_class_never_its_message`,
  `test_a_degraded_grant_is_not_silently_retried_as_always`.
- *Config is human-edited only:* `test_config_cannot_be_granted`, `test_config_is_never_offered`,
  `test_nothing_in_the_module_writes_the_config_file` (the one writer is watched across every
  offered scope and a check; config bytes unchanged), `test_both_grant_files_sit_under_a_floor`,
  `test_a_config_rule_is_read_and_covers_the_exact_call`.
- *Once, never always (D116):* `test_a_destructive_call_is_offered_request_only`,
  `test_a_destructive_call_cannot_be_granted_wider_than_once`,
  `test_a_destructive_grant_is_keyed_to_the_one_call`, `test_no_file_covers_a_destructive_call`,
  `test_a_destructive_key_cannot_be_held_in_memory_wider_than_request`.
- *What is checked is what is used:* `test_a_near_miss_is_not_the_call` (eight near misses, each
  refused while the exact call is allowed at every scope), `test_the_key_is_one_function_on_both_sides`,
  `test_a_grant_for_the_plain_call_does_not_cover_it_marked_destructive`,
  `test_the_narrowest_covering_scope_is_reported`.
- *Fail closed:* ten rule shapes that do not validate, a bad line beside a good one, a link, a
  folder where the file should be, a newer `v`, nesting past the recursion limit, and
  `test_a_broken_always_file_does_not_void_a_session_grant`.
- *Types up front:* `Call`, `Live`, `Grant`, scope-as-string, call id, principal and timestamp
  each refused before anything is written.
- *Dormant, default off:* `test_grants_are_off_by_default` (no grant, nothing written, a check
  refuses with a grant in memory and a rule in config), `test_only_the_one_spelling_turns_grants_on`,
  `test_while_off_nothing_is_read` (the loader is patched to fail if called),
  `test_the_module_starts_nothing_and_reads_no_clock`.

Written first: with no `walle.grants`, `tests/test_grants.py` failed at import. After the
module, four tests failed: three on my own test code (a fixture collision, an assertion on a
message for the wrong scope, and a word in the docstring tripping the no-clock test) and one
real one, below.

```
uv run pytest               2986 passed, 31 skipped in 130.62s
uv run ruff check           All checks passed!
uv run ruff format --check  167 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** kernels A and B saving `always` for the same principal; reader R
checking. A takes `grants/always.lock` exclusively; B blocks having opened nothing else. A
checks the file is not a link, appends one line, `fsync`s, closes, releases. B wakes, makes the
same check, appends after A's line. Either order gives two whole lines
(`test_two_kernels_saving_always_at_once_leave_two_whole_lines`, eight threads). R takes no
lock: it reads whole lines, and a line a writer is mid-way through has no newline yet. If R
reads exactly then, the partial last line is "not JSON" and the file contributes nothing to
that one check — a refusal where a grant was owed, which is the fail-closed side to err on,
and the next check reads it whole. A kernel killed between open and `fsync` leaves a partial
last line for good; every check reports it and applies nothing from the file until a person
looks. That is deliberate: a repair would be a kernel write to a rule file on its own judgment.
`request` and `session` grants are per `Live`, which is per session and immutable, so two
runners never share one.

**Stranger pass:** done on the whole diff: IO, locks and error paths throughout. Found and fixed:
- **A session grant in memory was invisible to a broken always file.** `check` returned on the
  memory match before opening the files, so a corrupt `always.jsonl` would be reported only on a
  check that reached it. Both files are now read on every check and `problems` rides on
  `Allowed` too. `test_a_broken_always_file_does_not_void_a_session_grant` failed before the fix.
- **A hand-built `Grant` could hold a destructive key at session scope.** `grant()` never makes
  one, but `Grant` is a public dataclass, and `covers` would have honoured it. `Grant.__post_init__`
  now refuses a per-call key outside `request`. `test_a_destructive_key_cannot_be_held_in_memory_wider_than_request`.
- **Deep nesting was a crash, not a refusal.** `json.loads` raises `RecursionError`, not
  `ValueError`, past the limit — the bug PR #94 fixed in the reviewer. Caught in `_load`.
  `test_a_line_nested_past_the_recursion_limit_grants_nothing`.
- **A grant for a misspelt principal would have created a profile folder.** `_append` made
  parents. It now makes `grants/` only; a missing principal folder is `FileNotFoundError`, which
  degrades the grant and names the class. `test_an_always_for_a_principal_with_no_folder_degrades_and_makes_none`.

Looked for and not found: an error message echoed into a note or a problem (every one names a
class or a line number, never text from the file or the OS); the switch read after a file
(checked before the loader, and a test patches the loader to fail); a lock held across anything
but the append; a status that lies (`degraded` is computed from `asked` and `scope`, not stored
separately).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run, because the brief replaces it for agent work.

**Choices made inside the task:**
- **Rules live in `grants/` as two JSONL files, not as log events.** D72 names `grants/` as the
  kernel-written home of "what a principal has allowed" and the floors already protect it, so a
  standing rule goes there. A new log event type would be a schema decision (D120's precedent),
  which agents do not make. `always.jsonl` is append-only (constraint 2); revocation is not in
  this task and would be a tombstone line, not a deletion. **Flag:** if the owner would rather
  `always` be a log event so the rebuild test covers it, that is a decision, and the file format
  here is one function away from being its renderer.
- **Config is JSONL with the same `ConfigRule` shape, one parser for both files.** D62's
  `tool: pattern*` text form carries patterns, which are D74's task; a second parser for a
  second syntax is where "what's checked differs from what's used" gets in. A non-technical
  person edits config through a surface later, not by hand either way.
- **Matching is exact bytes.** D62 says matched exactly; D74's split-and-justify is listed as its
  own Phase 3 item. No normalisation of whitespace or case, because every normalisation is a
  class of near miss that becomes a hit.
- **`request` covers repeats of the same call within the request**, not one use. D73 refines
  D62's "once" into "this request" as the default; that is the fatigue relief. The one-use
  reading is kept for destructive calls, where the key is per call.
- **A file never covers a destructive call**, even a config line matching it byte for byte.
  D116 says once, never always; config is wider than always. The same bytes with the flag off
  are covered, so a human allowlist for a tool declared safe still works.
- **`ts` is passed in.** The module reads no clock, like the floors, missions and reviewer.
- **`OSError` degrades; anything else propagates.** D73's "cannot be persisted" is the
  filesystem's refusal. A `ValueError` from a bad argument is the caller's bug, reported as
  such, not softened into a session grant.

**What to read first:** the module docstring, then `key_for`, `Grant.covers` and `check`
(about 60 lines: the whole of what is checked against what), then
`test_a_near_miss_is_not_the_call`, `test_an_always_that_cannot_be_saved_drops_to_session_and_says_so`
and `test_grants_are_off_by_default`.

**Noticed, not done:**
- **Wiring.** Nothing calls `check` yet. The reviewer's `Standing.GO` ("a standing rule allows
  it, D62") is where an `Allowed` would land, and the inbox's `approved` answer is where
  `grant` would be called with the scope the person chose. Both are the dispatcher's task.
- **D74's matching** (split on separators, justify every part, argument-executors never
  prefix-eligible) is its own item. Until it lands a standing rule covers one exact string.
- **D62's fifteen-minute window** on an approval reply is the inbox's clock, not this module's.
- **Revocation** of an `always` rule: a tombstone line in `always.jsonl`, human-initiated.
- **`_load` reads the whole file on every check.** Fine at the sizes a person produces; a cache
  keyed on `(size, mtime)` is a later task if it ever shows up.
- **`always.lock` as a link** would flock elsewhere. Only the agent is kept from `grants/`, and
  it is floored; a person who links their own lock file gets what they asked for.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time. Hyperagent's figure for this thread is to be
filled in by the owner.
