"""Tests for PIP (Protocol Identification Protocol)."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.events import Signal

from jmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_mti,
)
from jmri_openlcb.pip import (
    DEFAULT_JMRI_FLAGS,
    MTI_PIP_INQUIRY,
    MTI_PIP_REPLY,
    PipClient,
    PipService,
    ProtocolFlag,
    decode_flags,
    encode_flags,
)


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


# ---- flag encode / decode -----------------------------------------------


def test_flag_bits_pin_the_wire_positions() -> None:
    """Regression pin against the S-9.7.4.2 sec.5 bit assignments --
    swap any bit and this test fails, catching the mistake before
    hardware ever sees it."""
    # Byte 0 -- top nibble.
    assert ProtocolFlag.SIMPLE == 0x800000
    assert ProtocolFlag.DATAGRAM == 0x400000
    assert ProtocolFlag.STREAM == 0x200000
    assert ProtocolFlag.MEMORY_CONFIG == 0x100000
    # Byte 0 -- bottom nibble.
    assert ProtocolFlag.RESERVATION == 0x080000
    assert ProtocolFlag.EVENT_EXCHANGE == 0x040000
    assert ProtocolFlag.IDENTIFICATION == 0x020000
    assert ProtocolFlag.LEARN_CONFIG == 0x010000
    # Byte 1 -- top nibble.
    assert ProtocolFlag.REMOTE_BUTTON == 0x008000
    assert ProtocolFlag.ABBREV_DEFAULT_CDI == 0x004000
    assert ProtocolFlag.DISPLAY == 0x002000
    assert ProtocolFlag.SNIP == 0x001000
    # Byte 1 -- bottom nibble.
    assert ProtocolFlag.CDI == 0x000800
    assert ProtocolFlag.TRACTION == 0x000400
    assert ProtocolFlag.FDI == 0x000200
    assert ProtocolFlag.COMMAND_STATION == 0x000100


def test_default_flags_advertise_the_expected_set() -> None:
    """Sanity pin the DEFAULT set so we don't accidentally start
    advertising something we don't actually implement (traction on
    the loco side would be embarrassing, as would advertising
    Command Station when we don't run one)."""
    assert ProtocolFlag.DATAGRAM in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.MEMORY_CONFIG in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.EVENT_EXCHANGE in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.IDENTIFICATION in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.SNIP in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.CDI in DEFAULT_JMRI_FLAGS
    # NOT advertised:
    assert ProtocolFlag.TRACTION not in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.COMMAND_STATION not in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.STREAM not in DEFAULT_JMRI_FLAGS
    assert ProtocolFlag.RESERVATION not in DEFAULT_JMRI_FLAGS


def test_encode_flags_produces_three_bytes_big_endian() -> None:
    """Wire form is 3 bytes so a peer that reads only 2 bytes still
    gets the top 16 bits (which cover byte 0 + byte 1 -- everything
    we actually populate)."""
    flags = ProtocolFlag.DATAGRAM | ProtocolFlag.SNIP
    encoded = encode_flags(flags)
    assert len(encoded) == 3
    assert encoded == b"\x40\x10\x00"


def test_decode_flags_round_trips() -> None:
    for flags in (
        ProtocolFlag.SIMPLE,
        DEFAULT_JMRI_FLAGS,
        ProtocolFlag.CDI | ProtocolFlag.COMMAND_STATION,
        ProtocolFlag(0),   # nothing supported
    ):
        assert decode_flags(encode_flags(flags)) == flags


def test_decode_flags_tolerates_short_reply() -> None:
    """Some older nodes send only 2 bytes (byte 0 + byte 1). We
    should zero-pad rather than raise."""
    # Only 2 bytes: byte0=0x40 (DATAGRAM), byte1=0x10 (SNIP).
    assert decode_flags(b"\x40\x10") == (
        ProtocolFlag.DATAGRAM | ProtocolFlag.SNIP
    )


def test_decode_flags_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        decode_flags(b"")


def test_decode_flags_ignores_bytes_past_three() -> None:
    """Newer nodes may append vendor extensions we don't recognise.
    Silently truncate rather than surface unknown flags."""
    # Byte 3 has a bit set that we don't know about; must be ignored.
    got = decode_flags(b"\x40\x10\x00\x55")
    assert got == (ProtocolFlag.DATAGRAM | ProtocolFlag.SNIP)


# ---- PipService -- auto-answer inquiries ---------------------------------


async def test_service_answers_inquiry_targeted_at_us() -> None:
    """Standard flow: peer sends addressed inquiry to our alias; we
    reply with an addressed PIP Reply carrying our flag mask."""
    a, b = _pair(0x111, 0x222)
    svc = PipService(a, DEFAULT_JMRI_FLAGS)
    try:
        # B sends a PIP Inquiry to us.
        for frame in encode_addressed_frames(
            MTI_PIP_INQUIRY, 0x222, 0x111, b"",
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert len(a.sent) == 1
        reply = a.sent[0]
        assert extract_mti(reply.header) == MTI_PIP_REPLY
        # Payload: 2-byte dest-alias prefix + 3-byte flag mask.
        # Dest alias in the reply is the peer's alias.
        assert reply.data[:2] == (0x222).to_bytes(2, "big")
        assert reply.data[2:5] == encode_flags(DEFAULT_JMRI_FLAGS)
    finally:
        svc.close()


async def test_service_ignores_inquiries_for_other_aliases() -> None:
    """PIP Inquiry is addressed; a probe targeting 0x999 must NOT
    provoke a reply from our node at 0x111. Otherwise every node on
    the bus would answer every inquiry -- catastrophic on a busy
    layout."""
    a, b = _pair(0x111, 0x222)
    svc = PipService(a, DEFAULT_JMRI_FLAGS)
    try:
        for frame in encode_addressed_frames(
            MTI_PIP_INQUIRY, 0x222, 0x999, b"",
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


async def test_set_flags_changes_subsequent_reply() -> None:
    """Adding a supported protocol mid-session (e.g. we just spun
    up a Stream service) should update what the next probe sees."""
    a, b = _pair(0x111, 0x222)
    svc = PipService(a, ProtocolFlag.DATAGRAM)
    try:
        svc.set_flags(ProtocolFlag.DATAGRAM | ProtocolFlag.STREAM)
        for frame in encode_addressed_frames(
            MTI_PIP_INQUIRY, 0x222, 0x111, b"",
        ):
            await b.send_frame(frame)
        await asyncio.sleep(0)
        reply = a.sent[0]
        flags = decode_flags(reply.data[2:5])
        assert flags == (ProtocolFlag.DATAGRAM | ProtocolFlag.STREAM)
    finally:
        svc.close()


async def test_service_ignores_non_pip_frames() -> None:
    from jmri_openlcb.codec import make_pcer_header  # noqa: PLC0415
    a, b = _pair(0x111, 0x222)
    svc = PipService(a)
    try:
        await b.send_frame(OlcbFrame(
            header=make_pcer_header(0x222),
            data=b"\x00" * 8,
        ))
        await asyncio.sleep(0)
        assert a.sent == []
    finally:
        svc.close()


# ---- PipClient -- probe path ---------------------------------------------


async def test_client_probes_service_and_gets_flag_mask() -> None:
    """End-to-end: client probes B's service; result is B's advertised flags."""
    a, b = _pair(0x111, 0x222)
    svc = PipService(b, ProtocolFlag.MEMORY_CONFIG | ProtocolFlag.CDI)
    client = PipClient(a)
    try:
        flags = await client.probe(0x222, timeout=1.0)
        assert flags == (ProtocolFlag.MEMORY_CONFIG | ProtocolFlag.CDI)
    finally:
        client.close()
        svc.close()


async def test_client_probe_times_out_when_target_silent() -> None:
    a, _ = _pair(0x111, 0x222)
    client = PipClient(a)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.probe(0x222, timeout=0.05)
    finally:
        client.close()


async def test_concurrent_probe_to_same_alias_raises() -> None:
    a, _ = _pair(0x111, 0x222)
    client = PipClient(a)
    try:
        pending = asyncio.create_task(client.probe(0x222, timeout=1.0))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="already in flight"):
            await client.probe(0x222, timeout=0.1)
        pending.cancel()
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await pending
    finally:
        client.close()


async def test_client_ignores_replies_from_other_peers() -> None:
    """A PIP Reply that arrives from an alias we didn't probe must
    NOT resolve a pending future -- the futures dict is keyed by
    the reply's source_alias for exactly this reason."""
    a, b = _pair(0x111, 0x222)
    client = PipClient(a)
    try:
        pending = asyncio.create_task(client.probe(0x222, timeout=0.1))
        await asyncio.sleep(0)
        # A spurious PIP Reply from a different alias.
        for frame in encode_addressed_frames(
            MTI_PIP_REPLY, 0x333, 0x111, encode_flags(DEFAULT_JMRI_FLAGS),
        ):
            await b.send_frame(frame)
        with pytest.raises(asyncio.TimeoutError):
            await pending
    finally:
        client.close()


async def test_client_surfaces_malformed_reply_as_valueerror() -> None:
    """A reply with a zero-byte payload can't decode. The future
    should reject rather than hang, so the caller knows the peer
    is broken."""
    a, b = _pair(0x111, 0x222)
    client = PipClient(a)
    try:
        pending = asyncio.create_task(client.probe(0x222, timeout=1.0))
        await asyncio.sleep(0)
        # Empty-payload reply -- just the 2-byte dest prefix.
        for frame in encode_addressed_frames(
            MTI_PIP_REPLY, 0x222, 0x111, b"",
        ):
            await b.send_frame(frame)
        with pytest.raises(ValueError, match="empty"):
            await pending
    finally:
        client.close()


# ---- connection wiring --------------------------------------------------


async def test_connection_exposes_pip_service_and_client() -> None:
    """OlcbConnection should stand up both sides on open."""
    from jmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    try:
        assert conn.pip_service is not None
        assert conn.pip_client is not None
        # Sanity: what we advertise matches the default set.
        assert conn.pip_service.flags == DEFAULT_JMRI_FLAGS
    finally:
        await conn.close()
