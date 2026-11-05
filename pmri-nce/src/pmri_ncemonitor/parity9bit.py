"""9-bit-via-parity -> high-bit-marker adapter.

Some NCE Cab Bus adapters carry the address-vs-data distinction on
the 9th bit but expose it to userland as mark/space parity rather
than as the high bit of the received byte. Operators who go that
route need to feed the codec (which expects the high-bit-marker
convention) a translated stream.

This module provides :class:`Parity9BitDecoder`, a
:class:`~pmri_ncemonitor.transport.port.BytePort`-compatible wrapper
that takes a parity-tagged byte source and produces the standard
high-bit-marker byte stream.

Usage sketch::

    async def read_tagged() -> tuple[int, bool]:
        # ... driver-specific parity-aware read
        return byte_value, is_address_byte

    port = Parity9BitDecoder(read_tagged, base_port_for_close)
    monitor = CabBusMonitor(port)

Kept in a separate module so the pyserial branch (which does the
raw parity-aware read) can live behind an optional dependency
without hurting the pure decoder logic.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from pmri_ncemonitor.transport.port import BytePort


class Parity9BitDecoder(BytePort):
    """Translate a parity-tagged byte stream into the high-bit-marker form.

    Constructor takes:

    * ``read_tagged`` -- awaitable returning ``(byte_value, is_address)``.
      ``byte_value`` is the 7-bit payload; ``is_address`` indicates
      whether this byte was received with mark parity (or however the
      operator's driver signals address-ness).
    * ``base_port`` -- the underlying byte port. ``aclose`` is
      delegated so lifecycle stays sane; ``read_byte`` is overridden
      by this class.

    The returned bytes carry the high bit set when ``is_address`` is
    true, matching :func:`pmri_ncemonitor.codec.is_address_byte`.
    """

    def __init__(
        self,
        read_tagged: Callable[[], Awaitable[tuple[int, bool]]],
        base_port: BytePort,
    ) -> None:
        self._read_tagged = read_tagged
        self._base = base_port

    async def read_byte(self) -> int:
        value, is_address = await self._read_tagged()
        # Force 7-bit payload; some drivers pass through a full byte
        # that already has bit 7 clear/set -- we mask so the semantic
        # comes only from ``is_address``.
        value &= 0x7F
        return (value | 0x80) if is_address else value

    async def write(self, data: bytes) -> None:  # pragma: no cover - passive
        raise RuntimeError(
            "NCE Cab Bus monitor is passive -- writes are not supported"
        )

    async def aclose(self) -> None:
        await self._base.aclose()

    @property
    def is_open(self) -> bool:
        return self._base.is_open
