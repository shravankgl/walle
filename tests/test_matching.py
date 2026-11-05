"""Command matching: split on every separator and justify each part (D74).

Every command here is synthetic. Nothing is run: ``match`` decides, and the tests read the
decision.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from walle import matching
from walle.matching import (
    MATCHING_SWITCH,
    SWITCH_ON,
    Allowed,
    Refusal,
    Refused,
    Rule,
    match,
    split,
)

ON = {MATCHING_SWITCH: SWITCH_ON}

GIT_STATUS = Rule("git status")
GIT_DIFF = Rule("git diff*")
FIND = Rule("find*")
LS = Rule("ls*")
RULES = (GIT_STATUS, GIT_DIFF, FIND, LS)


def refused(command: str, rules: tuple[Rule, ...] = RULES) -> Refused:
    decision = match(command, rules, environ=ON)
    assert isinstance(decision, Refused), f"{command!r} was allowed by {decision}"
    return decision


def allowed(command: str, rules: tuple[Rule, ...] = RULES) -> Allowed:
    decision = match(command, rules, environ=ON)
    assert isinstance(decision, Allowed), f"{command!r} was refused: {decision}"
    return decision


# --- the verify criterion ----------------------------------------------------------------------


def test_both_parts_matching_passes() -> None:
    """`git status && git diff` passes when both match."""
    decision = allowed("git status && git diff")
    assert [part.text for part in decision.parts] == ["git status ", " git diff"]
    assert [part.rule for part in decision.parts] == [GIT_STATUS, GIT_DIFF]


def test_find_exec_fails_under_a_find_rule() -> None:
    """`find . -exec rm {} +` fails under a `find` rule."""
    decision = refused("find . -exec rm {} +", (FIND,))
    assert decision.part == 1
    # It is refused for what it is, not for failing to match: the braces alone would refuse
    # it, and the -exec flag alone would too.
    assert decision.kind in (Refusal.EXEC_FLAG, Refusal.OPAQUE)
    plain = refused("find . -name x -exec rm", (FIND,))
    assert plain.kind is Refusal.EXEC_FLAG


def test_one_unjustified_part_refuses_the_whole() -> None:
    decision = refused("git status && rm -rf /")
    assert decision.part == 2
    assert decision.kind is Refusal.NO_RULE
    assert "part 2 of 2" in decision.reason
    assert decision.parts == ("git status ", " rm -rf /")


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git diff --stat HEAD",
        "git diff -- src/walle/matching.py",
        "git status && git diff",
        "git status; git diff",
        "git status || git diff",
        "git status\ngit diff",
        "git status | ls -l",
        "git status && git diff --cached && ls -la /tmp/walle",
        "  git status  ",
        "ls",
        "ls -la --color=never /var/log",
    ],
)
def test_plain_commands_under_plain_rules_pass(command: str) -> None:
    allowed(command)


# --- the bypass corpus: every one refused -------------------------------------------------------

BYPASSES: dict[str, list[str]] = {
    "quoting": [
        'git "status"',
        "git 'status'",
        'g"it" status',
        "gi't' status",
        '"git" status',
        "git status ''",
        'git diff "$(rm -rf /)"',
        "git diff 'a; rm -rf /'",
        'git status && "rm" -rf /',
        'git diff -- "x"',
    ],
    "escapes": [
        "git\\ status",
        "gi\\t status",
        "git status \\; rm -rf /",
        "git diff \\$HOME",
        "git status \\\n",
        "git\\\nstatus",
        "git diff -- a\\ b",
    ],
    "newlines": [
        "git status\nrm -rf /",
        "git status\n\nrm -rf /",
        "git status\r\ngit diff",
        "git status\rrm -rf /",
        "git status\n",
        "\ngit status",
        "git\nstatus",
        "git status\n;rm -rf /",
    ],
    "unicode separators": [
        "git status\u2028rm -rf /",
        "git status\u2029rm -rf /",
        "git status\u0085rm -rf /",
        "git status\uff1brm -rf /",  # fullwidth semicolon
        "git status\uff06\uff06rm -rf /",  # fullwidth ampersands
        "git status\u037erm -rf /",  # Greek question mark, looks like ;
        "git status\u00a0&&\u00a0rm -rf /",  # no-break spaces
        "git\u2003status",  # em space
        "git status\u200b&& rm -rf /",  # zero-width space
        "git status\u2062&& rm -rf /",  # invisible times
        "git status\u3000&& rm -rf /",  # ideographic space
        "git status\ufeff",  # BOM
        "g\u0131t status",  # dotless i
        "git \u0455tatus",  # Cyrillic dze, a homoglyph of s
        "ls \u2015 /",
    ],
    "env assignment prefixes": [
        "FOO=bar git status",
        "LD_PRELOAD=/tmp/x.so git status",
        "PATH=/tmp git status",
        "GIT_SSH_COMMAND=rm git status",
        "A=1 B=2 git status",
        "git status && FOO=bar git diff",
        "=git status",
        "git=rm status",
        "a=b",
    ],
    "argument executors": [
        "xargs git status",
        "xargs rm -rf",
        "env git status",
        "env -i git status",
        "sudo git status",
        "sudo -u root git status",
        "nohup git status",
        "timeout 10 git status",
        "timeout 10s ls",
        "ssh host git status",
        "ssh -T host ls",
        "docker run alpine ls",
        "docker exec c ls",
        "npx git status",
        "npx -y pkg",
        "uvx git status",
        "uvx ruff check",
        "/usr/bin/xargs ls",
        "/usr/bin/env git status",
        "/usr/bin/sudo ls",
        "/bin/sh -c ls",
        "/usr/local/bin/timeout 5 ls",
        "git status && xargs rm",
        "git status | xargs rm",
        "ls | xargs rm -rf",
        "git status && env",
        "git status; sudo ls",
        "git status\nnohup ls",
        "git status || timeout 5 ls",
        "git status & ssh host ls",
        "git status && docker ps",
        "git status && npx x",
        "git status && uvx x",
        "doas ls",
        "su root -c ls",
        "pkexec ls",
        "nice ls",
        "nice -n 5 git status",
        "ionice ls",
        "setsid ls",
        "chroot / ls",
        "nsenter -t 1 ls",
        "strace ls",
        "ltrace ls",
        "watch ls",
        "time ls",
        "flock /tmp/l ls",
        "parallel ls",
        "podman run x ls",
        "pipx run x",
        "bunx x",
        "pnpx x",
        "command ls",
        "builtin cd",
        "exec ls",
        "eval ls",
        "source x.sh",
        ". x.sh",
        "sh x.sh",
        "sh -c ls",
        "bash -c ls",
        "bash x.sh",
        "zsh -c ls",
        "dash -c ls",
        "ksh -c ls",
        "fish -c ls",
        "busybox sh",
        "busybox ls",
        "toybox ls",
        "gdb -ex run ls",
        "lldb -- ls",
        "valgrind ls",
        "perf stat ls",
        "script -c ls /dev/null",
        "screen ls",
        "tmux new ls",
        "systemd-run ls",
        "at now -f x.sh",
        "caffeinate -i ls",
        "arch -x86_64 ls",
        "/lib64/ld-linux-x86-64.so.2 /bin/ls",
        "/lib/ld-linux-aarch64.so.1 /bin/ls",
        "mksh -c ls",
        "ash -c ls",
        "csh -c ls",
        "tcsh -c ls",
        # case and version: a case-insensitive filesystem runs SUDO, and bash5 is bash
        "SUDO ls",
        "Xargs ls",
        "/usr/bin/ENV git status",
        "BASH -c ls",
        "bash5 -c ls",
        "ssh2 host ls",
        "Docker run x ls",
    ],
    "shell keywords and state builtins": [
        "export PATH=/tmp; git status",
        "export GIT_SSH_COMMAND=rm && git status",
        "declare -x PATH=/tmp; git status",
        "typeset -x PATH=/tmp; git status",
        "local PATH=/tmp; git status",
        "readonly PATH=/tmp",
        "unset PATH; git status",
        "set -x; git status",
        "shopt -s expand_aliases; git status",
        "alias git=rm; git status",
        "unalias git",
        "hash -p /tmp/x git; git status",
        "enable -f /tmp/x.so ls",
        "trap ls EXIT",
        "ulimit -n 1",
        "umask 000; git status",
        "for x in a; do ls; done",
        "while true; do ls; done",
        "until false; do ls; done",
        "if true; then ls; fi",
        "if true; then ls; else ls; fi",
        "case x in x; esac",
        "select x in a; do ls; done",
        "function f; ls; done",
        "coproc ls",
        "then ls",
        "do ls",
        "in x",
    ],
    "interpreters carrying inline code": [
        "python -c ls",
        "python3 -c ls",
        "python3 -ic ls",
        "python3 -Bc ls",
        "python3 -",
        "node -e ls",
        "node -p ls",
        "node --eval ls",
        "node --print ls",
        "node -pe ls",
        "node -",
        "perl -e ls",
        "perl -E ls",
        "perl -ne ls",
        "perl -",
        "ruby -e ls",
        "ruby -ne ls",
        "ruby -",
        "php -r ls",
        "deno eval ls",
        "bun -e ls",
        "bun --eval ls",
        "/usr/bin/python3 -c ls",
        "Python3 -c ls",
        "python3.12 -c ls",
        "python3.12 -ic ls",
        "python2 -c ls",
        "pypy3 -c ls",
        "node18 -e ls",
        "node22 --eval=ls",
        "perl5.36 -e ls",
        "ruby3.3 -e ls",
        "php8.2 -r ls",
        "python -",
        "PYTHON -c ls",
    ],
    "execution flags on search tools": [
        "find . -exec rm",
        "find . -execdir rm",
        "find . -ok rm",
        "find . -okdir rm",
        "find . -delete",
        "find . -name x -delete",
        "find / -type f -exec cat",
        "fd . -x rm",
        "fd . -X rm",
        "fd . --exec rm",
        "fd . --exec-batch rm",
        "fd -x rm",
        "fd . -Hx rm",
        "fd . --exec=rm",
        "FIND . -delete",
        "/usr/bin/find . -exec rm",
        "Fd . -x rm",
    ],
    "opaque constructs": [
        "git status `rm -rf /`",
        "git diff $(rm -rf /)",
        "git diff $HOME",
        "git diff ${HOME}",
        "git diff $",
        "git status > /tmp/out",
        "git status >> /tmp/out",
        "git status < /tmp/in",
        "git status 2>&1",
        "git status 2>/dev/null",
        "git status &> /tmp/out",
        "git status <<< x",
        "git status <<EOF",
        "git status |& ls",
        "(git status)",
        "( git status )",
        "git status && (ls)",
        "{ git status; }",
        "git diff {a,b}",
        "git diff a{1..3}",
        "git diff ~",
        "git diff ~/x",
        "git diff *",
        "git diff ?",
        "git diff [a]",
        "git diff !x",
        "!git status",
        "git status # && rm -rf /",
        "git status #",
        "git status ^",
        "git diff --output=x>y",
        "git diff --output=x|y",
        "git diff a;b",
    ],
    "separator edge cases": [
        "",
        " ",
        "\n",
        ";",
        "&&",
        "git status &&",
        "git status;",
        "git status &",
        "git status & ",
        "&& git status",
        "; git status",
        "| git status",
        "git status && && git diff",
        "git status ;; git diff",
        "git status ||| git diff",
        "git status &&& git diff",
        "git status |  | git diff",
        "git status\n&& git diff",
        "git status &&\n",
    ],
    "near misses of the rule itself": [
        "git  status",  # two spaces: still not the rule text, and exact means exact
        "git\tstatus",
        "git status extra",  # GIT_STATUS has no *
        "git statuss",
        "gitstatus",
        "git stat",
        "git",
        "Git status",
        "GIT STATUS",
        "git diffs",
        "git difftool",
        "./git status",
        "/usr/bin/git status",
        "-git status",
        "git --git-dir=/x status",
        "git -C /tmp status",
        "git -c core.pager=rm status",
        "git status status",
        "status git",
        "ls&&rm",
        "lsx",
        "ls2",
    ],
}


@pytest.mark.parametrize(
    "command",
    [c for group in BYPASSES.values() for c in group],
    ids=[
        f"{name}:{i}:{c!r}"
        for name, group in BYPASSES.items()
        for i, c in enumerate(group, start=1)
    ],
)
def test_every_bypass_is_refused(command: str) -> None:
    """A corpus of bypass attempts all fail."""
    refused(command)


def test_the_corpus_covers_every_class_d74_names() -> None:
    """A reader can check the corpus against D74's list by heading."""
    assert set(BYPASSES) >= {
        "quoting",
        "escapes",
        "newlines",
        "unicode separators",
        "env assignment prefixes",
        "argument executors",
        "shell keywords and state builtins",
        "interpreters carrying inline code",
        "execution flags on search tools",
        "opaque constructs",
        "separator edge cases",
    }
    corpus = "\n".join(c for group in BYPASSES.values() for c in group)
    for named in matching.D74_EXECUTORS:
        assert re.search(rf"(^|[\s/]){re.escape(named)}(\s|$)", corpus, re.M), named


def test_the_corpus_is_refused_under_a_wide_rule_too() -> None:
    """A prefix rule on every program the corpus uses still refuses every bypass: the refusals
    are about what the command is, not about which rules happen to exist."""
    wide = (
        Rule("git*"),
        Rule("ls*"),
        Rule("find*"),
        Rule("fd*"),
        Rule("python*"),
        Rule("python3*"),
        Rule("node*"),
        Rule("perl*"),
        Rule("ruby*"),
        Rule("php*"),
        Rule("deno*"),
        Rule("bun*"),
        Rule("cd*"),
        Rule("x*"),
        Rule("x.sh*"),
        Rule("pkg*"),
        Rule("root*"),
        Rule("host*"),
        Rule("alpine*"),
        Rule("c*"),
    )
    for group, commands in BYPASSES.items():
        if group == "near misses of the rule itself":
            continue  # those exist to miss narrow rules; a wide rule is meant to catch some
        for command in commands:
            refused(command, wide)


def test_an_executor_is_refused_before_matching_is_tried() -> None:
    """Never eligible, regardless of match: a rule naming the executor cannot be made, and a
    command naming it is refused for that reason even when a wide rule would cover it."""
    with pytest.raises(ValueError, match="argument-executor"):
        Rule("xargs*")
    with pytest.raises(ValueError, match="argument-executor"):
        Rule("sudo git status")
    with pytest.raises(ValueError, match="argument-executor"):
        Rule("/usr/bin/env git status")
    decision = refused("xargs ls", (Rule("x*"), LS))
    assert decision.kind is Refusal.EXECUTOR


def test_a_refusal_names_the_class_and_the_part_never_the_text() -> None:
    """The caller has the command; the reason says which part and why, and does not echo
    the command's text into a message that may be logged or shown elsewhere."""
    decision = refused("git status && git diff $(cat /tmp/secret-synthetic)")
    assert decision.part == 2
    assert decision.kind is Refusal.OPAQUE
    assert "secret-synthetic" not in decision.reason
    assert "part 2 of 2" in decision.reason
    assert decision.parts[1] == " git diff $(cat /tmp/secret-synthetic)"


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("git status 2>&1", Refusal.OPAQUE),
        ("git diff $(x)", Refusal.OPAQUE),
        ("git diff `x`", Refusal.OPAQUE),
        ("git diff 'x'", Refusal.QUOTED),
        ('git diff "x"', Refusal.QUOTED),
        ("git diff \\x", Refusal.ESCAPED),
        ("git diff *", Refusal.GLOB),
        ("git status\rgit diff", Refusal.CONTROL),
        ("git status\tx", Refusal.CONTROL),  # a tab is not the space the rule has
        ("git  status", Refusal.EMPTY),  # two spaces hold an empty word
        ("git diff \u00e9", Refusal.NON_ASCII),
        ("FOO=1 git status", Refusal.ASSIGNMENT),
        ("sudo ls", Refusal.EXECUTOR),
        ("SUDO ls", Refusal.EXECUTOR),
        ("export PATH=/tmp", Refusal.SHELL),
        ("for x in a", Refusal.SHELL),
        ("python3 -c x", Refusal.INLINE_CODE),
        ("python3.12 -c x", Refusal.INLINE_CODE),
        ("find . -delete", Refusal.EXEC_FLAG),
        ("git status;", Refusal.EMPTY),
        ("", Refusal.EMPTY),
        ("rm -rf /", Refusal.NO_RULE),
    ],
)
def test_each_refusal_class_is_reported(command: str, kind: Refusal) -> None:
    assert refused(command).kind is kind


def test_the_first_failing_part_is_reported_in_order() -> None:
    decision = refused("git status && sudo ls && rm -rf /")
    assert (decision.part, decision.kind) == (2, Refusal.EXECUTOR)


# --- splitting ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "parts"),
    [
        ("git status", ["git status"]),
        ("git status && git diff", ["git status ", " git diff"]),
        ("a||b", ["a", "b"]),
        ("a;b", ["a", "b"]),
        ("a|b", ["a", "b"]),
        ("a&b", ["a", "b"]),
        ("a\nb", ["a", "b"]),
        ("a&&b||c;d|e&f\ng", ["a", "b", "c", "d", "e", "f", "g"]),
        ("a;;b", ["a", "", "b"]),
        ("a |& b", ["a ", "", " b"]),
        ("a &", ["a ", ""]),
        ("a\n", ["a", ""]),
        ("", [""]),
        ('echo "a && b"', ['echo "a ', ' b"']),  # quotes do not protect a separator
        ("echo 'a | b'", ["echo 'a ", " b'"]),
        ("a \\&\\& b", ["a \\", "\\", " b"]),  # nor does a backslash
        ("a `b; c`", ["a `b", " c`"]),
    ],
)
def test_split_is_on_every_separator_with_nothing_protecting_one(
    command: str, parts: list[str]
) -> None:
    assert list(split(command)) == parts


def test_split_is_total_and_loses_nothing() -> None:
    """Every part is a literal slice of the command: joining them with the separators back
    gives the command, so what is checked is the text that would run."""
    for group in BYPASSES.values():
        for command in group:
            parts = split(command)
            rest = command
            for part in parts:
                assert rest.startswith(part), (command, part)
                rest = rest[len(part) :]
                for separator in matching.SEPARATORS:
                    if rest.startswith(separator):
                        rest = rest[len(separator) :]
                        break
            assert rest == "", (command, rest)


def test_over_splitting_only_ever_adds_parts() -> None:
    """The property D74 relies on: a command that passes split-and-justify also passes a
    whole-text prefix check against the same rules, so splitting can only ever refuse more.
    The reverse is not true (`git status && rm -rf /` passes a whole-text prefix check of
    `git status*`), which is the point."""
    rules = (Rule("git status*"), GIT_DIFF, LS)

    def whole_text_prefix(command: str) -> bool:
        text = command.strip(" ")
        return any(
            text == " ".join(rule.words) or (rule.prefix and text.startswith(" ".join(rule.words)))
            for rule in rules
        )

    commands = [
        "git status",
        "git status && git diff",
        "git status; git diff; ls",
        "git status && rm -rf /",
        "ls | ls",
        "git status\ngit diff",
        "git status | rm -rf /",
        "git diff --stat",
    ]
    for command in commands:
        assert len(split(command)) == 1 + len(re.findall(r"&&|\|\||[;|&\n]", command))
        if isinstance(match(command, rules, environ=ON), Allowed):
            assert whole_text_prefix(command), command
    assert whole_text_prefix("git status && rm -rf /")
    assert isinstance(match("git status && rm -rf /", rules, environ=ON), Refused)
    assert whole_text_prefix("git status | rm -rf /")
    assert isinstance(match("git status | rm -rf /", rules, environ=ON), Refused)


def test_the_separators_are_the_ones_d74_names() -> None:
    assert set(matching.SEPARATORS) == {"&&", "||", ";", "|", "&", "\n"}
    # Longest first, so `&&` is one separator and not two `&`.
    assert list(matching.SEPARATORS)[:2] == ["&&", "||"]


# --- rules -------------------------------------------------------------------------------------


def test_a_rule_is_exact_without_a_star_and_a_prefix_with_one() -> None:
    assert GIT_STATUS.words == ("git", "status")
    assert not GIT_STATUS.prefix
    assert GIT_DIFF.words == ("git", "diff")
    assert GIT_DIFF.prefix
    detached = Rule("git diff *")
    assert (detached.words, detached.prefix) == (GIT_DIFF.words, GIT_DIFF.prefix)
    assert detached != GIT_DIFF  # a rule is the pattern as the person wrote it
    refused("git status --short", (GIT_STATUS,))
    allowed("git diff --short", (GIT_DIFF,))
    allowed("git diff", (GIT_DIFF,))


@pytest.mark.parametrize(
    "pattern",
    [
        "*",
        " *",
        "* ",
        "",
        " ",
        "git * status",
        "git *status",
        "git status**",
        "git status *x",
        "git 'status'",
        'git "status"',
        "git status;",
        "git status && git diff",
        "git status | ls",
        "git status > x",
        "git $(x)",
        "git `x`",
        "git ~",
        "git status\n",
        "git\tstatus",
        "FOO=1 git status",
        "git=x",
        "gït status",
        "git status\u00a0",
        "#git",
        "find -exec*",
        "find . -delete",
        "python3 -c*",
        "node -e x",
        "xargs*",
        "sudo*",
        "env*",
        "nohup*",
        "timeout*",
        "ssh*",
        "docker*",
        "npx*",
        "uvx*",
        "bash -c*",
        "/usr/bin/xargs*",
        "SUDO*",
        "bash5*",
        "python3.12 -c*",
        "export*",
        "export PATH=/tmp",
        "for*",
        "if true",
        "set -x",
        "alias*",
        "trap*",
    ],
)
def test_a_rule_that_cannot_be_matched_safely_cannot_be_made(pattern: str) -> None:
    with pytest.raises(ValueError):
        Rule(pattern)


def test_a_glued_star_is_still_a_prefix_of_whole_words() -> None:
    """D62 writes `gh pr list*`. The star means further words, never the rest of a word:
    `git st*` covers `git st` and `git st x`, and not `git status`."""
    rule = Rule("git st*")
    assert rule.words == ("git", "st") and rule.prefix
    allowed("git st", (rule,))
    allowed("git st x", (rule,))
    refused("git status", (rule,))
    refused("git stx", (rule,))


def test_a_rule_is_a_str_pattern() -> None:
    with pytest.raises(TypeError):
        Rule(["git", "status"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Rule(None)  # type: ignore[arg-type]


def test_rules_are_matched_against_raw_words_never_an_unquoted_copy() -> None:
    """What a shell would run for `git "status"` is `git status`. The rule `git status`
    still does not cover it, because the text checked is the text given."""
    refused('git "status"', (GIT_STATUS,))
    refused("git 'status'", (GIT_STATUS,))
    refused("git\\ status", (GIT_STATUS,))
    refused("gi\\t status", (GIT_STATUS,))


def test_the_first_covering_rule_is_the_one_reported() -> None:
    decision = allowed("git diff", (Rule("git*"), GIT_DIFF))
    assert decision.parts[0].rule == Rule("git*")


def test_match_checks_types_before_anything_else() -> None:
    with pytest.raises(TypeError):
        match(b"git status", RULES, environ=ON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        match("git status", ("git status",), environ=ON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        match("git status", "git status", environ=ON)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="NUL"):
        match("git status\x00", RULES, environ=ON)


def test_no_rules_means_nothing_is_allowed() -> None:
    assert refused("git status", ()).kind is Refusal.NO_RULE


def test_allowed_parts_cover_the_whole_command() -> None:
    decision = allowed("git status && git diff;ls")
    assert "&&".join(p.text for p in decision.parts[:2]) + ";" + decision.parts[2].text == (
        "git status && git diff;ls"
    )


# --- the tables --------------------------------------------------------------------------------


def test_the_executor_table_holds_every_one_d74_names() -> None:
    assert set(matching.D74_EXECUTORS) == {
        "xargs",
        "env",
        "sudo",
        "nohup",
        "timeout",
        "ssh",
        "docker",
        "npx",
        "uvx",
    }
    assert set(matching.D74_EXECUTORS) <= set(matching.EXECUTORS)


def test_the_exec_flags_cover_find_and_fd() -> None:
    assert {"-exec", "-execdir", "-ok", "-okdir", "-delete"} <= set(matching.EXEC_FLAGS["find"])
    assert {"-x", "-X", "--exec", "--exec-batch"} <= set(matching.EXEC_FLAGS["fd"])


def test_an_executor_reached_by_path_is_still_an_executor() -> None:
    for path in ("/usr/bin/xargs", "/bin/sh", "./sudo", "bin/env", "../../timeout"):
        assert refused(f"{path} ls", (Rule("ls*"), Rule("bin*"))).kind is Refusal.EXECUTOR


def test_folding_a_name_only_ever_widens_a_refusal() -> None:
    """Case and version are folded for the lookup only. A rule still matches the raw word,
    so folding cannot make a command pass that would not have."""
    assert matching._family("/usr/bin/Python3.12") == "python"
    assert matching._family("SUDO") == "sudo"
    assert matching._family("/lib64/ld-linux-x86-64.so.2") == "ld-linux-x86-64.so"
    assert matching._family("7z") == "7z"
    assert matching._family("base64") == "base"
    assert matching._family("md5sum") == "md5sum"
    refused("Git status", (GIT_STATUS,))  # raw words, so case is not folded for the match
    refused("base64 x", (Rule("base*"),))  # nor is a version: base64 is not base
    allowed("base64 x", (Rule("base64*"),))


def test_a_shell_word_is_never_a_program() -> None:
    for word in sorted(matching.SHELL_WORDS):
        assert refused(f"{word} x", (Rule("x*"),)).kind is Refusal.SHELL, word
        with pytest.raises(ValueError):
            Rule(f"{word}*")


# --- dormant, default off ----------------------------------------------------------------------


def test_matching_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's condition. Switch unset: a command every rule covers is refused, and the
    refusal says why."""
    monkeypatch.delenv(MATCHING_SWITCH, raising=False)
    assert not matching.enabled()
    assert not matching.enabled({})
    off = match("git status", RULES)
    assert isinstance(off, Refused)
    assert off.kind is Refusal.OFF
    assert "off" in off.reason and MATCHING_SWITCH in off.reason
    still_off = match("git status", RULES, environ={})
    assert isinstance(still_off, Refused)
    assert still_off.kind is Refusal.OFF


@pytest.mark.parametrize("value", ["1", "true", "ON", "On", "yes", " on", "on "])
def test_only_the_one_spelling_turns_matching_on(value: str) -> None:
    assert not matching.enabled({MATCHING_SWITCH: value})
    assert matching.enabled({MATCHING_SWITCH: SWITCH_ON})


def test_while_off_nothing_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("a command was parsed while off")

    monkeypatch.setattr(matching, "split", boom)
    monkeypatch.setattr(matching, "justify", boom)
    monkeypatch.setattr(matching, "_classify", boom)
    off = match("git status", RULES, environ={})
    assert isinstance(off, Refused)
    assert off.kind is Refusal.OFF


def test_the_module_starts_nothing_runs_nothing_and_reads_no_clock() -> None:
    """Like the grants: no clock, no environment read outside ``enabled``, no command run.
    A matcher that could run what it matches would be the bug it exists to prevent."""
    source = (Path(__file__).parent.parent / "src" / "walle" / "matching.py").read_text()
    for forbidden in (
        "now_instant",
        "datetime",
        "time.",
        "subprocess",
        "os.system",
        "os.exec",
        "os.spawn",
        "os.popen",
        "shlex",
        "click",
        "threading",
        "import pty",
    ):
        assert forbidden not in source, forbidden
    assert source.count("os.environ") == 1
    assert "open(" not in source
