"""Lenz XpressNet driver stub.

Registers the ``lenz`` entry point so :func:`pmri_server.app.select_connection`
can find it, but the open path is a no-op: the actual XpressNet
transport, protocol framing, and bean managers are not yet
implemented. Picking this connection in the PMRI launcher lets the
web service boot (so a Lenz operator can at least see the UI); any
turnout / sensor / throttle traffic ends in the in-memory stub.

The package exists primarily to reserve the entry-point name and
let the launcher's connection pulldown surface Lenz without the
server erroring on an unknown driver.
"""
from __future__ import annotations

from pmri_lenz.connection import LenzConnection

__all__ = ["LenzConnection"]
