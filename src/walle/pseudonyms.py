"""Pseudonyms at the egress boundary (D18), and the one door every proposer call goes through.

Off by default (issue #144's dormant rule). With ``WALLE_PSEUDONYMS=on`` in the owner's
environment, and only then, ``Pseudonymiser`` replaces addresses, phone numbers, street
addresses, account numbers and message ids with **stable local tokens** before the proposer
sends anything, and puts the real values back into what comes back. The log is not changed:
connectors keep writing what they write today, and a proposal is staged re-identified, so
the log stays what D18 wants it to be, re-identifiable locally and nowhere else. The mapping
from token to value never leaves the machine: it is never in a model call, a report, a log
line or the terminal, and nothing here has a ``__repr__`` or an error message that carries
a value.

Off, nothing changes: the proposer's hazard gate withholds an observation carrying contact
details exactly as before, and this module writes nothing anywhere.

What becomes a token
--------------------
A structured header first, then anything of the same shape in text:

``From``, ``To``, ``Cc``, ``Organizer``, ``Attendees``
    each mailbox, ``Name <address>`` or a bare address, becomes one ``PERSON_`` token. The
    display name goes with the address; the note after it stays (``(you, accepted)``), as
    does ``and 5 more``.
``Message-ID``
    the value becomes one ``MSGID_`` token.
an address in text
    ``PERSON_``, the same token as the header gives the same address.
a phone number, a street address
    ``PHONE_``, ``ADDRESS_``, by exactly the patterns the hazard gate flags as ``contact``
    (``hazards.EMAIL_PATTERN``, ``PHONE_PATTERNS``, ``STREET_PATTERN``). Sharing the
    patterns means the layer and the gate cannot disagree about what an address is: a
    shape the gate knows is replaced here, and a shape this layer misses is still flagged
    there and withheld, never sent.
an account number
    ``ACCOUNT_``: an IBAN, or a run of six or more digits after a word that names an
    account. The gate has no detector for these, so a miss here is the door's to catch,
    below, when the value is one the mapping already holds.

Free-text person names are a **known gap**. "Thanks, Alice" is left as it is: telling a
name from a word needs a list or a model, and neither belongs here (no dependency, and no
model before the egress check). A name beside an address in a header is replaced because
the header says it is a name. Say so in the devlog; do not widen this quietly.

Tokens are stable
-----------------
A token is ``<KIND>_<10 hex>``, the hex being an HMAC-SHA256 of the normalised value (an
address lowercased, a phone reduced to its digits) under a **per-principal key**. So the
same address is the same token across runs, across a rebuild (``walle rebuild`` discards
derived state, and neither the key nor the mapping is derived), and across two devices that
share the profile folder (D32, D57), with no coordination: each device computes the token
from the key, and the mapping only records what it stands for. ``PERSON_A`` means the same
person across years of log, which is what D18 says the frontier model needs. The token is
short enough, and shaped so, that no hazard detector matches it.

The mapping is its own append-only stream
-----------------------------------------
``principals/p-<name>/pseudonyms/`` holds ``key`` and one ``d-<device>.jsonl`` per device,
mode ``0600`` in a ``0700`` folder, beside the log and never in it. Per principal (D28,
D32): one person's mapping is read by nothing that serves another. A line is an ``Entry``:
``{"v": 1, "ts": …, "op": "map", "token": …, "value": …}``. Entries are appended, never
rewritten (D18): a token is written once, to the stream of the device that first sees its
value with no line for it in any stream present; a device that finds the line already in
another device's file writes nothing.

Reading resolves the stream like the log is resolved: a pure fold, no model. A token's
value is the one its ``map`` line holds; a ``shred`` line for the token, from any device,
wins over every ``map`` line, and a token whose lines disagree (two values, which only a
hash collision can produce) is treated as shredded rather than guessed at. A line that does
not load is stepped over (D80), and an append after a torn tail begins with a newline.

Shredding (D12)
---------------
``shred(token)`` appends a ``shred`` line. From then on the token is opaque everywhere: it is
not re-identified on any device, and the value is not written to the mapping again when it is
seen (it still becomes the same token, so the frontier model's correlation survives, and
nothing sent changes). That is the resolution rule D12 needs. **What is left to build:**
the ``map`` line that was shredded is still on disk, plain, and so is every observation the
value came from. Crypto-shredding proper, a per-entry key beside each value, destroyed with
the entry, so the line becomes unreadable, and D12's TTL compaction of the observations
themselves, are the two halves not here. Until they are, ``shred`` is a resolution rule, not
a deletion, and this docstring says so rather than letting the name imply more.

The door
--------
``Egress`` wraps a model. ``propose`` and the shadow judge call ``door.complete(request)``
and nothing else; the proposer never calls a model's ``complete`` itself, and a test reads
the source to keep it that way. Before the call the door checks **the exact string the
adapter will send**, ``request.prompt``, not the pieces the pipeline thinks went into it:

1. every item in the request is enveloped under this request's marker and sits verbatim in
   the prompt, else it was not built by the pipeline (``envelope``);
2. every item's text, markers off, passes ``hazards.flag`` in full;
3. what is left of the prompt once every item and every item id is taken out of it, the
   instructions and the labels, passes ``hazards.flag`` in full too, so every character of
   the final text but the ids is scanned by some detector pass. The ids are left out
   because they are refs this build made from validated names (``d-<device>:<seq>#<n>``),
   never text from a source, and a long device name with a digit in it reads to the
   credential detector like a key. The items are scanned one at a time rather than the
   prompt whole because the detectors read structure: a sentence that mentions a password
   in one excerpt and the hex marker that opens the next are not one secret, and a
   ``user:`` line that opens an excerpt is not a forged turn because instructions came
   before it;
4. with the layer on, no value the mapping holds appears anywhere in the prompt
   (``mapping``); this one is a plain substring test and runs on the whole string.

Any finding refuses the call: ``Refused`` names the kinds, never the text, and the inner
model is not called. The pipeline scanned everything already, so a refusal here is a
detector that missed earlier or a bug in the cut, and the right answer to either is to send
nothing. What is scanned is what is sent, by construction, because the scan is of the sent
string.

Two writers, step by step
-------------------------
Runs A and B on one device, both pseudonymising text that holds the same new address.

1. Both compute the same token for it: a pure function of the key and the value, with no
   file read. If neither has the key yet, each writes a key under a random temporary name
   and tries to ``link`` it as ``key``; the link is atomic and fails for the second, who
   unlinks its own and reads the first's. Both now hold the same key; a key is never
   partly written where a reader can see it.
2. Both read the mapping (this device's file under a shared lock, waited for; the other
   devices' files under a shared lock that does not wait, as the proposer reads other
   streams) and find no line for the token.
3. A takes this device's file exclusively, re-reads it under the lock, still finds no
   line, appends one, ``fsync``s, releases.
4. B takes the lock, re-reads, finds A's line, appends nothing.

Nothing between steps 1 and 4 can make B's token differ from A's, and the worst a crash can
leave is a torn tail, which every read steps over and the next append closes. Two devices
each append to their own file, so they never contend; a token mapped on one is read on the
other once the folder has synced, and until then the other re-identifies nothing for it,
which is a value withheld locally, never a value sent.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from walle import hazards
from walle.events import Instant, now_instant
from walle.paths import STREAM_SUFFIX, check_name, principal_dir, stream_id

SWITCH = "WALLE_PSEUDONYMS"
"""The environment variable the owner sets to turn the layer on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for every other switch."""

MAPPING_DIRNAME = "pseudonyms"
KEY_FILENAME = "key"
KEY_BYTES = 32
DIGEST_CHARS = 10
"""Hex characters of the HMAC a token carries: 40 bits, far past any one person's contacts,
and short enough that no token is the long mixed run the credential detector flags."""

PERSON = "PERSON"
"""A mailbox: an address, with whatever display name stood beside it."""
PHONE = "PHONE"
ADDRESS = "ADDRESS"
"""A street address."""
ACCOUNT = "ACCOUNT"
MSGID = "MSGID"
KINDS: tuple[str, ...] = (PERSON, PHONE, ADDRESS, ACCOUNT, MSGID)

TOKEN = re.compile(rf"\b(?:{'|'.join(KINDS)})_[0-9a-f]{{{DIGEST_CHARS}}}\b")
"""A token as this module writes one. No capture group, so ``findall`` returns tokens."""

LEAK = "mapping"
"""The door's own kind: a value the mapping holds was in the final text."""

UNWRAPPED = "envelope"
"""The door's other kind: an item not enveloped under its request's marker, or not in the
prompt at all, was handed to it."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the layer on: ``WALLE_PSEUDONYMS`` is exactly ``on``.
    Reads the process environment when no mapping is given. No event and no reply can reach
    it."""
    env = os.environ if environ is None else environ
    return env.get(SWITCH) == SWITCH_ON


def mapping_dir(root: Path, principal: str) -> Path:
    """``principals/p-<name>/pseudonyms/``: beside the log, never in it, and read by nothing
    that serves another principal (D28, D32)."""
    return principal_dir(Path(root), principal) / MAPPING_DIRNAME


class Entry(BaseModel):
    """One line of a mapping stream. Not an event: the log never holds one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1
    ts: Instant
    op: Literal["map", "shred"]
    token: str
    value: str | None = None
    """What the token stands for, for a ``map`` line; absent for a ``shred``."""

    @model_validator(mode="after")
    def _shaped(self) -> Entry:
        if not TOKEN.fullmatch(self.token):
            raise ValueError("not a token this build writes")
        if (self.op == "map") != (self.value is not None):
            raise ValueError("a map line carries a value and a shred line carries none")
        return self


class Refused(Exception):
    """The door did not send: the final text carried what ``kinds`` names. The message
    names the kinds and nothing of the text."""

    def __init__(self, kinds: tuple[str, ...]) -> None:
        super().__init__(f"not sent: the final text carries {', '.join(kinds)}")
        self.kinds = kinds


# --- the patterns ----------------------------------------------------------------------------

_HEADER = re.compile(r"(?im)^((?:From|To|Cc|Bcc|Reply-To|Organizer|Attendees):[ \t]*)(.*)$")
_MSGID_HEADER = re.compile(r"(?im)^(Message-ID:[ \t]*)(\S+)[ \t]*$")
# A mailbox on a header line: a quoted or bare display name, then ``<address>``; or a bare
# address, with an RFC 5322 comment after it or not (``addr (Name)``). The name must open
# with something other than a space or a comma, so the separator between two mailboxes is
# left where it is, and it may hold a comma: ``google_calendar._person`` writes the name
# unquoted, so ``Tutor, Alice <addr>`` is one mailbox (review of PR #145). It may not hold
# ``@``, so a bare address before a named one is never swallowed into the name, nor a
# parenthesis, so the note after one mailbox (``(you, accepted)``) is never the start of
# the next one's name.
_MAILBOX = re.compile(
    r'(?:"[^"\n]*"|[^<>,@()\n\s][^<>@()\n]*?)?\s*<([^<>\s@]+@[^<>\s@]+)>'
    rf"|({hazards.EMAIL_PATTERN.pattern})(?:[ \t]*\([^()\n]*\))?"
)
_ACCOUNT = re.compile(
    r"(?i)(\b(?:account|acct\.?|a/c|iban|sort code|routing(?: number)?|card)"
    r"(?:[ \t]+(?:number|no\.?|#))?[ \t]*[:#]?[ \t]*)"
    r"([A-Z]{2}\d{2}(?:[ \t]?[A-Z0-9]{4}){2,7}(?:[ \t]?[A-Z0-9]{1,4})?|\d(?:[ -]?\d){5,})"
    r"(?![\w-])"
)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _collapsed(value: str) -> str:
    return " ".join(value.split()).lower()


def _alnum(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum()).upper()


def _replace_known(text: str, known: Mapping[str, str]) -> str:
    """``text`` with every value in ``known`` replaced by its token, case-insensitively,
    longest first so a value inside a longer one is not cut up. Only the text between tokens
    is searched: a digit run inside a token's hex is the token, not a value."""
    if not known or not any(value.lower() in text.lower() for value in known):
        return text
    values = re.compile(
        "|".join(re.escape(v) for v in sorted(known, key=len, reverse=True)), re.IGNORECASE
    )
    by_lower = {v.lower(): t for v, t in known.items()}
    parts = TOKEN.split(text)
    tokens = TOKEN.findall(text)
    parts = [values.sub(lambda m: by_lower[m.group(0).lower()], p) for p in parts]
    return parts[0] + "".join(t + p for t, p in zip(tokens, parts[1:], strict=True))


# --- the layer -------------------------------------------------------------------------------


class Pseudonymiser:
    """Replace, re-identify and shred for one principal, writing to one device's stream.

    Holds no value in memory between calls beyond the key, and says nothing about either in
    its ``repr``.
    """

    def __init__(self, root: Path, principal: str, device: str) -> None:
        self._root = Path(root)
        self._principal = check_name(principal, kind="principal")
        self._device = check_name(device, kind="device")
        self._dir = mapping_dir(self._root, self._principal)
        self._path = self._dir / f"{stream_id(self._device)}{STREAM_SUFFIX}"
        self._key: bytes | None = None

    def __repr__(self) -> str:
        return f"Pseudonymiser(principal={self._principal!r}, device={self._device!r})"

    # -- replacing --

    def pseudonymise(self, text: str) -> str:
        """``text`` with every value this module knows the shape of replaced by its token,
        and every new token recorded in this device's mapping stream. Text with nothing to
        replace is returned as it is, and nothing is written or created for it."""
        found: dict[str, str] = {}  # token -> the value it stands for, as first seen here

        def token(kind: str, normalised: str, value: str) -> str:
            tok = self._token(kind, normalised)
            found.setdefault(tok, value)
            return tok

        def msgid(match: re.Match[str]) -> str:
            value = match.group(2)
            return match.group(1) + token(MSGID, value, value)

        def mailbox(match: re.Match[str]) -> str:
            address = (match.group(1) or match.group(2)).lower()
            return token(PERSON, address, address)

        def bare(match: re.Match[str]) -> str:
            address = match.group(0).lower()
            return token(PERSON, address, address)

        def header(match: re.Match[str]) -> str:
            return match.group(1) + _MAILBOX.sub(mailbox, match.group(2))

        def account(match: re.Match[str]) -> str:
            value = match.group(2)
            return match.group(1) + token(ACCOUNT, _alnum(value), value)

        def phone(match: re.Match[str]) -> str:
            value = match.group(0)
            return token(PHONE, _digits(value), value)

        def street(match: re.Match[str]) -> str:
            value = match.group(0)
            return token(ADDRESS, _collapsed(value), value)

        # The structured headers first, so ``Name <addr>`` is one mailbox and the name goes
        # with the address. Then every value the mapping already holds, and this call's new
        # ones, wherever they appear: an account number or a message id is only
        # *recognised* after its keyword or header, but once known it is replaced bare as
        # well, so a repeat of it never reaches the door (review of PR #145, medium 2).
        # That pass runs before the shapes in text so a known message id is its ``MSGID_``
        # token and not the address inside it, and once more after them for what they found.
        known = {entry.value: entry.token for entry in self._entries() if entry.value is not None}
        out = _MSGID_HEADER.sub(msgid, text)
        out = _HEADER.sub(header, out)
        out = _replace_known(out, {**known, **{v: t for t, v in found.items()}})
        out = hazards.EMAIL_PATTERN.sub(bare, out)
        out = _ACCOUNT.sub(account, out)
        for pattern in hazards.PHONE_PATTERNS:
            out = pattern.sub(phone, out)
        out = hazards.STREET_PATTERN.sub(street, out)
        out = _replace_known(out, {v: t for t, v in found.items()})
        if found:
            self._record(found)
        return out

    def _token(self, kind: str, normalised: str) -> str:
        digest = hmac.new(self._key_bytes(), f"{kind}\0{normalised}".encode(), hashlib.sha256)
        return f"{kind}_{digest.hexdigest()[:DIGEST_CHARS]}"

    # -- reading back --

    def reidentify(self, text: str) -> str:
        """``text`` with every token the mapping resolves replaced by its value. A token the
        mapping does not hold, or holds shredded, is left as it is."""
        resolved = self._resolved()

        def value(match: re.Match[str]) -> str:
            return resolved.get(match.group(0)) or match.group(0)

        return TOKEN.sub(value, text)

    def leaks(self, text: str) -> bool:
        """Whether any value the mapping holds, shredded or not, appears in ``text``. The
        door's last check before a send."""
        lower = text.lower()
        return any(
            entry.value is not None and entry.value.lower() in lower for entry in self._entries()
        )

    def shred(self, token: str) -> None:
        """Append a ``shred`` line for ``token``: opaque everywhere from now on (D12). See the
        module docstring for what this does not yet do."""
        if not TOKEN.fullmatch(token):
            raise ValueError("not a token")
        self._append([Entry(ts=now_instant(), op="shred", token=token)])

    # -- the stream --

    def _entries(self) -> tuple[Entry, ...]:
        return read_entries(self._root, self._principal, own=self._path)

    def _resolved(self) -> dict[str, str | None]:
        return resolve(self._entries())

    def _record(self, found: Mapping[str, str]) -> None:
        resolved = self._resolved()
        new = [
            Entry(ts=now_instant(), op="map", token=token, value=value)
            for token, value in found.items()
            if token not in resolved
        ]
        if new:
            self._append(new)

    def _append(self, entries: Iterable[Entry]) -> None:
        """Append each line this device's file does not hold yet, under its exclusive lock:
        read, write, ``fsync``, nothing between (the walkthrough, steps 3 and 4)."""
        self._ensure_dir()
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            os.fchmod(f.fileno(), 0o600)
            f.seek(0)
            data = f.read()
            held = _parse(data)
            have = {(e.op, e.token) for e in held}
            shredded = {e.token for e in held if e.op == "shred"}
            torn = bool(data) and not data.endswith(b"\n")
            written = False
            for entry in entries:
                if (entry.op, entry.token) in have or (
                    entry.op == "map" and entry.token in shredded
                ):
                    continue
                if torn:
                    f.write(b"\n")
                    torn = False
                f.write(entry.model_dump_json().encode("utf-8") + b"\n")
                have.add((entry.op, entry.token))
                written = True
            if written:
                f.flush()
                os.fsync(f.fileno())

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)

    def _key_bytes(self) -> bytes:
        if self._key is None:
            self._key = _load_key(self._dir)
        return self._key


def read_entries(root: Path, principal: str, *, own: Path | None = None) -> tuple[Entry, ...]:
    """Every line that loads, from every device's mapping stream, in file then line order.
    ``own`` is read under a shared lock that waits; every other file under one that does
    not, as the proposer reads other devices' streams. ``()`` when there is no mapping."""
    folder = mapping_dir(root, principal)
    if not folder.is_dir():
        return ()
    out: list[Entry] = []
    for path in sorted(folder.glob(f"*{STREAM_SUFFIX}")):
        with open(path, "rb") as f:
            if path == own:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            else:
                with contextlib.suppress(BlockingIOError):
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            out.extend(_parse(f.read()))
    return tuple(out)


def resolve(entries: Iterable[Entry]) -> dict[str, str | None]:
    """Token to value, as a pure fold: a ``shred`` wins over every ``map``, and two ``map``
    lines that disagree resolve to nothing rather than to one of them."""
    out: dict[str, str | None] = {}
    for entry in entries:
        if entry.op == "shred":
            out[entry.token] = None
        elif entry.token not in out:
            out[entry.token] = entry.value
        elif out[entry.token] is not None and out[entry.token] != entry.value:
            out[entry.token] = None
    return out


def _parse(data: bytes) -> tuple[Entry, ...]:
    out: list[Entry] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            out.append(Entry.model_validate_json(line))
        except ValidationError:
            continue  # D80: a line that does not load is stepped over, never fatal
    return tuple(out)


def _load_key(folder: Path) -> bytes:
    """The principal's key, created once if there is none (the walkthrough, step 1)."""
    path = folder / KEY_FILENAME
    if not path.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        temp = folder / f"{KEY_FILENAME}.{secrets.token_hex(8)}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_hex(KEY_BYTES).encode("ascii") + b"\n")
                f.flush()
                os.fsync(f.fileno())
            with contextlib.suppress(FileExistsError):
                os.link(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    os.chmod(path, 0o600)
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except ValueError:  # UnicodeDecodeError is one; neither message is let through
        key = b""
    if len(key) != KEY_BYTES:
        raise ValueError(f"{path}: not a key this build wrote")
    return key


# --- the door --------------------------------------------------------------------------------


class Outbound(Protocol):
    """What the door needs to see of a request: the proposer's ``Request`` has all three."""

    @property
    def marker(self) -> str: ...
    @property
    def items(self) -> tuple[tuple[str, str], ...]: ...
    @property
    def prompt(self) -> str: ...


class Model(Protocol):
    @property
    def name(self) -> str: ...

    def complete(self, request: Any) -> str: ...


class Egress:
    """The one door. ``complete`` checks the exact final text and sends it, or refuses."""

    def __init__(self, model: Model, layer: Pseudonymiser | None) -> None:
        self._model = model
        self._layer = layer

    @property
    def name(self) -> str:
        return self._model.name

    def __repr__(self) -> str:
        return f"Egress(model={self._model.name!r}, layer={'on' if self._layer else 'off'})"

    def check(self, request: Outbound) -> tuple[str, ...]:
        """The kinds the final text carries, in ``hazards.KINDS`` order then this module's;
        ``()`` when it is clean. Pure over the request."""
        found: set[str] = set()
        opening = f"<<<untrusted:{request.marker}>>>\n"
        closing = f"\n<<</untrusted:{request.marker}>>>"
        whole: list[tuple[str, str]] = []
        for item_id, wrapped in request.items:
            if (
                not wrapped.startswith(opening)
                or not wrapped.endswith(closing)
                or wrapped not in request.prompt
            ):
                found.add(UNWRAPPED)
                continue
            found.update(hazards.flag(wrapped[len(opening) : -len(closing)]))
            whole.append((item_id, wrapped))
        # The residue is worked out from the original prompt, never by mutating it item by
        # item: every block comes out first, then every id, longest first. Stripping an id
        # before a later block was removed could change that block, leave its markers
        # behind, and refuse the request as ``boundary`` (review of PR #145, medium 1).
        rest = request.prompt
        for _, wrapped in whole:
            rest = rest.replace(wrapped, "")
        for item_id, _ in sorted(whole, key=lambda pair: len(pair[0]), reverse=True):
            rest = rest.replace(item_id, "")
        found.update(hazards.flag(rest))
        if self._layer is not None and self._layer.leaks(request.prompt):
            found.add(LEAK)
        order = (*hazards.KINDS, LEAK, UNWRAPPED)
        return tuple(kind for kind in order if kind in found)

    def complete(self, request: Outbound) -> str:
        if kinds := self.check(request):
            raise Refused(kinds)
        return self._model.complete(request)
