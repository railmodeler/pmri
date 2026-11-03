"""``TcpPort`` -- LocoNet over TCP (LbServer / network bridge)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pmri_loconet.transport.port import LocoNetPort

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


class TcpPort(LocoNetPort):
    """LocoNet over a TCP socket (e.g. JMRI's LbServer or Digitrax LNWI)."""

    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._open = True

    @classmethod
    async def open(cls, host: str, port: int) -> TcpPort:
        reader, writer = await asyncio.open_connection(host, port)
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
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    @property
    def is_open(self) -> bool:
        return self._open
