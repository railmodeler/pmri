"""Throttle tests."""
from __future__ import annotations

import asyncio

from jmri_core.state import ThrottleDirection

from jmri_loconet.messages import (
    LocoNetMessage,
    OpcLocoAdr,
    OpcLocoDirF,
    OpcLocoSnd,
    OpcLocoSpd,
    OpcMoveSlots,
    OpcSlotStat1,
    OpcSlRdData,
)
from jmri_loconet.opcodes import LOCO_COMMON, LOCO_IN_USE, LOCO_STAT_MASK
from jmri_loconet.slots import SlotManager
from jmri_loconet.throttle import LnThrottleManager, pack_dirf, pack_snd, speed_to_wire
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import loopback_pair


# --- pure packing helpers ---------------------------------------------------


def test_pack_dirf_direction_and_functions() -> None:
    # Forward + F0 (headlight)
    funcs = [True] + [False] * 31
    assert pack_dirf(ThrottleDirection.FORWARD, funcs) == 0x10
    # Reverse + F0 + F4
    funcs = [True, False, False, False, True] + [False] * 27
    assert pack_dirf(ThrottleDirection.REVERSE, funcs) == 0x20 | 0x10 | 0x08
    # No functions, forward
    assert pack_dirf(ThrottleDirection.FORWARD, [False] * 32) == 0


def test_pack_snd_packs_f5_through_f8() -> None:
    funcs = [False] * 5 + [True, False, True, False] + [False] * 23  # F5, F7
    assert pack_snd(funcs) == 0x01 | 0x04


def test_speed_to_wire_extremes() -> None:
    assert speed_to_wire(0.0) == 0
    assert speed_to_wire(-1.0) == 1     # emergency stop
    assert speed_to_wire(1.0) == 127
    # Mid-speed: scaled
    assert speed_to_wire(0.5) == 64
    # Negative non-emergency uses absolute (direction is a separate field).
    assert speed_to_wire(-0.5) == 64


# --- end-to-end throttle ----------------------------------------------------


async def _build_pair_with_slot_for(address: int):
    """Build two TCs + a slot manager and pre-populate the slot for *address*."""
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    layout = LocoNetTrafficController(b, min_send_interval=0.0)
    await tc.start()
    await layout.start()
    slot_table: dict[int, int] = {}

    async def listener(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcLocoAdr):
            slot = slot_table.setdefault(msg.address, max(slot_table.values(), default=0) + 1)
            await layout.send(OpcSlRdData(
                slot=slot, stat=LOCO_IN_USE, address=msg.address,
                speed=0, dirf=0, trk=0x07, snd=0,
            ))

    layout.inbound.connect(listener)

    captured: list[LocoNetMessage] = []
    layout.inbound.connect(captured.append)

    sm = SlotManager(tc)
    tm = LnThrottleManager("L", tc=tc, slot_manager=sm)
    return tc, layout, tm, captured


async def test_throttle_acquire_then_set_speed_sends_opc_loco_spd() -> None:
    tc, layout, tm, captured = await _build_pair_with_slot_for(3601)
    th = await tm.acquire(3601)
    await th.set_speed(0.5)

    for _ in range(20):
        if any(isinstance(m, OpcLocoSpd) for m in captured):
            break
        await asyncio.sleep(0.01)
    spds = [m for m in captured if isinstance(m, OpcLocoSpd)]
    assert spds, "expected OPC_LOCO_SPD on the wire"
    assert spds[-1].slot == th.slot.number
    assert spds[-1].speed == 64

    await tc.aclose()
    await layout.aclose()


async def test_throttle_set_direction_sends_dirf() -> None:
    tc, layout, tm, captured = await _build_pair_with_slot_for(3601)
    th = await tm.acquire(3601)
    await th.set_direction(ThrottleDirection.REVERSE)

    for _ in range(20):
        if any(isinstance(m, OpcLocoDirF) for m in captured):
            break
        await asyncio.sleep(0.01)
    dirfs = [m for m in captured if isinstance(m, OpcLocoDirF)]
    assert dirfs and dirfs[-1].dirf & 0x20  # reverse bit set

    await tc.aclose()
    await layout.aclose()


async def test_throttle_set_function_high_uses_snd() -> None:
    tc, layout, tm, captured = await _build_pair_with_slot_for(3601)
    th = await tm.acquire(3601)
    await th.set_function(5, True)  # F5 lives in SND

    for _ in range(20):
        if any(isinstance(m, OpcLocoSnd) for m in captured):
            break
        await asyncio.sleep(0.01)
    snds = [m for m in captured if isinstance(m, OpcLocoSnd)]
    assert snds and snds[-1].snd & 0x01

    await tc.aclose()
    await layout.aclose()


async def test_throttle_set_function_low_uses_dirf() -> None:
    tc, layout, tm, captured = await _build_pair_with_slot_for(3601)
    th = await tm.acquire(3601)
    captured.clear()
    await th.set_function(0, True)  # F0 (headlight) lives in DIRF

    for _ in range(20):
        if any(isinstance(m, OpcLocoDirF) for m in captured):
            break
        await asyncio.sleep(0.01)
    dirfs = [m for m in captured if isinstance(m, OpcLocoDirF)]
    assert dirfs and dirfs[-1].dirf & 0x10

    await tc.aclose()
    await layout.aclose()


async def test_throttle_acquire_is_idempotent_per_address() -> None:
    tc, layout, tm, _ = await _build_pair_with_slot_for(3601)
    a = await tm.acquire(3601)
    b = await tm.acquire(3601)
    assert a is b

    await tc.aclose()
    await layout.aclose()


async def test_throttle_release_writes_slot_common() -> None:
    """Regression for the Tier 4 #12 release bug.

    The old code sent ``OPC_MOVE_SLOTS src=<slot> dest=0`` which on
    real command stations means "dispatch this slot" -- the CS starts
    purging it and other cabs can grab it, but the loco stops
    responding to us immediately. That's not what we want when the
    user hits Release.

    The correct wire form (matches JMRI Java's
    ``LocoNetSlot.releaseSlot()``) is ``OPC_SLOT_STAT1(slot=<n>,
    stat=(current & ~LOCO_STAT_MASK) | LOCO_COMMON)`` -- marks the
    slot common so the CS can purge on its own timeout schedule
    without stealing our loco right this instant.
    """
    tc, layout, tm, captured = await _build_pair_with_slot_for(3601)
    th = await tm.acquire(3601)
    captured.clear()
    await tm.release(th)
    for _ in range(20):
        if any(isinstance(m, OpcSlotStat1) for m in captured):
            break
        await asyncio.sleep(0.01)

    stats = [m for m in captured if isinstance(m, OpcSlotStat1)]
    assert stats, "release must emit OPC_SLOT_STAT1"
    stat_msg = stats[-1]
    assert stat_msg.slot == th.slot.number
    # The low 2 status bits (0x30 mask) must read LOCO_COMMON (0x10).
    assert (stat_msg.stat & LOCO_STAT_MASK) == LOCO_COMMON

    # And critically: no dispatch (dest=0 move) went out.
    dispatched = [
        m for m in captured if isinstance(m, OpcMoveSlots) and m.dest == 0
    ]
    assert not dispatched, (
        "release must not dispatch the slot -- that would let another "
        "cab grab it while the loco is still under our control"
    )

    await tc.aclose()
    await layout.aclose()
