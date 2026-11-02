"""DCC-EX advanced consist (CV19) end-to-end test.

DCC-EX has no CS-side consist opcode, so consist options on this
driver are:

* ``kind="software"`` (default) -- send throttle commands to each
  loco individually via ``<t>``.
* ``kind="advanced"`` (CV19) -- write CV19 on each member decoder;
  then throttle commands to the consist address fan out on the
  layout because the decoders themselves respond. Uses the DCC-EX
  ops-mode CV write (``<w cab cv value>``) via the base
  ``ConsistManager._program_cv19`` path.

This test verifies that opting a consist into ``advanced`` correctly
fires the ``<w>`` opcodes for each member -- the deferred item from
the beta 3 workitem doc.
"""
from __future__ import annotations

import asyncio

from jmri_core.beans.consist_manager import ConsistManager
from jmri_dccex.codec import DccexMessage
from jmri_dccex.programmer import DccexProgrammer, DccexProgrammerManager
from jmri_dccex.throttle import DccexThrottleManager
from jmri_dccex.traffic import DccexTrafficController
from jmri_dccex.transport import loopback_pair


async def _build_env() -> tuple[
    DccexTrafficController, DccexTrafficController,
    ConsistManager, list[DccexMessage],
]:
    """Loopback pair + consist manager wired with the DCC-EX ops
    programmer. Returns (device_tc, layout_tc, cm, captured_on_layout)."""
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    layout = DccexTrafficController(b)
    await tc.start()
    await layout.start()

    captured: list[DccexMessage] = []
    layout.inbound.connect(lambda m: captured.append(m))

    throttles = DccexThrottleManager("D", tc=tc)
    programmer_inner = DccexProgrammer(tc)
    programmers = DccexProgrammerManager(programmer_inner)
    cm = ConsistManager(throttles, "D", programmers)
    return tc, layout, cm, captured


async def test_advanced_consist_writes_cv19_on_add_member() -> None:
    """Adding a member to an ``advanced`` consist emits an ops-mode
    ``<w address 19 value>`` frame where ``value`` is the consist
    address in bits 0..6."""
    tc, layout, cm, captured = await _build_env()
    try:
        await cm.create(20, kind="advanced")
        captured.clear()
        await cm.add_member(20, 3601, long_address=True)
        # Give the ops-mode write a moment to hit the wire.
        await asyncio.sleep(0.05)

        writes = [
            m for m in captured
            if m.command == "w" and len(m.args) == 3
        ]
        assert len(writes) == 1
        assert writes[0].args == ("3601", "19", "20")
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_advanced_consist_inverted_member_sets_cv19_bit_7() -> None:
    """A member added with ``direction_normal=False`` runs backward
    relative to the consist. Per NMRA S-9.2.2, CV19 bit 7 (0x80)
    signals that. Consist address 20 + inverted = 20 | 0x80 = 148."""
    tc, layout, cm, captured = await _build_env()
    try:
        await cm.create(20, kind="advanced")
        captured.clear()
        await cm.add_member(
            20, 3601, long_address=True, direction_normal=False,
        )
        await asyncio.sleep(0.05)

        writes = [
            m for m in captured
            if m.command == "w" and len(m.args) == 3
        ]
        assert len(writes) == 1
        assert writes[0].args == ("3601", "19", "148")   # 20 | 0x80
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_advanced_consist_removes_member_clears_cv19() -> None:
    """Removing a member from an advanced consist writes CV19=0 on
    that decoder so it stops responding to the consist address."""
    tc, layout, cm, captured = await _build_env()
    try:
        await cm.create(20, kind="advanced")
        await cm.add_member(20, 3601, long_address=True)
        await cm.add_member(20, 3602, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()

        await cm.remove_member(20, 3602)
        await asyncio.sleep(0.05)

        writes = [
            m for m in captured
            if m.command == "w" and len(m.args) == 3
        ]
        # One <w 3602 19 0> to release the loco from the consist.
        assert len(writes) == 1
        assert writes[0].args == ("3602", "19", "0")
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_advanced_consist_drive_addresses_the_consist_number() -> None:
    """Once CV19 is written on each member, decoders respond to
    packets on the consist address only. Verify that ``set_speed``
    on an advanced consist emits ONE ``<t consist_addr speed dir>``
    and no per-member throttle frames -- fanning to members would
    talk to decoders that (correctly) ignore their own address now."""
    tc, layout, cm, captured = await _build_env()
    try:
        await cm.create(20, kind="advanced")
        await cm.add_member(20, 3601, long_address=True)
        await cm.add_member(20, 3602, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()

        await cm.set_speed(20, 0.5)
        await asyncio.sleep(0.05)

        throttle_frames = [m for m in captured if m.command == "t"]
        # One frame, addressed to 20 (the consist), not 3601 or 3602.
        assert len(throttle_frames) == 1
        assert throttle_frames[0].args[0] == "20"
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_delete_advanced_consist_writes_cv19_zero_on_wire() -> None:
    """End-to-end: deleting an advanced consist emits an ops-mode
    ``<w member 19 0>`` for every member so the decoder stops
    responding to the consist address. Otherwise the loco is left
    stranded -- outside every consist in our tables, but still
    ignoring its own address on the layout."""
    tc, layout, cm, captured = await _build_env()
    try:
        await cm.create(20, kind="advanced")
        await cm.add_member(20, 3601, long_address=True)
        await cm.add_member(20, 3602, long_address=True)
        await asyncio.sleep(0.05)
        captured.clear()

        await cm.delete(20)
        await asyncio.sleep(0.05)

        clears = [
            m for m in captured
            if m.command == "w" and len(m.args) == 3 and m.args[2] == "0"
        ]
        assert {m.args[0] for m in clears} == {"3601", "3602"}
    finally:
        await layout.aclose()
        await tc.aclose()


async def test_software_consist_does_not_touch_cv19() -> None:
    """The default ``software`` mode skips the CV19 path entirely.
    Regression pin: an operator who wants the software mode
    shouldn't get surprised by ops-mode writes to their locos."""
    tc, layout, cm, captured = await _build_env()
    try:
        await cm.create(20)                    # default = software
        captured.clear()
        await cm.add_member(20, 3601, long_address=True)
        await asyncio.sleep(0.05)

        writes = [
            m for m in captured
            if m.command == "w" and len(m.args) == 3
        ]
        assert writes == []
    finally:
        await layout.aclose()
        await tc.aclose()
