"""Loopback byte-pair transport for NCE tests.

Two :class:`LoopbackPort` halves share a pair of asyncio queues; bytes
written into one appear on the other's read stream. The test side wires a
"fake command station" listener that reads requests, computes a reply,
and writes it back.
"""
from __future__ import annotations

import asyncio

from jmri_nce.transport.port import BytePort


class LoopbackPort(BytePort):
    """One half of a connected pair."""

    def __init__(self, read_q: asyncio.Queue[int], write_q: asyncio.Queue[int]) -> None:
        self._r = read_q
        self._w = write_q
        self._open = True

    async def write(self, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("port is closed")
        for b in data:
            await self._w.put(b)

    async def read_exact(self, n: int) -> bytes:
        out = bytearray()
        for _ in range(n):
            out.append(await self._r.get())
        return bytes(out)

    async def read_pending(self) -> bytes:
        """Drain anything currently sitting in the receive queue."""
        out = bytearray()
        while not self._r.empty():
            out.append(self._r.get_nowait())
        return bytes(out)

    async def aclose(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


def loopback_pair() -> tuple[LoopbackPort, LoopbackPort]:
    """Two connected loopback ports -- A writes to B's read stream and vice versa."""
    a_to_b: asyncio.Queue[int] = asyncio.Queue()
    b_to_a: asyncio.Queue[int] = asyncio.Queue()
    return LoopbackPort(b_to_a, a_to_b), LoopbackPort(a_to_b, b_to_a)
