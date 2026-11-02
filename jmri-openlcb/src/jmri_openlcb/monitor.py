"""Bus monitor -- a rolling window of recent inbound frames.

Runs off the traffic controller's ``frames`` signal. Every frame
it captures gets summarised into a small dict that a UI can
display without needing to re-decode the wire format. The buffer
is fixed-size (a ring) so a busy bus doesn't grow it without bound.

Typical usage::

    monitor = FrameMonitor(conn.traffic, max_entries=200)
    ...
    for entry in monitor.recent():
        print(entry.summary)
    ...
    monitor.close()

Wire the monitor once per connection; a JSON topic action reads
:meth:`recent` on demand and the web UI polls it every few seconds.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jmri_openlcb.codec import (
    CTRL_AMD,
    CTRL_AME,
    CTRL_AMR,
    CTRL_RID,
    FT_CONTROL,
    FT_MTI,
    MTI_PCER,
    OlcbFrame,
    cid_seq,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
    extract_variable_field,
    is_cid,
)
from jmri_openlcb.events import EventId, well_known_name

if TYPE_CHECKING:
    from jmri_openlcb.traffic import OlcbTrafficController

_log = logging.getLogger(__name__)

DEFAULT_MONITOR_SIZE = 200


@dataclass(frozen=True, slots=True)
class MonitorEntry:
    """One captured frame + a human-readable summary. Timestamp is
    :func:`time.time` wall-clock so a UI can render it against the
    operator's local clock without extra conversion."""

    timestamp: float
    header: int
    source_alias: int
    frame_type: int
    summary: str
    data_hex: str


class FrameMonitor:
    """Watch a traffic controller and keep the last N frames.

    ``max_entries`` bounds the ring buffer; older entries drop off
    silently as new ones arrive. :meth:`recent` returns a snapshot
    in oldest-first order.
    """

    def __init__(
        self,
        tc: OlcbTrafficController,
        *,
        max_entries: int = DEFAULT_MONITOR_SIZE,
    ) -> None:
        self._tc = tc
        self._buffer: deque[MonitorEntry] = deque(maxlen=max_entries)
        self._paused = False
        # Listen on both inbound and outbound so the monitor is a
        # complete bus log -- inbound from real peers, outbound so
        # an operator can see the frames we sent that provoked the
        # replies. Both signals feed the same summarize path.
        self._unsub_in = tc.frames.connect(self._on_frame)
        self._unsub_out = tc.sent_frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsub_in()
        self._unsub_out()

    def recent(self) -> list[MonitorEntry]:
        """Snapshot of the buffer in insertion order (oldest first)."""
        return list(self._buffer)

    def clear(self) -> None:
        """Drop every buffered entry. Useful for a "reset log" UI
        button."""
        self._buffer.clear()

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop appending new frames to the buffer. Frames already
        captured stay visible. The traffic-controller signal is still
        connected (a full unsubscribe would risk missing the pipeline
        resuming while an operator is mid-analysis)."""
        self._paused = True

    def resume(self) -> None:
        """Re-enable frame capture. Idempotent."""
        self._paused = False

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if self._paused:
            return
        try:
            self._buffer.append(self._summarize(frame))
        except Exception as e:  # noqa: BLE001
            # Monitor mustn't break the frame pipeline -- log and
            # keep the connection healthy.
            _log.warning("monitor summarize failed: %s", e)

    def _summarize(self, frame: OlcbFrame) -> MonitorEntry:
        ft = extract_frame_type(frame.header)
        source = extract_source_alias(frame.header)
        summary = summarize_frame(frame)
        return MonitorEntry(
            timestamp=time.time(),
            header=frame.header,
            source_alias=source,
            frame_type=ft,
            summary=summary,
            data_hex=frame.data.hex().upper(),
        )


def summarize_frame(frame: OlcbFrame) -> str:
    """Human-readable one-line description of *frame*.

    Coverage:

    * PCER -- "PCER: <event dotted-hex>"
    * RID / AMD / AME / AMR -- "RID from <alias>" etc, plus NID
      body when present.
    * Verified NID / Verify NID Global / Verify NID Addressed --
      MTI name + NID or dest alias where relevant.
    * SNIP request/reply, PIP inquiry/reply, Traction Command/Reply
      -- MTI name + addressed dest alias when the payload starts with
      the addressed 2-byte prefix.
    * CID 1..4 -- sequence + NID slice.
    * Fallback: hex header + hex data.

    Kept as a pure function so tests can pass hand-crafted frames
    without spinning up a monitor."""
    ft = extract_frame_type(frame.header)
    if ft == FT_CONTROL:
        return _summarize_control(frame)
    if is_cid(frame.header):
        seq = cid_seq(frame.header)
        slice_bits = extract_variable_field(frame.header)
        return f"CID {seq} slice={slice_bits:#05x}"
    if ft == FT_MTI:
        return _summarize_mti(frame)
    return f"frame_type={ft:#x} data={frame.data.hex().upper() or '(empty)'}"


def _summarize_control(frame: OlcbFrame) -> str:
    var = extract_variable_field(frame.header)
    if var == CTRL_RID:
        return "RID (alias reserve)"
    if var == CTRL_AMD:
        if len(frame.data) >= 6:
            nid = ".".join(f"{b:02X}" for b in frame.data[:6])
            return f"AMD nid={nid}"
        return "AMD (short body)"
    if var == CTRL_AME:
        if len(frame.data) >= 6:
            nid = ".".join(f"{b:02X}" for b in frame.data[:6])
            return f"AME target-nid={nid}"
        return "AME (global)"
    if var == CTRL_AMR:
        if len(frame.data) >= 6:
            nid = ".".join(f"{b:02X}" for b in frame.data[:6])
            return f"AMR nid={nid}"
        return "AMR"
    return f"control var={var:#05x}"


# Well-known MTI names -- a UI-friendly rendering for the frames
# our layer knows about. Anything not here shows as "MTI 0xNNN".
_MTI_NAMES: dict[int, str] = {
    MTI_PCER: "PCER",
    0x0170: "Verified NID",
    0x0488: "Verify NID Addressed",
    0x0490: "Verify NID Global",
    0x0544: "Producer Identified Valid",
    0x0545: "Producer Identified Invalid",
    0x0547: "Producer Identified Unknown",
    0x04C4: "Consumer Identified Valid",
    0x04C5: "Consumer Identified Invalid",
    0x04C7: "Consumer Identified Unknown",
    0x0668: "PIP Reply",
    0x0828: "PIP Inquiry",
    0x08F4: "Identify Consumers",
    0x0914: "Identify Producers",
    0x0968: "Identify Events Addressed",
    0x0970: "Identify Events Global",
    0x0A08: "SNIP Reply",
    0x0DE8: "SNIP Request",
    0x01E9: "Traction Reply",
    0x05EB: "Traction Command",
}


def _summarize_mti(frame: OlcbFrame) -> str:
    mti = extract_mti(frame.header)
    name = _MTI_NAMES.get(mti, f"MTI {mti:#05x}")
    # PCER: 8-byte event ID. Append the S-9.7.3 well-known name
    # when the event ID is reserved (Emergency Off, Emergency Stop,
    # etc.) -- much friendlier for an operator scanning the log than
    # raw hex.
    if mti == MTI_PCER and len(frame.data) == 8:
        ev_str = ".".join(f"{b:02X}" for b in frame.data)
        ev_id = EventId(int.from_bytes(frame.data, "big"))
        name = well_known_name(ev_id)
        if name is not None:
            return f"PCER event={ev_str} ({name})"
        return f"PCER event={ev_str}"
    # Verified NID: 6-byte NID.
    if mti == 0x0170 and len(frame.data) >= 6:
        nid = ".".join(f"{b:02X}" for b in frame.data[:6])
        return f"Verified NID nid={nid}"
    # Addressed messages have a 2-byte flag+dest-alias prefix. When
    # we recognise the MTI as addressed, surface the dest alias so
    # the UI can see who the frame targets.
    if _is_addressed_mti(mti) and len(frame.data) >= 2:
        prefix = int.from_bytes(frame.data[:2], "big")
        dest = prefix & 0xFFF
        return f"{name} -> {dest:#05x}"
    return f"{name} data={frame.data.hex().upper() or '(empty)'}"


def _is_addressed_mti(mti: int) -> bool:
    """Bit 3 of the 12-bit MTI signals addressed messages per
    S-9.7.0.4 sec.5.4."""
    return bool(mti & 0x008)


# ---- structured drill-down decoder --------------------------------------


# Frame-type nibbles -> human names for the drill-down "kind" field.
# Covers everything the codec assigns a value to.
_FRAME_TYPE_NAMES: dict[int, str] = {
    FT_CONTROL: "Control",
    0x4: "CID (bits 11..0)",
    0x5: "CID (bits 23..12)",
    0x6: "CID (bits 35..24)",
    0x7: "CID (bits 47..36)",
    FT_MTI: "OpenLCB MTI",
    0xA: "Datagram Complete",
    0xB: "Datagram First",
    0xC: "Datagram Middle",
    0xD: "Datagram Final",
    0xF: "Stream Data",
}


def decode_frame_details(frame: OlcbFrame) -> dict[str, object]:
    """Break a frame into a structured dict for the monitor's
    drill-down view. Every entry has ``kind`` (human name of the
    frame-type nibble) and ``source_alias``; more specific fields
    layer on based on the frame kind:

    * Control (RID / AMD / AME / AMR): ``variable_field`` + a
      ``control_name`` + a decoded ``node_id`` when the body carries
      one.
    * CID 1..4: ``cid_seq`` (1..4) + ``nid_slice`` (12 bits).
    * MTI: ``mti_hex`` + ``mti_name``; when the payload carries an
      addressed prefix, ``dest_alias``; when it's a PCER, ``event_id``
      + optional ``event_name`` from the well-known catalog.
    * Stream / datagram fragments: ``dest_alias`` from the variable
      field where applicable.

    Payload always ends up in ``data_hex`` for the raw view.
    """
    ft = extract_frame_type(frame.header)
    source = extract_source_alias(frame.header)
    details: dict[str, object] = {
        "kind": _FRAME_TYPE_NAMES.get(ft, f"frame_type={ft:#x}"),
        "frame_type": ft,
        "source_alias": f"{source:#05x}",
        "header": f"{frame.header:#010x}",
        "data_hex": frame.data.hex().upper(),
    }
    if ft == FT_CONTROL:
        var = extract_variable_field(frame.header)
        details["variable_field"] = f"{var:#05x}"
        details["control_name"] = {
            CTRL_RID: "RID (alias reserve)",
            CTRL_AMD: "AMD (Alias Map Definition)",
            CTRL_AME: "AME (Alias Map Enquiry)",
            CTRL_AMR: "AMR (Alias Map Reset)",
        }.get(var, f"control {var:#05x}")
        if len(frame.data) >= 6:
            details["node_id"] = ".".join(
                f"{b:02X}" for b in frame.data[:6]
            )
        return details
    if is_cid(frame.header):
        details["cid_seq"] = cid_seq(frame.header)
        details["nid_slice"] = f"{extract_variable_field(frame.header):#05x}"
        return details
    if ft == FT_MTI:
        mti = extract_mti(frame.header)
        details["mti_hex"] = f"{mti:#05x}"
        details["mti_name"] = _MTI_NAMES.get(mti, f"MTI {mti:#05x}")
        details["addressed"] = _is_addressed_mti(mti)
        if mti == MTI_PCER and len(frame.data) == 8:
            ev = EventId(int.from_bytes(frame.data, "big"))
            details["event_id"] = str(ev)
            name = well_known_name(ev)
            if name is not None:
                details["event_name"] = name
        elif mti == 0x0170 and len(frame.data) >= 6:
            details["node_id"] = ".".join(
                f"{b:02X}" for b in frame.data[:6]
            )
        elif _is_addressed_mti(mti) and len(frame.data) >= 2:
            prefix = int.from_bytes(frame.data[:2], "big")
            details["dest_alias"] = f"{prefix & 0xFFF:#05x}"
        return details
    if ft in (0xA, 0xB, 0xC, 0xD):
        # Datagram frames use the variable field as the dest alias
        # per S-9.7.0.1 sec.6.2.
        details["dest_alias"] = f"{extract_variable_field(frame.header):#05x}"
        return details
    if ft == 0xF:
        # Stream Data -- variable field = dest alias, byte 0 = dest
        # stream ID (S-9.7.4.9 sec.3.1).
        details["dest_alias"] = f"{extract_variable_field(frame.header):#05x}"
        if len(frame.data) >= 1:
            details["dest_stream_id"] = f"{frame.data[0]:#04x}"
        return details
    return details


# ---- bus stats ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BusStats:
    """Aggregated view of the frames currently in the monitor ring.

    Everything is computed from a single pass over the buffer at
    query time -- no counters running in the background, no
    concurrency worries. On a busy bus with a 200-entry ring, a
    single aggregation walk is ~microseconds."""

    total_frames: int
    span_seconds: float
    oldest_timestamp: float | None
    newest_timestamp: float | None
    # (source_alias -> frame count), sorted descending by count
    top_talkers: list[tuple[int, int]]
    # (summary category -> frame count), sorted descending
    top_categories: list[tuple[str, int]]
    # (event_id_dotted -> frame count) from PCER frames only, sorted
    # descending. Empty when the buffer has no PCERs. Lets an
    # operator spot "which event is flooding the bus" -- a stuck
    # sensor or a chatty consist heartbeat surfaces immediately.
    top_events: list[tuple[str, int]]


def _category_of(entry: "MonitorEntry") -> str:
    """Group entries by their broad "kind" so the top-categories
    breakdown is useful. Uses the first word of the summary
    (``"PCER"``, ``"Verified"``, ``"CID"``, ``"AMD"``, etc.) since
    that's already been normalised by :func:`summarize_frame`. For
    unknown MTIs we group under ``"MTI 0xNNN"`` verbatim."""
    if not entry.summary:
        return f"frame_type={entry.frame_type:#x}"
    # Trim to the first "field=" boundary so PCERs don't split by
    # event ID and traction commands don't split by dest alias.
    first_field = entry.summary.split(" ", 1)[0]
    return first_field


def compute_bus_stats(
    entries: list["MonitorEntry"],
    *,
    top_n: int = 10,
) -> BusStats:
    """Aggregate the given monitor entries into a :class:`BusStats`.

    Pure function so a JSON layer can pass in any list (usually
    ``monitor.recent()``). *top_n* bounds the length of both the
    top-talkers and top-categories lists -- a UI rarely wants more
    than a handful."""
    if not entries:
        return BusStats(
            total_frames=0,
            span_seconds=0.0,
            oldest_timestamp=None,
            newest_timestamp=None,
            top_talkers=[],
            top_categories=[],
            top_events=[],
        )
    from collections import Counter  # noqa: PLC0415
    stamps = [e.timestamp for e in entries]
    oldest = min(stamps)
    newest = max(stamps)
    talkers = Counter(e.source_alias for e in entries)
    categories = Counter(_category_of(e) for e in entries)
    # PCER frames carry an 8-byte event ID as their data. Group by
    # dotted-hex form (matches how the rest of the API renders event
    # IDs) so a UI can look them up in the well-known catalog.
    events: Counter[str] = Counter()
    for e in entries:
        if e.summary.startswith("PCER") and len(e.data_hex) == 16:
            dotted = ".".join(
                e.data_hex[i:i + 2] for i in range(0, 16, 2)
            )
            events[dotted] += 1
    return BusStats(
        total_frames=len(entries),
        span_seconds=newest - oldest,
        oldest_timestamp=oldest,
        newest_timestamp=newest,
        top_talkers=talkers.most_common(top_n),
        top_categories=categories.most_common(top_n),
        top_events=events.most_common(top_n),
    )
