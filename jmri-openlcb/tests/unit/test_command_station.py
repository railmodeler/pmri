"""Tests for the OpenLCB Command Station node.

Each hosted DCC address gets its own alias + train NodeID + full
protocol handling. Tests use a two-sided fake TC + a fake DCC
throttle manager so the wire behaviour + HAL bridging can be
verified together.
"""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.beans.throttle import Throttle
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.events import Signal
from jmri_core.state import ThrottleDirection

from jmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import (
    CTRL_AMD,
    CTRL_AME,
    CTRL_AMR,
    FT_CONTROL,
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
    extract_variable_field,
    make_ame_header,
)
from jmri_openlcb.command_station import (
    TRAIN_PIP_FLAGS,
    CommandStationNode,
    HostedLoco,
)
from jmri_openlcb.pip import ProtocolFlag, decode_flags
from jmri_openlcb.snip import MTI_SNIP_REPLY, MTI_SNIP_REQUEST, NodeInfo
from jmri_openlcb.throttle import MPH_PER_UNIT, dcc_to_train_node_id
from jmri_openlcb.traction import (
    CC_ASSIGN,
    CC_QUERY,
    CC_RELEASE,
    MTI_TRACTION_REPLY,
    MTI_TRACTION_REQUEST,
    TC_CONTROLLER_CONFIG,
    TC_EMERGENCY_STOP,
    TC_QUERY_FUNCTION,
    TC_QUERY_SPEED,
    TC_SET_FUNCTION,
    TC_SET_SPEED_DIR,
    decode_speed,
    encode_speed,
)
from jmri_openlcb.verify import (
    MTI_VERIFIED_NID,
    MTI_VERIFY_NID_ADDRESSED,
    MTI_VERIFY_NID_GLOBAL,
)


STATION_NID = NodeId.parse("02.01.12.FE.DC.BA")
CONTROLLER_NID = NodeId.parse("02.02.02.02.02.02")


class _FakeTc:
    """Two-sided fake -- frames on side A land on B's frame signal.
    Also allows sending a frame with an arbitrary source alias
    (needed for CID/RID/Verified NID from the CS's hosted aliases)."""

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


# ---- DCC-side fake -------------------------------------------------------


class _RecordingThrottle(Throttle):
    """Wraps the base Throttle to record method calls for assertion."""

    __slots__ = ("calls",)

    def __init__(self, address: int, long_address: bool) -> None:
        super().__init__(address=address, long_address=long_address)
        self.calls: list[tuple[str, ...]] = []  # type: ignore[assignment]

    async def set_speed(self, speed: float) -> None:  # type: ignore[override]
        self.calls.append(("set_speed", speed))
        await super().set_speed(speed)

    async def set_direction(self, direction: ThrottleDirection) -> None:  # type: ignore[override]
        self.calls.append(("set_direction", direction))
        await super().set_direction(direction)

    async def set_function(self, index: int, on: bool) -> None:  # type: ignore[override]
        self.calls.append(("set_function", index, on))
        await super().set_function(index, on)

    async def emergency_stop(self) -> None:  # type: ignore[override]
        self.calls.append(("emergency_stop",))
        await super().emergency_stop()


class _FakeThrottleManager(ThrottleManager):
    """Base ThrottleManager overridden to build _RecordingThrottle
    handles."""

    def __init__(self) -> None:
        super().__init__("X")
        self.releases: list[Throttle] = []

    async def _construct(  # type: ignore[override]
        self, address: int, *, long_address: bool,
    ) -> Throttle:
        return _RecordingThrottle(address=address, long_address=long_address)

    async def release(self, throttle: Throttle) -> None:  # type: ignore[override]
        self.releases.append(throttle)
        await super().release(throttle)


async def _bring_up_cs(
    throttles: _FakeThrottleManager | None = None,
) -> tuple[_FakeTc, _FakeTc, _FakeThrottleManager, CommandStationNode]:
    """Common setup: OlcbConnection <-> peer + CS + fake throttle mgr."""
    a, b = _pair(0x111, 0x222)
    dcc = throttles or _FakeThrottleManager()
    cs = CommandStationNode(a, dcc, station_node_id=STATION_NID)
    return a, b, dcc, cs


def _reassemble(tc: _FakeTc, expected_mti: int, source_alias: int | None = None):
    """Return the assembled addressed message with matching MTI (and
    optionally source alias). Raises if not found."""
    r = AddressedReassembler()
    for f in tc.sent:
        msg = r.receive(f)
        if msg is None or msg.mti != expected_mti:
            continue
        if source_alias is not None and msg.source_alias != source_alias:
            continue
        return msg
    raise AssertionError(
        f"no assembled {expected_mti:#06x} message in tc.sent",
    )


# ---- add_loco lifecycle --------------------------------------------------


async def test_add_loco_claims_alias_and_populates_maps() -> None:
    """add_loco should assign a valid alias for the DCC-derived NodeID
    and expose the hosted loco via get()."""
    a, _, _, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3, long_address=False)
        assert isinstance(loco, HostedLoco)
        assert loco.address == 3
        assert loco.long_address is False
        assert loco.node_id == dcc_to_train_node_id(3, long_address=False)
        assert 0 < loco.alias <= 0xFFF
        # Lookup by address.
        assert cs.get(3) is loco
        assert cs.get(3, long_address=True) is None
    finally:
        cs.close()


async def test_add_loco_is_idempotent() -> None:
    """A second add_loco for the same address returns the existing
    handle rather than claiming a fresh alias -- supports a "re-add
    all configured locos" loop on startup."""
    a, _, _, cs = await _bring_up_cs()
    try:
        first = await cs.add_loco(3)
        alias = first.alias
        second = await cs.add_loco(3)
        assert second is first
        assert second.alias == alias
    finally:
        cs.close()


async def test_canonical_name_reflects_address_form() -> None:
    """SNIP replies use the canonical name; short addresses get no
    prefix, long addresses get an ``L`` prefix so a UI can tell them
    apart at a glance."""
    a, _, _, cs = await _bring_up_cs()
    try:
        short = await cs.add_loco(3)
        long_ = await cs.add_loco(3601, long_address=True)
        assert short.canonical_name == "DCC 3"
        assert long_.canonical_name == "DCC L3601"
    finally:
        cs.close()


async def test_remove_loco_sends_amr_and_drops_registration() -> None:
    """AMR carries variable field 0x703 + 6-byte NID; matches what
    the alias machinery expects for a graceful alias release."""
    a, _, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        alias = loco.alias
        a.sent.clear()
        await cs.remove_loco(3)
        # AMR frame present.
        amrs = [
            f for f in a.sent
            if extract_frame_type(f.header) == 0x0
            and extract_variable_field(f.header) == CTRL_AMR
        ]
        assert len(amrs) == 1
        assert extract_source_alias(amrs[0].header) == alias
        assert amrs[0].data == loco.node_id.bytes
        # Dropped from registration.
        assert cs.get(3) is None
    finally:
        cs.close()


async def test_remove_loco_is_safe_on_unknown_address() -> None:
    """Removing a loco we never hosted is a no-op -- doesn't raise
    and doesn't send anything on the wire."""
    a, _, _, cs = await _bring_up_cs()
    try:
        await cs.remove_loco(99)
        assert a.sent == []
    finally:
        cs.close()


# ---- Verify NID answering ----------------------------------------------


async def test_verify_global_with_matching_nid_provokes_reply() -> None:
    """Verify NID Global carrying a hosted train's NID must draw a
    Verified NID reply from the hosted alias."""
    a, b, _, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        a.sent.clear()
        # Peer sends targeted Verify NID Global.
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_GLOBAL, 0x222),
            data=loco.node_id.bytes,
        ))
        await asyncio.sleep(0)
        verified = [f for f in a.sent if extract_mti(f.header) == MTI_VERIFIED_NID]
        assert len(verified) == 1
        assert extract_source_alias(verified[0].header) == loco.alias
        assert verified[0].data == loco.node_id.bytes
    finally:
        cs.close()


async def test_verify_global_empty_data_is_ignored_by_cs() -> None:
    """Empty-data Verify NID Global draws a reply from every node.
    The CS must NOT amplify that -- 50 hosted locos would emit 50
    Verified NIDs and drown the bus. The connection's main
    VerifyNodeIdService still answers for the station node."""
    a, b, _, cs = await _bring_up_cs()
    try:
        await cs.add_loco(3)
        await cs.add_loco(4)
        a.sent.clear()
        await b.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFY_NID_GLOBAL, 0x222),
            data=b"",
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        cs.close()


async def test_verify_addressed_at_train_alias_provokes_reply() -> None:
    a, b, _, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        a.sent.clear()
        for frame in encode_addressed_frames(
            MTI_VERIFY_NID_ADDRESSED, 0x222, loco.alias, b"",
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        verified = [f for f in a.sent if extract_mti(f.header) == MTI_VERIFIED_NID]
        assert len(verified) == 1
        assert extract_source_alias(verified[0].header) == loco.alias
    finally:
        cs.close()


# ---- SNIP answering -----------------------------------------------------


async def test_snip_request_returns_canonical_name() -> None:
    """SNIP reply from a train alias carries a NodeInfo whose
    ``user_name`` is the canonical DCC-derived name."""
    a, b, _, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        a.sent.clear()
        for frame in encode_addressed_frames(
            MTI_SNIP_REQUEST, 0x222, loco.alias, b"",
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = _reassemble(a, MTI_SNIP_REPLY, source_alias=loco.alias)
        info = NodeInfo.decode(reply.body)
        assert info.user_name == "DCC 3"
        assert "loco address 3" in info.user_description
    finally:
        cs.close()


# ---- PIP answering ------------------------------------------------------


async def test_pip_reply_advertises_train_flags() -> None:
    """Hosted trains advertise TRACTION + SNIP + IDENTIFICATION but
    NOT COMMAND_STATION (that's for the station node, not a loco).
    FDI + MEMORY_CONFIG only advertised when the CS has a datagram
    engine wired to serve them -- this fixture has none."""
    a, b, _, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        a.sent.clear()
        for frame in encode_addressed_frames(
            0x0828,   # MTI_PIP_INQUIRY
            0x222, loco.alias, b"",
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = _reassemble(a, 0x0668, source_alias=loco.alias)
        flags = decode_flags(reply.body)
        assert ProtocolFlag.TRACTION in flags
        assert ProtocolFlag.SNIP in flags
        assert ProtocolFlag.IDENTIFICATION in flags
        assert ProtocolFlag.COMMAND_STATION not in flags
        # No datagram engine -> FDI + MEMORY_CONFIG are NOT advertised
        # (would be a lie -- we don't serve them without the engine).
        assert ProtocolFlag.FDI not in flags
        assert ProtocolFlag.MEMORY_CONFIG not in flags
        assert flags == cs.train_pip_flags
    finally:
        cs.close()


# ---- Traction command handling ------------------------------------------


async def test_set_speed_forward_bridges_to_dcc_throttle() -> None:
    """Set Speed/Dir forward 50 mph -> DCC throttle sees set_speed
    at normalized 0.5 with direction FORWARD."""
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        payload = bytes([TC_SET_SPEED_DIR]) + encode_speed(50.0, forward=True)
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert isinstance(loco.throttle, _RecordingThrottle)
        # Direction call should precede speed call so the loco knows
        # which way "forward" is before it starts moving.
        assert loco.throttle.direction == ThrottleDirection.FORWARD
        # Half-precision loses precision; allow slop.
        assert abs(loco.throttle.speed - 0.5) < 0.02
    finally:
        cs.close()


async def test_set_speed_reverse_flips_direction() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        payload = bytes([TC_SET_SPEED_DIR]) + encode_speed(25.0, forward=False)
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert loco.throttle is not None
        assert loco.throttle.direction == ThrottleDirection.REVERSE
        assert abs(loco.throttle.speed - 0.25) < 0.02
    finally:
        cs.close()


async def test_set_function_bridges_to_dcc_throttle() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        payload = (
            bytes([TC_SET_FUNCTION])
            + (5).to_bytes(3, "big")
            + (1).to_bytes(2, "big")
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert loco.throttle is not None
        assert loco.throttle.functions[5] is True
    finally:
        cs.close()


async def test_set_function_over_range_dropped_silently() -> None:
    """Core Throttle only has F0..F31. A controller asking for F35
    shouldn't blow up the session -- silently drop rather than
    raising."""
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        payload = (
            bytes([TC_SET_FUNCTION])
            + (35).to_bytes(3, "big")
            + (1).to_bytes(2, "big")
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert loco.throttle is not None
        # No exception, no state change on any tracked function.
        assert all(v is False for v in loco.throttle.functions)
    finally:
        cs.close()


async def test_emergency_stop_calls_throttle_emergency_stop() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_EMERGENCY_STOP]),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert isinstance(loco.throttle, _RecordingThrottle)
        assert ("emergency_stop",) in loco.throttle.calls
        assert loco.throttle.speed == -1.0
    finally:
        cs.close()


# ---- Controller assign / release / query --------------------------------


async def test_assign_acquires_throttle_and_stores_controller() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        assert loco.throttle is None
        payload = (
            bytes([TC_CONTROLLER_CONFIG, CC_ASSIGN, 0x00])
            + CONTROLLER_NID.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert loco.throttle is not None
        assert loco.controller == CONTROLLER_NID
    finally:
        cs.close()


async def test_release_frees_throttle_and_clears_controller() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        # Assign first.
        assign = (
            bytes([TC_CONTROLLER_CONFIG, CC_ASSIGN, 0x00])
            + CONTROLLER_NID.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, assign,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        acquired_throttle = loco.throttle
        assert acquired_throttle is not None
        # Now release.
        release = (
            bytes([TC_CONTROLLER_CONFIG, CC_RELEASE, 0x00])
            + CONTROLLER_NID.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, release,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert loco.throttle is None
        assert loco.controller == NodeId(0)
        # DCC-side release was called.
        assert acquired_throttle in dcc.releases
    finally:
        cs.close()


async def test_query_controller_returns_current_owner() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        # Assign so there's an owner to report.
        assign = (
            bytes([TC_CONTROLLER_CONFIG, CC_ASSIGN, 0x00])
            + CONTROLLER_NID.bytes
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, assign,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        a.sent.clear()
        # Query controller.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_CONTROLLER_CONFIG, CC_QUERY]),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = _reassemble(a, MTI_TRACTION_REPLY, source_alias=loco.alias)
        assert reply.body[0] == TC_CONTROLLER_CONFIG
        assert reply.body[1] == CC_QUERY
        assert reply.body[3:9] == CONTROLLER_NID.bytes
    finally:
        cs.close()


async def test_query_controller_reports_zero_nid_when_unassigned() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_CONTROLLER_CONFIG, CC_QUERY]),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = _reassemble(a, MTI_TRACTION_REPLY, source_alias=loco.alias)
        assert reply.body[3:9] == bytes(6)
    finally:
        cs.close()


# ---- Query speed / function replies -------------------------------------


async def test_query_speed_reports_bean_state() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        # Set some state on the throttle first.
        set_speed_payload = (
            bytes([TC_SET_SPEED_DIR]) + encode_speed(50.0, forward=True)
        )
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias, set_speed_payload,
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        a.sent.clear()
        # Now query.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_QUERY_SPEED]),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = _reassemble(a, MTI_TRACTION_REPLY, source_alias=loco.alias)
        assert reply.body[0] == TC_QUERY_SPEED
        mph, forward = decode_speed(reply.body[1:3])
        assert abs(mph - 50.0) < 1.0
        assert forward is True
    finally:
        cs.close()


async def test_query_function_reports_bean_state() -> None:
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco = await cs.add_loco(3)
        # Set F7 on.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_SET_FUNCTION])
            + (7).to_bytes(3, "big") + (1).to_bytes(2, "big"),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        a.sent.clear()
        # Query F7.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_QUERY_FUNCTION]) + (7).to_bytes(3, "big"),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = _reassemble(a, MTI_TRACTION_REPLY, source_alias=loco.alias)
        assert reply.body[0] == TC_QUERY_FUNCTION
        assert int.from_bytes(reply.body[1:4], "big") == 7
        assert int.from_bytes(reply.body[4:6], "big") == 1
    finally:
        cs.close()


# ---- Isolation between hosted locos -------------------------------------


async def test_commands_are_isolated_per_hosted_alias() -> None:
    """A command targeting loco A's alias must not touch loco B.
    Regression pin against a bug where the CS could route by MTI
    alone and clobber the wrong throttle."""
    a, b, dcc, cs = await _bring_up_cs()
    try:
        loco_a = await cs.add_loco(3)
        loco_b = await cs.add_loco(4)
        # Set speed on loco_a only.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco_a.alias,
            bytes([TC_SET_SPEED_DIR]) + encode_speed(75.0, forward=True),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert loco_a.throttle is not None
        assert loco_b.throttle is None
        assert abs(loco_a.throttle.speed - 0.75) < 0.02
    finally:
        cs.close()


async def test_ame_empty_data_provokes_amd_from_every_hosted_train() -> None:
    """AME with no data -> each hosted train replies with AMD. Peers
    use this to bulk-populate their alias<->NID maps on startup, so
    every hosted node must answer per S-9.7.0.1 sec.6.3.4."""
    a, b, _, cs = await _bring_up_cs()
    try:
        loco_a = await cs.add_loco(3)
        loco_b = await cs.add_loco(4)
        loco_c = await cs.add_loco(3601, long_address=True)
        a.sent.clear()
        # Peer B fires a global AME.
        await b.send_frame(OlcbFrame(
            header=make_ame_header(0x222), data=b"",
        ))
        await asyncio.sleep(0)
        # Three AMDs, one per hosted train.
        amds = [
            f for f in a.sent
            if extract_frame_type(f.header) == FT_CONTROL
            and extract_variable_field(f.header) == CTRL_AMD
        ]
        assert len(amds) == 3
        alias_to_nid = {
            extract_source_alias(f.header): f.data for f in amds
        }
        assert alias_to_nid[loco_a.alias] == loco_a.node_id.bytes
        assert alias_to_nid[loco_b.alias] == loco_b.node_id.bytes
        assert alias_to_nid[loco_c.alias] == loco_c.node_id.bytes
    finally:
        cs.close()


async def test_ame_targeted_nid_provokes_only_matching_amd() -> None:
    """AME with 6-byte NID data -> only the matching hosted train
    replies. Peers use this when they know a NID and just want to
    resolve its current alias without a bus-wide sweep."""
    a, b, _, cs = await _bring_up_cs()
    try:
        loco_a = await cs.add_loco(3)
        loco_b = await cs.add_loco(4)
        a.sent.clear()
        # Probe for loco_a's NID specifically.
        await b.send_frame(OlcbFrame(
            header=make_ame_header(0x222), data=loco_a.node_id.bytes,
        ))
        await asyncio.sleep(0)
        amds = [
            f for f in a.sent
            if extract_frame_type(f.header) == FT_CONTROL
            and extract_variable_field(f.header) == CTRL_AMD
        ]
        assert len(amds) == 1
        assert extract_source_alias(amds[0].header) == loco_a.alias
    finally:
        cs.close()


async def test_ame_targeted_at_unhosted_nid_stays_silent() -> None:
    """An AME probe for a NID we don't host must not provoke a
    reply -- otherwise every CS on a shared bus would echo probes
    aimed at another CS's trains."""
    a, b, _, cs = await _bring_up_cs()
    try:
        await cs.add_loco(3)
        a.sent.clear()
        stranger_nid = NodeId.parse("02.99.99.99.99.99")
        await b.send_frame(OlcbFrame(
            header=make_ame_header(0x222), data=stranger_nid.bytes,
        ))
        await asyncio.sleep(0)
        amds = [
            f for f in a.sent
            if extract_frame_type(f.header) == FT_CONTROL
            and extract_variable_field(f.header) == CTRL_AMD
        ]
        assert amds == []
    finally:
        cs.close()


async def test_ame_before_any_add_loco_is_no_op() -> None:
    """AME on a CS with zero hosted trains just does nothing --
    doesn't raise, doesn't send anything."""
    a, b, _, cs = await _bring_up_cs()
    try:
        await b.send_frame(OlcbFrame(
            header=make_ame_header(0x222), data=b"",
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        cs.close()


# ---- CS-side FDI serving --------------------------------------------------


async def test_add_loco_registers_alias_with_datagram_engine() -> None:
    """When constructed with a datagram engine, add_loco should
    stand up a per-loco memory responder and register the alias
    with the engine so inbound datagrams reach it."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, OlcbTrafficController, loopback_pair,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine = DatagramEngine(tc_a)
    await engine.attach()
    dcc = _FakeThrottleManager()
    cs = CommandStationNode(
        tc_a, dcc, station_node_id=STATION_NID,
        datagram_engine=engine,
    )
    try:
        loco = await cs.add_loco(3)
        # Loco carries its own memory responder.
        assert loco.memory_responder is not None
        # Engine now routes datagrams for both our alias + the loco's.
        aliases = engine._target_aliases()  # noqa: SLF001
        assert 0x111 in aliases
        assert loco.alias in aliases
        # SPACE_FDI is populated with the synthesized XML.
        from jmri_openlcb import SPACE_FDI   # noqa: PLC0415
        raw = loco.memory_responder.get_space(SPACE_FDI)
        assert raw is not None
        assert b"Headlight" in raw
        assert b"Function 1" in raw
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_hosted_train_serves_fdi_over_the_wire() -> None:
    """End-to-end: a peer reads the hosted train's SPACE_FDI and
    gets back the synthesized XML. The reply's source alias is the
    train's alias (not our root), so the peer's routing works."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        SPACE_FDI, loopback_pair, parse_fdi_bytes,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine_a = DatagramEngine(tc_a)
    await engine_a.attach()
    engine_b = DatagramEngine(tc_b)
    await engine_b.attach()
    dcc = _FakeThrottleManager()
    cs = CommandStationNode(
        tc_a, dcc, station_node_id=STATION_NID,
        datagram_engine=engine_a,
    )
    peer_client = MemoryClient(engine_b)
    try:
        loco = await cs.add_loco(3)
        # Peer reads FDI targeting the loco's alias. The synthesized
        # 29-function XML is ~3 KiB; ask for plenty so we get the
        # null terminator.
        raw = await peer_client.read(loco.alias, SPACE_FDI, 0, 8192)
        term = raw.find(b"\x00")
        payload = raw[:term] if term >= 0 else raw
        fdi = parse_fdi_bytes(payload + b"\x00")
        # Every function 0..28 present.
        numbers = {fn.number for fn in fdi.flatten()}
        assert numbers == set(range(29))
        # F0 gets the canonical Headlight label.
        assert fdi.by_number(0).name == "Headlight"
        # F7 gets the generic label.
        assert fdi.by_number(7).name == "Function 7"
    finally:
        peer_client.close()
        cs.close()
        await engine_a.detach()
        await engine_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_hosted_train_pip_advertises_fdi_when_engine_wired() -> None:
    """With a datagram engine, hosted trains include FDI +
    MEMORY_CONFIG in their PIP flags -- the truthful advertisement."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, OlcbTrafficController, loopback_pair,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine = DatagramEngine(tc_a)
    await engine.attach()
    dcc = _FakeThrottleManager()
    cs = CommandStationNode(
        tc_a, dcc, station_node_id=STATION_NID,
        datagram_engine=engine,
    )
    try:
        flags = cs.train_pip_flags
        assert ProtocolFlag.FDI in flags
        assert ProtocolFlag.MEMORY_CONFIG in flags
        assert ProtocolFlag.TRACTION in flags
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_remove_loco_closes_memory_responder_and_unregisters_alias() -> None:
    """After remove_loco, the alias is no longer routed to and the
    responder is closed."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, OlcbTrafficController, loopback_pair,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine = DatagramEngine(tc_a)
    await engine.attach()
    dcc = _FakeThrottleManager()
    cs = CommandStationNode(
        tc_a, dcc, station_node_id=STATION_NID,
        datagram_engine=engine,
    )
    try:
        loco = await cs.add_loco(3)
        alias = loco.alias
        assert alias in engine._target_aliases()  # noqa: SLF001
        await cs.remove_loco(3)
        assert alias not in engine._target_aliases()  # noqa: SLF001
        # The HostedLoco reference we grabbed still holds -- its
        # responder was closed but the dataclass slot lets us
        # verify the cleanup happened.
        assert loco.memory_responder is None
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


# ---- CS-side CDI serving --------------------------------------------------


async def test_hosted_train_serves_cdi_describing_user_settings() -> None:
    """Peer reads SPACE_CDI on hosted train alias, gets XML
    describing the two user_name/user_description string fields."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        SPACE_CDI, loopback_pair, parse_cdi_bytes,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine_a = DatagramEngine(tc_a)
    await engine_a.attach()
    engine_b = DatagramEngine(tc_b)
    await engine_b.attach()
    cs = CommandStationNode(
        tc_a, _FakeThrottleManager(),
        station_node_id=STATION_NID,
        datagram_engine=engine_a,
    )
    peer_client = MemoryClient(engine_b)
    try:
        loco = await cs.add_loco(3)
        raw = await peer_client.read(loco.alias, SPACE_CDI, 0, 2048)
        term = raw.find(b"\x00")
        payload = raw[:term] if term >= 0 else raw
        cdi = parse_cdi_bytes(payload + b"\x00")
        seg = cdi.segments[0]
        assert seg.space == 0xFE
        # Two string fields: user_name + user_description.
        assert len(seg.children) == 2
        assert seg.children[0].name == "User name"
        assert seg.children[0].size == 63
        assert seg.children[1].name == "User description"
        assert seg.children[1].size == 128
    finally:
        peer_client.close()
        cs.close()
        await engine_a.detach()
        await engine_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_user_name_write_reflects_in_display_and_snip() -> None:
    """Operator writes a user_name via memory config -> HostedLoco's
    display_name switches to it AND subsequent SNIP replies use
    it as the user_name field."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        loopback_pair,
    )
    from jmri_openlcb.snip import NodeInfo   # noqa: PLC0415
    from jmri_openlcb.addressed import (   # noqa: PLC0415
        AddressedReassembler, encode_addressed_frames,
    )
    from jmri_openlcb.snip import (   # noqa: PLC0415
        MTI_SNIP_REPLY, MTI_SNIP_REQUEST,
    )

    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine_a = DatagramEngine(tc_a)
    await engine_a.attach()
    engine_b = DatagramEngine(tc_b)
    await engine_b.attach()
    cs = CommandStationNode(
        tc_a, _FakeThrottleManager(),
        station_node_id=STATION_NID,
        datagram_engine=engine_a,
    )
    peer_client = MemoryClient(engine_b)
    try:
        loco = await cs.add_loco(3)
        # Fresh: display_name = canonical, user_name empty.
        assert loco.user_name == ""
        assert loco.display_name == "DCC 3"
        # Peer writes a user name via SPACE_CONFIG at offset 0.
        await peer_client.write(loco.alias, 0xFE, 0, b"Big Boy\x00")
        # HostedLoco properties read from the responder -- updated
        # immediately.
        assert loco.user_name == "Big Boy"
        assert loco.display_name == "Big Boy"
        # And a SNIP probe now returns Big Boy in the user_name slot.
        # Capture the reply by reassembling frames from tc_a's sent
        # log via the peer's traffic controller (fires on the peer's
        # inbound frames signal).
        replies: list[bytes] = []
        r = AddressedReassembler()

        async def _capture(f):
            m = r.receive(f)
            if m is not None and m.mti == MTI_SNIP_REPLY:
                replies.append(bytes(m.body))

        tc_b.frames.connect(_capture)
        # Send the SNIP request from peer -> hosted train.
        for f in encode_addressed_frames(
            MTI_SNIP_REQUEST, 0x222, loco.alias, b"",
        ):
            await tc_b.send_frame(f)
        # Give the reply a couple of ticks to arrive.
        for _ in range(3):
            await asyncio.sleep(0)
        assert replies, "no SNIP reply captured"
        info = NodeInfo.decode(replies[-1])
        assert info.user_name == "Big Boy"
    finally:
        peer_client.close()
        cs.close()
        await engine_a.detach()
        await engine_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_user_description_write_populates_snip_description() -> None:
    """A second SPACE_CONFIG slot holds the description; SNIP
    populates its user_description from that slot."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        loopback_pair,
    )
    from jmri_openlcb.command_station import (   # noqa: PLC0415
        CDI_USER_DESC_OFFSET,
    )

    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine_a = DatagramEngine(tc_a)
    await engine_a.attach()
    engine_b = DatagramEngine(tc_b)
    await engine_b.attach()
    cs = CommandStationNode(
        tc_a, _FakeThrottleManager(),
        station_node_id=STATION_NID,
        datagram_engine=engine_a,
    )
    peer_client = MemoryClient(engine_b)
    try:
        loco = await cs.add_loco(3)
        await peer_client.write(
            loco.alias, 0xFE, CDI_USER_DESC_OFFSET,
            b"UP #4014 - Big Boy - restored 2019\x00",
        )
        assert loco.user_description.startswith("UP #4014")
    finally:
        peer_client.close()
        cs.close()
        await engine_a.detach()
        await engine_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_user_name_properties_return_empty_when_no_responder() -> None:
    """A CS built without a datagram engine has no responder for
    hosted trains; user_name/user_description return empty and
    display_name falls through to canonical."""
    a, _, _, cs = await _bring_up_cs()   # existing no-engine fixture
    try:
        loco = await cs.add_loco(3)
        assert loco.memory_responder is None
        assert loco.user_name == ""
        assert loco.user_description == ""
        assert loco.display_name == "DCC 3"
    finally:
        cs.close()


# ---- Hosted-loco persistence --------------------------------------------


async def _bring_up_cs_with_persist(persist_path):
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, OlcbTrafficController, loopback_pair,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine = DatagramEngine(tc_a)
    await engine.attach()
    cs = CommandStationNode(
        tc_a, _FakeThrottleManager(),
        station_node_id=STATION_NID,
        datagram_engine=engine,
        persist_path=str(persist_path),
    )
    return tc_a, tc_b, engine, cs


async def test_add_loco_saves_persistence_file(tmp_path) -> None:
    """First add_loco creates the file with one entry."""
    persist = tmp_path / "hosted.json"
    tc_a, tc_b, engine, cs = await _bring_up_cs_with_persist(persist)
    try:
        await cs.add_loco(3)
        assert persist.exists()
        import json  # noqa: PLC0415
        data = json.loads(persist.read_text())
        assert len(data) == 1
        assert data[0]["address"] == 3
        assert data[0]["long_address"] is False
        assert data[0]["user_name"] == ""
        assert data[0]["user_description"] == ""
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_remove_loco_updates_persistence_file(tmp_path) -> None:
    persist = tmp_path / "hosted.json"
    tc_a, tc_b, engine, cs = await _bring_up_cs_with_persist(persist)
    try:
        await cs.add_loco(3)
        await cs.add_loco(4)
        await cs.remove_loco(3)
        import json  # noqa: PLC0415
        data = json.loads(persist.read_text())
        assert [e["address"] for e in data] == [4]
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_user_name_write_persists(tmp_path) -> None:
    """CDI-editor writes to SPACE_CONFIG trigger the on_write
    callback which snapshots the current user_name into the JSON."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        loopback_pair,
    )
    persist = tmp_path / "hosted.json"
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine_a = DatagramEngine(tc_a)
    await engine_a.attach()
    engine_b = DatagramEngine(tc_b)
    await engine_b.attach()
    cs = CommandStationNode(
        tc_a, _FakeThrottleManager(),
        station_node_id=STATION_NID,
        datagram_engine=engine_a,
        persist_path=str(persist),
    )
    peer_client = MemoryClient(engine_b)
    try:
        loco = await cs.add_loco(3)
        # Peer writes a user name via memory-config write.
        await peer_client.write(loco.alias, 0xFE, 0, b"Big Boy\x00")
        # File now carries the user name.
        import json  # noqa: PLC0415
        data = json.loads(persist.read_text())
        assert data[0]["user_name"] == "Big Boy"
    finally:
        peer_client.close()
        cs.close()
        await engine_a.detach()
        await engine_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_restore_from_disk_replays_saved_locos(tmp_path) -> None:
    """Seed the file directly, restore, verify the CS has the same
    hosted trains with user_name preserved."""
    persist = tmp_path / "hosted.json"
    persist.write_text("""[
        {"address": 3, "long_address": false, "user_name": "Big Boy",
         "user_description": "UP #4014"},
        {"address": 3601, "long_address": true, "user_name": "Sd40",
         "user_description": ""}
    ]""", encoding="utf-8")
    tc_a, tc_b, engine, cs = await _bring_up_cs_with_persist(persist)
    try:
        restored = await cs.restore_from_disk()
        assert len(restored) == 2
        by_addr = {(loco.address, loco.long_address): loco for loco in restored}
        short_3 = by_addr[(3, False)]
        long_3601 = by_addr[(3601, True)]
        assert short_3.user_name == "Big Boy"
        assert short_3.user_description == "UP #4014"
        assert long_3601.user_name == "Sd40"
        assert long_3601.display_name == "Sd40"
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_restore_missing_file_is_noop(tmp_path) -> None:
    persist = tmp_path / "does-not-exist.json"
    tc_a, tc_b, engine, cs = await _bring_up_cs_with_persist(persist)
    try:
        restored = await cs.restore_from_disk()
        assert restored == []
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_restore_skips_malformed_entries(tmp_path) -> None:
    """A single garbage entry doesn't kill the whole restore -- good
    entries around it still land."""
    persist = tmp_path / "hosted.json"
    persist.write_text("""[
        {"address": 3, "long_address": false},
        "not an object",
        {"long_address": false},
        {"address": 5, "long_address": false}
    ]""", encoding="utf-8")
    tc_a, tc_b, engine, cs = await _bring_up_cs_with_persist(persist)
    try:
        restored = await cs.restore_from_disk()
        addrs = {loco.address for loco in restored}
        assert addrs == {3, 5}
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_restore_malformed_json_returns_empty(tmp_path) -> None:
    persist = tmp_path / "hosted.json"
    persist.write_text("this is not JSON", encoding="utf-8")
    tc_a, tc_b, engine, cs = await _bring_up_cs_with_persist(persist)
    try:
        restored = await cs.restore_from_disk()
        assert restored == []
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_persist_off_when_no_path(tmp_path) -> None:
    """CS without persist_path skips the file entirely -- no crashes
    on add/remove, no file created next to tmp_path."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, OlcbTrafficController, loopback_pair,
    )
    port_a, port_b = loopback_pair()
    tc_a = OlcbTrafficController(port_a, source_alias=0x111)
    tc_b = OlcbTrafficController(port_b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    engine = DatagramEngine(tc_a)
    await engine.attach()
    cs = CommandStationNode(
        tc_a, _FakeThrottleManager(),
        station_node_id=STATION_NID,
        datagram_engine=engine,
    )
    try:
        await cs.add_loco(3)
        await cs.remove_loco(3)
        # tmp_path stays empty since no persist_path was configured.
        assert list(tmp_path.iterdir()) == []
    finally:
        cs.close()
        await engine.detach()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_add_then_reopen_restores_state(tmp_path) -> None:
    """End-to-end persistence: add locos in one CS lifecycle, tear
    down + rebuild with a fresh CS pointing at the same file,
    restore, verify the same trains + user names are hosted."""
    from jmri_openlcb import (   # noqa: PLC0415
        DatagramEngine, MemoryClient, OlcbTrafficController,
        loopback_pair,
    )
    persist = tmp_path / "hosted.json"

    async def _one_cycle():
        port_a, port_b = loopback_pair()
        tc_a = OlcbTrafficController(port_a, source_alias=0x111)
        tc_b = OlcbTrafficController(port_b, source_alias=0x222)
        await tc_a.start()
        await tc_b.start()
        engine_a = DatagramEngine(tc_a)
        await engine_a.attach()
        engine_b = DatagramEngine(tc_b)
        await engine_b.attach()
        cs = CommandStationNode(
            tc_a, _FakeThrottleManager(),
            station_node_id=STATION_NID,
            datagram_engine=engine_a,
            persist_path=str(persist),
        )
        return tc_a, tc_b, engine_a, engine_b, cs

    async def _teardown(tc_a, tc_b, engine_a, engine_b, cs):
        cs.close()
        await engine_a.detach()
        await engine_b.detach()
        await tc_a.aclose()
        await tc_b.aclose()

    # Cycle 1: add a loco + set its user_name via memory write.
    tc_a, tc_b, engine_a, engine_b, cs1 = await _one_cycle()
    peer_client = MemoryClient(engine_b)
    try:
        loco = await cs1.add_loco(7)
        await peer_client.write(loco.alias, 0xFE, 0, b"Little Guy\x00")
    finally:
        peer_client.close()
        await _teardown(tc_a, tc_b, engine_a, engine_b, cs1)

    # Cycle 2: fresh CS reads the same file and restores.
    tc_a2, tc_b2, engine_a2, engine_b2, cs2 = await _one_cycle()
    try:
        restored = await cs2.restore_from_disk()
        assert len(restored) == 1
        assert restored[0].address == 7
        assert restored[0].user_name == "Little Guy"
    finally:
        await _teardown(tc_a2, tc_b2, engine_a2, engine_b2, cs2)


async def test_command_for_unknown_alias_is_ignored() -> None:
    """A command targeting an alias we don't host must not
    accidentally acquire a DCC throttle or emit a reply."""
    a, b, dcc, cs = await _bring_up_cs()
    try:
        # No locos added.
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x999,
            bytes([TC_SET_SPEED_DIR]) + encode_speed(50.0, forward=True),
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert a.sent == []
        assert dcc.releases == []
    finally:
        cs.close()
