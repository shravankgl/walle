"""Model adapters for the evals: the fake, and a provider through aisuite (D87).

The proposer's ``Model`` is a ``name`` and ``complete(request) -> str``. The capability eval
needs less: ``text(prompt) -> str``. ``ProviderModel`` is both, over one client.

``aisuite`` is imported inside ``ProviderModel`` and nowhere else, so this module loads with
no key, no network library and no provider SDK on the path. It is not in the project's
dependencies yet (it is on the allowlist in ``tests/test_constraints.py``; adding it is
Shravan's call, with the lockfile change that comes with it). Until then::

    uv run --with aisuite[anthropic] walle eval --model anthropic:claude-...

with the provider's key in the environment under aisuite's own name for it.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from walle.proposer import FakeModel, Request

_PROVIDER_RE = re.compile(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+")


class Completer(Protocol):
    """What the capability eval needs from a model."""

    @property
    def name(self) -> str: ...

    def text(self, prompt: str) -> str: ...


class ModelUnavailable(Exception):
    """The model named cannot be used here: the library or the key is missing."""


class ProviderModel:
    """``provider:model`` through aisuite's OpenAI-shaped chat API.

    One prompt, one ``user`` message, temperature 0, the reply's text and nothing else.
    ``client`` is injectable so the adapter is tested with no network; when it is ``None``
    the real one is built on first use, and a missing library is a ``ModelUnavailable``
    rather than an import error from inside a loop.
    """

    def __init__(self, name: str, *, client: Any | None = None) -> None:
        if not _PROVIDER_RE.fullmatch(name):
            raise ValueError(f"not a model name: {name!r} — expected provider:model, or fake")
        self._name = name
        self._client = client

    @property
    def name(self) -> str:
        return self._name

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import aisuite  # pyright: ignore[reportMissingImports]  # see the module docstring
            except ModuleNotFoundError as exc:
                raise ModelUnavailable(
                    f"{self._name} needs the aisuite package, which is not installed; "
                    "run with `uv run --with aisuite[<provider>] ...`"
                ) from exc
            self._client = aisuite.Client()
        return self._client

    def text(self, prompt: str) -> str:
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=self._name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else ""

    def complete(self, request: Request) -> str:
        return self.text(request.prompt)


def resolve(name: str) -> FakeModel | ProviderModel:
    """The model for ``--model``: ``fake``, or ``provider:model`` (built lazily)."""
    if name == "fake":
        return FakeModel()
    model = ProviderModel(name)
    model._ensure_client()  # fail here, with the reason, not inside the run
    return model
