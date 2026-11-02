"""Slot refresh task tests."""
from __future__ import annotations

import asyncio

from jmri_loconet.messages import LocoNetMessage, OpcLocoAdr, OpcMoveSlots, OpcSlRdData
from jmri_loconet.opcodes import LOCO_IN_USE
from jmri_loconet.slots import SlotManager
from jmri_loconet.throttle import LnThrottleManager
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import loopback_pair


async def _build_pair():
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await tc.start()
    await layout_tc.start()

    captured: list[LocoNetMessage] = []
    layout_tc.inbound.connect(captured.append)

    async def cs(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcLocoAdr):
            await layout_tc.send(OpcSlRdData(
                slot=5, stat=LOCO_IN_USE, address=msg.address,
                speed=0, dirf=0, trk=0x07, snd=0,
            ))
    layout_tc.inbound.connect(cs)
    return tc, layout_tc, captured


async def test_start_stop_refresh_is_idempotent() -> None:
    tc, layout_tc, _ = await _build_pair()
    sm = SlotManager(tc)
    tm = LnThrottleManager("L", tc=tc, slot_manager=sm, refresh_interval=0.05)
    tm.start_refresh()
    tm.start_refresh()    # second start is a no-op
    await tm.stop_refresh()
    await tm.stop_refresh()
    await tc.aclose()
    await layout_tc.aclose()


async def test_refresh_emits_null_move_for_each_active_throttle() -> None:
    tc, layout_tc, captured = await _build_pair()
    sm = SlotManager(tc)
    tm = LnThrottleManager("L", tc=tc, slot_manager=sm, refresh_interval=0.05)
    # Acquire one throttle so there's a slot to refresh.
    await tm.acquire(3)
    captured.clear()
    tm.start_refresh()
    # Wait for at least one refresh tick.
    for _ in range(40):
        if any(isinstance(m, OpcMoveSlots) and m.src == m.dest for m in captured):
            break
        await asyncio.sleep(0.01)
    await tm.stop_refresh()
    moves = [m for m in captured if isinstance(m, OpcMoveSlots) and m.src == m.dest]
    assert moves, "expected at least one null-move during refresh"
    await tc.aclose()
    await layout_tc.aclose()
