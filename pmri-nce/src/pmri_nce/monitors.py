"""Polling tasks that turn NCE's request/response wire into push-style
state-change signals.

NCE doesn't push state changes from the layout -- clients must poll. This
module owns two background tasks:

* :class:`NceTurnoutMonitor` reads the command-station's turnout-status
  memory in 16-byte chunks every ~1.5s, diffs against the previous
  snapshot, and emits state changes through any registered turnout.
* :class:`NceAiuPoller` reads each known AIU module's input word at a
  faster cadence (~250ms) to catch occupancy changes promptly.

Both tasks run inside the connection's lifetime -- started in
:meth:`NceConnection.open` and cancelled in :meth:`NceConnection.close`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pmri_core.state import SensorState, TurnoutState

from pmri_nce.messages import read_accessory_memory, read_aiu

if TYPE_CHECKING:
    from pmri_nce.beans import NceSensorManager, NceTurnoutManager
    from pmri_nce.command_set import NceCommandSet
    from pmri_nce.traffic import NceTrafficController

_log = logging.getLogger(__name__)


class NceTurnoutMonitor:
    """Periodically poll the command-station turnout-status memory."""

    POLL_INTERVAL_S = 1.5
    CHUNK_BYTES = 16          # 16 bytes x 8 turnouts/byte = 128 per chunk

    def __init__(
        self,
        tc: NceTrafficController,
        mgr: NceTurnoutManager,
        cmd_set: NceCommandSet,
    ) -> None:
        self._tc = tc
        self._mgr = mgr
        self._cs = cmd_set
        # Pre-allocated snapshot buffer: the bytes we most recently read.
        self._snapshot = bytearray(cmd_set.turnout_table_bytes)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Run forever until stopped via :meth:`stop`."""
        import contextlib  # noqa: PLC0415
        try:
            while not self._stop.is_set():
                await self._poll_once()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.POLL_INTERVAL_S
                    )
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        self._stop.set()

    async def _poll_once(self) -> None:
        for offset in range(0, self._cs.turnout_table_bytes, self.CHUNK_BYTES):
            addr = self._cs.turnout_memory_base + offset
            try:
                reply = await self._tc.send_command(read_accessory_memory(addr))
            except (TimeoutError, Exception) as e:  # noqa: BLE001
                _log.warning("NCE turnout-monitor poll failed at %#06x: %s", addr, e)
                return
            await self._diff_and_emit(offset, reply.data)

    async def _diff_and_emit(self, offset: int, fresh: bytes) -> None:
        """Compare *fresh* against the snapshot at *offset*; emit per flipped bit."""
        old_chunk = self._snapshot[offset : offset + len(fresh)]
        for i, (old, new) in enumerate(zip(old_chunk, fresh, strict=False)):
            if old == new:
                continue
            changed = old ^ new
            for bit in range(8):
                if not (changed & (1 << bit)):
                    continue
                address = (offset + i) * 8 + bit + 1
                turnout = self._mgr.get_by_address(address)
                if turnout is None:
                    continue
                state = TurnoutState.CLOSED if (new & (1 << bit)) else TurnoutState.THROWN
                await turnout._set_known_state(state)
        self._snapshot[offset : offset + len(fresh)] = fresh


class NceAiuPoller:
    """Periodically poll the configured AIU modules and emit sensor state."""

    POLL_INTERVAL_S = 0.25

    def __init__(
        self,
        tc: NceTrafficController,
        mgr: NceSensorManager,
        aiu_addresses: list[int],
    ) -> None:
        self._tc = tc
        self._mgr = mgr
        self._aius = list(aiu_addresses)
        self._snapshots: dict[int, int] = {}  # AIU address -> last raw 16-bit word
        self._stop = asyncio.Event()

    async def run(self) -> None:
        import contextlib  # noqa: PLC0415
        try:
            while not self._stop.is_set():
                for aiu in self._aius:
                    await self._poll_one(aiu)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.POLL_INTERVAL_S
                    )
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        self._stop.set()

    async def _poll_one(self, aiu: int) -> None:
        try:
            reply = await self._tc.send_command(read_aiu(aiu))
        except (TimeoutError, Exception) as e:  # noqa: BLE001
            _log.warning("NCE AIU-poll failed for %d: %s", aiu, e)
            return
        if len(reply.data) < 2:
            return
        word = (reply.data[0] << 8) | reply.data[1]
        prev = self._snapshots.get(aiu, ~word & 0xFFFF)  # force diff first time
        changed = prev ^ word
        for bit in range(14):                 # 14 inputs per AIU
            if not (changed & (1 << bit)):
                continue
            sensor_address = (aiu - 1) * 14 + bit + 1
            sensor = self._mgr.get_by_address(sensor_address)
            if sensor is None:
                continue
            state = SensorState.ACTIVE if (word & (1 << bit)) else SensorState.INACTIVE
            await sensor._set_known_state(state)
        self._snapshots[aiu] = word
