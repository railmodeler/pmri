"""Frame-level codec: read whole LocoNet frames from a byte stream, verify
checksums, and build new frames with correct checksums.

This module is *protocol-shape only* -- it doesn't know what opcodes mean.
The :mod:`pmri_loconet.messages` module sits on top and turns frame bytes
into typed message dataclasses.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from pmri_loconet.opcodes import (
    expected_length,
    is_valid_opcode_byte,
    length_class_of,
)


def compute_checksum(body: bytes) -> int:
    """Return the LocoNet checksum byte for *body* (everything before the CS).

    The checksum is ``0xFF XOR`` of every byte in *body*.
    """
    cs = 0xFF
    for b in body:
        cs ^= b
    return cs


def verify_checksum(frame: bytes) -> bool:
    """True if the trailing byte of *frame* is its correct checksum."""
    if len(frame) < 2:
        return False
    return compute_checksum(frame[:-1]) == frame[-1]


def append_checksum(body: bytes) -> bytes:
    """Append a correct checksum to *body* and return the complete frame."""
    return body + bytes([compute_checksum(body)])


def frame_length(frame: bytes) -> int | None:
    """Return the expected total length of *frame* given its leading opcode.

    For fixed-length frames returns 2/4/6 directly. For variable-length
    frames returns the value in ``frame[1]`` if present, else ``None``.
    Returns ``None`` for any leading byte that isn't a valid opcode.
    """
    if not frame or not is_valid_opcode_byte(frame[0]):
        return None
    fixed = expected_length(frame[0])
    if fixed is not None:
        return fixed
    if len(frame) < 2:
        return None
    return frame[1]


async def read_frame(
    read_byte: Callable[[], Awaitable[int]],
    read_exact: Callable[[int], Awaitable[bytes]],
) -> bytes:
    """Read one complete LocoNet frame from a byte stream.

    *read_byte* returns a single byte; *read_exact(n)* returns exactly *n*
    bytes. Non-opcode bytes seen before an opcode (e.g. mid-message noise)
    are silently dropped -- matching JMRI's resync behaviour.
    """
    # Resync to the next opcode byte.
    while True:
        b = await read_byte()
        if is_valid_opcode_byte(b):
            opcode = b
            break

    cls = length_class_of(opcode)
    if cls == 0b11:
        length_byte = await read_byte()
        rest = await read_exact(length_byte - 2)
        return bytes([opcode, length_byte]) + rest

    total = expected_length(opcode)
    assert total is not None  # cls != 0b11 guarantees a known fixed length
    rest = await read_exact(total - 1)
    return bytes([opcode]) + rest
