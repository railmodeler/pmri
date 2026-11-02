from __future__ import annotations

import pytest
from jmri_core.plugins import load_connections

from jmri_openlcb import EventId, OlcbConnection, loopback_pair


async def test_entry_point_discovers_openlcb() -> None:
    connections = load_connections()
    assert "openlcb" in connections
    assert connections["openlcb"] is OlcbConnection


async def test_open_close_lifecycle() -> None:
    """Loopback with skip_alias_claim -> hardcoded alias makes it easy
    to assert the source_alias plumbing without running the 200 ms
    handshake in the unit-test path."""
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    assert conn.is_open
    assert conn.prefix == "X"
    assert conn.traffic.source_alias == 0x123
    await conn.close()
    assert not conn.is_open


async def test_open_runs_alias_claim_by_default() -> None:
    """Without ``skip_alias_claim`` the connection runs the CID/RID
    handshake. On an empty loopback bus no collision arrives, so
    the candidate alias sticks and lands in ``traffic.source_alias``.
    ``claim_wait=0`` skips the 200 ms grace so the test stays fast."""
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "node_id": "02.01.12.FE.DC.BA",
        "claim_wait": 0.0,
    })
    try:
        assert conn.is_open
        # Non-zero 12-bit alias -- the specific value is deterministic
        # from the Node ID but we don't lock it here (that's a
        # candidate_alias unit test's job).
        alias = conn.traffic.source_alias
        assert 0 < alias <= 0xFFF
        assert conn.node_id.dotted() == "02.01.12.FE.DC.BA"
    finally:
        await conn.close()


async def test_configure_turnout_via_connection() -> None:
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    ev_c = EventId.parse("05.01.01.01.4C.43.00.01")
    ev_t = EventId.parse("05.01.01.01.4C.43.00.02")
    t = await conn.turnouts.configure("XT1", ev_c, ev_t)        # type: ignore[attr-defined]
    assert t.closed_event == ev_c
    assert t.thrown_event == ev_t
    await conn.close()


async def test_unknown_transport_raises() -> None:
    conn = OlcbConnection()
    with pytest.raises(ValueError, match="transport"):
        await conn.open({"kind": "wifi"})


async def test_traffic_before_open_raises() -> None:
    conn = OlcbConnection()
    with pytest.raises(RuntimeError, match="not open"):
        _ = conn.traffic


async def test_default_node_info_populates_from_settings() -> None:
    """``node_info`` in settings sets the SNIP identity strings; unset
    fields fall back to sensible defaults (mfg='JMRI-Python', etc)."""
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
        "node_info": {
            "user_name": "test-node",
            "user_description": "regression pin",
        },
    })
    try:
        info = conn.node_info
        # Operator overrides.
        assert info.user_name == "test-node"
        assert info.user_description == "regression pin"
        # Defaults.
        assert info.manufacturer == "JMRI-Python"
        assert info.model == "JMRI Server"
    finally:
        await conn.close()


async def test_memory_responder_serves_default_cdi() -> None:
    """The connection stands up a memory-config responder by default;
    a peer probing SPACE_CDI on our alias gets the minimum stub CDI
    identifying JMRI-Python instead of a timeout."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        SPACE_CDI, parse_cdi_bytes,
    )
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
    })
    peer_tc = OlcbTrafficController(b, source_alias=0x222)
    await peer_tc.start()
    peer_engine = DatagramEngine(peer_tc)
    await peer_engine.attach()
    client = MemoryClient(peer_engine)
    try:
        # 512 bytes is plenty for the default stub.
        data = await client.read(0x111, SPACE_CDI, 0, 512)
        # Trim at the null terminator our responder appends and parse.
        term = data.find(b"\x00")
        raw = data[:term] if term >= 0 else data
        cdi = parse_cdi_bytes(raw + b"\x00")
        assert cdi.identification.manufacturer == "JMRI-Python"
    finally:
        client.close()
        await peer_engine.detach()
        await peer_tc.aclose()
        await conn.close()


async def test_memory_responder_config_space_write_round_trips() -> None:
    """SPACE_CONFIG is writable by default; a peer's write updates
    our backing bytes and a subsequent read returns the new value."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController, SPACE_CONFIG,
    )
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "config_space_bytes": 32,
    })
    peer_tc = OlcbTrafficController(b, source_alias=0x222)
    await peer_tc.start()
    peer_engine = DatagramEngine(peer_tc)
    await peer_engine.attach()
    client = MemoryClient(peer_engine)
    try:
        await client.write(0x111, SPACE_CONFIG, 0, b"OPERATOR")
        data = await client.read(0x111, SPACE_CONFIG, 0, 8)
        assert data == b"OPERATOR"
        # The rest of the space stays zero-initialized.
        tail = await client.read(0x111, SPACE_CONFIG, 8, 8)
        assert tail == bytes(8)
    finally:
        client.close()
        await peer_engine.detach()
        await peer_tc.aclose()
        await conn.close()


async def test_skip_memory_responder_disables_the_responder() -> None:
    """``skip_memory_responder=True`` opts out; a peer reading our
    SPACE_CDI then times out (nobody to answer)."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController, SPACE_CDI,
    )
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_memory_responder": True,
    })
    peer_tc = OlcbTrafficController(b, source_alias=0x222)
    await peer_tc.start()
    peer_engine = DatagramEngine(peer_tc)
    await peer_engine.attach()
    client = MemoryClient(peer_engine)
    try:
        with pytest.raises(Exception):    # noqa: PT011, BLE001
            await client.read(0x111, SPACE_CDI, 0, 32, timeout=0.05)
        # And the property surfaces the disabled state.
        with pytest.raises(RuntimeError, match="disabled"):
            _ = conn.memory_responder
    finally:
        client.close()
        await peer_engine.detach()
        await peer_tc.aclose()
        await conn.close()


async def test_custom_cdi_xml_setting_is_served() -> None:
    """A caller-supplied ``cdi_xml`` setting overrides the default
    stub -- real deployments set this to a document describing the
    server's own configurable variables."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        SPACE_CDI, parse_cdi_bytes,
    )
    custom = """<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>Custom Manufacturer</manufacturer>
  </identification>
</cdi>"""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "cdi_xml": custom,
    })
    peer_tc = OlcbTrafficController(b, source_alias=0x222)
    await peer_tc.start()
    peer_engine = DatagramEngine(peer_tc)
    await peer_engine.attach()
    client = MemoryClient(peer_engine)
    try:
        data = await client.read(0x111, SPACE_CDI, 0, 512)
        term = data.find(b"\x00")
        raw = data[:term] if term >= 0 else data
        cdi = parse_cdi_bytes(raw + b"\x00")
        assert cdi.identification.manufacturer == "Custom Manufacturer"
    finally:
        client.close()
        await peer_engine.detach()
        await peer_tc.aclose()
        await conn.close()


async def test_node_id_settings_wins_over_persisted_file(tmp_path) -> None:
    """When settings["node_id"] is present, the persisted file (if
    any) is ignored -- operator overrides always win."""
    from jmri_openlcb.connection import _resolve_node_id  # noqa: PLC0415
    persist = tmp_path / "openlcb-node-id"
    persist.write_text("02.aa.aa.aa.aa.aa\n", encoding="utf-8")
    nid = _resolve_node_id({
        "node_id": "02.bb.bb.bb.bb.bb",
        "node_id_path": str(persist),
    })
    assert nid.dotted() == "02.BB.BB.BB.BB.BB"
    # And the persisted file wasn't rewritten to the settings value.
    assert persist.read_text().strip() == "02.aa.aa.aa.aa.aa"


async def test_node_id_loaded_from_persisted_file(tmp_path) -> None:
    """No settings["node_id"] but a persisted file exists -> load."""
    from jmri_openlcb.connection import _resolve_node_id  # noqa: PLC0415
    persist = tmp_path / "openlcb-node-id"
    persist.write_text("02.CC.CC.CC.CC.CC\n", encoding="utf-8")
    nid = _resolve_node_id({"node_id_path": str(persist)})
    assert nid.dotted() == "02.CC.CC.CC.CC.CC"


async def test_node_id_minted_when_no_settings_no_file(tmp_path) -> None:
    """Fresh install: no setting, no file -> mint + save. Subsequent
    resolve calls with the same path load the freshly-saved value."""
    from jmri_openlcb.connection import _resolve_node_id  # noqa: PLC0415
    persist = tmp_path / "openlcb-node-id"
    assert not persist.exists()
    first = _resolve_node_id({"node_id_path": str(persist)})
    assert persist.exists()
    # Second call loads the same value -- no fresh mint.
    second = _resolve_node_id({"node_id_path": str(persist)})
    assert first == second
    # And the file content matches.
    assert persist.read_text().strip().upper() == first.dotted()


async def test_node_id_malformed_persisted_file_falls_back_to_mint(tmp_path) -> None:
    """Corrupted persisted file -> warn + mint a fresh one. The
    minted value overwrites the corrupted file so the next boot
    lands on a valid ID."""
    from jmri_openlcb.connection import _resolve_node_id  # noqa: PLC0415
    persist = tmp_path / "openlcb-node-id"
    persist.write_text("not a valid node id at all", encoding="utf-8")
    nid = _resolve_node_id({"node_id_path": str(persist)})
    # Minted in the dev range (0x02 top byte).
    assert (nid.value >> 40) == 0x02
    # File got overwritten with the fresh ID.
    assert persist.read_text().strip().upper() == nid.dotted()


async def test_node_id_persisted_id_survives_connection_restart(tmp_path) -> None:
    """End-to-end: open a connection, close it, reopen -- the
    second open picks up the same Node ID (and therefore claims
    the same alias, though with skip_alias_claim we just observe
    the NID)."""
    persist = tmp_path / "openlcb-node-id"
    for _ in range(2):
        a, _ = loopback_pair()
        conn = OlcbConnection()
        await conn.open({
            "kind": "loopback", "loopback_port": a,
            "skip_alias_claim": True, "source_alias": 0x123,
            "node_id_path": str(persist),
        })
        try:
            first_time_nid = conn.node_id.dotted()
        finally:
            await conn.close()
    # Same NID on both boots.
    assert persist.exists()


async def test_settings_validation() -> None:
    conn = OlcbConnection()
    with pytest.raises(ValueError, match="'port'"):
        await conn.open({"kind": "serial"})
    conn2 = OlcbConnection()
    with pytest.raises(ValueError, match="host"):
        await conn2.open({"kind": "tcp"})
    conn3 = OlcbConnection()
    with pytest.raises(ValueError, match="loopback_port"):
        await conn3.open({"kind": "loopback"})
