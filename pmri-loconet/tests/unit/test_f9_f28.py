"""F9..F28 via OPC_IMM_PACKET tests."""
from __future__ import annotations

import asyncio

from pmri_loconet.dcc import function_high_packet_dcc, wrap_in_imm_packet
from pmri_loconet.messages import LocoNetMessage, OpcUnknown
from pmri_loconet.opcodes import OPC_IMM_PACKET
from pmri_loconet.slots import SlotManager
from pmri_loconet.throttle import LnThrottleManager
from pmri_loconet.traffic import LocoNetTrafficController
from pmri_loconet.transport import loopback_pair


def test_function_high_group_3_packet() -> None:
    funcs = [False] * 32
    funcs[13] = True
    pkt = function_high_packet_dcc(
        address=3, long_address=False, functions=funcs, group=3,
    )
    # Format: addr, 0xDE, mask, xor
    assert pkt[0] == 3
    assert pkt[1] == 0xDE
    assert pkt[2] == 0x01     # F13 = bit 0


def test_function_high_group_4_packet() -> None:
    funcs = [False] * 32
    funcs[28] = True
    pkt = function_high_packet_dcc(
        address=3, long_address=False, functions=funcs, group=4,
    )
    assert pkt[1] == 0xDF
    assert pkt[2] == 0x80     # F28 = bit 7


def test_imm_packet_wraps_dcc_payload() -> None:
    dcc = function_high_packet_dcc(
        address=3, long_address=False,
        functions=[False] * 32, group=3,
    )
    frame = wrap_in_imm_packet(dcc, repeat=2)
    assert frame[0] == OPC_IMM_PACKET
    assert frame[2] == 0x7F   # IM1


async def _fake_layout(b) -> list[LocoNetMessage]:  # noqa: ANN001
    """Capture inbound LocoNet messages on the layout side."""
    captured: list[LocoNetMessage] = []
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await layout_tc.start()
    layout_tc.inbound.connect(captured.append)
    return captured, layout_tc  # type: ignore[return-value]


async def _set_up_loco() -> tuple[
    LocoNetTrafficController, LocoNetTrafficController, list[LocoNetMessage], LnThrottleManager,
]:
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    await tc.start()
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await layout_tc.start()

    captured: list[LocoNetMessage] = []
    layout_tc.inbound.connect(captured.append)

    # Fake CS responding to OpcLocoAdr with an OpcSlRdData.
    from pmri_loconet.messages import OpcLocoAdr, OpcSlRdData  # noqa: PLC0415
    from pmri_loconet.opcodes import LOCO_IN_USE  # noqa: PLC0415

    async def cs(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcLocoAdr):
            await layout_tc.send(OpcSlRdData(
                slot=5, stat=LOCO_IN_USE, address=msg.address,
                speed=0, dirf=0, trk=0x07, snd=0,
            ))

    layout_tc.inbound.connect(cs)

    sm = SlotManager(tc)
    tm = LnThrottleManager("L", tc=tc, slot_manager=sm)
    return tc, layout_tc, captured, tm


async def test_set_f13_emits_imm_packet() -> None:
    tc, layout_tc, captured, tm = await _set_up_loco()
    t = await tm.acquire(3)
    captured.clear()
    await t.set_function(13, True)
    for _ in range(40):
        if any(isinstance(m, OpcUnknown) and m.opcode == OPC_IMM_PACKET for m in captured):
            break
        await asyncio.sleep(0.005)
    imm = [m for m in captured if isinstance(m, OpcUnknown) and m.opcode == OPC_IMM_PACKET]
    assert imm, "expected at least one OPC_IMM_PACKET on the wire"

    await tc.aclose()
    await layout_tc.aclose()


async def test_set_f25_emits_imm_packet() -> None:
    tc, layout_tc, captured, tm = await _set_up_loco()
    t = await tm.acquire(3)
    captured.clear()
    await t.set_function(25, True)
    for _ in range(40):
        if any(isinstance(m, OpcUnknown) and m.opcode == OPC_IMM_PACKET for m in captured):
            break
        await asyncio.sleep(0.005)
    imm = [m for m in captured if isinstance(m, OpcUnknown) and m.opcode == OPC_IMM_PACKET]
    assert imm

    await tc.aclose()
    await layout_tc.aclose()
