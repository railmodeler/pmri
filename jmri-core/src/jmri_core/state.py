"""Shared state enums matching JMRI's Java values.

Each enum mirrors the integer constants used in JMRI's wire formats and XML
files. **Do not renumber these** -- the values are part of the persisted
contract that golden XML files in stage-0/contracts/xml/golden/ depend on.

Per-bean state enums also live in this module so persistence code can import
a single namespace without circular imports through ``beans``.
"""
from __future__ import annotations

from enum import IntEnum, IntFlag


class TurnoutState(IntEnum):
    """Matches ``jmri.Turnout.UNKNOWN/CLOSED/THROWN/INCONSISTENT`` Java constants."""

    UNKNOWN      = 1
    CLOSED       = 2
    THROWN       = 4
    INCONSISTENT = 8


class TurnoutFeedback(IntFlag):
    """Matches ``jmri.Turnout`` feedback-mode flags."""

    DIRECT     = 1
    ONESENSOR  = 2
    TWOSENSOR  = 4
    EXACT      = 8
    SIGNAL     = 16
    MONITORING = 32
    DELAYED    = 64
    LN_ALM     = 128
    PUSHBUTTON = 256


class SensorState(IntEnum):
    """Matches ``jmri.Sensor.UNKNOWN/ACTIVE/INACTIVE/INCONSISTENT``."""

    UNKNOWN      = 1
    ACTIVE       = 2
    INACTIVE     = 4
    INCONSISTENT = 8


class SignalAppearance(IntEnum):
    """Matches ``jmri.SignalHead`` appearance constants -- the 9 standard aspects.

    Custom signals may add manufacturer-specific appearances via the
    SignalMast layer; SignalHead is restricted to this set.
    """

    DARK          = 0
    RED           = 1
    YELLOW        = 2
    GREEN         = 4
    LUNAR         = 8
    FLASHRED      = 16
    FLASHYELLOW   = 32
    FLASHGREEN    = 64
    FLASHLUNAR    = 128


class BlockState(IntEnum):
    """Matches ``jmri.Block.UNDETECTED/UNOCCUPIED/OCCUPIED/UNKNOWN``."""

    UNKNOWN    = 1
    OCCUPIED   = 2
    UNOCCUPIED = 4
    UNDETECTED = 256


class LightState(IntEnum):
    """Matches ``jmri.Light.OFF/ON/INTERMEDIATE``."""

    UNKNOWN      = 1
    OFF          = 2
    ON           = 4
    INTERMEDIATE = 8


class PowerState(IntEnum):
    """Matches ``jmri.PowerManager.UNKNOWN/ON/OFF/IDLE``."""

    UNKNOWN = 0
    ON      = 2
    OFF     = 4
    IDLE    = 8


class RouteState(IntEnum):
    """Internal route state -- JMRI has no equivalent integer enum, this maps
    to behavior of ``jmri.Route`` (set / unlocked / locked)."""

    UNLOCKED = 0
    LOCKED   = 1
    UNKNOWN  = 2


class ThrottleDirection(IntEnum):
    """Locomotive direction. Matches NMRA convention."""

    REVERSE = 0
    FORWARD = 1
