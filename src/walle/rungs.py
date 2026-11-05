"""Rung 3 and rung 4 as protocols, not prompts (D62, D73, D116, D74, D72, D117).

Rung 3, act-with-confirm
------------------------
The request names the exact command. The person answers with one of three strings, and
nothing else is an answer::

    #<id> approve      this command, once
    #<id> always       this command, every time, until a person says otherwise (D62, D73)
    #<id> no

*Exact match only.* ``parse`` compares bytes: no case folding, no whitespace trimmed, no
punctuation forgiven, no second word. ``#c-1 Always``, ``#c-1 always ``, ``#c-1 always!`` and
*"yes, always!"* are each not a reply, and a reply that is not a reply settles nothing.
Approval text is the attack surface: something that outlives the conversation must never come
from a reply that merely sounded enthusiastic (D62).

*One command, resolved once.* ``ask`` takes the call as it will be applied — the same
``walle.grants.Call`` the dispatcher has in hand when it runs it — and puts it in the
``Pending`` under its id. ``Approved.call`` is that object, not a copy and not a paraphrase,
and ``Approved.permits`` compares the call to run against it byte for byte. An id names one
command for its whole life: a second ``ask`` under a live or settled id with another command is
refused, so what was approved and what runs cannot differ.

*Only the owner, operating, settles.* A ``Ledger`` belongs to one principal — the kernel is
theirs (hard constraint 6) — and ``resolve`` and ``settle`` take ``by``, the authority of the
speaker whose reply this is (D61): ``walle.speakers.Order`` or ``Request``, as
``walle.conversation.authority`` decided it. A ``Request`` — another person, a guest, the
principal in a role that does not operate (D27) — is refused as ``NOT_AN_ORDER`` before the
reply is read, and so is an ``Order`` for another principal. ``by`` has no default: a caller
cannot forget it, and an ``Order`` cannot be built for anyone but the principal as operator,
so holding one is proof of who spoke. The same ``Order`` is what ``walle.grants.grant`` takes
to write the grant an ``Approved`` stands for (issue #119).

*Once, within fifteen minutes.* A ``Pending`` is settled by its first fitting reply and never
again: a replay is refused as ``REPLAYED``. A reply more than ``WINDOW_MINUTES`` after the ask
is refused as ``EXPIRED`` and settles the item, so it cannot be approved later by a clock that
went backwards; a reply timed before the ask is refused as ``EARLY``. ``ts`` is passed in: this
module reads no clock.

*A button's payload is exactly the text it stands for* (D62). ``Pending.buttons`` makes
buttons whose label and payload are the same reply string, and ``resolve`` refuses a
``Button`` whose payload differs from its label before it reads the payload.

*Once, never always, for a destructive call* (D116). ``Pending.answers`` omits ``always`` for
a destructive call, the card does not show it, and the reply is refused as ``NOT_OFFERED`` if
said anyway. ``SCOPE_FOR`` maps the two yeses to the scopes ``walle.grants`` offers, so an
``Approved`` goes straight to ``grants.grant`` with its ``call`` and ``scope``.

*The inbox.* An unattended ask is an inbox approval (D78), and this module is the source of
truth for what it says and what it means; the inbox carries it. ``to_ask`` makes the ``Ask``
that ``walle.inbox.ask_event`` stores verbatim: the id as the item's ``tool_call_id``, the
card as its body, timed at the ask. ``INBOX_REPLIES`` is the one table from an inbox outcome
to the word it stands for — ``approved`` is ``approve``, ``denied`` is ``no`` — and both the
unattended card (``Pending.card(via_inbox=True)``) and ``reply_for`` are read off it, so the
card never offers what the inbox cannot carry, and nothing that arrives through the inbox is
the literal word ``always``. ``settle`` takes the resolved inbox ``Item`` itself and settles
the ``Pending`` it is bound to: the same session, the same id, and the body the exact card —
an item saying anything else is ``MISMATCH``, however it was answered. The fifteen-minute
window is the typed reply's (D62); an inbox item lives by the inbox's rule instead —
resolved once, first answer wins, orphaned when its session dies (D78) — because a mission
waits forty minutes for a person who is away (D67), and a yes they gave to the exact command
must not come back as "expired". An orphaned item settles nothing: orphaned is never a yes.

Rung 4, autonomous-bounded
--------------------------
The envelope is a list of standing rules, one per line, ``tool: pattern*`` (D62). The pattern
is a ``walle.matching.Rule``: a prefix of plain words with a trailing ``*``, or an exact
command without one. A bare ``*`` is rejected, as a pattern and as a tool. A rule that the
matcher would refuse — a separator, a quote, an executor, inline code — cannot be an envelope
line either, and one bad line is no envelope (fail closed, D86).

``bounded`` asks whether a call is inside the envelope: the rules for the call's tool, and
the arguments through ``walle.matching.match``, so a compound command is split on every
separator and each part justified (D74). A destructive call is never bounded by a rule
(D116). ``load_envelope`` reads ``grants/envelope.txt`` under the principal's folder, which
is under a floor (D72): a person edits it, nothing here writes it, a link there is not read.

The dormant rule
----------------
Off unless the owner has set ``WALLE_RUNGS=on`` in the environment, outside the log (D117).
Off means: ``ask`` raises, ``resolve`` refuses before parsing, ``bounded`` refuses before
matching. Rung 4 also needs ``WALLE_MATCHING=on``, since it matches through that module, and
the refusal says so. This module starts nothing, runs nothing, writes nothing, reads no clock
and has no command.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from pydantic import TypeAdapter

from walle import inbox, matching
from walle.events import ACTOR_KERNEL, AnswerOutcome, Ask, AskKind, CallId, Instant
from walle.grants import TOOL_NAME_CHARS, TOOL_NAME_RE, Call, Scope, grants_dir
from walle.paths import check_name
from walle.speakers import Authority, Order, Request

__all__ = [
    "ALWAYS",
    "APPROVE",
    "ENVELOPE_FILE",
    "INBOX_REPLIES",
    "NO",
    "RUNGS_SWITCH",
    "SCOPE_FOR",
    "SWITCH_ON",
    "WINDOW_MINUTES",
    "WORDS",
    "Approved",
    "Asked",
    "Bounded",
    "Button",
    "Denied",
    "Envelope",
    "How",
    "Ledger",
    "Pending",
    "Refusal",
    "Refused",
    "Reply",
    "RungsOffError",
    "Settled",
    "Standing",
    "ask",
    "bounded",
    "enabled",
    "load_envelope",
    "parse",
    "reply_for",
    "resolve",
    "settle",
    "to_ask",
]

RUNGS_SWITCH = "WALLE_RUNGS"
"""The environment variable the owner sets to turn the rung protocols on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for ``WALLE_GRANTS`` and ``WALLE_MATCHING``: a stray ``1``
is not consent."""

WINDOW_MINUTES = 15
"""How long a request stays answerable (D62)."""

APPROVE = "approve"
ALWAYS = "always"
NO = "no"
WORDS: tuple[str, ...] = (APPROVE, ALWAYS, NO)
"""The three answers, in the order a card offers them."""

SCOPE_FOR: Mapping[str, Scope] = MappingProxyType({APPROVE: Scope.REQUEST, ALWAYS: Scope.ALWAYS})
"""What each yes means to ``walle.grants``: ``approve`` is this request, ``always`` is the
standing rule (D73). ``no`` means nothing to grant."""

INBOX_REPLIES: Mapping[AnswerOutcome, str] = MappingProxyType(
    {AnswerOutcome.APPROVED: APPROVE, AnswerOutcome.DENIED: NO}
)
"""The one table from an inbox outcome to the word it stands for. The unattended card offers
exactly these words, ``reply_for`` and ``settle`` read them back, and ``always`` is not here:
a standing rule comes from the literal word, typed (D62). ``tests/test_audit_approvals.py``
holds this equal to ``walle.inbox.FITS[AskKind.APPROVAL]``."""

ENVELOPE_FILE = "envelope.txt"
"""``principals/p-<name>/grants/envelope.txt``: the rung 4 envelope, ``tool: pattern*`` per
line. Under the grants floor, so a person edits it and the agent cannot (D72)."""

_WINDOW = timedelta(minutes=WINDOW_MINUTES)
_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"  # walle.events.CallId, spelt out for the one regex
_REPLY = re.compile(rf"#({_ID}) ({'|'.join(WORDS)})")
_RULE_SEP = ": "
_COMMENT = "#"

_CALL_ID: TypeAdapter[str] = TypeAdapter(CallId)
_INSTANT: TypeAdapter[str] = TypeAdapter(Instant)


class RungsOffError(Exception):
    """The owner has not turned the rung protocols on. Raised by ``ask`` before anything."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the rung protocols on: ``WALLE_RUNGS`` is exactly ``on``.

    Reads the process environment when no mapping is given; a mapping is the whole answer
    when one is. No event and no file under the root can reach it.
    """
    env = os.environ if environ is None else environ
    return env.get(RUNGS_SWITCH) == SWITCH_ON


# --- rung 3: the card --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pending:
    """One request awaiting its answer: the id, the exact call, when it was asked, and
    where. The call is the one the dispatcher will run; nothing here copies or reshapes it."""

    id: str
    call: Call
    asked_at: str
    session: str
    request: str

    def __post_init__(self) -> None:
        _check_id(self.id)
        _require(self.call, Call, "call")
        _require(self.asked_at, str, "asked_at")
        _INSTANT.validate_python(self.asked_at)
        _check_id(self.session)
        _check_id(self.request)

    def answers(self, *, via_inbox: bool = False) -> tuple[str, ...]:
        """The exact reply strings this item accepts, in card order. A destructive call is
        offered ``approve`` and ``no`` only (D116). Through the inbox, only the words an
        inbox outcome stands for (``INBOX_REPLIES``) are offered, whatever the call."""
        words: tuple[str, ...] = WORDS
        if self.call.destructive:
            words = tuple(w for w in words if w != ALWAYS)
        if via_inbox:
            words = tuple(w for w in words if w in INBOX_REPLIES.values())
        return tuple(f"#{self.id} {word}" for word in words)

    def buttons(self) -> tuple[Button, ...]:
        """One button per answer, each with payload equal to its label (D62)."""
        return tuple(Button(text, text) for text in self.answers())

    def card(self, *, via_inbox: bool = False) -> str:
        """What the person reads: the exact command, and the answers, verbatim. The inbox
        card offers only what the inbox can carry, and names the inbox's rule, not the
        typed reply's window."""
        how = (
            "Answer from the inbox; the first answer settles it, and it stands for one of:"
            if via_inbox
            else f"Reply with one of these, exactly, within {WINDOW_MINUTES} minutes:"
        )
        lines = [
            f"{self.call.tool} wants to run, exactly:",
            "",
            f"    {self.call.arguments}",
            "",
            how,
            *(f"    {text}" for text in self.answers(via_inbox=via_inbox)),
        ]
        if self.call.destructive:
            lines.append("")
            lines.append("This action is destructive, so it can be approved once, never always.")
        return "\n".join(lines)


@dataclass(frozen=True)
class Button:
    """A button on a surface: what it shows, and what it sends. The two are the same string,
    or the button resolves nothing."""

    label: str
    payload: str

    def __post_init__(self) -> None:
        _require(self.label, str, "label")
        _require(self.payload, str, "payload")


@dataclass(frozen=True)
class Reply:
    """A parsed reply: the id it names and the word it says."""

    id: str
    word: str


def parse(text: str) -> Reply | None:
    """The reply ``text`` is, or ``None``. Exact: the whole string is ``#<id> <word>`` with one
    space, nothing before, nothing after, the word in lower case as the card printed it."""
    _require(text, str, "a reply")
    found = _REPLY.fullmatch(text)
    if found is None:
        return None
    return Reply(found.group(1), found.group(2))


# --- rung 3: the ledger ------------------------------------------------------------------------


class How(StrEnum):
    """How a settled item was settled."""

    APPROVED = "approved"
    ALWAYS = "always"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Settled:
    """An id that has had its one resolution. It is never pending again and never reused."""

    id: str
    how: How

    def __post_init__(self) -> None:
        _check_id(self.id)
        _require(self.how, How, "how")


@dataclass(frozen=True)
class Ledger:
    """Every request of one kernel, which is one principal's: the ones waiting and the ones
    settled. Immutable; ``ask``, ``resolve`` and ``settle`` return a new one. In memory only:
    an unattended item's durable form is the inbox's (``to_ask``). Only an ``Order`` for
    ``principal`` settles anything in it."""

    principal: str
    pending: tuple[Pending, ...] = ()
    settled: tuple[Settled, ...] = ()

    def __post_init__(self) -> None:
        _require(self.principal, str, "principal")
        check_name(self.principal, kind="principal")
        _require(self.pending, tuple, "pending")
        _require(self.settled, tuple, "settled")
        for item in self.pending:
            _require(item, Pending, "a pending item")
        for item in self.settled:
            _require(item, Settled, "a settled item")
        ids = [item.id for item in self.pending] + [item.id for item in self.settled]
        if len(set(ids)) != len(ids):
            raise ValueError("an id appears twice in the ledger")

    @classmethod
    def start(cls, principal: str) -> Ledger:
        """A new ledger for one principal's kernel: nothing asked."""
        return cls(principal)

    def find(self, id: str) -> Pending | Settled | None:
        for item in self.pending:
            if item.id == id:
                return item
        for done in self.settled:
            if done.id == id:
                return done
        return None

    def _settle(self, item: Pending, how: How) -> Ledger:
        kept = tuple(p for p in self.pending if p is not item)
        return Ledger(self.principal, kept, (*self.settled, Settled(item.id, how)))


# --- rung 3: what comes out --------------------------------------------------------------------


class Refusal(StrEnum):
    """Every way a reply, an ask or a call fails here, one kind each. The reason that goes
    with a kind is fixed text: it never echoes the reply or the command."""

    OFF = "off"
    NOT_A_REPLY = "not-a-reply"
    UNKNOWN = "unknown-id"
    REPLAYED = "replayed"
    EXPIRED = "expired"
    EARLY = "early"
    NOT_OFFERED = "not-offered"
    BUTTON = "button-payload"
    TAKEN = "id-taken"
    NOT_AN_ORDER = "not-an-order"
    MISMATCH = "item-mismatch"
    DESTRUCTIVE = "destructive"
    ENVELOPE = "outside-envelope"


@dataclass(frozen=True)
class Asked:
    """``ask`` made, or found, the item."""

    ledger: Ledger
    pending: Pending


@dataclass(frozen=True)
class Approved:
    """A yes. ``call`` is the exact call the person saw, and ``scope`` is what the yes means
    to ``walle.grants``: ``request`` for ``approve``, ``always`` for ``always``."""

    ledger: Ledger
    pending: Pending
    scope: Scope

    @property
    def call(self) -> Call:
        return self.pending.call

    def permits(self, call: Call) -> bool:
        """Whether ``call`` is the call that was approved: the same tool, the same arguments
        byte for byte, the same destructive flag. Anything else is another call."""
        _require(call, Call, "call")
        return call == self.pending.call


@dataclass(frozen=True)
class Denied:
    """A no. The item is settled and nothing runs."""

    ledger: Ledger
    pending: Pending


@dataclass(frozen=True)
class Bounded:
    """Rung 4: the call is inside the envelope, every part justified by a rule."""

    call: Call
    justified: tuple[matching.Justified, ...]


@dataclass(frozen=True)
class Refused:
    """Nothing happened. ``ledger`` is the ledger as it now stands: unchanged, except that
    an expired item has been settled as expired. ``bounded`` has no ledger and leaves it
    ``None``."""

    kind: Refusal
    reason: str
    ledger: Ledger | None = None


# --- rung 3: the two operations ----------------------------------------------------------------


def ask(
    ledger: Ledger,
    call: Call,
    *,
    id: str,
    session: str,
    request: str,
    ts: str,
    environ: Mapping[str, str] | None = None,
) -> Asked | Refused:
    """Put ``call`` to the person under ``id``, asked at ``ts``.

    Raises ``RungsOffError`` while off, before anything; ``TypeError`` and ``ValueError`` for
    a wrong type or shape. The same id with the same call, in the same session and request,
    still pending, is the same item and nothing changes (a restarted run does not ask twice).
    The same id with another call, from another session or request, or an id already
    settled, is refused: an id names one command for its whole life, and one session's reply
    never answers another's call.
    """
    _require_on(environ)
    _require(ledger, Ledger, "ledger")
    item = Pending(id, call, ts, session, request)
    found = ledger.find(id)
    if found is None:
        return Asked(Ledger(ledger.principal, (*ledger.pending, item), ledger.settled), item)
    if isinstance(found, Pending) and found == Pending(id, call, found.asked_at, session, request):
        # The same call, session and request, whenever it was first asked: the same item.
        return Asked(ledger, found)
    return Refused(
        Refusal.TAKEN,
        "this id already names a command, or has been settled; an id is never reused for "
        "another command, another session or another request",
        ledger,
    )


def resolve(
    ledger: Ledger,
    reply: str | Button,
    *,
    by: Authority,
    ts: str,
    environ: Mapping[str, str] | None = None,
) -> Approved | Denied | Refused:
    """What ``reply``, spoken by ``by`` and received at ``ts``, does to the ledger.

    In order: off refuses before the reply is read; a speaker who is not the ledger's
    principal giving an ``Order`` is refused before the reply is read; a ``Button`` whose
    payload is not its label is refused before its payload is read; a string that is not
    exactly ``#<id> <word>`` is not a reply; an id not in the ledger is unknown; a settled id
    is replayed, or expired if it expired; a reply before the ask is early; a reply after the
    window expires the item; ``always`` for a destructive call was not offered. What is left
    is the one fitting reply, and the item is settled by it.
    """
    _require(ledger, Ledger, "ledger")
    _require(ts, str, "ts")
    _require_authority(by)
    now = _instant(ts)
    if not enabled(environ):
        return Refused(
            Refusal.OFF,
            f"the rung protocols are off; the owner turns them on with {RUNGS_SWITCH}={SWITCH_ON}",
            ledger,
        )
    if (who := _not_an_order(ledger, by)) is not None:
        return who
    if isinstance(reply, Button):
        if reply.payload != reply.label:
            return Refused(
                Refusal.BUTTON,
                "the button's payload is not its label, so it is not the answer it shows (D62)",
                ledger,
            )
        text = reply.payload
    else:
        _require(reply, str, "a reply")
        text = reply
    parsed = parse(text)
    if parsed is None:
        return Refused(
            Refusal.NOT_A_REPLY,
            "not a reply: an answer is exactly '#<id> approve', '#<id> always' or '#<id> no'",
            ledger,
        )
    found = ledger.find(parsed.id)
    if found is None:
        return Refused(Refusal.UNKNOWN, "no request has this id", ledger)
    if isinstance(found, Settled):
        if found.how is How.EXPIRED:
            return Refused(Refusal.EXPIRED, _EXPIRED, ledger)
        return Refused(
            Refusal.REPLAYED, "this request was already answered; an answer is used once", ledger
        )
    asked_at = _instant(found.asked_at)
    if now < asked_at:
        return Refused(Refusal.EARLY, "this reply is timed before the request it answers", ledger)
    if now - asked_at > _WINDOW:
        return Refused(Refusal.EXPIRED, _EXPIRED, ledger._settle(found, How.EXPIRED))
    if parsed.word == ALWAYS and found.call.destructive:
        return Refused(
            Refusal.NOT_OFFERED,
            "a destructive action is approved once, never always (D116)",
            ledger,
        )
    if parsed.word == NO:
        return Denied(ledger._settle(found, How.DENIED), found)
    how = How.ALWAYS if parsed.word == ALWAYS else How.APPROVED
    return Approved(ledger._settle(found, how), found, SCOPE_FOR[parsed.word])


_EXPIRED = f"this request is older than {WINDOW_MINUTES} minutes; ask again"
_NOT_AN_ORDER = (
    "this reply is not an order from the owner of this ledger speaking as the operator; "
    "nothing is settled on a request (D61, D27)"
)


def _not_an_order(ledger: Ledger, by: Authority) -> Refused | None:
    """The refusal for a speaker who may not settle this ledger, or ``None`` for the
    principal as operator. Fixed text: it never names the speaker."""
    if isinstance(by, Order) and by.principal == ledger.principal:
        return None
    return Refused(Refusal.NOT_AN_ORDER, _NOT_AN_ORDER, ledger)


# --- rung 3: the inbox -------------------------------------------------------------------------


def to_ask(pending: Pending, *, unattended: bool) -> Ask:
    """The inbox item for a request nobody is watching (D78): an approval, keyed by the
    session and the id, whose body is the inbox card, timed at the ask and written by the
    kernel. The inbox resolves it once, first answer wins, so a replay fails there as it
    fails here. Nothing is written: the caller hands the event to ``walle.inbox.ask_event``,
    which stores it as it is, and the resolved item back to ``settle``."""
    _require(pending, Pending, "pending")
    return Ask(
        ts=pending.asked_at,
        actor=ACTOR_KERNEL,
        kind=AskKind.APPROVAL,
        session=pending.session,
        tool_call_id=pending.id,
        body=pending.card(via_inbox=True),
        unattended=unattended,
    )


def reply_for(pending: Pending, outcome: AnswerOutcome) -> str | None:
    """The reply an inbox outcome stands for, from ``INBOX_REPLIES``: ``approved`` is
    ``#id approve``, ``denied`` is ``#id no``, anything else is no reply. There is no outcome
    that is ``always``: a standing rule comes from the literal word, typed, and never from a
    button in an inbox."""
    _require(pending, Pending, "pending")
    _require(outcome, AnswerOutcome, "outcome")
    word = INBOX_REPLIES.get(outcome)
    return None if word is None else f"#{pending.id} {word}"


def settle(
    ledger: Ledger,
    item: inbox.Item,
    *,
    by: Authority,
    environ: Mapping[str, str] | None = None,
) -> Approved | Denied | Refused:
    """What a resolved inbox ``item``, read back by ``by``, does to the ledger.

    In order: off refuses before the item is read; a speaker who is not the ledger's
    principal giving an ``Order`` is refused before the item is read; an item whose id is
    not in the ledger is unknown; a settled id is replayed, or expired if it expired; an
    item that is not this pending's — another session, or a body that is not the exact inbox
    card — is a mismatch, approved or not; an item still pending, or orphaned, or whose
    answer does not fit it by the inbox's own ``misfit`` (a machine actor's, say), is not a
    reply. What is left is a person's approved or denied, and the item is settled by it:
    ``approve`` for this request (``Scope.REQUEST``), never ``always``. No window: the
    inbox's rule governs the inbox.
    """
    _require(ledger, Ledger, "ledger")
    _require(item, inbox.Item, "item")
    _require_authority(by)
    if not enabled(environ):
        return Refused(
            Refusal.OFF,
            f"the rung protocols are off; the owner turns them on with {RUNGS_SWITCH}={SWITCH_ON}",
            ledger,
        )
    if (who := _not_an_order(ledger, by)) is not None:
        return who
    found = ledger.find(item.ask.tool_call_id)
    if found is None:
        return Refused(Refusal.UNKNOWN, "no request has this id", ledger)
    if isinstance(found, Settled):
        if found.how is How.EXPIRED:
            return Refused(Refusal.EXPIRED, _EXPIRED, ledger)
        return Refused(
            Refusal.REPLAYED, "this request was already answered; an answer is used once", ledger
        )
    bound = (
        item.ask.kind is AskKind.APPROVAL
        and item.ask.session == found.session
        and item.ask.body == found.card(via_inbox=True)
        and not item.ask.choices
    )
    if not bound:
        return Refused(
            Refusal.MISMATCH,
            "this inbox item is not the request it names: another session, or another text "
            "than the exact command the person was shown",
            ledger,
        )
    word = None
    if item.answer is not None and inbox.misfit(item.ask, item.answer) is None:
        word = INBOX_REPLIES.get(item.answer.outcome)
    if word is None:
        return Refused(
            Refusal.NOT_A_REPLY,
            "this inbox item is not a person's approved or denied: pending, orphaned, or an "
            "answer that does not fit it settles nothing",
            ledger,
        )
    if word == NO:
        return Denied(ledger._settle(found, How.DENIED), found)
    return Approved(ledger._settle(found, How.APPROVED), found, SCOPE_FOR[word])


# --- rung 4: the envelope ----------------------------------------------------------------------


@dataclass(frozen=True)
class Standing:
    """One standing rule: a tool, and a ``walle.matching.Rule`` for its arguments."""

    tool: str
    rule: matching.Rule

    def __post_init__(self) -> None:
        _require(self.tool, str, "tool")
        _require(self.rule, matching.Rule, "rule")
        if len(self.tool) > TOOL_NAME_CHARS or not TOOL_NAME_RE.fullmatch(self.tool):
            raise ValueError(
                "a standing rule names one tool, a dotted identifier: a '*' is not a tool"
            )

    @classmethod
    def parse(cls, line: str) -> Standing:
        """``tool: pattern*``, exactly: the tool, a colon, one space, the pattern. The
        pattern is parsed by ``matching.Rule``, which rejects a bare ``*`` and every shape
        the matcher would refuse. ``ValueError`` says what was wrong."""
        _require(line, str, "a rule line")
        tool, sep, pattern = line.partition(_RULE_SEP)
        if not sep:
            raise ValueError("a standing rule is 'tool: pattern*', with a colon and one space")
        if not pattern or pattern != pattern.strip(" "):
            raise ValueError("a standing rule's pattern has no spaces at its ends and is not empty")
        return cls(tool, matching.Rule(pattern))


@dataclass(frozen=True)
class Envelope:
    """The list of standing rules a principal has written (D62). Parsed whole: one line that
    is not a rule, and there is no envelope."""

    rules: tuple[Standing, ...] = ()

    def __post_init__(self) -> None:
        _require(self.rules, tuple, "rules")
        for rule in self.rules:
            _require(rule, Standing, "a standing rule")

    @classmethod
    def parse(cls, text: str) -> Envelope:
        """One rule per line. Blank lines and lines starting with ``#`` are skipped. The
        error names the line."""
        _require(text, str, "an envelope")
        rules: list[Standing] = []
        for number, line in enumerate(text.split("\n"), start=1):
            if not line.strip() or line.startswith(_COMMENT):
                continue
            try:
                rules.append(Standing.parse(line))
            except ValueError as exc:
                raise ValueError(f"line {number}: {exc}") from exc
        return cls(tuple(rules))

    def for_tool(self, tool: str) -> tuple[matching.Rule, ...]:
        return tuple(s.rule for s in self.rules if s.tool == tool)


def load_envelope(root: Path, principal: str) -> Envelope | str:
    """The principal's envelope, or one string saying why there is none. A missing file is
    an empty envelope. A link is not read; a file that cannot be read is reported by its
    error class; a line that is not a rule voids the file (D86). Reading is not acting: this
    works while off, and ``bounded`` refuses regardless."""
    path = grants_dir(root, principal) / ENVELOPE_FILE
    try:
        if os.path.islink(path):
            return f"{ENVELOPE_FILE} is a link, which is not read: the envelope is a plain file"
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Envelope()
    except (OSError, ValueError) as exc:
        return f"{ENVELOPE_FILE} could not be read ({type(exc).__name__})"
    try:
        return Envelope.parse(text)
    except ValueError as exc:
        return f"{ENVELOPE_FILE} {exc}"


def bounded(
    envelope: Envelope | str,
    call: Call,
    *,
    environ: Mapping[str, str] | None = None,
) -> Bounded | Refused:
    """Rung 4: whether ``call`` is inside ``envelope``.

    Off refuses before anything is matched. A problem string from ``load_envelope`` is no
    envelope and refuses. A destructive call is never bounded by a rule (D116). Otherwise the
    rules for the call's tool go with its arguments through ``walle.matching.match``, which
    splits on every separator and justifies each part (D74), and needs its own switch on.
    """
    _require(call, Call, "call")
    if not isinstance(envelope, Envelope | str):
        raise TypeError(
            f"an envelope is an Envelope or a problem str, not {type(envelope).__name__}"
        )
    if not enabled(environ):
        return Refused(
            Refusal.OFF,
            f"the rung protocols are off; the owner turns them on with {RUNGS_SWITCH}={SWITCH_ON}",
        )
    if isinstance(envelope, str):
        return Refused(Refusal.ENVELOPE, f"no envelope applies: {envelope}")
    if call.destructive:
        return Refused(
            Refusal.DESTRUCTIVE,
            "a destructive action is never inside an envelope: it is approved once, by a "
            "person (D116)",
        )
    outcome = matching.match(call.arguments, envelope.for_tool(call.tool), environ=environ)
    if isinstance(outcome, matching.Refused):
        return Refused(Refusal.ENVELOPE, f"outside the envelope: {outcome.reason}")
    return Bounded(call, outcome.parts)


# --- checks ------------------------------------------------------------------------------------


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(_INSTANT.validate_python(value))


def _check_id(value: object) -> str:
    _require(value, str, "an id")
    return _CALL_ID.validate_python(value)


def _require(value: object, kind: type, name: str) -> None:
    if not isinstance(value, kind):
        raise TypeError(f"{name} is a {kind.__name__}, not {type(value).__name__}")


def _require_authority(value: object) -> None:
    if not isinstance(value, Order | Request):
        raise TypeError(
            f"by is the speaker's authority, an Order or a Request, not {type(value).__name__}"
        )


def _require_on(environ: Mapping[str, str] | None) -> None:
    if not enabled(environ):
        raise RungsOffError(
            f"the rung protocols are off; the owner turns them on with {RUNGS_SWITCH}={SWITCH_ON}"
        )
