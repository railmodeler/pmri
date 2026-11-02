"""``BytePort`` ABC -- bidirectional byte stream."""
from __future__ import annotations

from abc import ABC, abstractmethod


class BytePort(ABC):
    @abstractmethod
    async def write(self, data: bytes) -> None: ...
    @abstractmethod
    async def read_byte(self) -> int: ...
    @abstractmethod
    async def aclose(self) -> None: ...
    @property
    @abstractmethod
    def is_open(self) -> bool: ...
