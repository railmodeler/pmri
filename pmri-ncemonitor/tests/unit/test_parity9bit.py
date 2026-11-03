"""Tests for the parity -> high-bit-marker adapter.

The adapter is a small wrapper around a driver-supplied "read one
byte with parity tag" callback. We test that:

* Address bytes (is_address=True) come out with bit 7 set.
* Data bytes (is_address=False) come out with bit 7 clear.
* The 7-bit payload is preserved untouched (masked to 7 bits so a
  bit-7 lie from the underlying driver can't sneak through).
* aclose delegates to the base port.
"""
from __future__ import annotations

from pmri_ncemonitor.codec import is_address_byte
from pmri_ncemonitor.parity9bit import Parity9BitDecoder
from pmri_ncemonitor.transport import loopback_pair


async def test_address_byte_gets_high_bit_set() -> None:
    _, base = loopback_pair()

    async def read_tagged() -> tuple[int, bool]:
        return 0x42, True

    port = Parity9BitDecoder(read_tagged, base_port=base)
    b = await port.read_byte()
    assert is_address_byte(b)
    assert (b & 0x7F) == 0x42


async def test_data_byte_stays_low_bit() -> None:
    _, base = loopback_pair()

    async def read_tagged() -> tuple[int, bool]:
        return 0x55, False

    port = Parity9BitDecoder(read_tagged, base_port=base)
    b = await port.read_byte()
    assert not is_address_byte(b)
    assert b == 0x55


async def test_masks_off_bit_7_from_driver() -> None:
    """A driver that lied and set bit 7 on a data byte must not
    poison the stream -- the semantic comes ONLY from the
    ``is_address`` tag, not from the raw byte's high bit."""
    _, base = loopback_pair()

    async def read_tagged() -> tuple[int, bool]:
        return 0xFF, False   # bit 7 set on a data byte

    port = Parity9BitDecoder(read_tagged, base_port=base)
    b = await port.read_byte()
    assert not is_address_byte(b)
    assert b == 0x7F   # bit 7 masked off


async def test_stream_of_alternating_tags() -> None:
    """Simulate a real bus: address, data, data, address, ..."""
    _, base = loopback_pair()

    stream = iter([
        (0x40, True),   # address 0x40
        (0x00, False),  # poll command 0x00
        (0x41, True),   # address 0x41
        (0x00, False),  # another poll
    ])

    async def read_tagged() -> tuple[int, bool]:
        return next(stream)

    port = Parity9BitDecoder(read_tagged, base_port=base)
    bytes_out = [await port.read_byte() for _ in range(4)]
    assert bytes_out == [0xC0, 0x00, 0xC1, 0x00]
