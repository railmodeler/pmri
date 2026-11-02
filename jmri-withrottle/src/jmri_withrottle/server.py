"""``WiThrottleServer`` -- accept TCP connections, run a session each.

Usage from a host app::

    server = WiThrottleServer(connection=conn, roster_provider=lambda: [...])
    await server.start(host="0.0.0.0", port=12090)
    ...
    await server.stop()

The server is independent of FastAPI -- it holds its own asyncio
listener -- but it's typically mounted inside the same process as
:mod:`jmri_server` so it shares the bean managers and roster.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jmri_core.beans.route import Route
from jmri_core.beans.turnout import Turnout, TurnoutStateChange
from jmri_core.hal.connection import SystemConnection

from jmri_withrottle import protocol
from jmri_withrottle.session import WiThrottleSession

_log = logging.getLogger(__name__)

DEFAULT_PORT = 12090


@dataclass
class WiThrottleServer:
    """TCP server speaking the WiThrottle line protocol."""

    connection: SystemConnection
    # Callable so the server can be told about roster + power state
    # without us hard-wiring to jmri-server. Returns a list of dicts
    # in the roster JSON shape (id, dccAddress, longAddress).
    roster_provider: Callable[[], list[dict]] = field(default_factory=lambda: list)
    # Callable returning the current fast-clock ``PFT`` sync line, or
    # ``None`` if no fast clock is attached. Called on connect and on
    # every heartbeat so Engine Driver / WiThrottle iOS clocks stay in
    # sync without needing a separate broadcast path.
    fast_clock_provider: Callable[[], str | None] = field(
        default_factory=lambda: (lambda: None),
    )
    # Callable returning the current turnout inventory as a list of
    # ``{"system_name", "user_name", "state"}`` dicts. Sent once per
    # session (after the roster) so Engine Driver / WiThrottle iOS can
    # populate their Turnouts tab.
    turnout_provider: Callable[[], list[dict]] = field(
        default_factory=lambda: list,
    )
    # Handler for turnout throw/close/toggle commands from the client.
    # Called as ``await handler(system_name, action)`` where action is
    # one of ``"T"``, ``"C"``, or ``"2"``. If None, turnout actions from
    # the client are silently dropped.
    turnout_action_handler: (
        Callable[[str, str], Awaitable[None]] | None
    ) = None
    # Callable returning the current route inventory as a list of
    # ``{"system_name", "user_name", "state"}`` dicts. Sent once per
    # session so Engine Driver / WiThrottle iOS populate the Routes
    # tab. Empty list = no routes advertised.
    route_provider: Callable[[], list[dict]] = field(
        default_factory=lambda: list,
    )
    # Handler for route-fire commands from the client. Called as
    # ``await handler(system_name, action)``; action ``"2"`` is JMRI's
    # fire opcode, others are no-ops at the contract level but still
    # dispatched so a custom handler can observe them.
    route_action_handler: (
        Callable[[str, str], Awaitable[None]] | None
    ) = None
    railroad: str = "Python JMRI"
    heartbeat_s: int = protocol.DEFAULT_HEARTBEAT_S
    web_port: int = 8000
    # How often to push a fresh PFT to every connected client. Engine
    # Driver's clock extrapolates from the last sync using the rate;
    # 5 s keeps it within a couple of fast-seconds of the server after
    # rate/state changes without spamming the wire.
    fast_clock_broadcast_s: float = 5.0
    _server: asyncio.Server | None = None
    _sessions: set[WiThrottleSession] = field(default_factory=set)
    _pft_task: asyncio.Task[None] | None = None
    # Disconnect callables from ``state_changed`` subscriptions and
    # the turnout manager's ``added``/``removed`` signals. Filled on
    # ``start()``, drained on ``stop()``.
    _turnout_unsubs: list[Callable[[], None]] = field(default_factory=list)
    # Same for routes.
    _route_unsubs: list[Callable[[], None]] = field(default_factory=list)

    # ----- lifecycle -----------------------------------------------------

    async def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle, host=host, port=port,
        )
        self._pft_task = asyncio.create_task(
            self._pft_broadcast_loop(), name="withrottle-pft-broadcast",
        )
        # Wire turnout state broadcasts: every existing turnout gets a
        # ``state_changed`` listener that fires a PTA line to every
        # connected client. ``added``/``removed`` on the manager keep
        # the subscription set in sync so turnouts created later (via
        # the JSON API, web UI, or a driver monitor) also broadcast.
        # Must run in ``start()`` (post-``conn.open()``), not at
        # construction -- drivers swap in their own turnout manager
        # during ``open()`` and any earlier subscription would leak to
        # the base manager.
        for t in self.connection.turnouts:
            self._turnout_unsubs.append(self._subscribe_turnout(t))
        self._turnout_unsubs.append(
            self.connection.turnouts.added.connect(self._on_turnout_added)
        )
        # Same pattern for routes -- subscribe every existing route's
        # ``state_changed`` + hook ``added`` so routes created later
        # (via JSON API / scripts) also broadcast. ``routes`` isn't
        # part of the base ``SystemConnection`` ABC yet, so guard
        # against drivers that don't expose a route manager.
        routes_mgr = getattr(self.connection, "routes", None)
        if routes_mgr is not None:
            for r in routes_mgr:
                self._route_unsubs.append(self._subscribe_route(r))
            self._route_unsubs.append(
                routes_mgr.added.connect(self._on_route_added)
            )
        _log.info("WiThrottle server listening on %s:%d", host, port)

    def _subscribe_turnout(self, t: Turnout) -> Callable[[], None]:
        """Attach a ``state_changed`` listener to *t* that pushes PTA."""
        async def _listener(change: TurnoutStateChange) -> None:
            # Only ``known`` transitions produce a wire-visible state on
            # the Turnouts tab; commanded transitions still show the
            # previous known state until the layout confirms.
            if change.kind != "known":
                return
            await self.broadcast_turnout_state(
                t.system_name, int(change.current),
            )
        return t.state_changed.connect(_listener)

    async def _on_turnout_added(self, t: Turnout) -> None:
        self._turnout_unsubs.append(self._subscribe_turnout(t))

    def _subscribe_route(self, r: Route) -> Callable[[], None]:
        """Attach a ``state_changed`` listener to *r* that pushes PRA.

        :class:`Route` emits :class:`RouteFired` on a successful
        ``fire()`` call. We translate that into a PRA ACTIVE line so
        the WiThrottle client lights the route briefly. We don't
        emit a follow-up INACTIVE because real JMRI routes are
        one-shot -- the client's UI returns to resting state on its
        own timer.
        """
        async def _listener(_payload: object) -> None:
            await self.broadcast_route_state(
                r.system_name, protocol.ROUTE_ACTIVE,
            )
        return r.state_changed.connect(_listener)

    async def _on_route_added(self, r: Route) -> None:
        self._route_unsubs.append(self._subscribe_route(r))

    async def stop(self) -> None:
        if self._server is None:
            return
        if self._pft_task is not None:
            self._pft_task.cancel()
            import contextlib  # noqa: PLC0415
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pft_task
            self._pft_task = None
        for unsub in self._turnout_unsubs:
            try:
                unsub()
            except Exception as e:  # noqa: BLE001
                _log.debug("turnout unsubscribe failed: %s", e)
        self._turnout_unsubs.clear()
        for unsub in self._route_unsubs:
            try:
                unsub()
            except Exception as e:  # noqa: BLE001
                _log.debug("route unsubscribe failed: %s", e)
        self._route_unsubs.clear()
        for s in list(self._sessions):
            await s.close()
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        _log.info("WiThrottle server stopped")

    async def broadcast_turnout_state(
        self, system_name: str, state: int,
    ) -> None:
        """Push a ``PTA<state><sysName>`` line to every connected session.

        The jmri_server wiring subscribes to each turnout's ``state_changed``
        signal and calls this so the WiThrottle Turnouts tab stays in
        sync with layout state -- whether the change came from another
        WiThrottle client, the web UI, or a physical cab.
        """
        if not self._sessions:
            return
        line = protocol.turnout_state(system_name, state)
        for sess in list(self._sessions):
            try:
                await sess.send_line(line)
            except Exception as e:  # noqa: BLE001
                _log.debug("turnout broadcast to session failed: %s", e)

    async def broadcast_route_state(
        self, system_name: str, state: int,
    ) -> None:
        """Push a ``PRA<state><sysName>`` line to every connected session.

        The jmri_server wiring subscribes to each route's
        ``state_changed`` signal and calls this after :meth:`Route.fire`
        completes so the client's Routes tab briefly shows the route
        as Active, then (via a follow-up broadcast) back to Unknown.
        A client's own PRA fire is reflected the same way -- the
        client sees its request echoed by the server rather than
        acting on its own optimistic state.
        """
        if not self._sessions:
            return
        line = protocol.route_state(system_name, state)
        for sess in list(self._sessions):
            try:
                await sess.send_line(line)
            except Exception as e:  # noqa: BLE001
                _log.debug("route broadcast to session failed: %s", e)

    async def broadcast_roster(self) -> None:
        """Push a fresh ``RL`` to every connected session.

        Engine Driver / WiThrottle iOS clients cache the roster from
        the RL received on connect and do not re-request it. Without a
        broadcast, a locomotive added via the web UI (or a script) is
        invisible to already-connected clients until they disconnect
        and reconnect. The jmri_server wiring subscribes to the
        :class:`RosterStore`'s ``added``/``updated``/``removed`` signals
        and calls this method so every mutation reaches active sessions.
        """
        if not self._sessions:
            return
        roster = self.roster_provider()
        line = protocol.roster_list(roster)
        for sess in list(self._sessions):
            try:
                await sess.send_line(line)
            except Exception as e:  # noqa: BLE001
                _log.debug("roster broadcast to session failed: %s", e)

    async def _pft_broadcast_loop(self) -> None:
        """Periodically push the current PFT to every session.

        Engine Driver clocks extrapolate locally from the last sync line,
        but if the server-side clock state changes (start, stop, rate
        change, big time jump) the client can drift until the next sync.
        A ~15 s broadcast keeps every client aligned within one fast-clock
        second at 8x rate, with negligible wire cost.
        """
        try:
            while True:
                await asyncio.sleep(self.fast_clock_broadcast_s)
                pft = self.fast_clock_provider()
                if pft is None or not self._sessions:
                    continue
                for sess in list(self._sessions):
                    try:
                        await sess.send_line(pft)
                    except Exception as e:  # noqa: BLE001
                        _log.debug("PFT broadcast to session failed: %s", e)
        except asyncio.CancelledError:
            return

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def sockets(self) -> list:
        """Bound sockets -- useful in tests to discover the assigned port."""
        if self._server is None:
            return []
        return list(self._server.sockets)

    # ----- per-connection handler ----------------------------------------

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        _log.info("WiThrottle client connected: %s", peer)

        async def send_line(line: str) -> None:
            try:
                writer.write((line + "\n").encode("utf-8"))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass

        session = WiThrottleSession(
            throttle_mgr=self.connection.throttles,
            send_line=send_line,
            consist_mgr=getattr(self.connection, "consists", None),
            fast_clock_provider=self.fast_clock_provider,
            turnout_action_handler=self.turnout_action_handler,
            route_action_handler=self.route_action_handler,
        )
        self._sessions.add(session)
        try:
            # Greeting + initial state.
            for line in protocol.greeting(
                name=self.railroad,
                heartbeat_s=self.heartbeat_s,
                web_port=self.web_port,
            ):
                await send_line(line)
            # Always send RL even when the roster is empty ("RL0").
            # JMRI Java does the same, and Engine Driver treats "no RL
            # received" differently from "RL0 received": the former
            # leaves the roster tab in an ambiguous "not loaded" state,
            # the latter shows a definitive empty roster the operator
            # can populate from the web UI.
            roster = self.roster_provider()
            _log.info(
                "WiThrottle client connected; sending RL with %d entries",
                len(roster),
            )
            await send_line(protocol.roster_list(roster))
            # Push the initial fast-clock reading so the client's clock
            # doesn't sit at 12:00 PM waiting for the first heartbeat.
            pft = self.fast_clock_provider()
            if pft is not None:
                await send_line(pft)
            # Turnout state labels + inventory so the Turnouts tab renders.
            # PTT (titles) must precede PTL (list) so the client already
            # knows how to render each numeric state code. We skip both
            # when the connection has no turnouts to avoid confusing
            # clients that hide the Turnouts tab on an empty PTT.
            turnouts = self.turnout_provider()
            if turnouts:
                await send_line(protocol.turnout_titles())
                await send_line(protocol.turnout_list(turnouts))
            # Routes tab: same pattern -- PRT titles before PRL list,
            # skip both when the connection has no routes so Engine
            # Driver / WiThrottle iOS hide the Routes tab cleanly.
            routes = self.route_provider()
            if routes:
                await send_line(protocol.route_titles())
                await send_line(protocol.route_list(routes))

            # Read loop.
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                keep_going = await session.dispatch(line)
                if not keep_going:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _log.warning("WiThrottle session error from %s: %s", peer, e)
        finally:
            await session.close()
            self._sessions.discard(session)
            writer.close()
            with _suppress_close():
                await writer.wait_closed()
            _log.info("WiThrottle client disconnected: %s", peer)


class _suppress_close:
    def __enter__(self) -> None: ...
    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        # Swallow close-time errors; the connection's already gone.
        return exc_type is not None and issubclass(
            exc_type, (ConnectionError, OSError),
        )
