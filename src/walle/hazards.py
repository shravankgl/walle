"""Secrets and other people's details, found before anything leaves the machine (D18, D77).

Observations hold the text of real sessions. Before one is shown to a model, ``flag`` says
whether it carries something that must not travel: a credential, a secret, contact details,
or a confidence about a third party. The proposer withholds a flagged observation entirely.
That is stricter than D77 asks (a dangerous item may become an assertion *with a flag*) and
coarser than D18 wants (pseudonymise, so the model still sees the structure). Both are the
right later shape. For now the rule is the one that cannot leak: nothing flagged is sent.

Deterministic, and conservative on purpose. A false flag costs one proposal; a miss sends a
secret to a vendor. Every rule here was checked against ``tests/corpora``: every item of
``dangerous.jsonl`` flags, and no item of ``benign.jsonl`` does, including the near-misses
that mention passwords, phone numbers and rules without carrying any.

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

KINDS: tuple[str, ...] = (CREDENTIAL, SECRET, CONTACT, DISCLOSURE)

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
    return tuple(kind for kind in KINDS if kind in found)
