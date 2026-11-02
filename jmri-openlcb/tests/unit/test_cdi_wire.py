"""CDI end-to-end tests over a real memory pipeline.

The peer runs a ``_FakeMemoryDevice`` (same shape as the memory
tests) with the CDI XML seeded into ``SPACE_CDI`` and a config
segment in ``SPACE_CONFIG``. The client fetches the CDI, parses
it, and does typed read/write against config fields -- hitting the
same wire path a real hardware node would.
"""
from __future__ import annotations

from jmri_openlcb import (
    DatagramEngine,
    EventId,
    MemoryClient,
    OlcbTrafficController,
    SPACE_CDI,
    SPACE_CONFIG,
    fetch_cdi,
    loopback_pair,
    read_field,
    write_field,
)
from jmri_openlcb.cdi import CdiBit, CdiEventId, CdiFloat, CdiInt, CdiString

from tests.unit.test_memory import _FakeMemoryDevice


CDI_XML = """\
<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>ACME</manufacturer>
    <model>Widget-3000</model>
    <hardwareVersion>rev A</hardwareVersion>
    <softwareVersion>1.2.3</softwareVersion>
  </identification>
  <segment origin="0" space="0xFE">
    <name>User Config</name>
    <int size="1">
      <name>Turnout count</name>
      <default>4</default>
    </int>
    <int size="2">
      <name>Startup delay ms</name>
    </int>
    <string size="16">
      <name>Node label</name>
    </string>
    <eventid>
      <name>Trigger event</name>
    </eventid>
    <float size="4">
      <name>Voltage</name>
    </float>
    <bit bit="3">
      <name>Enabled</name>
    </bit>
  </segment>
</cdi>
"""


async def _make_bus_with_device():
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
    return tc_a, tc_b, eng_a, eng_b, device, client


async def _teardown(*things):  # noqa: ANN002
    for thing in things:
        if isinstance(thing, MemoryClient):
            thing.close()
        elif isinstance(thing, _FakeMemoryDevice):
            thing.close()
    for thing in things:
        if isinstance(thing, DatagramEngine):
            await thing.detach()
    for thing in things:
        if isinstance(thing, OlcbTrafficController):
            await thing.aclose()


def _seed_cdi(device: _FakeMemoryDevice, xml: str) -> None:
    """Write the XML + null terminator into the fake device's
    CDI space at offset 0."""
    data = xml.encode("utf-8") + b"\x00"
    for i, byte in enumerate(data):
        device.store[(SPACE_CDI, i)] = byte


# ---- fetch_cdi ----------------------------------------------------------


async def test_fetch_cdi_retrieves_and_parses() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        assert cdi.identification.manufacturer == "ACME"
        assert cdi.identification.model == "Widget-3000"
        # Layout: int(1) + int(2) + string(16) + eventid(8) + float(4) + bit(1) = 32 bytes.
        assert cdi.segments[0].size == 32
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_fetch_cdi_stops_at_null_terminator() -> None:
    """Bytes after the null terminator are ignored so we don't
    parse trailing garbage the device may have left in memory."""
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        payload = CDI_XML.encode("utf-8") + b"\x00" + b"garbage after terminator"
        for i, byte in enumerate(payload):
            device.store[(SPACE_CDI, i)] = byte
        cdi = await fetch_cdi(client, tc_b.source_alias)
        assert cdi.identification.manufacturer == "ACME"
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_fetch_cdi_respects_max_bytes_ceiling() -> None:
    """A device that never null-terminates within *max_bytes* triggers
    a ValueError rather than reading forever."""
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        # Seed a huge non-terminated payload (larger than our cap).
        for i in range(2048):
            device.store[(SPACE_CDI, i)] = ord("A")
        import pytest  # noqa: PLC0415
        with pytest.raises(ValueError, match="without hitting a null"):
            await fetch_cdi(client, tc_b.source_alias, max_bytes=512)
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


# ---- read_field / write_field --------------------------------------------


async def test_int_field_round_trip() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        turnout_count = seg.children[0]
        assert isinstance(turnout_count, CdiInt)
        # Not-yet-written -> reads as 0 (backing store defaults to 0).
        assert await read_field(client, tc_b.source_alias, seg, turnout_count) == 0
        # Write 7 -> reads back 7.
        await write_field(client, tc_b.source_alias, seg, turnout_count, 7)
        assert await read_field(client, tc_b.source_alias, seg, turnout_count) == 7
        # The single byte lands at offset 0 in SPACE_CONFIG per the CDI.
        assert device.store[(SPACE_CONFIG, 0)] == 7
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_int_field_signed_bigendian() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        delay = seg.children[1]
        assert isinstance(delay, CdiInt) and delay.size == 2 and delay.offset == 1
        # Big-endian signed round trip through both directions.
        await write_field(client, tc_b.source_alias, seg, delay, -12345)
        assert await read_field(client, tc_b.source_alias, seg, delay) == -12345
        # Verify the byte layout on the wire.
        raw = bytes([device.store[(SPACE_CONFIG, 1)], device.store[(SPACE_CONFIG, 2)]])
        assert int.from_bytes(raw, "big", signed=True) == -12345
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_string_field_round_trip_pads_with_nulls() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        label = seg.children[2]
        assert isinstance(label, CdiString) and label.size == 16
        await write_field(client, tc_b.source_alias, seg, label, "hello")
        got = await read_field(client, tc_b.source_alias, seg, label)
        assert got == "hello"
        # Backing bytes: "hello" + 11 null padding.
        raw = bytes(device.store.get((SPACE_CONFIG, label.offset + i), 0) for i in range(16))
        assert raw == b"hello" + b"\x00" * 11
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_string_write_rejects_overlength() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        label = seg.children[2]
        import pytest  # noqa: PLC0415
        with pytest.raises(ValueError, match="too long"):
            await write_field(
                client, tc_b.source_alias, seg, label, "x" * 17,
            )
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_eventid_field_round_trip() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        ev = seg.children[3]
        assert isinstance(ev, CdiEventId)
        want = EventId.parse("05.01.01.01.4C.43.00.01")
        await write_field(client, tc_b.source_alias, seg, ev, want)
        got = await read_field(client, tc_b.source_alias, seg, ev)
        assert got == want
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_float_field_round_trip() -> None:
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        volts = seg.children[4]
        assert isinstance(volts, CdiFloat)
        await write_field(client, tc_b.source_alias, seg, volts, 12.5)
        got = await read_field(client, tc_b.source_alias, seg, volts)
        assert abs(got - 12.5) < 1e-6
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)


async def test_bit_field_read_modify_write_preserves_neighbours() -> None:
    """A bit-write must not clobber other bits in the same host byte.
    Seed the hosting byte with 0xFF, flip bit 3 off, verify the
    other 7 bits are unchanged."""
    tc_a, tc_b, eng_a, eng_b, device, client = await _make_bus_with_device()
    try:
        _seed_cdi(device, CDI_XML)
        cdi = await fetch_cdi(client, tc_b.source_alias)
        seg = cdi.segments[0]
        bit = seg.children[5]
        assert isinstance(bit, CdiBit) and bit.bit == 3
        # Seed the hosting byte fully set.
        device.store[(SPACE_CONFIG, bit.offset)] = 0xFF
        assert await read_field(client, tc_b.source_alias, seg, bit) is True
        # Flip bit 3 off. Bit mask = 0x08.
        await write_field(client, tc_b.source_alias, seg, bit, False)
        assert device.store[(SPACE_CONFIG, bit.offset)] == 0xFF & ~0x08
        assert await read_field(client, tc_b.source_alias, seg, bit) is False
        # Flip bit 3 back on. All other bits still set from the seed.
        await write_field(client, tc_b.source_alias, seg, bit, True)
        assert device.store[(SPACE_CONFIG, bit.offset)] == 0xFF
    finally:
        await _teardown(client, device, eng_a, eng_b, tc_a, tc_b)
