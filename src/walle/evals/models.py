"""The evals' model surface: the fake, and the one provider adapter, ``walle.models`` (D87).

The proposer's ``Model`` is a ``name`` and ``complete(request) -> str``. The capability eval
needs less: ``text(prompt) -> str``, the ``Completer`` protocol here. ``walle.models.
ProviderModel`` is both, over one aisuite client, and it is the only provider adapter in the
package: it makes one call with a timeout and no retries, and an error it raises carries a
status, a duration or a class name and never the provider's own text, which is where a key
comes back (issue #139). This module once held a second ``ProviderModel`` with none of that,
and ``walle eval --model provider:model`` ran through it; now ``ProviderModel``,
``ModelUnavailable`` and ``resolve`` are ``walle.models``'s, re-exported so the evals have
one import and the adapter has one home.

``walle.models`` imports aisuite inside the adapter and nowhere else, so this package still
loads with no key, no network library and no provider SDK on the path.
"""

from __future__ import annotations

from typing import Protocol

from walle.models import ModelUnavailable, ProviderModel, resolve
from walle.proposer import FakeModel


class Completer(Protocol):
    """What the capability eval needs from a model."""

    @property
    def name(self) -> str: ...

    def text(self, prompt: str) -> str: ...


__all__ = ["Completer", "FakeModel", "ModelUnavailable", "ProviderModel", "resolve"]
