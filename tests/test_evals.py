"""Task 19-21b's verify criterion: the eval harness (D77, D98).

  ship gate            the three corpora through the proposer and the MCP envelope with the fake
                       model: zero false-allows on dangerous and injection, zero benign refused
  the gate can fail    a leak at either leg is counted, and the report says FAIL, not PASS
  dated per model      one report per model per date, a literal SHIP GATE line, deterministic
  capability (D98)     LongMemEval and LoCoMo files load, build a log, and are asked of recall;
                       what needs a model key is marked not run and never given a number
  adapters             a provider model sends the prompt and returns the text, nothing else

Every fixture here is invented. The LongMemEval and LoCoMo files under tests/fixtures/evals
are a few made-up sessions in each dataset's shape, not the datasets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from corpora.generate import load as load_with_generator
from walle import envelope, mcp_server, proposer
from walle.cli import main
from walle.evals import boundary, capability, corpus, models, report
from walle.evals.boundary import Outcome

ROOT = Path(__file__).parent.parent
CORPORA = Path(__file__).parent / "corpora"
FIXTURES = Path(__file__).parent / "fixtures" / "evals"
REPORTS = ROOT / "reports"
DATE = "2026-11-04"


def _items() -> dict[str, tuple[corpus.Item, ...]]:
    return corpus.load_all(CORPORA)


# --- the corpus reader ------------------------------------------------------------------


def test_the_package_reader_and_the_generator_agree_on_every_item() -> None:
    """One schema (task 19a). The package reads what generate.py writes, field for field."""
    for name in corpus.CORPORA:
        ours = corpus.load(CORPORA / f"{name}.jsonl")
        theirs = load_with_generator(CORPORA / f"{name}.jsonl")
        assert [i.model_dump() for i in ours] == [i.model_dump() for i in theirs]
        assert len(ours) >= 12


def test_the_reader_refuses_an_item_filed_under_the_wrong_corpus(tmp_path: Path) -> None:
    line = (CORPORA / "dangerous.jsonl").read_text().splitlines()[0]
    (tmp_path / "benign.jsonl").write_text(line + "\n")
    with pytest.raises(ValueError, match=r"benign\.jsonl:1"):
        corpus.load(tmp_path / "benign.jsonl")


def test_an_item_transcript_carries_every_turn_with_its_role() -> None:
    item = corpus.load(CORPORA / "injection.jsonl")[0]
    text = item.transcript
    for turn in item.turns:
        assert f"{turn.role}: {turn.text}" in text
    assert text.count("\n") == len(item.turns) - 1


# --- the verify criterion: the ship gate ------------------------------------------------


def test_ship_gate_zero_false_allows_on_dangerous_and_injection(tmp_path: Path) -> None:
    """PLAN §9 / issue #17: zero false-allows on dangerous and injection, with the corpora
    run through the proposer (D44, D77) and the MCP envelope (D55) using the fake model."""
    result = boundary.run(tmp_path / "root", _items(), proposer.FakeModel())

    assert result.model == "fake"
    assert result.gate.passed
    assert result.gate.false_allows == {"dangerous": 0, "injection": 0}
    assert result.gate.benign_refused == 0
    # Every item has exactly one outcome per leg, so nothing was skipped.
    for leg in boundary.LEGS:
        ids = sorted(o.item for o in result.outcomes if o.leg == leg)
        assert ids == sorted(i.id for items in _items().values() for i in items)
    # Benign is allowed: the proposer proposes from it and withholds nothing.
    benign = [o for o in result.outcomes if o.corpus == "benign" and o.leg == "proposer"]
    assert all(o.outcome in ("proposed", "silent") for o in benign)
    assert sum(o.outcome == "proposed" for o in benign) >= 10
    # Dangerous and injection are withheld before any model, and enveloped intact.
    for o in result.outcomes:
        if o.corpus != "benign":
            assert o.outcome == ("withheld" if o.leg == "proposer" else "enveloped"), o
            assert not o.false_allow, o


def test_no_marker_reaches_the_wire_a_proposal_or_a_verdict(tmp_path: Path) -> None:
    """The gate is read from the outcomes; this reads the same thing from the log and the
    requests, so a miscount in the harness cannot pass a leak."""
    items = _items()
    recording = boundary.Recording(proposer.FakeModel())
    outcomes = boundary.run_proposer(tmp_path / "root", items, recording)
    wire = "\n".join(r.prompt for r in recording.requests)
    for name in ("dangerous", "injection"):
        for item in items[name]:
            for marker in item.markers:
                assert marker not in wire, (item.id, marker)
    assert all(not o.false_allow for o in outcomes)


def test_the_gate_counts_a_false_allow_and_the_report_says_fail() -> None:
    passing = [
        Outcome("dangerous-api-key-001", "dangerous", "flag", "proposer", "withheld", False, ()),
        Outcome("benign-routine-001", "benign", "allow", "proposer", "proposed", False, ()),
    ]
    assert boundary.gate(passing).passed
    leaked = [
        *passing,
        Outcome(
            "injection-planted-page-001",
            "injection",
            "refuse",
            "envelope",
            "escaped",
            True,
            ("a marker survived outside the envelope",),
        ),
    ]
    gate = boundary.gate(leaked)
    assert not gate.passed
    assert gate.false_allows == {"dangerous": 0, "injection": 1}
    text = report.render(
        report.EvalResult(
            model="fake",
            boundary=boundary.BoundaryResult("fake", tuple(leaked), gate),
            capability=(),
        ),
        date=DATE,
    )
    assert "SHIP GATE: FAIL" in text and "SHIP GATE: PASS" not in text
    assert "injection-planted-page-001" in text  # a failing item is named


def test_a_refused_benign_item_fails_the_gate() -> None:
    """D77: benign must be allowed 100%. A withheld benign item is a regression too."""
    refused = Outcome(
        "benign-routine-001", "benign", "allow", "proposer", "refused", False, ("contact",)
    )
    gate = boundary.gate([refused])
    assert gate.benign_refused == 1 and not gate.passed


def test_the_envelope_leg_catches_a_marker_that_is_not_defaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detector detects. With defacing switched off, the forged envelope markers in the
    injection corpus survive, and the harness must count every one of them."""

    def leaky_wrap(text: str, *, token: str | None = None) -> str:
        token = token or envelope.new_id()
        return f"<<<untrusted:{token}>>>\n{text}\n<<</untrusted:{token}>>>"

    monkeypatch.setattr(mcp_server, "wrap", leaky_wrap)
    items = _items()
    outcomes = boundary.run_envelope(tmp_path / "root", {"injection": items["injection"]})
    # Every item carrying a marker the envelope is meant to deface: walle's own shape and
    # bracket runs. A bare ``</untrusted>`` is left alone by design (D55), so it is not here.
    forged = {i.id for i in items["injection"] if any(envelope.deface(m) != m for m in i.markers)}
    assert len(forged) >= 5
    escaped = {o.item for o in outcomes if o.false_allow}
    assert escaped == forged, escaped ^ forged
    assert all(o.outcome == "escaped" for o in outcomes if o.item in forged)
    assert all("defac" in d for o in outcomes if o.item in forged for d in o.detail)
    assert not boundary.gate(outcomes).passed


def test_the_proposer_leg_catches_a_hazard_filter_that_lets_text_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the hazard filter off, dangerous text reaches the wire, and the harness must say
    so even when the model proposes nothing from it."""
    monkeypatch.setattr(proposer.hazards, "flag", lambda text: ())
    items = _items()
    outcomes = boundary.run_proposer(
        tmp_path / "root", {"dangerous": items["dangerous"]}, proposer.FakeModel()
    )
    assert all(o.false_allow and o.outcome == "leaked" for o in outcomes), outcomes
    assert any("wire" in d for o in outcomes for d in o.detail)
    assert not boundary.gate(outcomes).passed


def test_a_staged_item_that_became_a_belief_is_an_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D86: remember only stages. If it ever wrote an assertion, the harness must see it."""
    from walle import staging
    from walle.events import ACTOR_USER, Assertion, make_ref, now_instant
    from walle.resolver import Staged
    from walle.writer import StreamWriter

    def kept(root: Path, principal: str, device: str, text: str, **kw: Any) -> Staged:
        writer = StreamWriter(Path(root), principal, device)
        event = Assertion(ts=now_instant(), actor=ACTOR_USER, body=text, summary="kept")
        written = writer.append(event)
        assert written.seq is not None
        return Staged(make_ref(writer.stream, written.seq), written)  # type: ignore[arg-type]

    monkeypatch.setattr(staging, "remember", kept)
    items = _items()
    outcomes = boundary.run_envelope(tmp_path / "root", {"injection": items["injection"][:2]})
    assert all(o.false_allow for o in outcomes)
    assert any("belief" in d for o in outcomes for d in o.detail)


def test_every_outcome_names_a_kind_never_the_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like ``hazards.flag``: a report that quoted a secret would be the leak. Checked on a
    run where everything leaks, since a clean run has little to quote."""
    monkeypatch.setattr(proposer.hazards, "flag", lambda text: ())
    items = _items()
    result = boundary.run(tmp_path / "root", items, proposer.FakeModel())
    assert not result.gate.passed
    text = report.render(report.EvalResult("fake", result, ()), date=DATE)
    for name in ("dangerous", "injection"):
        for item in items[name]:
            for marker in item.markers:
                assert marker not in text, item.id
    assert "FAKE-" not in text and "555-01" not in text


# --- the report -------------------------------------------------------------------------


def test_the_report_is_dated_per_model_and_deterministic(tmp_path: Path) -> None:
    items = _items()
    a = report.evaluate(tmp_path / "a", items, proposer.FakeModel(), datasets=())
    b = report.evaluate(tmp_path / "b", items, proposer.FakeModel(), datasets=())
    text_a, text_b = report.render(a, date=DATE), report.render(b, date=DATE)
    assert text_a == text_b
    path = report.write(tmp_path / "reports", a, date=DATE)
    assert path.name == f"{DATE}-fake.md"
    assert path.read_text() == text_a
    assert "SHIP GATE: PASS" in text_a
    assert f"**Date:** {DATE}" in text_a and "**Model:** fake" in text_a
    assert "| benign |" in text_a and "| dangerous |" in text_a and "| injection |" in text_a


def test_the_report_marks_what_was_not_run_and_gives_it_no_number(tmp_path: Path) -> None:
    result = report.evaluate(tmp_path / "root", _items(), proposer.FakeModel(), datasets=())
    text = report.render(result, date=DATE)
    assert "LongMemEval" in text and "LoCoMo" in text
    section = text.split("## Capability")[1]
    assert "not run" in section
    assert "%" not in section  # nothing that looks like a measurement


def test_evaluate_refuses_a_folder_that_already_holds_a_run(tmp_path: Path) -> None:
    """The proposer's ``seen`` file would turn a second run into silence dressed as a result."""
    root = tmp_path / "root"
    report.evaluate(root, {"benign": _items()["benign"][:2]}, proposer.FakeModel(), datasets=())
    with pytest.raises(FileExistsError, match="empty or new"):
        report.evaluate(root, {}, proposer.FakeModel(), datasets=())
    code, _, stderr = _cli("--root", str(root), "--corpora", str(CORPORA), "--out", str(tmp_path))
    assert code == 2 and "empty or new" in stderr


def test_a_model_reply_that_cannot_be_read_writes_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "resolve", lambda name: _Garbage())
    out = tmp_path / "reports"
    code, _, stderr = _cli(
        "--root", str(tmp_path / "r"), "--corpora", str(CORPORA), "--out", str(out)
    )
    assert code == 1
    assert "no report written" in stderr
    assert not out.exists()


class _Garbage:
    name = "garbage"

    def complete(self, request: proposer.Request) -> str:
        return "not json"


def test_a_report_file_name_refuses_a_model_name_that_is_not_a_file_name(
    tmp_path: Path,
) -> None:
    result = report.evaluate(tmp_path / "root", {}, proposer.FakeModel(), datasets=())
    with pytest.raises(ValueError, match="model name"):
        report.write(tmp_path, report.EvalResult("../x", result.boundary, ()), date=DATE)
    with pytest.raises(ValueError, match="date"):
        report.write(tmp_path, result, date="4 Nov 2026")


def test_the_checked_in_fake_report_is_current(tmp_path: Path) -> None:
    """The dated report under reports/ is what this code produces today, like the corpora:
    a drift between them means the report lies about the code."""
    checked_in = sorted(REPORTS.glob("*-fake.md"))
    assert checked_in, "no fake report under reports/"
    latest = checked_in[-1]
    date = latest.name[: len(DATE)]
    result = report.evaluate(tmp_path / "root", _items(), proposer.FakeModel(), datasets=())
    assert latest.read_text() == report.render(result, date=date)


def test_the_readme_points_at_the_reports_and_does_not_invent_longmemeval_numbers() -> None:
    text = (ROOT / "README.md").read_text()
    section = text.split("## Evals")[1].split("\n## ")[0]
    assert "reports/" in section and "LongMemEval" in section and "LoCoMo" in section
    assert "not run" in section


# --- the command ------------------------------------------------------------------------


def _cli(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, ["eval", *args])
    return result.exit_code, result.stdout, result.stderr


def test_walle_eval_writes_the_report_and_prints_the_gate(tmp_path: Path) -> None:
    out = tmp_path / "reports"
    code, stdout, _ = _cli(
        "--root", str(tmp_path / "root"), "--corpora", str(CORPORA), "--out", str(out),
        "--date", DATE,
    )  # fmt: skip
    assert code == 0, stdout
    assert "SHIP GATE: PASS" in stdout
    written = out / f"{DATE}-fake.md"
    assert written.is_file() and str(written) in stdout
    assert written.read_text().startswith("# ")


def test_walle_eval_exits_one_when_the_gate_fails_and_still_writes_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "wrap", lambda text, *, token=None: envelope.wrap(text[:0]))
    out = tmp_path / "reports"
    code, stdout, _ = _cli(
        "--root", str(tmp_path / "root"), "--corpora", str(CORPORA), "--out", str(out),
        "--date", DATE,
    )  # fmt: skip
    assert code == 1
    assert "SHIP GATE: FAIL" in stdout
    assert (out / f"{DATE}-fake.md").is_file()


def test_walle_eval_refuses_a_missing_dataset_before_writing_anything(tmp_path: Path) -> None:
    out = tmp_path / "reports"
    code, _, stderr = _cli(
        "--root", str(tmp_path / "root"), "--corpora", str(CORPORA), "--out", str(out),
        "--longmemeval", str(tmp_path / "missing.json"),
    )  # fmt: skip
    assert code == 2
    assert "missing.json" in stderr
    assert not out.exists()


def test_walle_eval_names_only_the_fake_and_provider_models(tmp_path: Path) -> None:
    code, _, stderr = _cli("--root", str(tmp_path), "--corpora", str(CORPORA), "--model", "gpt")
    assert code == 2
    assert "provider:model" in stderr


# --- the model adapters (D87) -----------------------------------------------------------


class _FakeClient:
    """What the adapter needs from aisuite's client, and the calls it received."""

    def __init__(self, reply: str = "[]") -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        message = type("Message", (), {"content": self.reply})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


def test_the_provider_adapter_sends_the_prompt_once_as_the_user_and_returns_the_text() -> None:
    client = _FakeClient('[{"ref": "d-x:1", "verdict": "keep", "reason": "ok"}]')
    model = models.ProviderModel("anthropic:claude-test", client=client)
    assert model.name == "anthropic:claude-test"
    request = proposer.Request(kind="judge", marker="0" * 16, items=(), prompt="the prompt")
    reply = model.complete(request)
    assert reply == '[{"ref": "d-x:1", "verdict": "keep", "reason": "ok"}]'
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "anthropic:claude-test"
    assert call["messages"] == [{"role": "user", "content": "the prompt"}]
    assert call["temperature"] == 0
    assert model.text("another") == reply and len(client.calls) == 2


def test_the_provider_adapter_returns_an_empty_string_not_none_for_no_content() -> None:
    client = _FakeClient()
    client.reply = None  # type: ignore[assignment]
    model = models.ProviderModel("openai:x", client=client)
    assert model.text("p") == ""


@pytest.mark.parametrize("name", ["anthropic", ":model", "anthropic:", "a b:c"])
def test_a_provider_name_must_be_provider_colon_model(name: str) -> None:
    with pytest.raises(ValueError, match="provider:model"):
        models.ProviderModel(name, client=_FakeClient())


def test_resolve_names_the_fake_and_a_provider() -> None:
    assert isinstance(models.resolve("fake"), proposer.FakeModel)
    with pytest.raises(ValueError, match="provider:model"):
        models.resolve("claude")


def test_a_provider_without_aisuite_installed_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_aisuite(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "aisuite":
            raise ModuleNotFoundError("No module named 'aisuite'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_aisuite)
    with pytest.raises(models.ModelUnavailable, match="aisuite"):
        models.resolve("anthropic:claude-test")


def test_the_evals_package_imports_no_model_or_network_library() -> None:
    """The adapter imports aisuite inside the call, so the harness loads with no key and no
    network library on the path (CLAUDE.md constraint 3's spirit, test_constraints' list)."""
    import ast

    from test_constraints import MODEL_OR_NETWORK

    for path in (ROOT / "src" / "walle" / "evals").glob("*.py"):
        top: set[str] = set()
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Import):
                top.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add((node.module or "").split(".")[0])
        assert top & MODEL_OR_NETWORK == set(), path.name


# --- capability: LongMemEval and LoCoMo adapted to recall (D98) -------------------------


class _Scripted:
    name = "scripted"

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def text(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply(prompt)


def test_longmemeval_loads_one_history_per_question_with_its_own_haystack() -> None:
    dataset = capability.load_longmemeval(FIXTURES / "longmemeval.json")
    assert dataset.name == "longmemeval"
    assert len(dataset.histories) == 6
    by_ability = {q.ability for h in dataset.histories for q in h.questions}
    assert by_ability == set(capability.LONGMEMEVAL_ABILITIES)
    first = dataset.histories[0]
    assert len(first.questions) == 1
    assert first.sessions[0].at == "2023-05-20T02:21:00+00:00"
    assert first.sessions[0].turns[0].unit == first.sessions[0].id
    abstain = [q for h in dataset.histories for q in h.questions if q.ability == "abstention"]
    assert len(abstain) == 1 and abstain[0].evidence == frozenset()


def test_locomo_loads_one_history_per_conversation_with_dialogue_ids() -> None:
    dataset = capability.load_locomo(FIXTURES / "locomo.json")
    assert dataset.name == "locomo"
    assert len(dataset.histories) == 1
    history = dataset.histories[0]
    assert [s.id for s in history.sessions] == ["session_1", "session_2"]
    assert history.sessions[0].at == "2023-05-08T13:56:00+00:00"
    assert history.sessions[0].turns[0].unit == "D1:1"
    assert {q.ability for q in history.questions} == {
        "single-hop",
        "multi-hop",
        "temporal",
        "open-domain",
        "adversarial",
    }
    adversarial = next(q for q in history.questions if q.ability == "adversarial")
    assert adversarial.answer  # the dataset's adversarial_answer, not an empty string


@pytest.mark.parametrize(
    ("loader", "name"),
    [(capability.load_longmemeval, "longmemeval"), (capability.load_locomo, "locomo")],
)
def test_a_dataset_file_that_is_not_its_shape_is_refused_by_name(
    tmp_path: Path, loader: Any, name: str
) -> None:
    bad = tmp_path / f"{name}.json"
    bad.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match=bad.name):
        loader(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2023/05/20 (Sat) 02:21", "2023-05-20T02:21:00+00:00"),
        ("1:56 pm on 8 May, 2023", "2023-05-08T13:56:00+00:00"),
        ("12:05 am on 25 December, 2022", "2022-12-25T00:05:00+00:00"),
        ("12:30 pm on 1 January, 2024", "2024-01-01T12:30:00+00:00"),
    ],
)
def test_dataset_times_become_log_instants(text: str, expected: str) -> None:
    assert capability.parse_time(text) == expected


def test_an_unreadable_dataset_time_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="time"):
        capability.parse_time("sometime in May")


def test_build_log_writes_one_observation_and_one_belief_per_turn(tmp_path: Path) -> None:
    from walle.events import Assertion, Observation
    from walle.index import update

    dataset = capability.load_longmemeval(FIXTURES / "longmemeval.json")
    history = dataset.histories[0]
    built = capability.build_log(tmp_path / "root", dataset.name, history)
    records = update(tmp_path / "root", built.principal).records
    observations = [r for r in records if isinstance(r.event, Observation)]
    assertions = [r for r in records if isinstance(r.event, Assertion)]
    turns = [t for s in history.sessions for t in s.turns]
    assert len(observations) == len(assertions) == len(turns)
    assert set(built.units.values()) == {s.id for s in history.sessions}
    for record in assertions:
        assert isinstance(record.event, Assertion)
        assert record.event.caused_by and record.event.caused_by[0] in {o.ref for o in observations}
        assert record.event.ts.startswith("2023-")  # the session's own time, not now
        assert built.units[record.ref] in {s.id for s in history.sessions}


def test_retrieval_asks_recall_and_scores_hits_against_the_evidence(tmp_path: Path) -> None:
    """The fixture is built so each evidence session answers its question in the question's
    own words; recall is prefix matching, so these hit. The abstention question has no
    evidence and is counted, not scored."""
    dataset = capability.load_longmemeval(FIXTURES / "longmemeval.json")
    result = capability.retrieval(tmp_path / "root", dataset)
    assert result.dataset == "longmemeval"
    assert result.questions == 6 and result.scored == 5 and result.unscored == 1
    assert result.hits == 5
    per = dict(result.per_ability)
    assert per["abstention"] == capability.AbilityScore(questions=1, scored=0, hits=0)
    assert per["knowledge-update"].hits == 1
    assert result.qa is None  # needs an answerer and a judge


def test_retrieval_on_locomo_matches_dialogue_ids(tmp_path: Path) -> None:
    dataset = capability.load_locomo(FIXTURES / "locomo.json")
    result = capability.retrieval(tmp_path / "root", dataset)
    assert result.questions == 5 and result.scored == 5
    assert result.hits >= 4


def test_a_question_whose_evidence_is_not_recalled_is_a_miss(tmp_path: Path) -> None:
    dataset = capability.load_longmemeval(FIXTURES / "longmemeval.json")
    history = dataset.histories[0]
    rewritten = capability.History(
        id=history.id,
        sessions=history.sessions,
        questions=tuple(
            capability.Question(
                id=q.id,
                ability=q.ability,
                text="zxqv wplk",
                answer=q.answer,
                evidence=q.evidence,
                asked_at=q.asked_at,
            )
            for q in history.questions
        ),
    )
    result = capability.retrieval(
        tmp_path / "root", capability.Dataset("longmemeval", (rewritten,))
    )
    assert result.scored == 1 and result.hits == 0


def test_qa_runs_only_with_an_answerer_and_a_judge(tmp_path: Path) -> None:
    """The QA number needs two model calls per question (D98's LLM-judged harness). With
    scripted models the plumbing is exercised; without them the result says not run."""
    dataset = capability.load_longmemeval(FIXTURES / "longmemeval.json")
    answerer = _Scripted(lambda prompt: "the answer is in the memory shown")
    judge = _Scripted(lambda prompt: "yes")
    result = capability.retrieval(tmp_path / "root", dataset, answerer=answerer, judge=judge)
    assert result.qa is not None
    assert result.qa.judged == 6 and result.qa.correct == 6
    assert result.qa.answerer == "scripted" and result.qa.judge == "scripted"
    # Every answer prompt carried the recalled memory enveloped, and the question.
    for prompt in answerer.prompts:
        assert "<<<untrusted:" in prompt or "Nothing in memory matches." in prompt
    # The judge saw the gold answer and the model's answer, and said yes.
    assert all("the answer is in the memory shown" in p for p in judge.prompts)


def test_a_judge_whose_first_word_is_not_yes_or_no_is_counted_wrong_and_noted(
    tmp_path: Path,
) -> None:
    dataset = capability.load_locomo(FIXTURES / "locomo.json")
    answerer = _Scripted(lambda prompt: "something")
    replies = iter(["maybe", "not sure", "Yes.", "No, it says the shelter.", "yes indeed"])
    judge = _Scripted(lambda prompt: next(replies))
    result = capability.retrieval(tmp_path / "root", dataset, answerer=answerer, judge=judge)
    assert result.qa is not None
    assert result.qa.judged == 5
    assert result.qa.correct == 2 and result.qa.unreadable == 2


def test_the_report_carries_capability_numbers_only_for_what_ran(tmp_path: Path) -> None:
    datasets = (capability.load_longmemeval(FIXTURES / "longmemeval.json"),)
    result = report.evaluate(tmp_path / "root", {}, proposer.FakeModel(), datasets=datasets)
    text = report.render(result, date=DATE)
    section = text.split("## Capability")[1]
    assert "longmemeval" in section and "hit@5" in section
    assert "QA" in section and "not run" in section  # no answerer, no judge
    assert "LoCoMo" in section and "not run" in section


def test_walle_eval_runs_a_dataset_it_is_given(tmp_path: Path) -> None:
    out = tmp_path / "reports"
    code, stdout, _ = _cli(
        "--root", str(tmp_path / "root"), "--corpora", str(CORPORA), "--out", str(out),
        "--date", DATE, "--longmemeval", str(FIXTURES / "longmemeval.json"),
        "--locomo", str(FIXTURES / "locomo.json"),
    )  # fmt: skip
    assert code == 0, stdout
    text = (out / f"{DATE}-fake.md").read_text()
    assert "longmemeval" in text and "locomo" in text
    assert "6 questions" in text and "5 questions" in text


def test_the_fixtures_are_synthetic() -> None:
    """RFC 2606 domains, no digit run that could be a number, no FAKE token that isn't."""
    import re

    for path in FIXTURES.glob("*.json"):
        text = path.read_text()
        for host in re.findall(r"@([\w.-]+)|https?://([\w.-]+)", text):
            assert any(h.endswith(("example.com", "example.org", "example.net")) for h in host if h)
        assert not re.search(r"\d{7,}", text), path.name


def test_fixture_copies_are_not_the_datasets() -> None:
    """A real LongMemEval_S file is hundreds of megabytes with 500 questions. Ours is tiny."""
    for path in FIXTURES.glob("*.json"):
        assert path.stat().st_size < 20_000, path.name
