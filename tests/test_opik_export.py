"""Issue #80's verify criteria, and the cases around them (D25, D22, D92).

  off by default      with the exporter off nothing is imported or sent, and the switch is not
                      on in a fresh environment
  on                  a fake Opik server receives each finished mission and step with its
                      token count, cost and duration, read from the mission and step events
  no duplicates       re-running sends nothing already there
  never in the way    a failed or missing Opik server never fails anything or changes the log
  counts and timings  no prompt, turn, title, note or memory text leaves walle: what the server
                      received is scanned, and a payload with text in it is never sent
  local by default    the endpoint defaults to localhost; a non-local one is refused unless the
                      owner names its host explicitly

Every log here is synthetic: a made-up Sunday meal plan and a made-up weekend of chores.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fake_opik import PREFIX, FakeOpik, closed_port_url
from walle import opik_export
from walle.events import (
    ACTOR_KERNEL,
    ACTOR_USER,
    AnyEvent,
    Assertion,
    Link,
    Mission,
    Node,
    Step,
    StepStatus,
    StepVerb,
    Supersede,
)
from walle.missions import fold_missions
from walle.opik_export import (
    DEFAULT_URL,
    OPIK_SWITCH,
    PROJECT_VARIABLE,
    REMOTE_VARIABLE,
    SWITCH_ON,
    URL_VARIABLE,
    Leak,
    Refused,
    Request,
    Response,
    enabled,
    endpoint,
    export,
    scan,
    traces,
)
from walle.paths import log_dir
from walle.reader import Record, read_file

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "src" / "walle"
BUDGET = Path(__file__).parent / "fixtures" / "v1" / "budget"

UUID7 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

# Text that must never leave walle. Each is in the synthetic log below, as a title, a note,
# a node id or memory, and long enough that a chance match inside a UUID or a number is
# impossible.
SECRET_TEXTS = (
    "Plan the zucchini party",
    "Buy zucchini at the corner shop",
    "buy-zucchini",
    "Call Grandma Pomona about dessert",
    "call-pomona",
    "the shop was out of zucchini, used marrows",
    "Pomona is allergic to walnuts",
    "allergic to walnuts, keep them out of every dish",
)


# --- builders ----------------------------------------------------------------------------


def _stream(*events: AnyEvent, device: str = "mbp") -> list[Record]:
    """Records as ``reader`` would hand them over: seq from 1, refs ``d-<device>:<seq>``."""
    out: list[Record] = []
    for seq, event in enumerate(events, start=1):
        stored = event.model_copy(update={"seq": seq})
        out.append(Record(ref=f"d-{device}:{seq}", seq=seq, event=stored, line=seq))
    return out


def _status(
    node: str, status: StepStatus, ts: str, *, tokens: int | None = None, cost: float | None = None
) -> Step:
    return Step(
        ts=ts,
        actor=ACTOR_KERNEL,
        mission="d-mbp:1",
        verb=StepVerb.STATUS,
        node=node,
        status=status,
        tokens=tokens,
        cost_usd=cost,
    )


def _party(*, finished: bool = True) -> list[Record]:
    """A two-node mission: buy, then call. Every string in it is in ``SECRET_TEXTS``."""
    mission = Mission(
        ts="2026-11-08T10:00:00.000000+05:30",
        actor=ACTOR_USER,
        title="Plan the zucchini party",
        nodes=[
            Node(id="buy-zucchini", title="Buy zucchini at the corner shop", token_budget=5000),
            Node(id="call-pomona", title="Call Grandma Pomona about dessert"),
        ],
        links=[Link(source="buy-zucchini", target="call-pomona")],
    )
    memory = Assertion(
        ts="2026-11-08T09:59:00.000000+05:30",
        actor=ACTOR_USER,
        body="allergic to walnuts, keep them out of every dish",
        summary="Pomona is allergic to walnuts",
    )
    steps: list[AnyEvent] = [
        _status("buy-zucchini", StepStatus.RUNNING, "2026-11-08T10:01:00.000000+05:30", tokens=300),
        Step(
            ts="2026-11-08T10:04:30.500000+05:30",
            actor=ACTOR_KERNEL,
            mission="d-mbp:1",
            verb=StepVerb.STATUS,
            node="buy-zucchini",
            status=StepStatus.COMPLETED,
            tokens=700,
            cost_usd=0.0125,
            note="the shop was out of zucchini, used marrows",
        ),
        _status("call-pomona", StepStatus.RUNNING, "2026-11-08T10:05:00.000000+05:30"),
    ]
    if finished:
        steps.append(
            _status(
                "call-pomona",
                StepStatus.COMPLETED,
                "2026-11-08T10:07:00.000000+05:30",
                tokens=50,
                cost=0.001,
            )
        )
    return _stream(mission, *steps, memory)


def _on(url: str, **extra: str) -> dict[str, str]:
    return {OPIK_SWITCH: SWITCH_ON, URL_VARIABLE: url, **extra}


def _budget_root(tmp_path: Path) -> Path:
    root = tmp_path / "walle"
    shutil.copytree(BUDGET, root, ignore=shutil.ignore_patterns("__expected__.json"))
    return root


def _read_root(root: Path) -> list[Record]:
    records = [
        record
        for path in sorted(log_dir(root, "ira").glob("*.jsonl"))
        for record in read_file(path, path.stem)
    ]
    assert records, "the fixture was not found; the test would prove nothing"
    return records


def _fingerprint(root: Path) -> dict[str, str]:
    """Every file under ``root`` and the hash of its bytes: the log, byte for byte."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def opik() -> Iterator[FakeOpik]:
    with FakeOpik() as fake:
        yield fake


class Spy:
    """A transport that records every request and answers from a script, never the network."""

    def __init__(self, status: int = 404) -> None:
        self.requests: list[Request] = []
        self.status = status

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        return Response(self.status if request.method == "GET" else 204, {}, b"")


# --- off by default (verify) ---------------------------------------------------------------


def test_the_switch_is_off_in_a_fresh_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OPIK_SWITCH, raising=False)
    assert enabled() is False
    assert enabled({}) is False


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", "On", " on", "on "])
def test_only_the_exact_word_turns_it_on(value: str) -> None:
    assert enabled({OPIK_SWITCH: value}) is False
    assert enabled({OPIK_SWITCH: "on"}) is True


def test_off_sends_nothing_even_with_an_endpoint_set(opik: FakeOpik) -> None:
    report = export(_party(), {URL_VARIABLE: opik.url})
    assert report.state == "off"
    assert opik.received == []
    assert report.sent == () and report.present == () and report.failed == ()


def test_off_imports_nothing(tmp_path: Path) -> None:
    """In a fresh interpreter: importing the module and exporting with the switch off loads
    neither ``opik`` nor any HTTP client, and makes no request (nothing is listening)."""
    code = (
        "import sys\n"
        "from walle import opik_export\n"
        "from walle.missions import fold_missions\n"
        f"report = opik_export.export([], {{'{URL_VARIABLE}': 'http://127.0.0.1:9/api'}})\n"
        "assert report.state == 'off', report\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('opik', 'httpx', 'requests') or m in ('urllib.request', 'http.client'))\n"
        "print(loaded)\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("WALLE_")}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_core_does_not_import_the_exporter() -> None:
    """D25 as narrowed on 2026-11-05: walle works the same without Opik, so nothing in the
    package reaches for the exporter; it is only ever called by whoever turns it on."""
    importers = {
        path.name
        for path in PACKAGE.rglob("*.py")
        if path.name != "opik_export.py" and re.search(r"\bopik_export\b", path.read_text())
    }
    assert importers == set()


def test_opik_is_an_extra_and_never_a_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert not [d for d in project["dependencies"] if d.lower().startswith("opik")]
    assert [d.split(">")[0] for d in project["optional-dependencies"]["opik"]] == ["opik"]


# --- on: each mission and step, with tokens, cost and duration (verify) -------------------


def test_a_finished_mission_and_its_steps_reach_the_server(opik: FakeOpik) -> None:
    report = export(_party(), _on(opik.url))
    assert report.state == "finished", report
    assert report.sent == ("d-mbp:1",)

    (trace,) = opik.traces.values()
    assert UUID7.fullmatch(trace["id"])
    assert trace["project_name"] == "walle"
    assert trace["start_time"] == "2026-11-08T04:30:00.000000Z"
    assert trace["end_time"] == "2026-11-08T04:37:00.000000Z"
    assert trace["metadata"] == {
        "walle_mission": "d-mbp:1",
        "outcome": "done",
        "tokens": 1050,
        "cost_usd": 0.0135,
        "duration_ms": 420_000,
        "steps": 2,
    }

    spans = sorted(opik.spans.values(), key=lambda s: s["start_time"])
    assert [s["trace_id"] for s in spans] == [trace["id"], trace["id"]]
    assert [s["usage"] for s in spans] == [{"total_tokens": 1000}, {"total_tokens": 50}]
    assert [s["total_estimated_cost"] for s in spans] == [0.0125, 0.001]
    assert [(s["start_time"], s["end_time"]) for s in spans] == [
        ("2026-11-08T04:31:00.000000Z", "2026-11-08T04:34:30.500000Z"),
        ("2026-11-08T04:35:00.000000Z", "2026-11-08T04:37:00.000000Z"),
    ]
    assert [s["metadata"]["duration_ms"] for s in spans] == [210_500, 120_000]
    assert [s["metadata"]["status"] for s in spans] == ["completed", "completed"]
    assert [s["metadata"]["walle_steps"] for s in spans] == [
        ["d-mbp:2", "d-mbp:3"],
        ["d-mbp:5"],
    ]


def test_figures_come_from_the_golden_budget_log(opik: FakeOpik, tmp_path: Path) -> None:
    """The v1 budget fixture on main: a node that blew its token budget and was aborted."""
    records = _read_root(_budget_root(tmp_path))
    state = fold_missions(records).mission("d-tab:1")
    report = export(records, _on(opik.url))
    assert report.sent == ("d-tab:1",)

    (trace,) = opik.traces.values()
    assert trace["metadata"]["outcome"] == "failed"
    assert trace["metadata"]["tokens"] == state.spent[1] == 2300
    assert trace["metadata"]["cost_usd"] == state.spent[0] == 0.06
    assert trace["metadata"]["duration_ms"] == 300_250
    (span,) = opik.spans.values()  # the two nodes that never started have no span
    assert span["usage"] == {"total_tokens": 2300}
    assert span["total_estimated_cost"] == 0.06
    assert span["metadata"]["status"] == "failed"
    assert span["metadata"]["duration_ms"] == 240_250


def test_spans_go_before_the_trace(opik: FakeOpik) -> None:
    """The trace is the commit mark: if it is there, its spans were accepted first."""
    export(_party(), _on(opik.url))
    assert [(r.method, r.path) for r in opik.received] == [
        ("GET", f"{PREFIX}/traces/{next(iter(opik.traces))}"),
        ("POST", f"{PREFIX}/spans/batch"),
        ("POST", f"{PREFIX}/traces/batch"),
    ]


def test_an_open_mission_waits_until_it_finishes(opik: FakeOpik) -> None:
    report = export(_party(finished=False), _on(opik.url))
    assert report.state == "finished" and report.sent == ()
    assert opik.received == []
    later = export(_party(), _on(opik.url))
    assert later.sent == ("d-mbp:1",)


def test_a_superseded_mission_is_not_exported(opik: FakeOpik) -> None:
    records = _party()
    undo = Supersede(ts="2026-11-08T11:00:00.000000+05:30", actor=ACTOR_USER, target="d-mbp:1")
    records = [
        *records,
        Record(ref="d-mbp:99", seq=99, event=undo.model_copy(update={"seq": 99}), line=99),
    ]
    assert export(records, _on(opik.url)).sent == ()
    assert opik.received == []


def test_times_are_by_clock_when_streams_interleave() -> None:
    """Refs sort by stream, then seq, so the last step in ref order need not be the latest.
    The trace ends at the latest step, and a span starts at its earliest."""
    mission = Mission(
        ts="2026-11-08T10:00:00.000000+00:00",
        actor=ACTOR_USER,
        title="Plan the zucchini party",
        nodes=[Node(id="buy-zucchini", title="x"), Node(id="call-pomona", title="y")],
    )
    on_mbp = _stream(
        mission,
        _status("call-pomona", StepStatus.COMPLETED, "2026-11-08T10:06:00.000000+00:00"),
    )
    on_phone = _stream(
        _status("buy-zucchini", StepStatus.RUNNING, "2026-11-08T10:04:00.000000+00:00"),
        _status("buy-zucchini", StepStatus.COMPLETED, "2026-11-08T10:02:00.000000+00:00"),
        device="zphone",
    )
    (trace,) = traces([*on_mbp, *on_phone])
    assert trace.end == "2026-11-08T10:06:00.000000+00:00"
    buy = next(s for s in trace.spans if s.end == "2026-11-08T10:02:00.000000+00:00")
    assert buy.start == "2026-11-08T10:02:00.000000+00:00"


def test_ids_are_uuid7_and_the_same_on_every_fold() -> None:
    first, second = traces(_party()), traces(list(reversed(_party())))
    assert first == second
    (trace,) = first
    assert UUID7.fullmatch(trace.id)
    assert all(UUID7.fullmatch(s.id) for s in trace.spans)
    assert len({trace.id, *(s.id for s in trace.spans)}) == 3


# --- no duplicates (verify) ----------------------------------------------------------------


def test_rerunning_sends_no_duplicates(opik: FakeOpik) -> None:
    export(_party(), _on(opik.url))
    posts = len(opik.posts)
    again = export(_party(), _on(opik.url))
    assert again.state == "finished"
    assert again.sent == () and again.present == ("d-mbp:1",)
    assert len(opik.posts) == posts, "the second run only asked; it sent nothing"
    assert opik.trace_posts == 1 and opik.span_posts == 1
    assert len(opik.spans) == 2


def test_the_cursor_is_the_log_and_the_server_not_a_side_file(
    opik: FakeOpik, tmp_path: Path
) -> None:
    """D92: nothing is written anywhere to remember what was sent."""
    root = _budget_root(tmp_path)
    before = _fingerprint(tmp_path)
    export(_read_root(root), _on(opik.url))
    export(_read_root(root), _on(opik.url))
    assert _fingerprint(tmp_path) == before
    assert opik.trace_posts == 1


# --- never in the way (verify) -------------------------------------------------------------


def test_a_missing_server_fails_nothing_and_leaves_the_log(tmp_path: Path) -> None:
    root = _budget_root(tmp_path)
    before, folded = _fingerprint(root), fold_missions(_read_root(root))
    report = export(_read_root(root), _on(closed_port_url()))
    assert report.state == "unreachable"
    assert report.sent == ()
    assert report.reason is not None and "127.0.0.1" in report.reason
    assert _fingerprint(root) == before
    assert fold_missions(_read_root(root)) == folded


@pytest.mark.parametrize("path", [f"{PREFIX}/spans/batch", f"{PREFIX}/traces/batch"])
def test_a_failing_server_is_reported_and_retried_next_run(
    path: str, opik: FakeOpik, tmp_path: Path
) -> None:
    root = _budget_root(tmp_path)
    before = _fingerprint(root)
    opik.fail[path] = 500
    report = export(_read_root(root), _on(opik.url))
    assert report.state == "finished"
    assert report.sent == ()
    assert [ref for ref, _ in report.failed] == ["d-tab:1"]
    assert "500" in report.failed[0][1]
    assert opik.traces == {}, "no trace without its spans"
    assert _fingerprint(root) == before

    del opik.fail[path]
    assert export(_read_root(root), _on(opik.url)).sent == ("d-tab:1",)


def test_a_failed_existence_check_sends_nothing_for_that_mission(opik: FakeOpik) -> None:
    trace_id = traces(_party())[0].id
    opik.fail[f"{PREFIX}/traces/{trace_id}"] = 503
    report = export(_party(), _on(opik.url))
    assert [ref for ref, _ in report.failed] == ["d-mbp:1"]
    assert opik.posts == []


def test_a_redirect_is_not_followed() -> None:
    """A redirect could carry the payload to a host nobody approved."""
    with FakeOpik() as elsewhere, FakeOpik() as opik:
        opik.redirect = elsewhere.url.removesuffix("/api")
        report = export(_party(), _on(opik.url))
        assert report.sent == ()
        assert [ref for ref, _ in report.failed] == ["d-mbp:1"]
        assert elsewhere.received == []


# --- counts and timings only (verify) ------------------------------------------------------


def test_no_text_reaches_the_server(opik: FakeOpik) -> None:
    export(_party(), _on(opik.url))
    assert opik.posts, "the test proves nothing if nothing was sent"
    for request in opik.received:
        scan(request.body, "walle") if request.body else None
        wire = request.path.encode() + request.body
        for text in SECRET_TEXTS:
            assert text.encode() not in wire, text


def test_a_payload_with_text_in_it_is_never_sent(
    opik: FakeOpik, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan runs on the bytes handed to the wire, so a builder that slips a title in is
    caught there, not trusted."""
    real = opik_export.span_payload

    def leaky(trace: opik_export.Trace, project: str) -> dict[str, Any]:
        payload = real(trace, project)
        payload["spans"][0]["input"] = {"title": "Buy zucchini at the corner shop"}
        return payload

    monkeypatch.setattr(opik_export, "span_payload", leaky)
    report = export(_party(), _on(opik.url))
    assert report.sent == ()
    assert [ref for ref, _ in report.failed] == ["d-mbp:1"]
    assert "scan" in report.failed[0][1]
    assert opik.posts == []
    assert all(b"zucchini" not in r.body for r in opik.received)


@pytest.mark.parametrize(
    "body",
    [
        b'{"spans": [{"name": "water the plants"}]}',
        b'{"traces": [{"input": {"prompt": "x"}}]}',
        b'{"spans": [{"metadata": {"note": "d-mbp:1"}}]}',
        b'{"spans": [{"tags": ["Pomona"]}]}',
        b'{"spans": [{"total_estimated_cost": true}]}',
        b"not json at all",
        b'{"spans": [{"name": "Buy zucchini", "name": "walle step"}]}',
        b'{"spans": [{"total_estimated_cost": NaN}]}',
        '{"spans": [{"name": "walle step\u00e9"}]}'.encode(),
    ],
)
def test_the_scan_refuses_anything_but_counts_ids_and_times(body: bytes) -> None:
    with pytest.raises(Leak):
        scan(body, "walle")


def test_the_scan_passes_what_the_exporter_builds() -> None:
    (trace,) = traces(_party())
    for payload in (
        opik_export.span_payload(trace, "walle"),
        opik_export.trace_payload(trace, "walle"),
    ):
        scan(json.dumps(payload).encode(), "walle")


def test_a_project_name_with_text_in_it_is_refused(opik: FakeOpik) -> None:
    report = export(_party(), _on(opik.url, **{PROJECT_VARIABLE: "Grandma Pomona"}))
    assert report.state == "refused"
    assert opik.received == []


# --- local by default (verify) -------------------------------------------------------------


def test_the_endpoint_defaults_to_localhost() -> None:
    assert endpoint({}) == DEFAULT_URL == "http://localhost:5173/api"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173/api",
        "http://127.0.0.1:8080/api",
        "http://127.0.0.2/api",
        "http://[::1]:5173/api",
        "https://LOCALHOST/api/",
    ],
)
def test_a_local_endpoint_is_accepted(url: str) -> None:
    assert endpoint({URL_VARIABLE: url}) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://opik.example.com/api",
        "http://localhost.example.com/api",
        "http://10.0.0.5:5173/api",
        "http://0.0.0.0:5173/api",
        "http://user:pw@localhost:5173/api",
        "http://localhost:5173/api?x=1",
        "file:///etc/passwd",
        "localhost:5173",
    ],
)
def test_a_non_local_or_odd_endpoint_is_refused(url: str) -> None:
    spy = Spy()
    with pytest.raises(Refused):
        endpoint({URL_VARIABLE: url})
    report = export(_party(), _on(url), transport=spy)
    assert report.state == "refused"
    assert spy.requests == []


def test_a_remote_endpoint_needs_its_host_named() -> None:
    url = "https://opik.example.com/api"
    spy = Spy()
    assert (
        export(_party(), _on(url, **{REMOTE_VARIABLE: "other.example.com"}), transport=spy).state
        == "refused"
    )
    assert spy.requests == []
    report = export(_party(), _on(url, **{REMOTE_VARIABLE: "opik.example.com"}), transport=spy)
    assert report.state == "finished", report
    assert {r.url.split("/api/")[0] for r in spy.requests} == {"https://opik.example.com"}


def test_a_named_remote_host_still_needs_https() -> None:
    env = _on("http://opik.example.com/api", **{REMOTE_VARIABLE: "opik.example.com"})
    spy = Spy()
    assert export(_party(), env, transport=spy).state == "refused"
    assert spy.requests == []
