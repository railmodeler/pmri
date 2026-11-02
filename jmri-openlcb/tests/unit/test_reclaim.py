"""OlcbConnection.reclaim() -- release the current alias and claim
a fresh one under a new NodeID, plus the auto-reclaim wiring off
the duplicate-NID watcher.
"""
from __future__ import annotations

import asyncio

import pytest

from jmri_openlcb import (
    OlcbConnection,
    loopback_pair,
)
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import (
    CTRL_AMR,
    FT_MTI,
    MTI_PCER,
    OlcbFrame,
    _pack_header,
    decode_frame,
    extract_mti,
    is_control,
    make_amd_header,
)
from jmri_openlcb.duplicate_nid import MTI_VERIFIED_NID


async def _drain(port, deadline: float) -> list[OlcbFrame]:
    frames: list[OlcbFrame] = []
    buf = bytearray()

    async def _run() -> None:
        while True:
            b = await port.read_byte()
            buf.append(b)
            if b == ord(";"):
                try:
                    frames.append(decode_frame(bytes(buf).decode("ascii")))
                except Exception:
                    pass
                buf.clear()

    try:
        await asyncio.wait_for(_run(), timeout=deadline)
    except asyncio.TimeoutError:
        pass
    return frames


async def test_reclaim_with_new_node_id_updates_identity() -> None:
    """Calling reclaim(new_node_id) switches our NodeID and claims
    a new alias -- the connection's identity + tc.source_alias both
    reflect the change."""
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_init_complete": True,
    })
    try:
        old_alias = conn._tc.source_alias  # type: ignore[union-attr]
        assert conn.node_id == NodeId.parse("02.01.12.FE.DC.BA")
        new_nid = NodeId.parse("02.99.99.99.99.99")
        result = await conn.reclaim(new_nid, claim_wait=0.0)
        assert result == new_nid
        assert conn.node_id == new_nid
        # The alias is now whatever the fresh claim landed on --
        # unlikely to equal the old one after a full CID/RID cycle.
        assert conn._tc.source_alias != old_alias  # type: ignore[union-attr]
    finally:
        await conn.close()


async def test_reclaim_sends_amr_for_old_alias() -> None:
    """A peer watching the bus sees our AMR for the old alias
    before the new claim starts -- that's the wire signal to drop
    the (alias -> old NID) cache entry."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_init_complete": True,
    })
    try:
        await conn.reclaim(
            NodeId.parse("02.99.99.99.99.99"), claim_wait=0.0,
        )
        frames = await _drain(b, deadline=0.05)
        # First outbound after reclaim() should be the AMR.
        amrs = [
            f for f in frames if is_control(f.header, CTRL_AMR)
        ]
        assert amrs, "expected an AMR for the old alias"
        # Header source_alias matches the old alias 0x111.
        assert (amrs[0].header & 0xFFF) == 0x111
    finally:
        await conn.close()


async def test_reclaim_mints_fresh_dev_range_nid_by_default() -> None:
    """No argument -> a fresh dev-range NodeID (top byte 0x02) is
    minted so the collision-detected identity gets replaced."""
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_init_complete": True,
    })
    try:
        old_nid = conn.node_id
        new_nid = await conn.reclaim(claim_wait=0.0)
        assert new_nid != old_nid
        # Dev-range top byte per S-9.7.0.10.
        assert new_nid.bytes[0] == 0x02
    finally:
        await conn.close()


async def test_reclaim_persists_new_node_id(tmp_path) -> None:
    """After reclaim() the new NodeID lands in the persist file,
    so a subsequent open() picks it up instead of the collided one."""
    a, _ = loopback_pair()
    persist = tmp_path / "openlcb-node-id"
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "node_id_path": str(persist),
        "skip_init_complete": True,
    })
    try:
        new_nid = NodeId.parse("02.99.99.99.99.99")
        await conn.reclaim(new_nid, claim_wait=0.0)
        assert persist.exists()
        assert new_nid.dotted() in persist.read_text()
    finally:
        await conn.close()


async def test_auto_reclaim_flag_wires_watcher_callback(tmp_path) -> None:
    """With auto_reclaim_on_duplicate=True, a peer AMD claiming our
    NodeID triggers a background reclaim -- our identity ends up
    different after the collision, without any explicit call."""
    a, _ = loopback_pair()
    persist = tmp_path / "openlcb-node-id"
    conn = OlcbConnection()
    original = NodeId.parse("02.01.12.FE.DC.BA")
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": original.dotted(),
        "node_id_path": str(persist),
        "auto_reclaim_on_duplicate": True,
        "skip_init_complete": True,
    })
    try:
        # Peer alias 0x222 claims our NID -> auto-reclaim runs.
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_amd_header(0x222), data=original.bytes,
        ))
        # Give the background task time to run + settle.
        await asyncio.sleep(0.1)
        assert conn.node_id != original
    finally:
        await conn.close()


async def test_reclaim_updates_duplicate_watcher_baseline() -> None:
    """After reclaim, the duplicate-NID watcher tracks the NEW NID.
    A peer emitting AMD for the OLD NID no longer counts as a
    collision (it's someone else's NID now)."""
    a, _ = loopback_pair()
    conn = OlcbConnection()
    original = NodeId.parse("02.01.12.FE.DC.BA")
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": original.dotted(),
        "skip_init_complete": True,
    })
    try:
        new_nid = NodeId.parse("02.99.99.99.99.99")
        await conn.reclaim(new_nid, claim_wait=0.0)
        seen: list[int] = []
        conn.duplicate_nid.on_duplicate(seen.append)
        # Peer AMD for the OLD NID should NOT trigger the watcher.
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_amd_header(0x222), data=original.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == []
        # But peer AMD for the NEW NID still does.
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_amd_header(0x333), data=new_nid.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == [0x333]
    finally:
        await conn.close()
