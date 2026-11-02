"""``LocoNetPort`` ABC -- the minimum surface a transport must provide.

The :class:`~jmri_loconet.traffic.LocoNetTrafficController` reads complete
frames using :meth:`read_byte` + :meth:`read_exact` (so the codec can
resync mid-stream after noise) and writes complete pre-framed bytes via
:meth:`write_frame`. Closing is :meth:`aclose`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LocoNetPort(ABC):
    """Abstract bidirectional byte-stream LocoNet port."""

    @abstractmethod
    async def read_byte(self) -> int:
        """Read one byte. Blocks until a byte is available."""

    @abstractmethod
    async def read_exact(self, n: int) -> bytes:
        """Read exactly *n* bytes."""

    @abstractmethod
    async def write_frame(self, frame: bytes) -> None:
        """Write a complete LocoNet frame (opcode + payload + checksum)."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close the port. Idempotent."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...
