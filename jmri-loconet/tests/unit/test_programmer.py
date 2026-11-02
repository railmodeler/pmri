"""LocoNet service-mode programmer tests via a fake command station."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.beans.programmer import ProgrammingMode

from jmri_loconet.messages import (
    LocoNetMessage,
    OpcLongAck,
    OpcSlRdData,
    OpcWrSlData,
)
from jmri_loconet.opcodes import OPC_WR_SL_DATA
from jmri_loconet.programmer import (
    LONG_ACK_READ_FAIL,
    LONG_ACK_WRITE_DONE,
    PCMD_SERVICE_DIRECT_READ,
    PCMD_SERVICE_DIRECT_WRITE,
    PROGRAMMING_SLOT,
    LnProgrammer,
    LnProgrammerManager,
    _pack_cv_and_data,
    _unpack_data,
)
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import loopback_pair


def test_pack_cv_and_data_basic() -> None:
    cvh, cvl, data7 = _pack_cv_and_data(cv=1, value=0x42)
    assert cvl == 0
    assert cvh & 0x07 == 0    # CV# 0 (1-based 1) -> bits 7-9 = 0
    assert cvh & 0x20 == 0    # data bit 7 = 0
    assert data7 == 0x42


def test_pack_cv_and_data_high_bits() -> None:
    """CV29, value 0xFF should set CVH bit 5 and CVL=28."""
    cvh, cvl, data7 = _pack_cv_and_data(cv=29, value=0xFF)
    assert cvl == 28
    assert cvh & 0x20 == 0x20   # data bit 7 set
    assert data7 == 0x7F


def test_unpack_data_round_trips() -> None:
    for value in (0x00, 0x42, 0x7F, 0x80, 0xFF):
        cvh, _, data7 = _pack_cv_and_data(cv=1, value=value)
        assert _unpack_data(cvh, data7) == value


def test_cv_validation() -> None:
    with pytest.raises(ValueError):
        _pack_cv_and_data(cv=0, value=0)
    with pytest.raises(ValueError):
        _pack_cv_and_data(cv=1025, value=0)
    with pytest.raises(ValueError):
        _pack_cv_and_data(cv=1, value=256)


async def _build_with_fake_cs(cv_state: dict[int, int]):
    """Build a TC pair where the layout side acts as a programming CS.

    Responds to OPC_WR_SL_DATA at slot 124 by:
    * if PCMD is a *write*, storing CV->value and replying OPC_LONG_ACK,
    * if PCMD is a *read*, replying with OPC_SL_RD_DATA carrying the
      stored value.
    """
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await tc.start()
    await layout_tc.start()

    async def cs(msg: LocoNetMessage) -> None:
        if not isinstance(msg, OpcWrSlData) or msg.slot != PROGRAMMING_SLOT:
            return
        pcmd = msg.speed       # bytes[4] per the programming-slot layout
        # CV# is split across address (cvl|cvh<<7 in OpcWrSlData encoding).
        cvl = msg.address & 0x7F
        cvh = (msg.address >> 7) & 0x7F
        cv = ((cvh & 0x07) << 7) | cvl
        cv += 1     # 0-based -> 1-based
        value = _unpack_data(cvh, msg.snd)

        if pcmd == PCMD_SERVICE_DIRECT_WRITE:
            cv_state[cv] = value
            await layout_tc.send(OpcLongAck(
                responding_opcode=OPC_WR_SL_DATA, ack_code=LONG_ACK_WRITE_DONE,
            ))
        elif pcmd == PCMD_SERVICE_DIRECT_READ:
            stored = cv_state.get(cv, 0)
            cvh_out = ((cv - 1) >> 7) & 0x07 | ((stored & 0x80) >> 2)
            data7_out = stored & 0x7F
            await layout_tc.send(OpcSlRdData(
                slot=PROGRAMMING_SLOT, stat=0,
                address=cvl | (cvh_out << 7),
                speed=0, dirf=0, trk=0, snd=data7_out,
            ))

    layout_tc.inbound.connect(cs)
    return tc, layout_tc


async def test_write_then_read_round_trip() -> None:
    cv_state: dict[int, int] = {}
    tc, layout_tc = await _build_with_fake_cs(cv_state)
    prog = LnProgrammer(tc)

    await prog.write_cv_direct(cv=29, value=6)
    assert cv_state[29] == 6
    value = await prog.read_cv_direct(cv=29)
    assert value == 6

    await tc.aclose()
    await layout_tc.aclose()


async def test_read_fail_raises() -> None:
    """When the fake CS sends LONG_ACK with read-fail, we surface an error."""
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await tc.start()
    await layout_tc.start()

    async def cs(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcWrSlData) and msg.slot == PROGRAMMING_SLOT:
            await layout_tc.send(OpcLongAck(
                responding_opcode=OPC_WR_SL_DATA, ack_code=LONG_ACK_READ_FAIL,
            ))
    layout_tc.inbound.connect(cs)

    prog = LnProgrammer(tc)
    with pytest.raises(RuntimeError, match="read failed"):
        await prog.read_cv_direct(cv=1, timeout=1.0)

    await tc.aclose()
    await layout_tc.aclose()


async def test_programmer_manager_service_singleton() -> None:
    a, _ = loopback_pair()
    tc = LocoNetTrafficController(a)
    mgr = LnProgrammerManager(LnProgrammer(tc))
    a_prog = mgr.global_programmer()
    b_prog = mgr.global_programmer()
    assert a_prog is b_prog
    assert a_prog.mode == ProgrammingMode.SERVICE_DIRECT
    await tc.aclose()


async def test_ops_programmer_address_and_mode() -> None:
    a, _ = loopback_pair()
    tc = LocoNetTrafficController(a)
    mgr = LnProgrammerManager(LnProgrammer(tc))
    ops = mgr.ops_programmer(3601, long_address=True)
    assert ops.mode == ProgrammingMode.OPS_MODE
    assert ops.address == 3601
    await tc.aclose()
