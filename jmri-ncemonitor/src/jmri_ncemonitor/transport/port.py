"""``BytePort`` ABC -- read-only-by-convention byte stream.

We define :meth:`write` for symmetry with other drivers, but the monitor
never calls it. Subclasses that connect to real RS-485 adapters may
implement ``write`` as a raise; the loopback impl supports writes so
tests can feed traffic *to* the monitor.
"""
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
