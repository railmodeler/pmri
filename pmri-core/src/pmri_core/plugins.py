"""Entry-point-based plugin discovery for connections and extensions.

JMRI's Java code uses Reflections to scan the classpath for ``Manager``
implementations and ``ConnectionConfig`` classes. In Python we do this
declaratively via ``importlib.metadata`` entry points:

    [project.entry-points."pmri.connection"]
    loconet  = "pmri_loconet:LocoNetConnection"
    nce      = "pmri_nce:NceConnection"
    internal = "pmri_core.hal.internal:InternalConnection"

Each entry point name is the connection prefix string the user sees in
panels and rosters. The loaded value must be a subclass of
:class:`pmri_core.hal.connection.SystemConnection`.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmri_core.hal.connection import SystemConnection

CONNECTION_GROUP = "pmri.connection"


def load_connections() -> dict[str, type[SystemConnection]]:
    """Return ``{prefix: connection_class}`` for every registered driver.

    Failures to load a single entry point are silently dropped -- a broken
    driver should never crash the core's discovery pass. Use
    :func:`load_connections_strict` if you want failures to surface.
    """
    result: dict[str, type[SystemConnection]] = {}
    for ep in entry_points(group=CONNECTION_GROUP):
        try:
            cls = ep.load()
        except Exception:  # noqa: BLE001 -- third-party plugin code
            continue
        result[ep.name] = cls
    return result


def load_connections_strict() -> dict[str, type[SystemConnection]]:
    """Like :func:`load_connections` but raises on the first failed entry."""
    return {ep.name: ep.load() for ep in entry_points(group=CONNECTION_GROUP)}
