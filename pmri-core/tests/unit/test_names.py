from __future__ import annotations

import pytest

from pmri_core.names import SystemName


def test_parse_simple_loconet_turnout() -> None:
    n = SystemName.parse("LT12")
    assert n.prefix == "L"
    assert n.type_letter == "T"
    assert n.address == "12"
    assert str(n) == "LT12"


def test_parse_multi_char_prefix() -> None:
    n = SystemName.parse("L2T12", expected_prefix="L2")
    assert n.prefix == "L2"
    assert n.type_letter == "T"
    assert n.address == "12"


def test_parse_complex_address_with_colons() -> None:
    """OpenLCB sensor names use colon-separated hierarchical addressing."""
    n = SystemName.parse("MS1:5:3", expected_prefix="M")
    assert n.prefix == "M"
    assert n.type_letter == "S"
    assert n.address == "1:5:3"


def test_parse_invalid_format_raises() -> None:
    with pytest.raises(ValueError):
        SystemName.parse("123")  # no type letter


def test_parse_wrong_expected_prefix_raises() -> None:
    with pytest.raises(ValueError, match="does not start with expected prefix"):
        SystemName.parse("LT12", expected_prefix="N")


def test_make_round_trips() -> None:
    s = SystemName.make("L", "T", 12)
    assert s == "LT12"
    parsed = SystemName.parse(s, expected_prefix="L")
    assert parsed.address == "12"


def test_make_rejects_bad_type_letter() -> None:
    with pytest.raises(ValueError):
        SystemName.make("L", "ab", 12)
    with pytest.raises(ValueError):
        SystemName.make("L", "t", 12)
    with pytest.raises(ValueError):
        SystemName.make("L", "1", 12)
