"""MRC Prodigy driver stub.

Registers the ``mrc`` entry point so :func:`jmri_server.app.select_connection`
can find it, but the open path is a no-op: the actual Prodigy
serial protocol and bean managers are not yet implemented. Picking
this connection in the PMRI launcher lets the web service boot (so
an MRC operator can at least see the UI); any turnout / sensor /
throttle traffic ends in the in-memory stub.

The package exists primarily to reserve the entry-point name and
let the launcher's connection pulldown surface MRC without the
server erroring on an unknown driver.
"""
from __future__ import annotations

from jmri_mrc.connection import MrcConnection

__all__ = ["MrcConnection"]
