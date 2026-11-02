"""Tests for the Producer/Consumer event identification protocol."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.events import Signal

from jmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_mti,
    extract_source_alias,
)
from jmri_openlcb.event_ident import (
    MTI_CONSUMER_IDENT_INVALID,
    MTI_CONSUMER_IDENT_UNKNOWN,
    MTI_CONSUMER_IDENT_VALID,
    MTI_CONSUMER_RANGE_IDENTIFIED,
    MTI_IDENTIFY_CONSUMERS,
    MTI_IDENTIFY_EVENTS_ADDRESSED,
    MTI_IDENTIFY_EVENTS_GLOBAL,
    MTI_IDENTIFY_PRODUCERS,
    MTI_PRODUCER_IDENT_INVALID,
    MTI_PRODUCER_IDENT_UNKNOWN,
    MTI_PRODUCER_IDENT_VALID,
    MTI_PRODUCER_RANGE_IDENTIFIED,
    EventRoster,
    EventState,
    ProducerConsumerService,
    decode_event_range,
    encode_event_range,
    summarize_node,
)
from jmri_openlcb.events import EventId


EV_A = EventId.parse("05.01.01.01.4C.43.00.01")
EV_B = EventId.parse("05.01.01.01.4C.43.00.02")
EV_C = EventId.parse("05.01.01.01.4C.43.00.03")


# ---- test doubles --------------------------------------------------------


class _FakeTc:
    """Two-sided fake -- same shape as the SNIP test's `_FakeTc`.
    Frames written on side A appear on B's inbound signal."""

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


def _mk_probe(mti: int, source: int, event: EventId | None = None,
              dest: int | None = None) -> OlcbFrame:
    """Build one probe frame -- Identify Producers/Consumers carry an
    8-byte event; Identify Events Addressed carries a 2-byte dest
    alias; Identify Events Global carries nothing."""
    if event is not None:
        data = event.bytes
    elif dest is not None:
        data = dest.to_bytes(2, "big")
    else:
        data = b""
    return OlcbFrame(
        header=_pack_header(FT_MTI, mti, source),
        data=data,
    )


# ---- ProducerConsumerService --------------------------------------------


async def test_register_stores_events_and_defaults_to_unknown_state() -> None:
    tc = _FakeTc(0x111)
    svc = ProducerConsumerService(tc)
    try:
        svc.register_produced(EV_A)
        svc.register_consumed(EV_B)
        assert svc.produced == {EV_A: EventState.UNKNOWN}
        assert svc.consumed == {EV_B: EventState.UNKNOWN}
    finally:
        svc.close()


async def test_identify_all_broadcasts_producer_then_consumer() -> None:
    """``identify_all`` should emit a Producer Identified for every
    produced event followed by a Consumer Identified for every consumed
    event -- matches JMRI Java's packet order."""
    tc = _FakeTc(0x111)
    svc = ProducerConsumerService(tc)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        svc.register_produced(EV_B, EventState.UNKNOWN)
        svc.register_consumed(EV_C, EventState.INVALID)
        await svc.identify_all()

        assert len(tc.sent) == 3
        # First two: producer valid + producer unknown, matching order.
        assert extract_mti(tc.sent[0].header) == MTI_PRODUCER_IDENT_VALID
        assert tc.sent[0].data == EV_A.bytes
        assert extract_mti(tc.sent[1].header) == MTI_PRODUCER_IDENT_UNKNOWN
        assert tc.sent[1].data == EV_B.bytes
        # Third: consumer invalid.
        assert extract_mti(tc.sent[2].header) == MTI_CONSUMER_IDENT_INVALID
        assert tc.sent[2].data == EV_C.bytes
        # Every sent frame has our source alias.
        for f in tc.sent:
            assert extract_source_alias(f.header) == 0x111
    finally:
        svc.close()


async def test_identify_events_global_triggers_full_broadcast() -> None:
    """A peer's global Identify Events request should provoke every
    P/C Identified frame we know."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        svc.register_consumed(EV_B, EventState.UNKNOWN)
        # Peer B fires the global Identify Events.
        await b.send_frame(_mk_probe(MTI_IDENTIFY_EVENTS_GLOBAL, 0x222))
        await asyncio.sleep(0)   # let the service's frame handler run
        # A should have sent both replies.
        mtis = [extract_mti(f.header) for f in a.sent]
        assert MTI_PRODUCER_IDENT_VALID in mtis
        assert MTI_CONSUMER_IDENT_UNKNOWN in mtis
    finally:
        svc.close()


async def test_identify_events_addressed_only_answers_when_targeted() -> None:
    """Addressed Identify Events must only provoke replies from the
    node whose alias is in the 2-byte prefix -- otherwise every node
    on the bus would spam every addressed probe."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        # Targeted at 0x999 (someone else) -- A should NOT answer.
        await b.send_frame(_mk_probe(
            MTI_IDENTIFY_EVENTS_ADDRESSED, 0x222, dest=0x999,
        ))
        await asyncio.sleep(0)
        assert a.sent == []
        # Targeted at 0x111 (us) -- A answers.
        await b.send_frame(_mk_probe(
            MTI_IDENTIFY_EVENTS_ADDRESSED, 0x222, dest=0x111,
        ))
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        assert extract_mti(a.sent[0].header) == MTI_PRODUCER_IDENT_VALID
    finally:
        svc.close()


async def test_identify_producers_answers_only_for_matching_event() -> None:
    """A targeted producer probe with event X only draws a reply
    from a node that produces X -- a produced-Y or consumed-X node
    stays silent."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        # Probe for EV_B (which we don't produce) -- no reply.
        await b.send_frame(_mk_probe(MTI_IDENTIFY_PRODUCERS, 0x222, event=EV_B))
        await asyncio.sleep(0)
        assert a.sent == []
        # Probe for EV_A -- reply comes back.
        await b.send_frame(_mk_probe(MTI_IDENTIFY_PRODUCERS, 0x222, event=EV_A))
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        assert extract_mti(a.sent[0].header) == MTI_PRODUCER_IDENT_VALID
        assert a.sent[0].data == EV_A.bytes
    finally:
        svc.close()


async def test_identify_consumers_answers_only_for_matching_event() -> None:
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_consumed(EV_A, EventState.VALID)
        # Producing a different event doesn't count -- must be consumed.
        svc.register_produced(EV_B, EventState.VALID)
        await b.send_frame(_mk_probe(MTI_IDENTIFY_CONSUMERS, 0x222, event=EV_A))
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        assert extract_mti(a.sent[0].header) == MTI_CONSUMER_IDENT_VALID
    finally:
        svc.close()


async def test_set_produced_state_changes_next_reply() -> None:
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        svc.set_produced_state(EV_A, EventState.INVALID)
        await b.send_frame(_mk_probe(MTI_IDENTIFY_PRODUCERS, 0x222, event=EV_A))
        await asyncio.sleep(0)
        assert extract_mti(a.sent[0].header) == MTI_PRODUCER_IDENT_INVALID
    finally:
        svc.close()


async def test_unregister_stops_replies() -> None:
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        svc.unregister_produced(EV_A)
        await b.send_frame(_mk_probe(MTI_IDENTIFY_PRODUCERS, 0x222, event=EV_A))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


async def test_service_ignores_non_mti_frames() -> None:
    """CAN control frames (RID/AMD/etc) reach the frame signal but
    must not confuse the service."""
    from jmri_openlcb.codec import make_amd_header  # noqa: PLC0415
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        await b.send_frame(OlcbFrame(
            header=make_amd_header(0x222), data=b"\x02\x01\x12\xFE\xDC\xBA",
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


# ---- EventRoster --------------------------------------------------------


async def test_roster_records_producer_identified_replies() -> None:
    """A Producer Identified from a peer populates producers_of()."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        # B advertises production of EV_A.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x222),
            data=EV_A.bytes,
        ))
        await asyncio.sleep(0)
        assert roster.producers_of(EV_A) == {0x222}
        assert roster.produced_by(0x222) == {EV_A: EventState.VALID}
        # No consumer info for EV_A -- that's separate.
        assert roster.consumers_of(EV_A) == set()
    finally:
        roster.close()


async def test_roster_records_consumer_identified_replies() -> None:
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_CONSUMER_IDENT_UNKNOWN, 0x222),
            data=EV_A.bytes,
        ))
        await asyncio.sleep(0)
        assert roster.consumers_of(EV_A) == {0x222}
        assert roster.consumed_by(0x222) == {EV_A: EventState.UNKNOWN}
    finally:
        roster.close()


async def test_roster_aggregates_across_peers() -> None:
    """Two peers both producing the same event both land in
    producers_of()."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x222),
            data=EV_A.bytes,
        ))
        # Simulate a third peer 0x333 also producing EV_A.
        await a.frames.emit(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x333),
            data=EV_A.bytes,
        ))
        await asyncio.sleep(0)
        assert roster.producers_of(EV_A) == {0x222, 0x333}
    finally:
        roster.close()


async def test_roster_updates_state_on_re_advertisement() -> None:
    """When a producer re-broadcasts with a different state, the
    roster should overwrite the previous state -- not keep both."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x222),
            data=EV_A.bytes,
        ))
        await asyncio.sleep(0)
        assert roster.produced_by(0x222)[EV_A] == EventState.VALID
        # State flips to invalid.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_INVALID, 0x222),
            data=EV_A.bytes,
        ))
        await asyncio.sleep(0)
        assert roster.produced_by(0x222)[EV_A] == EventState.INVALID
    finally:
        roster.close()


async def test_roster_discover_broadcasts_global_identify() -> None:
    tc = _FakeTc(0x111)
    roster = EventRoster(tc)
    try:
        await roster.discover()
        assert len(tc.sent) == 1
        assert extract_mti(tc.sent[0].header) == MTI_IDENTIFY_EVENTS_GLOBAL
        assert tc.sent[0].data == b""
    finally:
        roster.close()


async def test_roster_identify_producers_of_sends_targeted_probe() -> None:
    tc = _FakeTc(0x111)
    roster = EventRoster(tc)
    try:
        await roster.identify_producers_of(EV_A)
        assert len(tc.sent) == 1
        assert extract_mti(tc.sent[0].header) == MTI_IDENTIFY_PRODUCERS
        assert tc.sent[0].data == EV_A.bytes
    finally:
        roster.close()


async def test_roster_forget_drops_the_alias() -> None:
    """An AMR from a peer should trigger ``forget`` so the alias
    doesn't linger."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x222),
            data=EV_A.bytes,
        ))
        await asyncio.sleep(0)
        assert 0x222 in roster.known_aliases()
        roster.forget(0x222)
        assert 0x222 not in roster.known_aliases()
        assert roster.producers_of(EV_A) == set()
    finally:
        roster.close()


async def test_summarize_node_bundles_both_directions() -> None:
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        # B produces EV_A and consumes EV_B.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_PRODUCER_IDENT_VALID, 0x222),
            data=EV_A.bytes,
        ))
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_CONSUMER_IDENT_UNKNOWN, 0x222),
            data=EV_B.bytes,
        ))
        await asyncio.sleep(0)
        summary = summarize_node(roster, 0x222)
        assert summary.alias == 0x222
        assert summary.produced == {EV_A: EventState.VALID}
        assert summary.consumed == {EV_B: EventState.UNKNOWN}
    finally:
        roster.close()


# ---- end-to-end: service + roster interoperate ---------------------------


async def test_service_and_roster_interoperate_via_identify_events() -> None:
    """End-to-end. Node A runs a service registering some events;
    node B runs a roster and calls discover(); the roster ends up
    knowing what A advertises."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    roster = EventRoster(b)
    try:
        svc.register_produced(EV_A, EventState.VALID)
        svc.register_produced(EV_B, EventState.UNKNOWN)
        svc.register_consumed(EV_C, EventState.INVALID)

        await roster.discover()
        # The global Identify Events routed through a's inbound
        # frame handler triggers identify_all(); those replies then
        # populate b's roster.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert roster.produced_by(0x111) == {
            EV_A: EventState.VALID, EV_B: EventState.UNKNOWN,
        }
        assert roster.consumed_by(0x111) == {EV_C: EventState.INVALID}
    finally:
        svc.close()
        roster.close()


# ---- Producer/Consumer Range Identified ---------------------------------


def test_decode_event_range_16_event_span_from_trailing_zeros() -> None:
    """Event 0x0000_0000_0000_0010: bit 0 = 0, four trailing zeros
    -> 16 events, 0x10..0x1F."""
    ev = EventId(0x10)
    start, end = decode_event_range(ev)
    assert start == EventId(0x10)
    assert end == EventId(0x1F)


def test_decode_event_range_32_event_span_from_trailing_ones() -> None:
    """Event ...001F: bit 0 = 1, five trailing ones -> 32 events,
    0x00..0x1F."""
    ev = EventId(0x1F)
    start, end = decode_event_range(ev)
    assert start == EventId(0x00)
    assert end == EventId(0x1F)


def test_decode_event_range_preserves_the_fixed_prefix() -> None:
    """A realistic well-known event with a high fixed prefix and a
    low-byte range. 05.01.01.01.4C.43.00.F0 -> bit 0 = 0, four
    trailing zeros -> 05.01.01.01.4C.43.00.F0 .. F0|F = FF."""
    ev = EventId.parse("05.01.01.01.4C.43.00.F0")
    start, end = decode_event_range(ev)
    assert start == EventId.parse("05.01.01.01.4C.43.00.F0")
    assert end == EventId.parse("05.01.01.01.4C.43.00.FF")


def test_decode_event_range_single_event_when_no_variable_bits() -> None:
    """No trailing zeros to consume -> range is a single event.
    Interesting edge but the encoding still parses cleanly."""
    ev = EventId(0x01)   # bit 0 = 1, only one trailing one -> 2 events.
    start, end = decode_event_range(ev)
    assert start == EventId(0x00)
    assert end == EventId(0x01)


def test_decode_event_range_all_zero_covers_entire_space() -> None:
    """The degenerate all-zero event: no fixed bits at all. Spec
    allows it; we return the full 64-bit event space rather than
    zero-divide."""
    start, end = decode_event_range(EventId(0))
    assert start == EventId(0)
    assert end == EventId((1 << 64) - 1)


async def test_roster_records_producer_range_identified() -> None:
    """A peer emits Producer Range Identified -> the roster stores
    the decoded (start, end) tuple for that peer."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        # Peer B advertises production of events F0..FF as one range.
        ev = EventId.parse("05.01.01.01.4C.43.00.F0")
        await b.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_PRODUCER_RANGE_IDENTIFIED, 0x222,
            ),
            data=ev.bytes,
        ))
        await asyncio.sleep(0)
        ranges = roster.produced_ranges_by(0x222)
        assert ranges == [(
            EventId.parse("05.01.01.01.4C.43.00.F0"),
            EventId.parse("05.01.01.01.4C.43.00.FF"),
        )]
        # And the peer shows up in known_aliases even without a
        # single-event advertisement.
        assert 0x222 in roster.known_aliases()
    finally:
        roster.close()


async def test_roster_records_consumer_range_identified() -> None:
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.F0")
        await b.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_CONSUMER_RANGE_IDENTIFIED, 0x222,
            ),
            data=ev.bytes,
        ))
        await asyncio.sleep(0)
        assert roster.consumed_ranges_by(0x222) == [(
            EventId.parse("05.01.01.01.4C.43.00.F0"),
            EventId.parse("05.01.01.01.4C.43.00.FF"),
        )]
    finally:
        roster.close()


async def test_roster_forget_drops_ranges_too() -> None:
    """AMR must clear range-identified data along with everything
    else -- otherwise a re-joining node with the same alias would
    show a stale range."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.F0")
        await b.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_PRODUCER_RANGE_IDENTIFIED, 0x222,
            ),
            data=ev.bytes,
        ))
        await asyncio.sleep(0)
        assert 0x222 in roster.known_aliases()
        roster.forget(0x222)
        assert 0x222 not in roster.known_aliases()
        assert roster.produced_ranges_by(0x222) == []
    finally:
        roster.close()


async def test_roster_dedupes_repeated_ranges() -> None:
    """Same range advertised twice by the same peer collapses to one
    entry -- the peer's state changed but the range didn't."""
    a, b = _pair(0x111, 0x222)
    roster = EventRoster(a)
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.F0")
        for _ in range(3):
            await b.send_frame(OlcbFrame(
                header=_pack_header(
                    FT_MTI, MTI_PRODUCER_RANGE_IDENTIFIED, 0x222,
                ),
                data=ev.bytes,
            ))
        await asyncio.sleep(0)
        assert len(roster.produced_ranges_by(0x222)) == 1
    finally:
        roster.close()


# ---- encode_event_range --------------------------------------------------


def test_encode_range_produces_bit_zero_form() -> None:
    """A 16-event range with 4 trailing variable bits encodes as
    the start event (which has bit 0 = 0)."""
    start = EventId.parse("05.01.01.01.4C.43.00.F0")
    end = EventId.parse("05.01.01.01.4C.43.00.FF")
    encoded = encode_event_range(start, end)
    assert encoded == start


def test_encode_range_round_trips_through_decode() -> None:
    """encode + decode = identity for any valid range. Try a few
    sizes to catch off-by-ones."""
    cases = [
        ("05.01.01.01.4C.43.00.F0", "05.01.01.01.4C.43.00.FF"),   # 16
        ("05.01.01.01.4C.43.00.00", "05.01.01.01.4C.43.00.FF"),   # 256
        ("05.01.01.01.4C.43.02.00", "05.01.01.01.4C.43.03.FF"),   # 512
        ("05.01.01.01.4C.43.00.02", "05.01.01.01.4C.43.00.03"),   # 2
    ]
    for s, e in cases:
        start = EventId.parse(s)
        end = EventId.parse(e)
        encoded = encode_event_range(start, end)
        decoded_start, decoded_end = decode_event_range(encoded)
        assert (decoded_start, decoded_end) == (start, end), (
            f"round-trip failed for {s}..{e}"
        )


def test_encode_range_rejects_misaligned_start() -> None:
    """05.01.01.01.4C.43.00.10 has 4 trailing zero bits, aligned to
    a 16-boundary. Starting at .11 breaks the alignment -- you'd
    need two ranges (11..1F, 20..2F) instead."""
    start = EventId.parse("05.01.01.01.4C.43.00.11")
    end = EventId.parse("05.01.01.01.4C.43.00.20")
    with pytest.raises(ValueError, match="aligned"):
        encode_event_range(start, end)


def test_encode_range_rejects_non_power_of_2_size() -> None:
    """Size 3 = binary 011, xor = 011, xor+1 = 100 (power of 2). OK.
    Size 5 = xor 100 (start=00, end=04). xor+1=101, not a power of 2
    -> invalid."""
    start = EventId.parse("05.01.01.01.4C.43.00.00")
    end = EventId.parse("05.01.01.01.4C.43.00.04")
    with pytest.raises(ValueError, match="power-of-2"):
        encode_event_range(start, end)


def test_encode_range_rejects_single_event() -> None:
    """A single-event 'range' should use per-event advertisement,
    not the Range MTI -- guard against silent misuse."""
    ev = EventId.parse("05.01.01.01.4C.43.00.42")
    with pytest.raises(ValueError, match="single-event"):
        encode_event_range(ev, ev)


def test_encode_range_rejects_reversed_bounds() -> None:
    start = EventId.parse("05.01.01.01.4C.43.00.FF")
    end = EventId.parse("05.01.01.01.4C.43.00.F0")
    with pytest.raises(ValueError, match="start"):
        encode_event_range(start, end)


# ---- service: emit Range Identified on identify_all ---------------------


async def test_service_emits_producer_range_on_identify_all() -> None:
    """A registered produced range is advertised as one Producer
    Range Identified frame on identify_all() -- not 16 individual
    Producer Identified frames."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    seen: list[OlcbFrame] = []
    b.frames.connect(lambda f: seen.append(f))
    try:
        start = EventId.parse("05.01.01.01.4C.43.00.F0")
        end = EventId.parse("05.01.01.01.4C.43.00.FF")
        svc.register_produced_range(start, end)
        await svc.identify_all()
        await asyncio.sleep(0)
        # Filter to Producer Range Identified frames.
        ranges = [
            f for f in seen
            if extract_mti(f.header) == MTI_PRODUCER_RANGE_IDENTIFIED
        ]
        assert len(ranges) == 1
        # Payload decodes back to (start, end).
        decoded = decode_event_range(
            EventId(int.from_bytes(ranges[0].data, "big")),
        )
        assert decoded == (start, end)
    finally:
        svc.close()


async def test_service_emits_consumer_range_on_identify_all() -> None:
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    seen: list[OlcbFrame] = []
    b.frames.connect(lambda f: seen.append(f))
    try:
        start = EventId.parse("05.01.01.01.4C.43.01.00")
        end = EventId.parse("05.01.01.01.4C.43.01.FF")
        svc.register_consumed_range(start, end)
        await svc.identify_all()
        await asyncio.sleep(0)
        ranges = [
            f for f in seen
            if extract_mti(f.header) == MTI_CONSUMER_RANGE_IDENTIFIED
        ]
        assert len(ranges) == 1
        decoded = decode_event_range(
            EventId(int.from_bytes(ranges[0].data, "big")),
        )
        assert decoded == (start, end)
    finally:
        svc.close()


async def test_targeted_identify_producers_matches_range() -> None:
    """A peer's Identify Producers targeting an event that falls
    inside one of our ranges triggers a Range Identified reply."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    seen: list[OlcbFrame] = []
    b.frames.connect(lambda f: seen.append(f))
    try:
        start = EventId.parse("05.01.01.01.4C.43.00.F0")
        end = EventId.parse("05.01.01.01.4C.43.00.FF")
        svc.register_produced_range(start, end)
        # Peer asks about F5 (inside the range) -- service must answer
        # with the range advertisement so peer learns the whole span.
        probe = EventId.parse("05.01.01.01.4C.43.00.F5")
        await b.send_frame(_mk_probe(MTI_IDENTIFY_PRODUCERS, 0x222, probe))
        await asyncio.sleep(0)
        ranges = [
            f for f in seen
            if extract_mti(f.header) == MTI_PRODUCER_RANGE_IDENTIFIED
        ]
        assert len(ranges) == 1
    finally:
        svc.close()


async def test_targeted_identify_ignores_event_outside_range() -> None:
    """A probe for an event we don't produce (and that's not in a
    range) must not provoke a reply."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    seen: list[OlcbFrame] = []
    b.frames.connect(lambda f: seen.append(f))
    try:
        svc.register_produced_range(
            EventId.parse("05.01.01.01.4C.43.00.F0"),
            EventId.parse("05.01.01.01.4C.43.00.FF"),
        )
        far_away = EventId.parse("05.01.01.01.4C.43.99.99")
        await b.send_frame(_mk_probe(MTI_IDENTIFY_PRODUCERS, 0x222, far_away))
        await asyncio.sleep(0)
        assert seen == []
    finally:
        svc.close()


async def test_register_range_validates_at_call_time() -> None:
    """Bad ranges fail at register-time, not at identify-time -- the
    operator hears about the mistake immediately."""
    a, _ = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    try:
        with pytest.raises(ValueError):
            svc.register_produced_range(
                EventId.parse("05.01.01.01.4C.43.00.11"),
                EventId.parse("05.01.01.01.4C.43.00.20"),
            )
    finally:
        svc.close()


async def test_service_end_to_end_with_peer_roster() -> None:
    """End-to-end: a service registering a produced range and a peer
    running an EventRoster. After identify_all(), the roster should
    know the range without any per-event advertisements."""
    a, b = _pair(0x111, 0x222)
    svc = ProducerConsumerService(a)
    roster = EventRoster(b)
    try:
        start = EventId.parse("05.01.01.01.4C.43.00.F0")
        end = EventId.parse("05.01.01.01.4C.43.00.FF")
        svc.register_produced_range(start, end)
        await svc.identify_all()
        await asyncio.sleep(0)
        assert roster.produced_ranges_by(0x111) == [(start, end)]
    finally:
        svc.close()
        roster.close()
