"""``DigitraxConnection`` -- entry-point placeholder for Digitrax command stations.

Behaves like :class:`pmri_core.hal.InternalConnection` -- an in-memory,
no-I/O system connection -- but reports its own system prefix (``L``,
matching JMRI's convention for LocoNet) and name (``Digitrax``) so
the web UI shows "Digitrax" instead of "Internal" when a Digitrax
operator picks this driver in the PMRI launcher.

A future full driver will compose ``pmri_loconet``'s wire-protocol
transport with Digitrax-specific command-station behaviour (track
power, service-mode programming, slot addressing). Until that work
lands, this stub lets operators see their hardware represented in
the UI without the server crashing on an unknown-connection error.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from pmri_core.hal.connection import ConnectionSettings, SystemConnection

_log = logging.getLogger(__name__)


class DigitraxConnection(SystemConnection):
    """Stub Digitrax command-station connection -- in-memory, logs a
    warning on open() so an operator sees why hardware traffic isn't
    moving."""

    name: ClassVar[str] = "Digitrax"
    prefix: ClassVar[str] = "L"

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        _log.warning(
            "pmri-digitrax driver is a stub -- no Digitrax command-station "
            "behaviour implemented; connection accepts commands but "
            "produces no hardware traffic. Install a real pmri-digitrax "
            "driver when it ships, or use pmri-loconet directly if you "
            "only need LocoNet protocol access (no command-station "
            "control).",
        )
        await super().open(settings)

    async def close(self) -> None:
        await super().close()
