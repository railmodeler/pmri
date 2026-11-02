"""Unit tests for :mod:`jmri_openlcb.datagrams`.

Two-node bus modelled via :func:`loopback_pair` -- both endpoints run
their own :class:`OlcbTrafficController` + :class:`DatagramEngine`,
and datagrams pass between them just as they would across a real
CAN bridge. Anything that fails here would fail identically on the
wire.
"""
from __future__ import annotations

import asyncio

import pytest

from jmri_openlcb import (
    Datagram,
    DatagramEngine,
    DatagramRejected,
    OlcbTrafficController,
    loopback_pair,
)
from jmri_openlcb.codec import OlcbFrame
from jmri_openlcb.datagrams import (
    MTI_DATAGRAM_REJECTED,
    _encode_dest_prefix,
    make_reply_header,
)


async def _two_nodes(
    alias_a: int = 0x111, alias_b: int = 0x222,
) -> tuple[OlcbTrafficController, OlcbTrafficController,
           DatagramEngine, DatagramEngine]:
    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=alias_a)
    tc_b = OlcbTrafficController(b, source_alias=alias_b)
    await tc_a.start()
    await tc_b.start()
    eng_a = DatagramEngine(tc_a)
    eng_b = DatagramEngine(tc_b)
    await eng_a.attach()
    await eng_b.attach()
    return tc_a, tc_b, eng_a, eng_b


async def _teardown(*things) -> None:  # noqa: ANN002
    for engine in things:
        if isinstance(engine, DatagramEngine):
            await engine.detach()
    for tc in things:
        if isinstance(tc, OlcbTrafficController):
            await tc.aclose()


# ---- single-frame datagrams --------------------------------------------


async def test_single_frame_datagram_roundtrips() -> None:
    """A <=8-byte datagram fits in one Complete frame. The receiver
    fires ``inbound`` with the exact body and the sender's send()
    awaits the peer's Received OK reply."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    received: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: received.append(dg))
    try:
        await eng_a.send(tc_b.source_alias, b"hello!")
        # Reply already awaited by send(); allow eng_b to fire its
        # inbound signal.
        await asyncio.sleep(0.01)
        assert len(received) == 1
        assert received[0].data == b"hello!"
        assert received[0].source_alias == tc_a.source_alias
        assert received[0].dest_alias == tc_b.source_alias
    finally:
        await _teardown(eng_a, eng_b, tc_a, tc_b)


async def test_zero_length_datagram_permitted() -> None:
    """A body of length 0 travels as a Complete frame with empty
    data. Some memory-config sub-protocols use this for
    "acknowledgement without content"."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    received: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: received.append(dg))
    try:
        await eng_a.send(tc_b.source_alias, b"")
        await asyncio.sleep(0.01)
        assert len(received) == 1
        assert received[0].data == b""
    finally:
        await _teardown(eng_a, eng_b, tc_a, tc_b)


# ---- multi-frame datagrams ---------------------------------------------


async def test_multi_frame_datagram_reassembles_in_order() -> None:
    """9..72 byte payload triggers First / Middle... / Final
    fragmentation. The receiver reassembles into the original bytes
    exactly."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    received: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: received.append(dg))
    try:
        payload = bytes(range(60))   # spans 8 frames (First + 6 Middle + Final)
        await eng_a.send(tc_b.source_alias, payload)
        await asyncio.sleep(0.02)
        assert len(received) == 1
        assert received[0].data == payload
    finally:
        await _teardown(eng_a, eng_b, tc_a, tc_b)


async def test_max_size_datagram() -> None:
    """A 72-byte payload should round-trip -- that's the spec cap."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    received: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: received.append(dg))
    try:
        payload = bytes(range(72))
        await eng_a.send(tc_b.source_alias, payload)
        await asyncio.sleep(0.02)
        assert received[0].data == payload
    finally:
        await _teardown(eng_a, eng_b, tc_a, tc_b)


async def test_oversize_datagram_refused() -> None:
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    try:
        with pytest.raises(ValueError, match="72"):
            await eng_a.send(tc_b.source_alias, bytes(73))
    finally:
        await _teardown(eng_a, eng_b, tc_a, tc_b)


# ---- rejection path ----------------------------------------------------


async def test_rejected_reply_raises() -> None:
    """When the peer replies with Datagram Rejected instead of
    Received OK, ``send`` raises :class:`DatagramRejected` with the
    2-byte error code. Simulate by intercepting eng_b's default
    Received-OK and hand-sending a Rejected reply."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    await eng_b.detach()     # stop eng_b from auto-Received-OK'ing
    try:
        seen = asyncio.Event()

        async def rejector(frame: OlcbFrame) -> None:
            # Any datagram frame from tc_a triggers a rejection.
            if not seen.is_set() and (frame.header >> 24) & 0xF in (0x2, 0x3, 0x4, 0x5):
                seen.set()
                # Rejected reply carries a 2-byte error code (0x1000
                # = "permanent error, unknown reason").
                await tc_b.send_frame(OlcbFrame(
                    header=make_reply_header(
                        MTI_DATAGRAM_REJECTED,
                        tc_b.source_alias,
                    ),
                    data=_encode_dest_prefix(tc_a.source_alias)
                    + bytes([0x10, 0x00]),
                ))

        tc_b.frames.connect(rejector)

        with pytest.raises(DatagramRejected) as exc:
            await eng_a.send(tc_b.source_alias, b"payload", timeout=1.0)
        assert exc.value.error_code == 0x1000
    finally:
        await _teardown(eng_a, tc_a, tc_b)


async def test_temporary_reject_retries_and_eventually_succeeds() -> None:
    """A rejected code with top nibble 0x2 is temporary -- the engine
    resends after backoff. Simulate a peer that rejects the first
    attempt then falls back to normal Received-OK on the second."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    await eng_b.detach()   # take over eng_b's reply logic manually
    try:
        rejections = 0

        async def flaky(frame: OlcbFrame) -> None:
            nonlocal rejections
            kind = (frame.header >> 24) & 0xF
            if kind not in (0x2, 0x3, 0x4, 0x5):
                return
            if rejections == 0:
                rejections = 1
                # 0x2040 = temporary error, buffer unavailable.
                await tc_b.send_frame(OlcbFrame(
                    header=make_reply_header(
                        MTI_DATAGRAM_REJECTED, tc_b.source_alias,
                    ),
                    data=_encode_dest_prefix(tc_a.source_alias)
                    + bytes([0x20, 0x40]),
                ))
            else:
                # Second attempt succeeds -- send Received OK.
                from jmri_openlcb.datagrams import MTI_DATAGRAM_RECEIVED_OK  # noqa: PLC0415
                await tc_b.send_frame(OlcbFrame(
                    header=make_reply_header(
                        MTI_DATAGRAM_RECEIVED_OK, tc_b.source_alias,
                    ),
                    data=_encode_dest_prefix(tc_a.source_alias),
                ))

        tc_b.frames.connect(flaky)

        # Should NOT raise -- the engine retries.
        await eng_a.send(
            tc_b.source_alias, b"payload",
            timeout=1.0, retry_backoff=0.01,
        )
        assert rejections == 1
    finally:
        await _teardown(eng_a, tc_a, tc_b)


async def test_permanent_reject_never_retries() -> None:
    """A 0x1xxx code is permanent -- the engine raises on the first
    rejection without any retry attempts."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    await eng_b.detach()
    try:
        attempts = 0

        async def always_perm(frame: OlcbFrame) -> None:
            nonlocal attempts
            kind = (frame.header >> 24) & 0xF
            if kind not in (0x2, 0x3, 0x4, 0x5):
                return
            attempts += 1
            await tc_b.send_frame(OlcbFrame(
                header=make_reply_header(
                    MTI_DATAGRAM_REJECTED, tc_b.source_alias,
                ),
                data=_encode_dest_prefix(tc_a.source_alias)
                + bytes([0x10, 0x00]),   # permanent
            ))

        tc_b.frames.connect(always_perm)

        with pytest.raises(DatagramRejected) as exc:
            await eng_a.send(
                tc_b.source_alias, b"x",
                timeout=1.0, retry_backoff=0.01,
            )
        assert exc.value.error_code == 0x1000
        assert attempts == 1   # no retries
    finally:
        await _teardown(eng_a, tc_a, tc_b)


async def test_retry_gives_up_after_max_retries() -> None:
    """If every attempt gets a temporary rejection, the engine
    surfaces the last DatagramRejected after exhausting retries."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    await eng_b.detach()
    try:
        attempts = 0

        async def always_temp(frame: OlcbFrame) -> None:
            nonlocal attempts
            kind = (frame.header >> 24) & 0xF
            if kind not in (0x2, 0x3, 0x4, 0x5):
                return
            attempts += 1
            await tc_b.send_frame(OlcbFrame(
                header=make_reply_header(
                    MTI_DATAGRAM_REJECTED, tc_b.source_alias,
                ),
                data=_encode_dest_prefix(tc_a.source_alias)
                + bytes([0x20, 0x40]),
            ))

        tc_b.frames.connect(always_temp)

        with pytest.raises(DatagramRejected):
            await eng_a.send(
                tc_b.source_alias, b"x",
                timeout=1.0, max_retries=2, retry_backoff=0.01,
            )
        # 1 initial + 2 retries = 3 attempts.
        assert attempts == 3
    finally:
        await _teardown(eng_a, tc_a, tc_b)


def test_is_permanent_rejection_matches_1xxx_family() -> None:
    from jmri_openlcb import is_permanent_rejection, is_temporary_rejection  # noqa: PLC0415
    assert is_permanent_rejection(0x1000) is True
    assert is_permanent_rejection(0x10FF) is True
    assert is_permanent_rejection(0x2000) is False
    assert is_temporary_rejection(0x2000) is True
    assert is_temporary_rejection(0x2FFF) is True
    assert is_temporary_rejection(0x1000) is False


def test_describe_rejection_known_codes() -> None:
    """A handful of known S-9.7.4.1 codes get their canonical
    description. This is where a wrong code value shows up
    obviously -- the spot to fix if verification later flags any."""
    from jmri_openlcb import describe_rejection  # noqa: PLC0415
    assert describe_rejection(0x1082) == "Address space unknown"
    assert describe_rejection(0x1083) == "Address out of bounds"
    assert describe_rejection(0x1084) == "Write to read-only space"
    assert describe_rejection(0x2040).startswith("Buffer unavailable")


def test_describe_rejection_unknown_permanent_falls_back_to_family() -> None:
    from jmri_openlcb import describe_rejection  # noqa: PLC0415
    got = describe_rejection(0x1234)
    assert "Permanent" in got
    assert "0x1234" in got
    assert "unspecified" in got


def test_describe_rejection_unknown_temporary_falls_back_to_family() -> None:
    from jmri_openlcb import describe_rejection  # noqa: PLC0415
    got = describe_rejection(0x2555)
    assert "Temporary" in got
    assert "0x2555" in got


def test_datagram_rejected_str_includes_description() -> None:
    """Raising DatagramRejected -> str(exc) surfaces both the human
    description and the raw hex code."""
    exc = DatagramRejected(error_code=0x1082)
    s = str(exc)
    assert "Address space unknown" in s
    assert "0x1082" in s


def test_datagram_rejected_description_property() -> None:
    exc = DatagramRejected(error_code=0x2040)
    assert exc.description.startswith("Buffer unavailable")


async def test_no_reply_times_out() -> None:
    """No Received OK on the wire -> the send raises TimeoutError.
    Modelled by tearing down eng_b so nobody replies."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    await eng_b.detach()
    try:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await eng_a.send(tc_b.source_alias, b"x", timeout=0.2)
    finally:
        await _teardown(eng_a, tc_a, tc_b)


# ---- alias filtering ---------------------------------------------------


async def test_datagram_addressed_to_other_alias_ignored() -> None:
    """A datagram whose dest alias isn't ours must not be delivered
    or acknowledged. Otherwise every node on a shared bus would ack
    every datagram -- chatter and duplicate replies."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    received: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: received.append(dg))
    try:
        # Send from tc_a to a THIRD alias (not tc_b's) -- tc_b sees the
        # frame but should ignore it.
        await eng_a.send(0x999, b"noise", wait_for_reply=False)
        await asyncio.sleep(0.02)
        assert received == []
    finally:
        await _teardown(eng_a, eng_b, tc_a, tc_b)


# ---- concurrent-send guard ---------------------------------------------


async def test_second_send_to_same_peer_while_pending_raises() -> None:
    """One outstanding datagram per (src -> dst) -- the standard is
    fuzzy on this but the engine bookkeeping is fragile with more
    than one, and no real peer needs it."""
    tc_a, tc_b, eng_a, eng_b = await _two_nodes()
    await eng_b.detach()     # so the first send never gets its ack
    try:
        # First send is in flight (waiting for a reply that won't come).
        task = asyncio.create_task(
            eng_a.send(tc_b.source_alias, b"first", timeout=5.0),
        )
        await asyncio.sleep(0.02)
        with pytest.raises(RuntimeError, match="already in flight"):
            await eng_a.send(tc_b.source_alias, b"second")
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, TimeoutError):
            pass
    finally:
        await _teardown(eng_a, tc_a, tc_b)
