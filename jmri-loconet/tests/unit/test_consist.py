"""LnConsistManager -- LocoNet hardware consists via LINK/UNLINK opcodes.

The tests use a loopback pair + a fake command station that hands out
slot numbers on OPC_LOCO_ADR and captures every outbound message on
the layout side. Speed / direction / LINK / UNLINK opcodes are then
asserted against the capture.
"""
from __future__ import annotations

import asyncio

from jmri_core.state import ThrottleDirection

from jmri_loconet.consist import LnConsistManager
from jmri_loconet.messages import (
    LocoNetMessage,
    OpcLinkSlots,
    OpcLocoAdr,
    OpcLocoSpd,
    OpcSlRdData,
    OpcUnlinkSlots,
)
from jmri_loconet.opcodes import LOCO_IN_USE
from jmri_loconet.slots import SlotManager
from jmri_loconet.throttle import LnThrottleManager
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import loopback_pair


async def _build_env() -> tuple[
    LocoNetTrafficController,
    LocoNetTrafficController,
    LnConsistManager,
    LnThrottleManager,
    list[LocoNetMessage],
]:
    """Build a loopback env with a fake CS that hands out slots.

    Returns (tc, layout_tc, consists, throttles, captured) -- the last
    is a live list every message the layout side receives is appended
    to, so tests can assert on the wire stream.
    """
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    layout = LocoNetTrafficController(b, min_send_interval=0.0)
    await tc.start()
    await layout.start()

    slot_table: dict[int, int] = {}

    async def cs_listener(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcLocoAdr):
            slot = slot_table.setdefault(
                msg.address, max(slot_table.values(), default=0) + 1,
            )
            await layout.send(OpcSlRdData(
                slot=slot, stat=LOCO_IN_USE, address=msg.address,
                speed=0, dirf=0, trk=0x07, snd=0,
            ))

    layout.inbound.connect(cs_listener)

    captured: list[LocoNetMessage] = []
    layout.inbound.connect(captured.append)

    sm = SlotManager(tc)
    tm = LnThrottleManager("L", tc=tc, slot_manager=sm)
    cm = LnConsistManager(tm, "L", tc=tc, slots=sm)
    return tc, layout, cm, tm, captured


# ---- LINK / UNLINK wire ops ------------------------------------------------


async def test_add_member_sends_link_slots() -> None:
    """Adding a follower to a CS consist LINKs the follower's slot to
    the lead's slot on the wire."""
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)   # lead itself
        await cm.add_member(36, 3602, long_address=True)   # follower
        # Loopback delivery is async; let pending sends settle.
        await asyncio.sleep(0.05)
        # The follower's slot follows the lead's slot.
        lead_slot = tm.get(3601).slot.number
        follower_slot = tm.get(3602).slot.number
        types_seen = [type(m).__name__ for m in captured]
        links = [m for m in captured if isinstance(m, OpcLinkSlots)]
        assert any(
            m.src == follower_slot and m.dest == lead_slot for m in links
        ), f"expected LINK slot {follower_slot} -> {lead_slot}, got links={links}, all msgs={types_seen}"
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_add_second_follower_only_links_the_new_one() -> None:
    """Two adds -> two LINKs total, not three (idempotent by pair)."""
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        # Second follower on the same lead.
        await asyncio.sleep(0.05)
        captured.clear()
        await cm.add_member(36, 3603, long_address=True)
        await asyncio.sleep(0.05)
        links = [m for m in captured if isinstance(m, OpcLinkSlots)]
        # Exactly one new LINK: 3603 -> 3601. 3602 was already linked.
        assert len(links) == 1
        follower_slot = tm.get(3603).slot.number
        lead_slot = tm.get(3601).slot.number
        assert links[0].src == follower_slot
        assert links[0].dest == lead_slot
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_remove_member_sends_unlink_slots() -> None:
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()
        await cm.remove_member(36, 3602)
        await asyncio.sleep(0.05)
        unlinks = [m for m in captured if isinstance(m, OpcUnlinkSlots)]
        assert len(unlinks) == 1
        follower_slot = tm.get(3602).slot.number
        lead_slot = tm.get(3601).slot.number
        assert unlinks[0].src == follower_slot
        assert unlinks[0].dest == lead_slot
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_delete_consist_unlinks_all_followers() -> None:
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        await cm.add_member(36, 3603, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()
        assert await cm.delete(36)
        await asyncio.sleep(0.05)
        unlinks = [m for m in captured if isinstance(m, OpcUnlinkSlots)]
        assert len(unlinks) == 2   # 3602 + 3603
    finally:
        await layout.aclose()
        await tc.aclose()


# ---- set_speed / set_direction / e-stop only target the lead ---------------


async def test_cs_consist_set_speed_only_sends_to_lead() -> None:
    """CS-managed consist: LINK causes the CS to fan speed/direction
    on its own, so we send only ONE OpcLocoSpd (to the lead)."""
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()
        await cm.set_speed(36, 0.5)
        await asyncio.sleep(0.05)
        speeds = [m for m in captured if isinstance(m, OpcLocoSpd)]
        assert len(speeds) == 1
        # And it targets the lead's slot.
        lead_slot = tm.get(3601).slot.number
        assert speeds[0].slot == lead_slot
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_cs_consist_set_direction_only_sends_to_lead() -> None:
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()
        await cm.set_direction(36, ThrottleDirection.REVERSE)
        await asyncio.sleep(0.05)
        # DIRF frames -- we expect exactly one (to the lead).
        from jmri_loconet.messages import OpcLocoDirF
        dirfs = [m for m in captured if isinstance(m, OpcLocoDirF)]
        assert len(dirfs) == 1
        lead_slot = tm.get(3601).slot.number
        assert dirfs[0].slot == lead_slot
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_cs_consist_inverted_follower_gets_opposite_dirf() -> None:
    """A CS-consist member added with ``direction_normal=False``
    physically points backward relative to the lead. LocoNet's LINK
    fan-out replicates the lead's DIRF verbatim, so an unaided
    follower would move the wrong way. The manager sends an inverted
    DIRF to the follower on ``set_direction`` to compensate. Matches
    the JMRI Java behaviour in ``LocoNetConsist.setDirection``
    (line 411..415)."""
    from jmri_loconet.messages import OpcLocoDirF
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)  # lead
        await cm.add_member(
            36, 3602, long_address=True, direction_normal=False,
        )  # reversed follower
        await asyncio.sleep(0.05)
        captured.clear()
        await cm.set_direction(36, ThrottleDirection.FORWARD)
        await asyncio.sleep(0.05)

        dirfs = [m for m in captured if isinstance(m, OpcLocoDirF)]
        # One DIRF to lead (FORWARD) + one to inverted follower
        # (REVERSE). Normal followers stay untouched (CS fans for
        # them via LINK).
        assert len(dirfs) == 2

        lead_slot = tm.get(3601).slot.number
        follower_slot = tm.get(3602).slot.number
        by_slot = {d.slot: d for d in dirfs}

        # Lead: forward means dirf bit 5 (0x20) is clear.
        assert (by_slot[lead_slot].dirf & 0x20) == 0
        # Follower: reverse means dirf bit 5 is set.
        assert (by_slot[follower_slot].dirf & 0x20) != 0
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_cs_consist_normal_followers_untouched_on_set_direction() -> None:
    """Regression pin -- a consist with only normal followers still
    sends exactly one DIRF (to the lead). The CS fans direction for
    normal followers on its own via the LINK relay."""
    from jmri_loconet.messages import OpcLocoDirF
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36)
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        # Both direction_normal defaults to True.
        await asyncio.sleep(0.05)
        captured.clear()
        await cm.set_direction(36, ThrottleDirection.REVERSE)
        await asyncio.sleep(0.05)

        dirfs = [m for m in captured if isinstance(m, OpcLocoDirF)]
        assert len(dirfs) == 1
        lead_slot = tm.get(3601).slot.number
        assert dirfs[0].slot == lead_slot
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_software_consist_on_ln_still_fans_out() -> None:
    """A caller who explicitly requests kind='software' gets base-class
    fan-out even on a LocoNet connection -- no LINK ops, per-loco
    commands to each member. Useful for consists with inverted-direction
    members (LN's LINK doesn't support the flag in this initial pass)."""
    tc, layout, cm, tm, captured = await _build_env()
    try:
        await cm.create(36, kind="software")
        await cm.add_member(36, 3601, long_address=True)
        await cm.add_member(36, 3602, long_address=True)
        await asyncio.sleep(0.05)
        # No LINK ops should have been sent.
        links = [m for m in captured if isinstance(m, OpcLinkSlots)]
        assert links == []
        captured.clear()
        await cm.set_speed(36, 0.5)
        await asyncio.sleep(0.05)
        speeds = [m for m in captured if isinstance(m, OpcLocoSpd)]
        # Software fan-out: one SPD per member.
        assert len(speeds) == 2
    finally:
        await layout.aclose()
        await tc.aclose()
