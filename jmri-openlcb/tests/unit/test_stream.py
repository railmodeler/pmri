"""StreamClient + StreamResponder round-trip tests.

Two-node loopback bus. Side A runs a StreamClient, side B runs a
StreamResponder. Data pushed by A.send() shows up in B's on_stream
callback with the exact same bytes.
"""
from __future__ import annotations

import asyncio

import pytest

from jmri_openlcb import (
    OlcbTrafficController,
    StreamClient,
    StreamRejected,
    StreamResponder,
    loopback_pair,
)
from jmri_openlcb.addressed import encode_addressed_frames
from jmri_openlcb.codec import (
    OlcbFrame,
    extract_mti,
)
from jmri_openlcb.stream import (
    MTI_STREAM_CONTROL,
    MTI_STREAM_INIT_REPLY,
    MTI_STREAM_INIT_REQUEST,
    MTI_STREAM_PROCEED,
    STREAM_CONTROL_TYPE_COMPLETED,
    STREAM_REPLY_ACCEPTED,
    STREAM_REPLY_STREAMS_NOT_SUPPORTED,
)


async def _make_bus(
    alias_a: int = 0x111, alias_b: int = 0x222,
) -> tuple[OlcbTrafficController, OlcbTrafficController]:
    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=alias_a)
    tc_b = OlcbTrafficController(b, source_alias=alias_b)
    await tc_a.start()
    await tc_b.start()
    return tc_a, tc_b


async def _teardown(*tcs) -> None:  # noqa: ANN002
    for tc in tcs:
        await tc.aclose()


async def test_small_stream_roundtrips_verbatim() -> None:
    """A payload smaller than one buffer window: single frame group
    + Complete signal, no Proceed exchange needed."""
    tc_a, tc_b = await _make_bus()
    received: list[tuple[int, bytes]] = []
    responder = StreamResponder(
        tc_b, buffer_size=1024,
        on_stream=lambda alias, data: received.append((alias, data)),
    )
    client = StreamClient(tc_a)
    try:
        payload = b"hello, stream!"
        await client.send(tc_b.source_alias, payload)
        await asyncio.sleep(0.02)
        assert received == [(tc_a.source_alias, payload)]
    finally:
        client.close()
        responder.close()
        await _teardown(tc_a, tc_b)


async def test_multi_window_stream_uses_proceed() -> None:
    """A payload longer than one buffer window exercises the
    Proceed flow-control cycle."""
    tc_a, tc_b = await _make_bus()
    received: list[tuple[int, bytes]] = []
    # Small buffer so a 300-byte payload takes multiple windows.
    responder = StreamResponder(
        tc_b, buffer_size=64,
        on_stream=lambda alias, data: received.append((alias, data)),
    )
    client = StreamClient(tc_a)
    try:
        payload = bytes(range(256)) * 2   # 512 bytes
        await client.send(
            tc_b.source_alias, payload, buffer_size=64,
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        alias, data = received[0]
        assert alias == tc_a.source_alias
        assert data == payload
    finally:
        client.close()
        responder.close()
        await _teardown(tc_a, tc_b)


async def test_stream_content_type_round_trips() -> None:
    """The optional 6-byte Content UID prefix in the Initiate Request
    is accepted by the responder -- verifies our request-payload
    layout with the extended tail."""
    tc_a, tc_b = await _make_bus()
    received: list[tuple[int, bytes]] = []
    responder = StreamResponder(
        tc_b, buffer_size=256,
        on_stream=lambda alias, data: received.append((alias, data)),
    )
    client = StreamClient(tc_a)
    try:
        await client.send(
            tc_b.source_alias, b"typed payload",
            content_type=bytes.fromhex("010203040506"),
        )
        await asyncio.sleep(0.02)
        assert received == [(tc_a.source_alias, b"typed payload")]
    finally:
        client.close()
        responder.close()
        await _teardown(tc_a, tc_b)


async def test_content_type_must_be_six_bytes() -> None:
    tc_a, tc_b = await _make_bus()
    client = StreamClient(tc_a)
    try:
        with pytest.raises(ValueError, match="6 bytes"):
            await client.send(
                tc_b.source_alias, b"x",
                content_type=b"12345",   # 5 bytes
            )
    finally:
        client.close()
        await _teardown(tc_a, tc_b)


async def test_streams_not_supported_reject_raises() -> None:
    """A peer that replies with 'streams not supported' surfaces as
    StreamRejected on the client side."""
    tc_a, tc_b = await _make_bus()
    client = StreamClient(tc_a)
    try:
        # Hand-roll a rejecter that replies with 0x1040 to any
        # Initiate Request. Watches inbound frames on B directly.
        async def _rejecter(frame: OlcbFrame) -> None:
            if extract_mti(frame.header) != MTI_STREAM_INIT_REQUEST:
                return
            if len(frame.data) < 6:
                return
            # Extract dest alias from body prefix (addressed message).
            source_id = frame.data[3]   # skip 2-byte alias prefix + suggested dest
            reply_body = (
                bytes([0xFF, source_id])
                + (0).to_bytes(2, "big")   # buffer size = 0 -> reject
                + STREAM_REPLY_STREAMS_NOT_SUPPORTED.to_bytes(2, "big")
            )
            for f in encode_addressed_frames(
                MTI_STREAM_INIT_REPLY,
                tc_b.source_alias, tc_a.source_alias, reply_body,
            ):
                await tc_b.send_frame(f)

        tc_b.frames.connect(_rejecter)

        with pytest.raises(StreamRejected) as exc:
            await client.send(tc_b.source_alias, b"data", timeout=1.0)
        assert exc.value.reason == STREAM_REPLY_STREAMS_NOT_SUPPORTED
    finally:
        client.close()
        await _teardown(tc_a, tc_b)


async def test_concurrent_streams_to_same_peer_blocked() -> None:
    """The client bookkeeping assumes at most one outstanding stream
    per peer alias -- a second send() to a busy peer must raise
    rather than corrupt state."""
    tc_a, tc_b = await _make_bus()
    # No responder -> the first send() hangs waiting for Init Reply.
    client = StreamClient(tc_a)
    try:
        first = asyncio.create_task(
            client.send(tc_b.source_alias, b"x", timeout=5.0),
        )
        await asyncio.sleep(0.02)
        with pytest.raises(RuntimeError, match="already in flight"):
            await client.send(tc_b.source_alias, b"y", timeout=0.1)
        first.cancel()
        try:
            await first
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        client.close()
        await _teardown(tc_a, tc_b)


async def test_complete_control_carries_type_and_count() -> None:
    """The Stream Control frame we send when the stream ends
    encodes type=Completed (0x3) in the low nibble of byte 2 and
    the total byte count in bytes 3..5."""
    tc_a, tc_b = await _make_bus()
    controls: list[bytes] = []

    from jmri_openlcb.addressed import AddressedReassembler
    reasm = AddressedReassembler()

    def _watch(frame: OlcbFrame) -> None:
        msg = reasm.receive(frame)
        if msg is None:
            return
        if msg.mti == MTI_STREAM_CONTROL:
            controls.append(msg.body)

    tc_b.frames.connect(_watch)
    responder = StreamResponder(tc_b, buffer_size=64)
    client = StreamClient(tc_a)
    try:
        payload = b"X" * 300
        await client.send(tc_b.source_alias, payload, buffer_size=64)
        await asyncio.sleep(0.05)
        assert controls, "expected a Stream Control frame"
        body = controls[-1]
        # dest_id, source_id, control_byte, count[3]
        control_byte = body[2]
        assert (control_byte & 0x0F) == STREAM_CONTROL_TYPE_COMPLETED
        # Count is the total payload bytes sent (not counting
        # per-frame overhead).
        count = int.from_bytes(body[3:6], "big")
        assert count == len(payload)
    finally:
        client.close()
        responder.close()
        await _teardown(tc_a, tc_b)


async def test_stream_frame_type_is_seven() -> None:
    """The wire uses CAN frame type 7 (FT_STREAM=0xF in our
    4-bit encoding) for Stream Data Send frames -- not the datagram
    or MTI kinds. Verifies sec.3.1."""
    from jmri_openlcb.codec import FT_STREAM, extract_frame_type  # noqa: PLC0415

    tc_a, tc_b = await _make_bus()
    data_frames: list[OlcbFrame] = []

    def _watch(frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) == FT_STREAM:
            data_frames.append(frame)

    tc_b.frames.connect(_watch)
    responder = StreamResponder(tc_b, buffer_size=256)
    client = StreamClient(tc_a)
    try:
        await client.send(tc_b.source_alias, b"payload")
        await asyncio.sleep(0.02)
        assert data_frames
        # Every frame carries [dest_stream_id, ...payload...] --
        # first byte is the dest stream ID we assigned.
        assert data_frames[0].data[0] > 0
        assert bytes(
            b for f in data_frames for b in f.data[1:]
        ) == b"payload"
    finally:
        client.close()
        responder.close()
        await _teardown(tc_a, tc_b)
