"""``SerialPort`` -- pyserial-asyncio LocoNet transport.

Wraps a ``serial_asyncio`` ``StreamReader``/``StreamWriter`` pair into the
:class:`LocoNetPort` interface. Used for real LocoBuffer-II / PR3 / PR4
connections.

Default settings match JMRI's defaults: 16,457 baud, 8N1, no flow control.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from jmri_loconet.transport.port import LocoNetPort

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

# JMRI's LocoBufferII default; lower-end LocoBuffer-I uses 19200, PR3 uses 57600.
DEFAULT_BAUD = 16457


class SerialPort(LocoNetPort):
    """LocoNet over an async serial port."""

    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._open = True

    @classmethod
    async def open(cls, port: str, baud: int = DEFAULT_BAUD) -> SerialPort:
        """Open the serial *port* (e.g. ``"COM5"`` or ``"/dev/loconet"``)."""
        # Import lazily so the package imports cleanly on systems without
        # pyserial installed (tests use the loopback transport).
        import serial_asyncio  # noqa: PLC0415
        reader, writer = await serial_asyncio.open_serial_connection(
            url=port, baudrate=baud
        )
        return cls(reader=reader, writer=writer)

    async def read_byte(self) -> int:
        data = await self._reader.readexactly(1)
        return data[0]

    async def read_exact(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    async def write_frame(self, frame: bytes) -> None:
        if not self._open:
            raise RuntimeError("port is closed")
        self._writer.write(frame)
        await self._writer.drain()

    async def aclose(self) -> None:
        if not self._open:
            return
        self._open = False
        self._writer.close()
        import contextlib  # noqa: PLC0415
        with contextlib.suppress(Exception):  # best-effort close
            await self._writer.wait_closed()

    @property
    def is_open(self) -> bool:
        return self._open
