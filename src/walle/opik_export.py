"""The optional Opik exporter: missions and steps, as counts and timings, to a self-hosted Opik.

D25 as the owner narrowed it on 2026-11-05: Opik is optional, never a required service. The
log stays the only source of truth; what Opik holds is a read-only copy, so walle works the
same with this module absent, off, or pointed at a server that is down. Nothing in the
package imports this module. Whoever turns it on calls ``export``.

What leaves walle
-----------------
One Opik *trace* per finished mission and one *span* per step (a node of the mission) that
started, with what D22's budgets already read from the log: tokens, dollars, start and end.
Names are fixed words, ids are UUIDs, times are instants, and the only identifiers from the
log are refs (``d-mbp:12``), so the owner can get from Opik back to the event. No title, no
note, no node id, no prompt, turn or memory text: a mission is a plan written in a person's
words, and a node id is too.

That is enforced on the wire, not trusted from the builder. ``scan`` decodes the exact bytes
handed to the transport and refuses anything that is not on its allowlist of keys and value
shapes: a payload with one stray string in it is never sent (``Leak``).

The rules
---------
1. **Off unless the owner turns it on (D117, the dormant rule).** ``WALLE_OPIK`` must be
   exactly ``on``, in the environment, where no event the agent appends can reach. Off, this
   module sends nothing and imports no HTTP client: ``urllib.request`` is imported inside the
   transport, which is only reached when on. It never imports the ``opik`` package at all;
   it speaks Opik's REST API with the standard library so that ``scan`` sees the bytes that
   go out, and so the tests run without the extra.
2. **Local by default.** The endpoint is ``WALLE_OPIK_URL``, default
   ``http://localhost:5173/api`` (Opik's local server). A host that is not ``localhost`` or a
   loopback address is refused unless ``WALLE_OPIK_REMOTE_HOST`` names that exact host, and
   then only over ``https``. Credentials, queries and fragments in the URL are refused. The
   transport follows no redirect and uses no proxy, so the bytes go where the URL says.
3. **Only finished missions.** A mission is exported once its outcome is ``done`` or
   ``failed`` (``walle.missions``). An open one waits; a superseded one is not in the fold.
   A step gets a span if it has a live status step; its end is the last one if its status
   is ``completed`` or ``failed``, else it has none.
4. **The cursor is a fold (D92).** What to send is folded from the log every run, and the
   ids are derived from refs, so they are the same on every fold. What was already sent is
   asked of Opik (``GET`` the trace by id), not kept in a side file. Spans go first and the
   trace last, so a trace that exists means its spans were accepted. Re-running sends
   nothing that is there.
5. **Never in the way.** Nothing here writes a file or appends an event. A server that is
   missing ends the run as ``unreachable``; one that answers with an error marks that
   mission ``failed`` in the report and moves on, and the next run tries it again. No
   exception from the server reaches the caller.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from walle.events import Step, StepStatus, parse_ref
from walle.missions import MissionState, fold_missions
from walle.resolver import live_events

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from walle.reader import Record


OPIK_SWITCH = "WALLE_OPIK"
"""The environment variable the owner sets to turn the exporter on. Nothing else does."""

SWITCH_ON = "on"
"""The one value that means on, as for missions: a stray ``1`` in a profile is not consent."""

URL_VARIABLE = "WALLE_OPIK_URL"
REMOTE_VARIABLE = "WALLE_OPIK_REMOTE_HOST"
"""The one non-local host the owner allows, spelled exactly as in the URL."""

PROJECT_VARIABLE = "WALLE_OPIK_PROJECT"
DEFAULT_URL = "http://localhost:5173/api"
DEFAULT_PROJECT = "walle"
TIMEOUT_S = 10.0

TRACE_NAME = "walle mission"
SPAN_NAME = "walle step"
SPAN_TYPE = "general"
TRACES_PATH = "/v1/private/traces"
SPANS_BATCH = "/v1/private/spans/batch"
TRACES_BATCH = "/v1/private/traces/batch"

type State = Literal["off", "refused", "unreachable", "finished"]
"""``finished`` means every finished mission was tried: see ``Report.failed`` for any that
were not sent. It is not a claim that everything went."""


class Refused(Exception):
    """The configuration asks for something the rules do not allow. Nothing was sent."""


class Leak(Exception):
    """A payload holds something other than counts, ids and times. It was not sent."""


class Unreachable(Exception):
    """The server could not be reached at all."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the owner has turned the exporter on: ``WALLE_OPIK`` is exactly ``on``."""
    env = os.environ if environ is None else environ
    return env.get(OPIK_SWITCH) == SWITCH_ON


def _local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint(environ: Mapping[str, str] | None = None) -> str:
    """The base URL to export to, without a trailing slash, or ``Refused`` (rule 2)."""
    env = os.environ if environ is None else environ
    url = env.get(URL_VARIABLE) or DEFAULT_URL
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise Refused(f"{URL_VARIABLE} must be an http or https URL, not {url!r}")
    if parts.username is not None or parts.password is not None:
        raise Refused(f"{URL_VARIABLE} must not carry credentials")
    if parts.query or parts.fragment:
        raise Refused(f"{URL_VARIABLE} must not carry a query or fragment")
    try:
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as error:
        raise Refused(f"{URL_VARIABLE} has a bad port: {error}") from error
    host = parts.hostname or ""
    if not _local(host):
        allowed = env.get(REMOTE_VARIABLE)
        if allowed is None or allowed.lower() != host:
            raise Refused(
                f"{host!r} is not local; set {REMOTE_VARIABLE}={host} to send there on purpose"
            )
        if parts.scheme != "https":
            raise Refused(f"a non-local endpoint must use https, not {parts.scheme}")
    return url.rstrip("/")


_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def project(environ: Mapping[str, str] | None = None) -> str:
    """The Opik project name: an owner-set slug, so that it cannot carry text either."""
    env = os.environ if environ is None else environ
    name = env.get(PROJECT_VARIABLE) or DEFAULT_PROJECT
    if not _PROJECT_RE.fullmatch(name):
        raise Refused(f"{PROJECT_VARIABLE} must be a lowercase slug, not {name!r}")
    return name


# --- the fold: what to send --------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One step of a finished mission, as counts and timings."""

    id: str
    start: str
    end: str | None
    tokens: int
    cost_usd: float
    status: StepStatus
    steps: tuple[str, ...]
    """Refs of the status steps that reported spend: where the figures were read from."""


@dataclass(frozen=True)
class Trace:
    """One finished mission, as counts and timings."""

    id: str
    mission: str
    outcome: Literal["done", "failed"]
    start: str
    end: str
    tokens: int
    cost_usd: float
    spans: tuple[Span, ...]


def _utc(instant: str) -> datetime:
    return datetime.fromisoformat(instant).astimezone(UTC)


def _z(instant: str) -> str:
    """An instant in UTC with a ``Z``: the one spelling Opik's backend parses everywhere."""
    return _utc(instant).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ms(start: str, end: str) -> int:
    return round((_utc(end) - _utc(start)).total_seconds() * 1000)


def _uuid7(kind: str, key: str, instant: str) -> str:
    """A UUID v7 that is a pure function of ``(kind, key, instant)``.

    Opik wants v7 ids, whose first 48 bits are a millisecond time. That time is the event's
    own ``ts``; the other 74 bits are SHA-256 of the key, so the same log always gives the
    same id (rule 4). The key is a ref or a ref and a position, never text: a hash of a node
    id could be guessed back from a short list of words.
    """
    millis = int(_utc(instant).timestamp() * 1000) & ((1 << 48) - 1)
    digest = int.from_bytes(hashlib.sha256(f"walle-opik:{kind}:{key}".encode()).digest()[:10])
    rand_a = digest >> 68  # 12 bits
    rand_b = digest & ((1 << 62) - 1)  # 62 bits
    value = (millis << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


_TERMINAL = (StepStatus.COMPLETED, StepStatus.FAILED)


def _trace(state: MissionState, steps: Mapping[str, Step]) -> Trace | None:
    outcome = state.outcome
    if outcome == "open":
        return None
    timeline: dict[str, list[str]] = {}
    for ref in state.applied:
        step = steps[ref]
        if step.node is not None and step.status is not None:
            timeline.setdefault(step.node, []).append(step.ts)
    spans: list[Span] = []
    for position, node in enumerate(state.nodes):
        times = timeline.get(node.id)
        if not times:
            continue  # never started: nothing to time
        key = node.injected_by or f"{state.ref}#{position}"
        cost, tokens = node.spent
        start = min(times, key=_utc)  # streams interleave: ref order is not time order
        spans.append(
            Span(
                id=_uuid7("span", key, start),
                start=start,
                # the last live status step in ref order is the one that set the status
                end=times[-1] if node.status in _TERMINAL else None,
                tokens=tokens,
                cost_usd=cost,
                status=node.status,
                steps=node.spent_from,
            )
        )
    stamps = [steps[ref].ts for ref in state.applied]
    cost, tokens = state.spent
    return Trace(
        id=_uuid7("trace", state.ref, state.mission.ts),
        mission=state.ref,
        outcome=outcome,
        start=state.mission.ts,
        end=max(stamps, key=_utc) if stamps else state.mission.ts,
        tokens=tokens,
        cost_usd=cost,
        spans=tuple(spans),
    )


def traces(records: Iterable[Record]) -> tuple[Trace, ...]:
    """Every finished live mission in ``records``, as what would be sent. A pure fold: the
    same records in any order give the same traces, with the same ids."""
    records = tuple(records)
    events, _ = live_events(records)
    steps = {ref: event for ref, event in events.items() if isinstance(event, Step)}
    found = (_trace(state, steps) for state in fold_missions(records).missions)
    return tuple(t for t in found if t is not None)


def span_payload(trace: Trace, project: str) -> dict[str, Any]:
    """The body of ``POST /v1/private/spans/batch`` for one trace."""
    spans: list[dict[str, Any]] = []
    for span in trace.spans:
        body: dict[str, Any] = {
            "id": span.id,
            "trace_id": trace.id,
            "project_name": project,
            "name": SPAN_NAME,
            "type": SPAN_TYPE,
            "start_time": _z(span.start),
            "usage": {"total_tokens": span.tokens},
            "total_estimated_cost": span.cost_usd,
            "metadata": {
                "walle_mission": trace.mission,
                "walle_steps": list(span.steps),
                "status": span.status.value,
            },
        }
        if span.end is not None:
            body["end_time"] = _z(span.end)
            body["metadata"]["duration_ms"] = _ms(span.start, span.end)
        spans.append(body)
    return {"spans": spans}


def trace_payload(trace: Trace, project: str) -> dict[str, Any]:
    """The body of ``POST /v1/private/traces/batch`` for one trace."""
    return {
        "traces": [
            {
                "id": trace.id,
                "project_name": project,
                "name": TRACE_NAME,
                "start_time": _z(trace.start),
                "end_time": _z(trace.end),
                "metadata": {
                    "walle_mission": trace.mission,
                    "outcome": trace.outcome,
                    "tokens": trace.tokens,
                    "cost_usd": trace.cost_usd,
                    "duration_ms": _ms(trace.start, trace.end),
                    "steps": len(trace.spans),
                },
                "tags": ["walle"],
            }
        ]
    }


# --- the scan: what is actually sent -----------------------------------------------------

_KEYS = frozenset(
    {
        # envelopes
        "spans",
        "traces",
        # trace and span fields
        "id",
        "trace_id",
        "project_name",
        "name",
        "type",
        "start_time",
        "end_time",
        "usage",
        "total_estimated_cost",
        "metadata",
        "tags",
        # usage and metadata
        "total_tokens",
        "walle_mission",
        "walle_steps",
        "status",
        "outcome",
        "tokens",
        "cost_usd",
        "duration_ms",
        "steps",
    }
)
_UUID7_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_WORDS = frozenset(
    {TRACE_NAME, SPAN_NAME, SPAN_TYPE, "walle", "done", "failed"} | {s.value for s in StepStatus}
)


def _ref(value: str) -> bool:
    try:
        parse_ref(value)
    except ValueError:
        return False
    return True


def _safe_string(value: str, project: str) -> bool:
    return (
        value in _WORDS
        or value == project
        or bool(_UUID7_RE.fullmatch(value) or _Z_RE.fullmatch(value))
        or _ref(value)
    )


def _refuse_constant(name: str) -> float:
    raise Leak(f"not a finite number: {name}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A repeated key is refused: ``json.loads`` would keep only the last one, so text in an
    earlier one would pass the scan and still be on the wire."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise Leak(f"scan: key {key!r} appears twice in one object")
        out[key] = value
    return out


def scan(body: bytes, project: str) -> None:
    """Refuse a body unless it is ASCII JSON whose every key is on the allowlist and whose
    every value is a finite number or a fixed word, the project, a UUID v7, a UTC instant or
    a ref. Run on the bytes handed to the transport: what is sent, not what was meant."""
    try:
        text = body.decode("ascii")
        data = json.loads(text, parse_constant=_refuse_constant, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError) as error:
        raise Leak(f"scan: not ASCII JSON: {error}") from error

    def walk(value: object, where: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in _KEYS:
                    raise Leak(f"scan: key {key!r} at {where} is not allowed")
                walk(item, f"{where}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{where}[{i}]")
        elif isinstance(value, bool) or value is None:
            raise Leak(f"scan: {value!r} at {where} is not a count, id or time")
        elif isinstance(value, int | float):
            if not math.isfinite(value):
                raise Leak(f"scan: not a finite number at {where}")
        elif isinstance(value, str):
            if not _safe_string(value, project):
                raise Leak(f"scan: string at {where} is not a count, id or time")
        else:
            raise Leak(f"scan: unexpected value at {where}")

    walk(data, "$")


# --- the wire ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    method: Literal["GET", "POST"]
    url: str
    body: bytes | None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    """How a request reaches Opik. Raises ``Unreachable`` when nothing answers at all."""

    def __call__(self, request: Request) -> Response: ...


def urllib_transport(request: Request) -> Response:
    """The real wire, from the standard library, imported only when the exporter is on.
    Follows no redirect and uses no proxy (rule 2): a 3xx comes back as itself."""
    import http.client
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
        ) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    headers = {"Accept": "application/json"}
    if request.body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        request.url, data=request.body, method=request.method, headers=headers
    )
    try:
        with opener.open(req, timeout=TIMEOUT_S) as reply:
            return Response(reply.status, dict(reply.headers.items()), reply.read())
    except urllib.error.HTTPError as error:
        with error:
            return Response(error.code, dict(error.headers.items()), error.read())
    except (OSError, http.client.HTTPException) as error:
        host = urlsplit(request.url).netloc
        reason = getattr(error, "reason", error)
        raise Unreachable(f"could not reach Opik at {host}: {reason}") from error


class _Failed(Exception):
    """One mission could not be sent; the run goes on."""


def _send(transport: Transport, base: str, request: Request, project: str) -> Response:
    """Scan, then send. The URL must be one of the three this module calls, and the body is
    scanned as the exact bytes object the transport is handed."""
    path = request.url.removeprefix(base)
    known = path in (SPANS_BATCH, TRACES_BATCH) or (
        path.startswith(f"{TRACES_PATH}/")
        and bool(_UUID7_RE.fullmatch(path.removeprefix(f"{TRACES_PATH}/")))
    )
    if not request.url.startswith(base) or not known:
        raise _Failed(f"scan: refused to call {request.url}")
    if request.body is not None:
        try:
            scan(request.body, project)
        except Leak as error:
            raise _Failed(str(error)) from error
    return transport(request)


def _post(transport: Transport, base: str, path: str, payload: object, project: str) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    reply = _send(transport, base, Request("POST", f"{base}{path}", body), project)
    if not 200 <= reply.status < 300:
        raise _Failed(f"POST {path} answered {reply.status}")


@dataclass(frozen=True)
class Report:
    """What one run did. Nothing in it is persisted: the next run folds and asks again."""

    state: State
    reason: str | None = None
    sent: tuple[str, ...] = ()
    """Mission refs sent this run."""

    present: tuple[str, ...] = ()
    """Mission refs Opik already had, so nothing was sent for them."""

    failed: tuple[tuple[str, str], ...] = ()
    """``(mission ref, why)`` for each mission not sent; the next run tries it again."""


def export(
    records: Iterable[Record],
    environ: Mapping[str, str] | None = None,
    *,
    transport: Transport | None = None,
) -> Report:
    """Send every finished mission in ``records`` that Opik does not have yet.

    Off unless ``WALLE_OPIK=on`` (rule 1), and then nothing else happens: no fold, no import,
    no request. Never writes anything and never raises for anything the server does (rule 5).
    """
    if not enabled(environ):
        return Report("off")
    try:
        base, name = endpoint(environ), project(environ)
    except Refused as error:
        return Report("refused", str(error))
    wire: Transport = transport or urllib_transport

    sent: list[str] = []
    present: list[str] = []
    failed: list[tuple[str, str]] = []
    for trace in traces(records):
        try:
            check = _send(wire, base, Request("GET", f"{base}{TRACES_PATH}/{trace.id}", None), name)
            if check.status == 200:
                present.append(trace.mission)
                continue
            if check.status != 404:
                raise _Failed(f"GET trace answered {check.status}")
            if trace.spans:
                _post(wire, base, SPANS_BATCH, span_payload(trace, name), name)
            _post(wire, base, TRACES_BATCH, trace_payload(trace, name), name)
        except _Failed as error:
            failed.append((trace.mission, str(error)))
        except Unreachable as error:
            return Report("unreachable", str(error), tuple(sent), tuple(present), tuple(failed))
        else:
            sent.append(trace.mission)
    return Report("finished", None, tuple(sent), tuple(present), tuple(failed))
