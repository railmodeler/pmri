"""``LocoNetConnection`` -- the ``SystemConnection`` subclass.

Construct, configure, ``open(...)``: builds the appropriate
:class:`LocoNetPort`, starts the :class:`LocoNetTrafficController`, wires
up bean managers + power + throttle. On ``close()`` everything is torn
down cleanly.

Settings dict:
    {
        "kind": "loopback" | "serial" | "tcp",
        # for serial:
        "port": "COM5",
        "baud": 16457,
        # for tcp:
        "host": "lbserver.local",
        "tcp_port": 1234,
        # for loopback (tests):
        "loopback_port": LoopbackPort,
    }
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from jmri_core.beans.block import BlockManager
from jmri_core.beans.light import LightManager
from jmri_core.beans.memory import MemoryManager
from jmri_core.beans.power import PowerManager
from jmri_core.beans.reporter import ReporterManager
from jmri_core.beans.route import RouteManager
from jmri_core.beans.signal_head import SignalHeadManager
from jmri_core.beans.signal_mast import SignalMastManager
from jmri_core.beans.turnout import TurnoutManager
from jmri_core.hal.connection import ConnectionSettings, SystemConnection
from jmri_core.manager import Manager

from jmri_loconet.beans import (
    LnPowerManager,
    LnSensorManager,
    LnTurnoutManager,
)
from jmri_loconet.consist import LnConsistManager
from jmri_loconet.programmer import LnProgrammer, LnProgrammerManager
from jmri_loconet.slots import SlotManager
from jmri_loconet.throttle import LnThrottleManager
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import LocoNetPort, LoopbackPort, SerialPort, TcpPort


class LocoNetConnection(SystemConnection):
    """LocoNet hardware connection. Prefix ``L``, name ``LocoNet``."""

    name: ClassVar[str] = "LocoNet"
    prefix: ClassVar[str] = "L"

    def __init__(self) -> None:
        super().__init__()
        # These are filled in by open(). The base-class defaults stay in place
        # until then so the connection has well-formed managers if opened with
        # no settings (rare; mainly the test path).
        self._port: LocoNetPort | None = None
        self._tc: LocoNetTrafficController | None = None
        self._slots: SlotManager | None = None
        self._programmer: LnProgrammer | None = None
        self._cs_monitor_task: asyncio.Task[None] | None = None

    # ----- lifecycle --------------------------------------------------------

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        s = settings or {}
        self._port = await _build_port(s)
        self._tc = LocoNetTrafficController(self._port)
        await self._tc.start()
        self._slots = SlotManager(self._tc)

        # Replace the base default managers with our LocoNet-aware ones.
        # ``LnTurnoutManager`` and friends are subclasses of the base managers,
        # but mypy's invariance in attribute typing means we have to ignore
        # the assignment -- the runtime substitution is sound (Liskov).
        self.turnouts = LnTurnoutManager(self.prefix, tc=self._tc)   # type: ignore[assignment]
        self.sensors = LnSensorManager(self.prefix, tc=self._tc)     # type: ignore[assignment]
        throttle_mgr = LnThrottleManager(
            self.prefix, tc=self._tc, slot_manager=self._slots,
        )
        self.throttles = throttle_mgr
        # Rebind the consist manager onto the LocoNet-aware throttle
        # manager. Without this, consist fan-out drives placeholder
        # Throttle beans left over from base ``__init__``, and no
        # OPC_LOCO_SPD frames reach the wire.
        self.consists.rebind_throttles(self.throttles)
        throttle_mgr.start_refresh()
        self.consists = LnConsistManager(   # type: ignore[assignment]
            throttle_mgr, self.prefix, tc=self._tc, slots=self._slots,
        )
        self.power = LnPowerManager(self._tc)
        self._programmer = LnProgrammer(self._tc)
        self.programmers = LnProgrammerManager(self._programmer)
        # Wire the advanced-consist CV19 write path to the LocoNet
        # programmer manager. Without this rebind, ``advanced`` consists
        # go through the base placeholder manager and CV19 never
        # actually programs.
        self.consists.rebind_programmers(self.programmers)

        # CS reachability monitor. LocoNet CS emit OPC_IDLE and other
        # frames continuously; 3s of complete silence is a strong
        # signal the bus is dark (CS off or unplugged). Start
        # pessimistic (base class default is optimistic True) so a
        # home-page render during the monitor's startup grace shows
        # "Off" until we've actually seen a frame, not a false "On".
        self._cs_reachable = False
        self._cs_monitor_task = asyncio.create_task(
            self._cs_reachability_loop(), name="loconet-cs-reachability",
        )

        await super().open(settings)

    async def _cs_reachability_loop(self) -> None:
        """Flip ``_cs_reachable`` on the 3-second inbound-silence rule.

        LocoNet's continuous chatter (CS heartbeat + slot poll traffic)
        means any real silence past 3 seconds indicates the CS has
        stopped talking. Any fresh inbound frame flips reachable back
        to True immediately.
        """
        import contextlib  # noqa: PLC0415
        try:
            # 3-second grace so the first CS heartbeat can arrive.
            await asyncio.sleep(3.0)
            while True:
                await asyncio.sleep(1.0)
                if self._tc is None:
                    return
                last = self._tc.last_inbound_at
                if last is None:
                    await self._set_cs_reachable(False)
                    continue
                age = time.monotonic() - last
                await self._set_cs_reachable(age <= 3.0)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                return

    async def close(self) -> None:
        if isinstance(self.throttles, LnThrottleManager):
            await self.throttles.stop_refresh()
        if self._cs_monitor_task is not None:
            self._cs_monitor_task.cancel()
            import contextlib  # noqa: PLC0415
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._cs_monitor_task
            self._cs_monitor_task = None
        if self._tc is not None:
            await self._tc.aclose()
            self._tc = None
        self._port = None
        await super().close()

    # ----- accessors --------------------------------------------------------

    @property
    def slots(self) -> SlotManager:
        if self._slots is None:
            raise RuntimeError("connection not open")
        return self._slots

    @property
    def traffic(self) -> LocoNetTrafficController:
        if self._tc is None:
            raise RuntimeError("connection not open")
        return self._tc

    # ----- fast clock (slot 123) --------------------------------------------

    async def read_hardware_clock(self):  # noqa: ANN201
        """Read the CS fast clock from slot 123.

        Sends ``OPC_RQ_SL_DATA(slot=123)`` and awaits the paired
        ``OPC_SL_RD_DATA`` reply on the inbound signal. Returns a
        :class:`~jmri_core.beans.fast_clock.HardwareClockState`.

        The :class:`~jmri_server.services.fast_clock.TimeService`
        prefers this method over the software bean whenever the
        connection exposes it, so wiring this on ``LocoNetConnection``
        automatically makes ``/json/time`` reflect the CS clock.
        """
        from jmri_loconet.clock import FAST_CLOCK_SLOT, decode_slot123  # noqa: PLC0415
        from jmri_loconet.messages import OpcRqSlData, OpcSlRdData  # noqa: PLC0415

        if self._tc is None:
            raise RuntimeError("connection not open")
        tc = self._tc
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[OpcSlRdData] = loop.create_future()

        async def on_inbound(msg) -> None:  # noqa: ANN001
            if (
                isinstance(msg, OpcSlRdData)
                and msg.slot == FAST_CLOCK_SLOT
                and not fut.done()
            ):
                fut.set_result(msg)

        unsub = tc.inbound.connect(on_inbound)
        try:
            await tc.send(OpcRqSlData(slot=FAST_CLOCK_SLOT))
            reply = await asyncio.wait_for(fut, timeout=2.0)
        finally:
            unsub()
        return decode_slot123(reply)

    async def _write_hardware_clock(self, state) -> None:  # noqa: ANN001
        """Common write path -- reads the current slot to preserve
        non-clock fields, merges *state*, and sends the write."""
        current = await self.read_hardware_clock()
        # ``current`` gave us the CS's authoritative days / trk / snd
        # bytes indirectly by reading the raw slot; we lose that in
        # the decode. To keep this pass simple we send zeros for those
        # fields -- JMRI Java's LnClockControl does the same on write,
        # relying on the CS to preserve day count internally.
        from jmri_loconet.clock import build_slot123_write  # noqa: PLC0415

        merged = state.__class__(
            hours=state.hours if state.hours is not None else current.hours,
            minutes=state.minutes if state.minutes is not None else current.minutes,
            seconds=state.seconds if state.seconds is not None else current.seconds,
            rate=state.rate if state.rate is not None else current.rate,
            running=state.running if state.running is not None else current.running,
            twenty_four_hour=True,
        )
        msg = build_slot123_write(merged)
        if self._tc is None:
            raise RuntimeError("connection not open")
        await self._tc.send(msg)

    async def set_hardware_clock_time(
        self, hours: int, minutes: int, seconds: int = 0,
    ) -> None:
        from jmri_core.beans.fast_clock import HardwareClockState  # noqa: PLC0415
        current = await self.read_hardware_clock()
        await self._write_hardware_clock(HardwareClockState(
            hours=hours, minutes=minutes, seconds=seconds,
            rate=current.rate, running=current.running,
            twenty_four_hour=True,
        ))

    async def set_hardware_clock_rate(self, rate: float) -> None:
        from jmri_core.beans.fast_clock import HardwareClockState  # noqa: PLC0415
        current = await self.read_hardware_clock()
        await self._write_hardware_clock(HardwareClockState(
            hours=current.hours, minutes=current.minutes,
            seconds=current.seconds,
            rate=rate, running=current.running,
            twenty_four_hour=True,
        ))

    async def start_hardware_clock(self) -> None:
        from jmri_core.beans.fast_clock import HardwareClockState  # noqa: PLC0415
        current = await self.read_hardware_clock()
        # LocoNet uses rate=0 to indicate stopped, so "start" without a
        # remembered rate defaults to 1x (real time). If the CS already
        # has a stored non-zero rate we preserve it.
        rate = current.rate if current.rate > 0 else 1.0
        await self._write_hardware_clock(HardwareClockState(
            hours=current.hours, minutes=current.minutes,
            seconds=current.seconds,
            rate=rate, running=True,
            twenty_four_hour=True,
        ))

    async def stop_hardware_clock(self) -> None:
        from jmri_core.beans.fast_clock import HardwareClockState  # noqa: PLC0415
        current = await self.read_hardware_clock()
        await self._write_hardware_clock(HardwareClockState(
            hours=current.hours, minutes=current.minutes,
            seconds=current.seconds,
            rate=current.rate, running=False,
            twenty_four_hour=True,
        ))

    # ----- manager overrides ------------------------------------------------
    # We only customize turnouts, sensors, and power; everything else uses the
    # base default in-memory managers (suitable since they don't talk to the
    # hardware). Stage 4.5 can add LnLightManager, LnReporterManager, etc.

    def _make_turnout_manager(self) -> TurnoutManager:  # pragma: no cover - overridden in open
        return TurnoutManager(self.prefix)

    # Keep all other _make_*_manager hooks at the inherited in-memory defaults.
    # The Manager generic type-cast below is just to keep mypy happy with the
    # in-memory defaults that aren't LocoNet-aware.
    _GENERIC_HINT: ClassVar[type[Manager[Any]]] = LightManager
    _OTHER_HINTS: ClassVar[tuple[type, ...]] = (
        BlockManager,
        MemoryManager,
        PowerManager,
        ReporterManager,
        RouteManager,
        SignalHeadManager,
        SignalMastManager,
    )


# ----- transport construction -----------------------------------------------


async def _build_port(settings: dict[str, Any]) -> LocoNetPort:
    kind = settings.get("kind", "loopback")
    if kind == "serial":
        port = settings.get("port")
        if not port:
            raise ValueError("serial transport requires 'port'")
        baud = int(settings.get("baud", 16457))
        return await SerialPort.open(port=port, baud=baud)
    if kind == "tcp":
        host = settings.get("host")
        tcp_port = settings.get("tcp_port")
        if not host or tcp_port is None:
            raise ValueError("tcp transport requires 'host' and 'tcp_port'")
        return await TcpPort.open(host=host, port=int(tcp_port))
    if kind == "loopback":
        port = settings.get("loopback_port")
        if not isinstance(port, LoopbackPort):
            raise ValueError(
                "loopback transport requires 'loopback_port' to be a LoopbackPort"
            )
        return port
    raise ValueError(f"unknown LocoNet transport kind: {kind!r}")
