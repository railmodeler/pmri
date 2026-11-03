"""Tests for the CID/RID alias-claim handshake."""
from __future__ import annotations

import asyncio

import pytest
from pmri_core.events import Signal

from pmri_openlcb.alias import (
    AliasCollision,
    AliasReleaseWatcher,
    AmeResponder,
    NodeId,
    candidate_alias,
    claim_alias,
)
from pmri_openlcb.codec import (
    CTRL_AMD,
    CTRL_RID,
    OlcbFrame,
    cid_seq,
    extract_frame_type,
    extract_source_alias,
    extract_variable_field,
    is_cid,
    is_control,
    make_ame_header,
    make_amr_header,
    make_rid_header,
)


# ---- NodeId --------------------------------------------------------------


def test_node_id_parses_dotted_form() -> None:
    nid = NodeId.parse("02.01.12.FE.DC.BA")
    assert nid.value == 0x02_01_12_FE_DC_BA
    assert nid.dotted() == "02.01.12.FE.DC.BA"


def test_node_id_parses_int_literal() -> None:
    nid = NodeId.parse("0x020112FEDCBA")
    assert nid.value == 0x020112FEDCBA


def test_node_id_range_enforced() -> None:
    with pytest.raises(ValueError, match="48-bit"):
        NodeId(1 << 48)
    with pytest.raises(ValueError, match="48-bit"):
        NodeId(-1)


def test_node_id_slices_pin_the_bit_order() -> None:
    """CID 1..4 slices must line up with S-9.7.0.1 sec.6:
    CID 1 = bits 47..36, CID 4 = bits 11..0. Locks in the bit
    layout against future refactors."""
    nid = NodeId(0xABC_DEF_123_456)
    assert nid.slice_12(1) == 0xABC
    assert nid.slice_12(2) == 0xDEF
    assert nid.slice_12(3) == 0x123
    assert nid.slice_12(4) == 0x456


def test_node_id_bytes_are_msb_first() -> None:
    """Wire form: 6 bytes big-endian. AMD data carries these bytes
    directly."""
    assert NodeId(0x02_01_12_FE_DC_BA).bytes == bytes.fromhex("020112FEDCBA")


# ---- candidate_alias ------------------------------------------------------


def test_candidate_alias_is_12_bit_nonzero() -> None:
    nid = NodeId(0x02_01_12_FE_DC_BA)
    for attempt in range(20):
        alias = candidate_alias(nid, attempt)
        assert 0 < alias <= 0xFFF


def test_candidate_alias_changes_on_retry() -> None:
    """A collision retry must land on a different candidate -- else
    two nodes with the same NID would loop forever."""
    nid = NodeId(0x02_01_12_FE_DC_BA)
    aliases = {candidate_alias(nid, a) for a in range(5)}
    assert len(aliases) >= 4  # some collisions between retries are OK


# ---- claim_alias -- happy path --------------------------------------------


class _FakeTc:
    """Minimal traffic-controller stand-in for the alias handshake.

    ``send_frame`` records outbound frames; ``frames`` is a Signal
    tests fire directly to simulate other nodes on the bus. Set
    ``on_send`` to inject a per-send hook (e.g. to fake a collision
    reply once a CID 4 goes out)."""

    def __init__(self) -> None:
        self.frames: Signal[OlcbFrame] = Signal()
        self.sent: list[OlcbFrame] = []
        self.on_send = None   # type: ignore[assignment]

    async def send_frame(self, frame: OlcbFrame) -> None:
        self.sent.append(frame)
        if self.on_send is not None:
            await self.on_send(frame)


async def test_claim_alias_sends_cid_1_through_4_then_rid_and_amd() -> None:
    """Happy path: no collision -> we send 4 CID frames, wait, then
    RID + AMD with the 48-bit NID as the AMD data."""
    tc = _FakeTc()
    nid = NodeId(0x02_01_12_FE_DC_BA)
    alias = await claim_alias(tc, nid, wait=0.0)

    # 4 CIDs, 1 RID, 1 AMD.
    assert len(tc.sent) == 6
    for i, seq in enumerate((1, 2, 3, 4), start=0):
        f = tc.sent[i]
        assert is_cid(f.header)
        assert cid_seq(f.header) == seq
        assert extract_source_alias(f.header) == alias
        # Variable field carries the NID slice for that CID.
        assert extract_variable_field(f.header) == nid.slice_12(seq)
    # RID: control frame with variable = 0x700 and no data.
    rid = tc.sent[4]
    assert is_control(rid.header, CTRL_RID)
    assert extract_source_alias(rid.header) == alias
    # AMD: control frame with variable = 0x701 and 6-byte NID.
    amd = tc.sent[5]
    assert is_control(amd.header, CTRL_AMD)
    assert extract_source_alias(amd.header) == alias
    assert amd.data == nid.bytes


# ---- claim_alias -- collision & retry -------------------------------------


async def test_claim_alias_retries_on_collision() -> None:
    """Simulate a node already on the bus using our first candidate.
    On CID 4 of the first attempt we fake an existing RID from the
    same alias -> claim_alias treats that as a collision and retries."""
    tc = _FakeTc()
    nid = NodeId(0x02_01_12_FE_DC_BA)
    first_candidate = candidate_alias(nid, 0)
    triggered = False

    async def _maybe_collide(frame: OlcbFrame) -> None:
        nonlocal triggered
        if (
            not triggered
            and is_cid(frame.header)
            and cid_seq(frame.header) == 4
            and extract_source_alias(frame.header) == first_candidate
        ):
            triggered = True
            await tc.frames.emit(OlcbFrame(
                header=make_rid_header(first_candidate), data=b"",
            ))

    tc.on_send = _maybe_collide

    alias = await claim_alias(tc, nid, wait=0.05)
    assert triggered
    assert alias != first_candidate
    assert alias == candidate_alias(nid, 1)


async def test_claim_alias_gives_up_after_max_attempts() -> None:
    """Every candidate collides -> after ``max_attempts`` we raise
    rather than looping forever."""
    tc = _FakeTc()
    nid = NodeId(0x02_01_12_FE_DC_BA)

    async def _always_collide(frame: OlcbFrame) -> None:
        if is_cid(frame.header) and cid_seq(frame.header) == 4:
            alias = extract_source_alias(frame.header)
            await tc.frames.emit(OlcbFrame(
                header=make_rid_header(alias), data=b"",
            ))

    tc.on_send = _always_collide

    with pytest.raises(AliasCollision):
        await claim_alias(tc, nid, wait=0.02, max_attempts=3)


# ---- AmeResponder --------------------------------------------------------


async def test_ame_responder_answers_global_probe() -> None:
    """Empty-data AME -> every node publishes AMD (alias<->NID map)."""
    tc = _FakeTc()
    nid = NodeId(0x02_01_12_FE_DC_BA)
    responder = AmeResponder(tc, nid, alias=0x456)
    try:
        # Simulate a peer's global AME probe.
        await tc.frames.emit(OlcbFrame(
            header=make_ame_header(0x789), data=b"",
        ))
        # Give the signal callback a chance to fire and send.
        await asyncio.sleep(0)
        assert len(tc.sent) == 1
        amd = tc.sent[0]
        assert is_control(amd.header, CTRL_AMD)
        assert extract_source_alias(amd.header) == 0x456
        assert amd.data == nid.bytes
    finally:
        responder.close()


async def test_ame_responder_answers_targeted_probe_for_us() -> None:
    tc = _FakeTc()
    nid = NodeId(0x02_01_12_FE_DC_BA)
    responder = AmeResponder(tc, nid, alias=0x456)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_ame_header(0x789), data=nid.bytes,
        ))
        await asyncio.sleep(0)
        assert len(tc.sent) == 1
    finally:
        responder.close()


async def test_ame_responder_ignores_targeted_probe_for_others() -> None:
    tc = _FakeTc()
    nid = NodeId(0x02_01_12_FE_DC_BA)
    other_nid = NodeId(0x02_02_02_02_02_02)
    responder = AmeResponder(tc, nid, alias=0x456)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_ame_header(0x789), data=other_nid.bytes,
        ))
        await asyncio.sleep(0)
        assert tc.sent == []
    finally:
        responder.close()


async def test_ame_responder_ignores_non_ame_frames() -> None:
    """A stray RID (or any non-AME control frame) does not provoke
    a spurious AMD response."""
    tc = _FakeTc()
    responder = AmeResponder(tc, NodeId(0x02_01_12_FE_DC_BA), alias=0x456)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_rid_header(0x789), data=b"",
        ))
        await asyncio.sleep(0)
        assert tc.sent == []
    finally:
        responder.close()


# ---- AliasReleaseWatcher --------------------------------------------------


async def test_alias_release_watcher_fires_callback_on_inbound_amr() -> None:
    tc = _FakeTc()
    watcher = AliasReleaseWatcher(tc)
    released: list[int] = []
    watcher.on_release(released.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amr_header(0x333), data=b"",
        ))
        await asyncio.sleep(0)
        assert released == [0x333]
    finally:
        watcher.close()


async def test_alias_release_watcher_ignores_non_amr_frames() -> None:
    """AMD, AME, RID and MTI frames must not trigger a release
    notification -- only the AMR variable field 0x703 counts."""
    tc = _FakeTc()
    watcher = AliasReleaseWatcher(tc)
    released: list[int] = []
    watcher.on_release(released.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_rid_header(0x333), data=b"",
        ))
        await tc.frames.emit(OlcbFrame(
            header=make_ame_header(0x333), data=b"",
        ))
        await asyncio.sleep(0)
        assert released == []
    finally:
        watcher.close()


async def test_alias_release_watcher_fans_out_to_all_subscribers() -> None:
    tc = _FakeTc()
    watcher = AliasReleaseWatcher(tc)
    a: list[int] = []
    b: list[int] = []
    watcher.on_release(a.append)
    watcher.on_release(b.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amr_header(0x444), data=b"",
        ))
        await asyncio.sleep(0)
        assert a == [0x444]
        assert b == [0x444]
    finally:
        watcher.close()


async def test_alias_release_watcher_unsubscribe_stops_delivery() -> None:
    tc = _FakeTc()
    watcher = AliasReleaseWatcher(tc)
    released: list[int] = []
    unsub = watcher.on_release(released.append)
    try:
        unsub()
        await tc.frames.emit(OlcbFrame(
            header=make_amr_header(0x555), data=b"",
        ))
        await asyncio.sleep(0)
        assert released == []
    finally:
        watcher.close()


async def test_alias_release_watcher_swallows_callback_exceptions() -> None:
    """One failing subscriber must not stop the fan-out to the rest --
    otherwise a stale cache with a bug in its forget hook would
    block every other subsystem from cleaning up."""
    tc = _FakeTc()
    watcher = AliasReleaseWatcher(tc)
    survived: list[int] = []

    def _broken(_: int) -> None:
        raise RuntimeError("boom")

    watcher.on_release(_broken)
    watcher.on_release(survived.append)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_amr_header(0x666), data=b"",
        ))
        await asyncio.sleep(0)
        assert survived == [0x666]
    finally:
        watcher.close()


# ---- connection-level wiring: AMR clears NID + roster caches -------------


async def test_connection_wires_amr_to_forget_nid_and_roster() -> None:
    """OlcbConnection registers node_id_lookup.forget + event_roster.forget
    with the AMR watcher, so a peer's AMR drops it from both maps
    even without any service-level bookkeeping."""
    from pmri_openlcb.codec import _pack_header, FT_MTI  # noqa: PLC0415
    from pmri_openlcb.connection import OlcbConnection  # noqa: PLC0415
    from pmri_openlcb.event_ident import (  # noqa: PLC0415
        MTI_PRODUCER_IDENT_VALID,
    )
    from pmri_openlcb.events import EventId  # noqa: PLC0415
    from pmri_openlcb.transport import loopback_pair  # noqa: PLC0415
    from pmri_openlcb.verify import MTI_VERIFIED_NID  # noqa: PLC0415

    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
    })
    try:
        # Directly inject peer frames onto the tc's inbound signal --
        # that's the same fan-out point every subsystem hooks into,
        # so we don't need to round-trip real bytes just to prove the
        # AMR wiring works.
        peer_alias = 0x222
        peer_nid = NodeId(0x02_01_12_00_00_02)
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, peer_alias),
            data=peer_nid.bytes,
        ))
        ev = EventId.parse("05.01.01.01.4C.43.00.99")
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=_pack_header(
                FT_MTI, MTI_PRODUCER_IDENT_VALID, peer_alias,
            ),
            data=ev.bytes,
        ))
        await asyncio.sleep(0)
        assert conn.node_id_lookup.get(peer_alias) == peer_nid
        assert ev in conn.event_roster.produced_by(peer_alias)

        # Peer releases its alias.
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_amr_header(peer_alias), data=b"",
        ))
        await asyncio.sleep(0)

        assert conn.node_id_lookup.get(peer_alias) is None
        assert conn.event_roster.produced_by(peer_alias) == {}
    finally:
        await conn.close()
