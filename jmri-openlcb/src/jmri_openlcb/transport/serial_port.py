"""pyserial-asyncio OpenLCB transport -- for USB-to-CAN adapters that present
as serial GridConnect."""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from jmri_openlcb.transport.port import BytePort

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


DEFAULT_BAUD = 333333   # CAN bus typical; some bridges use 115200 ASCII speed


class SerialPort(BytePort):
    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._open = True

    @classmethod
    async def open(cls, port: str, baud: int = DEFAULT_BAUD) -> SerialPort:
        import serial_asyncio  # noqa: PLC0415
        reader, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baud)
        return cls(reader=reader, writer=writer)

    async def write(self, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("port is closed")
        self._writer.write(data)
        await self._writer.drain()

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
