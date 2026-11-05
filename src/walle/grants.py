"""Grant scopes: this request, this session, always, config (D62, D73, D116, D72).

Rung 3 is a protocol, not a prompt. When a person approves an action, the approval covers one
exact call, and how long it lasts is a scope the person chose (D73):

``request``  the default. Covers the same call for the rest of this request, and is cleared at
             the run boundary: *"'Allow for this request' never outlives the answer the user
             was watching."* Lives in memory only.
``session``  the middle tier, the approval-fatigue answer. Covers the same call until the
             session ends. Lives in memory only.
``always``   only the literal word writes a standing rule (D62). Appended to the principal's
             ``grants/always.jsonl``, which the kernel alone writes (D72). When it cannot be
             saved, the grant **degrades to session scope, never to nothing**, and the
             ``Granted.note`` says so (D73).
``config``   the ceiling: ``grants/config.jsonl``, human-edited only (D72). This module reads
             it and nothing here writes it. ``grant`` refuses the scope outright.

The key
-------
A grant is keyed by the exact call: tool name and arguments, byte for byte, through one
function, ``key_for``, that both ``grant`` and ``check`` call. A trailing space, a case change
or a second command appended is another call (D62: matched exactly; D74's splitting is its
own task). The ``destructive`` flag is in the key too, so a grant given while a tool was
declared safe does not carry over once it is declared destructive (D75: overrides only
tighten). The caller passes the call as it will be *applied* — the kernel's dispatcher has
the same ``Call`` in hand when it asks and when it runs — and a grant checked against a
paraphrase would be the bug this module exists to prevent.

Once, never always (D116)
-------------------------
A destructive call is offered ``request`` and nothing wider: ``offered`` is the one list, used
by the surface that draws the buttons and by ``grant`` to refuse what was not offered. The
key such an approval writes carries the call id, so it matches that one call and nothing
afterwards — not the same command from the next call, even inside the request. Because only a
per-call key can cover a destructive call, and neither rule file carries one, no file ever
covers a destructive call, whatever it says.

Fail closed (D86)
-----------------
A grant that cannot be resolved is no grant. A rule file with one line that does not parse
is not a rule file: nothing in it applies, and ``problems`` says which line. A rule from a
newer build (``v`` past ``KNOWN_V``) is unresolvable, as a newer event is (D111). A rule file
that is a link is not read: the floor stops the agent writing ``grants/``, and a link there
would carry a rule, or a grant, somewhere else. A file that cannot be read for any other
reason is reported by its error class, never its message. A grant the person gave this
session is still resolvable when the always file is broken, so it still applies; the problem
is reported beside it.

Types are checked before anything is read or written: a scope given as a plain string, a
call that is not a ``Call``, a ``Live`` that is not one, are ``TypeError``; a tool name, call
id, session, request or timestamp of the wrong shape is ``ValueError``. ``check`` and
``grant`` raise these rather than refuse, because a wrong type is the caller's bug, not a
person's decision.

The dormant rule
----------------
Off unless the owner has set ``WALLE_GRANTS=on`` in the environment, outside the log (D117).
Off means rung 3 as it was: every action is asked about, with nothing remembered. ``grant``
raises before anything is read or written, and ``check`` refuses before any file is opened,
with a grant in memory or a rule in config or not. This module starts nothing, reads no clock
(``ts`` is passed in), and has no command.

Two writers, step by step
-------------------------
Kernels A and B, for the same principal, each saving an ``always`` grant.

1. A takes ``grants/always.lock`` (exclusive). B blocks, having opened nothing else.
2. A checks that ``always.jsonl`` is not a link, opens it for append, writes its one line,
   ``fsync``s, closes, and releases the lock.
3. B wakes, makes the same check, appends its line after A's, ``fsync``s, releases.

Either order gives a file of two whole lines. A reader (``check``) takes no lock: it reads
whole lines, and a line a writer is mid-way through has no newline yet, so it is read on the
next check and not before. A kernel killed between open and ``fsync`` may leave a partial
last line; the next ``check`` reports that line and applies nothing from the file until a
person looks, which is the fail-closed answer, not a repair.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from walle.events import CallId, Instant
from walle.paths import principal_dir

if sys.platform == "win32":  # pragma: no cover - POSIX only in Phase 0
    raise ImportError(
        "walle's grants need POSIX file locking (fcntl); Windows is not supported in Phase 0. "
        "Run it under WSL."
    )

import fcntl

__all__ = [
    "ALWAYS_FILE",
    "CONFIG_FILE",
    "GRANTABLE",
    "GRANTS_DIRNAME",
    "GRANTS_SWITCH",
    "KNOWN_V",
    "LOCK_FILE",
    "ONCE",
    "SCOPES",
    "SWITCH_ON",
    "TOOL_NAME_CHARS",
    "TOOL_NAME_RE",
    "Allowed",
    "AlwaysRule",
    "Call",
    "ConfigRule",
    "Decision",
    "Grant",
    "Granted",
    "GrantsOffError",
    "Key",
    "Live",
    "NotGrantableError",
    "Refused",
    "Scope",
    "check",
    "enabled",
    "grant",
    "grants_dir",
    "key_for",
    "offered",
]

GRANTS_SWITCH = "WALLE_GRANTS"
"""The environment variable the owner sets to turn grants on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_MISSIONS``, ``WALLE_INBOX`` and
``WALLE_REVIEWER``: a stray ``1`` is not consent."""

GRANTS_DIRNAME = "grants"
"""``principals/p-<name>/grants/``: under a floor, so the agent cannot write it (D72)."""

ALWAYS_FILE = "always.jsonl"
"""The standing rules the person gave with the word ``always``. Kernel-written, append-only."""

CONFIG_FILE = "config.jsonl"
"""The ceiling. Human-edited only; this module reads it and never writes it (D72)."""

LOCK_FILE = "always.lock"
"""Held while one ``always`` line is appended."""

KNOWN_V = 1
"""The rule schema this build writes and reads. A higher ``v`` is a newer build's rule, and
is refused rather than guessed at (D111)."""

TOOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
"""The one shape a tool name may have, the same as ``walle.reviewer.TOOL_NAME_RE``: a plain
dotted identifier. A test holds the two equal."""

TOOL_NAME_CHARS = 64

_CALL_ID: TypeAdapter[str] = TypeAdapter(CallId)
_INSTANT: TypeAdapter[str] = TypeAdapter(Instant)


class GrantsOffError(Exception):
    """The owner has not turned grants on. Raised by ``grant`` before anything is read or
    written."""


class NotGrantableError(Exception):
    """The scope asked for is not one this call may be granted: ``config`` is human-edited,
    and a destructive call is approved once, never wider (D72, D116). Raised before anything
    is written."""


class Scope(StrEnum):
    """How long an approval lasts, narrowest first (D73)."""

    REQUEST = "request"
    SESSION = "session"
    ALWAYS = "always"
    CONFIG = "config"


SCOPES: tuple[Scope, ...] = (Scope.REQUEST, Scope.SESSION, Scope.ALWAYS, Scope.CONFIG)
"""Every scope, narrowest first: the order ``check`` looks, so the narrowest cover is the one
reported."""

GRANTABLE: tuple[Scope, ...] = (Scope.REQUEST, Scope.SESSION, Scope.ALWAYS)
"""What a person may be offered for an ordinary call. ``config`` is never offered."""

ONCE: tuple[Scope, ...] = (Scope.REQUEST,)
"""What a person may be offered for a destructive call (D116)."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned grants on: ``WALLE_GRANTS`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(GRANTS_SWITCH) == SWITCH_ON


# --- the call and its key ----------------------------------------------------------------------


def _check_tool(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"a tool name is a str, not {type(value).__name__}")
    if len(value) > TOOL_NAME_CHARS or not TOOL_NAME_RE.fullmatch(value):
        raise ValueError(
            f"not a tool name: {value!r} — a dotted identifier of at most {TOOL_NAME_CHARS} "
            "characters, like calendar.read"
        )
    return value


@dataclass(frozen=True)
class Call:
    """One call as it will be applied: the tool's name, its arguments as the agent gave them,
    and whether the tool is declared destructive (D75). Types are checked here, once, so
    nothing downstream has to."""

    tool: str
    arguments: str
    destructive: bool = False

    def __post_init__(self) -> None:
        _check_tool(self.tool)
        if not isinstance(self.arguments, str):
            raise TypeError(f"arguments are a str, not {type(self.arguments).__name__}")
        if "\x00" in self.arguments:
            raise ValueError("arguments carry a NUL, which no tool takes and no file stores")
        if not isinstance(self.destructive, bool):
            raise TypeError(f"destructive is a bool, not {type(self.destructive).__name__}")


@dataclass(frozen=True)
class Key:
    """What a grant covers: the exact call, and for a destructive call, the one call id."""

    tool: str
    arguments: str
    destructive: bool
    call: str | None
    """The call id, for a destructive call only (D116). ``None`` for any other."""


def key_for(call: Call, call_id: str) -> Key:
    """The one key function. Both ``grant`` and ``check`` call it with the call they were
    given, so what is checked cannot differ from what was granted."""
    _require(call, Call, "call")
    _check_call_id(call_id)
    return Key(call.tool, call.arguments, call.destructive, call_id if call.destructive else None)


def offered(call: Call) -> tuple[Scope, ...]:
    """The scopes a person may be offered for this call: the buttons a surface draws, and the
    list ``grant`` refuses anything outside. A destructive call gets ``request`` only."""
    _require(call, Call, "call")
    return ONCE if call.destructive else GRANTABLE


# --- what lives in memory ----------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """One grant held in memory: ``request`` or ``session`` scope, and the session and
    request it was given in. ``always`` and ``config`` live in files, not here."""

    key: Key
    scope: Scope
    session: str
    request: str

    def __post_init__(self) -> None:
        _require(self.key, Key, "key")
        _require(self.scope, Scope, "scope")
        if self.scope not in (Scope.REQUEST, Scope.SESSION):
            raise ValueError(f"a {self.scope} grant does not live in memory")
        if self.key.call is not None and self.scope is not Scope.REQUEST:
            raise ValueError(
                f"a destructive call is approved once, never for a {self.scope} (D116)"
            )
        _check_call_id(self.session)
        _check_call_id(self.request)

    def covers(self, key: Key, live: Live) -> bool:
        """Whether this grant covers ``key`` now: the same key, the same session, and for a
        request grant the same request. The request is compared, not assumed cleared."""
        if self.key != key or self.session != live.session:
            return False
        return self.scope is Scope.SESSION or self.request == live.request


@dataclass(frozen=True)
class Live:
    """The grants in memory for one session, and where the run is: the session, the current
    request, and every ``request`` or ``session`` grant given so far. Immutable; a grant
    returns a new ``Live``, and so does the run boundary."""

    session: str
    request: str
    grants: tuple[Grant, ...] = ()

    def __post_init__(self) -> None:
        _require(self.session, str, "session")
        _require(self.request, str, "request")
        _check_call_id(self.session)
        _check_call_id(self.request)
        _require(self.grants, tuple, "grants")
        for held in self.grants:
            _require(held, Grant, "a grant")

    @classmethod
    def start(cls, session: str, request: str) -> Live:
        """A new session: nothing granted."""
        return cls(session, request)

    def next_request(self, request: str) -> Live:
        """The run boundary (D73). Every ``request`` grant is cleared; ``session`` grants
        stay."""
        kept = tuple(g for g in self.grants if g.scope is Scope.SESSION)
        return Live(self.session, request, kept)

    def _with(self, held: Grant) -> Live:
        return Live(self.session, self.request, (*self.grants, held))


# --- what lives in files -----------------------------------------------------------------------


def _check_v(value: int) -> int:
    if value < 1:
        raise ValueError(f"v is at least 1, not {value}")
    return value


type RuleVersion = Annotated[int, Field(strict=True), AfterValidator(_check_v)]
type ToolName = Annotated[str, Field(strict=True), AfterValidator(_check_tool)]


class ConfigRule(BaseModel):
    """One line of ``config.jsonl``: a tool and its exact arguments, written by a person.
    Strict: a number for a string, an extra field, a missing one, are each a refusal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    v: RuleVersion = 1
    tool: ToolName
    arguments: str

    def key(self) -> Key:
        """The key this rule covers. Never destructive: a file is never once (D116)."""
        return Key(self.tool, self.arguments, False, None)


class AlwaysRule(ConfigRule):
    """One line of ``always.jsonl``: a config rule plus when and where the person said
    ``always``, for the record."""

    given_at: Instant
    session: CallId
    request: CallId


# --- what comes out ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Granted:
    """What ``grant`` did: the new ``Live``, the scope asked for and the scope given, the
    grant or rule that records it, and a note for the person when they differ."""

    live: Live
    asked: Scope
    scope: Scope
    grant: Grant | None
    """The in-memory grant, for ``request`` and ``session`` scope (including a degraded
    ``always``)."""
    rule: AlwaysRule | None
    """The line written, for an ``always`` that was saved."""
    note: str = ""
    """Empty when the scope asked for is the scope given. Otherwise why not, and what was
    given instead: the card shows this (D73)."""

    @property
    def degraded(self) -> bool:
        return self.scope is not self.asked


@dataclass(frozen=True)
class Allowed:
    """A grant covers the call: at which scope, by which key. ``problems`` carries anything
    unresolvable that was met on the way, which the caller should surface even so."""

    scope: Scope
    key: Key
    reason: str
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refused:
    """No grant covers the call. ``reason`` is one line for the person; ``problems`` names
    every rule file, or line, that could not be resolved."""

    reason: str
    problems: tuple[str, ...] = ()


type Decision = Allowed | Refused


# --- the two operations ------------------------------------------------------------------------


def grants_dir(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/grants``. Validates the principal's name."""
    return principal_dir(Path(root), principal) / GRANTS_DIRNAME


def grant(
    live: Live,
    call: Call,
    scope: Scope,
    *,
    call_id: str,
    root: Path,
    principal: str,
    ts: str,
    environ: Mapping[str, str] | None = None,
) -> Granted:
    """Record that a person approved ``call`` at ``scope``.

    Checks, in this order and before anything is read or written: grants are on
    (``GrantsOffError``); every argument has its type and shape (``TypeError``,
    ``ValueError``); the scope is one ``offered`` for this call (``NotGrantableError``).
    Then ``request`` and ``session`` go into memory; ``always`` is appended to the rule file,
    and when that fails for any ``OSError`` it goes into memory at ``session`` scope with a
    ``note`` saying so (D73). ``ts`` is passed in: this module reads no clock.
    """
    _require_on(environ)
    _require(live, Live, "live")
    _require(call, Call, "call")
    _require(scope, Scope, "scope")
    key = key_for(call, call_id)
    _require(ts, str, "ts")
    given_at = _INSTANT.validate_python(ts)
    folder = grants_dir(root, principal)
    if scope not in offered(call):
        if scope is Scope.CONFIG:
            raise NotGrantableError(
                f"{Scope.CONFIG} is human-edited only: a person edits {CONFIG_FILE}, nothing "
                "grants it (D72)"
            )
        raise NotGrantableError(
            f"a destructive call may be approved once, never {scope}: only "
            f"{', '.join(ONCE)} is offered (D116)"
        )
    if scope is not Scope.ALWAYS:
        held = Grant(key, scope, live.session, live.request)
        return Granted(live._with(held), scope, scope, held, None)
    rule = AlwaysRule(
        tool=call.tool,
        arguments=call.arguments,
        given_at=given_at,
        session=live.session,
        request=live.request,
    )
    try:
        _append(folder / ALWAYS_FILE, rule)
    except OSError as exc:
        held = Grant(key, Scope.SESSION, live.session, live.request)
        note = (
            f"always could not be saved ({type(exc).__name__} writing {ALWAYS_FILE}), so this "
            "grant lasts for this session only; say always again next session, or ask the "
            "owner to look at the grants folder (D73)"
        )
        return Granted(live._with(held), scope, Scope.SESSION, held, None, note)
    return Granted(live, scope, scope, None, rule)


def check(
    live: Live,
    call: Call,
    *,
    call_id: str,
    root: Path,
    principal: str,
    environ: Mapping[str, str] | None = None,
) -> Decision:
    """Whether a grant covers ``call`` now, and at which scope.

    Looks narrowest first: the grants in memory, then ``always.jsonl``, then ``config.jsonl``.
    The first cover is the answer. While grants are off every check is ``Refused`` and no
    file is opened. A destructive call is covered only by a per-call grant in memory. Any
    rule file that cannot be resolved whole contributes nothing and is named in
    ``problems``. Types and shapes are checked before anything is read.
    """
    _require(live, Live, "live")
    _require(call, Call, "call")
    key = key_for(call, call_id)
    folder = grants_dir(root, principal)
    if not enabled(environ):
        return Refused(f"grants are off; the owner turns them on with {GRANTS_SWITCH}={SWITCH_ON}")
    # Both files are read on every check, even when a grant in memory will decide it, so a
    # broken rule file is reported the first time anyone looks, not the first time it matters.
    problems: list[str] = []
    files: dict[Scope, tuple[ConfigRule, ...]] = {}
    for scope, name, model in (
        (Scope.ALWAYS, ALWAYS_FILE, AlwaysRule),
        (Scope.CONFIG, CONFIG_FILE, ConfigRule),
    ):
        rules = _load(folder / name, model)
        if isinstance(rules, str):
            problems.append(rules)
        else:
            files[scope] = rules
    found = tuple(problems)
    for scope in (Scope.REQUEST, Scope.SESSION):
        for held in live.grants:
            if held.scope is scope and held.covers(key, live):
                return Allowed(scope, key, f"approved for this {scope} (D73)", found)
    if call.destructive:
        return Refused(
            "no grant: a destructive call is approved once, for the one call, never from a "
            "rule file (D116)",
            found,
        )
    for scope in (Scope.ALWAYS, Scope.CONFIG):
        if any(rule.key() == key for rule in files.get(scope, ())):
            why = "a standing rule the person gave (D62)" if scope is Scope.ALWAYS else "config"
            return Allowed(scope, key, f"allowed by {why}", found)
    return Refused(f"no grant covers {call.tool} with these arguments", found)


# --- files -------------------------------------------------------------------------------------


def _load[R: ConfigRule](path: Path, model: type[R]) -> tuple[R, ...] | str:
    """Every rule in ``path``, or one string saying why none of them count. A missing file
    is no rules and no problem. Everything else that is not a whole file of valid lines is a
    problem: a link, a line that is not JSON, a line that is not a ``model``, a line from a
    newer build. Blank lines are skipped."""
    try:
        if os.path.islink(path):
            return f"{path.name} is a link, which is not read: a rule file is a plain file"
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        return f"{path.name} could not be read ({type(exc).__name__})"
    rules: list[R] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):  # nesting past the limit is malformed too (#94)
            return f"{path.name} line {number} is not JSON"
        if not isinstance(data, dict):
            return f"{path.name} line {number} is not a rule object"
        v = data.get("v", 1)
        if isinstance(v, int) and not isinstance(v, bool) and v > KNOWN_V:
            return f"{path.name} line {number} is a v{v} rule; this build knows v{KNOWN_V}"
        try:
            rules.append(model.model_validate(data))
        except ValidationError:
            return f"{path.name} line {number} is not a rule"
    return tuple(rules)


def _append(path: Path, rule: AlwaysRule) -> None:
    """Append one line to the always file, under ``always.lock``. Raises ``OSError`` for
    anything that stops it, including a link where the file should be, and a principal
    folder that is not there: a misspelt principal must not get a profile folder."""
    path.parent.mkdir(exist_ok=True)  # grants/ only, never its parents
    with _exclusive(path.parent / LOCK_FILE):
        if os.path.islink(path):
            raise FileExistsError(f"{path.name} is a link; a rule file is a plain file")
        line = rule.model_dump_json() + "\n"
        with open(path, "ab") as f:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())


@contextlib.contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    with open(lock, "ab") as f:  # "a": opening never truncates, and the file stays empty
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
        # The lock goes with close.


# --- checks ------------------------------------------------------------------------------------


def _check_call_id(value: object) -> str:
    """A session, request or tool-call id, in the inbox's shape (``walle.events.CallId``)."""
    _require(value, str, "an id")
    return _CALL_ID.validate_python(value)


def _require(value: object, kind: type, name: str) -> None:
    if not isinstance(value, kind):
        raise TypeError(f"{name} is a {kind.__name__}, not {type(value).__name__}")


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise GrantsOffError(
            f"grants are off; the owner turns them on with {GRANTS_SWITCH}={SWITCH_ON}"
        )
