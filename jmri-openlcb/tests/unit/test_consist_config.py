"""Traction Consist Configuration tests -- encode/decode + client
+ CS-side handlers.
"""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.events import Signal

from jmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import OlcbFrame, extract_mti
from jmri_openlcb.command_station import CommandStationNode
from jmri_openlcb.traction import (
    CONSIST_ATTACH,
    CONSIST_DETACH,
    CONSIST_FLAG_HIDDEN,
    CONSIST_FLAG_REVERSE,
    CONSIST_INDEX_INVALID,
    CONSIST_QUERY,
    ConsistMember,
    MTI_TRACTION_REPLY,
    MTI_TRACTION_REQUEST,
    TC_CONSIST_CONFIG,
    TractionClient,
    decode_consist_flags,
    encode_consist_flags,
)


STATION_NID = NodeId.parse("02.01.12.FE.DC.BA")
MEMBER_A = NodeId(0x06_01_00_00_00_03)   # DCC short 3
MEMBER_B = NodeId(0x06_01_00_00_00_04)   # DCC short 4
MEMBER_LONG = NodeId(0x06_01_00_00_C0_11)   # DCC long 17


class _FakeTc:
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


def _last_request(tc: _FakeTc):
    """Reassemble the last outbound Traction Control Command."""
    r = AddressedReassembler()
    last = None
    for f in tc.sent:
        m = r.receive(f)
        if m is not None and m.mti == MTI_TRACTION_REQUEST:
            last = m
    assert last is not None
    return last


# ---- flag encoding ------------------------------------------------------


def test_encode_consist_flags_layout() -> None:
    """Regression pin: high byte reserved (0), low byte carries the
    hidden + reverse bits."""
    assert encode_consist_flags(
        ConsistMember(node_id=MEMBER_A)
    ) == b"\x00\x00"
    assert encode_consist_flags(
        ConsistMember(node_id=MEMBER_A, hidden=True)
    ) == b"\x00\x01"
    assert encode_consist_flags(
        ConsistMember(node_id=MEMBER_A, reverse=True)
    ) == b"\x00\x02"
    assert encode_consist_flags(
        ConsistMember(node_id=MEMBER_A, hidden=True, reverse=True)
    ) == b"\x00\x03"


def test_decode_consist_flags_round_trips() -> None:
    for hidden in (False, True):
        for reverse in (False, True):
            m = ConsistMember(node_id=MEMBER_A, hidden=hidden, reverse=reverse)
            got_hidden, got_reverse = decode_consist_flags(
                encode_consist_flags(m),
            )
            assert got_hidden == hidden
            assert got_reverse == reverse


def test_decode_consist_flags_ignores_unused_bits() -> None:
    """A spec extension might light up bit 2..7 in the low byte. Our
    decode must still yield the two flags we know without raising --
    otherwise a future field revision would break existing decoders."""
    hidden, reverse = decode_consist_flags(b"\xFF\xFF")
    # Both known bits are set; unknown bits are silently absorbed.
    assert hidden is True
    assert reverse is True


def test_decode_consist_flags_rejects_short_buffer() -> None:
    with pytest.raises(ValueError, match="2 bytes"):
        decode_consist_flags(b"\x00")


# ---- TractionClient outbound frames ------------------------------------


async def test_attach_sends_correct_payload_bytes() -> None:
    """Payload layout: 0x30, 0x01, flags(2), NID(6). Regression pin
    against a wire byte-by-byte."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.attach_consist_member(
            0x222,
            ConsistMember(node_id=MEMBER_A, hidden=True, reverse=False),
        )
        msg = _last_request(a)
        assert msg.dest_alias == 0x222
        assert msg.body == bytes([
            TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x01,
        ]) + MEMBER_A.bytes
    finally:
        client.close()


async def test_detach_sends_correct_payload_bytes() -> None:
    """Payload: 0x30, 0x02, NID(6). No flags -- just the target."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.detach_consist_member(0x222, MEMBER_A)
        msg = _last_request(a)
        assert msg.body == bytes([
            TC_CONSIST_CONFIG, CONSIST_DETACH,
        ]) + MEMBER_A.bytes
    finally:
        client.close()


async def test_query_sends_index_byte() -> None:
    """Payload: 0x30, 0x03, index."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        # Fire and cancel -- we only care about the outbound bytes.
        task = asyncio.create_task(
            client.query_consist_member(0x222, index=2, timeout=0.02),
        )
        await asyncio.sleep(0)
        msg = _last_request(a)
        assert msg.body == bytes([
            TC_CONSIST_CONFIG, CONSIST_QUERY, 0x02,
        ])
        task.cancel()
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await task
    finally:
        client.close()


async def test_query_rejects_out_of_range_index() -> None:
    tc = _FakeTc(0x111)
    client = TractionClient(tc)
    try:
        with pytest.raises(ValueError, match="0..255"):
            await client.query_consist_member(0x222, index=256, timeout=0.01)
    finally:
        client.close()


# ---- CS-side handling ---------------------------------------------------


async def _cs():
    """Two-sided fake bus + CS + minimal DCC throttle manager."""
    a, b = _pair(0x111, 0x222)
    dcc = ThrottleManager("X")
    cs = CommandStationNode(a, dcc, station_node_id=STATION_NID)
    return a, b, dcc, cs


async def test_attach_appends_to_hosted_consist_members() -> None:
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        # Attach a member.
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_A.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert len(head.consist_members) == 1
        assert head.consist_members[0].node_id == MEMBER_A
        assert head.consist_members[0].hidden is False
        assert head.consist_members[0].reverse is False
    finally:
        cs.close()


async def test_attach_preserves_flags() -> None:
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00,
                   CONSIST_FLAG_HIDDEN | CONSIST_FLAG_REVERSE])
            + MEMBER_A.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        m = head.consist_members[0]
        assert m.hidden is True
        assert m.reverse is True
    finally:
        cs.close()


async def test_attach_is_idempotent_and_updates_flags() -> None:
    """Re-attaching an existing member updates its flags in place
    rather than appending a duplicate. Matters when a throttle
    re-syncs consist state without a preceding detach."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        # First attach -- no flags.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # Re-attach with reverse flag.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00,
                   CONSIST_FLAG_REVERSE]) + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert len(head.consist_members) == 1
        assert head.consist_members[0].reverse is True
    finally:
        cs.close()


async def test_detach_removes_matching_member() -> None:
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        head.consist_members = [
            ConsistMember(node_id=MEMBER_A),
            ConsistMember(node_id=MEMBER_B),
        ]
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_DETACH]) + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert [m.node_id for m in head.consist_members] == [MEMBER_B]
    finally:
        cs.close()


async def test_detach_on_absent_member_is_noop() -> None:
    """Detaching a member that isn't in the consist just leaves the
    list unchanged -- no exception, no state change."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        head.consist_members = [ConsistMember(node_id=MEMBER_A)]
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_DETACH]) + MEMBER_B.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert [m.node_id for m in head.consist_members] == [MEMBER_A]
    finally:
        cs.close()


async def test_query_reply_layout_for_valid_index() -> None:
    """Reply payload: 0x30, 0x03, size, index, flags(2), NID(6)."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        head.consist_members = [
            ConsistMember(node_id=MEMBER_A, hidden=False, reverse=False),
            ConsistMember(node_id=MEMBER_B, hidden=True, reverse=True),
        ]
        a.sent.clear()
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_QUERY, 1]),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # Reassemble the reply.
        r = AddressedReassembler()
        reply = None
        for f in a.sent:
            m = r.receive(f)
            if m is not None and m.mti == MTI_TRACTION_REPLY:
                reply = m
        assert reply is not None
        assert reply.body[0] == TC_CONSIST_CONFIG
        assert reply.body[1] == CONSIST_QUERY
        assert reply.body[2] == 2   # size
        assert reply.body[3] == 1   # this index
        assert reply.body[4:6] == b"\x00\x03"   # hidden + reverse
        assert reply.body[6:12] == MEMBER_B.bytes
    finally:
        cs.close()


async def test_query_reply_uses_invalid_sentinel_for_out_of_range() -> None:
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        head.consist_members = [ConsistMember(node_id=MEMBER_A)]
        a.sent.clear()
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_QUERY, 5]),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        r = AddressedReassembler()
        reply = None
        for f in a.sent:
            m = r.receive(f)
            if m is not None and m.mti == MTI_TRACTION_REPLY:
                reply = m
        assert reply is not None
        assert reply.body[2] == 1                        # actual size
        assert reply.body[3] == CONSIST_INDEX_INVALID    # sentinel
    finally:
        cs.close()


# ---- End-to-end: client <-> CS -------------------------------------------


async def test_end_to_end_attach_query_detach() -> None:
    """Client attaches two members, queries them back, detaches one,
    queries the list again. Full round-trip through the wire."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        client = TractionClient(b)
        try:
            await client.attach_consist_member(
                head.alias, ConsistMember(node_id=MEMBER_A),
            )
            await client.attach_consist_member(
                head.alias, ConsistMember(node_id=MEMBER_B, reverse=True),
            )
            await asyncio.sleep(0)

            size = await client.query_consist_size(head.alias, timeout=1.0)
            assert size == 2

            members = await client.query_consist_members(
                head.alias, timeout=1.0,
            )
            assert len(members) == 2
            assert members[0].node_id == MEMBER_A
            assert members[0].reverse is False
            assert members[1].node_id == MEMBER_B
            assert members[1].reverse is True

            await client.detach_consist_member(head.alias, MEMBER_A)
            await asyncio.sleep(0)

            after = await client.query_consist_members(head.alias, timeout=1.0)
            assert [m.node_id for m in after] == [MEMBER_B]
        finally:
            client.close()
    finally:
        cs.close()


async def test_empty_consist_query_returns_empty_list() -> None:
    """A hosted loco with no attached members should answer Query(0)
    with size=0 and the invalid sentinel -- the client returns []."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        client = TractionClient(b)
        try:
            members = await client.query_consist_members(
                head.alias, timeout=1.0,
            )
            assert members == []
        finally:
            client.close()
    finally:
        cs.close()


async def test_consist_query_does_not_acquire_dcc_throttle() -> None:
    """Consist config is CS bookkeeping -- it must not spuriously
    acquire a DCC slot for a loco we're only inspecting."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        assert head.throttle is None
        client = TractionClient(b)
        try:
            await client.query_consist_size(head.alias, timeout=1.0)
            # Still no acquired throttle.
            assert head.throttle is None
        finally:
            client.close()
    finally:
        cs.close()


# ---- CS bridge to DCC ConsistManager ------------------------------------


class _RecordingConsistManager:
    """Minimal ConsistManager stand-in that records every call. The
    CS uses a ``ConsistManager`` structural type (get/create/
    add_member/remove_member/delete); this fake implements those
    exact methods without wrapping the base class + a real
    ThrottleManager, which would require more setup than the test
    actually needs."""

    def __init__(self) -> None:
        self.consists: dict[int, list[tuple[int, bool, bool]]] = {}
        self.calls: list[tuple[str, tuple]] = []

    def get(self, address: int):  # noqa: ANN201
        return self.consists.get(address)

    async def create(self, address: int, *, kind=None, comment: str = ""):
        self.calls.append(("create", (address,)))
        self.consists.setdefault(address, [])

    async def add_member(
        self, consist_address: int, member_address: int,
        *, long_address: bool = False, direction_normal: bool = True,
    ):
        self.calls.append((
            "add_member",
            (consist_address, member_address, long_address, direction_normal),
        ))
        self.consists.setdefault(consist_address, []).append(
            (member_address, long_address, direction_normal),
        )
        return True

    async def remove_member(
        self, consist_address: int, member_address: int,
    ) -> bool:
        self.calls.append((
            "remove_member", (consist_address, member_address),
        ))
        members = self.consists.get(consist_address, [])
        remaining = [m for m in members if m[0] != member_address]
        if len(remaining) == len(members):
            return False
        self.consists[consist_address] = remaining
        return True

    async def delete(self, address: int) -> bool:
        self.calls.append(("delete", (address,)))
        return self.consists.pop(address, None) is not None


async def _cs_with_bridge() -> tuple[
    _FakeTc, _FakeTc, ThrottleManager, _RecordingConsistManager, CommandStationNode,
]:
    a, b = _pair(0x111, 0x222)
    throttles = ThrottleManager("X")
    dcc_consists = _RecordingConsistManager()
    cs = CommandStationNode(
        a, throttles, station_node_id=STATION_NID,
        dcc_consists=dcc_consists,
    )
    return a, b, throttles, dcc_consists, cs


async def test_bridge_attach_creates_dcc_consist_and_adds_member() -> None:
    """First attach -> CS creates the DCC consist at head loco's DCC
    address, then adds the member. Head is address 1, member decoded
    from NID 0x06.01.00.00.00.03 -> short address 3."""
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_A.bytes   # NID 0x06.01.00.00.00.03
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # DCC consist exists at address 1 with member 3.
        assert dcc.get(1) is not None
        assert (3, False, True) in dcc.get(1)
        # create + add_member both fired.
        call_names = [c[0] for c in dcc.calls]
        assert "create" in call_names
        assert "add_member" in call_names
    finally:
        cs.close()


async def test_bridge_attach_reuses_existing_dcc_consist() -> None:
    """Second attach -> DCC consist already exists, only add_member
    fires (no duplicate create)."""
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        for member_nid in (MEMBER_A, MEMBER_B):
            payload = (
                bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
                + member_nid.bytes
            )
            for frame in encode_addressed_frames(
                MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
            ):
                await b.send_frame(frame)
            await asyncio.sleep(0)
        assert len([c for c in dcc.calls if c[0] == "create"]) == 1
        assert len([c for c in dcc.calls if c[0] == "add_member"]) == 2
    finally:
        cs.close()


async def test_bridge_attach_passes_long_flag_from_nid() -> None:
    """MEMBER_LONG is 0x06.01.00.00.C0.11 = long address 17."""
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_LONG.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # Address 17, long_address=True.
        assert (17, True, True) in dcc.get(1)
    finally:
        cs.close()


async def test_bridge_attach_reverse_flag_becomes_direction_normal_false() -> None:
    """OpenLCB consist ``reverse`` flag <-> DCC ``direction_normal``.
    They're opposites -- reverse=True means direction_normal=False."""
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00,
                   CONSIST_FLAG_REVERSE])
            + MEMBER_A.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert (3, False, False) in dcc.get(1)   # direction_normal False
    finally:
        cs.close()


async def test_bridge_skips_foreign_nid() -> None:
    """A member NID outside the 0x06.01.00.00.* train range can't
    be mapped to a DCC address -- the CS still tracks it in memory
    but doesn't forward to DCC."""
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        stranger = NodeId.parse("02.99.99.99.99.99")
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + stranger.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # Still recorded in memory.
        assert len(head.consist_members) == 1
        assert head.consist_members[0].node_id == stranger
        # But no DCC-side calls.
        assert dcc.calls == []
    finally:
        cs.close()


async def test_bridge_detach_removes_dcc_member() -> None:
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        # Attach first.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # Then detach.
        dcc.calls.clear()
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_DETACH]) + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert ("remove_member", (1, 3)) in dcc.calls
        assert dcc.get(1) == []
    finally:
        cs.close()


async def test_bridge_remove_loco_dissolves_dcc_consist() -> None:
    """Removing the hosted head loco should delete the DCC-side
    consist so the CS's consist table stays in sync."""
    _, b, _, dcc, cs = await _cs_with_bridge()
    try:
        head = await cs.add_loco(1)
        # Attach one member so a consist exists on the DCC side.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert dcc.get(1) is not None
        await cs.remove_loco(1)
        assert dcc.get(1) is None
        assert ("delete", (1,)) in dcc.calls
    finally:
        cs.close()


async def test_no_bridge_when_dcc_consists_not_passed() -> None:
    """The default CommandStationNode has no ``dcc_consists`` -- the
    old in-memory-only behaviour is preserved so existing
    deployments don't regress. The DCC bridge is opt-in."""
    a, b, _, cs = await _cs()   # from earlier fixture -- no dcc_consists
    try:
        head = await cs.add_loco(1)
        # Attach a member; no DCC side effects should occur (no
        # ConsistManager to receive them, and no crash).
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, head.alias,
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH, 0x00, 0x00])
            + MEMBER_A.bytes,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        # Memory-side member tracked.
        assert len(head.consist_members) == 1
    finally:
        cs.close()


# ---- train_node_id_to_dcc unit tests -----------------------------------


def test_train_node_id_to_dcc_short() -> None:
    from jmri_openlcb import train_node_id_to_dcc  # noqa: PLC0415
    assert train_node_id_to_dcc(NodeId(0x06_01_00_00_00_03)) == (3, False)
    assert train_node_id_to_dcc(NodeId(0x06_01_00_00_00_7F)) == (127, False)


def test_train_node_id_to_dcc_long() -> None:
    from jmri_openlcb import train_node_id_to_dcc  # noqa: PLC0415
    assert train_node_id_to_dcc(NodeId(0x06_01_00_00_C0_03)) == (3, True)
    # 0xFFFF -> address 0x3FFF long.
    assert train_node_id_to_dcc(NodeId(0x06_01_00_00_FF_FF)) == (0x3FFF, True)


def test_train_node_id_to_dcc_returns_none_for_non_train_nid() -> None:
    """Anything outside 0x06.01.00.00.* is not a DCC train node."""
    from jmri_openlcb import train_node_id_to_dcc  # noqa: PLC0415
    assert train_node_id_to_dcc(NodeId(0x02_01_12_FE_DC_BA)) is None
    # Off-by-one in the prefix.
    assert train_node_id_to_dcc(NodeId(0x06_01_00_01_00_03)) is None


def test_train_node_id_to_dcc_rejects_malformed_short_range() -> None:
    """Low16 in 0x80..0xBFFF has bit 15 set but bit 14 clear -- that's
    neither a valid short (0..127) nor a valid long (bits 15..14=11)
    per NMRA baseline. Returns None rather than misinterpret."""
    from jmri_openlcb import train_node_id_to_dcc  # noqa: PLC0415
    assert train_node_id_to_dcc(NodeId(0x06_01_00_00_80_00)) is None


async def test_query_stops_early_when_reply_reports_size_zero() -> None:
    """Regression pin: query_consist_members must NOT loop forever
    when the CS reports size=0. The first Query(0) reply carries
    size=0 + sentinel index; the client should return [] immediately."""
    a, b, _, cs = await _cs()
    try:
        head = await cs.add_loco(1)
        assert head.consist_members == []
        client = TractionClient(b)
        try:
            got = await asyncio.wait_for(
                client.query_consist_members(head.alias, timeout=1.0),
                timeout=2.0,
            )
            assert got == []
        finally:
            client.close()
    finally:
        cs.close()
