"""Per-peer PCER event history -- count + last-seen per event ID.

The bus monitor's ring holds recent frames globally; the event
roster tracks "which peer advertises which events." Neither answers
"how often is peer X actually emitting event Y, and when did I last
see it?" -- the workaday question when debugging a stuck sensor or a
chatty consist heartbeat.

:class:`PcerHistory` subscribes to a traffic controller and records
per (source_alias, event_id) counts + last-seen timestamps. Bounded
via an LRU on the event dimension per alias so a peer that emits a
long tail of distinct events doesn't grow the map without limit.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jmri_openlcb.codec import (
    MTI_PCER,
    OlcbFrame,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
)
from jmri_openlcb.events import EventId

if TYPE_CHECKING:
    from jmri_openlcb.alias import OlcbTrafficLike

_log = logging.getLogger(__name__)


# Max distinct events kept per alias. A single decoder rarely
# advertises more than a dozen events; 50 covers boards with many
# sensors without letting a rogue peer inflate memory.
DEFAULT_MAX_EVENTS_PER_ALIAS = 50

FT_MTI = 0x9   # kept local to avoid a codec import cycle


@dataclass(frozen=True, slots=True)
class PcerEntry:
    """One (event, count, last_seen) row for a specific peer."""

    event: EventId
    count: int
    last_seen: float   # wall-clock epoch seconds


class PcerHistory:
    """Watch inbound frames for PCERs and maintain per-alias counts.

    Query with :meth:`for_alias` (returns entries sorted last-seen
    first). :meth:`forget` drops an alias -- wire it into the
    connection's AMR / stale-peer forget fan-out to keep the map
    tidy.
    """

    def __init__(
        self,
        tc: "OlcbTrafficLike",
        *,
        max_events_per_alias: int = DEFAULT_MAX_EVENTS_PER_ALIAS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._tc = tc
        self._clock = clock
        self._max = max_events_per_alias
        # per-alias LRU: {alias: OrderedDict[event, PcerEntry]}
        # Newest-touched event moves to the end via move_to_end.
        self._by_alias: dict[int, OrderedDict[EventId, PcerEntry]] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    def for_alias(self, alias: int) -> list[PcerEntry]:
        """Snapshot of *alias*'s PCER history, most-recent first."""
        events = self._by_alias.get(alias)
        if not events:
            return []
        return sorted(
            events.values(), key=lambda e: e.last_seen, reverse=True,
        )

    def known_aliases(self) -> set[int]:
        return set(self._by_alias)

    def forget(self, alias: int) -> None:
        """Drop history for *alias*. Hook up to :class:`AliasReleaseWatcher`
        so an AMR or stale-peer sweep clears the map alongside the
        NID lookup and event roster."""
        self._by_alias.pop(alias, None)

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) != FT_MTI:
            return
        if extract_mti(frame.header) != MTI_PCER:
            return
        if len(frame.data) != 8:
            return
        event = EventId(int.from_bytes(frame.data, "big"))
        alias = extract_source_alias(frame.header)
        events = self._by_alias.setdefault(alias, OrderedDict())
        prev = events.get(event)
        new_count = (prev.count + 1) if prev is not None else 1
        events[event] = PcerEntry(
            event=event, count=new_count, last_seen=self._clock(),
        )
        # LRU: move to end so oldest gets popped when we hit the cap.
        events.move_to_end(event)
        while len(events) > self._max:
            events.popitem(last=False)
