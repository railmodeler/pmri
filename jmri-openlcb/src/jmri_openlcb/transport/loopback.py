from __future__ import annotations

import asyncio

from jmri_openlcb.transport.port import BytePort


class LoopbackPort(BytePort):
    def __init__(self, read_q: asyncio.Queue[int], write_q: asyncio.Queue[int]) -> None:
        self._r = read_q
        self._w = write_q
        self._open = True

    async def write(self, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("port is closed")
        for b in data:
            await self._w.put(b)

    async def read_byte(self) -> int:
        return await self._r.get()

    async def aclose(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


def loopback_pair() -> tuple[LoopbackPort, LoopbackPort]:
    a_to_b: asyncio.Queue[int] = asyncio.Queue()
    b_to_a: asyncio.Queue[int] = asyncio.Queue()
    return LoopbackPort(b_to_a, a_to_b), LoopbackPort(a_to_b, b_to_a)
