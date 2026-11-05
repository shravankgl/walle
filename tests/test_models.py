"""Issue #52's verify criterion: a real model route, with ``ollama:`` as the local tier (D16, D87).

  a fake per provider          a local HTTP server stands in for ollama, openrouter and openai
  the string is the switch     one code path; only the ``provider:model`` string changes
  colons and slashes           ``openrouter:<author>/<model>:free`` reaches the provider unchanged
  failure proposes nothing     an HTTP error, a timeout or a refused connection writes nothing
                               and says why
  429 stops                    a rate limit is one request, never retried, and said as such
  no key leaks                 a key-shaped value is sent to the provider and appears in no
                               error, log line or command output

No real key is needed or read: every provider variable is cleared before each test, and the
providers that will not start without a key get ``fake_provider.FAKE_KEY``. Nothing here
depends on file permissions, so the tests mean the same run as root or not.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from fake_provider import (
    FAKE_KEY,
    Reply,
    Seen,
    closed_port,
    completion,
    proposer_reply,
    serving,
)
from walle import models, proposer
from walle.cli import main
from walle.events import Proposal
from walle.index import update
from walle.paths import principal_dir

FIXTURE = Path(__file__).parent / "fixtures" / "proposer"
P = "sam"
D = "mbp"

PROVIDER_VARS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OLLAMA_API_URL",
    "OR_SITE_URL",
    "OR_APP_NAME",
    "ANTHROPIC_API_KEY",
)

# Each route, and the model name its provider must receive.
ROUTES = [
    ("ollama:llama3.1:8b", "llama3.1:8b"),
    ("openrouter:meta-llama/llama-3.3-8b-instruct:free", "meta-llama/llama-3.3-8b-instruct:free"),
    ("openai:gpt-4o-mini", "gpt-4o-mini"),
]


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real key in the environment is never read by these tests."""
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    log = root / "principals" / f"p-{P}" / "log"
    log.mkdir(parents=True)
    shutil.copy(FIXTURE / f"d-{D}.jsonl", log / f"d-{D}.jsonl")
    return root


def _stream(root: Path) -> bytes:
    return (principal_dir(root, P) / "log" / f"d-{D}.jsonl").read_bytes()


def _proposals(root: Path) -> list[Proposal]:
    return [r.event for r in update(root, P).records if isinstance(r.event, Proposal)]


def _cli(root: Path, *args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, [args[0], "--root", str(root), *args[1:]])
    return result.exit_code, result.stdout, result.stderr


def _propose(root: Path, name: str, base_url: str, **options: float) -> proposer.ProposeReport:
    """The one code path every route takes. Only ``name`` differs between them."""
    model = models.ProviderModel(name, base_url=base_url, **options)
    return proposer.propose(root, P, D, model)


# --- the verify criterion ---------------------------------------------------------------


@pytest.mark.parametrize(("name", "wire"), ROUTES)
def test_each_route_reaches_its_fake_provider_and_proposes(
    tmp_path: Path, name: str, wire: str
) -> None:
    """D87: one string, swapped without code. The same call, three providers."""
    root = _fixture(tmp_path)
    with serving() as fake:
        report = _propose(root, name, fake.base_url)

    assert report.model == name
    assert len(report.proposed) == 1
    assert [p.summary for p in _proposals(root)][-1].startswith("Waters the allotment")
    assert fake.seen, "the request reached the fake"
    for seen in fake.seen:
        assert seen.path == "/v1/chat/completions"
        assert seen.body["model"] == wire
        assert seen.body["temperature"] == 0
        assert [m["role"] for m in seen.body["messages"]] == ["user"]
    # Propose, then judge the queue in shadow: both went through the provider.
    assert fake.seen[0].prompt.startswith(proposer.PROPOSE_INSTRUCTIONS)
    assert fake.seen[-1].prompt.startswith(proposer.JUDGE_INSTRUCTIONS)
    assert report.verdicts >= 1
    assert report.shadow_error is None


def test_an_openrouter_free_model_name_reaches_the_provider_unchanged(tmp_path: Path) -> None:
    """The model part holds a slash and a colon. Only the first colon splits provider from
    model; the slash stays in the JSON body and never becomes part of the URL path."""
    name = "openrouter:google/gemma-3-27b-it:free"
    root = _fixture(tmp_path)
    with serving() as fake:
        _propose(root, name, fake.base_url)

    assert {seen.body["model"] for seen in fake.seen} == {"google/gemma-3-27b-it:free"}
    assert {seen.path for seen in fake.seen} == {"/v1/chat/completions"}
    assert all(seen.headers["authorization"] == f"Bearer {FAKE_KEY}" for seen in fake.seen)


def test_ollama_needs_no_key_and_is_found_by_its_own_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local tier: no key in the environment at all, the host from ``OLLAMA_API_URL``,
    and the command line is all that names it."""
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    root = _fixture(tmp_path)
    with serving() as fake:
        monkeypatch.setenv("OLLAMA_API_URL", fake.url)
        code, out, err = _cli(
            root, "propose", "--principal", P, "--device", D, "--model", "ollama:llama3.1:8b"
        )

    assert code == 0, out + err
    assert f"{P}: proposed 1 item" in out
    assert {seen.body["model"] for seen in fake.seen} == {"llama3.1:8b"}


def test_score_needs_no_provider_at_all(tmp_path: Path) -> None:
    """``--score`` reads the shadow log by the model's name; it never builds a client."""
    root = _fixture(tmp_path)
    code, out, err = _cli(
        root, "propose", "--principal", P, "--device", D, "--model", "ollama:x", "--score"
    )
    assert code == 0, out + err
    assert "shadow score for ollama:x" in out


# --- failure proposes nothing, and says why ------------------------------------------------


def _assert_nothing_written(root: Path, before: bytes) -> None:
    assert _stream(root) == before
    assert not proposer.seen_path(root, P).exists()
    assert not proposer.verdicts_path(root, P).exists()


def test_a_provider_error_proposes_nothing_and_says_why(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    with (
        serving(lambda _: Reply(503, {"error": {"message": "overloaded"}})) as fake,
        pytest.raises(proposer.ModelCallError) as caught,
    ):
        _propose(root, "openai:gpt-4o-mini", fake.base_url)

    assert len(fake.seen) == 1, "one request: the client does not retry"
    message = str(caught.value)
    assert "openai:gpt-4o-mini" in message and "HTTP 503" in message
    _assert_nothing_written(root, before)


def test_a_timeout_proposes_nothing_and_says_why(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    with (
        serving(lambda s: Reply(payload=completion("[]"), delay=2.0)) as fake,
        pytest.raises(proposer.ModelCallError) as caught,
    ):
        _propose(root, "ollama:llama3.1:8b", fake.base_url, timeout=0.3)

    assert len(fake.seen) == 1
    assert "timed out: no response for 0.3 s" in str(caught.value)
    _assert_nothing_written(root, before)


def test_a_provider_that_is_not_running_proposes_nothing(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    with pytest.raises(proposer.ModelCallError) as caught:
        _propose(root, "ollama:llama3.1:8b", closed_port())
    assert "could not connect" in str(caught.value)
    _assert_nothing_written(root, before)


@pytest.mark.parametrize(
    "payload",
    [completion(None), {"id": "chatcmpl-fake", "object": "chat.completion", "choices": []}],
    ids=["null-content", "no-choices"],
)
def test_a_reply_with_no_message_proposes_nothing(tmp_path: Path, payload: object) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    with (
        serving(lambda _: Reply(payload=payload)) as fake,
        pytest.raises((proposer.ModelCallError, proposer.ModelReplyError)),
    ):
        _propose(root, "ollama:llama3.1:8b", fake.base_url)
    _assert_nothing_written(root, before)


def test_the_cli_says_why_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    with serving(lambda _: Reply(500, {"error": {"message": "boom"}})) as fake:
        monkeypatch.setenv("OLLAMA_API_URL", fake.url)
        code, out, err = _cli(
            root, "propose", "--principal", P, "--device", D, "--model", "ollama:llama3.1:8b"
        )
    assert code == 1
    assert out == ""
    assert "nothing written" in err and "HTTP 500" in err
    assert _stream(root) == before


# --- 429: stop, once, and say so -----------------------------------------------------------


def test_a_rate_limit_is_one_request_never_retried(tmp_path: Path) -> None:
    """Free tiers count failed requests against a daily cap, so a 429 is the end of the run."""
    root = _fixture(tmp_path)
    before = _stream(root)
    limited = Reply(429, {"error": {"message": "rate limited"}}, headers={"Retry-After": "41"})
    with (
        serving(lambda _: limited) as fake,
        pytest.raises(proposer.ModelCallError) as caught,
    ):
        _propose(root, "openrouter:meta-llama/llama-3.3-8b-instruct:free", fake.base_url)

    assert len(fake.seen) == 1
    message = str(caught.value)
    assert "rate limited" in message and "HTTP 429" in message
    assert "not retried" in message
    assert "41" in message
    _assert_nothing_written(root, before)


def test_a_rate_limit_while_judging_keeps_the_proposals_and_says_so(tmp_path: Path) -> None:
    """The proposals are durable before judging starts (D82): a 429 there stops judging,
    is reported in ``shadow_error``, and asks nothing further of the provider."""
    root = _fixture(tmp_path)

    def respond(seen: Seen) -> Reply:
        if seen.prompt.startswith(proposer.JUDGE_INSTRUCTIONS):
            return Reply(429, {"error": {"message": "slow down"}})
        return proposer_reply(seen)

    with serving(respond) as fake:
        report = _propose(root, "openrouter:meta-llama/llama-3.3-8b-instruct:free", fake.base_url)

    assert len(report.proposed) == 1
    assert report.shadow_error is not None and "HTTP 429" in report.shadow_error
    judged = [s for s in fake.seen if s.prompt.startswith(proposer.JUDGE_INSTRUCTIONS)]
    assert len(judged) == 1


# --- keys ----------------------------------------------------------------------------------


def test_no_key_appears_in_errors_or_logs_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The fake echoes the Authorization header back in its 401, as a careless proxy might.
    The key reaches the provider and nowhere else: not the error, not a log line at DEBUG,
    not the command's output."""
    root = _fixture(tmp_path)

    def echo(seen: Seen) -> Reply:
        sent = seen.headers.get("authorization", "")
        return Reply(401, {"error": {"message": f"Incorrect API key provided: {sent}"}})

    caplog.set_level(logging.DEBUG)
    with serving(echo) as fake:
        with pytest.raises(proposer.ModelCallError) as caught:
            _propose(root, "openrouter:meta-llama/llama-3.3-8b-instruct:free", fake.base_url)
        monkeypatch.setenv("OLLAMA_API_URL", fake.url)
        code, out, err = _cli(
            root, "propose", "--principal", P, "--device", D, "--model", "ollama:llama3.1:8b"
        )

    assert fake.seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}", "sent to the provider"
    assert "HTTP 401" in str(caught.value)
    assert code == 1
    assert any(r.name.startswith("openai") for r in caplog.records), "the SDK's DEBUG was read"
    for text in (str(caught.value), repr(caught.value), caplog.text, out, err):
        assert FAKE_KEY not in text
        assert "FAKE-KEY-NOT-REAL" not in text
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_missing_key_names_the_variable_not_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(models.ModelUnavailable) as caught:
        models.resolve("openrouter:meta-llama/llama-3.3-8b-instruct:free")
    assert "OPENROUTER_API_KEY" in str(caught.value)


# --- names ---------------------------------------------------------------------------------


def test_resolve_returns_the_fake_for_fake() -> None:
    assert isinstance(models.resolve("fake"), proposer.FakeModel)


@pytest.mark.parametrize("bad", ["gpt-4o", "ollama:", ":model", "ollama:llama 3", ""])
def test_a_name_that_is_not_provider_model_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="provider:model"):
        models.ProviderModel(bad)


def test_a_name_longer_than_the_shadow_log_holds_is_refused() -> None:
    """``ShadowVerdict.model`` and ``Seen.model`` hold 64 characters. A longer name would
    stage proposals and then fail every shadow write, so it is refused before any call."""
    with pytest.raises(ValueError, match="64"):
        models.ProviderModel("openrouter:" + "a" * 60)


def test_an_unknown_provider_is_unavailable_before_any_call() -> None:
    with pytest.raises(models.ModelUnavailable) as caught:
        models.resolve("nosuchvendor:some-model")
    assert "nosuchvendor" in str(caught.value)


def test_the_cli_refuses_a_name_that_is_not_provider_model(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    code, _out, err = _cli(root, "propose", "--principal", P, "--device", D, "--model", "gpt4")
    assert code == 2
    assert "fake" in err and "provider:model" in err
    assert _stream(root) == before


def test_the_cli_reports_an_unavailable_provider_and_writes_nothing(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    before = _stream(root)
    code, out, err = _cli(
        root, "propose", "--principal", P, "--device", D, "--model", "nosuchvendor:m"
    )
    assert code == 1
    assert out == ""
    assert "nosuchvendor:m" in err
    assert _stream(root) == before


def test_the_adapter_is_imported_lazily() -> None:
    """``walle.models`` loads with no provider library imported, so every other command
    keeps its start-up time and a missing SDK is a reason, not an import error."""
    source = Path(models.__file__).read_text()
    top = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("aisuite" in line or "openai" in line for line in top)
    assert json.loads(json.dumps(models.TIMEOUT_SECONDS)) > 0
