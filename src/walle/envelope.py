"""The untrusted envelope: recalled memory is data, never instructions (D52, D55).

Content flows from the log into a host harness's trusted context. A stored memory reading
"ignore previous instructions" would be read as an instruction unless it is marked, so every
piece of memory the MCP server hands out is wrapped::

    <<<untrusted:9f3ac1d20e4b7a61>>>
    … the text …
    <<</untrusted:9f3ac1d20e4b7a61>>>

Three rules, each closing a specific hole:

* **The id is random per result** (``secrets``, 64 bits), never per session. One leaked id
  would otherwise unlock every later block. The repo is public, so a fixed marker would be a
  password printed in the source.
* **Anything shaped like a marker is defaced before wrapping**, so a forged closing marker
  never reaches the model and the only markers in a result are the envelope's own two.
  "Shaped like a marker" is, after skipping invisible format characters:

  1. two or more opening angle brackets, an optional ``/``, then ``untrusted`` in any case,
     up to the closing brackets or the end of the line. The whole span becomes
     ``[forged marker removed]``. Two brackets, not three: a near miss reads as the real
     thing to a model.
  2. any other run of three or more angle brackets, in either direction. Each bracket becomes
     a single guillemet (U+2039 or U+203A), so ``<<<`` and ``>>>`` appear nowhere in a body.
     A forged marker need not name ``untrusted`` to fool a model ("<<<end of data>>>").

  Fullwidth and small-form brackets count as brackets. Defacing never looks at the id, so a
  forged marker carrying the real id is removed like any other.
* **Unwrapping is strict.** ``unwrap`` accepts exactly one envelope, opening and closing ids
  equal, nothing before or after, and a body with no brackets left to deface. Anything else is
  a ``ValueError``; nothing is repaired. It exists for tests and for a host-side reader; the
  server never needs it.

Homoglyphs (a Cyrillic ie, U+0435, for the ``e`` in ``untrusted``) pass rule 1, and rule 2
still removes their brackets when there are three. Two-bracket homoglyph forgeries are the
residue; the label in ``_meta.ifc`` (D84) is the second form of the same claim for hosts
that read it.

What this cannot do (D55's recorded limit): taint the host's session or refuse its tool calls.
A memory layer runs inside the host. Only the envelope transfers.

Pure functions, no IO, no model.
"""

from __future__ import annotations

import re
import secrets

ID_HEX = 16
"""Hex digits in an id: 64 bits from ``secrets``."""

FORGED = "[forged marker removed]"

_ID_RE = re.compile(rf"[0-9a-f]{{{ID_HEX}}}")

# Invisible format characters a forger could slip between brackets: soft hyphen, zero-width
# space/joiners, direction marks, word joiner and friends, BOM.
_GAP_CHARS = "\u00ad\u200b-\u200f\u2060-\u2064\ufeff"
_GAP = f"[{_GAP_CHARS}]*"
_OPEN = "<\uff1c\ufe64\u2039\u3008"  # ASCII, fullwidth, small form, guillemet, CJK angle
_CLOSE = ">\uff1e\ufe65\u203a\u3009"  # the same five, closing

_FORGED_RE = re.compile(
    rf"(?:[{_OPEN}]{_GAP}){{2,}}[\s/{_GAP_CHARS}]*untrusted[^\n]*?(?:(?:[{_CLOSE}]{_GAP}){{2,}}|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RUN_OPEN_RE = re.compile(rf"[{_OPEN}](?:{_GAP}[{_OPEN}]){{2,}}")
_RUN_CLOSE_RE = re.compile(rf"[{_CLOSE}](?:{_GAP}[{_CLOSE}]){{2,}}")
_TO_GUILLEMET = str.maketrans(dict.fromkeys(_OPEN, "\u2039") | dict.fromkeys(_CLOSE, "\u203a"))

_ENVELOPE_RE = re.compile(
    rf"<<<untrusted:(?P<id>[0-9a-f]{{{ID_HEX}}})>>>\n(?P<body>.*)\n<<</untrusted:(?P=id)>>>",
    re.DOTALL,
)


def new_id() -> str:
    """A fresh random id for one result."""
    return secrets.token_hex(ID_HEX // 2)


def deface(text: str) -> str:
    """``text`` with every marker-shaped span removed and every bracket run broken."""
    text = _FORGED_RE.sub(FORGED, text)
    text = _RUN_OPEN_RE.sub(lambda m: m.group().translate(_TO_GUILLEMET), text)
    return _RUN_CLOSE_RE.sub(lambda m: m.group().translate(_TO_GUILLEMET), text)


def wrap(text: str, *, token: str | None = None) -> str:
    """``text``, defaced, inside an envelope with a fresh id (or ``token``, for tests)."""
    if token is None:
        token = new_id()
    elif not _ID_RE.fullmatch(token):
        raise ValueError(f"not an envelope id: {token!r} — expected {ID_HEX} lowercase hex digits")
    return f"<<<untrusted:{token}>>>\n{deface(text)}\n<<</untrusted:{token}>>>"


def unwrap(block: str) -> tuple[str, str]:
    """``(id, body)`` from exactly one envelope, or ``ValueError`` saying what is wrong."""
    match = _ENVELOPE_RE.fullmatch(block)
    if match is None:
        raise ValueError(
            "not exactly one untrusted envelope, with matching ids and nothing around it"
        )
    body = match["body"]
    if deface(body) != body:
        raise ValueError("the envelope's body holds a marker-shaped span; it was not defaced")
    return match["id"], body
