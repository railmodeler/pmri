"""Programming session -- binds a decoder definition + programmer.

A :class:`ProgrammingSession` is the top-level handle DecoderPro hands
to clients. It knows:

* which :class:`DecoderDefinition` is on the programming track (or the
  ops-mode loco being modified);
* which :class:`Programmer` to drive;
* the current cache of CV values read so far.

Two flavors:

* :meth:`from_global` -- service-mode session against the programming
  track. Reads work on any decoder.
* :meth:`from_ops` -- ops-mode session against a running loco; reads
  only succeed with RailCom-capable hardware.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from jmri_core.beans.programmer import Programmer, ProgrammingMode
from jmri_core.hal.connection import SystemConnection

from jmri_decoderpro.catalog import DecoderDefinition

_log = logging.getLogger(__name__)


@dataclass
class CvCacheEntry:
    """One observed CV value + the time it was read."""

    value: int
    read_at: float       # monotonic seconds when we last read it
    dirty: bool = False  # True if local writes haven't been pushed yet


@dataclass
class ProgrammingSession:
    """Active CV programming session."""

    definition: DecoderDefinition
    programmer: Programmer
    cv_cache: dict[int, CvCacheEntry] = field(default_factory=dict)

    @property
    def mode(self) -> ProgrammingMode:
        return self.programmer.mode

    # ----- construction ----------------------------------------------------

    @classmethod
    def from_global(
        cls, connection: SystemConnection, definition: DecoderDefinition
    ) -> ProgrammingSession:
        """Service-mode session -- uses the connection's global programmer."""
        programmer = connection.programmers.global_programmer()
        if programmer is None:
            raise RuntimeError(
                f"connection {connection.name!r} has no global programmer"
            )
        return cls(definition=definition, programmer=programmer)

    @classmethod
    def from_ops(
        cls,
        connection: SystemConnection,
        definition: DecoderDefinition,
        *,
        address: int,
        long_address: bool = False,
    ) -> ProgrammingSession:
        """Ops-mode session for the loco at *address*."""
        programmer = connection.programmers.ops_programmer(
            address, long_address=long_address
        )
        if programmer is None:
            raise RuntimeError(
                f"connection {connection.name!r} has no ops-mode programmer"
            )
        return cls(definition=definition, programmer=programmer)

    # ----- CV access -------------------------------------------------------

    async def read_cv(self, cv: int) -> int:
        """Read one CV, caching the result for later lookups."""
        value = await self.programmer.read_cv(cv)
        self.cv_cache[cv] = CvCacheEntry(
            value=value, read_at=asyncio.get_event_loop().time(), dirty=False,
        )
        return value

    async def write_cv(self, cv: int, value: int) -> None:
        """Write one CV, also updating the local cache."""
        if not 0 <= value <= 255:
            raise ValueError(f"CV value must be 0..255, got {value}")
        await self.programmer.write_cv(cv, value)
        self.cv_cache[cv] = CvCacheEntry(
            value=value, read_at=asyncio.get_event_loop().time(), dirty=False,
        )

    async def read_all(self) -> dict[int, int]:
        """Read every CV the decoder definition exposes. Returns a {cv: value} dict.

        Variables with non-integer CV numbers (e.g., "17.18" for long-address
        composite) are skipped -- those need dedicated split-CV handling.
        """
        out: dict[int, int] = {}
        for variable in self.definition.variables:
            try:
                cv_num = int(variable.cv)
            except ValueError:
                continue
            if cv_num in out:
                continue
            try:
                out[cv_num] = await self.read_cv(cv_num)
            except Exception as e:  # noqa: BLE001 -- keep reading the rest
                _log.warning("read_all: CV%d failed (%s)", cv_num, e)
        return out

    # ----- identity --------------------------------------------------------

    async def read_identity(self) -> tuple[int, int]:
        """Read CV7 (version) and CV8 (manufacturer). Common for "detect decoder"."""
        return await self.programmer.read_cv(7), await self.programmer.read_cv(8)

    async def read_identity_extended(self) -> tuple[int, int, int | None]:
        """Full identity: CV7 (version) + CV8 (mfg) + productID probe.

        The productID probe is per-manufacturer -- see
        :mod:`jmri_decoderpro.probes`. Returns ``product_id=None``
        when no probe is registered for the mfg, when the probe
        determined this model doesn't populate the productID CVs,
        or when a wire error interrupted the probe. Callers pass
        the returned productID to
        :meth:`~jmri_decoderpro.catalog.DecoderCatalog.identify_by_cv_extended`
        to narrow the identify picker beyond CV7/CV8 alone.
        """
        from jmri_decoderpro.probes import read_product_id  # noqa: PLC0415

        cv7 = await self.programmer.read_cv(7)
        cv8 = await self.programmer.read_cv(8)
        product_id = await read_product_id(self, mfg_id=cv8, model_id=cv7)
        return cv7, cv8, product_id
