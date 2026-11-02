"""``WiThrottleSession`` -- one connected client.

Holds the per-connection state (client name, acquired throttles,
last-heartbeat timestamp) and bridges WiThrottle commands to the
active connection's :class:`ThrottleManager`. The TCP server in
``server.py`` owns one session per accepted socket.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jmri_core.beans.consist_manager import ConsistManager
from jmri_core.beans.throttle import Throttle
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.state import ThrottleDirection

from jmri_withrottle import protocol

_log = logging.getLogger(__name__)

# Speed comes in as 0..126 on WiThrottle (1 is the e-stop sentinel,
# 0 is stop, 2..126 is motion). Our throttle bean uses 0.0..1.0.
WITHROTTLE_SPEED_MAX = 126


SendLine = Callable[[str], Awaitable[None]]

# Handler for turnout throw/close/toggle commanded from a WiThrottle client.
# Called as ``await handler(system_name, action)`` where action is one of
# ``"T"`` (throw), ``"C"`` (close), or ``"2"`` (toggle).
TurnoutActionHandler = Callable[[str, str], Awaitable[None]]

# Handler for route-fire commanded from a WiThrottle client. Called as
# ``await handler(system_name, action)`` where ``action`` echoes the
# client's PRA opcode (``"2"`` = fire; others are no-ops at the JMRI
# contract level).
RouteActionHandler = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class _Acquired:
    throttle_char: str          # "T" or "S"
    address_token: str          # raw client-supplied token
    bean: Throttle              # the underlying core Throttle
    # When the acquired address matches a server-side consist, we
    # remember it here so throttle actions (V / R / F / X) dispatch
    # through the consist manager's fan-out instead of driving only
    # the lead's :class:`Throttle`. Software consists on any driver
    # then reach every member; CS-managed consists on LocoNet keep
    # working via LINK.
    consist_address: int | None = None


@dataclass(eq=False)   # identity-based eq+hash so set() tracking works
class WiThrottleSession:
    """One WiThrottle client connection."""

    throttle_mgr: ThrottleManager
    send_line: SendLine                          # writer to push lines back
    consist_mgr: ConsistManager | None = None
    fast_clock_provider: Callable[[], str | None] | None = None
    turnout_action_handler: TurnoutActionHandler | None = None
    route_action_handler: RouteActionHandler | None = None
    client_name: str = ""
    hardware_id: str = ""
    _acquired: dict[tuple[str, str], _Acquired] = field(default_factory=dict)

    # ----- entry point ----------------------------------------------------

    async def dispatch(self, line: str) -> bool:
        """Process one inbound line. Returns False if the client said Quit."""
        if line and _log.isEnabledFor(logging.DEBUG):
            _log.debug("WiThrottle IN: %r", line)
        msg = protocol.decode_line(line)
        if isinstance(msg, protocol.Heartbeat):
            # Engine Driver and WiThrottle iOS treat the heartbeat as
            # ping/pong -- if the server doesn't echo, the client thinks
            # the link is dead and tears down the TCP session. Always
            # echo back.
            await self.send_line("*")
            # Piggyback a fast-clock resync on the heartbeat so the
            # client's clock stays aligned without needing a separate
            # broadcast timer.
            if self.fast_clock_provider is not None:
                pft = self.fast_clock_provider()
                if pft is not None:
                    await self.send_line(pft)
            return True
        if isinstance(msg, protocol.Quit):
            await self._release_all()
            return False
        if isinstance(msg, protocol.HelloName):
            self.client_name = msg.name
            return True
        if isinstance(msg, protocol.HelloHardwareId):
            self.hardware_id = msg.hardware_id
            return True
        if isinstance(msg, protocol.AcquireLoco):
            await self._acquire(msg)
            return True
        if isinstance(msg, protocol.ReleaseLoco):
            await self._release(msg.throttle, msg.address_token)
            return True
        if isinstance(msg, protocol.ThrottleAction):
            await self._action(msg)
            return True
        if isinstance(msg, protocol.TurnoutAction):
            if self.turnout_action_handler is not None:
                try:
                    await self.turnout_action_handler(msg.system_name, msg.action)
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "WiThrottle turnout %s action %r failed: %s",
                        msg.system_name, msg.action, e,
                    )
            return True
        if isinstance(msg, protocol.RouteAction):
            if self.route_action_handler is not None:
                try:
                    await self.route_action_handler(msg.system_name, msg.action)
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "WiThrottle route %s action %r failed: %s",
                        msg.system_name, msg.action, e,
                    )
            return True
        if isinstance(msg, protocol.Unknown):
            if msg.raw:
                _log.debug("WiThrottle unknown line: %r", msg.raw)
            return True
        return True

    # ----- acquire / release ---------------------------------------------

    async def _acquire(self, msg: protocol.AcquireLoco) -> None:
        try:
            addr, is_long = protocol.parse_address_token(msg.address_token)
        except ValueError:
            _log.warning("WiThrottle bad address token: %r", msg.address_token)
            return
        # Consist first-check: if the address matches a server-side
        # consist, we still acquire a Throttle for the lead so the
        # client's initial state (function labels, speed, direction)
        # renders normally, but subsequent throttle actions fan through
        # the consist manager to every member. Engine Driver /
        # WiThrottle iOS have no wire-level notion of "this is a
        # consist" -- MU support is a server-side transparent thing.
        consist_addr: int | None = None
        if self.consist_mgr is not None:
            consist = self.consist_mgr.get(addr)
            if consist is not None and consist.locos:
                consist_addr = addr
                # Use the lead loco's address for the underlying
                # throttle acquire so the client sees the lead's
                # function/speed state.
                lead = consist.locos[0]
                addr = lead.address
                is_long = lead.long_address
        try:
            bean = await self.throttle_mgr.acquire(addr, long_address=is_long)
        except Exception as e:  # noqa: BLE001
            _log.warning("WiThrottle acquire %s failed: %s", msg.address_token, e)
            return
        self._acquired[(msg.throttle, msg.address_token)] = _Acquired(
            throttle_char=msg.throttle,
            address_token=msg.address_token,
            bean=bean,
            consist_address=consist_addr,
        )
        await self.send_line(
            protocol.throttle_acquired(msg.throttle, msg.address_token)
        )
        # Broadcast the throttle's initial state so Engine Driver /
        # WiThrottle iOS know how to render its buttons. Without the
        # per-function ``F<state><n>`` lines the client doesn't display
        # the function bar at all; without the initial V / R the slider
        # sits at an undefined position.
        await self._broadcast_initial_state(
            msg.throttle, msg.address_token, bean,
        )

    async def _broadcast_initial_state(
        self, throttle: str, address_token: str, bean: Throttle,
    ) -> None:
        """Send the state lines Engine Driver expects after an acquire ack.

        Order matches JMRI's ``ThrottleController.notifyControllerAddressFound``.

        The label line matters and must be present with non-empty labels:

        * Roster acquires (``M+L<addr><;>E<id>``) hide the function bar
          if we skip the labels line -- the client expects roster
          metadata and interprets a missing line as "no functions".
        * Manual acquires (``M+L<addr><;>L<addr>``) hide the bar if we
          send *empty* labels -- the client interprets those as "loco
          has no functions". Non-empty defaults (``F0``..``F28``) work
          for both cases.

        Order sent:

        1. ``M<mux>L<addr><;>]\\[F0]\\[F1]\\[...`` -- function labels
        2. ``F<state><fn>`` -- one line per F0..F28 (function states)
        3. ``V<speed>`` -- initial speed (0..126)
        4. ``R<0|1>`` -- initial direction
        5. ``s<mode>`` -- speed step mode (1 = 128-step NMRA DCC)
        """
        # Labels first, populated with default F0..F28 so both roster
        # and manual acquisitions render the function bar.
        await self.send_line(
            protocol.throttle_function_labels(throttle, address_token)
        )
        # Function states next -- must precede speed/direction or the
        # client hides the function bar.
        for fn in range(protocol.MAX_FUNCTION + 1):
            await self.send_line(
                protocol.throttle_function(
                    throttle, address_token, fn, bean.functions[fn],
                )
            )
        speed_wire = int(round(WITHROTTLE_SPEED_MAX * max(0.0, bean.speed)))
        await self.send_line(
            protocol.throttle_speed(throttle, address_token, speed_wire)
        )
        forward = bean.direction == ThrottleDirection.FORWARD
        await self.send_line(
            protocol.throttle_direction(throttle, address_token, forward)
        )
        # Speed step mode -- 1 = NMRA DCC 128-step (the modern default;
        # NCE, LocoNet, and DCC-EX all speak this).
        await self.send_line(
            protocol.throttle_speed_step_mode(throttle, address_token, 1)
        )

    async def _release(self, throttle: str, address_token: str) -> None:
        # Engine Driver sends the wildcard address ``*`` on the Release
        # button to release every loco on the multiplex slot at once --
        # mirrors the ``*`` semantics on the throttle-action path. If we
        # match only the exact token, ED's Release leaves the previous
        # loco in ``_acquired``; the next acquire on the same slot then
        # coexists with the ghost, and any subsequent broadcast throttle
        # action (``MTA*<;>V64`` or ``R1``) fans to BOTH -- the user
        # sees the newly-selected loco AND the previously-"released"
        # one both move. Verified on NCE hardware 2026-09-18.
        if address_token == "*":
            keys = [
                (t, tok) for (t, tok) in list(self._acquired)
                if t == throttle
            ]
            for t, tok in keys:
                await self._release_one(t, tok)
            return
        await self._release_one(throttle, address_token)

    async def _release_one(self, throttle: str, address_token: str) -> None:
        key = (throttle, address_token)
        a = self._acquired.pop(key, None)
        if a is None:
            return
        # Stop the loco (or every consist member) before returning the
        # throttle to the pool. Without this, the driver's command
        # station keeps refreshing the last commanded speed to the
        # released address -- NCE tracks it in its slot table, LocoNet
        # refreshes from slot memory, DCC-EX from its reminders list.
        # Zeroing speed first lets the CS refresh at 0 (loco stops),
        # and any residual refresh is harmless.
        try:
            if a.consist_address is not None and self.consist_mgr is not None:
                await self.consist_mgr.set_speed(a.consist_address, 0.0)
            else:
                await a.bean.set_speed(0.0)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "WiThrottle release-stop %s failed: %s", address_token, e,
            )
        try:
            await self.throttle_mgr.release(a.bean)
        except Exception as e:  # noqa: BLE001
            _log.warning("WiThrottle release %s failed: %s", address_token, e)
        await self.send_line(protocol.throttle_released(throttle, address_token))

    async def _release_all(self) -> None:
        # Iterate the specific keys so the wildcard path in ``_release``
        # doesn't kick in mid-loop; each iteration goes straight to the
        # single-throttle release.
        for (throttle, token) in list(self._acquired):
            await self._release_one(throttle, token)

    # ----- per-throttle actions ------------------------------------------

    async def _action(self, msg: protocol.ThrottleAction) -> None:
        # WiThrottle allows a broadcast address token ``*`` meaning
        # "every throttle currently acquired on this multiplex
        # character". Engine Driver uses this for slider drags -- so
        # speed commands arrive as ``M0A*<;>V29`` while direction
        # arrives as ``M0AL463<;>R0``. Without ``*`` support, direction
        # works but speed doesn't (the bug we saw on hardware).
        if msg.address_token == "*":
            targets = [
                acq for (t, _tok), acq in self._acquired.items()
                if t == msg.throttle
            ]
        else:
            target = self._acquired.get((msg.throttle, msg.address_token))
            targets = [target] if target is not None else []
        if not targets:
            return
        for a in targets:
            await self._apply_action(a, msg)

    async def _apply_action(
        self, a: _Acquired, msg: protocol.ThrottleAction,
    ) -> None:
        op = msg.action
        if op == "V":
            try:
                wire = int(msg.value)
            except ValueError:
                return
            await self._set_speed(a, wire)
        elif op == "R":
            forward = msg.value == "1"
            direction = (
                ThrottleDirection.FORWARD if forward
                else ThrottleDirection.REVERSE
            )
            if a.consist_address is not None and self.consist_mgr is not None:
                await self.consist_mgr.set_direction(a.consist_address, direction)
            else:
                await a.bean.set_direction(direction)
            await self.send_line(
                protocol.throttle_direction(a.throttle_char, a.address_token, forward)
            )
        elif op in ("F", "f"):
            # Format: state then function number; e.g. "F128" = pressed F28,
            # "F01" = released F1. "F" is press/release toggle, "f" is
            # force-set (which we treat the same for alpha).
            if not msg.value:
                return
            state = msg.value[0] == "1"
            try:
                fn = int(msg.value[1:])
            except ValueError:
                return
            if not 0 <= fn <= protocol.MAX_FUNCTION:
                return
            if a.consist_address is not None and self.consist_mgr is not None:
                await self.consist_mgr.set_function(a.consist_address, fn, state)
            else:
                await a.bean.set_function(fn, state)
            await self.send_line(
                protocol.throttle_function(a.throttle_char, a.address_token, fn, state)
            )
        elif op == "X":
            # E-stop: full reverse direction on JMRI is encoded as speed
            # ``-1`` in our bean; the wire encoding goes via the same
            # set_speed path. Use the bean's emergency_stop helper.
            if a.consist_address is not None and self.consist_mgr is not None:
                await self.consist_mgr.emergency_stop(a.consist_address)
            else:
                await a.bean.emergency_stop()
            await self.send_line(
                protocol.throttle_speed(a.throttle_char, a.address_token, 1)
            )
        elif op == "I":
            # Idle: speed 0 without changing direction.
            if a.consist_address is not None and self.consist_mgr is not None:
                await self.consist_mgr.set_speed(a.consist_address, 0.0)
            else:
                await a.bean.set_speed(0.0)
            await self.send_line(
                protocol.throttle_speed(a.throttle_char, a.address_token, 0)
            )
        # "L" (speed-step mode) accepted silently for alpha.

    async def _set_speed(self, a: _Acquired, wire_speed: int) -> None:
        if wire_speed < 0:
            wire_speed = 0
        if wire_speed > WITHROTTLE_SPEED_MAX:
            wire_speed = WITHROTTLE_SPEED_MAX
        # Speed target: fan through the consist manager when this
        # acquired throttle is bound to a server-side consist.
        if a.consist_address is not None and self.consist_mgr is not None:
            if wire_speed == 0:
                await self.consist_mgr.set_speed(a.consist_address, 0.0)
            elif wire_speed == 1:
                await self.consist_mgr.emergency_stop(a.consist_address)
            else:
                fraction = (wire_speed - 1) / (WITHROTTLE_SPEED_MAX - 1)
                await self.consist_mgr.set_speed(a.consist_address, fraction)
        elif wire_speed <= 1:
            # 0 = stop, 1 = e-stop sentinel; both map to the same throttle bean call.
            if wire_speed == 0:
                await a.bean.set_speed(0.0)
            else:
                await a.bean.emergency_stop()
        else:
            # Map 2..126 onto 0.0..1.0 linearly.
            fraction = (wire_speed - 1) / (WITHROTTLE_SPEED_MAX - 1)
            await a.bean.set_speed(fraction)
        await self.send_line(
            protocol.throttle_speed(a.throttle_char, a.address_token, wire_speed)
        )

    # ----- cleanup -------------------------------------------------------

    async def close(self) -> None:
        """Release all acquired throttles. Called when the socket goes away."""
        await self._release_all()
