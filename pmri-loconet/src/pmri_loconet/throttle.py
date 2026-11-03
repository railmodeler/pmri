"""``LnThrottle`` + ``LnThrottleManager`` -- LocoNet throttle handling.

Acquires a slot via :class:`SlotManager`, then routes speed/direction/function
changes to ``OPC_LOCO_SPD`` / ``OPC_LOCO_DIRF`` / ``OPC_LOCO_SND`` against
that slot. F9-F28 are sent as raw NMRA function-group packets wrapped in
``OPC_IMM_PACKET`` (see :mod:`pmri_loconet.dcc`).

Function packing (matches Digitrax's DIRF byte layout):
    bit 5 = direction (0 = forward, 1 = reverse)
    bit 4 = F0 (headlight)
    bits 3..0 = F4..F1
And SND:
    bits 3..0 = F8..F5

The :class:`LnThrottleManager` runs a periodic *slot refresh task* --
every ~90 seconds it sends a null-move on every active slot, so the
command station doesn't purge them during long ops sessions.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pmri_core.beans.throttle import Throttle
from pmri_core.beans.throttle_manager import ThrottleManager
from pmri_core.state import ThrottleDirection

from pmri_loconet.dcc import function_high_packet_dcc, wrap_in_imm_packet
from pmri_loconet.messages import OpcLocoDirF, OpcLocoSnd, OpcLocoSpd, OpcMoveSlots, OpcUnknown
from pmri_loconet.opcodes import OPC_IMM_PACKET
from pmri_loconet.slots import Slot, SlotManager
from pmri_loconet.traffic import LocoNetTrafficController

_log = logging.getLogger(__name__)

# CS purges an idle slot after ~120s; refresh well before that.
SLOT_REFRESH_INTERVAL_S = 90.0


def pack_dirf(direction: ThrottleDirection, functions: list[bool]) -> int:
    """Pack direction + F0..F4 into the DIRF byte."""
    byte = 0
    if direction == ThrottleDirection.REVERSE:
        byte |= 0x20
    if len(functions) >= 1 and functions[0]:
        byte |= 0x10  # F0
    if len(functions) >= 5 and functions[4]:
        byte |= 0x08  # F4
    if len(functions) >= 4 and functions[3]:
        byte |= 0x04  # F3
    if len(functions) >= 3 and functions[2]:
        byte |= 0x02  # F2
    if len(functions) >= 2 and functions[1]:
        byte |= 0x01  # F1
    return byte


def pack_snd(functions: list[bool]) -> int:
    """Pack F5..F8 into the SND byte."""
    byte = 0
    if len(functions) >= 9 and functions[8]:
        byte |= 0x08  # F8
    if len(functions) >= 8 and functions[7]:
        byte |= 0x04  # F7
    if len(functions) >= 7 and functions[6]:
        byte |= 0x02  # F6
    if len(functions) >= 6 and functions[5]:
        byte |= 0x01  # F5
    return byte


def speed_to_wire(speed: float) -> int:
    """Map [-1.0, 1.0] throttle speed to LocoNet's 0..127 wire form.

    -1.0 is emergency stop (wire 1); 0.0 is stop (wire 0); positive maps
    linearly into 2..127. Negative non-emergency values (direction is set
    separately) are treated as |speed|.
    """
    if speed == -1.0:
        return 1
    if speed == 0.0:
        return 0
    return min(127, max(2, round(abs(speed) * 127)))


class LnThrottle(Throttle):
    """A LocoNet throttle bound to one acquired slot."""

    def __init__(self, address: int, slot: Slot, tc: LocoNetTrafficController,
                 long_address: bool = False) -> None:
        super().__init__(address=address, long_address=long_address)
        self._slot = slot
        self._tc = tc

    @property
    def slot(self) -> Slot:
        return self._slot

    async def set_speed(self, speed: float) -> None:
        await super().set_speed(speed)
        await self._tc.send(OpcLocoSpd(slot=self._slot.number, speed=speed_to_wire(speed)))

    async def set_direction(self, direction: ThrottleDirection) -> None:
        await super().set_direction(direction)
        await self._push_dirf()

    async def set_function(self, index: int, on: bool) -> None:
        await super().set_function(index, on)
        if index <= 4:
            await self._push_dirf()
        elif index <= 8:
            await self._tc.send(OpcLocoSnd(slot=self._slot.number, snd=pack_snd(self.functions)))
        elif index <= 20:
            await self._push_func_group(group=3)
        elif index <= 28:
            await self._push_func_group(group=4)

    async def _push_func_group(self, *, group: int) -> None:
        """Build an NMRA function-high DCC packet and ship it via OPC_IMM_PACKET."""
        dcc = function_high_packet_dcc(
            address=self.address,
            long_address=self.long_address,
            functions=self.functions,
            group=group,
        )
        body = wrap_in_imm_packet(dcc, repeat=2)
        # Send as a raw OpcUnknown with the IMM_PACKET opcode -- the
        # traffic controller will compute and append the checksum.
        # The first byte of body is already the opcode; strip it so
        # OpcUnknown's payload contains just the rest.
        assert body[0] == OPC_IMM_PACKET
        await self._tc.send(OpcUnknown(opcode=OPC_IMM_PACKET, payload=body[1:]))

    async def _push_dirf(self) -> None:
        await self._tc.send(OpcLocoDirF(
            slot=self._slot.number,
            dirf=pack_dirf(self.direction, self.functions),
        ))


class LnThrottleManager(ThrottleManager):
    """LocoNet throttle manager -- extends the core ThrottleManager.

    Override hook :meth:`_construct` acquires a slot via :class:`SlotManager`
    before handing the throttle back. :meth:`release` is overridden to
    return the slot to the command station as well as drop the bookkeeping.

    Owns the slot-refresh task: periodically sends a null-move
    (``OPC_MOVE_SLOTS src=N dest=N``) on every active slot so the CS
    doesn't purge it during long operations.
    """

    def __init__(
        self,
        system_prefix: str,
        *,
        tc: LocoNetTrafficController,
        slot_manager: SlotManager,
        refresh_interval: float = SLOT_REFRESH_INTERVAL_S,
    ) -> None:
        super().__init__(system_prefix)
        self._tc = tc
        self._slots = slot_manager
        self._refresh_interval = refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_stop = asyncio.Event()

    async def _construct(self, address: int, *, long_address: bool) -> LnThrottle:
        slot = await self._slots.acquire(address)
        await self._slots.claim(slot)
        return LnThrottle(
            address=address, slot=slot, tc=self._tc, long_address=long_address
        )

    async def release(self, throttle: Throttle) -> None:
        if isinstance(throttle, LnThrottle):
            await self._slots.release(throttle.slot)
        await super().release(throttle)

    # ----- slot refresh task ------------------------------------------------

    def start_refresh(self) -> None:
        """Start the periodic slot-refresh background task. Idempotent."""
        if self._refresh_task is not None:
            return
        self._refresh_stop.clear()
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="loconet-slot-refresh",
        )

    async def stop_refresh(self) -> None:
        """Stop the slot-refresh task. Idempotent."""
        if self._refresh_task is None:
            return
        self._refresh_stop.set()
        self._refresh_task.cancel()
        import contextlib  # noqa: PLC0415
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._refresh_task
        self._refresh_task = None

    async def _refresh_loop(self) -> None:
        import contextlib  # noqa: PLC0415
        try:
            while not self._refresh_stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._refresh_stop.wait(),
                        timeout=self._refresh_interval,
                    )
                if self._refresh_stop.is_set():
                    return
                await self._refresh_all_slots()
        except asyncio.CancelledError:
            return

    async def _refresh_all_slots(self) -> None:
        """Send a null-move for every currently-active throttle's slot."""
        for throttle in list(self):
            if not isinstance(throttle, LnThrottle):
                continue
            try:
                await self._tc.send(OpcMoveSlots(
                    src=throttle.slot.number, dest=throttle.slot.number,
                ))
            except Exception as e:  # noqa: BLE001
                _log.warning("slot refresh failed for slot %d: %s", throttle.slot.number, e)


# Touch unused import to keep ruff happy until the next change uses it.
_ = (Callable,)
