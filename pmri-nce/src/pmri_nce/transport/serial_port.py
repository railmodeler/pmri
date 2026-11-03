"""``SerialPort`` -- pyserial-asyncio NCE transport.

PowerCab USB and PowerPro USB present as standard USB-serial devices.
Default baud is 9600 (PowerPro) but some configurations use 19200 or
57600; user is expected to set the right value in the connection settings.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from pmri_core.hal import resolve_serial_port_name

from pmri_nce.transport.port import BytePort

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


DEFAULT_BAUD = 9600  # PowerPro default; PowerCab uses the same.


class SerialPort(BytePort):
    """NCE over an async serial port."""

    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._open = True

    @classmethod
    async def open(cls, port: str, baud: int = DEFAULT_BAUD) -> SerialPort:
        """Open *port*.

        Accepts both bare JMRI-style device names (``"ttyUSB0"``) and
        fully-qualified paths (``"/dev/ttyUSB0"``, ``"COM7"``).
        :func:`resolve_serial_port_name` handles the ``/dev/`` prefix
        on POSIX so a profile.xml authored on JMRI (which stores the
        bare name) opens cleanly without the operator having to edit
        the port string.
        """
        import serial_asyncio  # noqa: PLC0415
        resolved = resolve_serial_port_name(port)
        reader, writer = await serial_asyncio.open_serial_connection(
            url=resolved, baudrate=baud
        )
        return cls(reader=reader, writer=writer)

    async def write(self, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("port is closed")
        self._writer.write(data)
        await self._writer.drain()

    async def read_exact(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    async def read_pending(self) -> bytes:
        """Return whatever bytes are already buffered without waiting.

        ``StreamReader._buffer`` is private API but stable across asyncio
        releases; ``read()`` blocks until at least one byte arrives, which
        is the opposite of what the drain needs.
        """
        buf = self._reader._buffer  # type: ignore[attr-defined]
        if not buf:
            return b""
        n = len(buf)
        return await self._reader.readexactly(n)

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
