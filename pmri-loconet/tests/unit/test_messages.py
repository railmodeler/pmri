"""Encode/decode round-trip tests for typed LocoNet messages.

Each test exercises one opcode by:
  1. Constructing a typed message,
  2. Encoding it to wire bytes,
  3. Verifying the checksum,
  4. Decoding back to a typed message and asserting equality.
"""
from __future__ import annotations

import pytest

from pmri_loconet.codec import verify_checksum
from pmri_loconet.messages import (
    OpcGpOff,
    OpcGpOn,
    OpcIdle,
    OpcInputRep,
    OpcLocoAdr,
    OpcLocoDirF,
    OpcLocoSnd,
    OpcLocoSpd,
    OpcLongAck,
    OpcMoveSlots,
    OpcRqSlData,
    OpcSlRdData,
    OpcSlotStat1,
    OpcSwReq,
    OpcUnknown,
    OpcWrSlData,
    decode,
    encode,
)


def _roundtrip(msg):  # noqa: ANN001 - typed via parametrize
    frame = encode(msg)
    assert verify_checksum(frame), "encoded frame must have correct checksum"
    return decode(frame)


# --- 2-byte messages --------------------------------------------------------


@pytest.mark.parametrize("msg", [OpcGpOn(), OpcGpOff(), OpcIdle()])
def test_2byte_opcodes_roundtrip(msg) -> None:  # noqa: ANN001
    out = _roundtrip(msg)
    assert type(out) is type(msg)


# --- 4-byte messages --------------------------------------------------------


@pytest.mark.parametrize(
    "address,closed,output_on",
    [(1, True, True), (256, False, True), (2048, True, False), (12, False, False)],
)
def test_sw_req_roundtrip(address: int, closed: bool, output_on: bool) -> None:
    msg = OpcSwReq(address=address, closed=closed, output_on=output_on)
    out = _roundtrip(msg)
    assert isinstance(out, OpcSwReq)
    assert out.address == address
    assert out.closed == closed
    assert out.output_on == output_on


@pytest.mark.parametrize("address,active", [(1, True), (100, False), (2048, True)])
def test_input_rep_roundtrip(address: int, active: bool) -> None:
    msg = OpcInputRep(address=address, active=active)
    out = _roundtrip(msg)
    assert isinstance(out, OpcInputRep)
    assert out.address == address
    assert out.active == active


def test_long_ack_roundtrip() -> None:
    msg = OpcLongAck(responding_opcode=0xBF, ack_code=0x7F)
    out = _roundtrip(msg)
    assert isinstance(out, OpcLongAck)
    assert out.responding_opcode == 0xBF
    assert out.ack_code == 0x7F


def test_slot_stat1_roundtrip() -> None:
    msg = OpcSlotStat1(slot=5, stat=0x30)
    out = _roundtrip(msg)
    assert isinstance(out, OpcSlotStat1)
    assert out.slot == 5
    assert out.stat == 0x30


def test_move_slots_roundtrip() -> None:
    msg = OpcMoveSlots(src=12, dest=12)  # null move = refresh
    out = _roundtrip(msg)
    assert isinstance(out, OpcMoveSlots)
    assert out.src == 12
    assert out.dest == 12


def test_rq_sl_data_roundtrip() -> None:
    msg = OpcRqSlData(slot=42)
    out = _roundtrip(msg)
    assert isinstance(out, OpcRqSlData)
    assert out.slot == 42


@pytest.mark.parametrize("address", [3, 127, 128, 3601, 9999])
def test_loco_adr_roundtrip(address: int) -> None:
    msg = OpcLocoAdr(address=address)
    out = _roundtrip(msg)
    assert isinstance(out, OpcLocoAdr)
    assert out.address == address


def test_loco_spd_roundtrip() -> None:
    msg = OpcLocoSpd(slot=5, speed=64)
    out = _roundtrip(msg)
    assert isinstance(out, OpcLocoSpd)
    assert out.slot == 5
    assert out.speed == 64


def test_loco_dirf_roundtrip() -> None:
    msg = OpcLocoDirF(slot=5, dirf=0x10)
    out = _roundtrip(msg)
    assert isinstance(out, OpcLocoDirF)
    assert out.slot == 5
    assert out.dirf == 0x10


def test_loco_snd_roundtrip() -> None:
    msg = OpcLocoSnd(slot=5, snd=0x0F)
    out = _roundtrip(msg)
    assert isinstance(out, OpcLocoSnd)
    assert out.snd == 0x0F


# --- 14-byte messages -------------------------------------------------------


def test_sl_rd_data_roundtrip() -> None:
    msg = OpcSlRdData(slot=5, stat=0x30, address=3601, speed=64, dirf=0x10, trk=0x07, snd=0x0F)
    out = _roundtrip(msg)
    assert isinstance(out, OpcSlRdData)
    assert out.slot == 5
    assert out.stat == 0x30
    assert out.address == 3601
    assert out.speed == 64
    assert out.dirf == 0x10
    assert out.snd == 0x0F


def test_wr_sl_data_roundtrip() -> None:
    msg = OpcWrSlData(slot=10, stat=0x30, address=42, speed=0, dirf=0x10, trk=0x07, snd=0)
    out = _roundtrip(msg)
    assert isinstance(out, OpcWrSlData)
    assert out.slot == 10
    assert out.address == 42


# --- unknown / malformed ----------------------------------------------------


def test_unknown_opcode_decodes_as_opcunknown() -> None:
    # 0xB7 is unassigned in our typed table.
    body = bytes([0xB7, 0x00, 0x00])
    frame = body + bytes([0xFF ^ sum(body) ^ (0xFF if len(body) % 2 else 0)])
    # Easier: compute checksum properly.
    from pmri_loconet.codec import append_checksum  # noqa: PLC0415
    frame = append_checksum(body)
    out = decode(frame)
    assert isinstance(out, OpcUnknown)
    assert out.opcode == 0xB7


def test_decode_rejects_invalid_checksum() -> None:
    bad = bytes([0x83, 0x00])  # 0x00 isn't the checksum for 0x83
    with pytest.raises(ValueError, match="checksum"):
        decode(bad)
