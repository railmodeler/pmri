from __future__ import annotations

import pytest

from jmri_openlcb.events import (
    WELL_KNOWN_EVENTS,
    EventId,
    well_known_name,
)


def test_parse_dotted_hex() -> None:
    e = EventId.parse("05.01.01.01.4C.43.00.01")
    assert e.value == 0x050101014C430001
    assert str(e) == "05.01.01.01.4C.43.00.01"


def test_parse_plain_hex() -> None:
    e = EventId.parse("050101014C430001")
    assert e.value == 0x050101014C430001


def test_parse_with_spaces() -> None:
    e = EventId.parse("05010101 4C430001")
    assert e.value == 0x050101014C430001


def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError):
        EventId.parse("   ")
    with pytest.raises(ValueError):
        EventId.parse("")


def test_parse_invalid_hex_raises() -> None:
    with pytest.raises(ValueError, match="invalid"):
        EventId.parse("not-hex")


def test_constructor_validates_range() -> None:
    EventId(0)
    EventId((1 << 64) - 1)
    with pytest.raises(ValueError):
        EventId(-1)
    with pytest.raises(ValueError):
        EventId(1 << 64)


def test_bytes_is_8_bytes_big_endian() -> None:
    e = EventId(0x05010101_4C430001)
    assert e.bytes == bytes.fromhex("050101014C430001")
    assert len(e.bytes) == 8


def test_well_known_name_matches_reserved_events() -> None:
    """Spot-check the reserved events from S-9.7.3 -- the exact
    names matter for cross-referencing docs."""
    assert well_known_name(
        EventId.parse("01.00.00.00.00.00.FF.FE"),
    ) == "Emergency Off"
    assert well_known_name(
        EventId.parse("01.00.00.00.00.00.FF.FD"),
    ) == "Emergency Stop of All Trains"


def test_well_known_name_returns_none_for_unnamed_event() -> None:
    """A project-space event ID isn't well-known -> None (the caller
    can then render hex only)."""
    ev = EventId.parse("05.01.01.01.4C.43.00.01")
    assert well_known_name(ev) is None


def test_well_known_events_catalog_is_nonempty() -> None:
    """Catalog exists and has at least the emergency events --
    guards against accidentally emptying the dict during a refactor."""
    assert EventId.parse("01.00.00.00.00.00.FF.FE") in WELL_KNOWN_EVENTS
    assert len(WELL_KNOWN_EVENTS) >= 4
