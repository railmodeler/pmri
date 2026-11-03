"""OlcbConnection.update_snip -- operator-editable SNIP fields."""
from __future__ import annotations

import json

from pmri_openlcb import OlcbConnection, loopback_pair


async def test_update_snip_mutates_advertised_identity(tmp_path) -> None:
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "node_snip_path": str(tmp_path / "snip.json"),
        "skip_init_complete": True,
    })
    try:
        assert conn.node_info.user_name == ""
        info = conn.update_snip(
            user_name="north-yard", user_description="bench Pi",
        )
        assert info.user_name == "north-yard"
        assert info.user_description == "bench Pi"
        # Live state reflects the update too.
        assert conn.node_info.user_name == "north-yard"
        # Vendor-fixed fields stay put.
        assert conn.node_info.manufacturer == "JMRI-Python"
    finally:
        await conn.close()


async def test_update_snip_persists_across_open(tmp_path) -> None:
    """A save + reopen with the same node_snip_path picks up the
    previously-persisted user_name / user_description."""
    a1, _ = loopback_pair()
    persist = tmp_path / "openlcb-node-snip.json"
    conn1 = OlcbConnection()
    await conn1.open({
        "kind": "loopback", "loopback_port": a1,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "node_snip_path": str(persist),
        "skip_init_complete": True,
    })
    try:
        conn1.update_snip(
            user_name="rescued", user_description="from disk",
        )
        # File exists with the JSON we saved.
        assert persist.exists()
        data = json.loads(persist.read_text())
        assert data == {
            "user_name": "rescued",
            "user_description": "from disk",
        }
    finally:
        await conn1.close()

    # Fresh connection reads the persisted values back.
    a2, _ = loopback_pair()
    conn2 = OlcbConnection()
    await conn2.open({
        "kind": "loopback", "loopback_port": a2,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "node_snip_path": str(persist),
        "skip_init_complete": True,
    })
    try:
        assert conn2.node_info.user_name == "rescued"
        assert conn2.node_info.user_description == "from disk"
    finally:
        await conn2.close()


async def test_update_snip_no_args_is_a_noop(tmp_path) -> None:
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "node_snip_path": str(tmp_path / "snip.json"),
        "skip_init_complete": True,
    })
    try:
        original = conn.node_info
        got = conn.update_snip()
        assert got == original
    finally:
        await conn.close()


async def test_snip_edit_visible_to_snip_probe(tmp_path) -> None:
    """A peer sending SNIP Request sees the updated user_name in
    the reply -- confirms the responder reads live state via the
    lambda wire-up."""
    from pmri_openlcb import SnipClient, OlcbTrafficController  # noqa: PLC0415

    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "node_snip_path": str(tmp_path / "snip.json"),
        "skip_init_complete": True,
    })
    tc_b = OlcbTrafficController(b, source_alias=0x222)
    await tc_b.start()
    client = SnipClient(tc_b)
    try:
        conn.update_snip(user_name="live-update-test")
        info = await client.probe(0x111, timeout=1.0)
        assert info.user_name == "live-update-test"
    finally:
        client.close()
        await tc_b.aclose()
        await conn.close()
