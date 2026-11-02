"""NCE Cab Bus framing -- read 9-bit-marker framed messages.

The Cab Bus is a polled RS-485 bus operating at 9600 baud where the
command station addresses each cab in turn. Production hardware uses
9-bit serial; some USB-serial adapters can't deliver the 9th bit, so
both JMRI and we use a "high bit set = address byte" wire convention
when reading from such adapters. We adopt the same convention here:

* If ``byte & 0x80 != 0`` -> this byte is the *address* and starts a new
  message frame. The low 7 bits are the cab ID (or master ID).
* All following bytes (up to a per-address length) are 7-bit data.

This module is byte-stream level only; the message-level decoding lives
in :mod:`jmri_ncemonitor.messages`.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable


def is_address_byte(b: int) -> bool:
    """True if *b* is an address byte (high bit set)."""
    return (b & 0x80) != 0


def address_of(b: int) -> int:
    """Extract the 7-bit address from an address byte."""
    return b & 0x7F


class PeekableReader:
    """Small wrapper around an async ``read_byte`` to support a 1-byte pushback.

    The Cab Bus message boundary is "first byte with high bit set"; we
    don't know we've crossed it until we read the byte. ``PeekableReader``
    lets the codec push back that boundary byte for the next call to see.
    """

    def __init__(self, read_byte: Callable[[], Awaitable[int]]) -> None:
        self._read = read_byte
        self._buf: int | None = None

    def push_back(self, b: int) -> None:
        if self._buf is not None:
            raise RuntimeError("PeekableReader already has a pushed-back byte")
        self._buf = b

    async def read_byte(self) -> int:
        if self._buf is not None:
            b, self._buf = self._buf, None
            return b
        return await self._read()


# Longest legitimate Cab Bus frame is ~10 bytes (address + 9-byte cab
# response). Anything past 32 bytes without a boundary is a stuck bus
# or a wildly misparsed stream -- cap so we don't grow the buffer
# unboundedly on a jammed link and eventually raise cleanly.
MAX_BODY_BYTES = 32


class BodyOverflowError(RuntimeError):
    """Raised when a single frame's body exceeds :data:`MAX_BODY_BYTES`.

    The monitor's read loop logs this at WARNING and resyncs to the
    next address byte on the wire. The raised instance carries the
    partial buffer so operators can inspect what showed up on the
    bus when the resync fired.
    """

    def __init__(self, partial: bytes) -> None:
        super().__init__(
            f"cab bus frame exceeded {MAX_BODY_BYTES}-byte cap "
            f"(no address byte for {len(partial) - 1} data bytes)"
        )
        self.partial = partial


async def read_message_peekable(reader: PeekableReader) -> bytes:
    """Read one message using a :class:`PeekableReader` for clean pushback.

    Raises :class:`BodyOverflowError` if the frame grows past
    :data:`MAX_BODY_BYTES` without seeing the next address byte.
    Callers (e.g. :class:`CabBusMonitor`) treat that as a resync
    signal -- drop the partial and read the next address byte fresh.
    """
    # Resync to the next address byte.
    while True:
        b = await reader.read_byte()
        if is_address_byte(b):
            addr = b
            break

    out = bytearray([addr])
    while True:
        try:
            b = await reader.read_byte()
        except EOFError:
            break
        if is_address_byte(b):
            reader.push_back(b)
            break
        out.append(b)
        # Cap counts data bytes only (out[0] is the address byte).
        # Strictly greater -- a body of exactly MAX_BODY_BYTES is
        # legitimate (JMRI-documented frames don't exceed 9 bytes,
        # but we allow generous headroom for uncommon variants).
        if len(out) - 1 > MAX_BODY_BYTES:
            # Don't push back -- no address boundary in sight. The
            # partial we return via the exception is useful for
            # diagnostics; the read loop resyncs on the next call.
            raise BodyOverflowError(bytes(out))
    return bytes(out)
