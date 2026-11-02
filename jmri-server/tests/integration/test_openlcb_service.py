"""OpenLcbService / OpenLcbCSService -- JSON topic surface tests.

Uses a real :class:`~jmri_openlcb.OlcbConnection` built with
loopback + skip_alias_claim so the JSON layer can be exercised
without a live bus. For probes that need a peer, tests wire in a
minimal responder on the other loopback side.

Uses ``httpx.AsyncClient`` with an ASGI transport rather than
``fastapi.testclient.TestClient`` because the async peer + loopback
transport need to share the pytest asyncio loop -- ``TestClient``
runs the app on a separate portal thread and would race with the
peer's frame delivery.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from jmri_openlcb import (
    NodeId,
    OlcbConnection,
    SnipResponder,
    NodeInfo,
    PipService,
    ProtocolFlag,
    VerifyNodeIdService,
    loopback_pair,
)
from jmri_openlcb import OlcbTrafficController
from jmri_openlcb.command_station import CommandStationNode
from jmri_openlcb.event_ident import ProducerConsumerService, EventState
from jmri_openlcb.events import EventId
from jmri_core.beans.throttle_manager import ThrottleManager

from jmri_server.app import build_app


@pytest.fixture()
async def olcb_handle():
    """Build a server whose connection is a real OpenLCB one, and
    stand up a peer traffic controller on the other end of the
    loopback so we can inject responses."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
    })
    handle = build_app(connection=conn)
    peer = OlcbTrafficController(b, source_alias=0x222)
    await peer.start()
    try:
        yield handle, peer, conn
    finally:
        await peer.aclose()
        await conn.close()


@pytest.fixture()
async def olcb_client(olcb_handle):
    handle, peer, conn = olcb_handle
    transport = httpx.ASGITransport(app=handle.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as tc:
        yield tc, peer, conn, handle


# ---- topic routing: driver-detection --------------------------------------


def test_openlcb_topic_400s_on_non_olcb_connection() -> None:
    """A stock build_app() gets an InternalConnection -- every
    openlcb.* verb should surface a friendly 400 rather than
    crashing."""
    handle = build_app()
    with TestClient(handle.app) as tc:
        for verb in ("list", "get", "post"):
            r = tc.post("/json/", json={
                "type": "openlcb",
                "method": verb,
                "data": {"alias": "0x222", "action": "discover"},
            })
            assert r.status_code == 400
            assert "not OpenLCB" in r.json()["detail"]


# ---- openlcb.list -------------------------------------------------------


async def test_list_empty_on_fresh_connection(olcb_client) -> None:
    """No nodes discovered yet -> empty list, no error."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={"type": "openlcb", "method": "list"})
    r.raise_for_status()
    assert r.json()["data"] == []


async def test_list_includes_verified_and_p_c_advertised_nodes(olcb_client) -> None:
    """Nodes populate the list via either the NID lookup (Verified
    NID heard on the bus) OR the event roster (P/C Identified) --
    the ``list`` verb unions both."""
    tc, peer, conn, handle = olcb_client
    # Peer advertises its NID via Verified NID.
    peer_svc = VerifyNodeIdService(peer, NodeId.parse("02.02.02.02.02.02"))
    # And produces one event.
    peer_pc = ProducerConsumerService(peer)
    peer_pc.register_produced(
        EventId.parse("05.01.01.01.4C.43.00.01"), EventState.VALID,
    )
    try:
        # Trigger discovery so the peer replies.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "discover"},
        })
        r.raise_for_status()
        # Give the loopback delivery + reply chain a couple of ticks.
        import asyncio  # noqa: PLC0415
        await asyncio.sleep(0.05)

        r = await tc.post("/json/", json={"type": "openlcb", "method": "list"})
        r.raise_for_status()
        data = r.json()["data"]
        assert len(data) == 1
        node = data[0]
        assert node["alias"] == "0x222"
        assert node["node_id"] == "02.02.02.02.02.02"
        assert node["produced_events"] == ["05.01.01.01.4C.43.00.01"]
        assert node["consumed_events"] == []
        # last_seen is populated because the peer's Verified NID
        # reply just landed on the tc, so seconds_ago is small.
        assert node["last_seen"] is not None
        assert node["seconds_ago"] is not None
        assert 0 <= node["seconds_ago"] < 5
    finally:
        peer_pc.close()
        peer_svc.close()


# ---- openlcb.get -------------------------------------------------------


async def test_get_returns_projection_for_known_alias(olcb_client) -> None:
    tc, peer, conn, handle = olcb_client
    # Pre-populate the NID lookup by sending a Verified NID.
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    peer_nid = NodeId.parse("02.02.02.02.02.02")
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=peer_nid.bytes,
    ))
    await asyncio.sleep(0)

    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "get",
        "data": {"alias": "0x222"},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert data["alias"] == "0x222"
    assert data["node_id"] == "02.02.02.02.02.02"


async def test_get_returns_none_node_id_for_unheard_alias(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "get",
        "data": {"alias": "0x999"},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert data["alias"] == "0x999"
    assert data["node_id"] is None


async def test_get_requires_alias(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={"type": "openlcb", "method": "get"})
    assert r.status_code == 400
    assert "alias" in r.json()["detail"]


async def test_get_accepts_int_alias(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "get",
        "data": {"alias": 0x222},
    })
    r.raise_for_status()
    assert r.json()["data"]["alias"] == "0x222"


async def test_get_rejects_out_of_range_alias(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "get",
        "data": {"alias": 0x1000},
    })
    assert r.status_code == 400


# ---- openlcb.post -- probe_snip -----------------------------------------


async def test_probe_snip_returns_node_info(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    info = NodeInfo(
        manufacturer="Acme", model="Widget",
        hardware_version="rev A", software_version="1.2",
        user_name="the-node", user_description="on the bus",
    )
    responder = SnipResponder(peer, info)
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "probe_snip", "alias": "0x222", "timeout": 1.0},
        })
        r.raise_for_status()
        data = r.json()["data"]
        assert data["manufacturer"] == "Acme"
        assert data["model"] == "Widget"
        assert data["hardware_version"] == "rev A"
        assert data["software_version"] == "1.2"
        assert data["user_name"] == "the-node"
        assert data["user_description"] == "on the bus"
    finally:
        responder.close()


async def test_recent_frames_returns_buffered_entries(olcb_client) -> None:
    """The peer emits a PCER; the connection's frame monitor
    captures it; recent_frames returns it with a decoded summary."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert data
    # Every entry has the projected keys.
    for entry in data:
        assert "timestamp" in entry
        assert "header" in entry
        assert "source_alias" in entry
        assert "summary" in entry
    # At least one entry summarizes the PCER.
    assert any("PCER" in e["summary"] for e in data)


async def test_recent_frames_honors_limit(olcb_client) -> None:
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    for i in range(5):
        await peer.produce_event(EventId(0x0501010100000000 | i))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames", "limit": 2},
    })
    r.raise_for_status()
    assert len(r.json()["data"]) == 2


async def test_recent_frames_is_newest_first(olcb_client) -> None:
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    for i in range(3):
        await peer.produce_event(EventId(0x0501010100000000 | i))
        await asyncio.sleep(0.01)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    data = r.json()["data"]
    # Timestamps monotonically decrease -> newest at index 0.
    stamps = [e["timestamp"] for e in data]
    assert stamps == sorted(stamps, reverse=True)


async def test_clear_frames_empties_the_buffer(olcb_client) -> None:
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "clear_frames"},
    })
    r.raise_for_status()
    assert r.json()["data"]["cleared"] is True
    # And the next recent_frames call is empty.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    assert r.json()["data"] == []


async def test_send_frame_puts_frame_on_the_wire(olcb_client) -> None:
    """The send_frame action writes a raw frame to the transport;
    the peer's traffic controller receives it."""
    tc, peer, conn, _ = olcb_client
    import asyncio  # noqa: PLC0415
    received: list = []
    peer.frames.connect(received.append)
    # A hand-crafted PCER header + 8-byte event ID.
    header_hex = "195B4123"
    data_hex = "0501010100000042"
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_frame",
            "header": header_hex,
            "data": data_hex,
        },
    })
    r.raise_for_status()
    assert r.json()["data"]["sent"] is True
    # Give the loopback delivery a tick.
    await asyncio.sleep(0.05)
    assert any(
        f.header == 0x195B4123 and f.data == bytes.fromhex(data_hex)
        for f in received
    )


async def test_send_frame_accepts_prefixed_hex_and_zero_data(
    olcb_client,
) -> None:
    """0x-prefixed hex is accepted, and an empty data field sends a
    zero-body frame -- useful for control frames like RID."""
    tc, peer, conn, _ = olcb_client
    import asyncio  # noqa: PLC0415
    received: list = []
    peer.frames.connect(received.append)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_frame",
            "header": "0x10700123",  # RID for alias 0x123
            "data": "",
        },
    })
    r.raise_for_status()
    await asyncio.sleep(0.05)
    assert any(
        f.header == 0x10700123 and f.data == b"" for f in received
    )


async def test_send_frame_rejects_bad_hex(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    # Missing header.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame"},
    })
    assert r.status_code == 400
    # Non-hex header.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame", "header": "not-hex"},
    })
    assert r.status_code == 400
    # Non-hex data.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_frame",
            "header": "195B4123", "data": "zzzz",
        },
    })
    assert r.status_code == 400


async def test_send_frame_rejects_over_29_bit_header(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_frame",
            "header": "F0000000",   # 32 bits set
            "data": "",
        },
    })
    assert r.status_code == 400


async def test_send_frame_presets_use_current_source_alias(olcb_client) -> None:
    """Presets embed our current source alias so an operator's
    click-to-send lands on a valid CAN header without editing."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame_presets"},
    })
    r.raise_for_status()
    presets = r.json()["data"]
    # Four canonical presets in a known order.
    names = [p["name"] for p in presets]
    assert "PCER (example event)" in names
    assert "Verify NID Global" in names
    assert "Identify Events Global" in names
    assert "AME (global)" in names
    # Fixture uses source_alias=0x111. Every preset header ends in
    # ...111 (the low 12 bits) because that's the alias slot.
    for p in presets:
        header_int = int(p["header"], 16)
        assert (header_int & 0xFFF) == 0x111


async def test_send_frame_presets_include_well_known_emergencies(olcb_client) -> None:
    """The Emergency Off / Clear + Emergency Stop All Trains / Clear
    presets carry the reserved event IDs from S-9.7.3 verbatim.
    Wrong bytes here would silently ship a broken emergency stop."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame_presets"},
    })
    r.raise_for_status()
    by_name = {p["name"]: p for p in r.json()["data"]}
    assert by_name["Emergency Off"]["data"].upper() == "010000000000FFFE"
    assert by_name["Clear Emergency Off"]["data"].upper() == "010000000000FFFF"
    assert (
        by_name["Emergency Stop of All Trains"]["data"].upper()
        == "010000000000FFFD"
    )
    assert (
        by_name["Clear Emergency Stop of All Trains"]["data"].upper()
        == "010000000000FFFC"
    )


async def test_recent_frames_carries_decode_details(olcb_client) -> None:
    """Each frame row in the recent_frames projection now includes
    a structured ``details`` dict with kind, source_alias, and
    frame-kind-specific fields (event_id for PCER, node_id for AMD,
    etc.). Powers the monitor's click-to-expand drill-down."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    # Emergency Off PCER -- should get event_name populated too.
    await peer.produce_event(EventId.parse("01.00.00.00.00.00.FF.FE"))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    rows = r.json()["data"]
    pcer = next(r for r in rows if "PCER" in r["summary"])
    d = pcer["details"]
    assert d["kind"] == "OpenLCB MTI"
    assert d["mti_name"] == "PCER"
    assert d["event_id"] == "01.00.00.00.00.00.FF.FE"
    assert d["event_name"] == "Emergency Off"
    assert d["source_alias"] == "0x222"


async def test_well_known_events_action_returns_catalog(olcb_client) -> None:
    """The well_known_events action lists the S-9.7.3 catalog as
    ``{event, name}`` entries, sorted by event ID for deterministic
    display order."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "well_known_events"},
    })
    r.raise_for_status()
    catalog = r.json()["data"]
    # Spot-check some canonical entries exist.
    names = {row["name"]: row["event"] for row in catalog}
    assert names.get("Emergency Off") == "01.00.00.00.00.00.FF.FE"
    assert names.get("Duplicate Node ID Detected") == (
        "01.01.00.00.00.00.03.03"
    )
    assert names.get("Firmware Corrupted") == "01.01.00.00.00.00.06.01"
    # Sort order is by event ID value -- Emergency Off (01.00...FF.FE)
    # comes before the 01.01.xx.xx.xx.xx.xx.xx family.
    events = [row["event"] for row in catalog]
    assert events == sorted(events)


async def test_send_frame_history_starts_empty(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame_history"},
    })
    r.raise_for_status()
    assert r.json()["data"] == []


async def test_send_frame_appends_to_history_newest_first(olcb_client) -> None:
    """Each send_frame call inserts an entry at the head; oldest
    drops off after SEND_HISTORY_MAX entries."""
    tc, _, _, _ = olcb_client
    # Send three distinct frames.
    for i, header in enumerate(["0x195B4111", "0x1949B111", "0x10701111"]):
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "send_frame",
                "header": header,
                "data": f"0{i}",
            },
        })
        r.raise_for_status()
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame_history"},
    })
    r.raise_for_status()
    hist = r.json()["data"]
    assert len(hist) == 3
    # Newest first -- third send is at index 0.
    assert hist[0]["header"] == "0x10701111"
    assert hist[2]["header"] == "0x195b4111"
    # Every entry carries a sent_at timestamp.
    assert all("sent_at" in e for e in hist)


async def test_send_frame_dedupes_consecutive_identical_sends(olcb_client) -> None:
    """A spammed resend button shouldn't crowd earlier distinct
    entries off the ring."""
    tc, _, _, _ = olcb_client
    for _ in range(5):
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "send_frame",
                "header": "0x195B4111",
                "data": "00",
            },
        })
        r.raise_for_status()
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "send_frame_history"},
    })
    r.raise_for_status()
    assert len(r.json()["data"]) == 1


async def test_send_datagram_delivers_to_peer(olcb_client) -> None:
    """The send_datagram action delivers a hand-crafted payload to
    the peer and reports the byte count on Received OK."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb.datagrams import Datagram, DatagramEngine  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    peer_engine = DatagramEngine(peer)
    await peer_engine.attach()
    received: list[Datagram] = []
    peer_engine.inbound.connect(lambda dg: received.append(dg))
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "send_datagram",
                "alias": "0x222",
                "data": "20 82",   # memory-config get-options
                "timeout": 1.0,
            },
        })
        r.raise_for_status()
        result = r.json()["data"]
        assert result["sent"] is True
        assert result["alias"] == "0x222"
        assert result["bytes"] == 2
        # Peer's engine saw the datagram exactly as sent.
        await asyncio.sleep(0.02)
        assert len(received) == 1
        assert received[0].data == bytes([0x20, 0x82])
    finally:
        await peer_engine.detach()


async def test_send_datagram_rejects_over_72_byte_payload(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_datagram",
            "alias": "0x222",
            "data": "00" * 73,
            "timeout": 1.0,
        },
    })
    assert r.status_code == 400
    assert "72" in r.text


async def test_send_datagram_surfaces_reject_description(olcb_client) -> None:
    """When the peer replies Datagram Rejected, the action returns
    rejected=True + the friendly description + hex code."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb.codec import OlcbFrame  # noqa: PLC0415
    from jmri_openlcb.datagrams import (  # noqa: PLC0415
        MTI_DATAGRAM_REJECTED,
        _encode_dest_prefix,
        make_reply_header,
    )
    import asyncio  # noqa: PLC0415

    async def _rejecter(frame: OlcbFrame) -> None:
        # Any datagram frame (kinds 0x2..0x5) from CT -> reject.
        if (frame.header >> 24) & 0xF not in (0x2, 0x3, 0x4, 0x5):
            return
        # Permanent, address space unknown (0x1082 in our taxonomy).
        await peer.send_frame(OlcbFrame(
            header=make_reply_header(MTI_DATAGRAM_REJECTED, 0x222),
            data=_encode_dest_prefix(0x111) + bytes([0x10, 0x82]),
        ))

    peer.frames.connect(_rejecter)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_datagram",
            "alias": "0x222",
            "data": "20 82",
            "timeout": 1.0,
        },
    })
    r.raise_for_status()
    result = r.json()["data"]
    assert result["sent"] is False
    assert result["rejected"] is True
    assert result["error_code"] == "0x1082"
    assert "Address space unknown" in result["description"]


async def test_send_frame_rejects_over_8_byte_data(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_frame",
            "header": "195B4123",
            "data": "00" * 10,   # 10 bytes
        },
    })
    assert r.status_code == 400


async def test_bus_stats_summarizes_the_ring_buffer(olcb_client) -> None:
    """Peer emits a couple of PCERs from different aliases; bus_stats
    returns total counts + top talkers with the right aliases."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    # Two PCERs from the peer at 0x222.
    for i in range(2):
        await peer.produce_event(EventId(0x0501010100000000 | i))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats"},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert data["total_frames"] >= 2
    assert data["oldest_timestamp"] is not None
    assert data["newest_timestamp"] is not None
    # Peer at 0x222 shows up in the top-talkers list.
    talker_aliases = {row["alias"] for row in data["top_talkers"]}
    assert "0x222" in talker_aliases
    # PCER shows up as a category.
    categories = {row["category"] for row in data["top_categories"]}
    assert "PCER" in categories


async def test_bus_stats_on_empty_buffer_returns_zeros(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    # No frames yet + clear to be safe.
    await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "clear_frames"},
    })
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats"},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert data["total_frames"] == 0
    assert data["top_talkers"] == []
    assert data["top_categories"] == []


async def test_bus_stats_honors_top_n_bound(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    for i in range(5):
        await peer.produce_event(EventId(0x0501010100000000 | i))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats", "top_n": 1},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert len(data["top_talkers"]) <= 1
    assert len(data["top_categories"]) <= 1


async def test_probe_all_populates_snip_and_pip_caches(olcb_client) -> None:
    """probe_all sweeps every known alias in parallel + caches SNIP
    and PIP replies. Subsequent list calls include the cached data
    inline so the discovery UI shows manufacturer/model + capability
    badges without per-node clicks."""
    tc, peer, conn, _ = olcb_client
    info = NodeInfo(
        manufacturer="Bus Peer Inc", model="RailMonitor",
        hardware_version="rev 2", software_version="3.4",
        user_name="rm-north", user_description="north yard",
    )
    snip_svc = SnipResponder(peer, info)
    pip_svc = PipService(
        peer, ProtocolFlag.DATAGRAM | ProtocolFlag.CDI,
    )
    try:
        # Make the peer discoverable -- send a Verified NID
        # unsolicited so the roster picks it up.
        from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
        from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
        import asyncio  # noqa: PLC0415
        await peer.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=NodeId.parse("02.02.02.02.02.02").bytes,
        ))
        await asyncio.sleep(0)
        # Now probe every alias.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "probe_all", "timeout": 1.0},
        })
        r.raise_for_status()
        data = r.json()["data"]
        # Peer at 0x222 appears with SNIP + PIP populated.
        by_alias = {n["alias"]: n for n in data}
        assert "0x222" in by_alias
        peer_row = by_alias["0x222"]
        assert peer_row["snip"] is not None
        assert peer_row["snip"]["manufacturer"] == "Bus Peer Inc"
        assert peer_row["pip"] is not None
        assert "DATAGRAM" in peer_row["pip"]["names"]
        assert "CDI" in peer_row["pip"]["names"]
        # And a subsequent plain list also carries the cached
        # results without re-probing.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "list",
        })
        r.raise_for_status()
        by_alias = {n["alias"]: n for n in r.json()["data"]}
        assert by_alias["0x222"]["snip"]["manufacturer"] == "Bus Peer Inc"
        assert "DATAGRAM" in by_alias["0x222"]["pip"]["names"]
    finally:
        pip_svc.close()
        snip_svc.close()


async def test_list_projects_range_identified_events(olcb_client) -> None:
    """A peer emits Producer Range Identified -> the JSON topic
    projects it into produced_ranges as [start, end] event ID
    strings. Boards that advertise 256 events as a single range
    would otherwise look empty to a JMRI operator."""
    tc, peer, conn, handle = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.event_ident import (  # noqa: PLC0415
        MTI_PRODUCER_RANGE_IDENTIFIED,
    )
    import asyncio  # noqa: PLC0415
    # Range: 05.01.01.01.4C.43.00.F0 (bit 0 = 0, 4 trailing zeros)
    # covers F0..FF -- 16 events.
    ev = EventId.parse("05.01.01.01.4C.43.00.F0")
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_PRODUCER_RANGE_IDENTIFIED, 0x222),
        data=ev.bytes,
    ))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={"type": "openlcb", "method": "list"})
    r.raise_for_status()
    data = r.json()["data"]
    by_alias = {n["alias"]: n for n in data}
    assert "0x222" in by_alias
    assert by_alias["0x222"]["produced_ranges"] == [
        ["05.01.01.01.4C.43.00.F0", "05.01.01.01.4C.43.00.FF"],
    ]
    # Single-event map is empty -- no per-event advertisement.
    assert by_alias["0x222"]["produced_events"] == []


async def test_recent_frames_projection_carries_utc_time_str(olcb_client) -> None:
    """Each row's time_str is HH:MM:SS.mmm formatted from the raw
    timestamp so a UI doesn't need to reformat client-side. The raw
    ``timestamp`` field stays for CSV / machine parsing."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio, re  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    rows = r.json()["data"]
    assert rows
    row = rows[0]
    # Time string looks like 15:04:05.123 (24-hour HH:MM:SS.mmm).
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{3}", row["time_str"])
    # Raw timestamp still present for CSV / JSON clients.
    assert isinstance(row["timestamp"], float)


async def test_snip_get_returns_current_identity(olcb_client) -> None:
    """snip_get surfaces the six SNIP strings with the vendor-fixed
    fields set to the JMRI-Python defaults. user_name /
    user_description may or may not be empty depending on whether an
    earlier test in the session mutated them -- check only the
    immutable fields for invariance."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "snip_get"},
    })
    r.raise_for_status()
    got = r.json()["data"]
    assert got["manufacturer"] == "JMRI-Python"
    assert got["model"] == "JMRI Server"
    # user_name / user_description keys present regardless of value.
    assert "user_name" in got
    assert "user_description" in got


async def test_snip_update_mutates_advertised_identity(olcb_client) -> None:
    """snip_update replaces user_name / user_description and a
    subsequent snip_get sees the new values. A peer probing via
    SNIP would see them too since the responder reads live state."""
    tc, _, conn, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "snip_update",
            "user_name": "north-yard-throttle",
            "user_description": "raspi in the workshop",
        },
    })
    r.raise_for_status()
    assert r.json()["data"]["user_name"] == "north-yard-throttle"
    # Same values live on the connection.
    assert conn.node_info.user_name == "north-yard-throttle"
    assert conn.node_info.user_description == "raspi in the workshop"
    # snip_get reflects them.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "snip_get"},
    })
    r.raise_for_status()
    assert r.json()["data"]["user_name"] == "north-yard-throttle"


async def test_snip_update_rejects_non_string(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "snip_update",
            "user_name": 42,
        },
    })
    assert r.status_code == 400
    assert "user_name" in r.text


async def test_roster_export_round_trips_through_import(olcb_client) -> None:
    """A peer with SNIP + PIP cached exports as JSON; a fresh
    connection imports it back and both caches populate the same
    way (identity round-trip proves shape stability)."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415

    info = NodeInfo(
        manufacturer="Bus Peer Inc", model="RailMonitor",
        hardware_version="rev 2", software_version="3.4",
        user_name="rm-north", user_description="north yard",
    )
    snip_svc = SnipResponder(peer, info)
    pip_svc = PipService(
        peer, ProtocolFlag.DATAGRAM | ProtocolFlag.CDI,
    )
    try:
        await peer.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=NodeId.parse("02.02.02.02.02.02").bytes,
        ))
        await asyncio.sleep(0)
        # Populate SNIP + PIP caches via probe_all.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "probe_all", "timeout": 1.0},
        })
        r.raise_for_status()

        # Export.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "roster_export"},
        })
        r.raise_for_status()
        exported = r.json()["data"]
        assert exported["version"] == 1
        assert "exported_at" in exported
        assert isinstance(exported["nodes"], list)
        assert any(n["alias"] == "0x222" for n in exported["nodes"])

        # Clear caches then import back -- verify counts.
        conn._openlcb_snip_cache = {}  # type: ignore[attr-defined]
        conn._openlcb_pip_cache = {}  # type: ignore[attr-defined]
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "roster_import",
                "roster": exported,
            },
        })
        r.raise_for_status()
        summary = r.json()["data"]
        assert summary["nodes_seen"] >= 1
        assert summary["snip_loaded"] >= 1
        assert summary["pip_loaded"] >= 1
    finally:
        pip_svc.close()
        snip_svc.close()


async def test_roster_import_skips_our_own_alias(olcb_client) -> None:
    """A roster containing our own alias must not overwrite our
    local identity -- the import silently skips it."""
    tc, _, _, _ = olcb_client
    payload = {
        "version": 1,
        "nodes": [
            {
                "alias": "0x111",   # fixture's local alias
                "node_id": "02.99.99.99.99.99",
                "snip": {
                    "manufacturer": "Attacker",
                    "model": "Impostor",
                    "hardware_version": "", "software_version": "",
                    "user_name": "", "user_description": "",
                },
                "pip": {"raw": "0x800000", "names": ["SIMPLE"]},
            },
        ],
    }
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "roster_import", "roster": payload},
    })
    r.raise_for_status()
    summary = r.json()["data"]
    assert summary["nodes_seen"] == 1
    # Our alias skipped -> nothing loaded.
    assert summary["snip_loaded"] == 0
    assert summary["pip_loaded"] == 0
    assert summary["nid_loaded"] == 0


async def test_identify_events_addressed_sends_targeted_probe(olcb_client) -> None:
    """The identify_events_addressed action puts MTI 0x0968 on the
    wire pointing at the target alias. Peer's addressed-message
    reassembler sees a request with our source alias in the header
    and the peer alias in the 2-byte body prefix."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb.addressed import AddressedReassembler  # noqa: PLC0415
    from jmri_openlcb.codec import OlcbFrame  # noqa: PLC0415
    from jmri_openlcb.event_ident import (  # noqa: PLC0415
        MTI_IDENTIFY_EVENTS_ADDRESSED,
    )
    import asyncio  # noqa: PLC0415
    seen = []
    reasm = AddressedReassembler()

    def _watch(frame: OlcbFrame) -> None:
        msg = reasm.receive(frame)
        if msg is not None and msg.mti == MTI_IDENTIFY_EVENTS_ADDRESSED:
            seen.append(msg)

    peer.frames.connect(_watch)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "identify_events_addressed",
            "alias": "0x222",
        },
    })
    r.raise_for_status()
    assert r.json()["data"] == {"alias": "0x222", "sent": True}
    await asyncio.sleep(0.02)
    assert len(seen) == 1
    msg = seen[0]
    # Header source alias = our (CT) alias 0x111.
    assert msg.source_alias == 0x111
    # Addressed prefix = dest alias 0x222.
    assert msg.dest_alias == 0x222


async def test_pcer_history_action_returns_counts_and_names(olcb_client) -> None:
    """pcer_history for a peer surfaces per-event counts, last_seen,
    seconds_ago, and the S-9.7.3 well-known name where applicable."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    ev = EventId.parse("01.00.00.00.00.00.FF.FE")   # Emergency Off
    for _ in range(3):
        await peer.produce_event(ev)
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "pcer_history", "alias": "0x222"},
    })
    r.raise_for_status()
    rows = r.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "01.00.00.00.00.00.FF.FE"
    assert row["name"] == "Emergency Off"
    assert row["count"] == 3
    assert 0 <= row["seconds_ago"] < 5


async def test_node_health_reports_healthy_when_peer_answers_all(olcb_client) -> None:
    """Peer with SNIP + PIP + Verify responders -> node_health
    reports all three round-trips + status=healthy."""
    tc, peer, conn, _ = olcb_client
    info = NodeInfo(
        manufacturer="Test", model="Node",
        hardware_version="1", software_version="1",
        user_name="", user_description="",
    )
    peer_verify = VerifyNodeIdService(
        peer, NodeId.parse("02.02.02.02.02.02"),
    )
    peer_snip = SnipResponder(peer, info)
    peer_pip = PipService(peer, ProtocolFlag.SNIP | ProtocolFlag.DATAGRAM)
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "node_health",
                "alias": "0x222", "timeout": 1.0,
            },
        })
        r.raise_for_status()
        h = r.json()["data"]
        assert h["alias"] == "0x222"
        assert h["verify_ms"] is not None
        assert h["pip_ms"] is not None
        assert h["snip_ms"] is not None
        assert h["status"] == "healthy"
    finally:
        peer_pip.close()
        peer_snip.close()
        peer_verify.close()


async def test_bus_wide_health_probes_every_known_peer(olcb_client) -> None:
    """bus_wide_health runs node_health in parallel against every
    known alias (union of NID lookup + event roster). One healthy
    peer + one silent peer -> one row per alias, healthy vs
    unresponsive."""
    tc, peer, conn, _ = olcb_client
    # Set up: peer 0x222 has full responders; also pre-register a
    # ghost 0x333 in the NID map that has no responder at all ->
    # sweep should report unresponsive.
    info = NodeInfo(
        manufacturer="X", model="Y",
        hardware_version="1", software_version="1",
        user_name="", user_description="",
    )
    peer_verify = VerifyNodeIdService(
        peer, NodeId.parse("02.02.02.02.02.02"),
    )
    peer_snip = SnipResponder(peer, info)
    peer_pip = PipService(peer, ProtocolFlag.SNIP | ProtocolFlag.DATAGRAM)
    # Seed both aliases in the NID map so the sweep picks them up.
    # 0x222 has responders (healthy); 0x333 is a ghost with none.
    conn.node_id_lookup._by_alias[0x222] = NodeId.parse(  # noqa: SLF001
        "02.02.02.02.02.02",
    )
    conn.node_id_lookup._by_alias[0x333] = NodeId.parse(  # noqa: SLF001
        "02.03.03.03.03.03",
    )
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "bus_wide_health", "timeout": 0.1},
        })
        r.raise_for_status()
        rows = r.json()["data"]
        by_alias = {row["alias"]: row for row in rows}
        assert set(by_alias.keys()) == {"0x222", "0x333"}
        assert by_alias["0x222"]["status"] == "healthy"
        assert by_alias["0x333"]["status"] == "unresponsive"
    finally:
        peer_pip.close()
        peer_snip.close()
        peer_verify.close()


async def test_bus_wide_health_returns_empty_with_no_peers(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_wide_health", "timeout": 0.1},
    })
    r.raise_for_status()
    assert r.json()["data"] == []


async def test_node_health_reports_unresponsive_when_peer_silent(olcb_client) -> None:
    """No responders on the peer -> all three probes time out and
    status=unresponsive."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "node_health",
            "alias": "0x333", "timeout": 0.1,
        },
    })
    r.raise_for_status()
    h = r.json()["data"]
    assert h["verify_ms"] is None
    assert h["pip_ms"] is None
    assert h["snip_ms"] is None
    assert h["status"] == "unresponsive"


async def test_bus_health_counts_identified_and_silent(olcb_client) -> None:
    """bus_health rolls up total nodes, identified count, cache
    coverage and longest silence into a compact projection. Zero-
    peer baseline is 0/0/0/0/0/None; after a peer identifies plus
    we age its last-seen the counts + oldest_silence reflect it."""
    tc, peer, conn, _ = olcb_client
    # Baseline -- nothing captured yet.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_health"},
    })
    r.raise_for_status()
    h = r.json()["data"]
    assert h["total_nodes"] == 0
    assert h["identified"] == 0
    assert h["with_snip"] == 0
    assert h["with_pip"] == 0
    assert h["quiet_count"] == 0
    assert h["oldest_silence_seconds"] is None

    # Peer identifies itself; then we back-date its last-seen so the
    # sweeper considers it quiet.
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio, time  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.05)
    # 5 minutes ago = well past the 60s quiet threshold.
    conn.last_seen._last_seen[0x222] = time.time() - 300.0

    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_health"},
    })
    r.raise_for_status()
    h = r.json()["data"]
    assert h["total_nodes"] == 1
    assert h["identified"] == 1
    assert h["quiet_count"] == 1
    assert h["oldest_silence_seconds"] > 60
    assert h["quiet_threshold_seconds"] == 60.0


async def test_cdi_fetch_caches_second_call(olcb_client) -> None:
    """First cdi_fetch downloads over the wire; the second returns
    the cached parse. Confirmed by tearing down the peer's memory
    responder between fetches -- the cached call still succeeds."""
    tc, peer, _, _ = olcb_client
    import sys, pathlib  # noqa: PLC0415
    olcb_tests = (
        pathlib.Path(__file__).resolve().parents[2].parent
        / "jmri-openlcb"
    )
    sys.path.insert(0, str(olcb_tests))
    try:
        from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415
        from jmri_openlcb import SPACE_CDI  # noqa: PLC0415
        from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
        engine = DatagramEngine(peer)
        await engine.attach()
        device = _FakeMemoryDevice(engine, peer)
        cdi_xml = (
            b"<?xml version='1.0'?><cdi>"
            b"<identification><manufacturer>Acme</manufacturer></identification>"
            b"</cdi>\x00"
        )
        for i, b in enumerate(cdi_xml):
            device.store[(SPACE_CDI, i)] = b

        # First fetch -- populates the cache.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "cdi_fetch", "alias": "0x222"},
        })
        r.raise_for_status()
        # Tear the peer down; a cache-less code path would 504 now.
        device.close()
        await engine.detach()

        # Second fetch -- should hit the cache, no wire trip needed.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "cdi_fetch", "alias": "0x222"},
        })
        r.raise_for_status()
        assert r.json()["data"]["identification"]["manufacturer"] == "Acme"
    finally:
        sys.path.remove(str(olcb_tests))


async def test_cdi_invalidate_reports_alias_and_clears(olcb_client) -> None:
    """cdi_invalidate returns the alias it targeted and a cleared
    count. On an empty cache the count is 0; the global form
    returns alias=None."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "cdi_invalidate", "alias": "0x222"},
    })
    r.raise_for_status()
    body = r.json()["data"]
    assert body["alias"] == "0x222"
    assert body["cleared"] == 0
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "cdi_invalidate"},
    })
    r.raise_for_status()
    assert r.json()["data"]["alias"] is None


async def test_cdi_refresh_bypasses_cache(olcb_client) -> None:
    """refresh=True forces a fresh wire fetch even when cached."""
    tc, peer, _, _ = olcb_client
    import sys, pathlib  # noqa: PLC0415
    olcb_tests = (
        pathlib.Path(__file__).resolve().parents[2].parent
        / "jmri-openlcb"
    )
    sys.path.insert(0, str(olcb_tests))
    try:
        from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415
        from jmri_openlcb import SPACE_CDI  # noqa: PLC0415
        from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
        engine = DatagramEngine(peer)
        await engine.attach()
        device = _FakeMemoryDevice(engine, peer)
        # Version A
        v_a = (
            b"<?xml version='1.0'?><cdi>"
            b"<identification><manufacturer>A</manufacturer></identification>"
            b"</cdi>\x00"
        )
        for i, b in enumerate(v_a):
            device.store[(SPACE_CDI, i)] = b

        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "cdi_fetch", "alias": "0x222"},
        })
        r.raise_for_status()
        assert r.json()["data"]["identification"]["manufacturer"] == "A"

        # Swap the peer's XML for version B; a cached call still
        # sees A, a refresh=True call sees B.
        v_b = (
            b"<?xml version='1.0'?><cdi>"
            b"<identification><manufacturer>B</manufacturer></identification>"
            b"</cdi>\x00"
        )
        # Clear then repopulate.
        for i in range(max(len(v_a), len(v_b))):
            device.store.pop((SPACE_CDI, i), None)
        for i, b in enumerate(v_b):
            device.store[(SPACE_CDI, i)] = b

        # Cached read -> still A.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "cdi_fetch", "alias": "0x222"},
        })
        r.raise_for_status()
        assert r.json()["data"]["identification"]["manufacturer"] == "A"

        # refresh=True -> B.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "cdi_fetch", "alias": "0x222",
                "refresh": True,
            },
        })
        r.raise_for_status()
        assert r.json()["data"]["identification"]["manufacturer"] == "B"
    finally:
        try:
            device.close()
            await engine.detach()
        except Exception:
            pass
        sys.path.remove(str(olcb_tests))


async def test_firmware_upgrade_writes_firmware_and_returns_count(olcb_client) -> None:
    """Full end-to-end via the JSON topic: firmware_upgrade action
    with a firmware_hex payload runs the S-9.7.4.3 flow; a fake
    responder-based target ends up with the bytes in SPACE_FIRMWARE."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import (  # noqa: PLC0415
        EVENT_INIT_COMPLETE_SIMPLE,
        SPACE_FIRMWARE,
        MemoryConfigResponder,
    )
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    import asyncio  # noqa: PLC0415

    peer_engine = DatagramEngine(peer)
    await peer_engine.attach()
    peer_responder = MemoryConfigResponder(peer_engine)
    peer_responder.set_space(SPACE_FIRMWARE, bytes(2048))

    async def _on_freeze(dg) -> None:  # noqa: ANN001
        if (
            len(dg.data) >= 3
            and dg.data[0] == 0x20 and dg.data[1] == 0xA1
            and dg.data[2] == SPACE_FIRMWARE
        ):
            # Real firmware target keeps space writable after Freeze;
            # emit Init Complete PCER so the CT proceeds.
            peer_responder._frozen.discard(SPACE_FIRMWARE)  # noqa: SLF001
            await peer.produce_event(EVENT_INIT_COMPLETE_SIMPLE)

    peer_engine.inbound.connect(_on_freeze)

    try:
        firmware = bytes(range(200))   # 200 bytes -> chunks 64/64/64/8
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "firmware_upgrade",
                "alias": "0x222",
                "firmware_hex": firmware.hex(),
            },
        })
        r.raise_for_status()
        assert r.json()["data"] == {
            "alias": "0x222",
            "bytes_written": 200,
        }
        # And the peer's Firmware Space actually holds the bytes.
        got = peer_responder.get_space(SPACE_FIRMWARE)
        assert got is not None
        assert got[:200] == firmware
    finally:
        peer_responder.close()
        await peer_engine.detach()


async def test_firmware_upgrade_rejects_missing_hex(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "firmware_upgrade", "alias": "0x222"},
    })
    assert r.status_code == 400
    assert "firmware_hex" in r.text


async def test_firmware_upgrade_rejects_bad_hex(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "firmware_upgrade",
            "alias": "0x222",
            "firmware_hex": "not-hex-chars-xyz",
        },
    })
    assert r.status_code == 400
    assert "hex" in r.text.lower()


async def test_memory_reboot_sends_lifecycle_upcode(olcb_client) -> None:
    """The memory_reboot action wires through to MemoryClient.reboot;
    intercepting on the peer's datagram engine confirms the upcode
    (0x20 0xA9) actually goes on the wire."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb.datagrams import Datagram, DatagramEngine  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    peer_engine = DatagramEngine(peer)
    await peer_engine.attach()
    seen: list[Datagram] = []
    peer_engine.inbound.connect(lambda dg: seen.append(dg))
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "memory_reboot",
                "alias": "0x222", "timeout": 1.0,
            },
        })
        r.raise_for_status()
        assert r.json()["data"]["action"] == "reboot"
        await asyncio.sleep(0.02)
        # Filter to memory-config datagrams (skip any Received-OK etc.).
        mem = [dg for dg in seen if dg.data and dg.data[0] == 0x20]
        assert mem, "expected a Memory Config datagram"
        assert mem[0].data == bytes([0x20, 0xA9])
    finally:
        await peer_engine.detach()


async def test_memory_factory_reset_requires_target_node_id(olcb_client) -> None:
    """The service action refuses to send Factory Reset without the
    target's NodeID -- a click without that field 400s instead of
    broadcasting an unsafe reset."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "memory_factory_reset", "alias": "0x222"},
    })
    assert r.status_code == 400
    assert "target_node_id" in r.text.lower()


async def test_memory_freeze_and_unfreeze_pair(olcb_client) -> None:
    """The service actions carry the space byte through to the wire.
    Confirmed by watching the datagrams that land on the peer."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb.datagrams import Datagram, DatagramEngine  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    peer_engine = DatagramEngine(peer)
    await peer_engine.attach()
    seen: list[Datagram] = []
    peer_engine.inbound.connect(lambda dg: seen.append(dg))
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "memory_freeze",
                "alias": "0x222", "space": 0xFE, "timeout": 1.0,
            },
        })
        r.raise_for_status()
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "memory_unfreeze",
                "alias": "0x222", "space": 0xFE, "timeout": 1.0,
            },
        })
        r.raise_for_status()
        await asyncio.sleep(0.02)
        mem = [dg for dg in seen if dg.data and dg.data[0] == 0x20]
        assert mem[0].data == bytes([0x20, 0xA1, 0xFE])
        assert mem[1].data == bytes([0x20, 0xA0, 0xFE])
    finally:
        await peer_engine.detach()


async def test_bus_stats_reports_top_events_with_well_known_name(olcb_client) -> None:
    """A PCER for a well-known event ID surfaces in top_events with
    the S-9.7.3 name populated so a UI can render "Emergency Off"
    alongside the hex."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    ev = EventId.parse("01.00.00.00.00.00.FF.FE")   # Emergency Off
    for _ in range(3):
        await peer.produce_event(ev)
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats", "top_n": 5},
    })
    r.raise_for_status()
    top_events = r.json()["data"]["top_events"]
    assert top_events
    top = top_events[0]
    assert top["event"] == "01.00.00.00.00.00.FF.FE"
    assert top["count"] == 3
    assert top["name"] == "Emergency Off"


async def test_prune_all_drops_every_peer_but_us(olcb_client) -> None:
    """prune_all iterates every known alias and fires the release
    fan-out for each -- same as an AMR would. Our own alias never
    appears in the drop list."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    # Two peers, both fresh -- normal prune_stale wouldn't touch them.
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x333),
        data=b"\x02\x03\x03\x03\x03\x03",
    ))
    await asyncio.sleep(0.05)
    assert conn.node_id_lookup.get(0x222) is not None

    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "prune_all"},
    })
    r.raise_for_status()
    result = r.json()["data"]
    assert result["count"] >= 2
    assert set(result["pruned"]) >= {"0x222", "0x333"}
    # Our alias (0x111 in the fixture) never gets pruned.
    assert "0x111" not in result["pruned"]
    # Roster cleared.
    assert conn.node_id_lookup.get(0x222) is None
    assert conn.node_id_lookup.get(0x333) is None


async def test_prune_stale_returns_dropped_aliases(olcb_client) -> None:
    """prune_stale runs one sweep synchronously. A peer we haven't
    heard from for longer than the sweeper's threshold gets pruned;
    the response lists what was dropped so an operator sees which
    aliases actually went away."""
    tc, peer, conn, _ = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.05)
    # Age the tracker's timestamp so the sweeper considers 0x222
    # stale. Direct manipulation is fine here -- the sweeper reads
    # from tracker._last_seen which we override to a far-past value.
    conn.last_seen._last_seen[0x222] = 0.0

    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "prune_stale"},
    })
    r.raise_for_status()
    result = r.json()["data"]
    assert result["count"] == 1
    assert result["pruned"] == ["0x222"]
    # And the roster no longer knows about 0x222.
    assert conn.node_id_lookup.get(0x222) is None


async def test_recent_frames_tags_local_alias_as_us(olcb_client) -> None:
    """Frames whose source_alias matches conn.traffic.source_alias
    (0x111 in the test fixture) carry is_us=True in the projection;
    peer-authored frames carry is_us=False. Lets a UI badge them
    without a second lookup."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    # We (0x111) emit one PCER; the peer (0x222) emits another.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "send_frame",
            "header": "0x195B4111",   # PCER from alias 0x111
            "data": "05.01.01.01.4C.43.00.01".replace(".", ""),
        },
    })
    r.raise_for_status()
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.02"))
    await asyncio.sleep(0.05)

    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    frames = r.json()["data"]
    by_alias = {f["source_alias"]: f for f in frames}
    assert by_alias["0x111"]["is_us"] is True
    assert by_alias["0x222"]["is_us"] is False


async def test_bus_stats_reports_our_alias(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats", "top_n": 1},
    })
    r.raise_for_status()
    assert r.json()["data"]["our_alias"] == "0x111"


async def test_pause_and_resume_toggle_monitor(olcb_client) -> None:
    """pause_monitor makes recent_frames stop growing; resume_monitor
    lets it resume. Both actions report the resulting paused state
    and bus_stats reflects it."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)

    # Pause and record buffer size.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "pause_monitor"},
    })
    r.raise_for_status()
    assert r.json()["data"]["paused"] is True
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats", "top_n": 1},
    })
    r.raise_for_status()
    stats = r.json()["data"]
    assert stats["paused"] is True
    before = stats["total_frames"]

    # New peer traffic while paused is dropped.
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.02"))
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.03"))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats", "top_n": 1},
    })
    r.raise_for_status()
    assert r.json()["data"]["total_frames"] == before

    # Resume + verify capture resumes.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "resume_monitor"},
    })
    r.raise_for_status()
    assert r.json()["data"]["paused"] is False
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.04"))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "bus_stats", "top_n": 1},
    })
    r.raise_for_status()
    assert r.json()["data"]["total_frames"] > before


async def test_recent_frames_filters_by_source_alias(olcb_client) -> None:
    """The service's ``recent_frames`` accepts a ``source_alias``
    hex string and returns only frames from that alias. Empty ->
    no filter."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    # Peer B at 0x222 emits a Verified NID (frames captured with
    # source_alias=0x222). Peer C wouldn't exist on the loopback
    # pair, so simulate two peers by sending frames with two
    # different aliases from the same peer.
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x333),
        data=b"\x02\x03\x03\x03\x03\x03",
    ))
    await asyncio.sleep(0.05)
    # No filter -> both frames.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames"},
    })
    r.raise_for_status()
    all_frames = r.json()["data"]
    src_aliases = {f["source_alias"] for f in all_frames}
    assert "0x222" in src_aliases and "0x333" in src_aliases

    # Filter to 0x222.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames", "source_alias": "222"},
    })
    r.raise_for_status()
    filtered = r.json()["data"]
    assert filtered, "expected at least one 0x222 frame"
    assert all(f["source_alias"] == "0x222" for f in filtered)


async def test_recent_frames_source_alias_invert_hides_that_peer(olcb_client) -> None:
    """source_alias_invert=True flips the filter from include to
    exclude: shows every frame *except* those from the given alias."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x333),
        data=b"\x02\x03\x03\x03\x03\x03",
    ))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "recent_frames",
            "source_alias": "222",
            "source_alias_invert": True,
        },
    })
    r.raise_for_status()
    rows = r.json()["data"]
    assert rows, "expected at least one non-0x222 frame"
    assert all(r["source_alias"] != "0x222" for r in rows)
    assert any(r["source_alias"] == "0x333" for r in rows)


async def test_recent_frames_text_invert_hides_matches(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    # Also trigger some Verified NID traffic so we have non-PCER
    # frames the inverted filter should keep.
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "recent_frames",
            "text": "pcer",
            "text_invert": True,
        },
    })
    r.raise_for_status()
    rows = r.json()["data"]
    assert rows
    assert all("PCER" not in r["summary"] for r in rows)


async def test_recent_frames_invert_flags_cascade(olcb_client) -> None:
    """Multiple invert filters compose -- hide alias X AND hide PCER
    means neither shows up."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    # 0x222 emits both a PCER and a Verified NID; 0x333 emits only
    # a Verified NID. Hiding alias 0x222 AND hiding PCER leaves
    # only 0x333's Verified NID.
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x333),
        data=b"\x02\x03\x03\x03\x03\x03",
    ))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "recent_frames",
            "source_alias": "222",
            "source_alias_invert": True,
            "text": "pcer",
            "text_invert": True,
        },
    })
    r.raise_for_status()
    rows = r.json()["data"]
    assert rows
    # Only the 0x333 non-PCER frame(s) remain.
    for row in rows:
        assert row["source_alias"] != "0x222"
        assert "PCER" not in row["summary"]


async def test_recent_frames_text_filter_matches_summary(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    # Case-insensitive substring match on the summary line.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames", "text": "pcer"},
    })
    r.raise_for_status()
    filtered = r.json()["data"]
    assert filtered
    assert all("PCER" in f["summary"] for f in filtered)
    # A nonsense filter -> empty.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames", "text": "xyzzy-no-match"},
    })
    r.raise_for_status()
    assert r.json()["data"] == []


async def test_recent_frames_invalid_hex_silently_no_filter(olcb_client) -> None:
    """A half-typed alias like ``2z`` shouldn't 400 the whole
    request; the service treats unparseable filters as "no filter"
    so a user typing in the UI sees frames come and go smoothly."""
    tc, peer, _, _ = olcb_client
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.05)
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "recent_frames", "source_alias": "2z"},
    })
    r.raise_for_status()
    # Frames still present -- bad filter ignored.
    assert r.json()["data"]


async def test_probe_all_caches_drop_peer_after_amr(olcb_client) -> None:
    """When a peer emits AMR (Alias Map Reset) it announces it's
    releasing its alias. The service-level SNIP + PIP caches must
    forget the entry so the discovery UI doesn't keep showing a
    ghost. Otherwise repeated bench cycling of a decoder leaves
    stale identity data around indefinitely."""
    tc, peer, conn, _ = olcb_client
    info = NodeInfo(
        manufacturer="Ghost Peer", model="Vanishing Decoder",
        hardware_version="rev 1", software_version="1.0",
        user_name="", user_description="",
    )
    snip_svc = SnipResponder(peer, info)
    pip_svc = PipService(peer, ProtocolFlag.DATAGRAM)
    try:
        from jmri_openlcb.codec import (  # noqa: PLC0415
            FT_MTI,
            OlcbFrame,
            _pack_header,
            make_amr_header,
        )
        from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
        import asyncio  # noqa: PLC0415
        await peer.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=NodeId.parse("02.02.02.02.02.02").bytes,
        ))
        await asyncio.sleep(0)
        # Populate the caches via probe_all.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "probe_all", "timeout": 1.0},
        })
        r.raise_for_status()
        by_alias = {n["alias"]: n for n in r.json()["data"]}
        assert by_alias["0x222"]["snip"] is not None
        assert by_alias["0x222"]["pip"] is not None

        # Peer says goodbye.
        await peer.send_frame(OlcbFrame(
            header=make_amr_header(0x222), data=b"",
        ))
        await asyncio.sleep(0.05)

        # Both caches must have forgotten 0x222; a plain list either
        # omits the row entirely (NID + roster also cleared) or
        # returns null identity, but never stale data.
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "list",
        })
        r.raise_for_status()
        by_alias = {n["alias"]: n for n in r.json()["data"]}
        if "0x222" in by_alias:
            assert by_alias["0x222"]["snip"] is None
            assert by_alias["0x222"]["pip"] is None
    finally:
        pip_svc.close()
        snip_svc.close()


async def test_probe_all_tolerates_unresponsive_peers(olcb_client) -> None:
    """A peer that doesn't answer either probe leaves ``snip: null``
    and ``pip: null`` in its row rather than erroring out. Real
    bus discovery routinely has one or two offline nodes."""
    tc, peer, _, _ = olcb_client
    # Populate a known alias so probe_all has something to try.
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=NodeId.parse("02.02.02.02.02.02").bytes,
    ))
    await asyncio.sleep(0)
    # No SNIP or PIP responders on the peer side.
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "probe_all", "timeout": 0.1},
    })
    r.raise_for_status()
    data = r.json()["data"]
    by_alias = {n["alias"]: n for n in data}
    assert "0x222" in by_alias
    assert by_alias["0x222"]["snip"] is None
    assert by_alias["0x222"]["pip"] is None


async def test_probe_all_returns_empty_when_no_aliases_known(olcb_client) -> None:
    """Fresh connection with no discovered peers -> probe_all just
    returns an empty list without erroring."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "probe_all"},
    })
    r.raise_for_status()
    assert r.json()["data"] == []


async def test_probe_snip_timeout_surfaces_504(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "probe_snip", "alias": "0x222", "timeout": 0.05},
    })
    assert r.status_code == 504


# ---- openlcb.post -- probe_pip ------------------------------------------


async def test_probe_pip_returns_flag_set(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    svc = PipService(peer, ProtocolFlag.DATAGRAM | ProtocolFlag.SNIP)
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "probe_pip", "alias": "0x222", "timeout": 1.0},
        })
        r.raise_for_status()
        data = r.json()["data"]
        assert data["alias"] == "0x222"
        assert "DATAGRAM" in data["flags"]["names"]
        assert "SNIP" in data["flags"]["names"]
        # ``raw`` is the numeric mask so a caller can bitwise test.
        assert int(data["flags"]["raw"], 16) == int(
            ProtocolFlag.DATAGRAM | ProtocolFlag.SNIP,
        )
    finally:
        svc.close()


# ---- openlcb.post -- search ---------------------------------------------


async def test_search_returns_hosted_locos(olcb_client) -> None:
    """The peer runs a CS with hosted DCC address 3 -- our search
    round-trips the alias back to the JSON caller."""
    tc, peer, _, _ = olcb_client
    dcc = ThrottleManager("X")
    cs = CommandStationNode(
        peer, dcc, station_node_id=NodeId.parse("02.02.02.02.02.02"),
    )
    try:
        loco = await cs.add_loco(3)
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "search", "address": 3, "timeout": 0.5,
            },
        })
        r.raise_for_status()
        data = r.json()["data"]
        assert len(data) == 1
        assert data[0]["alias"] == f"{loco.alias:#05x}"
    finally:
        cs.close()


async def test_search_requires_address(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "search"},
    })
    assert r.status_code == 400


async def test_unknown_action_400s(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "eat_pizza"},
    })
    assert r.status_code == 400


# ---- openlcbCS.list ----------------------------------------------------


async def test_cs_list_empty_when_no_cs_attached(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={"type": "openlcbCS", "method": "list"})
    r.raise_for_status()
    assert r.json()["data"] == []


CDI_XML = """\
<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>Acme</manufacturer>
    <model>Widget-3000</model>
    <hardwareVersion>rev A</hardwareVersion>
    <softwareVersion>1.2.3</softwareVersion>
  </identification>
  <segment origin="0" space="0xFE">
    <name>User Config</name>
    <int size="1">
      <name>Turnouts</name>
      <default>4</default>
    </int>
    <string size="16">
      <name>Label</name>
    </string>
    <bit bit="3">
      <name>Enabled</name>
    </bit>
  </segment>
</cdi>
"""


async def _wire_cdi_peer(peer, xml=CDI_XML):
    """Attach a datagram engine + fake memory device to the peer so
    the client's CDI fetches / reads / writes have something to
    respond. Returns the device so tests can inspect its store."""
    from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415
    from jmri_openlcb import SPACE_CDI  # noqa: PLC0415
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    engine = DatagramEngine(peer)
    await engine.attach()
    device = _FakeMemoryDevice(engine, peer)
    # Seed the CDI space with the XML + null terminator.
    payload = xml.encode("utf-8") + b"\x00"
    for i, byte in enumerate(payload):
        device.store[(SPACE_CDI, i)] = byte
    return device, engine


async def test_cdi_fetch_returns_projected_tree(olcb_client) -> None:
    """Round-trip: fake CDI seeded on the peer -> cdi_fetch action
    returns a JSON tree with identification + segments + fields."""
    tc, peer, _, _ = olcb_client
    # The DatagramEngine tests live in jmri-openlcb; here we borrow
    # the fake memory device to serve reads/writes on the peer side.
    # Need the jmri-openlcb tests dir on sys.path for the import.
    import sys, pathlib   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    device, engine = await _wire_cdi_peer(peer)
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "cdi_fetch",
                "alias": "0x222",
                "max_bytes": 8192,
            },
        })
        r.raise_for_status()
        data = r.json()["data"]
        assert data["identification"]["manufacturer"] == "Acme"
        assert data["identification"]["model"] == "Widget-3000"
        assert len(data["segments"]) == 1
        seg = data["segments"][0]
        assert seg["space"] == 0xFE
        # Three children: int(1), string(16), bit.
        assert len(seg["children"]) == 3
        int_field, str_field, bit_field = seg["children"]
        assert int_field["type"] == "int"
        assert int_field["size"] == 1
        assert int_field["offset"] == 0
        assert int_field["default"] == 4
        assert str_field["type"] == "string"
        assert str_field["size"] == 16
        assert str_field["offset"] == 1
        assert bit_field["type"] == "bit"
        assert bit_field["bit"] == 3
    finally:
        device.close()
        await engine.detach()
        sys.path.remove(str(olcb_tests))


async def test_cdi_read_returns_current_value(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    import sys, pathlib   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    device, engine = await _wire_cdi_peer(peer)
    try:
        from jmri_openlcb import SPACE_CONFIG   # noqa: PLC0415
        # Seed the config space so the int reads as 7.
        device.store[(SPACE_CONFIG, 0)] = 7
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "cdi_read", "alias": "0x222",
                "space": 0xFE, "offset": 0, "size": 1, "type": "int",
            },
        })
        r.raise_for_status()
        assert r.json()["data"]["value"] == 7
    finally:
        device.close()
        await engine.detach()
        sys.path.remove(str(olcb_tests))


async def test_cdi_write_updates_peer_memory(olcb_client) -> None:
    tc, peer, _, _ = olcb_client
    import sys, pathlib   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    device, engine = await _wire_cdi_peer(peer)
    try:
        from jmri_openlcb import SPACE_CONFIG   # noqa: PLC0415
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "cdi_write", "alias": "0x222",
                "space": 0xFE, "offset": 0, "size": 1, "type": "int",
                "value": 42,
            },
        })
        r.raise_for_status()
        assert r.json()["data"]["written"] is True
        # Peer memory has the new byte.
        assert device.store[(SPACE_CONFIG, 0)] == 42
    finally:
        device.close()
        await engine.detach()
        sys.path.remove(str(olcb_tests))


async def test_cdi_write_encodes_eventid_from_dotted_string(olcb_client) -> None:
    """EventId fields accept a dotted-hex string; the write encodes
    it back to 8 bytes on the wire."""
    tc, peer, _, _ = olcb_client
    import sys, pathlib   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    device, engine = await _wire_cdi_peer(peer)
    try:
        from jmri_openlcb import SPACE_CONFIG   # noqa: PLC0415
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {
                "action": "cdi_write", "alias": "0x222",
                "space": 0xFE, "offset": 0, "size": 8, "type": "eventid",
                "value": "05.01.01.01.4C.43.00.01",
            },
        })
        r.raise_for_status()
        got = bytes(
            device.store.get((SPACE_CONFIG, i), 0) for i in range(8)
        )
        assert got == bytes.fromhex("05010101 4C430001".replace(" ", ""))
    finally:
        device.close()
        await engine.detach()
        sys.path.remove(str(olcb_tests))


FDI_XML = """\
<?xml version="1.0"?>
<fdi>
  <segment>
    <group>
      <name>Front cab</name>
      <function size="1" kind="binary">
        <name>Headlight</name>
        <number>0</number>
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


async def _wire_fdi_peer(peer, xml=FDI_XML):
    """Attach fake memory device + seed FDI space (0xFA)."""
    from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415
    from jmri_openlcb import SPACE_FDI  # noqa: PLC0415
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    engine = DatagramEngine(peer)
    await engine.attach()
    device = _FakeMemoryDevice(engine, peer)
    payload = xml.encode("utf-8") + b"\x00"
    for i, byte in enumerate(payload):
        device.store[(SPACE_FDI, i)] = byte
    return device, engine


async def test_fdi_fetch_returns_tree_and_flat_list(olcb_client) -> None:
    """Round-trip: fake FDI on peer -> JSON tree with segments +
    groups + functions + a top-level flat list."""
    tc, peer, _, _ = olcb_client
    import sys, pathlib   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    device, engine = await _wire_fdi_peer(peer)
    try:
        r = await tc.post("/json/", json={
            "type": "openlcb", "method": "post",
            "data": {"action": "fdi_fetch", "alias": "0x222"},
        })
        r.raise_for_status()
        data = r.json()["data"]
        # Tree shape.
        assert len(data["segments"]) == 1
        seg = data["segments"][0]
        assert len(seg["groups"]) == 2
        # Front cab: Headlight (F0) + Horn (F2, momentary).
        front_cab = seg["groups"][0]
        assert front_cab["name"] == "Front cab"
        fns = front_cab["functions"]
        assert len(fns) == 2
        assert fns[0]["number"] == 0
        assert fns[0]["name"] == "Headlight"
        assert fns[0]["momentary"] is False
        assert fns[1]["number"] == 2
        assert fns[1]["momentary"] is True
        # Sound: Volume (F7, analog with min/max).
        sound = seg["groups"][1]
        vol = sound["functions"][0]
        assert vol["kind"] == "analog"
        assert vol["min"] == 0 and vol["max"] == 127
        # Flat list -- 3 functions in declaration order.
        flat_numbers = [fn["number"] for fn in data["flat"]]
        assert flat_numbers == [0, 2, 7]
    finally:
        device.close()
        await engine.detach()
        sys.path.remove(str(olcb_tests))


async def test_fdi_fetch_requires_alias(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "fdi_fetch"},
    })
    assert r.status_code == 400


async def test_fdi_fetch_times_out_when_peer_absent(olcb_client) -> None:
    """No FDI seeded on the peer -> memory read times out -> 504."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {"action": "fdi_fetch", "alias": "0x222"},
    })
    assert r.status_code == 504


async def test_cdi_read_rejects_unknown_type(olcb_client) -> None:
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcb", "method": "post",
        "data": {
            "action": "cdi_read", "alias": "0x222",
            "space": 0xFE, "offset": 0, "size": 1, "type": "purple",
        },
    })
    assert r.status_code == 400


async def test_cs_post_without_attached_cs_returns_400(olcb_client) -> None:
    """No ``ctx._openlcb_cs`` -> add/remove verbs surface the missing
    dependency as a clear 400 rather than crashing."""
    tc, _, _, _ = olcb_client
    r = await tc.post("/json/", json={
        "type": "openlcbCS", "method": "post",
        "data": {"action": "add", "address": 3},
    })
    assert r.status_code == 400
    assert "command station" in r.json()["detail"].lower()


async def test_cs_add_registers_hosted_loco(olcb_client) -> None:
    """With a CS attached, add returns the projected loco AND a
    subsequent list reflects it."""
    tc, _, conn, handle = olcb_client
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        handle.ctx._openlcb_cs = cs
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add", "address": 3, "long_address": False},
        })
        r.raise_for_status()
        entry = r.json()["data"]
        assert entry["address"] == 3
        assert entry["long_address"] is False
        assert entry["name"] == "DCC 3"
        # And ``list`` shows it.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "list",
        })
        r.raise_for_status()
        assert len(r.json()["data"]) == 1
    finally:
        cs.close()
        handle.ctx._openlcb_cs = None


async def test_cs_add_is_idempotent(olcb_client) -> None:
    """Two adds for the same address return the existing entry
    (CS's add_loco is idempotent) rather than double-registering."""
    tc, _, conn, handle = olcb_client
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        handle.ctx._openlcb_cs = cs
        first = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add", "address": 3},
        })
        second = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add", "address": 3},
        })
        assert first.json()["data"]["alias"] == second.json()["data"]["alias"]
        # Still just one hosted loco.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "list",
        })
        assert len(r.json()["data"]) == 1
    finally:
        cs.close()
        handle.ctx._openlcb_cs = None


async def test_cs_remove_drops_hosted_loco(olcb_client) -> None:
    tc, _, conn, handle = olcb_client
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        handle.ctx._openlcb_cs = cs
        await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add", "address": 3},
        })
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "remove", "address": 3},
        })
        r.raise_for_status()
        assert r.json()["data"]["removed"] is True
        # List now empty.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "list",
        })
        assert r.json()["data"] == []
    finally:
        cs.close()
        handle.ctx._openlcb_cs = None


async def test_cs_add_rejects_bad_address(olcb_client) -> None:
    tc, _, conn, handle = olcb_client
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        handle.ctx._openlcb_cs = cs
        # Missing address.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add"},
        })
        assert r.status_code == 400
        # Short address too big.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add", "address": 200, "long_address": False},
        })
        assert r.status_code == 400
        # Long address too big.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "add", "address": 20000, "long_address": True},
        })
        assert r.status_code == 400
    finally:
        cs.close()
        handle.ctx._openlcb_cs = None


async def test_cs_unknown_action_returns_400(olcb_client) -> None:
    tc, _, conn, handle = olcb_client
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        handle.ctx._openlcb_cs = cs
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "post",
            "data": {"action": "cook_dinner"},
        })
        assert r.status_code == 400
    finally:
        cs.close()
        handle.ctx._openlcb_cs = None


async def test_throttle_flow_drives_hosted_loco_end_to_end(olcb_client) -> None:
    """End-to-end drive-a-loco test -- the whole point of the CS.

    Setup: our node is at alias 0x111 (the server-side connection).
    A CS on peer alias 0x222 hosts DCC address 3 with a
    :class:`_RecordingThrottle` on the DCC side. We call the
    throttle JSON service ``put`` action to acquire address 3 --
    that runs the OlcbThrottleManager resolve/assign path over
    the wire. Then ``post`` action set-speed -> traction command
    -> CS routes to the DCC throttle. Finally we inspect the
    recorded DCC calls to confirm the speed reached the layout.
    """
    tc, peer, conn, handle = olcb_client
    from jmri_openlcb.command_station import CommandStationNode  # noqa: PLC0415
    from jmri_openlcb.alias import NodeId  # noqa: PLC0415
    from jmri_core.beans.throttle import Throttle  # noqa: PLC0415
    from jmri_core.beans.throttle_manager import ThrottleManager  # noqa: PLC0415
    from jmri_core.state import ThrottleDirection   # noqa: PLC0415
    import asyncio  # noqa: PLC0415

    class _RecordingThrottle(Throttle):
        __slots__ = ("calls",)

        def __init__(self, address, long_address):
            super().__init__(address=address, long_address=long_address)
            self.calls = []

        async def set_speed(self, speed):
            self.calls.append(("set_speed", speed))
            await super().set_speed(speed)

        async def set_direction(self, direction):
            self.calls.append(("set_direction", direction))
            await super().set_direction(direction)

        async def set_function(self, index, on):
            self.calls.append(("set_function", index, on))
            await super().set_function(index, on)

        async def emergency_stop(self):
            self.calls.append(("emergency_stop",))
            await super().emergency_stop()

    class _FakeThrottleManager(ThrottleManager):
        def __init__(self):
            super().__init__("Y")

        async def _construct(self, address, *, long_address):
            return _RecordingThrottle(address, long_address)

    # CS on the peer side, hosting DCC address 3.
    dcc = _FakeThrottleManager()
    cs = CommandStationNode(
        peer, dcc,
        station_node_id=NodeId.parse("06.01.00.00.00.00"),
    )
    try:
        loco = await cs.add_loco(3)

        # Acquire the throttle through the JSON service -- this is
        # what /throttle would do on behalf of a web client.
        r = await tc.post("/json/", json={
            "type": "throttle", "method": "put",
            "data": {"address": 3, "isLongAddress": False},
        })
        r.raise_for_status()
        acquired = r.json()["data"]
        assert acquired["address"] == 3

        # Give the assign-controller frame time to reach the CS.
        await asyncio.sleep(0.05)
        # Set speed = 0.5 forward.
        r = await tc.post("/json/", json={
            "type": "throttle", "method": "post",
            "data": {"name": "3", "speed": 0.5, "forward": True},
        })
        r.raise_for_status()
        # Wire round-trip settle.
        await asyncio.sleep(0.05)

        # The CS's DCC-side throttle should have received the
        # speed command translated back from the Traction Control
        # frame.
        assert loco.throttle is not None
        assert isinstance(loco.throttle, _RecordingThrottle)
        # Direction call before speed (the OlcbThrottle bean sends
        # direction first on speed changes).
        speed_calls = [c for c in loco.throttle.calls if c[0] == "set_speed"]
        assert speed_calls, "no set_speed reached the DCC throttle"
        # Normalized speed 0.5 with the OpenLCB MPH scaling.
        # OlcbThrottle sends abs(speed) * MPH_PER_UNIT = 50 mph;
        # CS translates back to normalized speed / MPH_PER_UNIT = 0.5.
        # Half-precision float precision loss is a few %; allow slack.
        assert abs(speed_calls[-1][1] - 0.5) < 0.05
        assert loco.throttle.direction == ThrottleDirection.FORWARD
    finally:
        cs.close()


async def test_throttle_function_flow_reaches_dcc_throttle(olcb_client) -> None:
    """Functions bridge end-to-end too: JSON throttle F1=on ->
    Traction Set Function -> CS's DCC throttle sees F1 set."""
    tc, peer, conn, handle = olcb_client
    from jmri_openlcb.command_station import CommandStationNode  # noqa: PLC0415
    from jmri_openlcb.alias import NodeId  # noqa: PLC0415
    from jmri_core.beans.throttle import Throttle  # noqa: PLC0415
    from jmri_core.beans.throttle_manager import ThrottleManager  # noqa: PLC0415
    import asyncio  # noqa: PLC0415

    class _T(Throttle):
        pass

    class _M(ThrottleManager):
        def __init__(self):
            super().__init__("Y")

        async def _construct(self, address, *, long_address):
            return _T(address=address, long_address=long_address)

    dcc = _M()
    cs = CommandStationNode(
        peer, dcc,
        station_node_id=NodeId.parse("06.01.00.00.00.00"),
    )
    try:
        loco = await cs.add_loco(3)
        # Acquire then set F1.
        await tc.post("/json/", json={
            "type": "throttle", "method": "put",
            "data": {"address": 3},
        })
        await asyncio.sleep(0.05)
        await tc.post("/json/", json={
            "type": "throttle", "method": "post",
            "data": {"name": "3", "F1": True},
        })
        await asyncio.sleep(0.05)
        assert loco.throttle is not None
        assert loco.throttle.functions[1] is True
    finally:
        cs.close()


async def test_cs_list_projects_hosted_locos(olcb_client) -> None:
    """When a CS is attached to ``ctx._openlcb_cs``, ``list``
    projects each hosted loco with alias, canonical name, and
    consist membership."""
    tc, _, conn, handle = olcb_client
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    try:
        loco = await cs.add_loco(3)
        from jmri_openlcb import ConsistMember  # noqa: PLC0415
        loco.consist_members.append(
            ConsistMember(node_id=NodeId(0x06_01_00_00_00_04)),
        )
        # Wire the CS into the server context -- the service picks it
        # up from this attribute.
        handle.ctx._openlcb_cs = cs

        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "list",
        })
        r.raise_for_status()
        data = r.json()["data"]
        assert len(data) == 1
        entry = data[0]
        assert entry["address"] == 3
        assert entry["long_address"] is False
        assert entry["node_id"] == "06.01.00.00.00.03"
        assert entry["alias"] == f"{loco.alias:#05x}"
        assert entry["name"] == "DCC 3"
        assert entry["has_throttle"] is False
        assert entry["consist_members"] == [
            {
                "node_id": "06.01.00.00.00.04",
                "hidden": False,
                "reverse": False,
            },
        ]
    finally:
        cs.close()
        handle.ctx._openlcb_cs = None
