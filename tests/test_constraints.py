"""Hard constraints as tests, because agents merge without a human read first (R6).

CODEOWNERS covers this file, so an agent cannot weaken it on its own.
"""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "src" / "walle"

# Modules on the resolution path. A later task adds its module's name here if it joins that path.
RESOLUTION_ROOTS = ("events", "paths", "writer", "reader", "resolver", "index")

MODEL_OR_NETWORK = {
    "aisuite",
    "anthropic",
    "google",
    "httpx",
    "litellm",
    "ollama",
    "openai",
    "opik",
    "requests",
}

# Runtime dependencies decided so far (D87, D106). Anything else needs Shravan.
ALLOWED_DEPENDENCIES = {"aisuite", "click", "fastmcp", "pydantic"}

# Optional extras decided so far, each with what it may install. D25 as narrowed on 2026-11-05:
# Opik is optional and off by default, so it is an extra, never a runtime dependency.
ALLOWED_EXTRAS = {"opik": {"opik"}}

# The one module that may import opik (issue #80). Every other module works without it.
OPIK_EXPORTER = "opik_export"


def imports_of(source: str) -> set[str]:
    """Every module named by an import, as `walle.x` for local ones and `pkg` for the rest."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top, _, rest = alias.name.partition(".")
                found.add(f"walle.{rest.split('.')[0]}" if top == "walle" and rest else top)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top, _, rest = module.partition(".")
            if node.level or top == "walle":
                local = (rest if top == "walle" else module).split(".")[0]
                names = [local] if local else [alias.name for alias in node.names]
                found.update(f"walle.{name}" for name in names)
            else:
                found.add(top)
    return found


def forbidden_in_closure(package: Path, roots: tuple[str, ...]) -> dict[str, set[str]]:
    """Follow local imports from `roots`; return each module's model or network imports."""
    seen: set[str] = set()
    pending = [name for name in roots if (package / f"{name}.py").exists()]
    bad: dict[str, set[str]] = {}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        imported = imports_of((package / f"{name}.py").read_text())
        if hits := imported & MODEL_OR_NETWORK:
            bad[name] = hits
        for module in imported:
            if module.startswith("walle.") and (package / f"{module[6:]}.py").exists():
                pending.append(module[6:])
    return bad


def test_nothing_the_resolver_imports_reaches_a_model() -> None:
    """Constraint 3: resolution is a pure function of the log."""
    assert forbidden_in_closure(PACKAGE, RESOLUTION_ROOTS) == {}


def test_the_check_follows_local_imports(tmp_path: Path) -> None:
    """A planted import one hop away must be caught, or the test above proves nothing."""
    (tmp_path / "resolver.py").write_text("from .helper import thing\n")
    (tmp_path / "helper.py").write_text("import anthropic\n")
    assert forbidden_in_closure(tmp_path, ("resolver",)) == {"helper": {"anthropic"}}


def test_the_check_sees_both_local_import_forms(tmp_path: Path) -> None:
    (tmp_path / "resolver.py").write_text("from . import helper\nimport walle.other\n")
    (tmp_path / "helper.py").write_text("from openai import OpenAI\n")
    (tmp_path / "other.py").write_text("import requests\n")
    assert forbidden_in_closure(tmp_path, ("resolver",)) == {
        "helper": {"openai"},
        "other": {"requests"},
    }


def test_runtime_dependencies_are_on_the_allowlist() -> None:
    """Constraint 7 and D87: a new dependency is a decision, not a side effect of a task."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = {
        re.split(r"[\s<>=!~;\[]", spec, maxsplit=1)[0].lower() for spec in project["dependencies"]
    }
    assert names <= ALLOWED_DEPENDENCIES


def package_names(specs: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!~;\[]", spec, maxsplit=1)[0].lower() for spec in specs}


def test_optional_extras_are_on_the_allowlist() -> None:
    """An optional extra is a decision too, not a way round the dependency allowlist."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project.get("optional-dependencies", {})
    assert set(extras) <= set(ALLOWED_EXTRAS)
    for name, specs in extras.items():
        assert package_names(specs) <= ALLOWED_EXTRAS[name]


def importers_of(package: Path, name: str) -> set[str]:
    """Every module under `package` that imports `name`, anywhere in the file, as a dotted path."""
    return {
        ".".join(path.relative_to(package).with_suffix("").parts)
        for path in package.rglob("*.py")
        if name in imports_of(path.read_text())
    }


def test_only_the_exporter_imports_opik() -> None:
    """D25: walle works the same without Opik, so nothing else may reach for it."""
    assert importers_of(PACKAGE, "opik") <= {OPIK_EXPORTER}


def test_the_opik_check_sees_a_lazy_import_in_any_module(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / OPIK_EXPORTER).with_suffix(".py").write_text("import opik\n")
    (tmp_path / "sub" / "lazy.py").write_text("def f():\n    from opik import Opik\n")
    (tmp_path / "clean.py").write_text("import json\n")
    assert importers_of(tmp_path, "opik") == {OPIK_EXPORTER, "sub.lazy"}
