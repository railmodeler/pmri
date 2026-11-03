"""FDI parser + fetcher tests."""
from __future__ import annotations

import pytest

from pmri_openlcb.fdi import (
    Fdi,
    FdiFunction,
    FdiGroup,
    FdiSegment,
    SPACE_FDI,
    fetch_fdi,
    parse_fdi,
    parse_fdi_bytes,
)


CANONICAL = """\
<?xml version="1.0"?>
<fdi>
  <segment>
    <name>Loco Functions</name>
    <group>
      <name>Front cab</name>
      <function size="1" kind="binary">
        <name>Headlight</name>
        <number>0</number>
      </function>
      <function size="1" kind="binary">
        <name>Bell</name>
        <number>1</number>
      </function>
      <function size="1" kind="binary">
        <name>Horn</name>
        <number>2</number>
        <momentary/>
      </function>
    </group>
    <group>
      <name>Sound</name>
      <function size="1" kind="analog">
        <name>Volume</name>
        <number>7</number>
        <min>0</min>
        <max>127</max>
      </function>
    </group>
  </segment>
</fdi>
"""


# ---- happy path: full CANONICAL structure ------------------------------


def test_parse_canonical_produces_expected_tree() -> None:
    """Full XML -> 1 segment, 2 groups, 4 functions total. Types
    and momentary flag round-trip."""
    fdi = parse_fdi(CANONICAL)
    assert len(fdi.segments) == 1
    seg = fdi.segments[0]
    assert seg.name == "Loco Functions"
    assert len(seg.groups) == 2
    front_cab, sound = seg.groups
    assert front_cab.name == "Front cab"
    assert len(front_cab.functions) == 3
    headlight, bell, horn = front_cab.functions
    assert headlight.name == "Headlight" and headlight.number == 0
    assert bell.name == "Bell" and bell.number == 1
    assert bell.momentary is False
    assert horn.name == "Horn" and horn.number == 2
    assert horn.momentary is True   # <momentary/> present
    volume = sound.functions[0]
    assert volume.name == "Volume" and volume.kind == "analog"
    assert volume.min == 0 and volume.max == 127


def test_flatten_walks_every_function_in_declaration_order() -> None:
    fdi = parse_fdi(CANONICAL)
    numbers = [fn.number for fn in fdi.flatten()]
    assert numbers == [0, 1, 2, 7]


def test_by_number_returns_matching_function() -> None:
    fdi = parse_fdi(CANONICAL)
    horn = fdi.by_number(2)
    assert horn is not None and horn.name == "Horn"
    assert fdi.by_number(99) is None


# ---- structural leniency -----------------------------------------------


def test_parse_tolerates_missing_group_wrapper() -> None:
    """Some real-world FDIs skip the ``<group>`` layer and put
    ``<function>`` directly under ``<segment>``. Our parser
    synthesizes a group so the flat API stays consistent."""
    xml = """<fdi><segment>
        <function size="1" kind="binary"><name>Only</name><number>0</number></function>
    </segment></fdi>"""
    fdi = parse_fdi(xml)
    assert len(fdi.segments) == 1
    assert len(fdi.segments[0].groups) == 1
    assert fdi.segments[0].groups[0].functions[0].name == "Only"


def test_parse_tolerates_missing_segment_wrapper() -> None:
    """No ``<segment>`` at all -- top-level ``<group>``. Synthesize
    a segment so callers can rely on ``fdi.segments`` being
    non-empty."""
    xml = """<fdi>
        <group><name>All</name>
            <function size="1"><name>Fn</name><number>0</number></function>
        </group>
    </fdi>"""
    fdi = parse_fdi(xml)
    assert len(fdi.segments) == 1
    assert fdi.segments[0].groups[0].name == "All"


def test_parse_tolerates_bare_function_elements() -> None:
    """Extreme case: neither ``<segment>`` nor ``<group>``; just a
    flat list of ``<function>`` at the root. Still fine."""
    xml = """<fdi>
        <function size="1"><name>A</name><number>0</number></function>
        <function size="1"><name>B</name><number>1</number></function>
    </fdi>"""
    fdi = parse_fdi(xml)
    flat = fdi.flatten()
    assert [fn.name for fn in flat] == ["A", "B"]


def test_parse_defaults_missing_kind_and_size() -> None:
    xml = """<fdi><segment><group>
        <function><name>Untyped</name><number>3</number></function>
    </group></segment></fdi>"""
    fdi = parse_fdi(xml)
    fn = fdi.by_number(3)
    assert fn is not None
    assert fn.kind == "binary"
    assert fn.size == 1


def test_parse_coerces_unknown_kind_to_binary() -> None:
    """Vendor-specific ``kind`` values shouldn't crash us. Coerce
    to binary -- matches what a throttle would treat them as
    anyway."""
    xml = """<fdi><segment><group>
        <function size="1" kind="marklin-special">
            <name>Special</name><number>1</number>
        </function>
    </group></segment></fdi>"""
    fdi = parse_fdi(xml)
    fn = fdi.by_number(1)
    assert fn is not None and fn.kind == "binary"


# ---- error paths --------------------------------------------------------


def test_parse_rejects_non_fdi_root() -> None:
    with pytest.raises(ValueError, match="root must be <fdi>"):
        parse_fdi("<other/>")


def test_parse_rejects_malformed_xml() -> None:
    with pytest.raises(ValueError, match="parse error"):
        parse_fdi("<fdi><segment>")


def test_parse_from_bytes_strips_null_terminator() -> None:
    payload = CANONICAL.encode("utf-8") + b"\x00\x00\x00"
    fdi = parse_fdi_bytes(payload)
    assert len(fdi.segments) == 1


def test_parse_from_bytes_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_fdi_bytes(b"\x00\x00")


def test_unknown_top_level_tags_ignored() -> None:
    """Forward-compat: future FDI additions shouldn't break us."""
    xml = """<fdi>
        <future-thing>whatever</future-thing>
        <segment><group>
            <function><name>Fn</name><number>0</number></function>
        </group></segment>
    </fdi>"""
    fdi = parse_fdi(xml)
    assert len(fdi.flatten()) == 1


# ---- wire fetch (integration with fake memory) --------------------------


async def test_fetch_fdi_returns_parsed_tree() -> None:
    """End-to-end: seed a loopback peer's SPACE_FDI (0xFA) with the
    XML, fetch it through the memory client, verify the parsed tree.
    Reuses the memory-test fake device pattern."""
    import asyncio  # noqa: PLC0415

    from pmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController, loopback_pair,
    )
    from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415

    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=0x111)
    tc_b = OlcbTrafficController(b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    eng_a = DatagramEngine(tc_a)
    eng_b = DatagramEngine(tc_b)
    await eng_a.attach()
    await eng_b.attach()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        payload = CANONICAL.encode("utf-8") + b"\x00"
        for i, byte in enumerate(payload):
            device.store[(SPACE_FDI, i)] = byte
        fdi = await fetch_fdi(client, tc_b.source_alias)
        assert len(fdi.segments) == 1
        # Sanity -- same content as parse_fdi(CANONICAL).
        assert fdi.by_number(2).name == "Horn"
    finally:
        client.close()
        device.close()
        await eng_a.detach()
        await eng_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_fetch_fdi_respects_max_bytes() -> None:
    """A peer that never null-terminates -> refuse to keep reading."""
    from pmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController, loopback_pair,
    )
    from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415

    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=0x111)
    tc_b = OlcbTrafficController(b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    eng_a = DatagramEngine(tc_a)
    eng_b = DatagramEngine(tc_b)
    await eng_a.attach()
    await eng_b.attach()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        # Fill 2 KiB with no terminator.
        for i in range(2048):
            device.store[(SPACE_FDI, i)] = ord("A")
        with pytest.raises(ValueError, match="without hitting a null"):
            await fetch_fdi(client, tc_b.source_alias, max_bytes=512)
    finally:
        client.close()
        device.close()
        await eng_a.detach()
        await eng_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


# ---- exports sanity ----------------------------------------------------


def test_dataclass_defaults_are_conservative() -> None:
    """A default FdiFunction should be a safely-usable "unset"
    binary F0 -- matches what an entirely-blank <function/> would
    produce and gives callers a predictable starting shape."""
    fn = FdiFunction()
    assert fn.number == 0
    assert fn.kind == "binary"
    assert fn.size == 1
    assert fn.momentary is False
    assert fn.min is None and fn.max is None

    grp = FdiGroup()
    assert grp.functions == []
    assert grp.groups == []

    seg = FdiSegment()
    assert seg.groups == []

    fdi = Fdi()
    assert fdi.segments == []
    assert fdi.flatten() == []
    assert fdi.by_number(0) is None
