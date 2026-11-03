"""``ThrottleManager`` -- acquire/release throttle handles per DCC address.

Throttle objects aren't NamedBeans (they're transient holds on a slot in
the command station), so they don't fit the regular ``Manager[T]`` shape.
A ``ThrottleManager`` instead provides:

* ``acquire(address, long_address)`` -> :class:`Throttle` -- get an existing
  throttle for *address* if one is already held, or create a new one.
* ``release(throttle)`` -- let go of the throttle; subsequent ``acquire``
  with the same address will create a fresh one.
* iteration yields every active throttle.

Drivers override :meth:`_construct` to return their hardware-aware
``Throttle`` subclass (e.g. ``LnThrottle``). The base in-memory impl is
used by ``InternalConnection`` and by tests.
"""
from __future__ import annotations

from collections.abc import Iterator

from pmri_core.beans.throttle import Throttle
from pmri_core.events import Signal


class ThrottleManager:
    """Acquire/release throttle handles by DCC address."""

    def __init__(self, system_prefix: str) -> None:
        if not system_prefix:
            raise ValueError("system_prefix must be a non-empty string")
        self.system_prefix = system_prefix
        self._throttles: dict[int, Throttle] = {}
        self.acquired: Signal[Throttle] = Signal()
        self.released: Signal[Throttle] = Signal()

    async def acquire(self, address: int, *, long_address: bool = False) -> Throttle:
        if address in self._throttles:
            return self._throttles[address]
        throttle = await self._construct(address, long_address=long_address)
        self._throttles[address] = throttle
        await self.acquired.emit(throttle)
        return throttle

    async def release(self, throttle: Throttle) -> None:
        existing = self._throttles.get(throttle.address)
        if existing is not throttle:
            return
        del self._throttles[throttle.address]
        await self.released.emit(throttle)

    def get(self, address: int) -> Throttle | None:
        return self._throttles.get(address)

    def __iter__(self) -> Iterator[Throttle]:
        return iter(self._throttles.values())

    def __len__(self) -> int:
        return len(self._throttles)

    async def _construct(self, address: int, *, long_address: bool) -> Throttle:
        """Build the throttle for *address*. Driver hook."""
        return Throttle(address=address, long_address=long_address)
