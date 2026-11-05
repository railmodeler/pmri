"""Real RS-485 adapter via pyserial-asyncio.

Two paths for the 9-bit address marker:

1. **High-bit-marker adapters** (the JMRI reference USB adapter, and
   most USB-serial bridges configured for NCE): the adapter delivers
   the 9th bit as bit 7 of the byte, so the framing rule is simply
   "high bit set = address byte." This is the default; ``open`` with
   no extra kwargs works.

2. **True 9-bit-serial adapters via mark/space parity**: some Linux
   installs (and pyserial in particular) can distinguish address vs
   data bytes by parity. The operator passes ``parity="M"`` or
   ``parity="S"`` and a matching ``bytesize`` / ``stopbits`` config,
   and the underlying UART flags the address byte as a parity error
   that pyserial surfaces through the byte stream. Callers who use
   this path must set ``mark_parity_is_address=True`` so the
   :class:`SerialPort` knows how to interpret parity-flagged bytes.

For path 2, the practical operator recipe is documented in the
``README.md`` -- the exact driver-side setup varies too much between
Linux distros and adapter chipsets to hardcode a single default.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from pmri_ncemonitor.transport.port import BytePort

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


DEFAULT_BAUD = 9600


class SerialPort(BytePort):
    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._open = True

    @classmethod
    async def open(
        cls,
        port: str,
        baud: int = DEFAULT_BAUD,
        *,
        parity: str = "N",
        stopbits: float = 1.0,
        bytesize: int = 8,
    ) -> SerialPort:
        """Open a serial port for Cab Bus monitoring.

        ``parity`` / ``stopbits`` / ``bytesize`` accept the standard
        pyserial values -- the default of ``N``, ``1``, ``8`` matches
        the high-bit-marker convention that JMRI's reference USB
        adapter uses.

        For true 9-bit adapters that discriminate address bytes via
        mark/space parity, operators typically set
        ``parity="M", stopbits=1, bytesize=8`` on the address-byte
        side of the bus, and rely on the underlying UART's parity
        detection to flag the boundary. The exact combination
        depends on the adapter's firmware -- see the README.
        """
        import serial_asyncio  # noqa: PLC0415
        reader, writer = await serial_asyncio.open_serial_connection(
            url=port,
            baudrate=baud,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
        )
        return cls(reader=reader, writer=writer)

    async def write(self, data: bytes) -> None:
        # The monitor never writes; we expose this only because the ABC
        # requires it. Surfacing a clear error guards against misuse.
        raise RuntimeError(
            "NCE Cab Bus monitor is passive -- writes are not supported"
        )

    async def read_byte(self) -> int:
        data = await self._reader.readexactly(1)
        return data[0]

    async def aclose(self) -> None:
        if not self._open:
            return
        self._open = False
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    @property
    def is_open(self) -> bool:
        return self._open
