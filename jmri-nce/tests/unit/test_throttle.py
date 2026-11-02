"""NCE throttle end-to-end via fake CS -- exercises the 0xA2 ``nceLocoCmd`` path."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.state import ThrottleDirection

from jmri_nce.opcodes import (
    ACK_OK,
    LOCO_SUB_128_FWD,
    LOCO_SUB_128_REV,
    LOCO_SUB_ESTOP_FWD,
    LOCO_SUB_FUNC_GROUP_1,
    LOCO_SUB_FUNC_GROUP_2,
    LOCO_SUB_FUNC_GROUP_3,
    LOCO_SUB_FUNC_GROUP_4,
    LOCO_SUB_FUNC_GROUP_5,
    OP_NCE_LOCO_CMD,
)
from jmri_nce.throttle import NceThrottleManager, speed_to_wire_128
from jmri_nce.traffic import NceTrafficController
from jmri_nce.transport import loopback_pair


async def _fake_cs(port) -> list[bytes]:  # noqa: ANN001
    """Drain NCE loco-command requests + ACK them.

    Wire form: ``[0xA2] [addr_hi] [addr_lo] [sub_cmd] [data]`` per JMRI's
    ``NceBinaryCommand.nceLocoCmd`` (modern firmware throttle path).
    Returns a list of 4-byte tuples: ``(addr_hi, addr_lo, sub_cmd, data)``.
    """
    captured: list[bytes] = []

    async def run() -> None:
        while True:
            head = await port.read_exact(1)
            if head[0] == OP_NCE_LOCO_CMD:
                rest = await port.read_exact(4)
                captured.append(rest)
                await port.write(ACK_OK)
            else:
                await port.write(ACK_OK)

    asyncio.create_task(run())
    return captured


# --- wire-form helpers ------------------------------------------------------


def test_speed_to_wire_128_extremes() -> None:
    assert speed_to_wire_128(0.0) == 0
    assert speed_to_wire_128(1.0) == 126
    assert speed_to_wire_128(0.5) == 63
    assert speed_to_wire_128(-0.5) == 63   # magnitude only; direction is in sub_cmd


# --- end-to-end -------------------------------------------------------------


async def test_throttle_set_speed_emits_loco_cmd() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    captured = await _fake_cs(b)

    mgr = NceThrottleManager("N", tc=tc)
    t = await mgr.acquire(3)
    await t.set_speed(0.5)

    for _ in range(20):
        if captured:
            break
        await asyncio.sleep(0.005)
    assert captured, "expected at least one loco command on the wire"
    addr_hi, addr_lo, sub_cmd, data = captured[0]
    assert addr_hi == 0 and addr_lo == 3
    assert sub_cmd == LOCO_SUB_128_FWD
    assert data == 63

    await tc.aclose()


async def test_throttle_reverse_changes_sub_cmd() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    captured = await _fake_cs(b)
    mgr = NceThrottleManager("N", tc=tc)
    t = await mgr.acquire(3)
    await t.set_direction(ThrottleDirection.REVERSE)

    for _ in range(20):
        if captured:
            break
        await asyncio.sleep(0.005)
    assert captured
    assert captured[-1][2] == LOCO_SUB_128_REV
    await tc.aclose()


async def test_throttle_function_groups_each_emit_a_packet() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    captured = await _fake_cs(b)
    mgr = NceThrottleManager("N", tc=tc)
    t = await mgr.acquire(3)

    await t.set_function(0, True)   # group 1: F0 -> bit 4 of data
    await t.set_function(5, True)   # group 2: F5 -> bit 0
    await t.set_function(9, True)   # group 3: F9 -> bit 0
    await t.set_function(13, True)  # group 4: F13 -> bit 0
    await t.set_function(21, True)  # group 5: F21 -> bit 0

    for _ in range(40):
        if len(captured) >= 5:
            break
        await asyncio.sleep(0.005)
    assert len(captured) >= 5
    subs = {pkt[2]: pkt[3] for pkt in captured}
    assert subs[LOCO_SUB_FUNC_GROUP_1] == 0b00010000   # F0 only
    assert subs[LOCO_SUB_FUNC_GROUP_2] == 0b0001        # F5 only
    assert subs[LOCO_SUB_FUNC_GROUP_3] == 0b0001        # F9 only
    assert subs[LOCO_SUB_FUNC_GROUP_4] == 0b00000001    # F13 only
    assert subs[LOCO_SUB_FUNC_GROUP_5] == 0b00000001    # F21 only
    await tc.aclose()


async def test_throttle_manager_idempotent_acquire() -> None:
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceThrottleManager("N", tc=tc)
    t1 = await mgr.acquire(3)
    t2 = await mgr.acquire(3)
    assert t1 is t2
    await tc.aclose()


async def test_throttle_emergency_stop_uses_estop_sub_cmd() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    captured = await _fake_cs(b)
    mgr = NceThrottleManager("N", tc=tc)
    t = await mgr.acquire(3)
    await t.emergency_stop()
    for _ in range(20):
        if captured:
            break
        await asyncio.sleep(0.005)
    assert captured
    assert captured[-1][2] == LOCO_SUB_ESTOP_FWD
    assert captured[-1][3] == 0
    await tc.aclose()


@pytest.mark.parametrize("addr,is_long", [(3, False), (3601, True)])
async def test_throttle_address_encoding(addr: int, is_long: bool) -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    captured = await _fake_cs(b)
    mgr = NceThrottleManager("N", tc=tc)
    t = await mgr.acquire(addr, long_address=is_long)
    await t.set_speed(0.25)
    for _ in range(20):
        if captured:
            break
        await asyncio.sleep(0.005)
    assert captured
    addr_hi, addr_lo, _, _ = captured[-1]
    wire = (addr_hi << 8) | addr_lo
    if is_long:
        # Long addresses are sent as addr + 0xC000.
        assert wire == addr + 0xC000
    else:
        assert wire == addr
    await tc.aclose()
