"""DuplicateNodeIdWatcher -- detect a peer claiming our own NodeID."""
from __future__ import annotations

import asyncio

from jmri_core.events import Signal

from jmri_openlcb import EVENT_DUPLICATE_NODE_ID, DuplicateNodeIdWatcher
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import (
    FT_MTI,
    MTI_PCER,
    OlcbFrame,
    _pack_header,
    extract_mti,
    make_amd_header,
)
from jmri_openlcb.duplicate_nid import MTI_VERIFIED_NID


class _FakeTc:
    def __init__(self, source_alias: int = 0x111) -> None:
        self.source_alias = source_alias
        self.frames: Signal[OlcbFrame] = Signal()
        self.sent: list[OlcbFrame] = []

    async def send_frame(self, frame: OlcbFrame) -> None:
        self.sent.append(frame)


OUR_NID = NodeId.parse("02.01.12.FE.DC.BA")
OTHER_NID = NodeId.parse("02.03.03.03.03.03")


async def test_amd_with_our_nid_from_peer_fires_callback() -> None:
    """A peer alias sends AMD carrying our own NodeID -> the watcher
    fires the on_duplicate callback with the offending alias."""
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    seen: list[int] = []
    watcher.on_duplicate(seen.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222), data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == [0x222]
    finally:
        watcher.close()


async def test_amd_from_our_own_alias_is_ignored() -> None:
    """Our outbound AMD (looping back through the TC or via
    ``sent_frames``) mustn't self-trigger."""
    tc = _FakeTc(source_alias=0x111)
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    seen: list[int] = []
    watcher.on_duplicate(seen.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x111), data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == []
    finally:
        watcher.close()


async def test_amd_with_different_nid_is_ignored() -> None:
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    seen: list[int] = []
    watcher.on_duplicate(seen.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222), data=OTHER_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == []
    finally:
        watcher.close()


async def test_verified_nid_with_our_nid_fires_callback() -> None:
    """A Verified NID reply from a peer alias carrying our NodeID
    also counts as a duplicate."""
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    seen: list[int] = []
    watcher.on_duplicate(seen.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x333),
            data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == [0x333]
    finally:
        watcher.close()


async def test_detection_broadcasts_well_known_event() -> None:
    """Beyond firing callbacks, the watcher emits a PCER for the
    Duplicate Node ID Detected well-known event so anyone else on
    the bus sees the collision."""
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222), data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        # Exactly one PCER, with the well-known event as its data.
        pcers = [
            f for f in tc.sent
            if extract_mti(f.header) == MTI_PCER
        ]
        assert len(pcers) == 1
        assert pcers[0].data == EVENT_DUPLICATE_NODE_ID.bytes
    finally:
        watcher.close()


async def test_multiple_subscribers_all_fire() -> None:
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    a: list[int] = []
    b: list[int] = []
    watcher.on_duplicate(a.append)
    watcher.on_duplicate(b.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222), data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert a == [0x222]
        assert b == [0x222]
    finally:
        watcher.close()


async def test_unsubscribe_stops_delivery() -> None:
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    seen: list[int] = []
    unsub = watcher.on_duplicate(seen.append)
    try:
        unsub()
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222), data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == []
    finally:
        watcher.close()


async def test_short_data_frames_are_ignored() -> None:
    """Malformed AMD (fewer than 6 bytes of data) shouldn't crash
    or spuriously fire -- just skip."""
    tc = _FakeTc()
    watcher = DuplicateNodeIdWatcher(tc, OUR_NID)
    seen: list[int] = []
    watcher.on_duplicate(seen.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222), data=b"\x02\x01\x12",
        ))
        await asyncio.sleep(0)
        assert seen == []
    finally:
        watcher.close()


# ---- connection-level wiring --------------------------------------------


async def test_connection_wires_duplicate_watcher_by_default() -> None:
    """OlcbConnection.open() stands up a DuplicateNodeIdWatcher and
    exposes it via .duplicate_nid -- the operator can subscribe
    without knowing the wiring."""
    from jmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
    })
    try:
        seen: list[int] = []
        conn.duplicate_nid.on_duplicate(seen.append)
        # Inject a peer AMD claiming our NodeID.
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_amd_header(0x222), data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert seen == [0x222]
    finally:
        await conn.close()


async def test_skip_flag_disables_the_watcher() -> None:
    """``skip_duplicate_nid_watch=True`` skips construction so a
    test running two nodes with the same NID for protocol testing
    doesn't get flooded with warnings."""
    import pytest  # noqa: PLC0415
    from jmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_duplicate_nid_watch": True,
    })
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            _ = conn.duplicate_nid
    finally:
        await conn.close()
