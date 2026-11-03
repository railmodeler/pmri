"""``SlotManager`` -- Digitrax LocoNet slot lifecycle.

LocoNet doesn't address locomotives directly; it works through up to ~120
"slots" maintained by the command station. To run a loco at DCC address N,
a client:

1. Sends ``OPC_LOCO_ADR`` for N.
2. Receives ``OPC_SL_RD_DATA`` reporting which slot holds N (or a free slot
   if N wasn't held).
3. Sends ``OPC_MOVE_SLOTS src=src dest=src`` to claim it (this writes the
   ``LOCO_IN_USE`` status).
4. Issues ``OPC_LOCO_SPD/DIRF/SND`` to that slot.
5. Refreshes the slot periodically (``OPC_MOVE_SLOTS src=src dest=src``) to
   prevent the command station from purging it.
6. On release, sends a status update to ``LOCO_COMMON``.

This is the smaller-scope port: enough to acquire a slot, drive a throttle,
and refresh it. Programming-track operations (slot 124) live in a later
module; consist management (``OPC_LINK_SLOTS`` / ``OPC_UNLINK_SLOTS``) is
deferred.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass

from pmri_loconet.messages import (
    LocoNetMessage,
    OpcLocoAdr,
    OpcMoveSlots,
    OpcSlotStat1,
    OpcSlRdData,
)
from pmri_loconet.opcodes import LOCO_COMMON, LOCO_IN_USE, LOCO_STAT_MASK
from pmri_loconet.traffic import LocoNetTrafficController

_log = logging.getLogger(__name__)

# Slot 0 is reserved; slot 124 is the programming-track slot.
PROGRAMMING_SLOT = 124
MAX_USER_SLOT = 119


@dataclass(slots=True)
class Slot:
    """Mutable mirror of one command-station slot."""

    number: int
    address: int = 0
    stat: int = 0
    speed: int = 0
    dirf: int = 0
    snd: int = 0
    trk: int = 0

    @property
    def in_use(self) -> bool:
        return (self.stat & LOCO_STAT_MASK) == LOCO_IN_USE


class SlotManager:
    """Owns the per-slot state mirror and the acquire/release workflow."""

    def __init__(self, tc: LocoNetTrafficController) -> None:
        self._tc = tc
        self._slots: dict[int, Slot] = {}
        self._by_address: dict[int, Slot] = {}
        self._waiters: dict[int, asyncio.Future[Slot]] = {}
        self._inbound_unsub = tc.inbound.connect(self._on_message)

    # ----- acquire / release -----------------------------------------------

    async def acquire(self, dcc_address: int, *, timeout: float = 2.0) -> Slot:
        """Acquire a slot for *dcc_address*. Returns the populated :class:`Slot`.

        If we already hold a slot for the address, return it directly. Otherwise
        send OPC_LOCO_ADR and wait for the command station's OPC_SL_RD_DATA
        response -- which arrives at us via the inbound listener.
        """
        if dcc_address in self._by_address:
            return self._by_address[dcc_address]

        fut: asyncio.Future[Slot] = asyncio.get_event_loop().create_future()
        self._waiters[dcc_address] = fut
        await self._tc.send(OpcLocoAdr(address=dcc_address))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(dcc_address, None)

    async def claim(self, slot: Slot) -> None:
        """Send the null-move that flips the slot to LOCO_IN_USE."""
        await self._tc.send(OpcMoveSlots(src=slot.number, dest=slot.number))

    async def release(self, slot: Slot) -> None:
        """Release the slot cleanly -- mark it ``LOCO_COMMON`` so the
        command station can purge it after its idle timeout without
        losing the loco's speed/direction state.

        The old implementation (``OPC_MOVE_SLOTS src=<n> dest=0``)
        transferred slot ownership to slot 0, which real command
        stations reinterpret as "dispatch this slot" -- the loco
        stops responding to us, keeps the same speed/direction, and
        the CS starts purging it. Worse, the JMRI Java throttle uses
        the *same* transient move-slot pattern to acquire a
        dispatched slot, so an inadvertent release-then-reacquire
        could steal the slot back before the CS actually purged it.
        Matching JMRI Java's :func:`LocoNetSlot.releaseSlot` here.

        Wire: ``OPC_SLOT_STAT1(slot=<n>, stat=(current & ~0x30) |
        LOCO_COMMON)`` -- clears the two loco-status bits and rewrites
        them to LOCO_COMMON. Non-status bits (consist flags,
        DCC-address type, etc.) are preserved.
        """
        released_stat = (slot.stat & ~LOCO_STAT_MASK) | LOCO_COMMON
        await self._tc.send(
            OpcSlotStat1(slot=slot.number, stat=released_stat),
        )
        self._by_address.pop(slot.address, None)

    # ----- mirror access ---------------------------------------------------

    def get(self, number: int) -> Slot | None:
        return self._slots.get(number)

    def by_address(self, address: int) -> Slot | None:
        return self._by_address.get(address)

    def __iter__(self) -> Iterator[Slot]:
        return iter(self._slots.values())

    # ----- inbound listener ------------------------------------------------

    async def _on_message(self, msg: LocoNetMessage) -> None:
        if not isinstance(msg, OpcSlRdData):
            return
        slot = self._slots.get(msg.slot)
        if slot is None:
            slot = Slot(number=msg.slot)
            self._slots[msg.slot] = slot
        # Update the mirror.
        slot.stat = msg.stat
        slot.speed = msg.speed
        slot.dirf = msg.dirf
        slot.snd = msg.snd
        slot.trk = msg.trk
        slot.address = msg.address

        # Maintain the by-address index.
        if msg.address:
            self._by_address[msg.address] = slot

        # Wake any acquire() waiting for this address.
        if msg.address in self._waiters and not self._waiters[msg.address].done():
            self._waiters[msg.address].set_result(slot)


