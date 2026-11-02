"""``NceMonitorConnection`` -- the passive ``SystemConnection`` subclass.

This is the abstraction stress test: a fully read-only driver dropping
into the same ``SystemConnection`` ABC as LocoNet and NCE. The contract
holds because the ABC only requires lifecycle (``open``/``close``) and
shaped manager attributes; whether the managers' beans are commanded or
purely observed is a per-bean concern.

Connection settings:

    {
        "kind": "loopback" | "serial",
        # for serial:
        "port": "COM7",
        "baud": 9600,
        # for loopback (tests):
        "loopback_port": LoopbackPort,
    }
"""
from __future__ import annotations

from typing import Any, ClassVar

from jmri_core.hal.connection import ConnectionSettings, SystemConnection

from jmri_ncemonitor.beans import (
    PassivePowerManager,
    PassiveSensorManager,
    PassiveTurnoutManager,
)
from jmri_ncemonitor.monitor import CabBusMonitor
from jmri_ncemonitor.transport import BytePort, LoopbackPort, SerialPort


class NceMonitorConnection(SystemConnection):
    """Passive NCE Cab Bus monitor. Prefix ``M``, name ``NCE Cab Bus monitor``."""

    name: ClassVar[str] = "NCE Cab Bus monitor"
    prefix: ClassVar[str] = "M"

    def __init__(self) -> None:
        super().__init__()
        self._port: BytePort | None = None
        self._monitor: CabBusMonitor | None = None

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        s = settings or {}
        self._port = await _build_port(s)
        self._monitor = CabBusMonitor(self._port)
        await self._monitor.start()

        self.turnouts = PassiveTurnoutManager(self.prefix, self._monitor)   # type: ignore[assignment]
        self.sensors = PassiveSensorManager(self.prefix, self._monitor)     # type: ignore[assignment]
        self.power = PassivePowerManager()

        await super().open(settings)

    async def close(self) -> None:
        if self._monitor is not None:
            await self._monitor.aclose()
            self._monitor = None
        self._port = None
        await super().close()

    @property
    def monitor(self) -> CabBusMonitor:
        if self._monitor is None:
            raise RuntimeError("connection not open")
        return self._monitor


async def _build_port(settings: dict[str, Any]) -> BytePort:
    kind = settings.get("kind", "loopback")
    if kind == "serial":
        port = settings.get("port")
        if not port:
            raise ValueError("serial transport requires 'port'")
        baud = int(settings.get("baud", 9600))
        return await SerialPort.open(port=port, baud=baud)
    if kind == "loopback":
        loop_port = settings.get("loopback_port")
        if not isinstance(loop_port, LoopbackPort):
            raise ValueError(
                "loopback transport requires 'loopback_port' to be a LoopbackPort"
            )
        return loop_port
    raise ValueError(f"unknown NCEMonitor transport kind: {kind!r}")
