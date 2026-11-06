"""Issue #98's verify criteria, and the cases around them (D114, D72, D55, D117).

  off by default         nothing reads a log or calls a model unless the owner has set
                         ``WALLE_MAINTENANCE=on``, and ``WALLE_ROT=on`` under it; no CLI
                         command and no module in the package calls the pass on its own
  only proposes          the pass writes nothing: every byte under the root is the same after
                         it, the register folds the same, and the module has no way to write.
                         A suggestion changes the register only when a person retires the
                         automation themselves; a machine actor acting on one is refused
  the deterministic      ``walle.rot`` labels with the model pass off and absent, imports
  pass runs without it   nothing of it, and the labels a pass returns are exactly ``rot``'s,
                         whatever the model answers or when it fails

Every automation, title and date here is synthetic: a made-up household, no real data.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from test_constraints import forbidden_in_closure
from walle import automations, maintenance, pseudonyms, rot
from walle.events import (
    ACTOR_CURATOR,
    ACTOR_KERNEL,
    Automation,
    AutomationVerb,
    Event,
    Outcome,
    OutcomeResult,
)
from walle.paths import log_dir
from walle.proposer import ModelCallError, Request
from walle.reader import Record, read_file
from walle.writer import StreamWriter

P = "ines"
D = "mbp"
REG_ON = {automations.AUTOMATIONS_SWITCH: automations.SWITCH_ON}
ROT_ON = {rot.ROT_SWITCH: rot.SWITCH_ON}
MAINT_ON = {maintenance.MAINTENANCE_SWITCH: maintenance.SWITCH_ON}
BOTH = {**ROT_ON, **MAINT_ON}
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
DAY = 120
PACKAGE = Path(__file__).parent.parent / "src" / "walle"
SOURCE = PACKAGE / "maintenance.py"


# --- builders -----------------------------------------------------------------------------


def _ts(days: float) -> str:
    return (T0 + timedelta(days=days)).isoformat(timespec="microseconds")


def _at(days: float) -> datetime:
    return T0 + timedelta(days=days)


def _write(root: Path, event: Event) -> str:
    written = StreamWriter(root, P, D).append(event)
    return f"d-{D}:{written.seq}"


def _add(
    root: Path, name: str, title: str, day: float = 0, *, depends_on: list[str] | None = None
) -> str:
    event = Automation(
        ts=_ts(day),
        actor=P,
        verb=AutomationVerb.ADD,
        name=name,
        title=title,
        depends_on=depends_on,
    )
    return _write(root, event)


def _ran(root: Path, ref: str, day: float) -> str:
    event = Outcome(ts=_ts(day), actor=ACTOR_KERNEL, target=ref, result=OutcomeResult("ok"))
    return _write(root, event)


def _records(root: Path) -> list[Record]:
    log = log_dir(root, P)
    return [r for f in sorted(log.glob("*.jsonl")) for r in read_file(f, f.stem).records]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _household(root: Path) -> dict[str, str]:
    """Seven automations, and what happened to each by day 120.

    water-plants  ran every week: active
    water-herbs   ran on day 110: active, and says the same thing as water-plants
    bin-night     pinned on day 1, never ran: pinned, never shown to the model
    school-run    last ran on day 70: stale
    dog-walk      never ran: archived
    car-service   never ran, but pay-rent names it: held, never shown to the model
    pay-rent      added on day 100, names car-service: active
    """
    refs = {
        "water-plants": _add(root, "water-plants", "water the balcony plants every sunday"),
        "water-herbs": _add(root, "water-herbs", "water the herb plants on sunday"),
        "bin-night": _add(root, "bin-night", "put the bins out on thursday"),
        "school-run": _add(root, "school-run", "drive the school run"),
        "dog-walk": _add(root, "dog-walk", "walk the dog"),
        "car-service": _add(root, "car-service", "book the car service"),
    }
    for day in range(7, DAY, 7):
        _ran(root, refs["water-plants"], day)
    _ran(root, refs["water-herbs"], 110)
    _write(root, Automation(ts=_ts(1), actor=P, verb=AutomationVerb.PIN, target=refs["bin-night"]))
    _ran(root, refs["school-run"], 70)
    refs["pay-rent"] = _add(root, "pay-rent", "pay the rent", 100, depends_on=[refs["car-service"]])
    return refs


# --- models -------------------------------------------------------------------------------


def _inner(wrapped: str) -> str:
    return wrapped.split("\n", 1)[1].rsplit("\n", 1)[0]


def _fields(wrapped: str) -> dict[str, str]:
    lines = _inner(wrapped).split("\n")
    return dict(line.split(": ", 1) for line in lines)


class FakeMaintainer:
    """Deterministic and offline. Suggests retiring what is labelled archived, and
    consolidating two automations whose titles share three words of four letters or more."""

    name = "fake-maintainer"

    def __init__(self) -> None:
        self.requests: list[Request] = []

    @staticmethod
    def _words(title: str) -> set[str]:
        return {w for w in title.lower().split() if len(w) >= 4}

    def complete(self, request: Request) -> str:
        self.requests.append(request)
        items = [(item_id, _fields(text)) for item_id, text in request.items]
        out: list[dict[str, Any]] = []
        for item_id, fields in items:
            if fields["label"].startswith("archived"):
                out.append({"kind": "retire", "items": [item_id], "reason": "nobody uses it"})
        for i, (a_id, a) in enumerate(items):
            for b_id, b in items[i + 1 :]:
                if len(self._words(a["title"]) & self._words(b["title"])) >= 3:
                    out.append(
                        {
                            "kind": "consolidate",
                            "items": [a_id, b_id],
                            "title": "water the plants and herbs on sunday",
                            "reason": "both water plants on sunday",
                        }
                    )
        return json.dumps(out)


class Scripted:
    """Answers every request with the same reply, and records what it was shown."""

    name = "scripted"

    def __init__(self, reply: str | list[Any] | dict[str, Any]) -> None:
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.requests: list[Request] = []

    def complete(self, request: Request) -> str:
        self.requests.append(request)
        return self.reply


class Failing:
    name = "failing"

    def complete(self, request: Request) -> str:
        raise ModelCallError("failing: could not connect")


class Untouchable:
    """A model that must never be asked."""

    name = "untouchable"

    def complete(self, request: Request) -> str:
        pytest.fail("the model was called")


def _suggest(root: Path, model: Any, day: float = DAY, **kw: Any) -> maintenance.Suggestions:
    records = _records(root)
    return maintenance.suggest(
        automations.fold_automations(records), records, _at(day), model, **kw
    )


def _rot(root: Path, day: float = DAY, **kw: Any) -> rot.Rot:
    records = _records(root)
    return rot.rot(automations.fold_automations(records), records, _at(day), **kw)


# --- the pass ------------------------------------------------------------------------------


def test_the_pass_suggests_retirement_and_consolidation(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    result = _suggest(tmp_path, FakeMaintainer())

    assert result.model == "fake-maintainer"
    assert [(s.target, s.name, s.label) for s in result.retire] == [
        (refs["dog-walk"], "dog-walk", "archived")
    ]
    assert result.retire[0].reason == "nobody uses it"
    assert len(result.consolidate) == 1
    merge = result.consolidate[0]
    assert merge.targets == (refs["water-plants"], refs["water-herbs"])
    assert merge.names == ("water-plants", "water-herbs")
    assert merge.title == "water the plants and herbs on sunday"
    assert merge.reason == "both water plants on sunday"
    assert result.dropped == () and result.failures == () and result.withheld == ()
    assert result.requests == 1


def test_each_request_is_enveloped_under_its_own_marker(tmp_path: Path) -> None:
    _household(tmp_path)
    model = FakeMaintainer()
    _suggest(tmp_path, model)
    (request,) = model.requests
    assert request.kind == "maintain"
    for item_id, wrapped in request.items:
        assert wrapped.startswith(f"<<<untrusted:{request.marker}>>>\n")
        assert wrapped.endswith(f"\n<<</untrusted:{request.marker}>>>")
        assert wrapped in request.prompt and item_id in request.prompt
    assert request.prompt.startswith(maintenance.INSTRUCTIONS)


def test_the_same_log_and_model_give_the_same_suggestions(tmp_path: Path) -> None:
    _household(tmp_path)
    first, second = _suggest(tmp_path, FakeMaintainer()), _suggest(tmp_path, FakeMaintainer())
    assert first == second


# --- verify: it only proposes, and nothing changes without approval -----------------------


def test_the_pass_writes_nothing(tmp_path: Path) -> None:
    _household(tmp_path)
    before = _tree(tmp_path)
    register = automations.fold_automations(_records(tmp_path))

    _suggest(tmp_path, FakeMaintainer())
    maintenance.suggestions(tmp_path, P, FakeMaintainer(), now=_at(DAY), environ=BOTH)

    assert _tree(tmp_path) == before  # not a byte, no index, no lock file
    assert automations.fold_automations(_records(tmp_path)) == register


def test_nothing_changes_until_a_person_retires_it(tmp_path: Path) -> None:
    """The suggestion is not an act. A machine acting on it is refused by the register; a
    person's own retire is the approval, and the next pass no longer offers it."""
    refs = _household(tmp_path)
    (suggestion,) = _suggest(tmp_path, FakeMaintainer()).retire
    assert automations.fold_automations(_records(tmp_path)).entry(suggestion.target) is not None
    entry = automations.fold_automations(_records(tmp_path)).entry(suggestion.target)
    assert entry is not None and entry.status == "active"

    with pytest.raises(automations.RefusedError):
        automations.retire(tmp_path, P, D, suggestion.name, actor=ACTOR_CURATOR, environ=REG_ON)
    entry = automations.fold_automations(_records(tmp_path)).entry(suggestion.target)
    assert entry is not None and entry.status == "active"

    automations.retire(tmp_path, P, D, suggestion.name, actor=P, environ=REG_ON)
    entry = automations.fold_automations(_records(tmp_path)).entry(refs["dog-walk"])
    assert entry is not None and entry.status == "retired"
    assert _suggest(tmp_path, FakeMaintainer()).retire == ()


def test_the_module_has_no_way_to_write() -> None:
    """No writer, no append or removal, and from the register only the fold and its types:
    none of ``add``, ``retire``, ``pin``, ``unpin`` or ``restore``. No inbox, no staging."""
    tree = ast.parse(SOURCE.read_text())
    froms = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    modules = {n.module for n in froms}
    plain = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not (modules | plain) & {
        "walle.writer",
        "walle.inbox",
        "walle.staging",
        "walle.index",
        "walle.builder",
        "walle.executor",
    }
    from_walle = {a.name for n in froms if n.module == "walle" for a in n.names}
    assert not from_walle & {"automations", "writer", "inbox", "staging", "index", "builder"}
    assert "walle.automations" not in plain
    from_register = {a.name for n in froms if n.module == "walle.automations" for a in n.names}
    assert from_register <= {"Entry", "Register", "fold_automations"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    writes = {"unlink", "rmdir", "write_text", "write_bytes", "rename", "mkdir", "write"}
    assert not attrs & (writes | {"retire", "pin", "unpin", "restore", "locked", "extend_log"})
    appended = {
        n.func.value.id if isinstance(n.func.value, ast.Name) else ast.dump(n.func.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"append", "extend"}
    }
    lists = {"batch", "items", "withheld", "retirements", "consolidations", "dropped", "failures"}
    assert appended <= lists  # only the module's own lists, never a stream
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not names & {"open", "remove", "StreamWriter", "LockedStream"}
    assert not called & {"add", "retire", "pin", "unpin", "restore", "ask", "answer"}


# --- what the model may see and name -------------------------------------------------------


def test_pinned_and_held_automations_are_never_shown(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    model = FakeMaintainer()
    _suggest(tmp_path, model)
    shown = {item_id for request in model.requests for item_id, _ in request.items}
    prompts = "".join(request.prompt for request in model.requests)
    assert refs["bin-night"] not in shown and "bin-night" not in prompts
    assert refs["car-service"] not in shown and "car-service" not in prompts
    assert shown == {
        refs[n] for n in ("water-plants", "water-herbs", "school-run", "dog-walk", "pay-rent")
    }


def test_a_reply_naming_what_it_was_not_shown_is_dropped(tmp_path: Path) -> None:
    """A model that names the pinned automation, the held one or a made-up ref suggests
    nothing about them: only what it was shown, in the same request, can be named."""
    refs = _household(tmp_path)
    reply = [
        {"kind": "retire", "items": [refs["bin-night"]], "reason": "pinned"},
        {"kind": "retire", "items": [refs["car-service"]], "reason": "held"},
        {"kind": "retire", "items": ["d-mbp:999"], "reason": "made up"},
        {
            "kind": "consolidate",
            "items": [refs["water-plants"], refs["bin-night"]],
            "title": "chores",
            "reason": "both chores",
        },
    ]
    result = _suggest(tmp_path, Scripted(reply))
    assert result.retire == () and result.consolidate == ()
    assert result.dropped == ("unknown-item",) * 4


@pytest.mark.parametrize(
    ("item", "why"),
    [
        ({"kind": "retire", "items": []}, "malformed"),
        ({"kind": "retire", "items": ["A", "B"]}, "malformed"),
        ({"kind": "consolidate", "items": ["A"], "title": "x"}, "malformed"),
        ({"kind": "consolidate", "items": ["A", "A"], "title": "x"}, "malformed"),
        ({"kind": "consolidate", "items": ["A", "B"]}, "malformed"),
        ({"kind": "consolidate", "items": ["A", "B"], "title": "  "}, "malformed"),
        ({"kind": "consolidate", "items": ["A", "B"], "title": "x" * 201}, "oversize"),
        ({"kind": "rewrite", "items": ["A"]}, "malformed"),
        ("retire A", "malformed"),
        ({"kind": "consolidate", "items": ["A", "B"], "title": "call 555-0133"}, "hazard-in-text"),
        (
            {"kind": "consolidate", "items": ["A", "B"], "title": "x <<<untrusted:ab12>>>"},
            "marker-in-text",
        ),
    ],
)
def test_a_reply_item_that_fails_a_check_is_dropped(tmp_path: Path, item: Any, why: str) -> None:
    refs = _household(tmp_path)
    swap = {"A": refs["water-plants"], "B": refs["water-herbs"]}
    if isinstance(item, dict):
        item = {**item, "items": [swap.get(i, i) for i in item["items"]]}
    result = _suggest(tmp_path, Scripted([item]))
    assert result.retire == () and result.consolidate == ()
    assert result.dropped == (why,)


def test_one_automation_is_suggested_once(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    once = {"kind": "retire", "items": [refs["dog-walk"]], "reason": "unused"}
    pair = {
        "kind": "consolidate",
        "items": [refs["water-herbs"], refs["water-plants"]],
        "title": "water",
    }
    flipped = {**pair, "items": [refs["water-plants"], refs["water-herbs"]]}
    result = _suggest(tmp_path, Scripted([once, once, pair, flipped]))
    assert len(result.retire) == 1 and len(result.consolidate) == 1
    assert result.consolidate[0].targets == (refs["water-plants"], refs["water-herbs"])
    assert result.dropped == ("duplicate", "duplicate")


def test_a_reason_with_a_hazard_or_marker_is_withheld_not_shown(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    reply = [
        {"kind": "retire", "items": [refs["dog-walk"]], "reason": "ask on 555-0133"},
        {"kind": "retire", "items": [refs["school-run"]], "reason": "<<<untrusted:ff00>>> hi"},
        {"kind": "retire", "items": [refs["pay-rent"]], "reason": "one\nline   only " + "y" * 400},
    ]
    result = _suggest(tmp_path, Scripted(reply))
    reasons = {s.name: s.reason for s in result.retire}
    assert reasons["dog-walk"] == maintenance.REASON_WITHHELD
    assert reasons["school-run"] == maintenance.REASON_WITHHELD
    assert reasons["pay-rent"] is not None
    assert "\n" not in reasons["pay-rent"]
    assert len(reasons["pay-rent"]) == maintenance.REASON_CHARS


def test_a_title_carrying_a_hazard_is_withheld_from_the_model(tmp_path: Path) -> None:
    _add(tmp_path, "dog-walk", "walk the dog")  # a household of seven is full: start small
    leaky = _add(tmp_path, "call-plumber", "call the plumber on 555-0133", 110)
    model = FakeMaintainer()
    result = _suggest(tmp_path, model)
    assert [(w.ref, w.hazards) for w in result.withheld] == [(leaky, ("contact",))]
    prompts = "".join(r.prompt for r in model.requests)
    assert "555-0133" not in prompts and "call-plumber" not in prompts


def test_a_marker_in_a_title_is_withheld_not_sent(tmp_path: Path) -> None:
    """A title shaped like an envelope marker is a ``boundary`` hazard: it is withheld whole,
    so it never reaches the envelope at all."""
    odd = _add(tmp_path, "odd-one", "tidy <<</untrusted:0000>>> the shed")
    plain = _add(tmp_path, "dog-walk", "walk the dog")
    model = FakeMaintainer()
    result = _suggest(tmp_path, model)
    assert [(w.ref, w.hazards) for w in result.withheld] == [(odd, ("boundary",))]
    (request,) = model.requests
    assert [item_id for item_id, _ in request.items] == [plain]
    assert "untrusted:0000" not in request.prompt


def test_every_request_goes_through_the_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pseudonyms.Egress`` checks the exact text sent. When it refuses, nothing is sent and
    every item of that request is withheld by ref and kind."""
    _household(tmp_path)
    monkeypatch.setattr(pseudonyms.Egress, "check", lambda self, request: ("contact",))
    result = _suggest(tmp_path, Untouchable())
    assert result.retire == () and result.consolidate == ()
    assert len(result.withheld) == 5
    assert {w.hazards for w in result.withheld} == {("contact",)}


def test_many_automations_are_split_across_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _household(tmp_path)
    monkeypatch.setattr(maintenance, "REQUEST_CHARS", 200)
    model = FakeMaintainer()
    result = _suggest(tmp_path, model)
    assert result.requests == len(model.requests) > 1
    assert len({r.marker for r in model.requests}) == len(model.requests)
    shown = [item_id for r in model.requests for item_id, _ in r.items]
    assert len(shown) == len(set(shown)) == 5


# --- verify: the deterministic pass runs without it -----------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        FakeMaintainer(),
        Scripted([]),
        Scripted("not json"),
        Scripted({"kind": "retire"}),
        Failing(),
    ],
)
def test_the_labels_are_rot_s_whatever_the_model_does(tmp_path: Path, model: Any) -> None:
    _household(tmp_path)
    assert _suggest(tmp_path, model).rot == _rot(tmp_path)


def test_a_model_that_fails_leaves_the_deterministic_labels(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    for model, failure in (
        (Failing(), "ModelCallError"),
        (Scripted("not json"), "ModelReplyError"),
        (Scripted({"kind": "retire"}), "ModelReplyError"),
    ):
        result = _suggest(tmp_path, model)
        assert result.retire == () and result.consolidate == ()
        assert result.failures == (failure,)
        label = result.rot.label(refs["dog-walk"])
        assert label is not None and label.status == "archived"


def test_with_nothing_to_show_the_model_is_not_asked(tmp_path: Path) -> None:
    added = _add(tmp_path, "bin-night", "put the bins out")
    _write(tmp_path, Automation(ts=_ts(1), actor=P, verb=AutomationVerb.PIN, target=added))
    result = _suggest(tmp_path, Untouchable())
    assert result.requests == 0 and result.rot == _rot(tmp_path)


def test_rot_labels_with_the_model_pass_off_and_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = _household(tmp_path)
    monkeypatch.delenv(maintenance.MAINTENANCE_SWITCH, raising=False)
    result = rot.labels(tmp_path, P, now=_at(DAY), environ=ROT_ON)
    label = result.label(refs["dog-walk"])
    assert label is not None and label.status == "archived"


def test_rot_imports_nothing_of_the_model_pass() -> None:
    for module in ("rot", "automations"):
        tree = ast.parse((PACKAGE / f"{module}.py").read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names = {
            a.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "walle"
            for a in n.names
        }
        assert "walle.maintenance" not in modules and "maintenance" not in names
    assert forbidden_in_closure(PACKAGE, ("rot",)) == {}


def test_thresholds_reach_the_deterministic_pass(tmp_path: Path) -> None:
    _household(tmp_path)
    kw: dict[str, Any] = {"stale_after": timedelta(days=5), "archive_after": timedelta(days=60)}
    assert _suggest(tmp_path, Scripted([]), **kw).rot == _rot(tmp_path, **kw)
    same = timedelta(days=9)
    with pytest.raises(ValueError):
        _suggest(tmp_path, Untouchable(), stale_after=same, archive_after=same)


def test_a_naive_now_is_refused_before_the_model_is_asked(tmp_path: Path) -> None:
    _household(tmp_path)
    records = _records(tmp_path)
    with pytest.raises(ValueError):
        maintenance.suggest(
            automations.fold_automations(records), records, datetime(2026, 9, 1), Untouchable()
        )


# --- the dormant rule ----------------------------------------------------------------------


def test_the_pass_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(maintenance.MAINTENANCE_SWITCH, raising=False)
    monkeypatch.delenv(rot.ROT_SWITCH, raising=False)
    assert not maintenance.enabled()
    with pytest.raises(maintenance.MaintenanceOffError):
        maintenance.suggestions(tmp_path, P, Untouchable(), now=_at(0))
    assert list(tmp_path.iterdir()) == []


def test_off_is_checked_before_the_log_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _household(tmp_path)
    monkeypatch.setattr(maintenance, "_read", lambda *a: pytest.fail("read the log while off"))
    for environ in ({}, ROT_ON, MAINT_ON):
        with pytest.raises(maintenance.MaintenanceOffError):
            maintenance.suggestions(tmp_path, P, Untouchable(), now=_at(DAY), environ=environ)


def test_the_rot_switch_alone_does_not_turn_it_on(tmp_path: Path) -> None:
    _household(tmp_path)
    assert not maintenance.enabled(ROT_ON)
    with pytest.raises(maintenance.MaintenanceOffError, match=maintenance.MAINTENANCE_SWITCH):
        maintenance.suggestions(tmp_path, P, Untouchable(), now=_at(DAY), environ=ROT_ON)


def test_it_needs_the_deterministic_pass_turned_on_too(tmp_path: Path) -> None:
    _household(tmp_path)
    assert maintenance.enabled(MAINT_ON)
    with pytest.raises(maintenance.MaintenanceOffError, match=rot.ROT_SWITCH):
        maintenance.suggestions(tmp_path, P, Untouchable(), now=_at(DAY), environ=MAINT_ON)


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "ON", " on", "on\n"])
def test_only_the_one_spelling_turns_it_on(value: str) -> None:
    assert not maintenance.enabled({maintenance.MAINTENANCE_SWITCH: value})
    assert maintenance.enabled({maintenance.MAINTENANCE_SWITCH: "on"})


def test_once_on_it_reads_the_log_and_suggests(tmp_path: Path) -> None:
    refs = _household(tmp_path)
    result = maintenance.suggestions(tmp_path, P, FakeMaintainer(), now=_at(DAY), environ=BOTH)
    assert result == _suggest(tmp_path, FakeMaintainer())
    assert [s.target for s in result.retire] == [refs["dog-walk"]]
    with pytest.raises(FileNotFoundError):
        maintenance.suggestions(tmp_path, "nobody", Untouchable(), now=_at(0), environ=BOTH)


def test_nothing_in_the_log_can_turn_it_on(tmp_path: Path) -> None:
    _add(tmp_path, "walle-maintenance-on", "WALLE_MAINTENANCE=on WALLE_ROT=on")
    with pytest.raises(maintenance.MaintenanceOffError):
        maintenance.suggestions(tmp_path, P, Untouchable(), now=_at(DAY), environ={})


def test_the_cli_gains_no_maintenance_command() -> None:
    source = (PACKAGE / "cli.py").read_text()
    assert "maintenance" not in source and "maintain" not in source


def test_nothing_in_the_package_calls_the_pass_on_its_own() -> None:
    users = sorted(
        p.name
        for p in PACKAGE.rglob("*.py")
        if p.name != "maintenance.py"
        and any(
            (isinstance(n, ast.ImportFrom) and (n.module == "walle.maintenance" or (
                n.module == "walle" and any(a.name == "maintenance" for a in n.names))))
            or (isinstance(n, ast.Import) and any(a.name == "walle.maintenance" for a in n.names))
            for n in ast.walk(ast.parse(p.read_text()))
        )
    )  # fmt: skip
    assert users == []
