"""``MrcConnection`` -- entry-point placeholder for MRC Prodigy.

Behaves like :class:`pmri_core.hal.InternalConnection` -- an
in-memory, no-I/O system connection -- but reports its own
system prefix (``M``) and name (``MRC``) so the web UI shows
"MRC" instead of "Internal" when an MRC operator picks this
driver in the PMRI launcher.

The real Prodigy serial transport (framing, polling model, bean
managers) hasn't been implemented yet. When the proper driver
lands, this class gets fleshed out with the usual connection
machinery; existing profile.xml entries with ``type="mrc"``
continue to resolve to the same entry point without migration.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from pmri_core.hal.connection import ConnectionSettings, SystemConnection

_log = logging.getLogger(__name__)


class MrcConnection(SystemConnection):
    """Stub MRC Prodigy connection -- in-memory, logs a warning on
    open() so an operator sees why hardware traffic isn't moving."""

    name: ClassVar[str] = "MRC"
    prefix: ClassVar[str] = "M"

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        _log.warning(
            "pmri-mrc driver is a stub -- no Prodigy serial transport "
            "implemented; connection accepts commands but produces "
            "no hardware traffic. Install a real pmri-mrc driver "
            "when it ships.",
        )
        await super().open(settings)

    async def close(self) -> None:
        await super().close()
