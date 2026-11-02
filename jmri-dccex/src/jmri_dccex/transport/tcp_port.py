"""TCP DCC-EX transport -- for DCC-EX command stations exposed via WiFi/Ethernet."""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from jmri_dccex.transport.port import BytePort

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


class TcpPort(BytePort):
    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._open = True

    @classmethod
    async def open(cls, host: str, port: int) -> TcpPort:
        reader, writer = await asyncio.open_connection(host, port)
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
