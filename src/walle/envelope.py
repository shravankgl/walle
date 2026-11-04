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
  "Shaped like a marker" is, after skipping invisible characters:

  1. two or more opening angle brackets, with horizontal whitespace allowed between them
     (``<< <``), an optional ``/``, then ``untrusted`` in any case, up to the closing
     brackets or the end of the line. The whole span becomes ``[forged marker removed]``.
     Two brackets, not three: a near miss reads as the real thing to a model. **A line break
     ends it**: ``<<`` at the end of one line and ``untrusted`` at the start of the next are
     two things (``x << 2`` in C, then a sentence), not a marker, and neither line is touched.
  2. any other run of three or more angle brackets, in either direction. Each bracket becomes
     a single guillemet (U+2039 or U+203A), so ``<<<`` and ``>>>`` appear nowhere in a body.
     A forged marker need not name ``untrusted`` to fool a model ("<<<end of data>>>").

  Fullwidth and small-form brackets count as brackets. "Invisible" is every Unicode format
  character (category Cf: soft hyphen, zero-width space and joiners, direction marks and
  isolates, the BOM, tag characters and the rest), plus the zero-width marks that are not Cf
  (the combining grapheme joiner, variation selectors) and the Hangul fillers. They may sit
  between the brackets, before the name, and **between the letters of the name**:
  ``un\u200btrusted`` is ``untrusted`` to a model. ``_GAP_SET`` is written out rather than
  computed from ``unicodedata`` at import; a test holds it to this runtime's Cf category.
  Defacing never looks at the id, so a forged marker carrying the real id is removed like any
  other.
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

# Invisible characters a forger could slip between brackets or between the letters of the
# name. Every Unicode 15 format character (category Cf), by range, then the zero-width marks
# that are not Cf: the combining grapheme joiner, the variation selectors, and the Hangul
# fillers. ``test_the_gap_set_covers_every_format_character`` checks the list against the
# running Python's ``unicodedata``; a newer Unicode with a new Cf character fails it, which
# is the cue to extend this list.
_GAP_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # combining grapheme joiner (Mn)
    (0x0600, 0x0605),  # Arabic number signs
    (0x061C, 0x061C),  # Arabic letter mark
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x115F, 0x1160),  # Hangul fillers (Lo)
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x200B, 0x200F),  # zero-width space, non-joiner, joiner; direction marks
    (0x202A, 0x202E),  # bidi embeddings and overrides
    (0x2060, 0x2064),  # word joiner and invisible operators
    (0x2066, 0x206F),  # bidi isolates and deprecated format characters
    (0x3164, 0x3164),  # Hangul filler (Lo)
    (0xFE00, 0xFE0F),  # variation selectors (Mn)
    (0xFEFF, 0xFEFF),  # BOM
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler (Lo)
    (0xFFF9, 0xFFFB),  # interlinear annotation
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),  # Egyptian hieroglyph format controls
    (0x1BCA0, 0x1BCA3),  # shorthand format controls
    (0x1D173, 0x1D17A),  # musical symbol begin/end
    (0xE0001, 0xE0001),  # language tag
    (0xE0020, 0xE007F),  # tag characters
    (0xE0100, 0xE01EF),  # variation selectors supplement (Mn)
)
_GAP_SET = frozenset(chr(c) for lo, hi in _GAP_RANGES for c in range(lo, hi + 1))
_GAP_CHARS = "".join(f"{chr(lo)}-{chr(hi)}" if hi > lo else chr(lo) for lo, hi in _GAP_RANGES)
_GAP = f"[{_GAP_CHARS}]*"
_BLANK = " \t\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000"
"""Horizontal whitespace, as a character class: every character ``str.isspace`` accepts
except the line breaks in ``_LINE_BREAKS``. Allowed between the brackets and before the
name; a line break is where a would-be marker ends."""
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\u2028\u2029")
_BLANK_SET = frozenset(" \t\u00a0\u1680\u202f\u205f\u3000") | frozenset(
    chr(c) for c in range(0x2000, 0x200B)
)
_OPEN = "<\uff1c\ufe64\u2039\u3008"  # ASCII, fullwidth, small form, guillemet, CJK angle
_CLOSE = ">\uff1e\ufe65\u203a\u3009"  # the same five, closing

# The name with invisible characters allowed between its letters.
_NAME_RE = re.compile(_GAP.join("untrusted"), re.IGNORECASE)
_OPEN_SET = frozenset(_OPEN)
_BETWEEN_BRACKETS = _GAP_SET | _BLANK_SET
# After the name: anything up to two or more closing brackets, or the end of the line. The
# scan is lazy and each step fails at once on a non-bracket, so it is linear.
_TAIL_RE = re.compile(
    rf"[^\n]*?(?:(?:[{_CLOSE}]{_GAP}){{2,}}|$)",
    re.MULTILINE,
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


def _forged_spans(text: str) -> list[tuple[int, int]]:
    """The spans rule 1 removes: two or more opening brackets (invisible characters and
    horizontal whitespace allowed between them), an optional ``/`` with the same around it,
    ``untrusted`` (invisible characters allowed inside it), then up to the closing brackets
    or the end of the line. Nothing here crosses a line break.

    One pass, each character looked at a bounded number of times. A single regex for this
    retries every start in a long run of brackets, which is quadratic on text that is exactly
    what this module treats as hostile.
    """
    spans: list[tuple[int, int]] = []
    floor = 0  # nothing before this belongs to a new span
    for name in _NAME_RE.finditer(text):
        if name.start() < floor:
            continue
        i = name.start()
        while i > floor and _skippable(text[i - 1]):
            i -= 1
        count = 0
        start = i
        while True:
            k = start
            while k > floor and text[k - 1] in _BETWEEN_BRACKETS:
                k -= 1
            if k > floor and text[k - 1] in _OPEN_SET:
                count += 1
                start = k - 1
            else:
                break
        if count < 2:
            continue
        end = _TAIL_RE.match(text, name.end())
        assert end is not None  # the tail can match the empty string at the end of a line
        spans.append((start, end.end()))
        floor = end.end()
    return spans


def _skippable(char: str) -> bool:
    """Horizontal whitespace, ``/`` or an invisible character: allowed before the name. A
    line break is not: it ends a would-be marker."""
    return char in _BLANK_SET or char == "/" or char in _GAP_SET


def deface(text: str) -> str:
    """``text`` with every marker-shaped span removed and every bracket run broken."""
    pieces: list[str] = []
    at = 0
    for start, end in _forged_spans(text):
        pieces += [text[at:start], FORGED]
        at = end
    text = "".join(pieces) + text[at:] if pieces else text
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
