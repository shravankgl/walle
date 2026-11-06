"""The model route: ``provider:model`` through aisuite, ``ollama:`` the local tier (D16, D87).

One string picks the model: ``ollama:llama3.1:8b`` on this machine, ``openrouter:<author>/
<model>:free`` or ``openai:gpt-4o-mini`` elsewhere. Swapping it changes no code. Only the
first colon splits the provider from the model, so a model name that holds a slash and a
colon of its own reaches the provider exactly as written (aisuite's ``split(":", 1)``).

The proposer's ``Model`` is a ``name`` and ``complete(request) -> str``; ``ProviderModel`` is
that, plus ``text(prompt) -> str``, over one aisuite client. It is the one provider adapter:
``walle.evals.models`` re-exports it, so ``walle eval`` and ``walle propose`` reach a
provider through the same call, the same timeout and the same scrubbing (issue #139).

What a call does
----------------
One prompt as one ``user`` message, temperature 0, the reply's text and nothing else. The
client is built with a timeout (``TIMEOUT_SECONDS`` unless given) and **no retries**: the
OpenAI SDK under aisuite's ollama, openrouter and openai providers retries twice by default,
on a 429 among other things, and a free tier may count every failed request against a daily
cap. So a call is made once. When it fails, ``complete`` raises
``walle.proposer.ModelCallError`` saying which of these it was:

- the provider rate-limited the request (HTTP 429): stopped, not retried, with the
  provider's ``Retry-After`` when it sent a number;
- the provider answered with another HTTP error: the status;
- the call timed out: how long it waited with no response (the timeout bounds each wait,
  not the whole call);
- nothing was listening: could not connect;
- anything else: the exception's class name.

Keys
----
The key is aisuite's business: it reads ``OPENROUTER_API_KEY``, ``OPENAI_API_KEY`` and the
like from the environment, and ``ollama:`` needs none. This module never reads a key, and
never repeats a provider's error text, which is where a key would come back (a 401 that
quotes the key it refused). An error carries the model name, a status, a duration or a
class name, and is raised ``from None`` so a traceback does not carry the provider's
exception either. Set-up messages from aisuite (an unknown provider, a missing key) are
passed on after ``redact``: any value of a ``*KEY*``, ``*TOKEN*``, ``*SECRET*`` or
``*PASSWORD*`` variable is masked, and so is a ``user:password@`` in the ``base_url`` the
adapter was given; they name the variable, not its value. Whatever the SDK's constructor
raises, of any class, is a ``ModelUnavailable`` said that way (issue #139): a provider
library's own error type is not a reason to show its text.

``redact`` is public because the ACP client (``walle.acp``) prints a host's error text to
the terminal and masks it the same way.

``aisuite`` is imported inside ``ProviderModel`` and nowhere else, so this module loads with
no provider library, and every other command keeps its start-up time.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

from walle.proposer import FakeModel, ModelCallError, Request

TIMEOUT_SECONDS = 120.0
"""How long one call may take. A 7-8B model on a laptop (D16) can be slow to its first token
after loading; a cloud call that takes this long has failed."""

NAME_CHARS = 64
"""The longest model name the shadow log holds (``ShadowVerdict.model``, ``Seen.model``)."""

_PROVIDER_RE = re.compile(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+")
_SECRET_VAR = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
_USERINFO = re.compile(r"//([^/@\s]+@)")
"""The ``user:password@`` of a URL, with the ``@``: masked whole, so neither half is said."""
_RETRY_AFTER = re.compile(r"\d{1,6}")


class ModelUnavailable(Exception):
    """The model named cannot be used here: the library, the provider or the key is missing.
    Raised before any call is made."""


def redact(text: str, *urls: str | None) -> str:
    """``text`` with the value of every secret-named environment variable masked, and the
    ``user:password@`` of each of ``urls`` (where given) masked wherever it appears."""
    for var, value in os.environ.items():
        if _SECRET_VAR.search(var) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    for url in urls:
        if url:
            for userinfo in _USERINFO.findall(url):
                text = text.replace(userinfo, "[redacted]@")
    return text


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and everything it was raised from or during. aisuite wraps the SDK's error in
    an ``LLMError`` raised inside the ``except``, so the real one is its context."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _why(exc: BaseException, timeout: float) -> str:
    """What went wrong, in words that carry no provider text. Duck-typed on the SDK's
    exception shapes (``status_code``, ``response.headers``, class names), so no provider
    library is imported here."""
    chain = list(_chain(exc))
    for link in chain:
        status = getattr(link, "status_code", None)
        if isinstance(status, int) and status == 429:
            why = (
                "was rate limited by the provider (HTTP 429); stopped, not retried, because a "
                "failed request may count against a daily cap"
            )
            headers = getattr(getattr(link, "response", None), "headers", None)
            after = headers.get("retry-after") if headers is not None else None
            if isinstance(after, str) and _RETRY_AFTER.fullmatch(after.strip()):
                why += f"; the provider says retry after {after.strip()} s"
            return why
        if isinstance(status, int):
            return f"failed: the provider answered HTTP {status}"
    for link in chain:
        names = _names(link)
        if names & {"APITimeoutError", "TimeoutException", "TimeoutError"}:
            return f"timed out: no response for {timeout:g} s"
    for link in chain:
        if _names(link) & {"APIConnectionError", "ConnectError", "ConnectionError"}:
            return "could not connect to the provider; is it running, and is the address right?"
    return f"failed: {type(chain[-1]).__name__}"


class ProviderModel:
    """``provider:model`` through aisuite's OpenAI-shaped chat API.

    ``client`` is injectable so the adapter can be driven with no aisuite at all; when it is
    ``None`` the real one is built on first use (``resolve`` builds it at once). ``base_url``
    points the provider somewhere other than its default, for a self-hosted endpoint or a
    test's fake server; ``timeout`` is in seconds.
    """

    def __init__(
        self,
        name: str,
        *,
        client: Any | None = None,
        base_url: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        if not _PROVIDER_RE.fullmatch(name):
            raise ValueError(f"not a model name: {name!r}; expected provider:model, or fake")
        if len(name) > NAME_CHARS:
            raise ValueError(
                f"model name is {len(name)} characters; the shadow log holds {NAME_CHARS}"
            )
        self._name = name
        self._provider = name.split(":", 1)[0]
        self._client = client
        self._base_url = base_url
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._name

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import aisuite
            from aisuite.provider import ProviderFactory
        except ModuleNotFoundError:
            raise ModelUnavailable(
                f"{self._name} needs the aisuite package, which is not installed"
            ) from None
        if self._provider not in ProviderFactory.get_supported_providers():
            raise ModelUnavailable(f"{self._name}: aisuite has no provider {self._provider!r}")
        # No retries, and a timeout: see the module docstring. Every provider walle routes to
        # takes both; one that does not is refused rather than run with its own defaults.
        config: dict[str, Any] = {"timeout": self._timeout, "max_retries": 0}
        if self._base_url is not None:
            config["base_url"] = self._base_url
        try:
            # aisuite builds a provider on first use. Build it now, so a missing library or
            # key is said before any request; the client then uses this one.
            provider = ProviderFactory.create_provider(self._provider, config)
            client = aisuite.Client()
            client.providers[self._provider] = provider
            self._client = client
        except ImportError:
            raise ModelUnavailable(
                f"{self._name}: the library for the {self._provider} provider is not "
                f"installed (aisuite[{self._provider}])"
            ) from None
        except TypeError:
            raise ModelUnavailable(
                f"{self._name}: the {self._provider} provider does not take a timeout and a "
                "retry limit, so walle does not use it"
            ) from None
        except ValueError as exc:
            raise ModelUnavailable(
                f"{self._name} cannot be used: {redact(str(exc), self._base_url)}"
            ) from None
        except Exception as exc:
            # An SDK's own error class (aisuite's LLMError, a provider's auth error) with
            # whatever it chose to quote: said by its name, its text masked, from nothing.
            raise ModelUnavailable(
                f"{self._name} cannot be used: {type(exc).__name__}: "
                f"{redact(str(exc), self._base_url)}"
            ) from None
        return self._client

    def text(self, prompt: str) -> str:
        client = self._ensure_client()
        try:
            response = client.chat.completions.create(
                model=self._name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
        except Exception as exc:
            raise ModelCallError(f"{self._name} {_why(exc, self._timeout)}") from None
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError, TypeError):
            raise ModelCallError(f"{self._name}: the provider's reply held no message") from None
        return content if isinstance(content, str) else ""

    def complete(self, request: Request) -> str:
        return self.text(request.prompt)


def resolve(name: str) -> FakeModel | ProviderModel:
    """The model for ``--model``: ``fake``, or ``provider:model`` with its client built now,
    so a missing library, provider or key is said here and not inside the run."""
    if name == "fake":
        return FakeModel()
    model = ProviderModel(name)
    model._ensure_client()
    return model
