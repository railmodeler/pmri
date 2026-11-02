"""WebSocket push fan-out.

When a bean changes state, every connected WebSocket client should receive an
unsolicited message with the bean's current data. :class:`PushManager` wires
this up by:

1. Tracking the set of active WebSocket connections.
2. Installing a listener on every bean's ``state_changed`` Signal -- for both
   beans that already exist and beans created later (via the manager's
   ``added`` signal).
3. On any change, formatting the bean through the appropriate service and
   broadcasting the JMRI envelope to every active client.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jmri_core.bean import AbstractNamedBean

if TYPE_CHECKING:
    from fastapi import WebSocket

    from jmri_server.context import ServerContext
    from jmri_server.registry import ServiceRegistry
    from jmri_server.services.beans import _BeanService


class PushManager:
    """Owns the set of WebSocket connections and the listener hookups."""

    def __init__(self, ctx: ServerContext, registry: ServiceRegistry) -> None:
        self._ctx = ctx
        self._registry = registry
        self._clients: set[WebSocket] = set()
        # disconnect callbacks for everything we've subscribed to:
        self._unsubscribes: list[Callable[[], None]] = []
        self._wired = False

    # ---- WebSocket lifecycle ----------------------------------------------

    async def attach(self, ws: WebSocket) -> None:
        """Register a newly accepted WebSocket. Wires up listeners on first attach."""
        if not self._wired:
            self._wire_managers()
            self._wired = True
        self._clients.add(ws)

    async def detach(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send *message* to every active client. Dead sockets are dropped."""
        if not self._clients:
            return
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 -- socket dead
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # ---- listener installation --------------------------------------------

    def _wire_managers(self) -> None:
        """Subscribe to added/removed/state_changed for every bean manager."""
        bean_topics = (
            "turnout", "sensor", "light", "signalHead", "signalMast",
            "memory", "reporter", "block", "route",
        )
        for topic in bean_topics:
            svc = self._registry.get(topic)
            if svc is None:
                continue
            self._wire_bean_service(topic, svc)  # type: ignore[arg-type]

        # Power is a singleton, not a Manager.
        power = self._registry.get("power")
        if power is not None:
            self._unsubscribes.append(
                self._ctx.connection.power.state_changed.connect(
                    self._make_power_listener(power)
                )
            )

        # Command-station power. Drivers with heartbeat detection emit
        # ``command_station_power_changed`` when reachability flips;
        # broadcast so the /home CS badge updates live without polling.
        conn = self._ctx.connection
        cs_signal = getattr(conn, "command_station_power_changed", None)
        if cs_signal is not None:
            async def on_cs_change(on: bool) -> None:
                await self.broadcast({
                    "type": "commandStationPower",
                    "data": {"on": bool(on)},
                })
            self._unsubscribes.append(cs_signal.connect(on_cs_change))

        # Consist manager exposes ``created`` / ``updated`` / ``deleted``
        # signals rather than the added/removed/state_changed trio, and
        # its beans aren't NamedBeans -- so wire it here explicitly.
        # Broadcasts trigger the /consists page's htmx swap and any
        # scripting client subscribed to the ``consist`` topic.
        consist_svc = self._registry.get("consist")
        if consist_svc is not None:
            self._wire_consist_manager()

        # Throttle manager exposes ``acquired`` / ``released`` and each
        # ``Throttle`` has its own ``state_changed`` -- Throttles are
        # NOT NamedBeans so they don't fit ``_wire_bean_service``. Wire
        # them explicitly so that ANY mutation (from the /throttle page,
        # from the consist fan-out, from the WiThrottle bridge, or
        # from a script) broadcasts to every open client. Without this,
        # a consist Forward-button click updates the underlying
        # throttle beans but the /throttle page for a member loco
        # never sees the change.
        throttle_svc = self._registry.get("throttle")
        if throttle_svc is not None:
            self._wire_throttle_manager()

    def _wire_throttle_manager(self) -> None:
        """Broadcast ``{type: "throttle", ...}`` on every throttle mutation.

        Two-tier subscription:
        * ``throttles.acquired`` fires when a new handle appears -- we
          install a per-throttle listener AND broadcast the initial
          state so anyone watching sees the fresh throttle immediately.
        * ``throttles.released`` clears the handle -- broadcast a
          release marker so watchers can drop their local mirror.
        * Each ``Throttle.state_changed`` fans out any speed / direction
          / function change through :func:`_project_throttle`.
        """
        from jmri_core.beans.throttle import Throttle  # noqa: PLC0415
        from jmri_server.services.throttle import _project as _project_throttle  # noqa: PLC0415

        throttles = self._ctx.connection.throttles

        def _install_listener(throttle: Throttle) -> None:
            async def on_change(_payload: Any) -> None:
                await self.broadcast({
                    "type": "throttle",
                    "data": _project_throttle(throttle),
                })
            self._unsubscribes.append(throttle.state_changed.connect(on_change))

        # Cover throttles already acquired before the first client
        # connected (rare -- normally acquire is client-driven -- but
        # possible if a script or the consist fan-out ran during startup).
        for throttle in throttles:
            _install_listener(throttle)

        async def on_acquired(throttle: Throttle) -> None:
            _install_listener(throttle)
            await self.broadcast({
                "type": "throttle",
                "data": _project_throttle(throttle),
            })

        async def on_released(throttle: Throttle) -> None:
            await self.broadcast({
                "type": "throttle",
                "data": _project_throttle(throttle, released=True),
            })

        self._unsubscribes.append(throttles.acquired.connect(on_acquired))
        self._unsubscribes.append(throttles.released.connect(on_released))

    def _wire_consist_manager(self) -> None:
        """Broadcast ``{type: "consist", ...}`` on every create / update /
        delete so the ``/consists`` page's htmx swap fires and any
        scripting client subscribed to the ``consist`` topic is
        notified without polling."""
        from jmri_core.beans.consist import Consist  # noqa: PLC0415
        from jmri_server.services.consist import _project  # noqa: PLC0415

        cm = self._ctx.connection.consists

        async def _emit(method: str, consist: Consist) -> None:
            await self.broadcast({
                "type": "consist", "method": method,
                "data": _project(consist, cm),
            })

        async def on_created(c: Consist) -> None:
            await _emit("put", c)

        async def on_updated(c: Consist) -> None:
            await _emit("post", c)

        async def on_deleted(c: Consist) -> None:
            await _emit("delete", c)

        self._unsubscribes.append(cm.created.connect(on_created))
        self._unsubscribes.append(cm.updated.connect(on_updated))
        self._unsubscribes.append(cm.deleted.connect(on_deleted))

    def _wire_bean_service(self, topic: str, svc: _BeanService) -> None:
        manager = svc.manager()

        # Subscribe state_changed on every bean already in the manager.
        for bean in manager:
            self._subscribe_bean(topic, svc, bean)

        # And subscribe to future additions / removals on the manager itself.
        self._unsubscribes.append(
            manager.added.connect(self._make_added_listener(topic, svc))
        )
        self._unsubscribes.append(
            manager.removed.connect(self._make_removed_listener(topic))
        )

    def _subscribe_bean(
        self, topic: str, svc: _BeanService, bean: AbstractNamedBean
    ) -> None:
        listener = self._make_bean_listener(topic, svc, bean)
        self._unsubscribes.append(bean.state_changed.connect(listener))

    def _make_bean_listener(
        self, topic: str, svc: _BeanService, bean: AbstractNamedBean
    ) -> Callable[[Any], Awaitable[None]]:
        async def listener(_payload: Any) -> None:
            await self.broadcast({
                "type": topic,
                "data": svc.project(bean),
            })
        return listener

    def _make_added_listener(
        self, topic: str, svc: _BeanService
    ) -> Callable[[AbstractNamedBean], Awaitable[None]]:
        async def listener(bean: AbstractNamedBean) -> None:
            self._subscribe_bean(topic, svc, bean)
            await self.broadcast({
                "type": topic,
                "method": "put",
                "data": svc.project(bean),
            })
        return listener

    def _make_removed_listener(
        self, topic: str
    ) -> Callable[[AbstractNamedBean], Awaitable[None]]:
        async def listener(bean: AbstractNamedBean) -> None:
            await self.broadcast({
                "type": topic,
                "method": "delete",
                "data": {"name": bean.system_name},
            })
        return listener

    def _make_power_listener(
        self, svc: JsonService
    ) -> Callable[[Any], Awaitable[None]]:
        async def listener(_payload: Any) -> None:
            data = await svc.on_get(None)
            await self.broadcast({"type": "power", "data": data})
        return listener

    # ---- shutdown ----------------------------------------------------------

    async def aclose(self) -> None:
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()
        # Best-effort: close any lingering sockets.
        import contextlib  # noqa: PLC0415
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self._clients.clear()


# Forward import for type checker on Python 3.12.
from jmri_server.service import JsonService  # noqa: E402, I001

asyncio  # re-export marker -- module name kept available for tests  # noqa: B018
