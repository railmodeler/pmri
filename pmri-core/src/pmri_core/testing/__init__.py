"""Test-time utilities shared across driver packages.

Kept here rather than under each driver's ``tests/`` because these
formats -- recorded-bus replay files being the current example -- are
one shared contract across drivers. A divergent per-driver shape
would defeat the point of any protocol-agnostic capture tooling
that reads them.

The modules here have no runtime hardware dependencies and are safe
to import from production code (though there's no reason to).
"""
from pmri_core.testing.bus_replay import (
    BusFrame,
    Direction,
    iter_direction,
    read_capture,
    write_capture,
)

__all__ = [
    "BusFrame",
    "Direction",
    "iter_direction",
    "read_capture",
    "write_capture",
]
