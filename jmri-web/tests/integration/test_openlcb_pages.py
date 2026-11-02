"""End-to-end OpenLCB web tests -- real connection + loopback peer.

Uses ``httpx.AsyncClient`` + ASGI transport so the pytest asyncio
loop runs both sides of the loopback pair. ``TestClient`` would
put the app on a separate portal loop and the peer's frame
delivery wouldn't route.
"""
from __future__ import annotations

import httpx
import pytest
from jmri_openlcb import (
    NodeInfo,
    OlcbConnection,
    OlcbTrafficController,
    PipService,
    ProtocolFlag,
    SnipResponder,
    VerifyNodeIdService,
    loopback_pair,
)
from jmri_openlcb.alias import NodeId

from jmri_web.app import build_web_app


@pytest.fixture()
async def olcb_web():
    """Web app wrapped around an OpenLCB connection with a peer
    responder on the far side of the loopback."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
    })
    web = build_web_app_with_conn(conn)
    peer = OlcbTrafficController(b, source_alias=0x222)
    await peer.start()
    transport = httpx.ASGITransport(app=web.app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as tc:
            yield tc, peer, conn
    finally:
        await peer.aclose()
        await conn.close()


@pytest.fixture()
async def olcb_web_handle():
    """Same as ``olcb_web`` but also yields the ``WebHandle`` so
    tests that need to wire something onto ``ctx`` (like attaching
    a Command Station) can reach the server context."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
    })
    web = build_web_app_with_conn(conn)
    peer = OlcbTrafficController(b, source_alias=0x222)
    await peer.start()
    transport = httpx.ASGITransport(app=web.app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as tc:
            yield tc, peer, conn, web
    finally:
        await peer.aclose()
        await conn.close()


def build_web_app_with_conn(conn):
    """Build a web app with a specific connection. Follows the
    same shape as build_web_app but injects the OpenLCB
    connection instead of the default InternalConnection."""
    from jmri_server.app import build_app  # noqa: PLC0415
    import pathlib  # noqa: PLC0415
    from contextlib import asynccontextmanager  # noqa: PLC0415
    from fastapi.staticfiles import StaticFiles  # noqa: PLC0415
    from fastapi.templating import Jinja2Templates  # noqa: PLC0415
    from jmri_web.app import STATIC_DIR, TEMPLATES_DIR, WebHandle  # noqa: PLC0415
    from jmri_web.routes import install_template_routes  # noqa: PLC0415

    server = build_app(connection=conn)
    app = server.app
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    install_template_routes(app, server, templates)
    return WebHandle(app=app, server=server, templates=templates)


# ---- discovery page under an OpenLCB connection ------------------------


async def test_openlcb_discover_shows_identity_and_capabilities(olcb_web) -> None:
    """Discovery kicks off broadcast + probe_all so the returned
    fragment shows manufacturer / model / PIP badges for peers
    that answered SNIP + PIP."""
    tc, peer, _ = olcb_web
    verify_svc = VerifyNodeIdService(peer, NodeId.parse("02.02.02.02.02.02"))
    snip_svc = SnipResponder(peer, NodeInfo(
        manufacturer="Acme Rails", model="Signal Master",
        hardware_version="", software_version="",
        user_name="", user_description="",
    ))
    pip_svc = PipService(
        peer, ProtocolFlag.DATAGRAM | ProtocolFlag.EVENT_EXCHANGE,
    )
    try:
        r = await tc.post("/openlcb/discover")
        r.raise_for_status()
        body = r.text
        # Identity + capabilities visible inline.
        assert "Acme Rails" in body
        assert "Signal Master" in body
        assert "DATAGRAM" in body
        assert "EVENT_EXCHANGE" in body
    finally:
        pip_svc.close()
        snip_svc.close()
        verify_svc.close()


async def test_openlcb_home_renders_discovery_ui(olcb_web) -> None:
    """When the active connection IS OpenLCB, the discovery UI is
    unhidden -- no warning banner, Refresh button visible."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "Discovered nodes" in body
    assert "Refresh" in body
    assert "isn't OpenLCB" not in body


async def test_openlcb_home_shows_last_heard_column(olcb_web) -> None:
    """The discovered-nodes table has a Last heard column that
    renders an age for each peer we've heard a frame from."""
    tc, peer, conn = olcb_web
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    nid = NodeId.parse("02.02.02.02.02.02")
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=nid.bytes,
    ))
    await asyncio.sleep(0.02)
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "Last heard" in body
    # The peer emitted less than a minute ago -> an "Ns" cell, not "m".
    # Look for the "s" suffix inside the peer's row (a naive assert
    # on ">Ns<" is fragile because of whitespace, so match the
    # unit-only substring "0s" or "1s" -- the age is a fresh sub-
    # second value formatted as "0s" or "1s".
    import re  # noqa: PLC0415
    # Find "Xs" style age markers in the body.
    assert re.search(r">\s*\d+s\s*<", body), body


async def test_node_detail_page_has_identify_events_button(olcb_web) -> None:
    tc, peer, _ = olcb_web
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.02)
    r = await tc.get("/openlcb/nodes/0x222")
    r.raise_for_status()
    body = r.text
    assert "Identify events" in body
    assert 'hx-post="/openlcb/nodes/0x222/identify_events"' in body


async def test_node_identify_events_route_returns_ok_notice(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/nodes/0x222/identify_events")
    r.raise_for_status()
    body = r.text
    assert "sent Identify Events" in body
    assert "0x222" in body


async def test_node_detail_page_shows_firmware_upgrade_panel(olcb_web) -> None:
    """The node detail page includes a Firmware upgrade card with
    the multipart file input + upgrade button + destructive-warning
    text. Covers UI presence, not the wire flow (that's in the
    integration test)."""
    tc, peer, _ = olcb_web
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.02)
    r = await tc.get("/openlcb/nodes/0x222")
    r.raise_for_status()
    body = r.text
    assert "Firmware upgrade" in body
    assert 'hx-post="/openlcb/nodes/0x222/firmware/upgrade"' in body
    assert 'hx-encoding="multipart/form-data"' in body
    assert "Upgrade firmware" in body
    # Non-recoverable warning text present.
    assert "brick" in body.lower() or "recovery" in body.lower()


async def test_firmware_upgrade_endpoint_rejects_empty_file(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.post(
        "/openlcb/nodes/0x222/firmware/upgrade",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
    )
    r.raise_for_status()
    assert "empty" in r.text.lower()


async def test_node_health_fragment_has_retry_button(olcb_web) -> None:
    """The health fragment renders a Retry button wired to the
    same POST endpoint so the operator can re-probe a slow peer
    without leaving the fragment."""
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/nodes/0x555/health")
    r.raise_for_status()
    body = r.text
    assert "Retry" in body
    assert 'hx-post="/openlcb/nodes/0x555/health"' in body
    # The Retry sits inside the health fragment (same swap target).
    assert 'hx-target="#openlcb-node-health"' in body


async def test_node_health_button_reports_unresponsive_peer(olcb_web) -> None:
    """POST /openlcb/nodes/{alias}/health with no responders on the
    peer returns the fragment with status=unresponsive and 'timeout'
    for each probe."""
    tc, _, _ = olcb_web
    r = await tc.post(
        "/openlcb/nodes/0x555/health",
    )
    r.raise_for_status()
    body = r.text
    assert "unresponsive" in body
    assert "timeout" in body


async def test_node_detail_page_shows_health_button(olcb_web) -> None:
    tc, peer, _ = olcb_web
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.02)
    r = await tc.get("/openlcb/nodes/0x222")
    r.raise_for_status()
    body = r.text
    assert "Health check" in body
    assert 'hx-post="/openlcb/nodes/0x222/health"' in body


async def test_openlcb_nodes_refresh_returns_fragment(olcb_web) -> None:
    """/openlcb/nodes/refresh returns just the discovery table
    fragment (id="openlcb-nodes"), not the full page shell."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/nodes/refresh")
    r.raise_for_status()
    body = r.text
    assert 'id="openlcb-nodes"' in body
    # Fragment endpoints return the swap target without the base
    # template's html/body/nav chrome.
    assert "<html" not in body


async def test_openlcb_home_auto_polls_nodes_fragment(olcb_web) -> None:
    """Home page renders an htmx auto-poll on the nodes fragment
    pointing at the refresh endpoint."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert 'hx-get="/openlcb/nodes/refresh"' in body
    assert 'hx-trigger="every 5s"' in body


async def test_openlcb_home_shows_bus_health_card(olcb_web) -> None:
    """The Bus health card renders with counts + longest-silence
    fields even on a fresh connection. Zero-peer baseline shows
    0 total and a Longest silence em-dash placeholder. The card
    also carries the auto-poll trigger."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "Bus health" in body
    assert "Longest silence" in body
    assert "Identified (NID)" in body
    assert 'hx-get="/openlcb/health/refresh"' in body


async def test_node_pcer_history_fragment_shows_counts(olcb_web) -> None:
    """Node detail's PCER history fragment surfaces the peer's
    event counts + well-known names."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    for _ in range(2):
        await peer.produce_event(
            EventId.parse("01.00.00.00.00.00.FF.FE"),   # Emergency Off
        )
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/nodes/0x222/pcer_history")
    r.raise_for_status()
    body = r.text
    assert "01.00.00.00.00.00.FF.FE" in body
    assert "Emergency Off" in body
    # Count column.
    assert ">2<" in body


async def test_node_detail_page_embeds_pcer_history_card(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/nodes/0x222")
    r.raise_for_status()
    body = r.text
    assert "PCER activity" in body
    assert 'hx-get="/openlcb/nodes/0x222/pcer_history"' in body


async def test_node_frames_fragment_filters_to_this_alias(olcb_web) -> None:
    """/openlcb/nodes/{alias}/frames returns only frames whose
    source_alias matches, using the recent_frames source_alias
    filter server-side."""
    tc, peer, _ = olcb_web
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
    r = await tc.get("/openlcb/nodes/0x222/frames")
    r.raise_for_status()
    body = r.text
    # 0x222 frame shows up in the summary line.
    assert "Verified NID" in body
    # 0x333 is filtered out -- its NID bytes 02.03.03.03.03.03 don't
    # appear anywhere in the fragment.
    assert "02.03.03.03.03.03" not in body


async def test_node_detail_page_embeds_frame_log(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/nodes/0x222")
    r.raise_for_status()
    body = r.text
    assert "Recent frames from this peer" in body
    assert 'hx-get="/openlcb/nodes/0x222/frames"' in body


async def test_openlcb_health_refresh_returns_fragment(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/health/refresh")
    r.raise_for_status()
    body = r.text
    assert 'id="openlcb-health"' in body
    assert "Bus health" in body
    assert "<html" not in body   # fragment only, no page chrome


async def test_openlcb_home_shows_identity_card(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "Our identity (SNIP)" in body
    assert 'name="user_name"' in body
    assert 'name="user_description"' in body


async def test_openlcb_identity_update_replaces_and_reflects(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.post(
        "/openlcb/identity",
        data={
            "user_name": "web-set-name",
            "user_description": "typed in the browser",
        },
    )
    r.raise_for_status()
    body = r.text
    # Fragment renders the new values pre-filled in the form.
    assert 'value="web-set-name"' in body
    assert 'value="typed in the browser"' in body


async def test_openlcb_roster_export_returns_json_attachment(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/roster.json")
    r.raise_for_status()
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    import json  # noqa: PLC0415
    body = json.loads(r.text)
    assert body["version"] == 1
    assert "exported_at" in body
    assert isinstance(body["nodes"], list)


async def test_openlcb_roster_import_uploads_and_reports_counts(olcb_web) -> None:
    tc, _, _ = olcb_web
    import io, json  # noqa: PLC0415
    payload = {
        "version": 1,
        "nodes": [
            {
                "alias": "0x555",
                "node_id": "02.05.05.05.05.05",
                "snip": {
                    "manufacturer": "Acme",
                    "model": "R",
                    "hardware_version": "1",
                    "software_version": "1",
                    "user_name": "",
                    "user_description": "",
                },
                "pip": {"raw": "0x001000", "names": ["SNIP"]},
                "produced_events": [],
                "consumed_events": [],
            },
        ],
    }
    r = await tc.post(
        "/openlcb/roster/import",
        files={
            "file": (
                "roster.json",
                io.BytesIO(json.dumps(payload).encode()),
                "application/json",
            ),
        },
    )
    r.raise_for_status()
    body = r.text
    assert "imported:" in body
    assert "1 SNIP" in body
    assert "1 PIP" in body


async def test_openlcb_home_shows_roster_export_import(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "Roster export / import" in body
    assert 'href="/openlcb/roster.json"' in body
    assert 'hx-post="/openlcb/roster/import"' in body


async def test_openlcb_home_shows_health_sweep_button(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "Health sweep" in body
    assert 'hx-post="/openlcb/health_sweep"' in body
    # Result target present so the swap has something to replace.
    assert 'id="openlcb-health-sweep"' in body


async def test_openlcb_health_sweep_returns_empty_notice(olcb_web) -> None:
    """Fresh connection has no known peers -> sweep returns the
    empty-state notice pointing the operator at Refresh first."""
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/health_sweep")
    r.raise_for_status()
    body = r.text
    assert "Bus-wide health" in body
    assert "No peers known" in body


async def test_openlcb_home_shows_reset_roster_button(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    assert "Reset roster" in r.text


async def test_openlcb_prune_all_drops_fresh_peers(olcb_web) -> None:
    """Fresh peer (not stale by time) still gets dropped by prune_all,
    unlike prune (stale sweep). The notice lists the dropped alias."""
    tc, peer, _ = olcb_web
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.05)

    r = await tc.post("/openlcb/prune_all")
    r.raise_for_status()
    body = r.text
    assert "Reset roster" in body
    assert "0x222" in body


async def test_openlcb_home_shows_prune_now_button(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    assert "Prune now" in r.text


async def test_openlcb_prune_reports_pruned_aliases(olcb_web) -> None:
    """POST /openlcb/prune runs an immediate sweep and renders the
    discovery fragment with a notice listing what was dropped."""
    tc, peer, conn = olcb_web
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=b"\x02\x02\x02\x02\x02\x02",
    ))
    await asyncio.sleep(0.05)
    # Age the tracker so the sweep considers 0x222 stale.
    conn.last_seen._last_seen[0x222] = 0.0

    r = await tc.post("/openlcb/prune")
    r.raise_for_status()
    body = r.text
    assert "Pruned 1 peer" in body
    assert "0x222" in body


async def test_openlcb_prune_no_stale_notice(olcb_web) -> None:
    """Fresh connection with no stale peers -> the notice reports
    nothing to prune (still a successful sweep)."""
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/prune")
    r.raise_for_status()
    assert "No stale peers" in r.text


async def test_openlcb_discover_broadcasts_and_populates(olcb_web) -> None:
    """POST /openlcb/discover fires the broadcast + returns the
    immediate table snapshot (which may be empty since replies flow
    in asynchronously). The reply-arrived state is visible on the
    next GET /openlcb after a short settle."""
    tc, peer, conn = olcb_web
    responder = VerifyNodeIdService(peer, NodeId.parse("02.02.02.02.02.02"))
    try:
        r = await tc.post("/openlcb/discover")
        r.raise_for_status()
        # New two-stage discovery: broadcast + probe_all in one click,
        # notice reports counts.
        assert "Discovered" in r.text
        assert "probed identity" in r.text
        # And the discovered peer already lives in the returned
        # fragment (probe_all populated the roster inline).
        assert "0x222" in r.text
        assert "02.02.02.02.02.02" in r.text
    finally:
        responder.close()


# ---- node detail page --------------------------------------------------


async def test_node_detail_renders_with_cached_data(olcb_web) -> None:
    """When we already know a node's NodeID (heard via Verified NID),
    the detail page shows it without needing to probe."""
    tc, peer, conn = olcb_web
    # Pre-populate the lookup by sending an unsolicited Verified NID.
    from jmri_openlcb.codec import FT_MTI, OlcbFrame, _pack_header  # noqa: PLC0415
    from jmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    nid = NodeId.parse("02.02.02.02.02.02")
    await peer.send_frame(OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
        data=nid.bytes,
    ))
    await asyncio.sleep(0)

    r = await tc.get("/openlcb/nodes/0x222")
    r.raise_for_status()
    body = r.text
    assert "0x222" in body
    assert "02.02.02.02.02.02" in body
    # No SNIP data yet -- the probe placeholder is visible.
    assert "not probed yet" in body
    # Last-heard row shows a recent age (the Verified NID we just
    # sent lands under a second ago).
    assert "Last heard" in body
    assert "s ago" in body


async def test_node_probe_renders_snip_and_pip(olcb_web) -> None:
    """POST /openlcb/nodes/{alias}/probe triggers both SNIP + PIP
    against a peer with responders -> the fragment shows both
    populated."""
    tc, peer, _ = olcb_web
    info = NodeInfo(
        manufacturer="Acme", model="Widget-3",
        hardware_version="rev C", software_version="7.7",
        user_name="test-node", user_description="on the bench",
    )
    snip_svc = SnipResponder(peer, info)
    pip_svc = PipService(
        peer, ProtocolFlag.SNIP | ProtocolFlag.CDI | ProtocolFlag.DATAGRAM,
    )
    try:
        r = await tc.post("/openlcb/nodes/0x222/probe")
        r.raise_for_status()
        body = r.text
        assert "Acme" in body
        assert "Widget-3" in body
        assert "test-node" in body
        assert "on the bench" in body
        # PIP flag names visible as badges.
        assert "SNIP" in body
        assert "CDI" in body
        assert "DATAGRAM" in body
    finally:
        pip_svc.close()
        snip_svc.close()


CDI_XML = """\
<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>Acme</manufacturer>
    <model>Widget-CDI</model>
    <hardwareVersion>rev X</hardwareVersion>
    <softwareVersion>0.0.1</softwareVersion>
  </identification>
  <segment origin="0" space="0xFE">
    <name>User Config</name>
    <int size="1">
      <name>Turnout count</name>
      <default>4</default>
    </int>
    <string size="8">
      <name>Label</name>
    </string>
  </segment>
</cdi>
"""


async def _wire_cdi_peer(peer):
    """Attach a datagram engine + fake memory device on the peer
    side so a client's cdi_fetch / read / write actually gets an
    answer."""
    import pathlib, sys   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415
    from jmri_openlcb import SPACE_CDI  # noqa: PLC0415
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    engine = DatagramEngine(peer)
    await engine.attach()
    device = _FakeMemoryDevice(engine, peer)
    payload = CDI_XML.encode("utf-8") + b"\x00"
    for i, byte in enumerate(payload):
        device.store[(SPACE_CDI, i)] = byte
    return device, engine, olcb_tests


async def test_cdi_editor_page_renders_tree(olcb_web) -> None:
    """CDI editor page fetches the tree and renders identification +
    fields with inputs + Save/Read buttons."""
    tc, peer, _ = olcb_web
    device, engine, olcb_tests = await _wire_cdi_peer(peer)
    try:
        r = await tc.get("/openlcb/nodes/0x222/cdi")
        r.raise_for_status()
        body = r.text
        # Identification.
        assert "Acme" in body
        assert "Widget-CDI" in body
        # Field labels.
        assert "Turnout count" in body
        assert "Label" in body
        # Segment metadata is visible.
        assert "User Config" in body
        # Read + Save buttons wire to the fragment endpoints.
        assert "/openlcb/nodes/0x222/cdi/read" in body
        assert "/openlcb/nodes/0x222/cdi/write" in body
    finally:
        device.close()
        await engine.detach()
        import sys   # noqa: PLC0415
        sys.path.remove(str(olcb_tests))


async def test_cdi_page_shows_reboot_and_factory_reset_buttons(olcb_web) -> None:
    """After the CDI tree renders, the Node lifecycle card shows the
    Reboot + Factory reset buttons wired to the right POST endpoints
    and with confirmation prompts (the destructive one gets a
    special warning)."""
    tc, peer, _ = olcb_web
    device, engine, olcb_tests = await _wire_cdi_peer(peer)
    try:
        r = await tc.get("/openlcb/nodes/0x222/cdi")
        r.raise_for_status()
        body = r.text
        assert "Node lifecycle" in body
        assert "Reboot node" in body
        assert "Factory reset" in body
        assert 'hx-post="/openlcb/nodes/0x222/reboot"' in body
        assert 'hx-post="/openlcb/nodes/0x222/factory_reset"' in body
        # The destructive button confirms with a specific warning.
        assert "CANNOT be undone" in body
    finally:
        device.close()
        await engine.detach()
        import sys   # noqa: PLC0415
        sys.path.remove(str(olcb_tests))


async def test_node_reboot_endpoint_returns_ok_notice(olcb_web) -> None:
    """POST /openlcb/nodes/{alias}/reboot dispatches to the memory
    lifecycle service action and returns a green notice on success."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import SPACE_CDI  # noqa: PLC0415
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    from jmri_openlcb.memory import MemoryConfigResponder  # noqa: PLC0415
    engine = DatagramEngine(peer)
    await engine.attach()
    # A peer responder acks the datagram so the send doesn't time out.
    responder = MemoryConfigResponder(engine)
    responder.set_space(SPACE_CDI, b"")
    try:
        r = await tc.post("/openlcb/nodes/0x222/reboot")
        r.raise_for_status()
        assert "reboot sent" in r.text
    finally:
        responder.close()
        await engine.detach()


async def test_node_factory_reset_refuses_without_cached_nid(olcb_web) -> None:
    """Factory reset needs the target's NodeID as a safety payload.
    If we don't have it cached, the route refuses and points the
    operator at Discover."""
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/nodes/0x555/factory_reset")
    r.raise_for_status()
    # We haven't discovered 0x555, so its NodeID is None -> refusal.
    assert "refused" in r.text
    assert "Discover" in r.text


async def test_cdi_read_fragment_returns_current_value(olcb_web) -> None:
    tc, peer, _ = olcb_web
    device, engine, olcb_tests = await _wire_cdi_peer(peer)
    try:
        from jmri_openlcb import SPACE_CONFIG   # noqa: PLC0415
        device.store[(SPACE_CONFIG, 0)] = 9
        r = await tc.get(
            "/openlcb/nodes/0x222/cdi/read",
            params={"space": 0xFE, "offset": 0, "size": 1, "type": "int"},
        )
        r.raise_for_status()
        # The read fragment is a plain span with the value text.
        assert ">9<" in r.text
    finally:
        device.close()
        await engine.detach()
        import sys   # noqa: PLC0415
        sys.path.remove(str(olcb_tests))


async def test_cdi_write_fragment_updates_and_confirms(olcb_web) -> None:
    tc, peer, _ = olcb_web
    device, engine, olcb_tests = await _wire_cdi_peer(peer)
    try:
        from jmri_openlcb import SPACE_CONFIG   # noqa: PLC0415
        r = await tc.post(
            "/openlcb/nodes/0x222/cdi/write",
            data={
                "space": 0xFE, "offset": 0, "size": 1, "type": "int",
                "value": "42",
            },
        )
        r.raise_for_status()
        assert "saved" in r.text
        assert device.store[(SPACE_CONFIG, 0)] == 42
    finally:
        device.close()
        await engine.detach()
        import sys   # noqa: PLC0415
        sys.path.remove(str(olcb_tests))


async def test_cdi_write_string_round_trips(olcb_web) -> None:
    """A string write pads the value to the declared size with
    nulls; the peer's memory shows the padded bytes."""
    tc, peer, _ = olcb_web
    device, engine, olcb_tests = await _wire_cdi_peer(peer)
    try:
        from jmri_openlcb import SPACE_CONFIG   # noqa: PLC0415
        r = await tc.post(
            "/openlcb/nodes/0x222/cdi/write",
            data={
                "space": 0xFE, "offset": 1, "size": 8, "type": "string",
                "value": "hi",
            },
        )
        r.raise_for_status()
        got = bytes(
            device.store.get((SPACE_CONFIG, 1 + i), 0) for i in range(8)
        )
        assert got == b"hi" + b"\x00" * 6
    finally:
        device.close()
        await engine.detach()
        import sys   # noqa: PLC0415
        sys.path.remove(str(olcb_tests))


FDI_XML = """\
<?xml version="1.0"?>
<fdi>
  <segment>
    <group>
      <name>Motion</name>
      <function size="1" kind="binary"><name>Headlight</name><number>0</number></function>
      <function size="1" kind="binary">
        <name>Horn</name><number>2</number><momentary/>
      </function>
    </group>
  </segment>
</fdi>
"""


async def _wire_fdi_peer(peer):
    import pathlib, sys   # noqa: PLC0415
    olcb_tests = pathlib.Path(__file__).resolve().parents[3] / "jmri-openlcb"
    sys.path.insert(0, str(olcb_tests))
    from tests.unit.test_memory import _FakeMemoryDevice  # noqa: PLC0415
    from jmri_openlcb import SPACE_FDI  # noqa: PLC0415
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    engine = DatagramEngine(peer)
    await engine.attach()
    device = _FakeMemoryDevice(engine, peer)
    payload = FDI_XML.encode("utf-8") + b"\x00"
    for i, byte in enumerate(payload):
        device.store[(SPACE_FDI, i)] = byte
    return device, engine, olcb_tests


async def test_fdi_page_renders_labeled_functions(olcb_web) -> None:
    tc, peer, _ = olcb_web
    device, engine, olcb_tests = await _wire_fdi_peer(peer)
    try:
        r = await tc.get("/openlcb/nodes/0x222/fdi")
        r.raise_for_status()
        body = r.text
        # Group heading + all function labels.
        assert "Motion" in body
        assert "Headlight" in body
        assert "Horn" in body
        # Momentary badge visible on the horn row.
        assert "momentary" in body
        # F# column present.
        assert "F0" in body
        assert "F2" in body
    finally:
        device.close()
        await engine.detach()
        import sys   # noqa: PLC0415
        sys.path.remove(str(olcb_tests))


async def test_fdi_page_error_banner_when_no_fdi_on_peer(olcb_web) -> None:
    """Peer with no FDI in memory space -> fetch times out; page
    still renders (with a friendly banner explaining that not
    every loco publishes FDI)."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/nodes/0x222/fdi")
    # 504 from the service is the expected shape.
    assert r.status_code == 504
    body = r.text
    assert "FDI fetch failed" in body
    assert "Not every loco publishes FDI" in body


def test_fdi_page_renders_error_on_non_olcb(client) -> None:
    """Non-OpenLCB connection -> 400 with the banner rendered."""
    r = client.get("/openlcb/nodes/0x222/fdi")
    assert r.status_code == 400
    assert "FDI fetch failed" in r.text


def test_node_detail_page_links_to_fdi_viewer(client) -> None:
    """The node detail page's action row links to /fdi so operators
    can find the viewer without knowing the URL."""
    r = client.get("/openlcb/nodes/0x222")
    assert "/openlcb/nodes/0x222/fdi" in r.text


async def test_cs_add_form_returns_error_banner_when_no_cs(olcb_web) -> None:
    """No CS attached -> POST /openlcb/cs/add renders the hosted
    table fragment with an inline error banner instead of a raw 400.
    Keeps the form usable -- the operator can see what went wrong
    and try again without losing context."""
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/cs/add", data={
        "address": 3, "long_address": "",
    })
    r.raise_for_status()
    body = r.text
    assert "Hosted-train action failed" in body
    assert "command station" in body.lower()


async def test_cs_add_and_remove_via_form_round_trips(olcb_web_handle) -> None:
    """End-to-end: add via form, verify hosted row, remove via row
    button, verify empty. Uses the extended fixture that yields the
    web + server handles so we can wire the CS onto ctx."""
    tc, _, conn, handle = olcb_web_handle
    from jmri_openlcb.command_station import CommandStationNode  # noqa: PLC0415
    from jmri_openlcb.alias import NodeId   # noqa: PLC0415
    from jmri_core.beans.throttle_manager import ThrottleManager  # noqa: PLC0415
    dcc = ThrottleManager("Y")
    cs = CommandStationNode(
        conn.traffic, dcc,
        station_node_id=NodeId.parse("02.01.12.FE.DC.BA"),
    )
    handle.server.ctx._openlcb_cs = cs
    try:
        # Add via form.
        r = await tc.post("/openlcb/cs/add", data={
            "address": 3, "long_address": "",
        })
        r.raise_for_status()
        assert "DCC 3" in r.text
        assert "Remove" in r.text
        # Confirm via the JSON list.
        r = await tc.post("/json/", json={
            "type": "openlcbCS", "method": "list",
        })
        assert len(r.json()["data"]) == 1
        # Remove via form.
        r = await tc.post("/openlcb/cs/remove", data={
            "address": 3, "long_address": 0,
        })
        r.raise_for_status()
        assert "DCC 3" not in r.text
        assert "No hosted trains" in r.text
    finally:
        cs.close()
        handle.server.ctx._openlcb_cs = None


async def test_monitor_page_renders_captured_frames(olcb_web) -> None:
    """End-to-end: peer emits a PCER; /openlcb/monitor shows it with
    a PCER summary line and the source alias link back to the
    node-detail page."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "OpenLCB bus monitor" in body
    assert "PCER" in body
    # The alias-to-detail link is present.
    assert "/openlcb/nodes/0x222" in body


async def test_monitor_clear_empties_the_table(olcb_web) -> None:
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.post("/openlcb/monitor/clear")
    r.raise_for_status()
    assert "No frames captured" in r.text


async def test_monitor_page_shows_bus_stats(olcb_web) -> None:
    """The monitor page renders a Bus stats card with the top-talker
    peer visible after some traffic lands."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    for i in range(3):
        await peer.produce_event(EventId(0x0501010100000000 | i))
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "Bus stats" in body
    assert "Top talkers" in body
    assert "0x222" in body
    assert "PCER" in body


async def test_monitor_stats_fragment_returns_html(olcb_web) -> None:
    """The dedicated /openlcb/monitor/stats route (used by the htmx
    poll on the stats card) returns just the fragment."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor/stats")
    r.raise_for_status()
    assert 'id="openlcb-stats"' in r.text


async def test_send_frame_form_puts_frame_on_wire(olcb_web) -> None:
    """Form submits a hand-crafted PCER header + event ID; the peer's
    traffic controller receives the frame."""
    tc, peer, _ = olcb_web
    import asyncio  # noqa: PLC0415
    received: list = []
    peer.frames.connect(received.append)
    r = await tc.post(
        "/openlcb/monitor/send",
        data={"header": "195B4123", "data": "0501010100000042"},
    )
    r.raise_for_status()
    # Inline confirmation shows the hex we sent.
    assert "sent" in r.text
    assert "0x195b4123" in r.text.lower()
    # Wire delivery lands.
    await asyncio.sleep(0.05)
    assert any(f.header == 0x195B4123 for f in received)


async def test_send_frame_form_shows_error_on_bad_header(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.post(
        "/openlcb/monitor/send",
        data={"header": "not-hex", "data": ""},
    )
    r.raise_for_status()
    # Renders the error inline in red.
    assert "must be hex" in r.text or "not a valid hex" in r.text.lower()
    assert "c33" in r.text  # error color CSS var


async def test_monitor_page_shows_send_form(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "Send a frame" in body
    assert "/openlcb/monitor/send" in body


async def test_monitor_page_shows_send_frame_presets(olcb_web) -> None:
    """The monitor page renders one-click preset buttons above the
    send form. Header + data live in data-* attributes so the JS
    can stuff them into the form on click."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    # All four canonical presets rendered as buttons.
    assert "PCER (example event)" in body
    assert "Verify NID Global" in body
    assert "Identify Events Global" in body
    assert "AME (global)" in body
    # Well-known emergency presets from #333.
    assert "Emergency Off" in body
    assert "Clear Emergency Off" in body
    assert "Emergency Stop of All Trains" in body
    # Reserved event ID appears in a data-body attribute somewhere.
    assert "010000000000fffe" in body.lower()
    # Alias appears in the encoded header (fixture uses 0x111).
    assert "0x195b4111" in body.lower()
    # JS hook is present.
    assert "olcbFillFrame" in body


async def test_monitor_row_has_replay_button_with_captured_frame(
    olcb_web,
) -> None:
    """Each frame row in the log gets a Replay button whose data
    attributes carry the captured header + payload, so a click
    populates the Send form."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    ev = EventId.parse("05.01.01.01.4C.43.00.42")
    await peer.produce_event(ev)
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    # Replay button visible in the frame row (title text is unique
    # to this button so it's a safer substring than "Replay" alone).
    assert "Load this frame into the Send form above" in body
    # The captured PCER header for source alias 0x222 is 0x195B4222.
    assert 'data-header="0x195b4222"' in body.lower()
    # And the event ID is present as the row's data.
    assert 'data-body="050101014c430042"' in body.lower()
    # The click handler scrolls to the send form.
    assert "openlcb-send-form" in body


async def test_monitor_page_shows_our_alias(olcb_web) -> None:
    """The Recent frames header includes an ``us = 0x111`` hint so
    the operator knows which alias the monitor treats as local."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "us =" in body
    assert "0x111" in body


async def test_monitor_row_flags_our_own_frame(olcb_web) -> None:
    """A frame we sent (source_alias == our alias) gets the "us"
    badge next to its src cell."""
    tc, _, _ = olcb_web
    import asyncio  # noqa: PLC0415
    # Send a PCER from our alias so a "us" frame lands in the log.
    r = await tc.post(
        "/openlcb/monitor/send",
        data={
            "header": "0x195B4111",
            "data": "0501010100000042",
        },
    )
    r.raise_for_status()
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor/refresh")
    r.raise_for_status()
    body = r.text
    # The us badge appears somewhere in the fragment.
    assert 'class="badge"' in body
    assert ">us<" in body


async def test_monitor_filter_form_has_invert_checkboxes(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert 'name="src_invert"' in body
    assert 'name="mti_invert"' in body
    assert 'name="text_invert"' in body


async def test_monitor_refresh_honours_alias_invert(olcb_web) -> None:
    """?src=222&src_invert=on hides that peer instead of showing
    only it. Sanity-checks the wire-through from URL param to
    service filter."""
    tc, peer, _ = olcb_web
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
    r = await tc.get("/openlcb/monitor/refresh?src=222&src_invert=on")
    r.raise_for_status()
    body = r.text
    assert "0x333" in body
    # 0x222 is hidden by the inverted filter.
    assert "0x222" not in body


async def test_monitor_row_carries_details_drill_down(olcb_web) -> None:
    """Each frame row is followed by a hidden details row rendered
    with the decoded fields (event ID, well-known name, MTI name).
    JS toggles visibility on row click."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("01.00.00.00.00.00.FF.FE"))
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    # Row itself is clickable + carries the toggle handler.
    assert 'onclick="olcbToggleDetails(this)"' in body
    # Hidden details row emitted right after the frame row.
    assert 'class="openlcb-frame-details" style="display:none"' in body
    # Decoded fields land in the details row.
    assert "Emergency Off" in body
    assert "01.00.00.00.00.00.FF.FE" in body
    assert "OpenLCB MTI" in body
    # JS helper defined.
    assert "olcbToggleDetails" in body


async def test_monitor_row_has_filter_to_alias_affordance(olcb_web) -> None:
    """Each frame row gets an [only] link next to the src alias
    that (via JS) fills the src filter and triggers a re-fetch.
    Verifies the anchor + data attribute + JS hook are present."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    # Anchor with data-alias + onclick that calls olcbFilterToAlias
    # (stopPropagation() prefix keeps the row drill-down from
    # toggling on the same click).
    assert "olcbFilterToAlias(event, this)" in body
    assert 'data-alias="0x222"' in body
    assert "[only]" in body
    # The JS helper is defined.
    assert "olcbFilterToAlias" in body


async def test_monitor_page_shows_well_known_event_dropdown(olcb_web) -> None:
    """The Well-known dropdown renders inside the send-frame form
    with S-9.7.3 event names as options and the local alias in a
    data attribute so the JS handler can build the PCER header."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert 'id="openlcb-well-known-select"' in body
    assert 'data-alias="0x111"' in body   # fixture's local alias
    # A few of the reserved event names.
    assert "Emergency Off" in body
    assert "Duplicate Node ID Detected" in body
    # And the JS helper.
    assert "olcbFillWellKnown" in body


async def test_monitor_send_history_populates_after_send(olcb_web) -> None:
    """After a send, the send-history fragment shows a resend button
    with the exact bytes."""
    tc, _, _ = olcb_web
    r = await tc.post(
        "/openlcb/monitor/send",
        data={"header": "0x195B4111", "data": "0501010100000042"},
    )
    r.raise_for_status()
    # And the response triggers a refresh event via HX-Trigger so
    # the panel updates without a page reload.
    assert r.headers.get("hx-trigger") == "openlcb-send-ok"

    r = await tc.get("/openlcb/monitor/send_history")
    r.raise_for_status()
    body = r.text
    assert "Recent sends" in body
    assert "0x195b4111" in body.lower()
    # Data hex appears in a data-body attribute on the resend button.
    assert "0501010100000042" in body.lower()


async def test_monitor_page_includes_send_history_container(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert 'id="openlcb-send-history"' in body
    # Auto-refresh wired via the HX-Trigger custom event.
    assert 'hx-trigger="openlcb-send-ok from:body"' in body


async def test_monitor_send_datagram_form_present(olcb_web) -> None:
    """The Send a datagram card renders on the monitor page with a
    form posting to /openlcb/monitor/send_datagram."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "Send a datagram" in body
    assert 'hx-post="/openlcb/monitor/send_datagram"' in body
    # 72-byte cap mentioned so the operator sees the limit up front.
    assert "72 bytes" in body


async def test_monitor_send_datagram_delivers_and_reports_ok(olcb_web) -> None:
    """POST /openlcb/monitor/send_datagram delivers to the peer and
    the response fragment says the byte count + Received OK."""
    tc, peer, _ = olcb_web
    from jmri_openlcb.datagrams import DatagramEngine  # noqa: PLC0415
    peer_engine = DatagramEngine(peer)
    await peer_engine.attach()
    try:
        r = await tc.post(
            "/openlcb/monitor/send_datagram",
            data={"alias": "0x222", "data": "20 82", "timeout": "1"},
        )
        r.raise_for_status()
        body = r.text
        assert "Received OK" in body
        assert "2 byte" in body
    finally:
        await peer_engine.detach()


async def test_monitor_toggle_pause_flips_label(olcb_web) -> None:
    """Clicking the pause button flips the button's label to Resume
    and stops the monitor from capturing new frames. Clicking again
    resumes."""
    tc, _, _ = olcb_web
    # Initially not paused -> button shows "Pause".
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert ">\n  Pause\n" in body or ">Pause<" in body or "Pause" in body
    # Not resumed yet -- no "Resume" text.
    assert "Resume" not in body

    # Toggle once -> fragment shows "Resume".
    r = await tc.post("/openlcb/monitor/toggle_pause")
    r.raise_for_status()
    assert "Resume" in r.text
    assert "Pause" not in r.text or r.text.count("Pause") == 0

    # Toggle again -> back to "Pause".
    r = await tc.post("/openlcb/monitor/toggle_pause")
    r.raise_for_status()
    assert "Pause" in r.text


async def test_monitor_export_csv_returns_headered_rows(olcb_web) -> None:
    """/openlcb/monitor/export.csv returns a text/csv attachment with
    a header row and one row per captured frame."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor/export.csv")
    r.raise_for_status()
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    lines = [ln for ln in body.splitlines() if ln]
    assert lines[0] == (
        "timestamp,source_alias,frame_type,header,summary,data_hex"
    )
    # At least the PCER row is present.
    assert any("PCER" in ln for ln in lines[1:])
    assert any("0x222" in ln for ln in lines[1:])


async def test_monitor_sse_generator_pushes_refresh_on_frame(olcb_web) -> None:
    """Drive the SSE endpoint's event generator directly (bypassing
    the httpx streaming layer, which buffers under TestClient) so we
    can observe the initial priming nudge and a second nudge on
    inbound peer traffic without racing an ASGI middleware."""
    tc, peer, _ = olcb_web
    import asyncio  # noqa: PLC0415
    from jmri_openlcb import EventId  # noqa: PLC0415

    # Reach into the FastAPI route table for the SSE route's
    # generator entry point + a fake Request that never disconnects.
    app = tc._transport.app  # type: ignore[attr-defined]
    route = next(
        r for r in app.routes if getattr(r, "path", None)
        == "/openlcb/monitor/sse"
    )

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    resp = await route.endpoint(_FakeRequest())  # type: ignore[arg-type]
    it = resp.body_iterator
    # First yield is the priming nudge.
    first = await asyncio.wait_for(anext(it), timeout=1.0)
    assert "data: refresh" in first
    # Peer traffic -> next nudge.
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    second = await asyncio.wait_for(anext(it), timeout=2.0)
    assert "data: refresh" in second
    # Clean up the generator.
    await it.aclose()


async def test_monitor_page_wires_sse_and_polling_fallback(olcb_web) -> None:
    """Home page renders both the SSE subscription (via inline
    script) and the every-2s polling trigger -- the poll remains as
    a safety net when SSE isn't available."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert 'EventSource("/openlcb/monitor/sse")' in body
    assert "olcb-sse-nudge from:body" in body
    # Fallback poll still present.
    assert 'hx-trigger="every 2s, olcb-sse-nudge from:body"' in body


async def test_monitor_export_session_returns_zip_with_csv_and_metadata(olcb_web) -> None:
    """/openlcb/monitor/export_session.zip returns a ZIP archive
    with frames.csv (same layout as the plain CSV export) and
    metadata.json capturing filter state + bus snapshot."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio, io, json, zipfile  # noqa: PLC0415
    await peer.produce_event(EventId.parse("05.01.01.01.4C.43.00.01"))
    await asyncio.sleep(0.05)
    r = await tc.get(
        "/openlcb/monitor/export_session.zip",
        params={"src": "222"},
    )
    r.raise_for_status()
    assert r.headers["content-type"] == "application/zip"
    assert "openlcb-session-" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert names == {"frames.csv", "metadata.json"}
    csv_body = zf.read("frames.csv").decode()
    assert csv_body.startswith(
        "timestamp,source_alias,frame_type,header,summary,data_hex",
    )
    meta = json.loads(zf.read("metadata.json"))
    # Filter state round-trips into metadata.
    assert meta["filter"]["source_alias"] == "222"
    assert meta["filter"]["source_alias_invert"] is False
    # Metadata captures our alias + frame count for the export.
    assert meta["our_alias"] == "0x111"
    assert meta["frame_count"] >= 1
    assert "exported_at" in meta
    assert "bus_stats" in meta


async def test_monitor_page_has_download_session_button(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "Download session" in body
    assert "olcbExportSession" in body


async def test_monitor_export_csv_honours_alias_filter(olcb_web) -> None:
    """Filter narrows the exported rows to the matching alias."""
    tc, peer, _ = olcb_web
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
    r = await tc.get("/openlcb/monitor/export.csv?src=222")
    r.raise_for_status()
    body = r.text
    assert "0x222" in body
    assert "0x333" not in body


async def test_monitor_page_has_download_csv_button(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "Download CSV" in body
    assert "olcbExportCsv" in body


async def test_monitor_refresh_honours_alias_filter(olcb_web) -> None:
    """Passing ?src=222 to /openlcb/monitor/refresh narrows to
    frames from alias 0x222. Uses two peer aliases synthesized by
    sending Verified NID frames with different source aliases."""
    tc, peer, _ = olcb_web
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
    r = await tc.get("/openlcb/monitor/refresh?src=222")
    r.raise_for_status()
    body = r.text
    assert "0x222" in body
    # 0x333 rendered in the alias column would show as "0x333";
    # verify the fragment doesn't include it.
    assert "0x333" not in body


async def test_monitor_limit_narrows_returned_rows(olcb_web) -> None:
    """?limit=1 on the refresh endpoint returns at most one row, no
    matter how many frames the ring holds."""
    tc, peer, _ = olcb_web
    from jmri_openlcb import EventId  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    for i in range(5):
        await peer.produce_event(
            EventId.parse(f"05.01.01.01.4C.43.00.0{i}"),
        )
    await asyncio.sleep(0.05)
    r = await tc.get("/openlcb/monitor/refresh?limit=1")
    r.raise_for_status()
    # Count frame rows in <tbody> -- every visible row is followed by
    # a hidden details row for the drill-down, so we filter on the
    # class the frame row carries.
    body = r.text
    tbody_start = body.find("<tbody>")
    tbody_end = body.find("</tbody>")
    assert tbody_start != -1 and tbody_end != -1
    tbody = body[tbody_start:tbody_end]
    assert tbody.count('class="openlcb-frame-row"') == 1


async def test_monitor_home_renders_limit_select_with_default(olcb_web) -> None:
    """Filter row includes a limit <select> with the default value
    (100) selected."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert 'name="limit"' in body
    assert '<option value="100" selected>100</option>' in body
    # Also renders the other choices.
    assert '<option value="25"' in body
    assert '<option value="500"' in body


async def test_monitor_home_renders_filter_form(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    # All three filter inputs present.
    assert 'name="src"' in body
    assert 'name="mti"' in body
    assert 'name="text"' in body
    # Filter form id used by hx-include hooks.
    assert 'id="openlcb-monitor-filter"' in body


async def test_monitor_refresh_returns_fragment(olcb_web) -> None:
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb/monitor/refresh")
    r.raise_for_status()
    # Fragment root id -- matches the htmx swap target on the page.
    assert 'id="openlcb-monitor"' in r.text


def test_monitor_page_shows_warning_on_non_olcb(client) -> None:
    r = client.get("/openlcb/monitor")
    r.raise_for_status()
    body = r.text
    assert "OpenLCB bus monitor" in body
    assert "isn't OpenLCB" in body


async def test_openlcb_home_links_to_monitor(olcb_web) -> None:
    """When OpenLCB is active, the main /openlcb page has a link
    to the monitor sub-page. On a non-OpenLCB connection the link
    is hidden along with the whole discovery UI (verified by the
    existing warning-banner test)."""
    tc, _, _ = olcb_web
    r = await tc.get("/openlcb")
    r.raise_for_status()
    assert "/openlcb/monitor" in r.text


def test_cdi_editor_page_renders_error_on_non_olcb(client) -> None:
    """Without an OpenLCB connection, the CDI page still renders
    (with the error banner) rather than a bare stack trace. Uses
    the sync TestClient since no async loop coordination is needed."""
    r = client.get("/openlcb/nodes/0x222/cdi")
    assert r.status_code == 400
    body = r.text
    assert "CDI editor" in body
    assert "not OpenLCB" in body


async def test_node_probe_shows_error_notice_on_timeout(olcb_web) -> None:
    """A probe with no peer responder -> SNIP + PIP both time out;
    the fragment renders with the probe_error banner rather than
    exploding into a 500."""
    tc, _, _ = olcb_web
    r = await tc.post("/openlcb/nodes/0x222/probe")
    # Either service errors -> the route surfaces it as 504 (from the
    # service layer), which propagates as an HTTPException. The web
    # route catches it and re-renders. Depending on which probe fires
    # first, HTTPException may bubble; check either the rendered
    # error notice or the 504 status code.
    if r.status_code == 200:
        assert "Probe failed" in r.text
    else:
        assert r.status_code == 504
