# Task 100: command matching — split on every separator and justify each part

Issue #100. Branch `task/p3-matching`, created empty by the owner as the claim and reused here
as the owner's comment on the issue says; from `main` at `73b9cdd` (PR #108, grants). Phase 3
(R8). Dormant: every command is refused unless the owner sets `WALLE_MATCHING=on`.

**Shipped:**
- `src/walle/matching.py`, new. The permission engine's answer to "does a standing rule cover
  this command?", D74's way.
  - `split(command)`: the parts between `&&`, `||`, `;`, `|`, `&` and newline, longest
    separator first. Nothing protects a separator: not a quote, not a backslash, not a
    backtick. Every part is a literal slice of the command and nothing is dropped, so the
    parts joined with their separators are the command.
  - `Rule(pattern)`: a prefix of words, `git diff*`, or an exact command without the star
    (D62's `pattern*`). Parsed once; the pattern must itself be a part the classifier would
    accept, so a rule can never be made for `xargs*`, `sudo git status`, `python3 -c*`,
    `find -exec*`, `export*`, `git status | ls`, or a bare `*` (D62). A rule's identity is the
    pattern as the person wrote it.
  - `justify(part, rules)`: the never-eligible classes first, then the first rule whose words
    equal the part's raw words (exact) or begin them (prefix). Returns a `Justified` or the
    `Refusal` kind.
  - `match(command, rules, environ=None)`: types, then the switch before anything is split,
    then every part through `justify`. `Allowed(parts)` or `Refused(kind, reason, part, parts)`,
    naming the first unjustified part by 1-based index. The reason names the class and the
    index and never the text.
  - `Refusal`: one kind per way of failing — `off`, `empty`, `control`, `non-ascii`, `quoted`,
    `escaped`, `glob`, `opaque`, `assignment`, `shell`, `executor`, `inline-code`,
    `exec-flag`, `no-rule`.
  - The tables: `SEPARATORS`; `WORD_CHARS` (ASCII letters, digits, `. _ / : @ % + , - =`);
    `D74_EXECUTORS` (the nine the decision names) inside `EXECUTORS` (those plus privilege
    wrappers, tracers, debuggers, multiplexers, container and package runners, the dynamic
    loader, shells and the builtins that run their argument); `SHELL_WORDS` (keywords, and
    builtins that change what a later part runs: `export`, `alias`, `trap`, `set` …);
    `INLINE_CODE` (interpreter → inline-code flags, plus `-` for stdin); `EXEC_FLAGS` (`find`
    and `fd`). Program names are looked up by basename, case-folded, with a trailing version
    dropped, so `/usr/bin/SUDO`, `bash5` and `python3.12 -c` are what they are. Folding is for
    the lookup only; the match is against the raw word.
- `tests/test_matching.py`, 432 tests, of which 296 are the bypass corpus, parametrised one
  test per command under twelve headings (quoting, escapes, newlines, unicode separators, env
  assignment prefixes, argument executors, shell keywords and state builtins, interpreters
  carrying inline code, execution flags on search tools, opaque constructs, separator edge
  cases, near misses of the rule itself). Every one is synthetic; nothing is run.

**Why:**
- D74: split on every separator and justify each part, so `git status && git diff` passes
  when both match and `find . -exec rm {} +` fails under a `find` rule. Opaque constructs,
  argument-executors, interpreters with inline code and search tools with execution flags are
  never eligible for a prefix rule, whatever they match. Fail closed on anything unparseable.
- D62: a rule is `pattern*`; a bare `*` is rejected; only one command per rule.
- D86: the refusal is the default and nothing is decided ahead of a parse that succeeded.
- D117: install is not enable. Off by default, one spelling turns it on, and a test proves it.
- The owner's instructions for this task: over-splitting must only ever produce more parts to
  justify, never fewer; a real bypass corpus; anything that cannot be parsed is refused; the
  exact string that would run is what is checked, not a normalised copy.

**Verify:**
- *`git status && git diff` passes when both match:* `test_both_parts_matching_passes` (two
  parts, each with its rule), `test_plain_commands_under_plain_rules_pass` (thirteen shapes
  across every separator), `test_allowed_parts_cover_the_whole_command`.
- *`find . -exec rm {} +` fails under a `find` rule:* `test_find_exec_fails_under_a_find_rule`
  (the braces refuse it as opaque; `find . -name x -exec rm` without braces refuses it as an
  exec flag, so each refusal stands on its own), `test_the_exec_flags_cover_find_and_fd`.
- *A corpus of bypass attempts all fail:* `test_every_bypass_is_refused` (296 commands under
  narrow rules), `test_the_corpus_is_refused_under_a_wide_rule_too` (the same corpus, less
  the near-miss group, under a prefix rule for every program it uses: the refusals are about
  what the command is, not which rules happen to exist), `test_the_corpus_covers_every_class_d74_names`
  (every heading D74 lists is present and every executor D74 names appears in the corpus).
- *Over-splitting only ever adds parts:* `test_over_splitting_only_ever_adds_parts` — the
  part count is one more than the separator count, and every command `match` allows also
  passes a naive whole-text prefix check of the same rules, while `git status && rm -rf /`
  and `git status | rm -rf /` pass the naive check and are refused here.
  `test_split_is_on_every_separator_with_nothing_protecting_one` (a separator inside quotes,
  after a backslash, inside a backtick still splits), `test_split_is_total_and_loses_nothing`
  (every corpus command reassembles from its parts and separators).
- *The exact string, not a normalised copy:*
  `test_rules_are_matched_against_raw_words_never_an_unquoted_copy` (`git "status"`,
  `git\ status` and `gi\t status` run `git status` and none is covered by the rule),
  `test_folding_a_name_only_ever_widens_a_refusal` (case and version fold for the executor
  lookup; `Git status` and `base64` still do not match `git status` and `base*`),
  `test_each_refusal_class_is_reported` (`2>&1` is refused as a redirect, a tab as a control
  character, two spaces as an empty word).
- *Never eligible, regardless of match:* `test_an_executor_is_refused_before_matching_is_tried`
  (a rule naming one cannot be made; a command naming one is refused even under a rule that
  would cover it), `test_an_executor_reached_by_path_is_still_an_executor`,
  `test_a_shell_word_is_never_a_program`, `test_a_rule_that_cannot_be_matched_safely_cannot_be_made`
  (fifty-odd patterns).
- *Refusals name the part and the class, not the text:*
  `test_a_refusal_names_the_class_and_the_part_never_the_text`,
  `test_the_first_failing_part_is_reported_in_order`.
- *Dormant, default off:* `test_matching_is_off_by_default` (a command every rule covers is
  refused with `Refusal.OFF`, with the switch unset and with an empty mapping),
  `test_only_the_one_spelling_turns_matching_on`, `test_while_off_nothing_is_parsed` (`split`,
  `justify` and the classifier are patched to fail if called),
  `test_the_module_starts_nothing_runs_nothing_and_reads_no_clock` (no `subprocess`, `os.system`,
  `shlex`, `open(`, clock, or second `os.environ`).

Written first: with no `walle.matching`, `tests/test_matching.py` failed at import. After the
module, one test failed, on my own test code: it asserted `Rule("git diff *") == Rule("git diff*")`,
and a rule's identity should be the pattern as written, so the test now compares words and
prefix and asserts the two are *not* equal. Two further expectations were settled before the
module was written and are recorded under *Choices*: a tab is a control character, not a word
separator; and the over-splitting property is checked against a naive whole-text matcher
built in the test, not one shipped in the module.

```
uv run pytest               3478 passed, 31 skipped in 148.83s
uv run ruff check           All checks passed!
uv run ruff format --check  173 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** not applicable. The module touches no log, no lock and no derived
file; it reads nothing but its arguments and the one environment variable, and holds no
state. Two kernels calling `match` at once share nothing.

**Stranger pass:** done on the whole diff, as a matcher is one long error path. Found and
fixed, each with corpus entries:
- **Case was not folded for the executor lookup.** `/usr/bin/SUDO ls` runs `sudo` on a
  case-insensitive filesystem (macOS by default) and was a plain program here. Lookups now
  fold case; the match against the rule's words does not.
- **A versioned interpreter was a plain program.** `python3.12 -c x` and `node18 -e x` were
  not in a table keyed on `python3` and `node`. The lookup drops a trailing version; the table
  is keyed by family (`python`, `pypy`, `node`, `perl`, `ruby`, `php`, `deno`, `bun`).
- **Shell keywords and state builtins were programs.** `export PATH=/tmp; git status` has two
  parts and the second runs whatever is in `/tmp`; `for x in a; do ls; done` is a construct.
  `SHELL_WORDS` refuses both kinds as `Refusal.SHELL`, and a rule cannot name one.
- **The executor table was D74's nine and the shells.** Added the same class as it occurred
  to a stranger: `doas`, `su`, `pkexec`, `chroot`, `nsenter`, `nice`, `ionice`, `setsid`,
  `strace`, `ltrace`, `gdb`, `lldb`, `valgrind`, `perf`, `watch`, `time`, `flock`, `parallel`,
  `script`, `screen`, `tmux`, `systemd-run`, `at`, `batch`, `busybox`, `toybox`, `podman`,
  `pipx`, `bunx`, `pnpx`, the dynamic loader by its names, and the other shells. The
  docstring says plainly that the table is not and cannot be complete.

Looked for and not found: a path through `match` that returns `Allowed` with a part the
classifier did not see (every part goes through `justify`, and `justify` classifies before it
looks at a rule); a refusal that echoes the command (the reasons are a fixed table, and a test
plants a distinctive string and checks it is absent); a separator that something protects (a
test splits inside quotes, after a backslash, inside a backtick); a normalisation other than
the spaces at a part's ends (there is one `strip(" ")` and it strips spaces only).

**Review:** independent review is the reviewer agent's, on this PR, per the brief. No
`scripts/review.sh` run, because the brief replaces it for agent work.

**Choices made inside the task:**
- **`2>&1` is refused, not stripped.** D62 stripped it before matching because models write
  it by reflex. D74 refines D62 and lists redirects among the opaque constructs without an
  exception, and the owner's instruction for this task is to check the exact string that
  would run. I followed D74 and the instruction. If the owner wants D62's convenience back,
  it is one more line in `_classify` and a decision to record, not a change to make here.
- **A tab is a control character, not whitespace.** A shell treats it as a word separator;
  this module compares the exact text, and the rule was written with a space. `git\tstatus`
  is refused, as `git  status` (an empty word) is. A model that emits a tab gets a refusal
  that names the class, and a rule author who wants tabs has no way to write one.
- **Quotes are refused outright, not parsed.** D74's list does not name them; its reasoning
  does — a quote hides from the rule what the shell will see — and the owner's corpus
  instruction names them first. Parsing quotes to compare the unquoted result would be
  matching a normalised copy. The cost is that `git commit -m "fix"` can never be covered by
  a prefix rule, which is the right cost: a commit message is free text a rule never saw.
- **Globs and `~` are opaque.** Not on D74's list by name, but the same thing: an expansion
  is contents the rule never saw. `rm *` under `rm*` would be the hole.
- **A glued star is a prefix of whole words.** D62 writes `gh pr list*`. `git st*` covers
  `git st` and `git st x`, never `git status` or `git stx`; a star inside a word, or two, or a
  detached star with text after it, is a glob and the pattern is rejected.
- **`python -m` is not inline code.** D74 names `-c` and `-e`; a module name is something a
  rule can name, as a script path is. Recorded under *Noticed* for the owner's view.
- **A NUL is a `ValueError`, not a refusal.** As in `grants.Call`: no shell takes one, so a
  caller that passes one has a bug, and a bug is reported, not softened into a refusal that
  looks like a decision.
- **The reason names the class and the index, not the text.** The caller has the command,
  and a reason is the kind of string that ends up in a log or a status line. `Refused.parts`
  carries the raw parts for a surface that wants to show them, and the part index says which.
- **`Rule` runs its own pattern through the classifier.** A rule for `sudo*` cannot be made,
  rather than being made and never matching, so a rule file cannot hold a rule that lies
  about what it covers. D100's load-time `match`/`not_match` examples are the next step and
  are not this task.
- **D74's network-client line is not implemented here.** "Network clients are never
  read-only, even for GET" is a property of a read-only classifier (D75's risk classes), not
  of rule matching: D108's own example of a granted command is `gh pr list*`, a network
  client. The module docstring says so.

**What to read first:** the module docstring, then `_classify` (about forty lines: every way
a part fails, in order), then `split`, `Rule.__post_init__` and `match`. In the tests,
`BYPASSES` is the corpus, and `test_over_splitting_only_ever_adds_parts` is the property the
whole design rests on.

**Noticed, not done:**
- **Wiring.** Nothing calls `match` yet. `grants.check` matches the exact bytes of one call
  (D62 as it was); when the dispatcher asks whether a standing rule covers a shell command,
  this is the function, and the rule file (D100: justification, examples, `host_executable`)
  is where its `Rule`s come from. Both are their own tasks.
- **The executor table is a denylist, and a denylist is never complete.** `git -c
  core.pager=X diff`, `git --exec-path`, `make`, `awk 'system()'`, `sed e`, `vim -c`, `less
  !cmd`, `python -m`, `node -r`, `perl -M`, `curl | sh` (refused today as two parts) are each
  a way for a plain-looking program to run another. D100's `host_executable` (basename rules
  apply only to listed absolute paths) and a rule author's judgement are the other half, and
  tree-sitter-bash is what the regexes grow into (CONVERSATION.md, openworker notes).
- **`find -fprint`, `-fls`, `-fprintf` write files** and are not in `EXEC_FLAGS`, which is
  D74's execution-and-deletion list. A write is a different class and belongs with the floors.
- **D62's `2>&1` convenience** is gone, by the choice above. If refusals for it turn out to
  be most of the fatigue, that is a number worth having before deciding.
- **PLAN §9's status column** is not updated, because `PLAN.md` is under CODEOWNERS.

**Cost:** not available to the author at write time. Hyperagent's figure for this thread is to
be filled in by the owner.
