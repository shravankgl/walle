"""Issue #144's verify criteria: D18's pseudonymisation at the egress boundary.

  off by default                 nothing here runs unless the owner sets WALLE_PSEUDONYMS=on
  off, withheld as today         a mail with addresses is withheld as ``contact``, no request made
  on, tokens reach the model     a synthetic Gmail message and Calendar event go out with tokens,
                                 and the proposal comes back re-identified in the local log
  stable tokens                  the same address is the same token across runs, a rebuild and
                                 two devices
  a miss fails closed            a layer that misses an address leaves ``contact`` to withhold it;
                                 the door refuses a final text that still carries one
  the mapping stays home         never in a model call, a report, a log line or the terminal
  one door                       every model call in the proposer goes through ``Egress``
  shredding (D12)                a shredded entry is opaque everywhere, on every device
  the mapping file               private, per device, torn tails passed over, v1 lines load

Every value here is synthetic: addresses at ``example.test``, phone numbers in the fictional
555-01xx range, an invented street, an account number that is a count to eight.
"""

from __future__ import annotations

import ast
import json
import re
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from walle import hazards, proposer, pseudonyms, staging
from walle.cli import main
from walle.events import ACTOR_OBSERVER, Observation, Proposal
from walle.index import rebuild, update
from walle.paths import index_path, log_dir, principal_dir
from walle.reader import Record
from walle.writer import StreamWriter

ROOT = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "pseudonyms"
P = "sam"
D = "mbp"
TS = "2026-11-05T08:00:00+00:00"
ON = {pseudonyms.SWITCH: pseudonyms.SWITCH_ON}
OFF: dict[str, str] = {}

TUTOR = "alice.tutor@example.test"
ME = "sam@example.test"
PAT = "pat@example.test"
BOOKINGS = "bookings@example.test"
MSGID = "<m-0001.abc@mail.example.test>"
PHONE = "555-0142"
STREET = "12 Orchard Lane"
ACCOUNT = "12345678"
NAMES = ("Alice Tutor", "Sam Example", "Pat Example")
VALUES = (TUTOR, ME, PAT, BOOKINGS, MSGID, PHONE, STREET, ACCOUNT, *NAMES)
"""Everything a model must never see, display names included."""

GMAIL = f"""Gmail message m-0001 in thread t-0001
From: Alice Tutor <{TUTOR}>
To: Sam Example <{ME}>, Pat Example <{PAT}>
Cc: {BOOKINGS}
Subject: Piano on Thursdays
Date: Thu, 29 Oct 2026 17:40:00 +0000
Message-ID: {MSGID}

Hello Sam, the Thursday lesson stays at 16:00. Always keep that time free.
Call {PHONE} if anything changes, or write to {TUTOR}."""

CALENDAR = f"""Google Calendar event e-0001 on calendar primary
Status: confirmed
Title: Piano lesson
When: 2026-11-05T16:00:00+00:00 to 2026-11-05T16:30:00+00:00 (UTC)
Location: {STREET}
Organizer: Alice Tutor <{TUTOR}>
Attendees: Sam Example <{ME}> (you, accepted), Alice Tutor <{TUTOR}> (accepted), and 2 more

Bring the grade 3 book. Invoice to account number {ACCOUNT} by Friday."""


def _observe(root: Path, body: str, *, source: str, device: str = D) -> str:
    writer = StreamWriter(root, P, device)
    event = Observation(ts=TS, actor=ACTOR_OBSERVER, source=source, observed_at=TS, body=body)
    written = writer.append(event)
    assert written.seq is not None
    return f"{writer.stream}:{written.seq}"


def _records(root: Path) -> tuple[Record, ...]:
    return update(root, P).records


def _proposals(root: Path) -> list[Proposal]:
    return [r.event for r in _records(root) if isinstance(r.event, Proposal)]


def _inner(wrapped: str) -> str:
    return wrapped.split("\n", 1)[1].rsplit("\n", 1)[0]


def _assert_nothing_private_in(text: str, *, where: str) -> None:
    for value in VALUES:
        assert value not in text, f"{value!r} reached {where}"
    assert hazards.CONTACT not in hazards.flag(text), where


class RecordingModel:
    """The fake, keeping every request for the test to inspect."""

    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.inner = proposer.FakeModel()
        self.requests: list[proposer.Request] = []

    def complete(self, request: proposer.Request) -> str:
        self.requests.append(request)
        return self.inner.complete(request)


class ScriptedModel:
    def __init__(self, reply: Callable[[proposer.Request], str], name: str = "scripted") -> None:
        self.name = name
        self.reply = reply
        self.requests: list[proposer.Request] = []

    def complete(self, request: proposer.Request) -> str:
        self.requests.append(request)
        return self.reply(request)


def _cites_first_person_token(request: proposer.Request) -> str:
    """A reply that proposes a fact about the first ``PERSON_`` token the request shows, so
    the test can watch it come back re-identified. Judge requests get an empty list."""
    if request.kind != "propose":
        return "[]"
    out: list[dict[str, str]] = []
    for item_id, wrapped in request.items:
        if match := re.search(r"PERSON_[0-9a-f]{10}", wrapped):
            token = match.group(0)
            out.append(
                {
                    "excerpt": item_id,
                    "summary": f"Piano lessons are arranged by {token}",
                    "body": f"Piano lessons are arranged by {token}; Thursday 16:00 is kept free.",
                    "level": "explicit",
                }
            )
    return json.dumps(out)


# --- the switch --------------------------------------------------------------------------


def test_the_layer_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's dormant rule: nothing here runs unless the switch is exactly ``on``."""
    monkeypatch.delenv(pseudonyms.SWITCH, raising=False)
    assert not pseudonyms.enabled()
    assert not pseudonyms.enabled(OFF)
    for value in ("", "ON", "On", "1", "true", "yes", " on"):
        assert not pseudonyms.enabled({pseudonyms.SWITCH: value}), value
    assert pseudonyms.enabled(ON)
    assert pseudonyms.SWITCH_ON == proposer.SWITCH_ON  # one spelling of "on" across switches


def test_off_contact_details_stay_withheld_exactly_as_today(tmp_path: Path) -> None:
    root = tmp_path / "root"
    ref = _observe(root, GMAIL, source="gmail")
    model = RecordingModel()
    report = proposer.propose(root, P, D, model, environ=OFF)
    assert report.withheld == (proposer.Withheld(ref, (hazards.CONTACT,)),)
    assert model.requests == [] and report.proposed == () and report.requests == 0
    assert not pseudonyms.mapping_dir(root, P).exists(), "off, nothing is written anywhere"
    assert _proposals(root) == []


# --- the verify criterion ----------------------------------------------------------------


def test_on_connector_text_reaches_the_model_as_tokens_and_comes_back_re_identified(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    mail = _observe(root, GMAIL, source="gmail")
    event = _observe(root, CALENDAR, source="google-calendar")
    model = ScriptedModel(_cites_first_person_token)
    report = proposer.propose(root, P, D, model, environ=ON)

    assert report.withheld == (), "with the layer on, nothing is withheld for contact details"
    shown = [r for r in model.requests if r.kind == "propose"]
    assert shown, "the connector observations were sent"
    for request in shown:
        _assert_nothing_private_in(request.prompt, where="the prompt")
        for _, wrapped in request.items:
            inner = _inner(wrapped)
            assert hazards.flag(inner) == (), inner
            assert re.search(r"\bPERSON_[0-9a-f]{10}\b", inner), "an address became a token"
    texts = "\n".join(_inner(w) for r in shown for _, w in r.items)
    assert re.search(r"Message-ID: MSGID_[0-9a-f]{10}$", texts, re.M)
    assert re.search(r"Call PHONE_[0-9a-f]{10} if", texts)
    assert re.search(r"Location: ADDRESS_[0-9a-f]{10}$", texts, re.M)
    assert re.search(r"account number ACCOUNT_[0-9a-f]{10} by", texts)
    assert "(you, accepted)" in texts and "(accepted), and 2 more" in texts
    assert "Subject: Piano on Thursdays" in texts and "Title: Piano lesson" in texts

    written = _proposals(root)
    assert len(written) == 2, [p.summary for p in written]
    for proposal in written:
        assert TUTOR in proposal.body and TUTOR in proposal.summary, proposal
        assert "PERSON_" not in proposal.body and "PERSON_" not in proposal.summary
    assert {p.caused_by[0] for p in written} == {mail, event}


def test_a_remembered_contact_is_judged_with_tokens_when_on(tmp_path: Path) -> None:
    """The judge is a model call too (D82), so a staged item goes through the same door."""
    root = tmp_path / "root"
    staging.remember(root, P, D, f"Write to {TUTOR} about the recital", summary="Recital mail")
    model = RecordingModel()
    report = proposer.propose(root, P, D, model, environ=ON)
    judged = [r for r in model.requests if r.kind == "judge"]
    assert len(judged) == 1 and report.verdicts == 1
    _assert_nothing_private_in(judged[0].prompt, where="the judge prompt")
    assert "PERSON_" in judged[0].prompt
    (verdict,) = proposer.read_verdicts(root, P)
    assert not verdict.reason.startswith("withheld from the model")

    off = RecordingModel("other")
    proposer.propose(root, P, D, off, environ=OFF)
    assert [r for r in off.requests if r.kind == "judge"] == []
    reasons = [v.reason for v in proposer.read_verdicts(root, P) if v.model == "other"]
    assert reasons == [f"withheld from the model: {hazards.CONTACT}"]


# --- stable tokens -----------------------------------------------------------------------


def test_the_same_address_is_the_same_token_across_runs_a_rebuild_and_two_devices(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _observe(root, GMAIL, source="gmail")
    text = f"From: Alice Tutor <{TUTOR}>\n\nwrite to {TUTOR.upper()} or call {PHONE}"

    first = pseudonyms.Pseudonymiser(root, P, D).pseudonymise(text)
    tokens = sorted(set(pseudonyms.TOKEN.findall(first)))
    assert len(tokens) == 2, first  # one person, spelt two ways, and one phone
    assert first == pseudonyms.Pseudonymiser(root, P, D).pseudonymise(text), "a second run"

    rebuild(root, P)
    index_path(root, P).unlink()
    assert first == pseudonyms.Pseudonymiser(root, P, D).pseudonymise(text), "after a rebuild"

    for path in pseudonyms.mapping_dir(root, P).glob("*.jsonl"):
        path.unlink()  # the mapping is lost; the tokens are not
    assert first == pseudonyms.Pseudonymiser(root, P, D).pseudonymise(text), "mapping gone"

    other = pseudonyms.Pseudonymiser(root, P, "phone")
    assert first == other.pseudonymise(text), "a second device sharing the profile"
    assert other.reidentify(first) == f"From: {TUTOR}\n\nwrite to {TUTOR} or call {PHONE}"
    assert pseudonyms.Pseudonymiser(root, P, D).reidentify(first) == other.reidentify(first)

    elsewhere = pseudonyms.Pseudonymiser(tmp_path / "another-root", P, D).pseudonymise(text)
    assert elsewhere != first, "a different profile has a different key"


def test_two_writers_on_one_device_leave_one_line_and_one_token(tmp_path: Path) -> None:
    """The walkthrough in the module docstring, as a test: A and B both map the same address;
    the mapping holds it once, and both read the same token back."""
    root = tmp_path / "root"
    a = pseudonyms.Pseudonymiser(root, P, D)
    b = pseudonyms.Pseudonymiser(root, P, D)
    text = f"mail {TUTOR} today"
    assert a.pseudonymise(text) == b.pseudonymise(text) == a.pseudonymise(text)
    entries = pseudonyms.read_entries(root, P)
    assert len(entries) == 1 and entries[0].op == "map"
    assert b.reidentify(a.pseudonymise(text)) == text


# --- fail closed -------------------------------------------------------------------------


def test_a_layer_that_misses_an_address_leaves_the_gate_to_withhold_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detector miss is the layer returning the text with an address still in it. The
    hazard scan runs on what the layer produced, so ``contact`` withholds it as before."""
    root = tmp_path / "root"
    ref = _observe(root, GMAIL, source="gmail")
    monkeypatch.setattr(pseudonyms.Pseudonymiser, "pseudonymise", lambda self, text: text)
    model = RecordingModel()
    report = proposer.propose(root, P, D, model, environ=ON)
    assert report.withheld == (proposer.Withheld(ref, (hazards.CONTACT,)),)
    assert model.requests == [] and _proposals(root) == []


def test_the_door_refuses_a_final_text_that_still_carries_a_hazard(tmp_path: Path) -> None:
    """The door checks the exact string the adapter would send. Whatever the pipeline did
    before, an address, a secret or a forged marker in that string stops the call."""
    model = RecordingModel()
    door = pseudonyms.Egress(model, None)
    assert door.name == model.name

    def request(inner: str, *, head: str = proposer.PROPOSE_INSTRUCTIONS) -> proposer.Request:
        marker = "0123456789abcdef"
        wrapped = proposer.envelope(inner, marker)
        return proposer.Request(
            kind="propose",
            marker=marker,
            items=((f"d-{D}:1#1", wrapped),),
            prompt=f"{head}\nexcerpt d-{D}:1#1\n{wrapped}\n",
        )

    clean = request("tool: Title: Piano lesson\nWhen: Thursday 16:00")
    assert json.loads(door.complete(clean)) == []
    assert len(model.requests) == 1

    with pytest.raises(pseudonyms.Refused) as refused:
        door.complete(request(f"tool: From: {TUTOR}"))
    assert refused.value.kinds == (hazards.CONTACT,)
    assert TUTOR not in str(refused.value), "the refusal names the kind, never the text"

    with pytest.raises(pseudonyms.Refused) as refused:
        door.complete(request("tool: the door code is 4821"))
    assert refused.value.kinds == (hazards.SECRET,)

    # A hazard outside every item, in the head itself, is still in the final text.
    with pytest.raises(pseudonyms.Refused) as refused:
        door.complete(request("tool: Title: Piano", head=f"Reach the tutor at {PHONE}.\n"))
    assert refused.value.kinds == (hazards.CONTACT,)

    # An item that is not enveloped under this request's marker was not built by the
    # pipeline, and is refused rather than sent.
    bare = clean.items[0][1].replace(clean.marker, "fedcba9876543210")
    with pytest.raises(pseudonyms.Refused):
        door.complete(
            proposer.Request(
                kind="propose", marker=clean.marker, items=((clean.items[0][0], bare),), prompt=bare
            )
        )
    assert len(model.requests) == 1, "nothing refused reached the model"


def test_the_door_refuses_a_mapped_value_however_it_got_into_the_final_text(
    tmp_path: Path,
) -> None:
    """The layer's own test: a value it has mapped must never be in what it sends, whether
    or not the hazard detectors would have seen it."""
    root = tmp_path / "root"
    layer = pseudonyms.Pseudonymiser(root, P, D)
    layer.pseudonymise(f"Invoice to account number {ACCOUNT}.")
    assert hazards.flag(f"Invoice to account number {ACCOUNT}.") == (), "no detector sees it"
    model = RecordingModel()
    door = pseudonyms.Egress(model, layer)
    marker = "0123456789abcdef"
    wrapped = proposer.envelope(f"tool: pay {ACCOUNT} on Friday", marker)
    with pytest.raises(pseudonyms.Refused) as refused:
        door.complete(
            proposer.Request(
                kind="propose", marker=marker, items=(("x#1", wrapped),), prompt=wrapped
            )
        )
    assert refused.value.kinds == (pseudonyms.LEAK,)
    assert ACCOUNT not in str(refused.value)
    assert model.requests == []


def test_a_door_refusal_in_propose_withholds_the_request_and_sends_nothing_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through ``propose``: when the door refuses, every observation in that request is
    reported withheld, nothing is written, and nothing is marked seen."""
    root = tmp_path / "root"
    ref = _observe(root, GMAIL, source="gmail")
    original = pseudonyms.Pseudonymiser.pseudonymise

    def half_blind(self: pseudonyms.Pseudonymiser, text: str) -> str:
        return original(self, text).replace("Thursday", f"Thursday ({PHONE})")

    monkeypatch.setattr(pseudonyms.Pseudonymiser, "pseudonymise", half_blind)

    def unscanned(
        ref: str, body: str, layer: pseudonyms.Pseudonymiser | None
    ) -> tuple[tuple[str, ...], list[tuple[str, str]]]:
        # The per-excerpt scan would catch this too; take it out of the picture so the door
        # is the one that refuses.
        text = layer.pseudonymise(body) if layer else body
        return (), proposer.excerpts(ref, proposer.labelled(text))

    monkeypatch.setattr(proposer, "_scanned", unscanned)
    model = RecordingModel()
    report = proposer.propose(root, P, D, model, environ=ON)
    # Two findings: a phone shape in the final text, and a value the mapping holds (the same
    # number, mapped when the body was pseudonymised). Either alone would have refused.
    kinds = (hazards.CONTACT, pseudonyms.LEAK)
    assert report.withheld == (proposer.Withheld(ref, kinds),)
    assert model.requests == [] and report.requests == 0
    assert proposer.read_seen(root, P) == ()
    assert _proposals(root) == []


def test_an_excerpt_id_from_a_long_device_name_is_not_a_credential_to_the_door(
    tmp_path: Path,
) -> None:
    """Found on the stranger pass: the door scans what is left of the prompt around the
    items, and an excerpt id carries the device name, which a long one with a digit in it
    reads like a key. Ids are refs this build made, never source text; they are left out."""
    root = tmp_path / "root"
    device = "mbp-2024-work-laptop-xyz"
    body = "user: Always keep Thursday evenings free.\nassistant: Noted."
    assert hazards.flag(f"excerpt d-{device}:1#1") == (hazards.CREDENTIAL,), "the shape at issue"
    _observe(root, body, source="transcript", device=device)
    for environ in (OFF, ON):
        model = RecordingModel(f"m-{len(environ)}")
        report = proposer.propose(root, P, device, model, environ=environ)
        assert report.withheld == () and len(model.requests) >= 1, environ


# --- the mapping stays on the machine ----------------------------------------------------


def test_the_mapping_is_never_in_a_model_call_a_report_a_log_line_or_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _observe(root, GMAIL, source="gmail")
    _observe(root, CALENDAR, source="google-calendar")
    staging.remember(root, P, D, f"Write to {TUTOR} about the recital", summary="Recital mail")
    model = ScriptedModel(_cites_first_person_token)
    report = proposer.propose(root, P, D, model, environ=ON)
    layer = pseudonyms.Pseudonymiser(root, P, D)
    entries = pseudonyms.read_entries(root, P)
    assert entries, "the run mapped something"
    tokens = {e.token for e in entries}

    for request in model.requests:  # a model call
        _assert_nothing_private_in(request.prompt, where="a model call")
    _assert_nothing_private_in(repr(report), where="the report")  # a report
    assert repr(layer) == f"Pseudonymiser(principal={P!r}, device={D!r})"  # the layer's repr
    for path in log_dir(root, P).glob("*.jsonl"):  # a log line: no token, so no pair
        for line in path.read_text().splitlines():
            assert not pseudonyms.TOKEN.search(line), line
            assert not any(tok in line for tok in tokens)
    assert pseudonyms.mapping_dir(root, P).parent == principal_dir(root, P)
    assert not pseudonyms.mapping_dir(root, P).is_relative_to(log_dir(root, P))
    for shadow in (proposer.seen_path(root, P), proposer.verdicts_path(root, P)):
        if shadow.is_file():
            _assert_nothing_private_in(shadow.read_text(), where=shadow.name)

    # The terminal: the same run through the command, from a fresh log.
    other = tmp_path / "other"
    _observe(other, GMAIL, source="gmail")
    _observe(other, CALENDAR, source="google-calendar")
    result = CliRunner().invoke(
        main,
        ["propose", "--root", str(other), "--principal", P, "--device", D, "--model", "fake"],
        env=ON,
    )
    assert result.exit_code == 0, result.output
    _assert_nothing_private_in(result.stdout + result.stderr, where="the terminal")
    assert "PERSON_" not in result.stdout + result.stderr
    assert "withheld" not in result.stdout, result.stdout


def test_a_proposal_from_the_fixture_log_is_unchanged_by_the_layer(tmp_path: Path) -> None:
    """The layer touches text with something to replace in it and nothing else: a transcript
    with no address goes out as it did, byte for byte."""
    root = tmp_path / "root"
    body = "user: I wrote the newsletter today, dates day first.\nassistant: Day first, noted."
    _observe(root, body, source="transcript")
    on, off = RecordingModel("on"), RecordingModel("off")
    assert proposer.propose(root, P, D, on, environ=ON).proposed == ()  # so "off" sees it too
    proposer.propose(root, P, D, off, environ=OFF)
    assert len(on.requests) == len(off.requests) == 1
    assert [r.items[0][1].split("\n")[1:-1] for r in on.requests] == [
        r.items[0][1].split("\n")[1:-1] for r in off.requests
    ]
    assert pseudonyms.read_entries(root, P) == ()


# --- one door ----------------------------------------------------------------------------


def test_every_model_call_in_the_proposer_goes_through_the_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted at run time, and read in the source: the proposer never calls a model's
    ``complete`` itself, only the door's."""
    root = tmp_path / "root"
    _observe(root, GMAIL, source="gmail")
    _observe(root, "user: Always keep Thursday free.\nassistant: Noted.", source="transcript")
    staging.remember(root, P, D, "Keep Thursday free", summary="Thursday")
    through: list[proposer.Request] = []
    original = pseudonyms.Egress.complete

    def spy(self: pseudonyms.Egress, request: proposer.Request) -> str:
        through.append(request)
        return original(self, request)

    monkeypatch.setattr(pseudonyms.Egress, "complete", spy)
    model = RecordingModel()
    report = proposer.propose(root, P, D, model, environ=ON)
    assert report.requests >= 2 and len(model.requests) == report.requests
    assert through == model.requests

    tree = ast.parse((ROOT / "src" / "walle" / "proposer.py").read_text())
    callers = {
        ast.unparse(node.func.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "complete"
    }
    assert callers == {"door"}, callers


# --- shredding (D12) ---------------------------------------------------------------------


def test_a_shredded_entry_is_opaque_everywhere(tmp_path: Path) -> None:
    root = tmp_path / "root"
    a = pseudonyms.Pseudonymiser(root, P, D)
    text = f"From: {TUTOR}\nCc: {PAT}"
    out = a.pseudonymise(text)
    tutor_token, _pat_token = pseudonyms.TOKEN.findall(out)
    assert a.reidentify(out) == text

    a.shred(tutor_token)
    opaque = a.reidentify(out)
    assert tutor_token in opaque and PAT in opaque and TUTOR not in opaque
    b = pseudonyms.Pseudonymiser(root, P, "phone")
    assert b.reidentify(out) == opaque, "the other device sees the same"

    # Seen again, the address still becomes the same token (the frontier can still
    # correlate), and the mapping gains no new line that would re-identify it.
    before = pseudonyms.read_entries(root, P)
    assert a.pseudonymise(text) == out and b.pseudonymise(text) == out
    assert pseudonyms.read_entries(root, P) == before
    assert [e.op for e in before] == ["map", "map", "shred"]
    assert a.reidentify(out) == opaque

    with pytest.raises(ValueError):
        a.shred("not-a-token")


# --- the mapping file --------------------------------------------------------------------


def test_the_mapping_is_private_per_device_and_beside_the_log(tmp_path: Path) -> None:
    root = tmp_path / "root"
    pseudonyms.Pseudonymiser(root, P, D).pseudonymise(f"call {PHONE}")
    other = pseudonyms.Pseudonymiser(root, P, "phone")
    other.pseudonymise(f"call {PHONE}")  # already mapped: the other device writes nothing
    assert not (pseudonyms.mapping_dir(root, P) / "d-phone.jsonl").exists()
    other.pseudonymise(f"write to {PAT}")  # new: its own stream, never the other device's
    folder = pseudonyms.mapping_dir(root, P)
    assert folder == principal_dir(root, P) / "pseudonyms"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["d-mbp.jsonl", "d-phone.jsonl", "key"]
    for name in files:
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name
    lines = [json.loads(line) for line in (folder / "d-mbp.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["v"] == 1 and lines[0]["op"] == "map"
    assert set(lines[0]) == {"v", "ts", "op", "token", "value"}


def test_a_torn_mapping_tail_is_passed_over_and_the_next_append_starts_fresh(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    layer = pseudonyms.Pseudonymiser(root, P, D)
    out = layer.pseudonymise(f"call {PHONE}")
    path = pseudonyms.mapping_dir(root, P) / f"d-{D}.jsonl"
    whole = path.read_bytes()
    path.write_bytes(whole + whole[:-20])  # a second line, torn mid-way
    assert len(pseudonyms.read_entries(root, P)) == 1
    layer.pseudonymise(f"write to {PAT}")
    data = path.read_bytes()
    assert data.startswith(whole + whole[:-20] + b"\n")
    entries = pseudonyms.read_entries(root, P)
    assert len(entries) == 2 and layer.reidentify(out) == f"call {PHONE}"
    pat = layer.pseudonymise(PAT)
    assert layer.reidentify(pat) == PAT


def test_v1_mapping_lines_still_load(tmp_path: Path) -> None:
    """A golden mapping (D88, D96): written by this build, frozen, never regenerated. A later
    build must read it and re-identify the same tokens."""
    root = tmp_path / "root"
    expected = json.loads((FIXTURE / "__expected__.json").read_text())
    folder = pseudonyms.mapping_dir(root, P)
    folder.mkdir(parents=True)
    for name in ("key", "d-mbp.jsonl", "d-phone.jsonl"):
        (folder / name).write_bytes((FIXTURE / name).read_bytes())
    layer = pseudonyms.Pseudonymiser(root, P, D)
    for token, value in expected["reidentify"].items():
        assert layer.reidentify(token) == value, token
    for text, token in expected["tokens"].items():
        assert layer.pseudonymise(text) == token, text
    assert {e.v for e in pseudonyms.read_entries(root, P)} == {1}


# --- what the layer replaces -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        (f"From: Alice Tutor <{TUTOR}>", r"^From: PERSON_[0-9a-f]{10}$"),
        (f"From: {TUTOR}", r"^From: PERSON_[0-9a-f]{10}$"),
        (f'From: "Tutor, Alice" <{TUTOR}>', r"^From: PERSON_[0-9a-f]{10}$"),
        (
            f"To: Sam Example <{ME}>, Pat Example <{PAT}>",
            r"^To: PERSON_[0-9a-f]{10}, PERSON_[0-9a-f]{10}$",
        ),
        (f"Cc: {BOOKINGS}, {PAT}", r"^Cc: PERSON_[0-9a-f]{10}, PERSON_[0-9a-f]{10}$"),
        (f"Organizer: Alice Tutor <{TUTOR}> (you)", r"^Organizer: PERSON_[0-9a-f]{10} \(you\)$"),
        (
            f"Attendees: Sam Example <{ME}> (you, accepted), Pat Example <{PAT}>, and 5 more",
            r"^Attendees: PERSON_[0-9a-f]{10} \(you, accepted\), PERSON_[0-9a-f]{10}, and 5 more$",
        ),
        (f"Message-ID: {MSGID}", r"^Message-ID: MSGID_[0-9a-f]{10}$"),
        (f"Ring {PHONE} or +1 555 010 1234.", r"^Ring PHONE_[0-9a-f]{10} or PHONE_[0-9a-f]{10}\.$"),
        (f"Meet at {STREET}, 4pm", r"^Meet at ADDRESS_[0-9a-f]{10}, 4pm$"),
        (f"account number {ACCOUNT} by Friday", r"^account number ACCOUNT_[0-9a-f]{10} by Friday$"),
        ("IBAN GB82 WEST 1234 5698 7654 32 please", r"^IBAN ACCOUNT_[0-9a-f]{10} please$"),
        ("Acct: 0012 3456 7890", r"^Acct: ACCOUNT_[0-9a-f]{10}$"),
    ],
)
def test_structured_headers_and_text_shapes_become_tokens(
    tmp_path: Path, text: str, shape: str
) -> None:
    layer = pseudonyms.Pseudonymiser(tmp_path / "root", P, D)
    out = layer.pseudonymise(text)
    assert re.fullmatch(shape, out), out
    assert hazards.CONTACT not in hazards.flag(out)
    for name in NAMES:
        assert name not in out


@pytest.mark.parametrize(
    "text",
    [
        "Every Thursday I coach the under-nines at the park.",
        "The meeting is at 16:00 on 2026-11-05, room 3.",
        "Grade 3 book, pages 12 to 18, 24 hours before.",
        "Thanks, Alice",
    ],
)
def test_plain_text_is_left_alone(tmp_path: Path, text: str) -> None:
    layer = pseudonyms.Pseudonymiser(tmp_path / "root", P, D)
    assert layer.pseudonymise(text) == text
    assert not pseudonyms.mapping_dir(tmp_path / "root", P).exists()


def test_a_token_never_trips_the_hazard_gate() -> None:
    """Whatever the gate does with the text around it, a token itself is clean: no kind of
    hazard matches one, so a tokenised address cannot withhold what it replaced."""
    for kind in pseudonyms.KINDS:
        token = f"{kind}_0123456789"
        assert pseudonyms.TOKEN.fullmatch(token)
        assert hazards.flag(f"tool: From: {token}\nTo: {token}\nnote {token} and {token}.") == ()
