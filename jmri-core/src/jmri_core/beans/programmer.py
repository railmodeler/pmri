"""``Programmer`` ABC + ``ProgrammingMode`` enum + ``ProgrammerManager``.

A *programmer* reads and writes Configuration Variables (CVs) on a DCC
decoder. Two operational modes:

* **Service mode** (programming track) -- the decoder is electrically
  isolated from the layout. Slow but works on any decoder; reads always
  succeed when a decoder is on the track. Three sub-modes:
  ``SERVICE_DIRECT`` (modern), ``SERVICE_PAGED`` (legacy), ``SERVICE_REGISTER``
  (oldest). Most drivers expose only DIRECT.
* **Ops mode** (also called "ops mode programming" or "POM") -- write to
  a specific loco *while it's running*. Reads only work with RailCom; on
  a non-RailCom layout, ops-mode reads time out.

A driver implements :class:`Programmer` once for service mode and once
per ops-mode address. :class:`ProgrammerManager` is the registry that
hands these out -- same shape as ``ThrottleManager``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum


class ProgrammingMode(StrEnum):
    """How the programmer talks to the decoder."""

    SERVICE_DIRECT = "service_direct"
    SERVICE_PAGED  = "service_paged"
    SERVICE_REGISTER = "service_register"
    OPS_MODE = "ops_mode"


class Programmer(ABC):
    """Reads and writes one DCC decoder's CVs.

    Subclasses implement the two abstract methods. They may raise
    :class:`asyncio.TimeoutError` if the CS doesn't respond in time, or
    any driver-specific exception on programming errors (no acknowledge
    pulse, decoder refused write, etc.).
    """

    @property
    @abstractmethod
    def mode(self) -> ProgrammingMode:
        """Which mode this programmer instance uses."""

    @abstractmethod
    async def read_cv(self, cv: int) -> int:
        """Read CV *cv*, returns 0..255."""

    @abstractmethod
    async def write_cv(self, cv: int, value: int) -> None:
        """Write *value* to CV *cv*."""


class ProgrammerManager:
    """Hands out programmer instances.

    * :meth:`global_programmer` returns the service-mode programmer (one
      per connection -- the programming track is a singleton).
    * :meth:`ops_programmer(address, long_address)` returns an ops-mode
      programmer scoped to the given DCC address.

    The base class returns ``None`` from both; drivers override.
    """

    def global_programmer(self) -> Programmer | None:
        return None

    def ops_programmer(
        self, address: int, *, long_address: bool = False
    ) -> Programmer | None:
        return None
