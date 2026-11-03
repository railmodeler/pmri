"""Hardware Abstraction Layer -- connection ABC and in-memory loopback.

Concrete drivers (LocoNet, NCE, DCC-EX, etc.) live in their own packages and
subclass :class:`pmri_core.hal.connection.SystemConnection`. They register
themselves via the ``pmri.connection`` entry point so :func:`load_connections`
in :mod:`pmri_core.plugins` can discover them.
"""
from pmri_core.hal.connection import ConnectionSettings, SystemConnection
from pmri_core.hal.internal import InternalConnection
from pmri_core.hal.serial_resolve import resolve_serial_port_name

__all__ = [
    "ConnectionSettings",
    "InternalConnection",
    "SystemConnection",
    "resolve_serial_port_name",
]
