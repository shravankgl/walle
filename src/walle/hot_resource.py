"""The hot tier as the MCP Resource ``memory://hot-tier``, with an update on every change (D83).

D54 put the hot tier in front of the conversation rather than behind a tool the model may never
call. D83 lists the surfaces that do that, in order: the host's hook (``hook.py``, task 8), then
this, an MCP Resource, for hosts that load resources into context without a hook. A resource is
read by the host, not chosen by the model, so it keeps D54's rule; it is not a fourth tool, and
D102's ``tools/list`` budget does not pay for it.

What a read returns
-------------------
Exactly what the hook hands Claude Code, from ``hook.snapshot``: the same files, the same order,
the same header on ``rules.md``, the same refusal of a symlink or a blank file. Two surfaces
that render the tier separately would drift; this one cannot, because there is one reader.

A read never raises, because a resource that errors is a session with no memory and no reason.
It says what is wrong instead, in a first paragraph walle writes:

``missing``    No hot tier, or an incomplete one (no render yet, a rendered file deleted). The
               text says so, why, and that ``walle render`` makes one. Half a tier is not
               served: ``identity.md`` points at ``learned.md``, so without it the framing lies.
``stale``      The render is older than the log: the fold it was made from (``state`` in
               ``rendered.json``, D66) is not the fold of the log now. The note comes first and
               the render follows it unchanged, because an old memory beats none, and the model
               is told it is old.
``unchecked``  The render is there but its age could not be told: no manifest, or the log does
               not fold right now. Served, with a note that names the kind of failure and never
               quotes it, because a corrupt line is log text and the hot tier is not enveloped.
``current``    No note. The text is byte-for-byte the hook's.

Paths are replaced by ``<root>`` or ``<path>`` (``mcp_server._hide_paths``); the model has no use
for where the log lives.

Checking the age needs the index brought up to date, as ``recall`` does. On a read-only profile
that raises ``PermissionError`` and the age is checked against the index as last built, which is
what ``recall`` answers from too.

The update
----------
A render happens in another process (``walle render``, or a session ending), so the server
cannot be told; it looks. ``Watch`` runs one task per server, started and stopped by the
server's lifespan, that every ``poll`` seconds takes a **non-blocking** shared ``render.lock``,
reads the tier and the manifest, and hashes them. The first hash is taken in the lifespan,
before the server answers anything, so no change after a client connected can be folded into
the starting point. A look that finds a render in progress is skipped rather than counted, and
the first look never waits, so a render that hangs holding the lock cannot keep the server from
starting (D115). When the hash moves, every subscriber hears
``notifications/resources/updated`` for ``memory://hot-tier``:

- on the 2026-07-28 wire, through ``subscriptions/listen``: the event goes on the server's
  ``SubscriptionBus`` and the SDK's ``ListenHandler`` delivers it to the streams that asked for
  this URI;
- on the handshake wire (2025-era), which most hosts still speak, through
  ``resources/subscribe``: the session is kept and told directly. ``resources/unsubscribe``
  drops it, and so does a send that fails, since that session has gone.

"Every render" means every render that changes what a read returns. A render rewrites
``rendered.json`` whenever the fold moved, even when no file did, so every render of a newer log
is seen. A render of an unchanged log writes no byte (R1), there is nothing new to read, and
nothing is sent. An edit to ``rules.md`` changes what the hook would emit, so it is sent too.
The log growing without a render is not seen: knowing it would mean folding the log every poll.
The read says ``stale`` all the same; a host that wants the new facts needs a render, and the
render is then seen.

Two runners, step by step
-------------------------
The watcher W (or a read R) and a renderer X for the same principal.

1. X holds ``render.lock`` exclusively. W's non-blocking request fails at once (``hook.Busy``)
   and W skips this look; no thread waits behind X. R's request blocks until X is done, as the
   hook's does, so R never reads a new ``identity.md`` beside an old ``learned.md``.
2. X releases. W's next look gets the shared lock, reads four files and the manifest, releases,
   hashes. The manifest X wrote is new, so the hash moved and W notifies.
3. R, woken by the notification, reads under the shared lock, then **after releasing it**
   folds the log to check the age. If another render Y lands between the two, R reports the
   text it read as ``stale`` against a newer log, which is true of that text; W sees Y's
   manifest on its next look and notifies again, and R's re-read is current. If an append lands
   after R's fold, R calls the text current one event early; newer is the right way round.

``render.lock`` is released before ``index.update`` takes ``index.lock``, so this module holds
at most one of the two at any time, and the renderer's order (``render.lock`` then
``index.lock``) cannot meet its reverse here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from fastmcp import FastMCP
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ResourceUpdated

from walle import hook, index
from walle.resolver import resolve

URI = "memory://hot-tier"
MIME = "text/markdown"
POLL_SECONDS = 1.0
"""How often the watcher looks. A look is five small reads under a shared lock."""

TASK_NAME = "walle-hot-tier-watch"

MISSING_HEAD = "No hot tier to give you."
STALE_HEAD = "This hot tier is older than the log."
UNCHECKED_HEAD = "This hot tier's age could not be checked."


class Status(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNCHECKED = "unchecked"
    MISSING = "missing"


@dataclass(frozen=True)
class Reading:
    status: Status
    text: str


def read(root: Path, principal: str, hide: Callable[[str], str] | None = None) -> Reading:
    """The resource's text and what state the tier is in. Never raises for a missing,
    incomplete, stale or unreadable tier: it says so.

    ``hide`` turns a message into one with no host path in it (``mcp_server`` passes its
    ``_hide_paths`` bound to the root); without it, messages are left as they are.
    """
    clean = hide or (lambda text: text)
    try:
        text, manifest = hook.snapshot(root, principal, wait=True)
    except hook.Silent as exc:
        return Reading(Status.MISSING, _missing(principal, clean(str(exc))))
    except OSError as exc:  # the lock could not be taken at all (say, ENOLCK on a network disk)
        why = exc.strerror or type(exc).__name__
        return Reading(
            Status.MISSING, _missing(principal, f"the hot tier could not be read: {why}")
        )

    try:
        rendered_from = _state_of(manifest)
    except _NoRecord as exc:
        return Reading(Status.UNCHECKED, _note(UNCHECKED_HEAD, str(exc), principal) + text)
    try:
        now = _current_state(root, principal)
    except Exception as exc:  # any failure to fold is "could not check", never an error
        why = f"the log could not be folded ({type(exc).__name__})"
        return Reading(Status.UNCHECKED, _note(UNCHECKED_HEAD, why, principal) + text)
    if now != rendered_from:
        why = "the log has changed since it was rendered, and the change is not in it"
        return Reading(Status.STALE, _note(STALE_HEAD, why, principal) + text)
    return Reading(Status.CURRENT, text)


class _NoRecord(Exception):
    """The manifest cannot say which fold the render came from."""


def _state_of(manifest: bytes | None) -> str:
    if manifest is None:
        raise _NoRecord("there is no record of the render it came from")
    try:
        data = json.loads(manifest)
    except ValueError:
        raise _NoRecord("the record of the render it came from is unreadable") from None
    state = data.get("state") if isinstance(data, dict) else None
    if not isinstance(state, str):
        raise _NoRecord("the record of the render it came from names no state")
    return state


def _current_state(root: Path, principal: str) -> str:
    """``index.state_hash`` of the log as it is now; on a read-only profile, of the index as
    last built, as ``recall`` answers from."""
    try:
        records = index.update(root, principal).records
    except PermissionError:
        records = index.load_records(root, principal)
    return index.state_hash(resolve(records))


def _note(head: str, why: str, principal: str) -> str:
    return (
        f"{head} {why[0].upper()}{why[1:]}. What follows is still what you remember of "
        f"{principal}; `walle render --principal {principal}` brings it up to date.\n\n"
    )


def _missing(principal: str, why: str) -> str:
    return (
        f"{MISSING_HEAD} walle holds no complete hot tier for {principal} ({why}). Carry on "
        "without it, and do not guess what it would have said. "
        f"`walle render --principal {principal}` makes one.\n"
    )


# --- the watcher ------------------------------------------------------------------------------


class Watch:
    """Looks at the hot tier every ``poll`` seconds and tells subscribers when it changed."""

    def __init__(self, root: Path, principal: str, *, poll: float = POLL_SECONDS) -> None:
        self.root = Path(root)
        self.principal = principal
        self.poll = poll
        self.bus = InMemorySubscriptionBus()
        self.listen = ListenHandler(self.bus)
        self.sessions: set[Any] = set()
        """Handshake-era sessions subscribed to ``URI``."""

    def signature(self) -> str | None:
        """A hash of what a read would find on disk, or ``None`` while a render runs."""
        try:
            text, manifest = hook.snapshot(self.root, self.principal, wait=False)
            seen = b"tier\0" + text.encode("utf-8") + b"\0" + (manifest or b"")
        except hook.Busy:
            return None
        except hook.Silent as exc:
            seen = b"missing\0" + str(exc).encode("utf-8")
        except Exception as exc:  # a failed look is a state, not the end of watching
            # Caught broadly on purpose: an exception escaping here would end the task, and
            # every later render would go unannounced with nothing to say so. A lasting
            # failure hashes the same every time, so it is announced once, when it starts.
            seen = b"error\0" + type(exc).__name__.encode()
        return hashlib.sha256(seen).hexdigest()

    async def run(self, last: str | None) -> None:
        """Look every ``poll`` seconds; publish when the signature moves.

        ``last`` is the signature the lifespan took before the server answered anything, or
        ``None`` if a render held the lock then. In that case the first look that gets the lock
        becomes the starting point and is not announced: nobody had connected while that
        render ran, and a read waits for the lock, so no one can hold the older text.
        """
        while True:
            await asyncio.sleep(self.poll)
            now = await asyncio.to_thread(self.signature)
            if now is None or now == last:
                continue
            if last is not None:
                await self.publish()
            last = now

    async def publish(self) -> None:
        """One ``notifications/resources/updated`` for ``URI`` to every subscriber, both eras."""
        await self.bus.publish(ResourceUpdated(URI))
        for session in list(self.sessions):
            try:
                await session.send_resource_updated(URI)
            except Exception:  # the session has gone; it cannot be told, so stop trying
                self.sessions.discard(session)

    async def subscribe(self, ctx: Any, params: mcp_types.SubscribeRequestParams) -> Any:
        if params.uri == URI:  # any other URI is honoured and never fires, as the spec has it
            self.sessions.add(ctx.session)
        return mcp_types.EmptyResult()

    async def unsubscribe(self, ctx: Any, params: mcp_types.UnsubscribeRequestParams) -> Any:
        if params.uri == URI:
            self.sessions.discard(ctx.session)
        return mcp_types.EmptyResult()

    @contextlib.asynccontextmanager
    async def lifespan(self, _server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        # One look, never a wait: a render that hangs holding the lock must not hold up the
        # server starting (D115's rule, that memory never blocks a session).
        first = await asyncio.to_thread(self.signature)
        task = asyncio.create_task(self.run(first), name=TASK_NAME)
        try:
            yield {}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.listen.close()
            self.sessions.clear()


def publish(server: FastMCP, watch: Watch, hide: Callable[[str], str] | None = None) -> None:
    """Add ``memory://hot-tier`` and both eras' subscription handlers to ``server``.

    The server must have been built with ``lifespan=watch.lifespan``, which runs the watcher.
    """

    @server.resource(URI, name="hot-tier", mime_type=MIME)
    async def hot_tier() -> str:
        """Your memory of the person you work for: the walle hot tier, as the hook gives it."""
        reading = await asyncio.to_thread(read, watch.root, watch.principal, hide)
        return reading.text

    low = server._mcp_server  # pyright: ignore[reportPrivateUsage]
    low.add_request_handler(
        "subscriptions/listen", mcp_types.SubscriptionsListenRequestParams, watch.listen
    )
    low.add_request_handler(
        "resources/subscribe", mcp_types.SubscribeRequestParams, watch.subscribe
    )
    low.add_request_handler(
        "resources/unsubscribe", mcp_types.UnsubscribeRequestParams, watch.unsubscribe
    )
