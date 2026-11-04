"""Task 19-21a's verify criterion: the eval corpora, part 1 (D77, D82).

  one schema           every item in every corpus loads against ``Item`` and nothing else
  expected verdict     benign items expect allow, dangerous expect flag, injection expect refuse
  deterministic        regenerating gives byte-identical files, in this process or a fresh one
  synthetic            reserved e-mail and web domains, fictional phone numbers, credentials that
                       say FAKE, and nothing shaped like a real provider's secret

The corpora are data. The injection corpus is full of instructions on purpose, and none of them
is addressed to whoever reads this repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from corpora.generate import (
    CORPORA,
    FAKE_SECRET,
    HAZARDS,
    VERDICT,
    Item,
    build,
    check,
    load,
    render_all,
    write,
)

HERE = Path(__file__).parent / "corpora"
SCRIPT = HERE / "generate.py"


def all_items() -> list[Item]:
    return [item for corpus in CORPORA for item in load(HERE / f"{corpus}.jsonl")]


def all_text() -> str:
    return "\n".join(turn.text for item in all_items() for turn in item.turns)


def sample(corpus: str = "dangerous") -> dict[str, object]:
    """One valid item as a plain dict, for the tests that break it."""
    return json.loads((HERE / f"{corpus}.jsonl").read_text().splitlines()[0])


# --- one schema, expected verdict -------------------------------------------------------------


@pytest.mark.parametrize("corpus", CORPORA)
def test_every_item_loads_against_one_schema_and_carries_its_expected_verdict(
    corpus: str,
) -> None:
    items = load(HERE / f"{corpus}.jsonl")
    assert len(items) >= 12
    for item in items:
        assert type(item) is Item
        assert item.corpus == corpus
        assert item.expected == VERDICT[corpus]
        assert item.id.startswith(f"{corpus}-")


def test_the_three_corpora_are_the_ones_d77_names() -> None:
    assert CORPORA == ("benign", "dangerous", "injection")
    assert VERDICT == {"benign": "allow", "dangerous": "flag", "injection": "refuse"}
    assert sorted(p.name for p in HERE.glob("*.jsonl")) == [f"{c}.jsonl" for c in sorted(CORPORA)]


@pytest.mark.parametrize("corpus", CORPORA)
def test_each_corpus_covers_every_hazard_it_is_meant_to(corpus: str) -> None:
    seen = {item.hazard for item in load(HERE / f"{corpus}.jsonl")}
    assert seen == set(HAZARDS[corpus])


def test_ids_are_unique_across_all_corpora() -> None:
    ids = [item.id for item in all_items()]
    assert len(ids) == len(set(ids))


def test_every_marker_is_in_the_transcript_and_only_benign_items_have_none() -> None:
    for item in all_items():
        assert (item.markers == []) == (item.corpus == "benign"), item.id
        for marker in item.markers:
            assert any(marker in turn.text for turn in item.turns), (item.id, marker)


def test_benign_holds_near_misses_so_a_keyword_filter_cannot_pass_it() -> None:
    """D77's benign gate is 100% allowed; words like these must not be enough to refuse."""
    text = "\n".join(t.text for i in load(HERE / "benign.jsonl") for t in i.turns).lower()
    for word in ("password", "ignore", "approved", "rule", "phone"):
        assert word in text, word


def test_injection_forges_the_d55_envelope_marker() -> None:
    forged = [
        marker
        for item in load(HERE / "injection.jsonl")
        if item.hazard == "forged-envelope"
        for marker in item.markers
        if re.fullmatch(r"<<</?untrusted:[0-9a-f]{16}>>>", marker)
    ]
    assert forged


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"surprise": 1}, "extra field"),
        ({"expected": "allow"}, "dangerous item expecting allow"),
        ({"corpus": "benign"}, "corpus and verdict disagree"),
        ({"hazard": "forged-envelope"}, "hazard from another corpus"),
        ({"markers": ["not in any turn"]}, "marker absent from the transcript"),
        ({"markers": ["Here is the key"]}, "a credential that does not say FAKE"),
        ({"markers": []}, "dangerous item with nothing to flag"),
        ({"turns": []}, "no turns"),
        ({"synthetic": False}, "not synthetic"),
        ({"v": 2}, "unknown version"),
    ],
)
def test_the_schema_rejects_an_item_that_breaks_it(change: dict[str, object], reason: str) -> None:
    good = sample()
    Item.model_validate(good)
    with pytest.raises(ValidationError):
        Item.model_validate(good | change)


def test_load_names_the_line_it_cannot_read(tmp_path: Path) -> None:
    lines = (HERE / "benign.jsonl").read_text().splitlines()
    lines[2] = lines[2].replace('"expected":"allow"', '"expected":"flag"')
    bad = tmp_path / "benign.jsonl"
    bad.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match=r"benign\.jsonl:3"):
        load(bad)


def test_a_marker_must_sit_inside_one_turn_not_across_two() -> None:
    good = sample()
    turns = [{"role": "user", "text": "split FAKE-"}, {"role": "user", "text": "here"}]
    with pytest.raises(ValidationError, match="not in any turn"):
        Item.model_validate(good | {"turns": turns, "markers": ["FAKE-\nhere"]})


@pytest.mark.parametrize(
    ("data", "message"),
    [(b"", "is empty"), (b"\xff\n", "not UTF-8"), (b"{}", "does not end in a newline")],
)
def test_load_says_what_is_wrong_with_a_bad_file(tmp_path: Path, data: bytes, message: str) -> None:
    bad = tmp_path / "benign.jsonl"
    bad.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        load(bad)


def test_load_refuses_items_filed_under_the_wrong_corpus(tmp_path: Path) -> None:
    moved = tmp_path / "benign.jsonl"
    moved.write_bytes((HERE / "dangerous.jsonl").read_bytes())
    with pytest.raises(ValueError, match="dangerous"):
        load(moved)


# --- deterministic ---------------------------------------------------------------------------


def test_regenerating_produces_byte_identical_files(tmp_path: Path) -> None:
    write(tmp_path)
    for corpus in CORPORA:
        name = f"{corpus}.jsonl"
        assert (tmp_path / name).read_bytes() == (HERE / name).read_bytes(), name
    assert render_all() == render_all()
    assert check(HERE) == []


def test_a_fresh_process_with_another_hash_seed_writes_the_same_bytes(tmp_path: Path) -> None:
    outputs = []
    for seed in ("1", "2"):
        out = tmp_path / seed
        env = os.environ | {"PYTHONHASHSEED": seed}
        subprocess.run(
            [sys.executable, str(SCRIPT), "--out", str(out)], check=True, env=env, timeout=60
        )
        outputs.append({p.name: p.read_bytes() for p in sorted(out.glob("*.jsonl"))})
    assert outputs[0] == outputs[1]
    assert outputs[0] == {f"{c}.jsonl": (HERE / f"{c}.jsonl").read_bytes() for c in CORPORA}


def test_check_mode_fails_on_a_drifted_or_missing_file(tmp_path: Path) -> None:
    write(tmp_path)
    assert (
        subprocess.run(
            [sys.executable, str(SCRIPT), "--check", "--out", str(tmp_path)], timeout=60
        ).returncode
        == 0
    )
    with (tmp_path / "injection.jsonl").open("a") as f:
        f.write("\n")
    (tmp_path / "benign.jsonl").unlink()
    assert check(tmp_path) == ["benign.jsonl", "injection.jsonl"]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "benign.jsonl" in result.stderr
    assert "injection.jsonl" in result.stderr


def test_write_leaves_readable_files_and_no_temporaries(tmp_path: Path) -> None:
    write(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{c}.jsonl" for c in CORPORA]
    for path in tmp_path.iterdir():
        assert path.stat().st_mode & 0o777 == 0o644, path.name


def test_build_is_a_pure_function() -> None:
    assert build() == build()


# --- synthetic: no real personal data --------------------------------------------------------

RESERVED_DOMAINS = {"example.com", "example.org", "example.net"}


def reserved(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in RESERVED_DOMAINS)


def test_every_email_and_url_uses_a_reserved_domain() -> None:
    """RFC 2606 reserves example.com, .org and .net; nobody can own an address there."""
    text = all_text()
    emails = re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)", text)
    hosts = re.findall(r"(?:https?://|www\.)([A-Za-z0-9.-]+)", text)
    assert emails and hosts
    assert [h for h in emails + hosts if not reserved(h)] == []


def test_every_phone_number_is_in_the_fictional_555_01_range() -> None:
    """555-0100 to 555-0199 are set aside for fiction. No other run of 7+ digits appears."""
    runs = re.findall(r"(?<![\w-])\d(?:[ .-]?\d){6,}(?![\w])", all_text())
    assert runs
    assert [r for r in runs if not re.fullmatch(r"555-01\d\d", r)] == []


def test_every_credential_says_it_is_fake() -> None:
    for item in all_items():
        if item.hazard in ("credential", "secret"):
            assert item.markers, item.id
            for marker in item.markers:
                assert re.fullmatch(FAKE_SECRET, marker), (item.id, marker)
    for token in re.findall(r"FAKE-[\w-]*", all_text()):
        assert re.fullmatch(FAKE_SECRET, token), token


REAL_SECRET_SHAPES = {
    "aws access key": r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    "github token": r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_",
    "gitlab token": r"glpat-",
    "openai or anthropic key": r"sk-[A-Za-z0-9_-]{16,}",
    "stripe key": r"[sr]k_(?:live|test)_",
    "slack token or hook": r"xox[abprs]-|hooks\.slack\.com",
    "google api key": r"AIza[0-9A-Za-z_-]{20,}",
    "npm token": r"npm_[A-Za-z0-9]{20,}",
    "private key block": r"-----BEGIN",
    "jwt": r"eyJ[A-Za-z0-9_-]{8,}\.",
    "real home directory": r"/Users/|C:\\Users|/home/(?!example\b)",
    "us social security number": r"\b\d{3}-\d{2}-\d{4}\b",
    "ipv4 address": r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
}


@pytest.mark.parametrize("shape", sorted(REAL_SECRET_SHAPES))
def test_nothing_is_shaped_like_a_real_secret_or_identifier(shape: str) -> None:
    raw = "\n".join((HERE / f"{c}.jsonl").read_text() for c in CORPORA)
    assert re.findall(REAL_SECRET_SHAPES[shape], raw) == []


def test_the_shape_checks_catch_what_they_are_for() -> None:
    """A planted fake of each shape is caught, or the tests above prove nothing."""
    planted = {
        "aws access key": "AKIA" + "Q" * 16,
        "github token": "ghp_" + "a" * 36,
        "gitlab token": "glpat-" + "a" * 20,
        "openai or anthropic key": "sk-" + "a" * 32,
        "stripe key": "sk_" + "live_" + "a" * 24,
        "slack token or hook": "xox" + "b-1",
        "google api key": "AIza" + "a" * 35,
        "npm token": "npm_" + "a" * 36,
        "private key block": "-" * 5 + "BEGIN",
        "jwt": "eyJ" + "hbGciOiJI" + ".x",
        "real home directory": "/home/" + "someone/notes",
        "us social security number": "123-45-" + "6789",
        "ipv4 address": "10.0.0." + "1",
    }
    assert set(planted) == set(REAL_SECRET_SHAPES)
    for shape, text in planted.items():
        assert re.findall(REAL_SECRET_SHAPES[shape], text), shape
