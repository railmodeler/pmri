"""Tests for OlcbThrottle + OlcbThrottleManager."""
from __future__ import annotations

import asyncio

import pytest
from pmri_core.events import Signal
from pmri_core.state import ThrottleDirection

from pmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from pmri_openlcb.alias import NodeId
from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_mti,
)
from pmri_openlcb.throttle import (
    MPH_PER_UNIT,
    OlcbThrottle,
    OlcbThrottleManager,
    dcc_to_train_node_id,
)
from pmri_openlcb.traction import (
    CC_ASSIGN,
    CC_RELEASE,
    MTI_TRACTION_REPLY,
    MTI_TRACTION_REQUEST,
    TC_CONTROLLER_CONFIG,
    TC_EMERGENCY_STOP,
    TC_SET_FUNCTION,
    TC_SET_SPEED_DIR,
    TractionClient,
    decode_speed,
)
from pmri_openlcb.verify import (
    MTI_VERIFIED_NID,
    MTI_VERIFY_NID_GLOBAL,
    NodeIdLookup,
    VerifyNodeIdService,
)


OUR_NID = NodeId.parse("02.01.12.FE.DC.BA")


# ---- dcc_to_train_node_id ------------------------------------------------


def test_short_dcc_address_maps_to_low_byte() -> None:
    """Short address N -> 0x06.01.00.00.00.NN. Regression pin against
    the S-9.7.5.1 sec.3 layout."""
    assert dcc_to_train_node_id(3, long_address=False) == NodeId(
        0x06_01_00_00_00_03,
    )
    assert dcc_to_train_node_id(127, long_address=False) == NodeId(
        0x06_01_00_00_00_7F,
    )


def test_long_dcc_address_sets_high_two_bits_of_low16() -> None:
    """Long address N -> 0x06.01.00.00.<0xC0 | Nhi>.<Nlo>. Matches
    the NMRA baseline packet's long-address discriminator (bits
    15..14 = 11)."""
    assert dcc_to_train_node_id(3, long_address=True) == NodeId(
        0x06_01_00_00_C0_03,
    )
    assert dcc_to_train_node_id(3601, long_address=True) == NodeId(
        0x06_01_00_00_00_00 | (0xC000 | 3601),
    )
    # Boundary -- largest 14-bit address (0x3FFF).
    assert dcc_to_train_node_id(0x3FFF, long_address=True) == NodeId(
        0x06_01_00_00_FF_FF,
    )


def test_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="0..127"):
        dcc_to_train_node_id(128, long_address=False)
    with pytest.raises(ValueError, match="0..16383"):
        dcc_to_train_node_id(0x4000, long_address=True)
    with pytest.raises(ValueError, match="0..127"):
        dcc_to_train_node_id(-1, long_address=False)


# ---- test fixtures -------------------------------------------------------


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


def _last_addressed_msg(tc: _FakeTc, expected_mti: int):
    """Reassemble the most-recently-sent addressed message with the
    given MTI. Used to verify wire-level payloads."""
    r = AddressedReassembler()
    last = None
    for f in tc.sent:
        msg = r.receive(f)
        if msg is not None and msg.mti == expected_mti:
            last = msg
    if last is None:
        raise AssertionError(
            f"no assembled {expected_mti:#06x} message in tc.sent",
        )
    return last


# ---- OlcbThrottle wire behaviour -----------------------------------------


async def test_set_speed_forward_sends_positive_mph() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.set_speed(0.5)
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.body[0] == TC_SET_SPEED_DIR
        mph, forward = decode_speed(msg.body[1:3])
        # Half-precision loses some precision -- allow slop.
        assert abs(mph - 0.5 * MPH_PER_UNIT) < 1.0
        assert forward is True
    finally:
        client.close()


async def test_set_speed_reverse_direction_encodes_negative_sign() -> None:
    """Direction lives in the sign bit of the speed frame -- a
    reverse-direction throttle should emit a negative-signed float."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.set_direction(ThrottleDirection.REVERSE)
        # Now speed changes should be sent with forward=False.
        a.sent.clear()
        await throttle.set_speed(0.25)
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        mph, forward = decode_speed(msg.body[1:3])
        assert abs(mph - 0.25 * MPH_PER_UNIT) < 1.0
        assert forward is False
    finally:
        client.close()


async def test_direction_flip_resends_speed_with_new_sign() -> None:
    """OpenLCB has no "just flip direction" opcode -- direction lives
    in the speed frame's sign bit. Flipping direction while running
    at 0.5 must resend the speed with the opposite sign, else the
    loco would keep going the old way."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.set_speed(0.5)   # forward at 0.5
        a.sent.clear()
        await throttle.set_direction(ThrottleDirection.REVERSE)
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.body[0] == TC_SET_SPEED_DIR
        mph, forward = decode_speed(msg.body[1:3])
        assert abs(mph - 0.5 * MPH_PER_UNIT) < 1.0
        assert forward is False
    finally:
        client.close()


async def test_set_speed_minus_one_sends_emergency_stop() -> None:
    """-1.0 in the core throttle abstraction is emergency stop. That
    must translate to the Emergency Stop opcode, not to a "negative
    speed" traction command -- the decoder halts without ramping."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.set_speed(-1.0)
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.body == bytes([TC_EMERGENCY_STOP])
    finally:
        client.close()


async def test_direction_change_after_estop_stays_silent() -> None:
    """After e-stop, the loco is halted. Flipping direction shouldn't
    re-arm the throttle by sending a speed frame -- the operator has
    to explicitly command speed again first."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.set_speed(-1.0)   # e-stop
        a.sent.clear()
        await throttle.set_direction(ThrottleDirection.REVERSE)
        assert a.sent == []
    finally:
        client.close()


async def test_set_function_sends_traction_set_function() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.set_function(5, True)
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.body[0] == TC_SET_FUNCTION
        assert msg.body[1:4] == (5).to_bytes(3, "big")
        assert msg.body[4:6] == (1).to_bytes(2, "big")
    finally:
        client.close()


async def test_emergency_stop_method_sends_estop_opcode() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        throttle = OlcbThrottle(
            address=3, long_address=False, alias=0x222,
            client=client, our_node_id=OUR_NID,
        )
        await throttle.emergency_stop()
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.body == bytes([TC_EMERGENCY_STOP])
    finally:
        client.close()


# ---- OlcbThrottleManager acquire flow ------------------------------------


async def test_acquire_resolves_nid_and_sends_assign_controller() -> None:
    """End-to-end acquire: a bus with a fake loco that answers Verify
    NID gets our throttle-manager to resolve the alias and fire
    Assign Controller. Two frames leave our side: the Verify probe,
    then the Assign."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    lookup = NodeIdLookup(a)
    verify_svc = VerifyNodeIdService(
        b, dcc_to_train_node_id(3, long_address=False),
    )
    try:
        mgr = OlcbThrottleManager(
            "X", client=client, lookup=lookup,
            our_node_id=OUR_NID, resolve_timeout=1.0,
        )
        throttle = await mgr.acquire(3, long_address=False)
        assert isinstance(throttle, OlcbThrottle)
        assert throttle.alias == 0x222
        # An Assign Controller carrying our NID must be on the wire.
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.dest_alias == 0x222
        assert msg.body[0] == TC_CONTROLLER_CONFIG
        assert msg.body[1] == CC_ASSIGN
        assert msg.body[3:9] == OUR_NID.bytes
    finally:
        verify_svc.close()
        lookup.close()
        client.close()


async def test_acquire_from_cache_skips_verify_probe() -> None:
    """If NodeIdLookup already knows the NID->alias mapping (because
    an earlier probe or unsolicited announcement populated it),
    ``acquire`` shouldn't fire a redundant Verify -- the resolve
    returns from cache."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    lookup = NodeIdLookup(a)
    try:
        # Pre-populate: the loco already announced.
        train_nid = dcc_to_train_node_id(3, long_address=False)
        await a.frames.emit(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, 0x222),
            data=train_nid.bytes,
        ))
        await asyncio.sleep(0)
        a.sent.clear()

        mgr = OlcbThrottleManager(
            "X", client=client, lookup=lookup,
            our_node_id=OUR_NID, resolve_timeout=1.0,
        )
        throttle = await mgr.acquire(3, long_address=False)
        assert throttle.alias == 0x222
        # Only frames sent should be the Assign Controller -- no Verify.
        mtis = [extract_mti(f.header) for f in a.sent]
        assert MTI_VERIFY_NID_GLOBAL not in mtis
    finally:
        lookup.close()
        client.close()


async def test_acquire_times_out_when_loco_absent() -> None:
    """No loco on the bus with our target NID -> resolve times out
    and ``acquire`` surfaces the error rather than returning a
    half-constructed throttle."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    lookup = NodeIdLookup(a)
    try:
        mgr = OlcbThrottleManager(
            "X", client=client, lookup=lookup,
            our_node_id=OUR_NID, resolve_timeout=0.05,
        )
        with pytest.raises(asyncio.TimeoutError):
            await mgr.acquire(99, long_address=False)
    finally:
        lookup.close()
        client.close()


async def test_release_sends_release_controller() -> None:
    """Releasing a throttle should hit the loco with Release
    Controller so a subsequent controller can attach cleanly."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    lookup = NodeIdLookup(a)
    verify_svc = VerifyNodeIdService(
        b, dcc_to_train_node_id(3, long_address=False),
    )
    try:
        mgr = OlcbThrottleManager(
            "X", client=client, lookup=lookup,
            our_node_id=OUR_NID, resolve_timeout=1.0,
        )
        throttle = await mgr.acquire(3, long_address=False)
        a.sent.clear()
        await mgr.release(throttle)
        # Release Controller uses the same 0x20 discriminator; the
        # sub-command byte is what changes.
        msg = _last_addressed_msg(a, MTI_TRACTION_REQUEST)
        assert msg.body[0] == TC_CONTROLLER_CONFIG
        assert msg.body[1] == CC_RELEASE
        assert msg.body[3:9] == OUR_NID.bytes
    finally:
        verify_svc.close()
        lookup.close()
        client.close()


async def test_repeated_acquire_returns_same_handle() -> None:
    """Base ThrottleManager semantics: a second acquire against the
    same address returns the existing handle without re-resolving.
    Guards against duplicate Assign frames on the wire."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    lookup = NodeIdLookup(a)
    verify_svc = VerifyNodeIdService(
        b, dcc_to_train_node_id(3, long_address=False),
    )
    try:
        mgr = OlcbThrottleManager(
            "X", client=client, lookup=lookup,
            our_node_id=OUR_NID, resolve_timeout=1.0,
        )
        t1 = await mgr.acquire(3, long_address=False)
        a.sent.clear()
        t2 = await mgr.acquire(3, long_address=False)
        assert t1 is t2
        # No fresh frames on the second acquire.
        assert a.sent == []
    finally:
        verify_svc.close()
        lookup.close()
        client.close()


# ---- connection wiring ---------------------------------------------------


async def test_connection_throttles_is_openlcb_manager() -> None:
    """After OlcbConnection.open, the ``throttles`` attribute should
    be the OpenLCB-aware manager, not the base placeholder."""
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    try:
        assert isinstance(conn.throttles, OlcbThrottleManager)
    finally:
        await conn.close()
