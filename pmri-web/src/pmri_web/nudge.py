"""WebSocket "nudge" fan-out -- tells connected pages that server state
has changed so they can fetch a fresh render instead of polling.

Each nudge carries just ``{topic, data?}`` on the wire. The client-side
``pmri_push.js`` dispatches a DOM CustomEvent per topic; templates
listen via ``hx-trigger="pmri:<topic> from:body"`` and fire their
existing ``hx-get`` in response.

This mechanism replaces the ``hx-trigger="every Xs"`` polling pattern:
the HTTP render pipeline doesn't change, only the trigger. The
server broadcasts a nudge when it knows state actually changed; pages
re-render on demand rather than on a fixed interval.

Why a separate endpoint (not the existing JMRI ``/json`` WebSocket):
the ``/json`` socket is a structured JMRI envelope protocol and
tests pin its exact shape. Mixing nudge frames in there would blur
the contract; a dedicated web-layer socket keeps both concerns clean.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket


_log = logging.getLogger(__name__)


class NudgeHub:
    """Tracks connected ``/ws/nudge`` clients and fans out broadcasts.

    One instance per :class:`WebHandle`. Call sites that mutate server
    state that other clients might be viewing invoke
    :meth:`broadcast_sync` (sync) or :meth:`broadcast` (async) with a
    topic name; every connected client receives a one-line JSON frame
    and the browser-side script dispatches a matching DOM event.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- lifecycle --------------------------------------------------------

    async def attach(self, ws: WebSocket) -> None:
        """Register a newly accepted WebSocket. Idempotent."""
        self._clients.add(ws)
        # Cache the loop so sync callsites (e.g. inside a sync route
        # handler) can still fan out via ``broadcast_sync``.
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

    async def detach(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    # ---- broadcast --------------------------------------------------------

    async def broadcast(self, topic: str, data: dict[str, Any] | None = None) -> None:
        """Send ``{topic, data}`` to every connected client.

        Dead sockets drop silently -- a client reconnect reattaches them
        via :meth:`attach`. Never raises.
        """
        if not self._clients:
            return
        frame = json.dumps({"topic": topic, "data": data or {}})
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(frame)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def broadcast_sync(self, topic: str, data: dict[str, Any] | None = None) -> None:
        """Fan-out from a sync context -- schedule the async broadcast
        on the cached event loop. No-op before any client has connected
        (hub hasn't seen the loop yet)."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(topic, data), self._loop,
            )
        except RuntimeError:
            # Loop closed between the check and the schedule -- drop.
            pass
