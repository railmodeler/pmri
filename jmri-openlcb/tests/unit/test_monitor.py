"""FrameMonitor + summarize_frame tests."""
from __future__ import annotations

import asyncio

import pytest

from jmri_openlcb import (
    FrameMonitor,
    MonitorEntry,
    OlcbTrafficController,
    loopback_pair,
    summarize_frame,
)
from jmri_openlcb.codec import (
    OlcbFrame,
    _pack_header,
    make_amd_header,
    make_ame_header,
    make_amr_header,
    make_cid_header,
    make_pcer_header,
    make_rid_header,
)


# ---- summarize_frame ---------------------------------------------------


def test_summarize_pcer_shows_event_id() -> None:
    frame = OlcbFrame(
        header=make_pcer_header(0x123),
        data=b"\x05\x01\x01\x01\x4C\x43\x00\x01",
    )
    got = summarize_frame(frame)
    assert "PCER" in got
    assert "05.01.01.01.4C.43.00.01" in got


def test_summarize_pcer_appends_well_known_event_name() -> None:
    """Reserved event IDs from S-9.7.3 (Emergency Off, etc.) show
    up as a parenthesised name after the hex -- makes the monitor
    log much easier to scan than raw hex."""
    frame = OlcbFrame(
        header=make_pcer_header(0x123),
        # 01.00.00.00.00.00.FF.FE = Emergency Off.
        data=b"\x01\x00\x00\x00\x00\x00\xFF\xFE",
    )
    got = summarize_frame(frame)
    assert "Emergency Off" in got
    assert "01.00.00.00.00.00.FF.FE" in got


def test_decode_details_pcer_carries_event_id_and_well_known_name() -> None:
    from jmri_openlcb import decode_frame_details  # noqa: PLC0415
    frame = OlcbFrame(
        header=make_pcer_header(0x123),
        data=b"\x01\x00\x00\x00\x00\x00\xFF\xFE",  # Emergency Off
    )
    d = decode_frame_details(frame)
    assert d["kind"] == "OpenLCB MTI"
    assert d["source_alias"] == "0x123"
    assert d["mti_name"] == "PCER"
    assert d["event_id"] == "01.00.00.00.00.00.FF.FE"
    assert d["event_name"] == "Emergency Off"


def test_decode_details_control_amd_extracts_node_id() -> None:
    from jmri_openlcb import decode_frame_details  # noqa: PLC0415
    frame = OlcbFrame(
        header=make_amd_header(0x123),
        data=bytes.fromhex("020112FEDCBA"),
    )
    d = decode_frame_details(frame)
    assert d["kind"] == "Control"
    assert d["control_name"].startswith("AMD")
    assert d["node_id"] == "02.01.12.FE.DC.BA"


def test_decode_details_cid_exposes_sequence_and_slice() -> None:
    from jmri_openlcb import decode_frame_details  # noqa: PLC0415
    frame = OlcbFrame(
        header=make_cid_header(2, 0xABC, 0x123),
        data=b"",
    )
    d = decode_frame_details(frame)
    assert "CID" in d["kind"]
    assert d["cid_seq"] == 2
    assert d["nid_slice"] == "0xabc"


def test_decode_details_addressed_mti_surfaces_dest_alias() -> None:
    from jmri_openlcb import decode_frame_details  # noqa: PLC0415
    from jmri_openlcb.codec import FT_MTI, _pack_header  # noqa: PLC0415
    frame = OlcbFrame(
        header=_pack_header(FT_MTI, 0x0A08, 0x111),   # SNIP Reply
        data=bytes([0x00, 0x22]) + b"payload",
    )
    d = decode_frame_details(frame)
    assert d["addressed"] is True
    assert d["dest_alias"] == "0x022"
    assert d["mti_name"] == "SNIP Reply"


def test_decode_details_datagram_frame_extracts_dest_alias() -> None:
    from jmri_openlcb import decode_frame_details  # noqa: PLC0415
    from jmri_openlcb.codec import _pack_header  # noqa: PLC0415
    # Datagram Complete (0xA) frame from 0x111 to 0x222.
    frame = OlcbFrame(
        header=_pack_header(0xA, 0x222, 0x111),
        data=bytes([0x20, 0x82]),
    )
    d = decode_frame_details(frame)
    assert d["kind"] == "Datagram Complete"
    assert d["dest_alias"] == "0x222"


def test_summarize_pcer_unnamed_event_has_no_paren() -> None:
    """A random project-space event ID stays hex-only -- we don't
    invent names."""
    frame = OlcbFrame(
        header=make_pcer_header(0x123),
        data=b"\x05\x01\x01\x01\x4C\x43\x00\x01",
    )
    got = summarize_frame(frame)
    assert "(" not in got


def test_summarize_rid_amd_ame_amr() -> None:
    """Control frames get named + NID surfaced when the body carries
    one (AMD/AME with data, AMR always)."""
    nid_bytes = bytes.fromhex("020112FEDCBA")
    nid_dotted = "02.01.12.FE.DC.BA"
    assert summarize_frame(OlcbFrame(
        header=make_rid_header(0x123), data=b"",
    )).startswith("RID")
    assert nid_dotted in summarize_frame(OlcbFrame(
        header=make_amd_header(0x123), data=nid_bytes,
    ))
    # Empty AME data -> global.
    assert "global" in summarize_frame(OlcbFrame(
        header=make_ame_header(0x123), data=b"",
    )).lower()
    # AME with data -> target NID.
    assert nid_dotted in summarize_frame(OlcbFrame(
        header=make_ame_header(0x123), data=nid_bytes,
    ))
    assert nid_dotted in summarize_frame(OlcbFrame(
        header=make_amr_header(0x123), data=nid_bytes,
    ))


def test_summarize_cid_reports_sequence_and_slice() -> None:
    frame = OlcbFrame(
        header=make_cid_header(2, 0xABC, 0x123),
        data=b"",
    )
    got = summarize_frame(frame)
    assert "CID 2" in got
    assert "0xabc" in got


def test_summarize_verify_nid_reply_shows_nid() -> None:
    """Verified NID (MTI 0x0170) with a 6-byte body -> NID surfaced."""
    from jmri_openlcb.codec import FT_MTI  # noqa: PLC0415
    header = _pack_header(FT_MTI, 0x0170, 0x123)
    frame = OlcbFrame(
        header=header, data=bytes.fromhex("020112FEDCBA"),
    )
    got = summarize_frame(frame)
    assert "Verified NID" in got
    assert "02.01.12.FE.DC.BA" in got


def test_summarize_addressed_mti_shows_dest_alias() -> None:
    """SNIP request / Traction command / etc. -> surface the dest
    alias from the addressed 2-byte prefix."""
    from jmri_openlcb.codec import FT_MTI  # noqa: PLC0415
    # SNIP request (0x0DE8) to alias 0x456.
    header = _pack_header(FT_MTI, 0x0DE8, 0x123)
    # Prefix = only-frame flag (0x00) + 12-bit dest alias.
    prefix = (0x456).to_bytes(2, "big")
    frame = OlcbFrame(header=header, data=prefix)
    got = summarize_frame(frame)
    assert "SNIP Request" in got
    assert "0x456" in got


def test_summarize_unknown_mti_falls_back_to_hex() -> None:
    """An MTI we don't have a name for is still summarised."""
    from jmri_openlcb.codec import FT_MTI  # noqa: PLC0415
    header = _pack_header(FT_MTI, 0x0ABC, 0x123)
    frame = OlcbFrame(header=header, data=b"\x11\x22")
    got = summarize_frame(frame)
    assert "0xabc" in got or "MTI" in got


# ---- FrameMonitor -----------------------------------------------------


async def _linked_tcs():
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    return tc_a, tc_b


async def test_monitor_captures_inbound_frames() -> None:
    tc_a, tc_b = await _linked_tcs()
    monitor = FrameMonitor(tc_a)
    try:
        # Peer emits a PCER we should capture.
        from jmri_openlcb import EventId  # noqa: PLC0415
        await tc_b.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
        await asyncio.sleep(0.05)
        entries = monitor.recent()
        # Just the peer's frame -- we don't send anything from tc_a.
        assert any(isinstance(e, MonitorEntry) for e in entries)
        assert any("PCER" in e.summary for e in entries)
    finally:
        monitor.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_monitor_ring_bounds_max_entries() -> None:
    """A busy bus fills the ring but old entries drop off -- the
    buffer never grows past max_entries."""
    tc_a, tc_b = await _linked_tcs()
    monitor = FrameMonitor(tc_a, max_entries=5)
    try:
        from jmri_openlcb import EventId  # noqa: PLC0415
        ev = EventId.parse("05.01.01.01.4C.43.00.01")
        for _ in range(20):
            await tc_b.produce_event(ev)
        await asyncio.sleep(0.05)
        assert len(monitor.recent()) == 5
    finally:
        monitor.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_monitor_pause_stops_capturing_new_frames() -> None:
    """After pause(), inbound frames are skipped so the ring stops
    growing. Frames already captured stay visible."""
    from jmri_openlcb import EventId  # noqa: PLC0415
    from jmri_openlcb.monitor import FrameMonitor  # noqa: PLC0415
    from jmri_openlcb.traffic import OlcbTrafficController  # noqa: PLC0415
    from jmri_openlcb.transport import loopback_pair  # noqa: PLC0415

    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=0x111)
    tc_b = OlcbTrafficController(b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    mon = FrameMonitor(tc_a)
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.01")
        await tc_b.produce_event(ev)
        await asyncio.sleep(0.02)
        assert len(mon.recent()) >= 1
        before = len(mon.recent())

        mon.pause()
        assert mon.paused is True
        await tc_b.produce_event(ev)
        await tc_b.produce_event(ev)
        await asyncio.sleep(0.02)
        assert len(mon.recent()) == before   # nothing new captured

        mon.resume()
        assert mon.paused is False
        await tc_b.produce_event(ev)
        await asyncio.sleep(0.02)
        assert len(mon.recent()) == before + 1
    finally:
        mon.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_monitor_clear_empties_buffer() -> None:
    tc_a, tc_b = await _linked_tcs()
    monitor = FrameMonitor(tc_a)
    try:
        from jmri_openlcb import EventId  # noqa: PLC0415
        await tc_b.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
        await asyncio.sleep(0.05)
        assert len(monitor.recent()) >= 1
        monitor.clear()
        assert monitor.recent() == []
    finally:
        monitor.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_monitor_close_stops_capturing() -> None:
    tc_a, tc_b = await _linked_tcs()
    monitor = FrameMonitor(tc_a)
    try:
        from jmri_openlcb import EventId  # noqa: PLC0415
        monitor.close()
        await tc_b.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
        await asyncio.sleep(0.05)
        # Nothing captured after close.
        assert monitor.recent() == []
    finally:
        await tc_a.aclose()
        await tc_b.aclose()


# ---- OlcbConnection wiring ---------------------------------------------


async def test_connection_exposes_frame_monitor_by_default() -> None:
    from jmri_openlcb import OlcbConnection  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        # Skip the boot PCER (S-9.7.3 Init Complete) so the "empty
        # buffer on boot" assertion still holds -- the emission is
        # covered by its own tests below.
        "skip_init_complete": True,
    })
    try:
        assert conn.frame_monitor is not None
        # Starts empty.
        assert conn.frame_monitor.recent() == []
    finally:
        await conn.close()


# ---- compute_bus_stats -------------------------------------------------


def _make_entry(
    ts: float, source: int, summary: str = "PCER",
    data_hex: str = "",
) -> MonitorEntry:
    return MonitorEntry(
        timestamp=ts, header=0, source_alias=source, frame_type=0x9,
        summary=summary, data_hex=data_hex,
    )


def test_compute_bus_stats_on_empty_list_returns_zeroes() -> None:
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    stats = compute_bus_stats([])
    assert stats.total_frames == 0
    assert stats.span_seconds == 0.0
    assert stats.oldest_timestamp is None
    assert stats.newest_timestamp is None
    assert stats.top_talkers == []
    assert stats.top_categories == []


def test_compute_bus_stats_reports_totals_and_span() -> None:
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    entries = [
        _make_entry(100.0, 0x111),
        _make_entry(101.5, 0x111),
        _make_entry(102.25, 0x222),
    ]
    stats = compute_bus_stats(entries)
    assert stats.total_frames == 3
    assert abs(stats.span_seconds - 2.25) < 1e-6
    assert stats.oldest_timestamp == 100.0
    assert stats.newest_timestamp == 102.25


def test_top_talkers_sorted_by_count_descending() -> None:
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    entries = (
        [_make_entry(1.0 + i, 0x111) for i in range(5)]
        + [_make_entry(10.0 + i, 0x222) for i in range(3)]
        + [_make_entry(20.0 + i, 0x333) for i in range(7)]
    )
    stats = compute_bus_stats(entries)
    # Top three, ordered by count descending: 0x333 (7), 0x111 (5), 0x222 (3).
    assert stats.top_talkers == [(0x333, 7), (0x111, 5), (0x222, 3)]


def test_top_talkers_honors_top_n_bound() -> None:
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    entries = [_make_entry(float(i), i) for i in range(20)]
    stats = compute_bus_stats(entries, top_n=5)
    assert len(stats.top_talkers) == 5


def test_top_categories_groups_by_summary_prefix() -> None:
    """Category = first word of the summary -- so PCERs cluster
    regardless of event ID and traction commands cluster regardless
    of dest alias."""
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    entries = [
        _make_entry(1.0, 0x111, "PCER event=05.01.01.01.4C.43.00.01"),
        _make_entry(2.0, 0x111, "PCER event=05.01.01.01.4C.43.00.02"),
        _make_entry(3.0, 0x222, "Verified NID nid=02.02.02.02.02.02"),
        _make_entry(4.0, 0x333, "AMD nid=02.03.03.03.03.03"),
        _make_entry(5.0, 0x333, "AMD nid=02.04.04.04.04.04"),
        _make_entry(6.0, 0x333, "AMD nid=02.05.05.05.05.05"),
    ]
    stats = compute_bus_stats(entries)
    # AMD (3) > PCER (2) > Verified (1).
    assert stats.top_categories == [("AMD", 3), ("PCER", 2), ("Verified", 1)]


def test_top_events_counts_pcer_frames_by_event_id() -> None:
    """PCER frames get grouped by their 8-byte data payload rendered
    as dotted hex. Non-PCER frames (Verified NID, AMD...) don't
    contribute to the counter."""
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    ev_a = "05.01.01.01.4C.43.00.01"
    ev_b = "01.00.00.00.00.00.FF.FE"
    entries = [
        _make_entry(1.0, 0x111, "PCER event=...", ev_a.replace(".", "")),
        _make_entry(2.0, 0x111, "PCER event=...", ev_a.replace(".", "")),
        _make_entry(3.0, 0x111, "PCER event=...", ev_a.replace(".", "")),
        _make_entry(4.0, 0x222, "PCER event=...", ev_b.replace(".", "")),
        _make_entry(5.0, 0x333, "Verified NID nid=02.02.02.02.02.02",
                    "020202020202"),
    ]
    stats = compute_bus_stats(entries)
    assert stats.top_events == [(ev_a, 3), (ev_b, 1)]


def test_top_events_empty_when_no_pcer_frames() -> None:
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    entries = [_make_entry(1.0, 0x111, "AMD nid=02.01.12.FE.DC.BA",
                           "020112FEDCBA")]
    stats = compute_bus_stats(entries)
    assert stats.top_events == []


def test_compute_bus_stats_uses_frame_type_for_empty_summary() -> None:
    """A monitor entry with a blank summary still gets a stable
    category label based on the frame type."""
    from jmri_openlcb import compute_bus_stats  # noqa: PLC0415
    entries = [_make_entry(1.0, 0x111, summary="")]
    stats = compute_bus_stats(entries)
    assert stats.top_categories == [("frame_type=0x9", 1)]


async def test_skip_monitor_setting_disables_it() -> None:
    from jmri_openlcb import OlcbConnection  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_monitor": True,
    })
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            _ = conn.frame_monitor
    finally:
        await conn.close()
