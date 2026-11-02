"""Slot manager tests -- acquire / release / state mirror."""
from __future__ import annotations

import asyncio

import pytest

from jmri_loconet.messages import (
    LocoNetMessage,
    OpcLocoAdr,
    OpcMoveSlots,
    OpcSlRdData,
)
from jmri_loconet.opcodes import LOCO_IN_USE
from jmri_loconet.slots import SlotManager
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import loopback_pair


async def _fake_command_station(other: LocoNetTrafficController, slot_table: dict[int, int]) -> None:
    """Tiny command-station simulator: when it sees OpcLocoAdr, look up which
    slot holds the address (or pick a free one) and respond with OpcSlRdData."""
    async def listener(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcLocoAdr):
            slot = slot_table.setdefault(msg.address, max(slot_table.values(), default=0) + 1)
            await other.send(OpcSlRdData(
                slot=slot, stat=LOCO_IN_USE, address=msg.address,
                speed=0, dirf=0, trk=0x07, snd=0,
            ))
    other.inbound.connect(listener)


async def test_acquire_returns_populated_slot() -> None:
    a_port, b_port = loopback_pair()
    tc = LocoNetTrafficController(a_port, min_send_interval=0.0)
    cs = LocoNetTrafficController(b_port, min_send_interval=0.0)
    await tc.start()
    await cs.start()
    await _fake_command_station(cs, {})
    sm = SlotManager(tc)

    slot = await sm.acquire(3601)
    assert slot.address == 3601
    assert slot.in_use

    await tc.aclose()
    await cs.aclose()


async def test_acquire_returns_existing_slot_idempotently() -> None:
    a_port, b_port = loopback_pair()
    tc = LocoNetTrafficController(a_port, min_send_interval=0.0)
    cs = LocoNetTrafficController(b_port, min_send_interval=0.0)
    await tc.start()
    await cs.start()
    await _fake_command_station(cs, {})
    sm = SlotManager(tc)

    a = await sm.acquire(3601)
    b = await sm.acquire(3601)
    assert a is b

    await tc.aclose()
    await cs.aclose()


async def test_acquire_timeout_when_no_response() -> None:
    a_port, _b_port = loopback_pair()
    tc = LocoNetTrafficController(a_port, min_send_interval=0.0)
    await tc.start()
    sm = SlotManager(tc)
    with pytest.raises(asyncio.TimeoutError):
        await sm.acquire(123, timeout=0.05)
    await tc.aclose()


async def test_release_unlinks_address_from_slot() -> None:
    a_port, b_port = loopback_pair()
    tc = LocoNetTrafficController(a_port, min_send_interval=0.0)
    cs = LocoNetTrafficController(b_port, min_send_interval=0.0)
    await tc.start()
    await cs.start()
    await _fake_command_station(cs, {})
    sm = SlotManager(tc)

    slot = await sm.acquire(3601)
    assert sm.by_address(3601) is slot
    await sm.release(slot)
    assert sm.by_address(3601) is None

    await tc.aclose()
    await cs.aclose()


async def test_inbound_move_slots_does_not_crash_slot_manager() -> None:
    """SlotManager only cares about OpcSlRdData; other inbound messages are ignored."""
    a_port, _ = loopback_pair()
    tc = LocoNetTrafficController(a_port, min_send_interval=0.0)
    sm = SlotManager(tc)
    # Manually invoke the listener with a non-SL_RD_DATA message.
    await sm._on_message(OpcMoveSlots(src=1, dest=1))
    assert list(sm) == []  # no slots created
