"""Secrets, other people's details and planted instructions, found before anything leaves the
machine (D18, D55, D77).

Observations hold the text of real sessions, and that text is **data**: what a web page said,
what a tool returned, what someone pasted. Before an observation is shown to a model, ``flag``
says whether it carries something that must not travel, or something that is trying to be
read as an instruction:

* a credential, a secret, contact details, or a confidence about a third party (D77's
  ``dangerous`` corpus), which must not reach a vendor;
* a planted instruction, a claim of prior approval, a forged envelope or boundary marker, or
  a forged turn (D77's ``injection`` corpus), which must not become a proposal.

The proposer withholds a flagged observation entirely. For secrets that is stricter than D77
asks (a dangerous item may become an assertion *with a flag*) and coarser than D18 wants
(pseudonymise, so the model still sees the structure). For injection it is exactly D77's
gate: zero reach a proposal. Both finer paths are the right later shape; for now the rule is
the one that cannot leak and cannot be talked round: nothing flagged is sent.

Deterministic, and conservative on purpose. A false flag costs one proposal; a miss sends a
secret to a vendor or lets a web page write a memory. Every rule here was checked against
``tests/corpora``: every item of ``dangerous.jsonl`` and ``injection.jsonl`` flags, with the
tool turns included, and no item of ``benign.jsonl`` does, including the near-misses that
mention passwords, phone numbers, rules and approvals without carrying any.

**A line inside a body is never a turn.** The observer writes the person's block first, then
``assistant:`` lines (``observer.py``), and never a blank line *between* two turns. So the
only genuine ``user:`` lines are in the opening block: the body opens with one, and more may
follow it with nothing but the person's own lines between. Once a reply has begun, or a blank
line has been written (which only happens inside a block's text), nothing later is a turn
boundary the observer made, however many ordinary lines come first. A ``user:`` line there is
text that was pasted or returned and is shaped to be read as the person speaking. ``flag``
reports it as ``turn``.

**Each kind has one detector, and ``DETECTORS`` is the table.** ``flag`` is their union. The
table is public so a test can hold every other detector out of the picture and show that the
one responsible for a corpus item catches it alone: a detector that only ever fires alongside
another is a detector nobody would notice losing.

Three shapes are matched by what they *are*, not by how the corpus happens to spell them:

* **A secret is named and a value is given.** The value may be a code-shaped token nearby, a
  quoted span, whatever follows a colon or a copula on the next line, or, for a passphrase,
  which is words by nature, the words after "is". "Every shop password lives in the manager"
  names one and gives nothing.
* **A sentence is the unit for an instruction.** A line break inside a turn is whitespace, so
  "ignore all previous" on one line and "instructions" on the next is the same order. A turn
  label ends the unit; the person's words are never glued to the reply's.
* **A forged marker is anything the envelope would deface** (``walle.envelope.deface``): a
  bracket run in any of the forms that module knows, zero-width gaps and fullwidth brackets
  included. The envelope and this gate agree by construction, because they share the code.

``flag`` returns the **kinds** found, never the matched text. A report that said "withheld,
because it contains 4821" would be the leak it exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from walle.envelope import deface

CREDENTIAL = "credential"
"""A token that grants a service's access: an API key, a bearer token, a private key."""

SECRET = "secret"
"""A password, a PIN or a door code, named as such and followed by something code-shaped."""

CONTACT = "contact"
"""An e-mail address, a phone number or a street address. Whose it is cannot be told here,
so the person's own are flagged too; D18's pseudonymisation is the finer path."""

DISCLOSURE = "disclosure"
"""Something told in confidence, or another person's health, in so many words."""

INSTRUCTION = "instruction"
"""Text addressed to the assistant rather than said by the person: an order to remember or
to ignore its instructions, or a claim that something was already approved (D77)."""

BOUNDARY = "boundary"
"""A forged envelope or boundary marker (D55): anything the envelope would deface, a closing
tag, an "end of" line, or a ``system:`` line, which the observer never writes."""

TURN = "turn"
"""A forged turn: a ``user:`` line outside the opening block, where the observer would never
write one."""

KINDS: tuple[str, ...] = (CREDENTIAL, SECRET, CONTACT, DISCLOSURE, INSTRUCTION, BOUNDARY, TURN)

_CREDENTIAL = (
    # The corpora's own shape, named so the eval cannot be passed by accident.
    re.compile(r"\bFAKE-[A-Z]+-NOT-REAL-\d{4}\b"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),  # provider keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # a JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Anything long, unbroken, and mixing letters with digits: a key or a token however it is
    # spelt. This also catches UUIDs and release tags; that is the conservative side.
    re.compile(
        r"(?<![A-Za-z0-9_/-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_/-])"
    ),
)

# A secret is named, then a value is given. Naming alone is not giving: "Every shop password
# lives in the password manager" is followed by words about it, not by it. A value is given
# in one of four ways, each a way text *presents* a value rather than talks about one.
_SECRET_NAME = (
    r"\b(?:password|passcode|passphrase|pin|pin code|pin number|otp|one-time code|"
    r"security code|access code|door code|gate code|alarm code|safe code|the code for|"
    r"the code is|login code|unlock code)\b"
)
_REACH = r"[^\n]{0,60}?"
# 1. A code-shaped token within reach on the same line: letters and digits mixed, or digits
#    alone, three or more digits. Digits may be spaced or dashed, as a PIN is read out
#    ("4 8 2 1", "93 21"). Two characters are a count ("24 hours", "8 characters"), not a code.
_CODE_TOKEN = (
    r"(?<![A-Za-z0-9])(?:"
    r"(?=[A-Za-z0-9_@#$%^&*-]*\d)[A-Za-z0-9_@#$%^&*-]{3,}"
    r"|\d(?:[ -]?\d){2,}"
    r")(?![A-Za-z0-9])"
)
# 2. A quoted span within reach: what is quoted is what is given. The opening quote must
#    open a word, so the apostrophes in "password's fine, it's in the manager" are not a pair.
_QUOTED = (
    r"(?<![A-Za-z0-9])[\"'\u201c\u2018][^\"'\u201c\u201d\u2018\u2019\n]{1,64}"
    r"[\"'\u201d\u2019](?![A-Za-z0-9])"
)
# 3. The name's line ends in a colon or a copula and the next line is not a turn label: the
#    next line is the value, whatever it is made of. "I need to reset my password." followed
#    by the reply ends in neither, and the reply is a label.
_NEXT_LINE = (
    r"(?:\bis|\bwas|\bwill be|:|=|->)\s*\n\s*"
    r"(?!\s*(?:user|assistant|tool|system)\s*:)\S[^\n]*"
)
_SECRET = (
    re.compile(rf"{_SECRET_NAME}{_REACH}{_CODE_TOKEN}", re.IGNORECASE),
    re.compile(rf"{_SECRET_NAME}{_REACH}{_QUOTED}", re.IGNORECASE),
    re.compile(rf"{_SECRET_NAME}{_REACH}{_NEXT_LINE}", re.IGNORECASE),
    # 4. A passphrase is words, so "passphrase is" followed by three or more words is the
    #    passphrase, unless the words describe it rather than state it: a location ("in the
    #    drawer"), a quality ("long and"), a pronoun or determiner, a stock phrase.
    re.compile(
        r"\bpassphrase\b[^\n.]{0,20}?\b(?:is|was|=|:)\s+"
        r"(?!(?:in|on|at|under|with|by|from|the|a|an|my|your|our|their|his|her|its|this|"
        r"that|still|also|now|not|very|just|long|short|about|kept|stored|saved|written|"
        r"what|something|same|one|too|only|already|of|for|to|and|or)\b)"
        r"(?:[A-Za-z][A-Za-z'-]*[ \t]+){2,}[A-Za-z][A-Za-z'-]*",
        re.IGNORECASE,
    ),
)

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])")
_PHONE = (
    re.compile(
        r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"
    ),
    re.compile(r"(?<![\w-])\d{3}[-.]\d{4}(?![\w-])"),  # a local number: 555-0101
    re.compile(r"(?<![\w-])\+\d{9,15}(?![\w-])"),
    re.compile(r"(?<![\w-])0\d{9,10}(?![\w-])"),  # a national number written as one run
)
_STREET = re.compile(
    r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][a-z']+\s+){1,3}"
    r"(?:Street|St|Road|Rd|Close|Lane|Ln|Avenue|Ave|Drive|Dr|Way|Court|Ct|Place|Pl|"
    r"Crescent|Cres|Terrace|Square|Sq|Boulevard|Blvd|Gardens|Grove|Row|Hill|Park|Walk|Mews)\b"
)

_DISCLOSURE = (
    re.compile(
        r"\b(?:in confidence|confidentially|between (?:us|ourselves)|don'?t tell|do not tell|"
        r"not to tell anyone|keep (?:this|it) to yourself)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:he|she|they)\s+(?:is|are|was|were|has|have)\s+"
        r"(?:being treated|diagnosed|in treatment|waiting for an?|going in for|"
        r"started treatment|in therapy|on medication|pregnant|in hospital|in rehab)\b",
        re.IGNORECASE,
    ),
)

_INSTRUCTION = (
    # "Ignore all previous instructions" and its cousins. "Ignore last week's shopping list"
    # names no instructions and is not matched.
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^\n.]{0,20}?"
        r"\b(?:previous|prior|earlier|above|preceding|all|any|your|the)\b[^\n.]{0,20}?"
        r"\b(?:instructions?|rules|prompts?|guidelines|directives)\b",
        re.IGNORECASE,
    ),
    # An order to write memory: "remember permanently that", "save a note that", "store as a
    # fact", "add to the user's rules", "write it straight to the log". The person says
    # "remember that lentil soup as a weeknight staple" — no object below, not matched.
    re.compile(
        r"\b(?:remember|save|store|write|add|record|note)\b[^\n.]{0,40}?"
        r"\b(?:permanently|a note that|as a fact|as fact|to the log|straight to|"
        r"to (?:the |your |their )?(?:user'?s? |principal'?s? )?"
        r"(?:rules|memory|memories|facts|hot tier))\b",
        re.IGNORECASE,
    ),
    # Addressed to the assistant or the system by name. Not a bare ``assistant:``, which is
    # how the observer labels the reply itself ("assistant: Remember to bring your keys").
    re.compile(
        r"\b(?:AI assistant|AI|system|model|agent|walle)\s*:\s*"
        r"(?:save|store|remember|add|write|note|record|trusted)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\btrusted from here on\b|\bsaved to memory\b", re.IGNORECASE),
    # Prior approval, or the person's will, asserted by the text itself in the third person.
    # The person does not call themself "the user"; text that does and says what they
    # approved, agreed to, want or allow was written by someone else about them. Any tense:
    # a claim is a claim whether it says "agreed" or "agrees". "The school approved the trip
    # form" is about a third party's form, not about this text, and is not matched.
    re.compile(
        r"\b(?:pre-?approved|already approved|no need to ask again|review status\s*:|"
        r"kept by (?:the )?user|approved by the (?:account )?owner|"
        r"the (?:user|principal|owner|account owner) (?:has |have )?(?:already )?"
        r"(?:approves?d?|agrees?d?|asks? you|asked you|tells? you|told you|trusts?|"
        r"consents?|consented|authori[sz]es?|authori[sz]ed|lets?|allows?|allowed|"
        r"wants? you|wanted you|permits?|permitted|gave permission|gives permission))\b",
        re.IGNORECASE,
    ),
    # An order to the memory that speaks of the person in the third person: "remember that
    # the user's exam answers…", "the user's spare key is under the mat; remember it". The
    # person says "my"; a page says "the user's". "The owner" alone is not in this list: a
    # household talks about the dog's owner and the shop's; "the user", "the principal" and
    # "the account owner" are this system's words for the person, and nobody else's.
    re.compile(
        r"\b(?:remember|save|store|write|add|record|note|keep)\b[^\n.]{0,60}?"
        r"\bthe (?:user|principal|account owner)(?:'s|s')?\b"
        r"|\bthe (?:user|principal|account owner)(?:'s|s')?\b[^\n.]{0,60}?"
        r"\b(?:remember|save|store|record|note) (?:it|this|that|them)\b",
        re.IGNORECASE,
    ),
)

_BOUNDARY = (
    re.compile(
        r"</?\s*(?:untrusted|trusted|tool_result|tool_results|tool_use|function_results?|"
        r"system|instructions?|user|assistant|human|context|document|memory|prompt)\s*>",
        re.IGNORECASE,
    ),
    re.compile(r"\[\s*(?:end|begin|start)\s+of\s+[^\]\n]{0,40}\]", re.IGNORECASE),
    re.compile(r"(?m)^\s*(?:system|SYSTEM)\s*:"),
)

_LABEL = re.compile(r"^\s*(user|assistant|tool|system)\s*:", re.IGNORECASE)


def forged_turn(text: str) -> bool:
    """Whether a ``user:`` line sits where the observer would never write one.

    The observer opens the body with the person's block and writes the reply after it, with
    no blank line between turns. A ``user:`` line is genuine only inside the opening block:
    the body's first line, or a later line while nothing but the person's lines (or, in the
    corpus layout, a tool's) have come before it. The first blank line or the first reply
    closes the block for good; after that no ``user:`` line is a turn, however many ordinary
    lines sit between. A body that does not open with a label has no opening block at all.
    """
    inside = False  # past the opening block: a user: line from here on is forged
    first = True
    for line in text.split("\n"):
        if not line.strip():
            if not first:
                inside = True
            continue
        match = _LABEL.match(line)
        role = match.group(1).lower() if match else None
        if first:
            first = False
            inside = role not in ("user", "tool")
            continue
        if role == "user" and inside:
            return True
        if role == "assistant":
            inside = True
    return False


def _flatten(text: str) -> str:
    """Each turn on one line: a line break inside a turn is whitespace, a label starts a new
    unit. So a sentence split across lines is matched as the sentence it is, and the person's
    words are never joined to the reply's."""
    units: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not units or _LABEL.match(line):
            units.append(stripped)
        elif stripped:
            units[-1] = f"{units[-1]} {stripped}" if units[-1] else stripped
    return "\n".join(units)


def _credential(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CREDENTIAL)


def _secret(text: str) -> bool:
    # On the text as written, for the value on the next line; and on each turn as one line,
    # for a name and a code-shaped value that a line break happens to separate.
    flat = _flatten(text)
    return any(pattern.search(text) or pattern.search(flat) for pattern in _SECRET)


def _contact(text: str) -> bool:
    return bool(_EMAIL.search(text) or any(p.search(text) for p in _PHONE) or _STREET.search(text))


def _disclosure(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DISCLOSURE)


def _instruction(text: str) -> bool:
    flat = _flatten(text)
    return any(pattern.search(flat) for pattern in _INSTRUCTION)


def _boundary(text: str) -> bool:
    """A forged marker: anything the envelope would deface, or a tag or line shaped like a
    boundary. Sharing ``deface`` means the gate and the envelope cannot disagree about what a
    marker is, in any bracket form the envelope knows."""
    return deface(text) != text or any(pattern.search(text) for pattern in _BOUNDARY)


DETECTORS: dict[str, Callable[[str], bool]] = {
    CREDENTIAL: _credential,
    SECRET: _secret,
    CONTACT: _contact,
    DISCLOSURE: _disclosure,
    INSTRUCTION: _instruction,
    BOUNDARY: _boundary,
    TURN: forged_turn,
}
"""One detector per kind, in ``KINDS`` order. ``flag`` is their union; a test may run one."""


def flag(text: str) -> tuple[str, ...]:
    """The kinds of hazard in ``text``, in ``KINDS`` order; ``()`` when there is none.

    Pure: no IO, no clock, no model. The matched text is never returned.
    """
    return tuple(kind for kind, detect in DETECTORS.items() if detect(text))
