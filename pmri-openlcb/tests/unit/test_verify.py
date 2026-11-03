"""Tests for the Verify Node ID protocol."""
from __future__ import annotations

import asyncio

import pytest
from pmri_core.events import Signal

from pmri_openlcb.alias import NodeId
from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_mti,
    extract_source_alias,
)
from pmri_openlcb.verify import (
    MTI_VERIFIED_NID,
    MTI_VERIFY_NID_ADDRESSED,
    MTI_VERIFY_NID_GLOBAL,
    NodeIdLookup,
    VerifyNodeIdService,
)


OUR_NID = NodeId.parse("02.01.12.FE.DC.BA")
OTHER_NID = NodeId.parse("02.02.02.02.02.02")


class _FakeTc:
    """Two-sided fake -- frames on side A appear on B's inbound signal."""

    def __init__(self, source_alias: int) -> None:
        self.source_alias = source_alias
        self.frames: Signal[OlcbFrame] = Signal()
        self._peer: _FakeTc | None = None
        self.sent: list[OlcbFrame] = []

    def link(self, peer: _FakeTc) -> None:
        self._peer = peer

    async def send_frame(self, frame: OlcbFrame) -> None:
        self.sent.append(frame)
        if self._peer is not None:
            await self._peer.frames.emit(frame)


def _pair(alias_a: int, alias_b: int) -> tuple[_FakeTc, _FakeTc]:
    a = _FakeTc(alias_a)
    b = _FakeTc(alias_b)
    a.link(b)
    b.link(a)
    return a, b


# ---- VerifyNodeIdService -- auto-answer path ------------------------------


async def test_service_answers_empty_global_verify() -> None:
    """Empty-data Verify NID Global -> every node answers with their
    Verified NID reply."""
    a, b = _pair(0x111, 0x222)
    svc = VerifyNodeIdService(a, OUR_NID)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_GLOBAL, 0x222),
            data=b"",
        ))
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        reply = a.sent[0]
        assert extract_mti(reply.header) == MTI_VERIFIED_NID
        assert extract_source_alias(reply.header) == 0x111
        assert reply.data == OUR_NID.bytes
    finally:
        svc.close()


async def test_service_answers_targeted_global_verify_for_us() -> None:
    """Verify NID Global carrying our NID as data -> we answer."""
    a, b = _pair(0x111, 0x222)
    svc = VerifyNodeIdService(a, OUR_NID)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_GLOBAL, 0x222),
            data=OUR_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        assert extract_mti(a.sent[0].header) == MTI_VERIFIED_NID
    finally:
        svc.close()


async def test_service_ignores_targeted_verify_for_others() -> None:
    """Verify NID Global with someone else's NID -> we stay silent."""
    a, b = _pair(0x111, 0x222)
    svc = VerifyNodeIdService(a, OUR_NID)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_GLOBAL, 0x222),
            data=OTHER_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


async def test_service_answers_addressed_verify_when_targeted_at_us() -> None:
    """Verify NID Addressed carries a 2-byte dest alias -- we answer
    only when it names our alias."""
    a, b = _pair(0x111, 0x222)
    svc = VerifyNodeIdService(a, OUR_NID)
    try:
        # Targeted at us (0x111).
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_ADDRESSED, 0x222),
            data=(0x111).to_bytes(2, "big"),
        ))
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        assert a.sent[0].data == OUR_NID.bytes
    finally:
        svc.close()


async def test_service_ignores_addressed_verify_for_others() -> None:
    a, b = _pair(0x111, 0x222)
    svc = VerifyNodeIdService(a, OUR_NID)
    try:
        # Targeted at some other alias 0x999.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_ADDRESSED, 0x222),
            data=(0x999).to_bytes(2, "big"),
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


async def test_service_ignores_unrelated_mtis() -> None:
    """PCERs and other traffic must not provoke a Verified reply."""
    from pmri_openlcb.codec import make_pcer_header  # noqa: PLC0415
    a, b = _pair(0x111, 0x222)
    svc = VerifyNodeIdService(a, OUR_NID)
    try:
        await b.send_frame(OlcbFrame(
            header=make_pcer_header(0x222),
            data=b"\x05\x01\x01\x01\x4C\x43\x00\x01",
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


# ---- NodeIdLookup -- client side -----------------------------------------


async def test_lookup_records_verified_reply_by_source_alias() -> None:
    """A Verified NID from an unsolicited announcement should still
    populate the map -- helpful when we hear a global sweep initiated
    by someone else."""
    a, b = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    try:
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=OTHER_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert lookup.get(0x222) == OTHER_NID
        assert lookup.by_nid(OTHER_NID) == 0x222
        assert lookup.known() == {0x222: OTHER_NID}
    finally:
        lookup.close()


async def test_lookup_verify_all_broadcasts_global_verify() -> None:
    tc = _FakeTc(0x111)
    lookup = NodeIdLookup(tc)
    try:
        await lookup.verify_all()
        assert len(tc.sent) == 1
        assert extract_mti(tc.sent[0].header) == MTI_VERIFY_NID_GLOBAL
        assert tc.sent[0].data == b""
    finally:
        lookup.close()


async def test_lookup_verify_by_nid_broadcasts_with_nid_data() -> None:
    tc = _FakeTc(0x111)
    lookup = NodeIdLookup(tc)
    try:
        await lookup.verify_by_nid(OTHER_NID)
        assert len(tc.sent) == 1
        assert extract_mti(tc.sent[0].header) == MTI_VERIFY_NID_GLOBAL
        assert tc.sent[0].data == OTHER_NID.bytes
    finally:
        lookup.close()


async def test_lookup_probe_awaits_and_returns_nid() -> None:
    """End-to-end: A's probe -> B's service answers -> A's future
    resolves with B's NodeId."""
    a, b = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    svc = VerifyNodeIdService(b, OTHER_NID)
    try:
        nid = await lookup.probe(0x222, timeout=1.0)
        assert nid == OTHER_NID
        # Also populated in the map.
        assert lookup.get(0x222) == OTHER_NID
    finally:
        svc.close()
        lookup.close()


async def test_lookup_probe_times_out_when_target_silent() -> None:
    a, _ = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await lookup.probe(0x222, timeout=0.05)
    finally:
        lookup.close()


async def test_concurrent_probe_to_same_alias_raises() -> None:
    """The client's per-target future dict enforces one probe per
    alias -- a second probe while the first is in flight is a
    programmer error, not a silent race."""
    tc = _FakeTc(0x111)
    lookup = NodeIdLookup(tc)
    try:
        pending = asyncio.create_task(lookup.probe(0x222, timeout=1.0))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="already in flight"):
            await lookup.probe(0x222, timeout=0.1)
        pending.cancel()
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await pending
    finally:
        lookup.close()


async def test_forget_drops_the_alias_and_cancels_pending() -> None:
    """AMR from a peer -> drop them from the map. If we were mid-probe,
    the pending future should get an error rather than hang."""
    a, b = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    try:
        # First populate.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=OTHER_NID.bytes,
        ))
        await asyncio.sleep(0)
        assert lookup.get(0x222) == OTHER_NID
        # Start a probe, then forget mid-flight.
        pending = asyncio.create_task(lookup.probe(0x222, timeout=5.0))
        await asyncio.sleep(0)
        lookup.forget(0x222)
        with pytest.raises(RuntimeError, match="released before"):
            await pending
        assert lookup.get(0x222) is None
    finally:
        lookup.close()


async def test_resolve_hits_cache_when_already_known() -> None:
    """If we already know a NID's alias, resolve returns immediately
    without any wire traffic. That's the fast path for
    OlcbThrottleManager.acquire on a second call."""
    a, b = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    try:
        # Pre-populate via an unsolicited Verified NID broadcast.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=OTHER_NID.bytes,
        ))
        await asyncio.sleep(0)
        a.sent.clear()
        alias = await lookup.resolve(OTHER_NID, timeout=1.0)
        assert alias == 0x222
        # No verify probe fired.
        assert a.sent == []
    finally:
        lookup.close()


async def test_resolve_fires_verify_and_awaits_reply() -> None:
    """First-time resolve fires Verify NID Global with the target
    NID in the data; the matching node's reply resolves the future."""
    a, b = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    svc = VerifyNodeIdService(b, OTHER_NID)
    try:
        alias = await lookup.resolve(OTHER_NID, timeout=1.0)
        assert alias == 0x222
        assert lookup.get(0x222) == OTHER_NID
    finally:
        svc.close()
        lookup.close()


async def test_resolve_times_out_when_nid_absent() -> None:
    a, _ = _pair(0x111, 0x222)
    lookup = NodeIdLookup(a)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await lookup.resolve(OTHER_NID, timeout=0.05)
    finally:
        lookup.close()


async def test_lookup_ignores_short_verified_replies() -> None:
    """A malformed Verified NID (fewer than 6 data bytes) can't
    populate the map -- it doesn't carry a full NID. Silently ignore
    rather than raise."""
    tc = _FakeTc(0x111)
    lookup = NodeIdLookup(tc)
    try:
        await tc.frames.emit(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=b"\x02\x01\x12",   # only 3 bytes
        ))
        await asyncio.sleep(0)
        assert lookup.get(0x222) is None
    finally:
        lookup.close()


# ---- end-to-end via OlcbConnection ---------------------------------------


async def test_connection_exposes_verify_service_and_lookup() -> None:
    """OlcbConnection should stand up both sides on open; ``node_id_lookup``
    is the client-side property."""
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
        "node_id": "02.01.12.FE.DC.BA",
    })
    try:
        assert conn.node_id_lookup is not None
        # Nothing has been discovered yet.
        assert conn.node_id_lookup.known() == {}
    finally:
        await conn.close()
