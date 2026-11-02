"""``LenzConnection`` -- entry-point placeholder for Lenz XpressNet.

Behaves like :class:`jmri_core.hal.InternalConnection` -- an
in-memory, no-I/O system connection -- but reports its own
system prefix (``X``) and name (``Lenz``) so the web UI shows
"Lenz" instead of "Internal" when a Lenz operator picks this
driver in the PMRI launcher.

The real XpressNet transport (LI-USB serial, framing, response
multiplexing) hasn't been implemented yet. When the proper driver
lands, this class gets fleshed out with the usual connection
machinery; existing profile.xml entries with ``type="lenz"``
continue to resolve to the same entry point without migration.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from jmri_core.hal.connection import ConnectionSettings, SystemConnection

_log = logging.getLogger(__name__)


class LenzConnection(SystemConnection):
    """Stub Lenz XpressNet connection -- in-memory, logs a warning
    on open() so an operator sees why hardware traffic isn't moving."""

    name: ClassVar[str] = "Lenz"
    prefix: ClassVar[str] = "X"

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        _log.warning(
            "jmri-lenz driver is a stub -- no XpressNet transport "
            "implemented; connection accepts commands but produces "
            "no hardware traffic. Install a real jmri-lenz driver "
            "when it ships.",
        )
        await super().open(settings)

    async def close(self) -> None:
        await super().close()
