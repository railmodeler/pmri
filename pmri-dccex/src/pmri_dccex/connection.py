"""``DccexConnection`` -- SystemConnection subclass for DCC-EX command stations."""
from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from pmri_core.hal.connection import ConnectionSettings, SystemConnection

from pmri_dccex.beans import DccexPowerManager, DccexSensorManager, DccexTurnoutManager
from pmri_dccex.codec import DccexMessage
from pmri_dccex.exrail import DccexExRailManager
from pmri_dccex.programmer import DccexProgrammer, DccexProgrammerManager
from pmri_dccex.throttle import DccexThrottleManager
from pmri_dccex.traffic import DccexTrafficController
from pmri_dccex.transport import BytePort, LoopbackPort, SerialPort, TcpPort


class DccexConnection(SystemConnection):
    name: ClassVar[str] = "DCC-EX"
    prefix: ClassVar[str] = "D"

    def __init__(self) -> None:
        super().__init__()
        self._port: BytePort | None = None
        self._tc: DccexTrafficController | None = None
        self._programmer: DccexProgrammer | None = None
        self._cs_monitor_task: asyncio.Task[None] | None = None
        self._cs_ping_task: asyncio.Task[None] | None = None
        self._exrail: DccexExRailManager | None = None

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        s = settings or {}
        self._port = await _build_port(s)
        self._tc = DccexTrafficController(self._port)
        await self._tc.start()

        self.turnouts = DccexTurnoutManager(self.prefix, tc=self._tc)        # type: ignore[assignment]
        self.sensors = DccexSensorManager(self.prefix, tc=self._tc)          # type: ignore[assignment]
        self.throttles = DccexThrottleManager(self.prefix, tc=self._tc)
        # Rebind the consist manager onto the DCC-EX-aware throttle
        # manager. Without this the fan-out drives placeholder beans
        # and no ``<t>`` (throttle) frames reach the CS.
        self.consists.rebind_throttles(self.throttles)
        self.power = DccexPowerManager(self._tc)
        self._programmer = DccexProgrammer(self._tc)
        self.programmers = DccexProgrammerManager(self._programmer)
        # Wire advanced-consist CV19 writes onto the DCC-EX programmer.
        self.consists.rebind_programmers(self.programmers)
        # EX-RAIL automation client. Routes/automations are compiled
        # into the CS firmware; we only introspect + trigger.
        self._exrail = DccexExRailManager(self._tc)

        # CS reachability: DCC-EX doesn't chatter unsolicited, so we
        # ping ``<s>`` (status query) every 2 seconds and watch for
        # ANY inbound message within 3 seconds. Status ping response
        # is ``<p 0>``/``<p 1>`` plus other startup chatter -- any of
        # which resets the monitor's silence timer. Start pessimistic
        # (base class default is optimistic True) so a home-page
        # render during the monitor's startup grace shows "Off" until
        # we've actually seen a reply, not a false "On".
        self._cs_reachable = False
        self._cs_ping_task = asyncio.create_task(
            self._cs_ping_loop(), name="dccex-cs-ping",
        )
        self._cs_monitor_task = asyncio.create_task(
            self._cs_reachability_loop(), name="dccex-cs-reachability",
        )

        await super().open(settings)

    async def _cs_ping_loop(self) -> None:
        """Send ``<s>`` every 2s so the CS keeps replying with status
        traffic -- that's what the reachability monitor watches for."""
        import contextlib  # noqa: PLC0415
        try:
            while True:
                await asyncio.sleep(2.0)
                if self._tc is None:
                    return
                try:
                    await self._tc.send(DccexMessage(command="s", args=()))
                except Exception:  # noqa: BLE001
                    # Send may raise on a closed port; the monitor
                    # loop notices via the silence timer.
                    pass
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                return

    async def _cs_reachability_loop(self) -> None:
        """3-second silence rule against ``last_inbound_at``."""
        import contextlib  # noqa: PLC0415
        try:
            await asyncio.sleep(3.0)   # startup grace
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
        for task in (self._cs_ping_task, self._cs_monitor_task):
            if task is not None:
                task.cancel()
                import contextlib  # noqa: PLC0415
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._cs_ping_task = None
        self._cs_monitor_task = None
        if self._tc is not None:
            await self._tc.aclose()
            self._tc = None
        self._port = None
        self._exrail = None
        await super().close()

    @property
    def traffic(self) -> DccexTrafficController:
        if self._tc is None:
            raise RuntimeError("connection not open")
        return self._tc

    @property
    def exrail(self) -> DccexExRailManager:
        """EX-RAIL manager: inspect compiled routes/automations and
        trigger them at runtime. Routes/automations are authored in
        the CS firmware's ``myAutomation.h``; the client can only
        introspect + control, not upload. Available after :meth:`open`."""
        if self._exrail is None:
            raise RuntimeError("connection not open")
        return self._exrail


async def _build_port(settings: dict[str, Any]) -> BytePort:
    kind = settings.get("kind", "loopback")
    if kind == "serial":
        port = settings.get("port")
        if not port:
            raise ValueError("serial transport requires 'port'")
        baud = int(settings.get("baud", 115200))
        return await SerialPort.open(port=port, baud=baud)
    if kind == "tcp":
        host = settings.get("host")
        tcp_port = settings.get("tcp_port")
        if not host or tcp_port is None:
            raise ValueError("tcp transport requires 'host' and 'tcp_port'")
        return await TcpPort.open(host=host, port=int(tcp_port))
    if kind == "loopback":
        lb = settings.get("loopback_port")
        if not isinstance(lb, LoopbackPort):
            raise ValueError("loopback transport requires 'loopback_port'")
        return lb
    raise ValueError(f"unknown DCC-EX transport kind: {kind!r}")
