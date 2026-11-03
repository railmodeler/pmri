"""``BytePort`` -- the minimum surface an NCE transport needs to provide.

Unlike LocoNet's frame-oriented transport, NCE is plain byte-stream: write
a request, read an exact number of reply bytes, read one ACK byte.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BytePort(ABC):
    """Bidirectional byte-stream port for NCE."""

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def read_exact(self, n: int) -> bytes: ...

    @abstractmethod
    async def read_pending(self) -> bytes:
        """Read any bytes already buffered, without waiting for more.

        Used by the traffic controller to drain stale bytes between
        commands. Returns ``b""`` if nothing is currently buffered.
        """

    @abstractmethod
    async def aclose(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...
