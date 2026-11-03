"""Tests for the addressed-frame encoder + multi-frame reassembler."""
from __future__ import annotations

import pytest

from pmri_openlcb.addressed import (
    ADDRESSED_PAYLOAD_PER_FRAME,
    FLAG_FIRST,
    FLAG_LAST,
    FLAG_MIDDLE,
    FLAG_ONLY,
    AddressedMessage,
    AddressedReassembler,
    encode_addressed_frames,
    is_addressed_mti,
)
from pmri_openlcb.codec import OlcbFrame

MTI_SNIP_REQUEST = 0x0DE8
MTI_SNIP_REPLY = 0x0A08
MTI_PCER = 0x5B4


# ---- is_addressed_mti ----------------------------------------------------


def test_addressed_bit_recognised() -> None:
    assert is_addressed_mti(MTI_SNIP_REQUEST)   # bit 3 set
    assert is_addressed_mti(MTI_SNIP_REPLY)     # bit 3 set
    assert not is_addressed_mti(MTI_PCER)       # global -- bit 3 clear
    assert not is_addressed_mti(0x000)          # trivially not addressed


# ---- encode_addressed_frames --------------------------------------------


def test_encode_empty_body_produces_one_only_frame() -> None:
    """A SNIP request has no payload beyond the 2-byte dest prefix."""
    frames = encode_addressed_frames(MTI_SNIP_REQUEST, 0x123, 0x456, b"")
    assert len(frames) == 1
    f = frames[0]
    # 2-byte prefix, no payload.
    assert len(f.data) == 2
    assert f.data == b"\x04\x56"   # FLAG_ONLY (00) + dest 0x456


def test_encode_short_body_fits_in_one_frame() -> None:
    """Bodies up to ADDRESSED_PAYLOAD_PER_FRAME bytes -> single frame."""
    body = b"hello!"   # exactly 6 bytes
    frames = encode_addressed_frames(MTI_SNIP_REPLY, 0x123, 0x456, body)
    assert len(frames) == 1
    assert frames[0].data == b"\x04\x56" + body
    # 7-byte body -> still 2 frames (spec's off-by-one edge, verified
    # below in the multi-frame test).
    assert len(encode_addressed_frames(
        MTI_SNIP_REPLY, 0x123, 0x456, b"hello!!",
    )) == 2


def test_encode_multi_frame_flags_progress_correctly() -> None:
    """13 bytes = 3 chunks of {6, 6, 1}. Flags: FIRST, MIDDLE, LAST.
    Every frame carries the same dest alias in the low 12 bits."""
    body = bytes(range(13))
    frames = encode_addressed_frames(MTI_SNIP_REPLY, 0x100, 0xABC, body)
    assert len(frames) == 3
    # FIRST
    assert frames[0].data[0] >> 6 == FLAG_FIRST
    assert frames[0].data[:2] == bytes([
        (FLAG_FIRST << 6) | (0xABC >> 8), 0xBC,
    ])
    assert frames[0].data[2:] == body[0:6]
    # MIDDLE
    assert frames[1].data[0] >> 6 == FLAG_MIDDLE
    assert frames[1].data[2:] == body[6:12]
    # LAST -- only 1 payload byte
    assert frames[2].data[0] >> 6 == FLAG_LAST
    assert frames[2].data[2:] == body[12:13]


def test_encode_rejects_global_mti() -> None:
    """Global MTIs (bit 3 clear) don't have an addressed layout; the
    encoder refuses rather than silently emitting a malformed frame."""
    with pytest.raises(ValueError, match="not an addressed MTI"):
        encode_addressed_frames(MTI_PCER, 0x123, 0x456, b"")


def test_encode_rejects_bad_dest_alias() -> None:
    with pytest.raises(ValueError, match="12-bit"):
        encode_addressed_frames(MTI_SNIP_REQUEST, 0x123, 0x1000, b"")


# ---- AddressedReassembler ------------------------------------------------


def test_reassembler_returns_message_on_only_frame() -> None:
    """A single-frame message emerges immediately from ``receive``."""
    r = AddressedReassembler()
    body = b"hello!"
    frames = encode_addressed_frames(MTI_SNIP_REPLY, 0x100, 0x200, body)
    msg = r.receive(frames[0])
    assert isinstance(msg, AddressedMessage)
    assert msg.mti == MTI_SNIP_REPLY
    assert msg.source_alias == 0x100
    assert msg.dest_alias == 0x200
    assert msg.body == body


def test_reassembler_stitches_multi_frame_body_back_together() -> None:
    """A 30-byte body (5 chunks) round-trips through encode+reassemble."""
    r = AddressedReassembler()
    body = bytes(range(30))
    frames = encode_addressed_frames(MTI_SNIP_REPLY, 0x100, 0x200, body)
    assert len(frames) == 5

    # First 4 frames -> None (still assembling).
    for f in frames[:-1]:
        assert r.receive(f) is None
    # Last frame -> complete message.
    msg = r.receive(frames[-1])
    assert isinstance(msg, AddressedMessage)
    assert msg.body == body
    assert msg.source_alias == 0x100
    assert msg.dest_alias == 0x200


def test_reassembler_ignores_non_addressed_frames() -> None:
    """PCERs, control frames, CID frames -- none should trigger a
    partial-assembly bug in the reassembler."""
    from pmri_openlcb.codec import make_pcer_header, make_rid_header  # noqa: PLC0415
    r = AddressedReassembler()
    assert r.receive(OlcbFrame(
        header=make_pcer_header(0x100),
        data=bytes.fromhex("0102030405060708"),
    )) is None
    assert r.receive(OlcbFrame(
        header=make_rid_header(0x100), data=b"",
    )) is None


def test_reassembler_ignores_lone_middle_frame() -> None:
    """A MIDDLE flag with no prior FIRST is a lost-frame recovery
    scenario; the spec says drop it silently rather than emit garbage."""
    r = AddressedReassembler()
    # Hand-craft: encode a full body and hand it just the middle.
    body = bytes(range(30))
    frames = encode_addressed_frames(MTI_SNIP_REPLY, 0x100, 0x200, body)
    # Middle-only frame -> None.
    assert r.receive(frames[2]) is None


def test_reassembler_ignores_lone_last_frame() -> None:
    r = AddressedReassembler()
    body = bytes(range(30))
    frames = encode_addressed_frames(MTI_SNIP_REPLY, 0x100, 0x200, body)
    assert r.receive(frames[-1]) is None


def test_reassembler_interleaves_by_source_alias() -> None:
    """Two peers sending SNIP replies at the same time don't corrupt
    each other's buffers -- the reassembler keys by (mti, source)."""
    r = AddressedReassembler()
    body_a = bytes.fromhex("AA") * 20
    body_b = bytes.fromhex("BB") * 20
    frames_a = encode_addressed_frames(MTI_SNIP_REPLY, 0xA00, 0x100, body_a)
    frames_b = encode_addressed_frames(MTI_SNIP_REPLY, 0xB00, 0x100, body_b)
    # Interleave the two peers' frame streams.
    ordered = [frames_a[0], frames_b[0]]
    for i in range(1, min(len(frames_a), len(frames_b))):
        ordered += [frames_a[i], frames_b[i]]
    ordered += frames_a[len(frames_b):] + frames_b[len(frames_a):]
    got: list[AddressedMessage] = []
    for f in ordered:
        m = r.receive(f)
        if m is not None:
            got.append(m)
    by_src = {m.source_alias: m.body for m in got}
    assert by_src[0xA00] == body_a
    assert by_src[0xB00] == body_b
