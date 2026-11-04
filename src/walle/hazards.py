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

**A line inside a body is never a turn.** The observer writes one ``user:`` block, then
``assistant:`` lines (``observer.py``). So a ``user:`` line that follows an ``assistant:`` line,
or follows a blank line, did not come from the person: it is text that was pasted or returned
and is shaped to be read as the person speaking. ``flag`` reports it as ``boundary``.

``flag`` returns the **kinds** found, never the matched text. A report that said "withheld,
because it contains 4821" would be the leak it exists to prevent.
"""

from __future__ import annotations

import re

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
"""A forged envelope or boundary marker (D55), or a forged turn: a ``user:`` or ``system:``
line where the observer would never write one."""

KINDS: tuple[str, ...] = (CREDENTIAL, SECRET, CONTACT, DISCLOSURE, INSTRUCTION, BOUNDARY)

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

# A secret is named, then within a short reach something code-shaped follows: a token with
# a digit in it. "Every shop password lives in the password manager" names one and is
# followed by words, so it is not flagged; "my pin is 4821" is.
_SECRET = re.compile(
    r"\b(?:password|passcode|passphrase|pin|otp|one-time code|security code|access code|"
    r"door code|gate code|the code for|the code is|login code|unlock code)\b"
    r"[^\n]{0,60}?(?<![A-Za-z0-9])(?=[A-Za-z0-9_@#$%^&*-]*\d)[A-Za-z0-9_@#$%^&*-]{4,}(?![A-Za-z0-9])",
    re.IGNORECASE,
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
    # Prior approval asserted by the text itself. "The school approved the trip form" is
    # about a third party's form, not about this text, and is not matched.
    re.compile(
        r"\b(?:pre-?approved|already approved|no need to ask again|review status\s*:|"
        r"kept by (?:the )?user|approved by the (?:account )?owner|"
        r"the (?:user|principal|owner|account owner) (?:has )?(?:already )?"
        r"(?:approved|agreed|asked you|told you|trusts|consents|authori[sz]ed|lets|allows|"
        r"wants you))\b",
        re.IGNORECASE,
    ),
)

_BOUNDARY = (
    re.compile(r"<<<\s*/?\s*(?:un)?trusted\b|<<<[^\n>]{0,40}>>>"),
    re.compile(
        r"</?\s*(?:untrusted|trusted|tool_result|tool_results|tool_use|function_results?|"
        r"system|instructions?|user|assistant|human|context|document|memory|prompt)\s*>",
        re.IGNORECASE,
    ),
    re.compile(r"\[\s*(?:end|begin|start)\s+of\s+[^\]\n]{0,40}\]", re.IGNORECASE),
    re.compile(r"(?m)^\s*(?:system|SYSTEM)\s*:"),
)

_TURN = re.compile(r"^\s*(user|assistant)\s*:", re.IGNORECASE)


def forged_turn(text: str) -> bool:
    """Whether a ``user:`` line sits where the observer would never write one: after an
    ``assistant:`` line, or after a blank line. ``observer._body`` writes the person's
    block first and the reply after it, with no blank line between roles; anything else
    shaped like a turn came from inside a block."""
    replied = False
    blank = False
    for line in text.split("\n"):
        if not line.strip():
            blank = True
            continue
        match = _TURN.match(line)
        if match:
            role = match.group(1).lower()
            if role == "user" and (replied or blank):
                return True
            replied = replied or role == "assistant"
        blank = False
    return False


def flag(text: str) -> tuple[str, ...]:
    """The kinds of hazard in ``text``, in ``KINDS`` order; ``()`` when there is none.

    Pure: no IO, no clock, no model. The matched text is never returned.
    """
    found: set[str] = set()
    if any(pattern.search(text) for pattern in _CREDENTIAL):
        found.add(CREDENTIAL)
    if _SECRET.search(text):
        found.add(SECRET)
    if _EMAIL.search(text) or any(p.search(text) for p in _PHONE) or _STREET.search(text):
        found.add(CONTACT)
    if any(pattern.search(text) for pattern in _DISCLOSURE):
        found.add(DISCLOSURE)
    if any(pattern.search(text) for pattern in _INSTRUCTION):
        found.add(INSTRUCTION)
    if any(pattern.search(text) for pattern in _BOUNDARY) or forged_turn(text):
        found.add(BOUNDARY)
    return tuple(kind for kind in KINDS if kind in found)
