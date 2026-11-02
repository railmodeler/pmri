"""Traction Search Protocol -- S-9.7.4.7 sec.7.

An OpenLCB throttle finds a loco by DCC address using a specially-
constructed **search event ID**, sent over the standard event
exchange primitives:

* Request: **Identify Producer** (MTI ``0x0914``) with the search
  event as its 8-byte payload -- same MTI as generic P/C identify,
  but the well-known event-ID prefix makes it obvious to CS-side
  handlers that this is a train search rather than a generic event.
* Reply: **Producer Identified Valid** (MTI ``0x0544``) with the
  same event ID, sourced from the matching loco's alias. A CS with
  three matching locos emits three replies, one per hosted loco;
  the client collects replies for a short window and returns the
  set.

Search event ID layout (S-9.7.4.7 sec.7.4)::

    bytes 0..3 : fixed prefix   0x09 0x00 0x99 0xFF
    bytes 4..6 : 6-digit BCD DCC address, high digit first
                 (e.g. 3601 -> 0x00 0x36 0x01)
    byte  7    : flag/protocol byte:
                   bit 7    = force allocate (CS should create if absent)
                   bit 1    = long-address match
                   bit 0    = short-address match
                   both 0/1 = match either type
                   both 1   = reserved (treated as "either")

Force-allocate is deliberately a hint -- a CS may honour it or ignore
it. Our :class:`CommandStationNode` currently ignores it: the
operator's expected to :meth:`~CommandStationNode.add_loco` the DCC
addresses they want hosted; a search doesn't dynamically materialize
a new loco. A separate task can add opt-in force-allocate handling
later.

Cross-referenced against openlcb-python's ``TractionSearchProtocol``
module and the S-9.7.4.7 Rev 2.0 examples.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
)
from jmri_openlcb.events import EventId

if TYPE_CHECKING:
    from jmri_core.events import Signal

_log = logging.getLogger(__name__)

# ---- MTIs ---------------------------------------------------------------

MTI_IDENTIFY_PRODUCERS = 0x0914
MTI_PRODUCER_IDENT_VALID = 0x0544

# ---- Wire constants -----------------------------------------------------

# Fixed 4-byte prefix identifying an event ID as a traction search
# request. Occupies bytes 0..3 of the 8-byte event.
SEARCH_EVENT_PREFIX = 0x09_00_99_FF_00_00_00_00

# Prefix + BCD address bits + protocol/flag byte fit in 8 bytes.
# BCD address lives in bytes 4..6 (24 bits = 6 decimal digits).
_BCD_MASK = 0x00_00_00_00_FF_FF_FF_00
_FLAG_MASK = 0x00_00_00_00_00_00_00_FF

# Flag-byte bit assignments (byte 7).
_FLAG_FORCE_ALLOCATE = 0x80
_FLAG_MATCH_LONG = 0x02
_FLAG_MATCH_SHORT = 0x01

# Default search timeout -- enough for one round-trip on a WAN-tunneled
# hub, generous enough to collect replies from a CS with several
# matching hosted locos (each is a separate reply frame).
SEARCH_TIMEOUT = 1.0


# ---- data ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Decoded contents of a traction search event.

    ``long_address`` is ``None`` when the caller didn't constrain
    address type (either matches); ``True`` / ``False`` when the
    search targeted a specific type only.
    """

    address: int
    long_address: bool | None = None
    force_allocate: bool = False

    def matches(self, address: int, long_address: bool) -> bool:
        """Return True iff a hosted loco with the given DCC address
        + long flag should reply to this search."""
        if self.address != address:
            return False
        if self.long_address is None:
            return True
        return self.long_address == long_address


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One matching loco reported by the search."""

    alias: int
    event: EventId   # echoed back -- useful for correlating multiple
                     # concurrent searches against distinct events


# ---- encode / decode ---------------------------------------------------


def _address_to_bcd(address: int) -> int:
    """Encode a decimal *address* into a 24-bit BCD word, high digit
    in the most significant nibble. Uses 6 digits of storage;
    addresses 0..999999 fit. DCC addresses top out at 10239 so the
    upper digits normally read 0."""
    if not 0 <= address <= 999999:
        raise ValueError(
            f"search address must fit in 6 BCD digits (0..999999), got {address}",
        )
    bcd = 0
    remaining = address
    for i in range(6):
        digit = remaining % 10
        remaining //= 10
        bcd |= digit << (i * 4)
    return bcd


def _bcd_to_address(bcd: int) -> int:
    """Inverse of :func:`_address_to_bcd`. Rejects nibbles > 9 (not
    valid BCD) so malformed search events surface immediately."""
    address = 0
    place = 1
    for i in range(6):
        digit = (bcd >> (i * 4)) & 0xF
        if digit > 9:
            raise ValueError(
                f"malformed BCD nibble at position {i}: {digit:#x}",
            )
        address += digit * place
        place *= 10
    return address


def encode_search_event(
    address: int,
    *,
    long_address: bool | None = None,
    force_allocate: bool = False,
) -> EventId:
    """Build a Traction Search event ID.

    *long_address* is a tri-state:

    * ``None`` (default) -- match either address type. Byte 7 has
      neither the short nor the long bit set. Some CS implementations
      treat this as "figure out from the address value" (short if
      <=127, long otherwise); others match any.
    * ``True`` -- match only long addresses. Byte 7 sets bit 1.
    * ``False`` -- match only short addresses. Byte 7 sets bit 0.
    """
    bcd = _address_to_bcd(address)
    flags = 0
    if force_allocate:
        flags |= _FLAG_FORCE_ALLOCATE
    if long_address is True:
        flags |= _FLAG_MATCH_LONG
    elif long_address is False:
        flags |= _FLAG_MATCH_SHORT
    value = SEARCH_EVENT_PREFIX | (bcd << 8) | flags
    return EventId(value)


def decode_search_event(event: EventId) -> SearchQuery | None:
    """Decode a search event ID -> :class:`SearchQuery`. Returns
    ``None`` if the event's prefix doesn't match -- that's how a
    handler distinguishes a search from any other Identify Producer
    payload."""
    if (event.value & ~(_BCD_MASK | _FLAG_MASK)) != SEARCH_EVENT_PREFIX:
        return None
    bcd = (event.value & _BCD_MASK) >> 8
    try:
        address = _bcd_to_address(bcd)
    except ValueError:
        return None
    flags = event.value & _FLAG_MASK
    long_flag = bool(flags & _FLAG_MATCH_LONG)
    short_flag = bool(flags & _FLAG_MATCH_SHORT)
    # Only one type explicitly requested -> constrained match.
    # Both flags clear (default) or both set (reserved) -> any.
    if long_flag and not short_flag:
        long_address: bool | None = True
    elif short_flag and not long_flag:
        long_address = False
    else:
        long_address = None
    return SearchQuery(
        address=address,
        long_address=long_address,
        force_allocate=bool(flags & _FLAG_FORCE_ALLOCATE),
    )


# ---- typing shim --------------------------------------------------------


class _TcLike:
    """Same shape as :class:`~jmri_openlcb.snip.SnipTrafficLike`."""


# ---- client -------------------------------------------------------------


class TractionSearchClient:
    """Search for loco nodes by DCC address.

    :meth:`search` broadcasts an Identify Producer with the encoded
    search event, then collects Producer Identified Valid replies
    for a bounded time window. Multiple locos on the bus may match
    the same search (unusual but not impossible -- two CSes each
    hosting address 3), so the result is a list rather than a
    single reply.

    Only one search can be in flight for a given event at a time;
    parallel searches with different addresses are fine.
    """

    def __init__(self, tc: _TcLike) -> None:
        self._tc = tc
        # (event_value -> list of collected results). The event value
        # both keys and cross-checks incoming replies (the reply
        # echoes it back), so two concurrent searches for different
        # addresses can't cross-contaminate.
        self._pending: dict[int, list[SearchResult]] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        self._pending.clear()

    async def search(
        self,
        address: int,
        *,
        long_address: bool | None = None,
        force_allocate: bool = False,
        timeout: float = SEARCH_TIMEOUT,
    ) -> list[SearchResult]:
        """Return a list of matching locos, sorted by alias.

        Blocks for at most *timeout* seconds even if no reply
        arrives -- a search that finds nothing is a normal result,
        not an error. Callers who want "at least one match" behavior
        should check the returned list length."""
        event = encode_search_event(
            address, long_address=long_address, force_allocate=force_allocate,
        )
        key = event.value
        if key in self._pending:
            raise RuntimeError(
                f"search for {address} (event {event.value:#018x}) "
                "already in flight",
            )
        collected: list[SearchResult] = []
        self._pending[key] = collected
        try:
            await self._tc.send_frame(OlcbFrame(
                header=_pack_header(
                    FT_MTI, MTI_IDENTIFY_PRODUCERS, self._tc.source_alias,
                ),
                data=event.bytes,
            ))
            # Block for the full window so slow replies aren't lost.
            # No future to await -- we just gather until the timer
            # runs out.
            await asyncio.sleep(timeout)
            return sorted(collected, key=lambda r: r.alias)
        finally:
            self._pending.pop(key, None)

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) != FT_MTI:
            return
        if extract_mti(frame.header) != MTI_PRODUCER_IDENT_VALID:
            return
        if len(frame.data) < 8:
            return
        event_value = int.from_bytes(frame.data[:8], "big")
        collected = self._pending.get(event_value)
        if collected is None:
            return
        collected.append(SearchResult(
            alias=extract_source_alias(frame.header),
            event=EventId(event_value),
        ))
