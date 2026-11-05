"""Command matching: split on every separator and justify each part (D74, refining D62).

A standing rule (D62) covers a shell command. Before this module, "covers" meant the exact
bytes of one call (``walle.grants``). This is the step after: a rule is a *prefix of words*,
``git diff*``, and a command is matched against the rules the way openworker does it:

1. **Split on every separator** — ``&&``, ``||``, ``;``, ``|``, ``&`` and the newline — with
   nothing protecting one: not a quote, not a backslash, not a backtick. *"Over-splitting only
   ever produces MORE parts to justify, never fewer."* A command that passes here would pass
   a whole-text prefix check of the same rules; the reverse is false, which is the point:
   ``git status && rm -rf /`` is a prefix match for ``git status*`` and is refused here,
   because ``rm -rf /`` is a part and no rule covers it.
2. **Justify each part.** Every part must be covered by a rule. One part without one, and the
   whole command is refused, naming the part.
3. **Three classes are never eligible, whatever the rules say:**

   - *opaque constructs* — a backtick, ``$``, a redirect, a parenthesis, a brace, a glob, a
     tilde, a comment, a quote, a backslash: contents the rule never saw;
   - *argument-executors* — ``xargs``, ``env``, ``sudo``, ``nohup``, ``timeout``, ``ssh``,
     ``docker``, ``npx``, ``uvx`` and their kin (``EXECUTORS``): *"a prefix rule on the outer
     program can never vouch for the inner one"*; an assignment prefix (``FOO=bar git ...``)
     is the same hazard spelt without a program;
   - *interpreters carrying inline code* (``python -c``, ``node -e``, a ``-`` for stdin) and
     *execution flags on search tools* (``find -exec``, ``-delete``, ``fd -x``).

   A rule naming one of these cannot be made either: ``Rule`` runs its own pattern through the
   same classifier.

The text checked is the text that would run
--------------------------------------------
Nothing is unquoted, unescaped, expanded or stripped before matching. The words of a part are
the part's text split on single spaces, and a rule's words must equal them byte for byte.
``git "status"`` runs ``git status`` and is **not** covered by the rule ``git status``: the
rule never saw a quote, and what a quote hides is the shell's business, not this module's.
D62 stripped ``2>&1`` before matching, because models write it by reflex; D74 lists redirects
among the opaque constructs, and a redirect is refused here as written. The one thing done to
a part is to drop the spaces at its ends, which separators are written with, and a space is
never part of a word here because every way of putting one in a word is refused.

Fail closed (D86): anything that cannot be parsed is refused. The parser is a small one on
purpose: a word is ASCII letters, digits and ``. _ / : @ % + , - =``; anything else — a
control character, a tab, a non-ASCII separator look-alike, a zero-width space — refuses the
part, by class. ``Refused.kind`` says which class and ``Refused.part`` says which part; the
reason names neither the text nor the rule, because the caller has both and a reason is the
kind of thing that gets logged. Tree-sitter is what this grows into when the small parser
starts lying (CONVERSATION.md, openworker notes); the corpus in ``tests/test_matching.py`` is
what it will be held to.

Not here, by design
-------------------
This module decides whether a command is covered; it never runs one, reads no clock, opens no
file and reaches no model. Which rules exist and where they live is the rule file's task
(D100: justification, load-time examples, ``host_executable``); when a decision is asked for
and what happens to it is the dispatcher's (D62, D76). D74's other line, that a network client
is never read-only even for GET, is a property of a read-only classifier this module is not.

The dormant rule
----------------
Off unless the owner has set ``WALLE_MATCHING=on`` in the environment, outside the log (D117).
Off means D62 as it was: a rule covers one exact string, and ``match`` refuses every command
before splitting it. This module starts nothing and has no command.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "D74_EXECUTORS",
    "EXECUTORS",
    "EXEC_FLAGS",
    "INLINE_CODE",
    "MATCHING_SWITCH",
    "SEPARATORS",
    "SHELL_WORDS",
    "SWITCH_ON",
    "WORD_CHARS",
    "Allowed",
    "Decision",
    "Justified",
    "Refusal",
    "Refused",
    "Rule",
    "enabled",
    "justify",
    "match",
    "split",
]

MATCHING_SWITCH = "WALLE_MATCHING"
"""The environment variable the owner sets to turn split-and-justify matching on."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_GRANTS``: a stray ``1`` is not consent."""

SEPARATORS: tuple[str, ...] = ("&&", "||", ";", "|", "&", "\n")
"""Every separator D74 names, longest first so ``&&`` is one separator and not two ``&``.
Nothing protects one: a separator inside quotes, after a backslash or inside a backtick still
splits, which can only add parts, and the quote, backslash or backtick refuses its part
anyway."""

WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/:@%+,-=")
"""What a word may be made of. Everything else is refused, by class."""

_QUOTES = frozenset("'\"")
_GLOB = frozenset("*?[]")

D74_EXECUTORS: tuple[str, ...] = (
    "xargs",
    "env",
    "sudo",
    "nohup",
    "timeout",
    "ssh",
    "docker",
    "npx",
    "uvx",
)
"""The argument-executors D74 names, kept apart so a test can hold the list to the decision."""

EXECUTORS: frozenset[str] = frozenset(
    (
        *D74_EXECUTORS,
        # privilege and identity
        "doas",
        "su",
        "pkexec",
        "runuser",
        "chroot",
        "nsenter",
        # scheduling, tracing and wrapping
        "nice",
        "ionice",
        "setsid",
        "strace",
        "ltrace",
        "watch",
        "time",
        "flock",
        "parallel",
        "stdbuf",
        "unbuffer",
        "gdb",
        "lldb",
        "valgrind",
        "perf",
        "script",
        "screen",
        "tmux",
        "systemd-run",
        "at",
        "batch",
        "chpst",
        "caffeinate",
        "arch",
        # containers, package runners and multi-call binaries
        "podman",
        "pipx",
        "bunx",
        "pnpx",
        "busybox",
        "toybox",
        # the dynamic loader run by hand
        "ld.so",
        "ld-linux.so",
        "ld-linux-x86-64.so",
        "ld-linux-aarch64.so",
        # shells and shell builtins that run their argument
        "command",
        "builtin",
        "exec",
        "eval",
        "source",
        ".",
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "mksh",
        "ash",
        "csh",
        "tcsh",
        "fish",
    )
)
"""Programs whose argument is a program. Matched by basename, case-folded (a case-insensitive
filesystem runs ``SUDO``), with a trailing version dropped (``ssh2``, ``bash5``). The table
is add-only; a program noticed to belong here is a one-line addition and a corpus entry. It
is not, and cannot be, complete: D100's ``host_executable`` and a rule author's judgement
are the other half."""

SHELL_WORDS: frozenset[str] = frozenset(
    (
        # keywords: a construct, not a program
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "case",
        "esac",
        "for",
        "select",
        "while",
        "until",
        "do",
        "done",
        "in",
        "function",
        "coproc",
        # builtins that change what a later part runs or sees
        "export",
        "declare",
        "typeset",
        "local",
        "readonly",
        "unset",
        "set",
        "shopt",
        "setopt",
        "alias",
        "unalias",
        "hash",
        "enable",
        "trap",
        "ulimit",
        "umask",
    )
)
"""Shell keywords, and builtins that shape what a later part runs: ``export PATH=/tmp`` before
``git status`` makes the second part run something the rule never saw. Never a program a
rule may name."""

INLINE_CODE: Mapping[str, tuple[frozenset[str], frozenset[str]]] = MappingProxyType(
    {
        # program: (short flag letters, long flags or subcommands), each meaning "the code
        # is in the arguments". A lone ``-`` (stdin as the program) is refused for all.
        "python": (frozenset("c"), frozenset()),
        "pypy": (frozenset("c"), frozenset()),
        "node": (frozenset("ep"), frozenset({"--eval", "--print"})),
        "perl": (frozenset("eE"), frozenset()),
        "ruby": (frozenset("e"), frozenset()),
        "php": (frozenset("r"), frozenset()),
        "deno": (frozenset(), frozenset({"eval"})),
        "bun": (frozenset("ep"), frozenset({"--eval", "--print"})),
    }
)
"""Interpreters and the flags that carry inline code (D74). Matched by basename, case-folded,
with a trailing version dropped: ``python3``, ``python3.12``, ``Python`` and ``pypy3`` are all
``python`` here."""

EXEC_FLAGS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "find": frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"}),
        "fd": frozenset({"-x", "-X", "--exec", "--exec-batch"}),
    }
)
"""Search tools and the flags that make them run or delete (D74). Matched by basename; a
short flag inside a cluster (``fd -Hx``) counts."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned matching on: ``WALLE_MATCHING`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(MATCHING_SWITCH) == SWITCH_ON


# --- what comes out ----------------------------------------------------------------------------


class Refusal(StrEnum):
    """Why a part, or a command, is refused. One class per way of failing, so a surface can
    say what kind of thing it saw without repeating the text."""

    OFF = "off"
    """Matching is off; the owner has not turned it on."""
    EMPTY = "empty"
    """A part with nothing in it (``a;;b``, a trailing ``&``, a trailing newline, an empty
    command), or an empty word (two spaces in a row)."""
    CONTROL = "control"
    """A control character: a tab, a carriage return, anything below a space."""
    NON_ASCII = "non-ascii"
    """A character outside ASCII: a separator look-alike, a zero-width space, a homoglyph."""
    QUOTED = "quoted"
    """A quote. What it hides is the shell's business, not a rule's (D74: opaque)."""
    ESCAPED = "escaped"
    """A backslash (D74: opaque)."""
    GLOB = "glob"
    """A glob character: the expansion is contents the rule never saw (D74: opaque)."""
    OPAQUE = "opaque"
    """A backtick, ``$``, a redirect, a parenthesis, a brace, a tilde, a comment, or any
    other character a word is not made of (D74: opaque)."""
    ASSIGNMENT = "assignment"
    """An ``=`` in the first word: an environment assignment prefix."""
    SHELL = "shell"
    """A shell keyword, or a builtin that changes what a later part runs."""
    EXECUTOR = "executor"
    """The program is an argument-executor (D74)."""
    INLINE_CODE = "inline-code"
    """An interpreter carrying inline code, or reading its program from stdin (D74)."""
    EXEC_FLAG = "exec-flag"
    """A search tool with an execution or deletion flag (D74)."""
    NO_RULE = "no-rule"
    """The part parses and no rule covers it."""


@dataclass(frozen=True)
class Rule:
    """One standing rule: a prefix of words, with a trailing ``*`` meaning any further
    words may follow (D62's ``pattern*``). Without the star the rule is exact.

    The pattern is parsed once, here, and must itself be a part this module would accept:
    one command, plain words, no separator, no opaque construct, no executor, no inline-code
    or exec flag. A bare ``*`` is rejected (D62). ``ValueError`` names what was wrong.
    """

    pattern: str
    words: tuple[str, ...] = field(init=False, compare=False, repr=False)
    prefix: bool = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str):
            raise TypeError(f"a rule pattern is a str, not {type(self.pattern).__name__}")
        text = self.pattern
        prefix = text.endswith("*")
        if prefix:
            text = text[:-1]
            if text.endswith(" "):
                text = text[:-1]
        if not text.strip(" "):
            raise ValueError("a bare * is not a rule (D62): name the program and its words")
        if text != text.strip(" "):
            raise ValueError("a rule pattern has no spaces at its ends")
        if any(sep in text for sep in SEPARATORS):
            raise ValueError("a rule covers one command: no separator in a pattern")
        kind = _classify(text)
        if kind is not None:
            raise ValueError(f"a rule cannot be made for this pattern: {_describe(kind)}")
        object.__setattr__(self, "words", tuple(text.split(" ")))
        object.__setattr__(self, "prefix", prefix)

    def covers(self, words: tuple[str, ...]) -> bool:
        """Whether these raw words are this rule's words (exact), or begin with them
        (prefix)."""
        if self.prefix:
            return len(words) >= len(self.words) and words[: len(self.words)] == self.words
        return words == self.words


@dataclass(frozen=True)
class Justified:
    """One part and the rule that covers it. ``text`` is the part exactly as it appears in
    the command, spaces at its ends included."""

    text: str
    words: tuple[str, ...]
    rule: Rule


@dataclass(frozen=True)
class Allowed:
    """Every part is justified. ``parts`` are in command order, and joined back with their
    separators they are the command."""

    parts: tuple[Justified, ...]


@dataclass(frozen=True)
class Refused:
    """Some part is not justified, or matching is off. ``part`` is 1-based and ``0`` when
    nothing was split; ``parts`` are every part's raw text, for a surface that shows them;
    ``reason`` is one line that names the class and the part and not the text."""

    kind: Refusal
    reason: str
    part: int = 0
    parts: tuple[str, ...] = ()


type Decision = Allowed | Refused


# --- the three operations ----------------------------------------------------------------------


def split(command: str) -> tuple[str, ...]:
    """The command's parts: literal slices between separators, longest separator first.
    Nothing protects a separator. Every part is a substring of ``command`` and nothing is
    dropped, so ``parts`` joined with their separators is ``command``."""
    parts: list[str] = []
    start = 0
    i = 0
    n = len(command)
    while i < n:
        for sep in SEPARATORS:
            if command.startswith(sep, i):
                parts.append(command[start:i])
                i += len(sep)
                start = i
                break
        else:
            i += 1
    parts.append(command[start:])
    return tuple(parts)


def justify(part: str, rules: Sequence[Rule]) -> Justified | Refusal:
    """One part against the rules: the ``Refusal`` that applies, or the ``Justified`` part
    with the first rule that covers it. The never-eligible classes are decided before any
    rule is looked at."""
    kind = _classify(part)
    if kind is not None:
        return kind
    words = tuple(part.strip(" ").split(" "))
    for rule in rules:
        if rule.covers(words):
            return Justified(part, words, rule)
    return Refusal.NO_RULE


def match(
    command: str,
    rules: Sequence[Rule],
    *,
    environ: Mapping[str, str] | None = None,
) -> Decision:
    """Whether ``command`` is covered by ``rules``: every part justified, or the first that
    is not. Types are checked first; then the switch, before anything is split; a NUL is a
    ``ValueError``, because no shell takes one and a caller that passes one has a bug."""
    if not isinstance(command, str):
        raise TypeError(f"a command is a str, not {type(command).__name__}")
    if isinstance(rules, str) or not isinstance(rules, Sequence):
        raise TypeError(f"rules are a sequence of Rule, not {type(rules).__name__}")
    for rule in rules:
        if not isinstance(rule, Rule):
            raise TypeError(f"a rule is a Rule, not {type(rule).__name__}")
    if "\x00" in command:
        raise ValueError("the command carries a NUL, which no shell takes")
    if not enabled(environ):
        return Refused(
            Refusal.OFF,
            f"matching is off; the owner turns it on with {MATCHING_SWITCH}={SWITCH_ON}",
        )
    parts = split(command)
    total = len(parts)
    justified: list[Justified] = []
    for index, part in enumerate(parts, start=1):
        outcome = justify(part, rules)
        if isinstance(outcome, Refusal):
            return Refused(
                outcome,
                f"part {index} of {total} is refused: {_describe(outcome)}",
                index,
                parts,
            )
        justified.append(outcome)
    return Allowed(tuple(justified))


# --- the classifier ----------------------------------------------------------------------------


def _classify(part: str) -> Refusal | None:
    """The never-eligible classes, in the order they are looked for. ``None`` means the part
    is plain words and may be matched against a rule."""
    text = part.strip(" ")
    if not text:
        return Refusal.EMPTY
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return Refusal.CONTROL
        if code > 0x7E:
            return Refusal.NON_ASCII
    if any(ch in _QUOTES for ch in text):
        return Refusal.QUOTED
    if "\\" in text:
        return Refusal.ESCAPED
    if any(ch in _GLOB for ch in text):
        return Refusal.GLOB
    if any(ch not in WORD_CHARS and ch != " " for ch in text):
        return Refusal.OPAQUE
    words = text.split(" ")
    if any(not word for word in words):
        return Refusal.EMPTY
    program, *arguments = words
    if "=" in program:
        return Refusal.ASSIGNMENT
    if program in SHELL_WORDS:
        return Refusal.SHELL
    name = _family(program)
    if name in EXECUTORS:
        return Refusal.EXECUTOR
    if name in INLINE_CODE:
        short, long = INLINE_CODE[name]
        if any(_carries(word, short, long) or word == "-" for word in arguments):
            return Refusal.INLINE_CODE
    if name in EXEC_FLAGS:
        flags = EXEC_FLAGS[name]
        letters = frozenset(flag[1] for flag in flags if len(flag) == 2 and flag[1] != "-")
        if any(_carries(word, letters, flags) for word in arguments):
            return Refusal.EXEC_FLAG
    return None


_VERSION = re.compile(r"[-.]?[0-9][0-9.]*$")


def _family(program: str) -> str:
    """The name a program is looked up by: its basename, case-folded, and when that is not
    in a table, the same with a trailing version dropped (``python3.12`` → ``python``,
    ``ld-linux-x86-64.so.2`` → ``ld-linux-x86-64.so``). Folding only ever widens a refusal."""
    name = program.rsplit("/", 1)[-1].lower()
    if name in EXECUTORS or name in INLINE_CODE or name in EXEC_FLAGS:
        return name
    return _VERSION.sub("", name)


def _carries(word: str, short: frozenset[str], long: frozenset[str]) -> bool:
    """Whether ``word`` is one of the ``long`` flags (with or without ``=value``), or a short
    flag cluster holding one of the ``short`` letters."""
    if word in long or word.split("=", 1)[0] in long:
        return True
    if word.startswith("-") and not word.startswith("--") and len(word) > 1:
        return any(letter in short for letter in word[1:])
    return False


_DESCRIPTIONS: Mapping[Refusal, str] = MappingProxyType(
    {
        Refusal.OFF: "matching is off",
        Refusal.EMPTY: "an empty part or an empty word",
        Refusal.CONTROL: "a control character",
        Refusal.NON_ASCII: "a character outside ASCII",
        Refusal.QUOTED: "a quote, which hides what the rule would have to see (D74)",
        Refusal.ESCAPED: "a backslash, which hides what the rule would have to see (D74)",
        Refusal.GLOB: "a glob, whose expansion the rule never saw (D74)",
        Refusal.OPAQUE: "an opaque construct: a substitution, redirect, subshell or the like (D74)",
        Refusal.ASSIGNMENT: "an environment assignment before the program",
        Refusal.SHELL: "a shell keyword, or a builtin that changes what a later part runs",
        Refusal.EXECUTOR: (
            "an argument-executor: a prefix rule on the outer program can never vouch for the "
            "inner one (D74)"
        ),
        Refusal.INLINE_CODE: "an interpreter carrying inline code (D74)",
        Refusal.EXEC_FLAG: "a search tool with an execution or deletion flag (D74)",
        Refusal.NO_RULE: "no rule covers it",
    }
)


def _describe(kind: Refusal) -> str:
    return _DESCRIPTIONS[kind]
