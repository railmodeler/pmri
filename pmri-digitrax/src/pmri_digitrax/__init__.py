"""Digitrax command station driver stub.

Registers the ``digitrax`` entry point so :func:`pmri_server.app.select_connection`
can find it. Digitrax hardware (DCS-xxx command stations, PR3 / PR4
programmers, LocoBuffer-USB interfaces) speaks the LocoNet protocol,
so a full driver would layer Digitrax-specific command-station
behaviour (track power, service-mode programming, slot assignment
details, throttle addressing conventions) on top of ``pmri_loconet``'s
wire-protocol transport. This stub is an entry-point placeholder
until that work lands.

The package exists primarily to reserve the entry-point name and let
the launcher's connection pulldown surface Digitrax without the
server erroring on an unknown driver.
"""
from __future__ import annotations

from pmri_digitrax.connection import DigitraxConnection

__all__ = ["DigitraxConnection"]
