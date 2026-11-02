"""Tests for the OpenLCB Traction protocol client."""
from __future__ import annotations

import asyncio
import math
import struct

import pytest
from jmri_core.events import Signal

from jmri_openlcb.addressed import (
    AddressedMessage,
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import (
    OlcbFrame,
    extract_mti,
    extract_source_alias,
)
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
    TractionClient,
    decode_speed,
    encode_speed,
)


OUR_NID = NodeId.parse("02.01.12.FE.DC.BA")
LOCO_NID = NodeId.parse("06.01.01.00.00.42")


# ---- test doubles --------------------------------------------------------


class _FakeTc:
    """Two-sided fake -- frames sent on A land on B's frame signal."""

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


def _assembled(tc: _FakeTc, expected_mti: int = MTI_TRACTION_REQUEST):
    """Reassemble whatever the given fake TC sent into one
    :class:`AddressedMessage`. Assumes exactly one request in
    the buffer."""
    r = AddressedReassembler()
    for f in tc.sent:
        msg = r.receive(f)
        if msg is not None and msg.mti == expected_mti:
            return msg
    raise AssertionError(
        f"no assembled {expected_mti:#06x} message in tc.sent",
    )


async def _reply_from_loco(loco_tc: _FakeTc, dest_alias: int, body: bytes) -> None:
    """Send a Traction Control Reply back from the loco side."""
    for f in encode_addressed_frames(
        MTI_TRACTION_REPLY, loco_tc.source_alias, dest_alias, body,
    ):
        await loco_tc.send_frame(f)


# ---- half-precision float encoding --------------------------------------


def test_encode_speed_wire_bit_pattern() -> None:
    """Speed 0.0 forward -> 0x0000. Sign bit for direction."""
    assert encode_speed(0.0, forward=True) == b"\x00\x00"
    # 1.0 mph forward = float16 0x3C00 (big-endian 0x3C 0x00).
    assert encode_speed(1.0, forward=True) == b"\x3C\x00"
    # 1.0 mph reverse = -1.0 float16 = 0xBC00.
    assert encode_speed(1.0, forward=False) == b"\xBC\x00"


def test_decode_speed_round_trip() -> None:
    """Every 2-byte float that we produce should round-trip back to
    the same magnitude + direction."""
    for mph, forward in ((0.0, True), (1.0, True), (5.5, False),
                          (100.0, True), (0.25, False)):
        raw = encode_speed(mph, forward=forward)
        got_mph, got_fwd = decode_speed(raw)
        # Half-precision has ~3 decimal digits of precision; allow slop.
        assert abs(got_mph - mph) < 0.01
        # 0.0 encodes as forward = True (sign bit clear) per spec.
        if mph == 0.0:
            assert got_fwd is True
        else:
            assert got_fwd == forward


def test_decode_speed_rejects_short_buffer() -> None:
    with pytest.raises(ValueError, match="2 bytes"):
        decode_speed(b"\x00")


def test_negative_zero_decodes_as_reverse() -> None:
    """S-9.7.4.7 uses sign bit alone for direction, so ``0x8000``
    (negative-zero float16) decodes as reverse at speed 0."""
    mph, forward = decode_speed(b"\x80\x00")
    assert mph == 0.0
    assert forward is False


# ---- fire-and-forget commands -------------------------------------------


async def test_set_speed_sends_addressed_request_with_speed_dir_byte() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.set_speed(0x222, 50.0, forward=True)
        msg = _assembled(a)
        assert msg.dest_alias == 0x222
        assert msg.body[0] == TC_SET_SPEED_DIR
        assert msg.body[1:] == encode_speed(50.0, forward=True)
    finally:
        client.close()


async def test_set_function_encodes_3_byte_number_and_2_byte_value() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.set_function(0x222, function=17, value=1)
        msg = _assembled(a)
        assert msg.body[0] == TC_SET_FUNCTION
        assert msg.body[1:4] == (17).to_bytes(3, "big")
        assert msg.body[4:6] == (1).to_bytes(2, "big")
    finally:
        client.close()


async def test_set_function_rejects_out_of_range() -> None:
    tc = _FakeTc(0x111)
    client = TractionClient(tc)
    try:
        with pytest.raises(ValueError, match="24-bit"):
            await client.set_function(0x222, function=1 << 24, value=1)
        with pytest.raises(ValueError, match="16-bit"):
            await client.set_function(0x222, function=1, value=1 << 16)
    finally:
        client.close()


async def test_emergency_stop_carries_one_byte_payload() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.emergency_stop(0x222)
        msg = _assembled(a)
        assert msg.body == bytes([TC_EMERGENCY_STOP])
    finally:
        client.close()


async def test_assign_controller_includes_flag_byte_and_our_nid() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.assign_controller(0x222, OUR_NID)
        msg = _assembled(a)
        # Layout: 0x20, 0x01, 0x00 (flags), NID(6).
        assert msg.body[0] == TC_CONTROLLER_CONFIG
        assert msg.body[1] == CC_ASSIGN
        assert msg.body[2] == 0x00
        assert msg.body[3:9] == OUR_NID.bytes
    finally:
        client.close()


async def test_release_controller_mirrors_assign_with_release_subcommand() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.release_controller(0x222, OUR_NID)
        msg = _assembled(a)
        assert msg.body[0] == TC_CONTROLLER_CONFIG
        assert msg.body[1] == CC_RELEASE
        assert msg.body[3:9] == OUR_NID.bytes
    finally:
        client.close()


# ---- query / reply --------------------------------------------------------


async def test_query_speed_awaits_matching_reply() -> None:
    """End-to-end: send Query Speed -> loco side replies -> we get
    back a SpeedReply. The full reply layout: cmd(1) + set_speed(2)
    + status(1) + commanded(2) + actual(2) = 8 bytes."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        # Kick off the query.
        pending = asyncio.create_task(client.query_speed(0x222, timeout=1.0))
        await asyncio.sleep(0)
        # Loco replies. Set = 45 fwd; commanded = 40 fwd; actual = 35 fwd.
        reply_body = (
            bytes([TC_QUERY_SPEED])
            + encode_speed(45.0, forward=True)
            + bytes([0x00])                      # status
            + encode_speed(40.0, forward=True)   # commanded
            + encode_speed(35.0, forward=True)   # actual
        )
        await _reply_from_loco(b, 0x111, reply_body)
        reply = await pending
        assert abs(reply.set_speed_mph - 45.0) < 0.5
        assert reply.set_forward is True
        assert reply.status == 0x00
        assert reply.commanded_speed_mph is not None
        assert abs(reply.commanded_speed_mph - 40.0) < 0.5
        assert reply.actual_speed_mph is not None
        assert abs(reply.actual_speed_mph - 35.0) < 0.5
    finally:
        client.close()


async def test_query_speed_tolerates_truncated_reply() -> None:
    """A minimal decoder that only reports set_speed (no commanded /
    actual) -> those fields land as None."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(client.query_speed(0x222, timeout=1.0))
        await asyncio.sleep(0)
        # Body cut off after status byte -- no commanded, no actual.
        reply_body = bytes([TC_QUERY_SPEED]) + encode_speed(10.0, forward=True) + bytes([0x01])
        await _reply_from_loco(b, 0x111, reply_body)
        reply = await pending
        assert reply.status == 0x01
        assert reply.commanded_speed_mph is None
        assert reply.actual_speed_mph is None
    finally:
        client.close()


async def test_query_speed_decodes_nan_optional_fields_as_none() -> None:
    """NaN in the commanded/actual slots signals "not tracked" --
    surface as None rather than a NaN float."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(client.query_speed(0x222, timeout=1.0))
        await asyncio.sleep(0)
        nan_bytes = struct.pack(">e", float("nan"))
        reply_body = (
            bytes([TC_QUERY_SPEED])
            + encode_speed(45.0, forward=True)
            + bytes([0x00])
            + nan_bytes    # commanded NaN
            + nan_bytes    # actual NaN
        )
        await _reply_from_loco(b, 0x111, reply_body)
        reply = await pending
        assert reply.commanded_speed_mph is None
        assert reply.actual_speed_mph is None
    finally:
        client.close()


async def test_query_speed_times_out_when_loco_silent() -> None:
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.query_speed(0x222, timeout=0.05)
    finally:
        client.close()


async def test_query_function_returns_value_when_fn_matches() -> None:
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(
            client.query_function(0x222, function=3, timeout=1.0),
        )
        await asyncio.sleep(0)
        reply_body = (
            bytes([TC_QUERY_FUNCTION])
            + (3).to_bytes(3, "big")
            + (1).to_bytes(2, "big")
        )
        await _reply_from_loco(b, 0x111, reply_body)
        value = await pending
        assert value == 1
    finally:
        client.close()


async def test_query_function_rejects_mismatched_function_number() -> None:
    """If the reply's function number differs from what we asked
    for, that's a protocol violation on the loco side -- raise rather
    than return a wrong value."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(
            client.query_function(0x222, function=3, timeout=1.0),
        )
        await asyncio.sleep(0)
        # Loco replies with function=5 (wrong) + value=1.
        reply_body = (
            bytes([TC_QUERY_FUNCTION])
            + (5).to_bytes(3, "big")
            + (1).to_bytes(2, "big")
        )
        await _reply_from_loco(b, 0x111, reply_body)
        with pytest.raises(ValueError, match="reply mismatch"):
            await pending
    finally:
        client.close()


async def test_query_controller_returns_current_owner_nid() -> None:
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(
            client.query_controller(0x222, timeout=1.0),
        )
        await asyncio.sleep(0)
        # Reply: 0x20, 0x03, 0x00 (flags), 6-byte NID.
        reply_body = (
            bytes([TC_CONTROLLER_CONFIG, CC_QUERY, 0x00])
            + LOCO_NID.bytes
        )
        await _reply_from_loco(b, 0x111, reply_body)
        reply = await pending
        assert reply.controller == LOCO_NID
    finally:
        client.close()


async def test_query_controller_reports_zero_nid_for_unassigned_loco() -> None:
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(
            client.query_controller(0x222, timeout=1.0),
        )
        await asyncio.sleep(0)
        reply_body = (
            bytes([TC_CONTROLLER_CONFIG, CC_QUERY, 0x00])
            + bytes(6)   # all-zero NID
        )
        await _reply_from_loco(b, 0x111, reply_body)
        reply = await pending
        assert reply.controller == NodeId(0)
    finally:
        client.close()


async def test_concurrent_query_same_kind_same_alias_raises() -> None:
    """Two queries of the same kind against the same loco would race
    on the same futures-dict key; reject the second."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        first = asyncio.create_task(client.query_speed(0x222, timeout=1.0))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="already in flight"):
            await client.query_speed(0x222, timeout=0.1)
        first.cancel()
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await first
    finally:
        client.close()


async def test_client_ignores_non_traction_reply_frames() -> None:
    """PCERs and other traffic must not populate a pending future or
    trigger a crash."""
    from jmri_openlcb.codec import make_pcer_header  # noqa: PLC0415
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        pending = asyncio.create_task(client.query_speed(0x222, timeout=0.1))
        await asyncio.sleep(0)
        # Someone else's PCER -- traffic on the bus that the client
        # should not treat as a reply.
        await b.send_frame(OlcbFrame(
            header=make_pcer_header(0x222),
            data=b"\x00\x00\x00\x00\x00\x00\x00\x01",
        ))
        with pytest.raises(asyncio.TimeoutError):
            await pending
    finally:
        client.close()


# ---- connection wiring ---------------------------------------------------


async def test_connection_exposes_traction_client() -> None:
    from jmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    try:
        assert conn.traction is not None
        # A basic call should work -- set_speed is fire-and-forget so
        # nothing on the wire needs to answer.
        await conn.traction.set_speed(0x999, 12.5, forward=True)
    finally:
        await conn.close()
