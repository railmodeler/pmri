"""Producer/Consumer event identification -- S-9.7.4.1.

Every OpenLCB node advertises which events it produces (fires
PCERs for) and which events it consumes (acts on when it hears a
PCER). A discovery UI uses that map to answer "if I click this
pushbutton, which turnouts follow?" without probing the CDI.

Two roles live in this module:

* :class:`ProducerConsumerService` -- our side. Register events we
  produce/consume; the service answers ``Identify Producers`` /
  ``Identify Consumers`` / ``Identify Events`` requests
  automatically. Also broadcasts ``identify_all`` on demand (e.g.
  right after alias claim, so peers can populate their rosters).
* :class:`EventRoster` -- peer-tracking side. Watches inbound
  ``Producer/Consumer Identified`` messages and builds an
  alias -> event map. Query with ``producers_of(event)`` /
  ``consumers_of(event)`` / ``produced_by(alias)`` /
  ``consumed_by(alias)``. ``discover()`` fires a global
  ``Identify Events`` probe to populate the roster from scratch.

Range messages (Identify {Producers,Consumers} Range,
{Producer,Consumer} Range Identified) are deliberately out of scope
for this pass -- production nodes use point identification for every
common case, and range support adds a bit-mask event-ID range
protocol layer that's better tackled once we've got a real user
who needs it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
)
from pmri_openlcb.events import EventId

if TYPE_CHECKING:
    from pmri_core.events import Signal

_log = logging.getLogger(__name__)

# ---- MTIs (S-9.7.4.1 Table 1) --------------------------------------------
# Every MTI here is 12 bits; frame type is FT_MTI (0x9).

# "Identify" requests -- ask peers to publish who does what.
MTI_IDENTIFY_PRODUCERS = 0x0914
MTI_IDENTIFY_CONSUMERS = 0x08F4
MTI_IDENTIFY_EVENTS_GLOBAL = 0x0970
MTI_IDENTIFY_EVENTS_ADDRESSED = 0x0968

# "Identified" replies -- announce production / consumption + state.
MTI_PRODUCER_IDENT_VALID = 0x0544
MTI_PRODUCER_IDENT_INVALID = 0x0545
MTI_PRODUCER_IDENT_UNKNOWN = 0x0547
MTI_CONSUMER_IDENT_VALID = 0x04C4
MTI_CONSUMER_IDENT_INVALID = 0x04C5
MTI_CONSUMER_IDENT_UNKNOWN = 0x04C7

# Range Identified -- a node advertises a contiguous span of events
# in one frame instead of one per event (S-9.7.4 sec.5.2). Encoded as an
# 8-byte event ID split into a fixed prefix + a variable trailing
# bit-mask; see :func:`decode_event_range`.
MTI_PRODUCER_RANGE_IDENTIFIED = 0x0524
MTI_CONSUMER_RANGE_IDENTIFIED = 0x04A4


def decode_event_range(event: EventId) -> tuple[EventId, EventId]:
    """Decode a Producer/Consumer Range Identified event ID into
    (start, end) -- the inclusive range of individual event IDs the
    range covers.

    The encoding (S-9.7.4 sec.5.2): bit 0 of the reported event ID
    picks the fill value for the mask. If bit 0 is 0, the run of
    trailing zeros forms the variable region and the range is
    ``event .. event | mask``. If bit 0 is 1, the run of trailing
    ones is the variable region and the range is
    ``event & ~mask .. event``.
    """
    value = event.value
    if value & 1 == 0:
        # Trailing zeros are the variable bits.
        if value == 0:
            # No fixed bits at all -> the whole 64-bit space. Unusual
            # but the spec permits it, so keep the arithmetic honest.
            return EventId(0), EventId((1 << 64) - 1)
        low_bit = value & -value  # isolate lowest 1-bit
        k = low_bit.bit_length() - 1  # count of trailing zeros
        mask = (1 << k) - 1
        return EventId(value & ~mask), EventId(value | mask)
    # bit 0 == 1 -> trailing ones are variable.
    inv = ~value & ((1 << 64) - 1)
    if inv == 0:
        return EventId(0), EventId((1 << 64) - 1)
    low_bit = inv & -inv
    k = low_bit.bit_length() - 1
    mask = (1 << k) - 1
    return EventId(value & ~mask), EventId(value | mask)


def encode_event_range(start: EventId, end: EventId) -> EventId:
    """Encode a ``(start, end)`` inclusive range into a Producer/
    Consumer Range Identified event ID (inverse of
    :func:`decode_event_range`).

    The range must form a contiguous power-of-2 span with a fixed
    prefix -- i.e. ``start`` and ``end`` differ only in a trailing
    run of bits, so ``end - start + 1`` is a power of 2 and
    ``start`` is aligned to that boundary. A range that doesn't fit
    (e.g. 5..12, or 4..10) raises :class:`ValueError`; the caller
    should split it into multiple valid ranges or fall back to
    per-event Producer/Consumer Identified frames.

    Two encoding forms exist (bit 0 = 0 -> start, bit 0 = 1 -> end);
    which one round-trips correctly depends on the bit ABOVE the
    mask in the fixed prefix. If that bit is 1, the bit-0=0 form
    gives exactly k trailing zeros; if it's 0, the bit-0=1 form
    gives exactly k trailing ones. Blindly picking one form gives
    the wrong length when the boundary lines up with a longer
    natural run (e.g. a 256-event range starting at ...00.00 has
    16 trailing zeros in ``start``, decoding as 65536 events).
    """
    if start.value > end.value:
        raise ValueError(
            f"range start {start} > end {end}",
        )
    if start.value == end.value:
        # Trivially a single event -- nothing to encode as a range.
        # Callers using ProducerConsumerService.register_produced()
        # emit these as regular Identified frames; catch it here so
        # they can't accidentally advertise a "range" of one.
        raise ValueError(
            "single-event range is not a Range Identified -- "
            "use register_produced/register_consumed instead",
        )
    xor = start.value ^ end.value
    # For a valid contiguous trailing run, xor+1 must be a power of 2
    # (all differing bits form a contiguous span starting at bit 0).
    if xor & (xor + 1) != 0:
        raise ValueError(
            f"range {start}..{end} is not power-of-2 aligned "
            f"(xor={xor:#x}); split into aligned sub-ranges",
        )
    # And start must be aligned to the mask (low bits zero).
    if start.value & xor != 0:
        raise ValueError(
            f"range start {start} is not aligned to mask "
            f"{xor:#x}; split into aligned sub-ranges",
        )
    k = xor.bit_length()   # number of variable trailing bits
    # Pick the encoding form so the decoder finds exactly k
    # trailing zeros or ones (not more). Bit k of the fixed prefix
    # decides. When k >= 64 (whole space) either form works; we
    # return ``start`` for stability.
    if k >= 64:
        return start
    bit_above = (start.value >> k) & 1
    if bit_above == 1:
        # Bit k of start is 1 -> trailing zeros of start = exactly k.
        return start
    # Bit k of the prefix is 0 -> trailing ones of end = exactly k.
    return end


class EventState(Enum):
    """Per S-9.7.4.1 sec.5, a Producer / Consumer Identified reply
    carries a 2-bit state indicating whether the event is currently
    active/asserted at the source (or, for consumers, the last
    thing they heard).

    * :attr:`VALID` -- the event is asserted / recently produced.
    * :attr:`INVALID` -- the event has been reset / recently un-produced.
    * :attr:`UNKNOWN` -- no state tracked. Default for freshly-registered
      events until the caller reports otherwise.
    """

    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


# The wire MTI for a P/C Identified reply is chosen by state.
_PRODUCER_MTI_BY_STATE: dict[EventState, int] = {
    EventState.VALID: MTI_PRODUCER_IDENT_VALID,
    EventState.INVALID: MTI_PRODUCER_IDENT_INVALID,
    EventState.UNKNOWN: MTI_PRODUCER_IDENT_UNKNOWN,
}
_CONSUMER_MTI_BY_STATE: dict[EventState, int] = {
    EventState.VALID: MTI_CONSUMER_IDENT_VALID,
    EventState.INVALID: MTI_CONSUMER_IDENT_INVALID,
    EventState.UNKNOWN: MTI_CONSUMER_IDENT_UNKNOWN,
}
# Reverse lookup for parsing inbound frames.
_STATE_BY_PRODUCER_MTI: dict[int, EventState] = {
    v: k for k, v in _PRODUCER_MTI_BY_STATE.items()
}
_STATE_BY_CONSUMER_MTI: dict[int, EventState] = {
    v: k for k, v in _CONSUMER_MTI_BY_STATE.items()
}


# ---- structural typing shim ----------------------------------------------


class _TcLike:
    """Structural type -- see :class:`~pmri_openlcb.snip.SnipTrafficLike`.

    Required attributes: ``source_alias`` (int), ``frames``
    (:class:`~pmri_core.events.Signal` of :class:`OlcbFrame`),
    ``send_frame(frame)`` coroutine.
    """


# ---- our side: ProducerConsumerService -----------------------------------


class ProducerConsumerService:
    """Advertise this node's produced/consumed events.

    Register events via :meth:`register_produced` /
    :meth:`register_consumed` (both take an optional starting
    state); ``set_produced_state`` / ``set_consumer_state`` update
    it later without re-registering. The service auto-answers
    ``Identify Producers`` / ``Identify Consumers`` /
    ``Identify Events`` frames.

    Call :meth:`identify_all` right after :func:`~pmri_openlcb.alias.claim_alias`
    completes so peers can populate their rosters without probing.
    """

    def __init__(self, tc: _TcLike) -> None:
        self._tc = tc
        self._produced: dict[EventId, EventState] = {}
        self._consumed: dict[EventId, EventState] = {}
        # Range advertisements -- (start, end) inclusive tuples. Stored
        # as a set so re-registering the same range doesn't duplicate.
        self._produced_ranges: set[tuple[EventId, EventId]] = set()
        self._consumed_ranges: set[tuple[EventId, EventId]] = set()
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    # ---- registration ------------------------------------------------

    def register_produced(
        self, event: EventId, state: EventState = EventState.UNKNOWN,
    ) -> None:
        self._produced[event] = state

    def register_consumed(
        self, event: EventId, state: EventState = EventState.UNKNOWN,
    ) -> None:
        self._consumed[event] = state

    def unregister_produced(self, event: EventId) -> None:
        self._produced.pop(event, None)

    def unregister_consumed(self, event: EventId) -> None:
        self._consumed.pop(event, None)

    def register_produced_range(
        self, start: EventId, end: EventId,
    ) -> None:
        """Advertise a contiguous span of produced events. On
        ``identify_all`` we'll emit a single Producer Range
        Identified frame instead of one per event -- the wire cost
        of 256 turnouts drops from 256 frames to 1.

        Range must be power-of-2 aligned; see :func:`encode_event_range`
        for validation rules. Raises :class:`ValueError` otherwise."""
        # Validate at registration time so the caller learns
        # immediately, not on the first identify_all attempt.
        encode_event_range(start, end)
        self._produced_ranges.add((start, end))

    def register_consumed_range(
        self, start: EventId, end: EventId,
    ) -> None:
        encode_event_range(start, end)
        self._consumed_ranges.add((start, end))

    def unregister_produced_range(
        self, start: EventId, end: EventId,
    ) -> None:
        self._produced_ranges.discard((start, end))

    def unregister_consumed_range(
        self, start: EventId, end: EventId,
    ) -> None:
        self._consumed_ranges.discard((start, end))

    @property
    def produced_ranges(self) -> set[tuple[EventId, EventId]]:
        return set(self._produced_ranges)

    @property
    def consumed_ranges(self) -> set[tuple[EventId, EventId]]:
        return set(self._consumed_ranges)

    def set_produced_state(self, event: EventId, state: EventState) -> None:
        """Update the state a subsequent ``Producer Identified`` will
        carry. Silently no-ops when *event* isn't registered -- the
        caller shouldn't have to check first."""
        if event in self._produced:
            self._produced[event] = state

    def set_consumer_state(self, event: EventId, state: EventState) -> None:
        if event in self._consumed:
            self._consumed[event] = state

    @property
    def produced(self) -> dict[EventId, EventState]:
        """Snapshot of our produced events."""
        return dict(self._produced)

    @property
    def consumed(self) -> dict[EventId, EventState]:
        return dict(self._consumed)

    # ---- broadcasting ------------------------------------------------

    async def identify_all(self) -> None:
        """Fire ``Producer Identified`` for every produced event and
        ``Consumer Identified`` for every consumed event, followed
        by ``Producer Range Identified`` / ``Consumer Range
        Identified`` for each registered range.

        Broadcast after alias claim, or in response to an
        ``Identify Events`` request. Ordering: producers first,
        then consumers -- matches the JMRI Java implementation so
        packet sniffers see the same pattern."""
        for ev, state in list(self._produced.items()):
            await self._send_producer_ident(ev, state)
        for ev, state in list(self._consumed.items()):
            await self._send_consumer_ident(ev, state)
        for start, end in list(self._produced_ranges):
            await self._send_producer_range(start, end)
        for start, end in list(self._consumed_ranges):
            await self._send_consumer_range(start, end)

    async def _send_producer_ident(
        self, event: EventId, state: EventState,
    ) -> None:
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, _PRODUCER_MTI_BY_STATE[state], self._tc.source_alias,
            ),
            data=event.bytes,
        ))

    async def _send_consumer_ident(
        self, event: EventId, state: EventState,
    ) -> None:
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, _CONSUMER_MTI_BY_STATE[state], self._tc.source_alias,
            ),
            data=event.bytes,
        ))

    async def _send_producer_range(
        self, start: EventId, end: EventId,
    ) -> None:
        encoded = encode_event_range(start, end)
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_PRODUCER_RANGE_IDENTIFIED, self._tc.source_alias,
            ),
            data=encoded.bytes,
        ))

    async def _send_consumer_range(
        self, start: EventId, end: EventId,
    ) -> None:
        encoded = encode_event_range(start, end)
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_CONSUMER_RANGE_IDENTIFIED, self._tc.source_alias,
            ),
            data=encoded.bytes,
        ))

    def _range_contains(
        self,
        ranges: set[tuple[EventId, EventId]],
        event: EventId,
    ) -> tuple[EventId, EventId] | None:
        for start, end in ranges:
            if start.value <= event.value <= end.value:
                return (start, end)
        return None

    # ---- inbound identify requests -----------------------------------

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) != FT_MTI:
            return
        mti = extract_mti(frame.header)
        if mti == MTI_IDENTIFY_EVENTS_GLOBAL:
            await self.identify_all()
            return
        if mti == MTI_IDENTIFY_EVENTS_ADDRESSED:
            # Addressed identify probes carry a 2-byte dest-alias
            # prefix; answer only when it targets us.
            if len(frame.data) >= 2:
                dest = int.from_bytes(frame.data[:2], "big") & 0x0FFF
                if dest == self._tc.source_alias:
                    await self.identify_all()
            return
        if mti == MTI_IDENTIFY_PRODUCERS:
            if len(frame.data) >= 8:
                ev = EventId(int.from_bytes(frame.data[:8], "big"))
                if ev in self._produced:
                    await self._send_producer_ident(ev, self._produced[ev])
                else:
                    # A peer asking about a single event that falls
                    # inside one of our ranges should get the range
                    # advertisement -- otherwise they'd conclude we
                    # don't produce the event.
                    r = self._range_contains(self._produced_ranges, ev)
                    if r is not None:
                        await self._send_producer_range(*r)
            return
        if mti == MTI_IDENTIFY_CONSUMERS:
            if len(frame.data) >= 8:
                ev = EventId(int.from_bytes(frame.data[:8], "big"))
                if ev in self._consumed:
                    await self._send_consumer_ident(ev, self._consumed[ev])
                else:
                    r = self._range_contains(self._consumed_ranges, ev)
                    if r is not None:
                        await self._send_consumer_range(*r)
            return


# ---- peer-side: EventRoster ----------------------------------------------


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """A single (event, state) advertisement from a peer alias.

    The state changes over time as the peer re-broadcasts; the
    :class:`EventRoster` stores the most-recent value per (alias,
    event). Kept as an explicit dataclass rather than a bare tuple
    so the API can grow without breaking callers.
    """

    event: EventId
    state: EventState


class EventRoster:
    """Track what every peer produces and consumes.

    Subscribes to inbound frames on the traffic controller and
    updates its map on every P/C Identified reply. Query via
    :meth:`producers_of` / :meth:`consumers_of` (event -> aliases)
    or :meth:`produced_by` / :meth:`consumed_by`
    (alias -> events).

    A discovery UI can call :meth:`discover` to broadcast a global
    ``Identify Events`` -- every peer will then emit its full set of
    P/C Identified replies, populating our maps.
    """

    def __init__(self, tc: _TcLike) -> None:
        self._tc = tc
        self._produced_by: dict[int, dict[EventId, EventState]] = defaultdict(dict)
        self._consumed_by: dict[int, dict[EventId, EventState]] = defaultdict(dict)
        # Range Identified stores (start, end) tuples (both inclusive).
        # Duplicate ranges from the same alias collapse via set-of-tuples.
        self._produced_ranges: dict[int, set[tuple[EventId, EventId]]] = (
            defaultdict(set)
        )
        self._consumed_ranges: dict[int, set[tuple[EventId, EventId]]] = (
            defaultdict(set)
        )
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    # ---- discovery ---------------------------------------------------

    async def discover(self) -> None:
        """Broadcast ``Identify Events`` -- every listening node
        answers with its full produced/consumed event set."""
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_IDENTIFY_EVENTS_GLOBAL, self._tc.source_alias,
            ),
            data=b"",
        ))

    async def identify_producers_of(self, event: EventId) -> None:
        """Ask specifically who produces *event*."""
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_IDENTIFY_PRODUCERS, self._tc.source_alias,
            ),
            data=event.bytes,
        ))

    async def identify_consumers_of(self, event: EventId) -> None:
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_IDENTIFY_CONSUMERS, self._tc.source_alias,
            ),
            data=event.bytes,
        ))

    # ---- queries -----------------------------------------------------

    def producers_of(self, event: EventId) -> set[int]:
        """Aliases of nodes advertising themselves as producers of
        *event*, regardless of the reported state."""
        return {
            alias for alias, evs in self._produced_by.items()
            if event in evs
        }

    def consumers_of(self, event: EventId) -> set[int]:
        return {
            alias for alias, evs in self._consumed_by.items()
            if event in evs
        }

    def produced_by(self, alias: int) -> dict[EventId, EventState]:
        """Snapshot of *alias*'s produced-events map (event -> state)."""
        return dict(self._produced_by.get(alias, {}))

    def consumed_by(self, alias: int) -> dict[EventId, EventState]:
        return dict(self._consumed_by.get(alias, {}))

    def produced_ranges_by(
        self, alias: int,
    ) -> list[tuple[EventId, EventId]]:
        """Ranges *alias* advertised via Producer Range Identified.
        Each entry is ``(start, end)`` inclusive. Sorted by start
        so a UI renders them in a predictable order."""
        return sorted(self._produced_ranges.get(alias, set()))

    def consumed_ranges_by(
        self, alias: int,
    ) -> list[tuple[EventId, EventId]]:
        return sorted(self._consumed_ranges.get(alias, set()))

    def known_aliases(self) -> set[int]:
        """Union of every alias we've heard producer or consumer
        replies from -- single-event and range identifiers alike."""
        return (
            set(self._produced_by) | set(self._consumed_by)
            | set(self._produced_ranges) | set(self._consumed_ranges)
        )

    def forget(self, alias: int) -> None:
        """Drop everything we know about *alias*. Call this on
        Alias Map Reset (AMR) so a node that vanishes doesn't
        haunt the roster forever."""
        self._produced_by.pop(alias, None)
        self._consumed_by.pop(alias, None)
        self._produced_ranges.pop(alias, None)
        self._consumed_ranges.pop(alias, None)

    # ---- inbound frame -> roster update -------------------------------

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) != FT_MTI:
            return
        mti = extract_mti(frame.header)
        source = extract_source_alias(frame.header)
        if mti in _STATE_BY_PRODUCER_MTI:
            if len(frame.data) >= 8:
                ev = EventId(int.from_bytes(frame.data[:8], "big"))
                self._produced_by[source][ev] = _STATE_BY_PRODUCER_MTI[mti]
            return
        if mti in _STATE_BY_CONSUMER_MTI:
            if len(frame.data) >= 8:
                ev = EventId(int.from_bytes(frame.data[:8], "big"))
                self._consumed_by[source][ev] = _STATE_BY_CONSUMER_MTI[mti]
            return
        if mti == MTI_PRODUCER_RANGE_IDENTIFIED and len(frame.data) >= 8:
            ev = EventId(int.from_bytes(frame.data[:8], "big"))
            self._produced_ranges[source].add(decode_event_range(ev))
            return
        if mti == MTI_CONSUMER_RANGE_IDENTIFIED and len(frame.data) >= 8:
            ev = EventId(int.from_bytes(frame.data[:8], "big"))
            self._consumed_ranges[source].add(decode_event_range(ev))
            return


# ---- helper for consumers of the roster ----------------------------------


@dataclass
class NodeEventSummary:
    """A convenience view: everything a single peer node advertises.

    :class:`EventRoster` doesn't expose this directly -- build one on
    demand via :func:`summarize_node` if a UI wants a per-alias
    "what does this node do" panel."""

    alias: int
    produced: dict[EventId, EventState] = field(default_factory=dict)
    consumed: dict[EventId, EventState] = field(default_factory=dict)


def summarize_node(roster: EventRoster, alias: int) -> NodeEventSummary:
    """Bundle *alias*'s produced/consumed maps into one struct."""
    return NodeEventSummary(
        alias=alias,
        produced=roster.produced_by(alias),
        consumed=roster.consumed_by(alias),
    )
