from __future__ import annotations

import pytest

from jmri_ncemonitor.codec import (
    MAX_BODY_BYTES,
    BodyOverflowError,
    PeekableReader,
    address_of,
    is_address_byte,
    read_message_peekable,
)


def test_address_byte_detection() -> None:
    assert is_address_byte(0x80)
    assert is_address_byte(0xC3)
    assert not is_address_byte(0x7F)
    assert not is_address_byte(0x00)


def test_address_extraction() -> None:
    assert address_of(0x80) == 0x00
    assert address_of(0xC3) == 0x43
    assert address_of(0xFF) == 0x7F


class _ByteIterator:
    """Wraps a list of bytes as an async ``read_byte`` callable."""

    def __init__(self, data: bytes) -> None:
        self._data = list(data)
        self._i = 0

    async def read_byte(self) -> int:
        if self._i >= len(self._data):
            raise EOFError
        b = self._data[self._i]
        self._i += 1
        return b


async def test_read_one_complete_message() -> None:
    # addr=0x80 (cab 0), payload 0x00 then another addr starts.
    src = _ByteIterator(bytes([0x80, 0x00, 0x81]))
    reader = PeekableReader(src.read_byte)
    msg = await read_message_peekable(reader)
    assert msg == bytes([0x80, 0x00])
    # The 0x81 must be pushed back and visible on the next read.
    msg2 = await read_message_peekable(reader)
    assert msg2[0] == 0x81


async def test_read_skips_leading_noise() -> None:
    # Two non-address bytes (mid-message garbage) before a real frame.
    src = _ByteIterator(bytes([0x10, 0x55, 0x80, 0x00]))
    reader = PeekableReader(src.read_byte)
    msg = await read_message_peekable(reader)
    assert msg == bytes([0x80, 0x00])


async def test_eof_returns_partial_message() -> None:
    src = _ByteIterator(bytes([0x80, 0x00, 0x01]))
    reader = PeekableReader(src.read_byte)
    msg = await read_message_peekable(reader)
    # EOF mid-data -> return what we have.
    assert msg == bytes([0x80, 0x00, 0x01])


async def test_peekable_pushback_is_consumed_first() -> None:
    src = _ByteIterator(bytes([0xAA]))
    reader = PeekableReader(src.read_byte)
    reader.push_back(0x99)
    assert (await reader.read_byte()) == 0x99
    assert (await reader.read_byte()) == 0xAA


# ---- Tier 4 #13: max-body cap + zero-body frames -----------------------


async def test_body_overflow_raises_with_partial() -> None:
    """A stuck bus (address byte followed by ``MAX_BODY_BYTES + N``
    data bytes with no next address) must raise
    :class:`BodyOverflowError` so the monitor can resync -- otherwise
    the read buffer grows without bound. The exception carries the
    partial for diagnostics."""
    # Address then MAX_BODY_BYTES + 5 non-address bytes and no
    # boundary -- read must give up after MAX_BODY_BYTES.
    src = _ByteIterator(
        bytes([0x80]) + bytes([0x00] * (MAX_BODY_BYTES + 5))
    )
    reader = PeekableReader(src.read_byte)
    with pytest.raises(BodyOverflowError) as exc:
        await read_message_peekable(reader)
    # ``partial`` includes the address byte + the data bytes we
    # captured before giving up (address + MAX_BODY_BYTES + 1
    # overflow byte, which is what tripped the cap).
    assert exc.value.partial[0] == 0x80
    assert len(exc.value.partial) == MAX_BODY_BYTES + 2


async def test_zero_body_frame_returns_address_only() -> None:
    """Two address bytes back-to-back -> the first is a zero-body
    frame; the second gets pushed back for the next call. Real Cab
    Bus traffic occasionally produces this (a stray poll with no
    body). Must not crash or silently drop."""
    src = _ByteIterator(bytes([0x80, 0x81, 0x00]))
    reader = PeekableReader(src.read_byte)
    msg = await read_message_peekable(reader)
    assert msg == bytes([0x80])
    # The 0x81 was pushed back; next read sees it as a fresh
    # address byte.
    msg2 = await read_message_peekable(reader)
    assert msg2 == bytes([0x81, 0x00])


async def test_body_at_exactly_max_is_returned() -> None:
    """A body of exactly ``MAX_BODY_BYTES`` succeeds -- the cap is
    strictly greater-than."""
    src = _ByteIterator(
        bytes([0x80]) + bytes([0x00] * MAX_BODY_BYTES) + bytes([0x81])
    )
    reader = PeekableReader(src.read_byte)
    msg = await read_message_peekable(reader)
    assert len(msg) == MAX_BODY_BYTES + 1
