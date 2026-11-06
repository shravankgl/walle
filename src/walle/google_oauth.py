"""What the two Google connectors share about a token (issue #136).

``walle.gmail`` and ``walle.google_calendar`` each sign the owner in with OAuth and each ask
for one read-only scope. Google can still hand back more: ``include_granted_scopes`` would
fold in every scope the client id was ever granted, and a project that once held
``gmail.modify`` or ``calendar`` keeps that grant on its consent record. A token that could
write must never reach the disk, for either connector, and the rule that says so is written
once here so the two cannot drift apart: ``wider_than`` is the check both ``_token_from``
functions run before a ``Token`` is built.

``scrubbed`` is the other thing both need and both got wrong in the same place (the low on
PR #65): the token endpoint's error text went into an ``AuthError`` as Google wrote it. A
misconfigured proxy that echoes the form back would have put the client secret, the code and
the PKCE verifier on the terminal. Every secret is replaced, the text is one line, and it is
cut.

Nothing here knows a scope, a host or the wire. The connectors own those.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[redacted]"


def wider_than(allowed: Iterable[str], reply: Mapping[str, Any]) -> str | None:
    """The scopes a token-endpoint reply grants beyond ``allowed``, as one line of text, or
    ``None`` when the grant is no wider than asked.

    A reply without a ``scope`` (a refresh may omit it) or with one that is not text is taken
    as the grant already held: the check refuses what Google *says* is wider, and says nothing
    when Google says nothing.
    """
    granted = reply.get("scope")
    if not isinstance(granted, str):
        return None
    extra = sorted(set(granted.split()) - set(allowed))
    return scrubbed(" ".join(extra), ()) if extra else None


def scrubbed(text: str, secrets: Iterable[str], *, limit: int = 200) -> str:
    """``text`` with every non-empty secret replaced by ``[redacted]``, folded onto one line,
    and cut at ``limit`` characters. For anything that goes into an error message."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return " ".join(text.split())[:limit]
