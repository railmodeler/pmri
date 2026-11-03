"""Traction Search Protocol tests -- encode/decode + client + CS reply."""
from __future__ import annotations

import asyncio

import pytest
from pmri_core.events import Signal

from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_mti,
    extract_source_alias,
)
from pmri_openlcb.command_station import CommandStationNode
from pmri_openlcb.events import EventId
from pmri_openlcb.traction_search import (
    MTI_IDENTIFY_PRODUCERS,
    MTI_PRODUCER_IDENT_VALID,
    SEARCH_EVENT_PREFIX,
    SearchQuery,
    TractionSearchClient,
    _address_to_bcd,
    _bcd_to_address,
    decode_search_event,
    encode_search_event,
)


# ---- BCD helpers --------------------------------------------------------


def test_address_to_bcd_matches_spec_layout() -> None:
    """Regression pin for the BCD encoding scheme: digits are stored
    least-significant-first at bit positions 0, 4, 8, 12, 16, 20."""
    # Address 3 -> single low nibble.
    assert _address_to_bcd(3) == 0x000003
    # Address 12 -> two low digits: 1 in bit 4-7, 2 in bit 0-3.
    assert _address_to_bcd(12) == 0x000012
    # Address 3601 -> 3,6,0,1 in nibbles 3,2,1,0.
    assert _address_to_bcd(3601) == 0x003601
    # Full 6-digit fill.
    assert _address_to_bcd(999999) == 0x999999


def test_bcd_to_address_round_trips() -> None:
    for addr in (0, 3, 12, 127, 128, 3601, 10239, 999999):
        assert _bcd_to_address(_address_to_bcd(addr)) == addr


def test_address_to_bcd_rejects_over_six_digits() -> None:
    with pytest.raises(ValueError, match="6 BCD digits"):
        _address_to_bcd(1000000)
    with pytest.raises(ValueError, match="6 BCD digits"):
        _address_to_bcd(-1)


def test_bcd_to_address_rejects_invalid_nibbles() -> None:
    """A nibble > 9 is not valid BCD -- surface it rather than
    return garbage."""
    # 0xA in low nibble.
    with pytest.raises(ValueError, match="malformed BCD"):
        _bcd_to_address(0x00000A)


# ---- encode_search_event / decode_search_event -------------------------


def test_encode_search_event_carries_prefix_and_bcd_address() -> None:
    """Regression pin: prefix in top 4 bytes, BCD address in bytes
    4..6 (bit positions 8..31 of the 64-bit value), flag byte 7 = 0
    when no options set."""
    event = encode_search_event(3)
    assert event.value == SEARCH_EVENT_PREFIX | (0x000003 << 8)
    # Explicit byte layout.
    assert event.bytes == b"\x09\x00\x99\xFF\x00\x00\x03\x00"


def test_encode_long_address_sets_bit_1_in_flag_byte() -> None:
    event = encode_search_event(3, long_address=True)
    assert event.bytes[-1] == 0x02


def test_encode_short_address_sets_bit_0_in_flag_byte() -> None:
    event = encode_search_event(3, long_address=False)
    assert event.bytes[-1] == 0x01


def test_encode_no_address_type_leaves_flag_bits_clear() -> None:
    """``long_address=None`` = match either -- neither bit 0 nor bit 1
    is set, so a CS matches both short and long variants of the
    given DCC address."""
    event = encode_search_event(3, long_address=None)
    assert event.bytes[-1] & 0x03 == 0x00


def test_encode_force_allocate_sets_top_bit() -> None:
    event = encode_search_event(3, force_allocate=True)
    assert event.bytes[-1] & 0x80 == 0x80


def test_decode_round_trip_preserves_all_fields() -> None:
    for long_ in (None, True, False):
        for force in (False, True):
            event = encode_search_event(
                3601, long_address=long_, force_allocate=force,
            )
            got = decode_search_event(event)
            assert got is not None
            assert got.address == 3601
            assert got.long_address == long_
            assert got.force_allocate == force


def test_decode_returns_none_for_non_search_event() -> None:
    """Any event ID whose top 4 bytes don't match the prefix is not
    a search event. Distinguishes generic Identify Producers from
    train search."""
    # A regular event we might use elsewhere (Power On).
    assert decode_search_event(EventId(0x01_00_00_00_00_00_FF_FF)) is None
    # Prefix off by one bit.
    assert decode_search_event(EventId(0x08_00_99_FF_00_00_03_00)) is None


def test_decode_returns_none_on_malformed_bcd() -> None:
    """A search event with a nibble > 9 in the address block is
    malformed -- decode returns None rather than raising, so a
    handler can just skip it."""
    # Deliberately place 0xA in the low BCD nibble.
    malformed = EventId(SEARCH_EVENT_PREFIX | 0x000A00)
    assert decode_search_event(malformed) is None


# ---- SearchQuery.matches ------------------------------------------------


def test_matches_wildcard_long_matches_both_types() -> None:
    q = SearchQuery(address=3, long_address=None)
    assert q.matches(3, long_address=False)
    assert q.matches(3, long_address=True)
    assert not q.matches(4, long_address=False)


def test_matches_specific_long_type_constrains() -> None:
    q_short = SearchQuery(address=3, long_address=False)
    assert q_short.matches(3, long_address=False)
    assert not q_short.matches(3, long_address=True)

    q_long = SearchQuery(address=3, long_address=True)
    assert q_long.matches(3, long_address=True)
    assert not q_long.matches(3, long_address=False)


# ---- test doubles -------------------------------------------------------


class _FakeTc:
    def __init__(self, source_alias: int) -> None:
        self.source_alias = source_alias
        self.frames: Signal[OlcbFrame] = Signal()
        self._peer: _FakeTc | None = None
        self.sent: list[OlcbFrame] = []

    def link(self, peer: _FakeTc) -> None:
        self._peer = peer

    async def send_frame(self, frame: OlcbFrame) -> None:
        self.sent.append(frame)
        if self._peer is not None:
            await self._peer.frames.emit(frame)


def _pair(alias_a: int, alias_b: int) -> tuple[_FakeTc, _FakeTc]:
    a = _FakeTc(alias_a)
    b = _FakeTc(alias_b)
    a.link(b)
    b.link(a)
    return a, b


# ---- TractionSearchClient -- wire format ---------------------------------


async def test_search_sends_identify_producer_with_search_event() -> None:
    """The client's initial frame is an Identify Producer carrying
    the encoded search event as its 8-byte payload."""
    tc = _FakeTc(0x111)
    client = TractionSearchClient(tc)
    try:
        # Zero timeout -- we don't want to wait, we just want to
        # inspect the outbound frame.
        results = await client.search(3, timeout=0.0)
        assert results == []
        assert len(tc.sent) == 1
        f = tc.sent[0]
        assert extract_mti(f.header) == MTI_IDENTIFY_PRODUCERS
        assert extract_source_alias(f.header) == 0x111
        assert f.data == encode_search_event(3).bytes
    finally:
        client.close()


async def test_search_collects_producer_identified_valid_replies() -> None:
    """Replies arriving during the search window populate the
    returned list. Multiple matching locos -> multiple results."""
    a, b = _pair(0x111, 0x222)
    client = TractionSearchClient(a)
    try:
        search_task = asyncio.create_task(client.search(3, timeout=0.1))
        await asyncio.sleep(0)
        event = encode_search_event(3)
        # Two locos on the bus reply.
        for src_alias in (0x300, 0x400):
            await b.send_frame(OlcbFrame(
                header=_pack_header(
                    FT_MTI, MTI_PRODUCER_IDENT_VALID, src_alias,
                ),
                data=event.bytes,
            ))
        results = await search_task
        assert len(results) == 2
        aliases = {r.alias for r in results}
        assert aliases == {0x300, 0x400}
        # Sorted by alias.
        assert results[0].alias < results[1].alias
        # Event echoed back verbatim.
        for r in results:
            assert r.event == event
    finally:
        client.close()


async def test_search_ignores_replies_with_wrong_event_id() -> None:
    """A Producer Identified Valid for a different event (a normal
    non-search event that some other node happens to produce) must
    not populate the results list -- the futures dict keys on the
    event so cross-search contamination can't happen."""
    a, b = _pair(0x111, 0x222)
    client = TractionSearchClient(a)
    try:
        search_task = asyncio.create_task(client.search(3, timeout=0.05))
        await asyncio.sleep(0)
        # A stray Producer Identified for a totally different event.
        stray = EventId(0x01_00_00_00_00_00_FF_FF)
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x999),
            data=stray.bytes,
        ))
        results = await search_task
        assert results == []
    finally:
        client.close()


async def test_search_returns_empty_list_on_no_matches() -> None:
    """A search that finds nothing is not an error -- the window
    just expires with an empty result."""
    a, _ = _pair(0x111, 0x222)
    client = TractionSearchClient(a)
    try:
        results = await client.search(3, timeout=0.02)
        assert results == []
    finally:
        client.close()


async def test_concurrent_search_for_same_event_rejected() -> None:
    """Two searches for the same address at the same time would
    share a result buffer -- rejected explicitly."""
    tc = _FakeTc(0x111)
    client = TractionSearchClient(tc)
    try:
        first = asyncio.create_task(client.search(3, timeout=0.1))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="already in flight"):
            await client.search(3, timeout=0.05)
        await first
    finally:
        client.close()


async def test_parallel_search_for_different_addresses_work() -> None:
    """Different event IDs -> separate buffers -> parallel searches
    coexist. Ensures the client's key discipline is per-event, not
    per-client."""
    a, b = _pair(0x111, 0x222)
    client = TractionSearchClient(a)
    try:
        s3 = asyncio.create_task(client.search(3, timeout=0.1))
        s4 = asyncio.create_task(client.search(4, timeout=0.1))
        await asyncio.sleep(0)
        # Reply for address 3 only.
        ev3 = encode_search_event(3)
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x300),
            data=ev3.bytes,
        ))
        r3, r4 = await asyncio.gather(s3, s4)
        assert len(r3) == 1
        assert r3[0].alias == 0x300
        assert r4 == []
    finally:
        client.close()


# ---- End-to-end: TractionSearchClient <-> CommandStationNode --------------


class _FakeThrottleManager:
    """Minimal ThrottleManager stand-in -- the CS doesn't need to
    acquire throttles for search testing, so no _construct override."""

    def __init__(self) -> None:
        from pmri_core.beans.throttle_manager import ThrottleManager
        self._inner = ThrottleManager("X")

    async def acquire(self, *args, **kwargs):
        return await self._inner.acquire(*args, **kwargs)

    async def release(self, *args, **kwargs):
        return await self._inner.release(*args, **kwargs)


async def test_cs_replies_from_every_matching_hosted_loco() -> None:
    """A CS with locos 3 and 4 hosted, probed for address 3, emits
    exactly one Producer Identified Valid -- the reply's source alias
    is loco 3's alias."""
    from pmri_openlcb.alias import NodeId
    a, b = _pair(0x111, 0x222)
    cs = CommandStationNode(
        a, _FakeThrottleManager(),
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        loco_3 = await cs.add_loco(3)
        await cs.add_loco(4)
        a.sent.clear()
        # Now the client on the other side searches for address 3.
        client = TractionSearchClient(b)
        try:
            results = await client.search(3, timeout=0.1)
            assert len(results) == 1
            assert results[0].alias == loco_3.alias
        finally:
            client.close()
    finally:
        cs.close()


async def test_cs_reply_respects_long_address_type_constraint() -> None:
    """Searching with ``long_address=True`` finds the long-address 3
    but not the short-address 3 hosted by the same CS."""
    from pmri_openlcb.alias import NodeId
    a, b = _pair(0x111, 0x222)
    cs = CommandStationNode(
        a, _FakeThrottleManager(),
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        short = await cs.add_loco(3, long_address=False)
        long_ = await cs.add_loco(3, long_address=True)
        client = TractionSearchClient(b)
        try:
            r_long = await client.search(3, long_address=True, timeout=0.1)
            assert len(r_long) == 1
            assert r_long[0].alias == long_.alias
            r_short = await client.search(3, long_address=False, timeout=0.1)
            assert len(r_short) == 1
            assert r_short[0].alias == short.alias
            # Wildcard matches both.
            r_any = await client.search(3, long_address=None, timeout=0.1)
            assert {r.alias for r in r_any} == {short.alias, long_.alias}
        finally:
            client.close()
    finally:
        cs.close()


async def test_cs_ignores_identify_producer_for_non_search_event() -> None:
    """A regular Identify Producer targeting some arbitrary event
    (not a search event) must not draw replies from hosted locos --
    that's someone else's event and the CS has no business in it."""
    from pmri_openlcb.alias import NodeId
    a, b = _pair(0x111, 0x222)
    cs = CommandStationNode(
        a, _FakeThrottleManager(),
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        await cs.add_loco(3)
        a.sent.clear()
        # Non-search event ID.
        arbitrary = EventId(0x01_00_00_00_00_00_FF_FF)
        await b.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_IDENTIFY_PRODUCERS, 0x222,
            ),
            data=arbitrary.bytes,
        ))
        await asyncio.sleep(0)
        # No Producer Identified Valid emitted.
        replies = [
            f for f in a.sent
            if extract_mti(f.header) == MTI_PRODUCER_IDENT_VALID
        ]
        assert replies == []
    finally:
        cs.close()


async def test_cs_search_for_unknown_address_stays_silent() -> None:
    """Search for an address the CS doesn't host -> no reply. The
    client's timeout expires with an empty result set."""
    from pmri_openlcb.alias import NodeId
    a, b = _pair(0x111, 0x222)
    cs = CommandStationNode(
        a, _FakeThrottleManager(),
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        await cs.add_loco(3)
        client = TractionSearchClient(b)
        try:
            results = await client.search(99, timeout=0.05)
            assert results == []
        finally:
            client.close()
    finally:
        cs.close()


# ---- connection wiring --------------------------------------------------


async def test_connection_exposes_traction_search_client() -> None:
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    try:
        assert conn.traction_search is not None
    finally:
        await conn.close()
