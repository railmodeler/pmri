from __future__ import annotations

import asyncio
from collections import deque

import pytest

from pmri_loconet.codec import (
    append_checksum,
    compute_checksum,
    frame_length,
    read_frame,
    verify_checksum,
)
from pmri_loconet.opcodes import (
    OPC_GPON,
    OPC_LOCO_SPD,
    OPC_SL_RD_DATA,
    OPC_SW_REQ,
    expected_length,
    is_valid_opcode_byte,
    length_class_of,
)


# --- opcodes table ----------------------------------------------------------


def test_opcode_length_classes() -> None:
    # 2-byte: OPC_GPON = 0x83 -> bits 5-6 = 0b00
    assert length_class_of(OPC_GPON) == 0b00
    assert expected_length(OPC_GPON) == 2
    # 4-byte: OPC_SW_REQ = 0xB0 -> bits 5-6 = 0b01
    assert length_class_of(OPC_SW_REQ) == 0b01
    assert expected_length(OPC_SW_REQ) == 4
    # 4-byte: OPC_LOCO_SPD = 0xA0 -> bits 5-6 = 0b01
    assert expected_length(OPC_LOCO_SPD) == 4
    # Variable: OPC_SL_RD_DATA = 0xE7 -> bits 5-6 = 0b11
    assert length_class_of(OPC_SL_RD_DATA) == 0b11
    assert expected_length(OPC_SL_RD_DATA) is None


def test_opcode_byte_validation() -> None:
    assert not is_valid_opcode_byte(0x7F)
    assert is_valid_opcode_byte(0x80)
    assert is_valid_opcode_byte(0xFF)


# --- checksum ---------------------------------------------------------------


def test_checksum_xor_with_ff() -> None:
    # Walk a couple frames from JMRI message monitors (verified by hand).
    # OPC_GPON: just opcode + checksum.
    cs = compute_checksum(bytes([0x83]))
    assert cs == (0xFF ^ 0x83)
    assert verify_checksum(bytes([0x83, cs]))


def test_append_then_verify() -> None:
    body = bytes([OPC_SW_REQ, 0x05, 0x30])
    frame = append_checksum(body)
    assert verify_checksum(frame)
    assert frame[:-1] == body


def test_verify_rejects_bad_checksum() -> None:
    frame = bytes([OPC_GPON, 0x00])
    assert not verify_checksum(frame)


def test_verify_rejects_short_frame() -> None:
    assert not verify_checksum(b"")
    assert not verify_checksum(b"\x83")


# --- frame_length -----------------------------------------------------------


def test_frame_length_for_fixed_opcodes() -> None:
    assert frame_length(bytes([OPC_GPON, 0x7C])) == 2
    assert frame_length(bytes([OPC_SW_REQ, 0, 0, 0])) == 4


def test_frame_length_for_variable() -> None:
    # Variable-length opcodes report length in byte[1].
    assert frame_length(bytes([OPC_SL_RD_DATA, 14])) == 14


def test_frame_length_invalid_opcode_returns_none() -> None:
    assert frame_length(b"") is None
    assert frame_length(b"\x05") is None


# --- read_frame -------------------------------------------------------------


class _Source:
    """Helper turning a bytestring into the read_byte / read_exact pair."""

    def __init__(self, data: bytes) -> None:
        self._q: deque[int] = deque(data)

    async def read_byte(self) -> int:
        if not self._q:
            raise EOFError
        return self._q.popleft()

    async def read_exact(self, n: int) -> bytes:
        out = bytearray()
        for _ in range(n):
            out.append(await self.read_byte())
        return bytes(out)


async def test_read_frame_reads_2byte_message() -> None:
    src = _Source(bytes([0x83, 0xFF ^ 0x83]))
    frame = await read_frame(src.read_byte, src.read_exact)
    assert frame == bytes([0x83, 0x7C])


async def test_read_frame_reads_4byte_message() -> None:
    body = bytes([OPC_SW_REQ, 0x05, 0x30])
    frame = append_checksum(body)
    src = _Source(frame)
    out = await read_frame(src.read_byte, src.read_exact)
    assert out == frame


async def test_read_frame_reads_variable_length_message() -> None:
    # Build a 14-byte OPC_SL_RD_DATA frame manually.
    body = bytes([OPC_SL_RD_DATA, 14, 5, 0x30, 3, 0, 0, 0, 0, 0, 0, 0, 0])
    frame = append_checksum(body)
    assert len(frame) == 14
    src = _Source(frame)
    out = await read_frame(src.read_byte, src.read_exact)
    assert out == frame


async def test_read_frame_resyncs_past_noise() -> None:
    # Three data bytes (high bit clear) before a real frame.
    noise = bytes([0x00, 0x7F, 0x55])
    real = append_checksum(bytes([OPC_GPON]))
    src = _Source(noise + real)
    out = await read_frame(src.read_byte, src.read_exact)
    assert out == real


def test_event_loop_can_run_the_async_helpers() -> None:
    """Sanity check: tests use the asyncio_mode=auto pytest_asyncio integration."""
    assert asyncio.get_event_loop_policy() is not None
    with pytest.raises(EOFError):
        asyncio.run(_Source(b"").read_byte())
