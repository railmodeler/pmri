"""``EventId`` -- the 64-bit OpenLCB event identifier.

OpenLCB events are 64-bit values uniquely identifying *something happened*
on the network. A sensor turning on produces one event ID; the same
sensor turning off produces a different event ID. Beans that should
react bind specific event IDs to specific state transitions.

The canonical text form is dotted hex, big-endian, e.g.
``"05.01.01.01.4C.43.00.01"`` -- eight bytes joined by dots. We parse
both that form and a plain hex string ``"05010101 4C430001"`` (with or
without spaces) since both appear in JMRI configs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventId:
    """64-bit OpenLCB event identifier."""

    value: int  # 0..2**64-1

    def __post_init__(self) -> None:
        if not 0 <= self.value < (1 << 64):
            raise ValueError(f"EventId must be 0..2**64-1, got {self.value}")

    @classmethod
    def parse(cls, text: str) -> EventId:
        """Parse the dotted-hex or plain-hex text form into an :class:`EventId`."""
        cleaned = text.replace(".", "").replace(" ", "").strip()
        if not cleaned:
            raise ValueError(f"empty EventId text {text!r}")
        try:
            return cls(int(cleaned, 16))
        except ValueError as e:
            raise ValueError(f"invalid EventId text {text!r}: {e}") from None

    @property
    def bytes(self) -> bytes:
        return self.value.to_bytes(8, "big")

    def __str__(self) -> str:
        b = self.bytes
        return ".".join(f"{x:02X}" for x in b)


@dataclass(frozen=True, slots=True)
class EventReport:
    """Produced/Consumer Event Report -- the workhorse message.

    Sent by producers to announce "this event happened"; observed by
    consumers to drive their state. The 64-bit ``event`` is enough; the
    16-bit source ``alias`` identifies *which* node produced it (useful
    for diagnostics; we don't validate aliases here).
    """

    event: EventId
    source_alias: int  # 12-bit node alias, observed in the CAN header


# ---- well-known events (S-9.7.3 sec.5, "Well-Known Event IDs") --------------
#
# The 01.00.00.00.00.00.__.__ prefix is reserved by the standard for
# bus-wide reserved events. Only the entries below are actually named
# by the current spec; a UI that shows an event summary uses the
# lookup table so an operator sees "Emergency Off" instead of hex.
# Additions are cheap; keep names verbatim from the spec so cross-
# referencing docs stays easy.

EVENT_DUPLICATE_NODE_ID = EventId.parse("01.01.00.00.00.00.03.03")
"""Reserved event ID a node emits when it detects another node
claiming the same 48-bit NodeID (S-9.7.3 sec.5). See
:class:`~jmri_openlcb.alias.DuplicateNodeIdWatcher`."""


# CROSS-CHECK BEFORE BETA: these two values come from JMRI Java's
# ``org.openlcb.OpenLcb`` constants (``INITIALIZATION_COMPLETE`` and
# ``INITIALIZATION_COMPLETE_SIMPLE``) which cite S-9.7.3. Verify
# against the current OpenLCB standard before shipping -- the well-
# known event ID table has evolved across spec revisions.
EVENT_INIT_COMPLETE = EventId.parse("01.01.00.00.00.00.02.01")
"""Node Initialization Complete -- emitted by a full-protocol node
after alias claim and roster warm-up finish (S-9.7.3 sec.5)."""

EVENT_INIT_COMPLETE_SIMPLE = EventId.parse("01.01.00.00.00.00.02.02")
"""Node Initialization Complete (Simple Protocol Subset) -- the
variant emitted by nodes advertising only the simple protocol
flag set (S-9.7.3 sec.5)."""

EVENT_FIRMWARE_CORRUPTED = EventId.parse("01.01.00.00.00.00.06.01")
"""Emitted by a target node when its production firmware is broken
and it booted into upgrade mode automatically (S-9.7.4.3 sec.6.3)."""

EVENT_FIRMWARE_UPGRADE_REQUESTED = EventId.parse("01.01.00.00.00.00.06.02")
"""Emitted when the operator flipped a hardware switch requesting
firmware upgrade mode at power-up (S-9.7.4.3 sec.6.3)."""


WELL_KNOWN_EVENTS: dict[EventId, str] = {
    EventId.parse("01.00.00.00.00.00.FF.FE"): "Emergency Off",
    EventId.parse("01.00.00.00.00.00.FF.FF"): "Clear Emergency Off",
    EventId.parse("01.00.00.00.00.00.FF.FD"): "Emergency Stop of All Trains",
    EventId.parse("01.00.00.00.00.00.FF.FC"): (
        "Clear Emergency Stop of All Trains"
    ),
    EventId.parse("01.00.00.00.00.00.FF.F8"): "New Log Entry",
    # 01.00.00.00.00.00.FE.00..FE.FF is a range reserved for train-
    # search event IDs; we don't enumerate every code but flag the
    # anchor so a UI can tell it's search-related.
    EventId.parse("01.00.00.00.00.00.FE.00"): "Train Search (anchor)",
    EVENT_DUPLICATE_NODE_ID: "Duplicate Node ID Detected",
    EVENT_INIT_COMPLETE: "Node Initialization Complete",
    EVENT_INIT_COMPLETE_SIMPLE: (
        "Node Initialization Complete (Simple)"
    ),
    EVENT_FIRMWARE_CORRUPTED: "Firmware Corrupted",
    EVENT_FIRMWARE_UPGRADE_REQUESTED: (
        "Firmware Upgrade Requested by Hardware Switch"
    ),
}


def well_known_name(event: EventId) -> str | None:
    """Return the S-9.7.3 name for *event*, or ``None`` if it's not
    a well-known event. Used by the bus-monitor summary and any UI
    that wants to render human-friendly labels next to event IDs."""
    return WELL_KNOWN_EVENTS.get(event)
