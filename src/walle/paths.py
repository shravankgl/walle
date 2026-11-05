"""Where things live on disk, and the validation that keeps them there.

    <root>/
      principals/
        p-maya/
          log/
            d-mbp.jsonl
            d-phone.jsonl

Every segment is self-describing and prefixed — ``p-`` principal, ``d-`` device — so a
collapsed layout is never ambiguous and a device can never masquerade as a principal (D85).

The prefixes are **added here, not accepted from callers**. A caller passes ``maya``; this
module produces ``p-maya``. Taking the prefixed form as input invites ``p-p-maya`` and, worse,
makes every call site responsible for a rule only one of them will remember.

Names are validated as a single safe segment before they are joined to anything. A value that
could escape is rejected, never sanitized: sanitizing turns an attack into a valid path
somewhere unintended, while rejecting turns it into an error message (D85, D109).
"""

from __future__ import annotations

import re
from pathlib import Path

# Lowercase, no dots, no separators. Dots are excluded outright rather than filtered for
# `..`: a name needs none, and the rule is then one a reader can check at a glance.
# Lowercase because macOS and Windows filesystems are case-insensitive — `p-Maya` and
# `p-maya` would be one directory holding two principals' logs.
# Anchors are deliberately absent and every use below is `fullmatch`, never `match`.
# Python's `$` matches at the end of a string *or just before a trailing newline*, so
# `^...$` with `.match` accepted "d-mbp:1\n" as a ref and "maya\n" as a name. A ref with a
# trailing newline prints identically to one without and compares unequal — the "one ref
# naming two events" failure this project calls its worst — and a name with one became a
# directory. Found by an independent reviewer after two author-blind passes missed it.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")

PRINCIPAL_PREFIX = "p-"
DEVICE_PREFIX = "d-"
LOG_DIRNAME = "log"
STREAM_SUFFIX = ".jsonl"
INDEX_FILENAME = "index.sqlite"
INDEX_LOCK_FILENAME = "index.lock"
HOT_DIRNAME = "hot"
STAGING_LOCK_FILENAME = "staging.lock"
INBOX_LOCK_FILENAME = "inbox.lock"
AUTOMATIONS_LOCK_FILENAME = "automations.lock"


def check_name(name: str, *, kind: str) -> str:
    """Validate one path segment — a principal or a device name, unprefixed.

    Raises ``ValueError`` naming the offending value. The caller is expected to let it
    propagate: there is no safe repair for a name that does not fit.
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"not a usable {kind} name: {name!r} — lowercase letters, digits, "
            "'_' and '-' only, starting with a letter or digit, 63 characters at most"
        )
    return name


def stream_id(device: str) -> str:
    """``mbp`` -> ``d-mbp``, the stream half of a provenance ref (D57)."""
    return f"{DEVICE_PREFIX}{check_name(device, kind='device')}"


def principal_dir(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>``."""
    return root / "principals" / f"{PRINCIPAL_PREFIX}{check_name(principal, kind='principal')}"


def log_dir(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/log`` — one file per device."""
    return principal_dir(root, principal) / LOG_DIRNAME


def stream_path(root: Path, principal: str, device: str) -> Path:
    """``<root>/principals/p-<principal>/log/d-<device>.jsonl``."""
    return log_dir(root, principal) / f"{stream_id(device)}{STREAM_SUFFIX}"


def index_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/index.sqlite`` — derived, rebuildable (D10).

    One per principal, inside their folder, never one per tenant: an index spanning two
    principals would put their facts back in one place (hard constraint 6).
    """
    return principal_dir(root, principal) / INDEX_FILENAME


def index_lock_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/index.lock`` — held by whoever changes the index."""
    return principal_dir(root, principal) / INDEX_LOCK_FILENAME


def hot_dir(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/hot`` — the rendered hot tier and ``rules.md``.

    Inside the principal's folder like everything else that holds their facts (hard
    constraint 6). The rendered files are derived (D10, D107); ``rules.md`` is not.
    """
    return principal_dir(root, principal) / HOT_DIRNAME


def staging_lock_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/staging.lock`` — held while a review decides one item.

    Per principal, not per stream: two reviews on two devices' streams of the same profile
    must still see each other's decisions.
    """
    return principal_dir(root, principal) / STAGING_LOCK_FILENAME


def inbox_lock_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/inbox.lock`` — held while one inbox item is asked or
    resolved (D78).

    Per principal, like ``staging.lock``: two answers given on two devices' streams of the same
    profile must still see each other, or both would win.
    """
    return principal_dir(root, principal) / INBOX_LOCK_FILENAME


def automations_lock_path(root: Path, principal: str) -> Path:
    """``<root>/principals/p-<principal>/automations.lock`` — held while one change to the
    register of active automations is decided (D6).

    Per principal, like ``inbox.lock``: two adds on two devices' streams of the same profile
    must still see each other, or both would take the last slot under the cap.
    """
    return principal_dir(root, principal) / AUTOMATIONS_LOCK_FILENAME
