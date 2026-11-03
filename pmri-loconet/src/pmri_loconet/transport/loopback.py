"""In-memory loopback transport -- wires two ports together via asyncio queues.

Used by tests + the integration suite: ``a, b = loopback_pair()`` gives two
:class:`LoopbackPort` instances where bytes written to one appear on the
other's read stream.
"""
from __future__ import annotations

import asyncio

from pmri_loconet.transport.port import LocoNetPort


class LoopbackPort(LocoNetPort):
    """One half of a loopback pair."""

    def __init__(self, read_queue: asyncio.Queue[int], write_queue: asyncio.Queue[int]) -> None:
        self._read = read_queue
        self._write = write_queue
        self._open = True

    async def read_byte(self) -> int:
        return await self._read.get()

    async def read_exact(self, n: int) -> bytes:
        return bytes([await self._read.get() for _ in range(n)])

    async def write_frame(self, frame: bytes) -> None:
        if not self._open:
            raise RuntimeError("port is closed")
        for b in frame:
            await self._write.put(b)

    async def aclose(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


def loopback_pair() -> tuple[LoopbackPort, LoopbackPort]:
    """Return a pair of connected loopback ports."""
    a_to_b: asyncio.Queue[int] = asyncio.Queue()
    b_to_a: asyncio.Queue[int] = asyncio.Queue()
    a = LoopbackPort(read_queue=b_to_a, write_queue=a_to_b)
    b = LoopbackPort(read_queue=a_to_b, write_queue=b_to_a)
    return a, b
