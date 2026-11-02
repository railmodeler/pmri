"""Wire-format state translation.

JMRI's JSON API encodes ``UNKNOWN`` as ``0`` on the wire, while the
internal ``NamedBean.UNKNOWN = 1`` matches JMRI's Java integer constants.
This module isolates the translation so service code can deal in the wire
form unambiguously.

JSON wire format (per the Stage 0 schemas):
  0 = UNKNOWN
  2 = ACTIVE   / CLOSED  / ON
  4 = INACTIVE / THROWN  / OFF
  8 = INCONSISTENT / INTERMEDIATE
"""
from __future__ import annotations

from jmri_core.state import (
    BlockState,
    LightState,
    PowerState,
    SensorState,
    TurnoutState,
)

WIRE_UNKNOWN = 0


def to_wire(internal: int) -> int:
    """Translate an internal IntEnum value to its JMRI JSON wire form."""
    return WIRE_UNKNOWN if internal == 1 else internal


def turnout_from_wire(wire: int) -> TurnoutState:
    """Translate a wire-format turnout state back to the internal enum."""
    if wire == WIRE_UNKNOWN:
        return TurnoutState.UNKNOWN
    return TurnoutState(wire)


def sensor_from_wire(wire: int) -> SensorState:
    if wire == WIRE_UNKNOWN:
        return SensorState.UNKNOWN
    return SensorState(wire)


def light_from_wire(wire: int) -> LightState:
    if wire == WIRE_UNKNOWN:
        return LightState.UNKNOWN
    return LightState(wire)


def power_from_wire(wire: int) -> PowerState:
    if wire == WIRE_UNKNOWN:
        return PowerState.UNKNOWN
    return PowerState(wire)


def block_from_wire(wire: int) -> BlockState:
    if wire == WIRE_UNKNOWN:
        return BlockState.UNKNOWN
    return BlockState(wire)
