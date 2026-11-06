"""Who is speaking, at what tier, and whether their words are orders (D61, D27).

The verified ``Speaker``, the ``Tier`` its role puts it at, and the two things a speaker's
words can be in one profile's conversation: an ``Order`` from the principal speaking as the
operator, or a ``Request`` from anyone else. ``walle.conversation`` decides which
(``authority``) and frames the turn; this module holds the types, so that ``walle.rungs`` and
``walle.grants`` — which ``walle.conversation`` imports — can require an ``Order`` in their
signatures without a cycle.

An ``Order`` is a guarantee, not a label. It cannot be built for anyone but the principal
speaking as the operator: ``Order(principal, speaker)`` raises for a speaker with another id
or another role. So a function that takes ``by: Order`` has already been told, by the type,
that the folder's owner is in the room and operating (D61: capability follows the speaker;
D27: the roles that do not operate). A function that takes ``by: Authority`` refuses the
``Request`` itself, so a caller can pass the turn's authority straight through and never
decide who may answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from walle.events import check_speaker
from walle.profiles import Role

__all__ = ["Authority", "Order", "Request", "Speaker", "Tier"]


class Tier(IntEnum):
    """D27's roles in the order the ratchet uses, lowest first, with a bottom for a speaker
    the kernel could attach no role to."""

    UNKNOWN = 0
    STAKEHOLDER = 1
    ADMIN = 2
    OPERATOR = 3

    @classmethod
    def of(cls, role: Role | None) -> Tier:
        """The tier of a role; ``None`` — no role attached — is the lowest."""
        if role is None:
            return cls.UNKNOWN
        return _TIER_OF[Role(role)]

    @property
    def label(self) -> str:
        return self.name.lower()


_TIER_OF: dict[Role, Tier] = {
    Role.STAKEHOLDER: Tier.STAKEHOLDER,
    Role.ADMIN: Tier.ADMIN,
    Role.OPERATOR: Tier.OPERATOR,
}


@dataclass(frozen=True, slots=True)
class Speaker:
    """Who the kernel verified a message to be from: the identity, and the role it attached
    for this message, or ``None`` when it could attach none.

    ``id`` is the principal's name when the platform's id resolves to one, otherwise the
    platform's id as given. It is never a display name and never taken from the message
    (D61). It is one token, so it cannot carry a sentence.
    """

    id: str
    role: Role | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise TypeError(f"a speaker id is a str, not {type(self.id).__name__}")
        check_speaker(self.id)
        if self.role is not None:
            object.__setattr__(self, "role", Role(self.role))

    @property
    def tier(self) -> Tier:
        return Tier.of(self.role)


@dataclass(frozen=True, slots=True)
class Order:
    """The principal, speaking as the operator: a call goes to their own grants, and their
    reply settles their own ledger. Exists only for that speaker: building one for anyone
    else is a ``ValueError``, so holding an ``Order`` is proof of who gave it."""

    principal: str
    speaker: Speaker

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str):
            raise TypeError(f"a principal is a str, not {type(self.principal).__name__}")
        if not isinstance(self.speaker, Speaker):
            raise TypeError(f"an Order carries a Speaker, not {type(self.speaker).__name__}")
        if self.speaker.id != self.principal or self.speaker.role is not Role.OPERATOR:
            raise ValueError(
                "an Order is the principal speaking as the operator and nobody else (D61, "
                "D27): anyone else gives a Request"
            )


@dataclass(frozen=True, slots=True)
class Request:
    """Anyone else: a call runs on nobody's say-so here. ``reason`` is one line for the
    person, built from the identity and the principal's name, never from the message."""

    principal: str
    speaker: Speaker
    reason: str


type Authority = Order | Request
