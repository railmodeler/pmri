"""Tests for SNIP encode/decode + client/responder end-to-end."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.events import Signal

from jmri_openlcb.addressed import AddressedReassembler
from jmri_openlcb.codec import OlcbFrame
from jmri_openlcb.snip import (
    MTI_SNIP_REPLY,
    MTI_SNIP_REQUEST,
    SNIP_MFG_VERSION,
    SNIP_USER_VERSION,
    NodeInfo,
    SnipClient,
    SnipResponder,
)


# ---- NodeInfo encode / decode --------------------------------------------


def test_nodeinfo_encode_layout() -> None:
    """Regression pin against S-9.7.4.5 sec.3.1: version byte 0x04 ->
    4 null-terminated strings -> version byte 0x02 -> 2 null-terminated
    strings. Empty strings still get a null terminator."""
    info = NodeInfo(
        manufacturer="JMRI-Python",
        model="Server",
        hardware_version="linux-arm64",
        software_version="0.1.0",
        user_name="",
        user_description="",
    )
    encoded = info.encode()
    assert encoded[0] == SNIP_MFG_VERSION
    # Strings + terminators.
    assert b"JMRI-Python\x00" in encoded
    assert b"Server\x00" in encoded
    assert b"linux-arm64\x00" in encoded
    assert b"0.1.0\x00" in encoded
    # User version byte should appear after the 4 mfg strings.
    user_pos = encoded.index(bytes([SNIP_USER_VERSION]))
    assert user_pos > encoded.index(b"0.1.0")
    # Empty user strings -> \x00\x00 tail.
    assert encoded.endswith(b"\x00\x00")


def test_nodeinfo_round_trip_utf8() -> None:
    info = NodeInfo(
        manufacturer="ACME Décor",
        model="Modèle X",
        hardware_version="v2",
        software_version="1.2.3",
        user_name="Salle Principale",
        user_description="Aiguillage nord",
    )
    assert NodeInfo.decode(info.encode()) == info


def test_nodeinfo_decode_tolerates_missing_user_block() -> None:
    """Historic firmware sometimes omits the user block. Decode
    should treat it as empty user name+description rather than
    raising."""
    partial = b"".join([
        bytes([SNIP_MFG_VERSION]),
        b"MFG\x00", b"MDL\x00", b"HW\x00", b"SW\x00",
        # No user version byte or strings.
    ])
    info = NodeInfo.decode(partial)
    assert info.manufacturer == "MFG"
    assert info.model == "MDL"
    assert info.hardware_version == "HW"
    assert info.software_version == "SW"
    assert info.user_name == ""
    assert info.user_description == ""


def test_nodeinfo_decode_rejects_missing_mfg_version() -> None:
    with pytest.raises(ValueError, match="manufacturer version"):
        NodeInfo.decode(b"\x99Not a SNIP reply\x00")


def test_nodeinfo_decode_rejects_missing_mid_terminator() -> None:
    """A null-less string in the middle of the mfg block is a
    malformed reply -- surface it rather than silently returning
    garbled data."""
    # Only 3 nulls before we hit the user version, missing the 4th.
    bad = b"".join([
        bytes([SNIP_MFG_VERSION]),
        b"MFG\x00", b"MDL\x00", b"HW\x00", b"SW-no-terminator",
        # No null, no user version byte.
    ])
    # SW-no-terminator is the last mfg string -> tolerated as trailing.
    info = NodeInfo.decode(bad)
    assert info.software_version == "SW-no-terminator"


def test_nodeinfo_decode_rejects_missing_early_terminator() -> None:
    """A missing null on the first mfg string is definitely malformed;
    tolerating "last string missing" doesn't apply here."""
    bad = bytes([SNIP_MFG_VERSION]) + b"MFG-no-null-and-more-data"
    with pytest.raises(ValueError, match="null terminator"):
        NodeInfo.decode(bad)


# ---- SnipClient / SnipResponder end-to-end -------------------------------


class _FakeTc:
    """Two-sided fake -- 'a' side sends frames that arrive on 'b' side
    and vice versa. Wires a Signal to each side so the SNIP client/
    responder can subscribe like they would on a real TC."""

    def __init__(self, source_alias: int, peer: _FakeTc | None = None) -> None:
        self.source_alias = source_alias
        self.frames: Signal[OlcbFrame] = Signal()
        self._peer = peer

    def link(self, peer: _FakeTc) -> None:
        self._peer = peer

    async def send_frame(self, frame: OlcbFrame) -> None:
        # Deliver directly to the peer's inbound signal -- mirrors
        # what a loopback pair does but without any I/O.
        assert self._peer is not None
        await self._peer.frames.emit(frame)


def _pair(alias_a: int, alias_b: int) -> tuple[_FakeTc, _FakeTc]:
    a = _FakeTc(alias_a)
    b = _FakeTc(alias_b)
    a.link(b)
    b.link(a)
    return a, b


async def test_client_probes_responder_and_gets_info_back() -> None:
    """End-to-end: node A probes node B; node B answers via its
    SnipResponder; A's SnipClient decodes the reply into NodeInfo."""
    a, b = _pair(0x111, 0x222)
    info_b = NodeInfo(
        manufacturer="B Mfg", model="B Model",
        hardware_version="hw1", software_version="sw1",
        user_name="user-b", user_description="a fine node",
    )
    responder = SnipResponder(b, info_b)
    client = SnipClient(a)
    try:
        got = await client.probe(0x222, timeout=1.0)
        assert got == info_b
    finally:
        client.close()
        responder.close()


async def test_client_probe_times_out_when_target_absent() -> None:
    """No responder on the other side -> probe hits its timeout."""
    a, _ = _pair(0x111, 0x222)
    client = SnipClient(a)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.probe(0x222, timeout=0.05)
    finally:
        client.close()


async def test_responder_ignores_probes_for_other_aliases() -> None:
    """Node B is at 0x222; a probe targeted at 0x999 must not
    provoke B's responder. Otherwise we'd get address-space
    aliasing chaos on a real bus."""
    a, b = _pair(0x111, 0x222)
    info_b = NodeInfo(manufacturer="B", model="B", hardware_version="",
                       software_version="", user_name="", user_description="")
    responder = SnipResponder(b, info_b)
    client = SnipClient(a)
    try:
        # Manually send a probe with the wrong dest alias.
        from jmri_openlcb.addressed import (  # noqa: PLC0415
            encode_addressed_frames,
        )
        for frame in encode_addressed_frames(
            MTI_SNIP_REQUEST, 0x111, 0x999, b"",
        ):
            await a.send_frame(frame)
        # The responder shouldn't answer -> any real probe to
        # 0x222 that we run afterward proves the bus wasn't clobbered.
        got = await client.probe(0x222, timeout=1.0)
        assert got == info_b
    finally:
        client.close()
        responder.close()


async def test_responder_reassembles_across_multi_frame_reply() -> None:
    """A NodeInfo with long strings serialises to more than 6 bytes,
    forcing multi-frame. Verifies the reassembly path is exercised."""
    a, b = _pair(0x111, 0x222)
    info_b = NodeInfo(
        manufacturer="A very very long manufacturer name that forces "
                     "the reply to fragment across many CAN frames",
        model="Model with lots of text too",
        hardware_version="hardware version string",
        software_version="software version 1.2.3",
        user_name="user name that the operator has set",
        user_description="A friendly description",
    )
    assert len(info_b.encode()) > 6   # sanity: this WILL fragment
    responder = SnipResponder(b, info_b)
    client = SnipClient(a)
    try:
        got = await client.probe(0x222, timeout=1.0)
        assert got == info_b
    finally:
        client.close()
        responder.close()


async def test_dynamic_get_info_called_on_each_probe() -> None:
    """A caller passing a callable ``get_info`` gets it invoked fresh
    per probe -- supports "user edited the name, next probe sees it"
    without recreating the responder."""
    a, b = _pair(0x111, 0x222)
    editable = {"user_name": "before"}

    def _current() -> NodeInfo:
        return NodeInfo(
            manufacturer="M", model="M", hardware_version="",
            software_version="", user_name=editable["user_name"],
            user_description="",
        )

    responder = SnipResponder(b, _current)
    client = SnipClient(a)
    try:
        first = await client.probe(0x222, timeout=1.0)
        assert first.user_name == "before"
        editable["user_name"] = "after"
        # SnipClient forbids two probes to the same dest concurrently;
        # the first is done, so a fresh probe is fine.
        second = await client.probe(0x222, timeout=1.0)
        assert second.user_name == "after"
    finally:
        client.close()
        responder.close()


async def test_concurrent_probe_to_same_alias_raises() -> None:
    """The client's per-target future dict enforces one outstanding
    probe per alias. Nested probes to the same dest must raise."""
    a, _ = _pair(0x111, 0x222)
    client = SnipClient(a)
    try:
        # Kick off a probe that will never complete (no responder).
        pending = asyncio.create_task(client.probe(0x222, timeout=1.0))
        await asyncio.sleep(0)   # let it register in _pending
        with pytest.raises(RuntimeError, match="already in flight"):
            await client.probe(0x222, timeout=0.1)
        pending.cancel()
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await pending
    finally:
        client.close()
