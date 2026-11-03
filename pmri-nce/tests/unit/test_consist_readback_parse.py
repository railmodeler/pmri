"""Parse tests for the NCE CS consist-table read-back path.

Locks in the three-region layout against JMRI Java's
``NceCmdStationMemory`` (region addresses) and ``NceConsist``
(``getLocoAddrText``, ``getLocoAddressType``) decoders.

Live-hardware refresh tests would need an actual NCE connection
attached; that path is exercised in the integration tests.
"""
from __future__ import annotations

import pytest

from pmri_nce.consist import (
    NCE_CONSIST_ADDR_MAX,
    NCE_CONSIST_ADDR_MIN,
    NCE_CONSIST_HEAD_REGION_BYTES,
    NCE_CONSIST_TAIL_REGION_BYTES,
    _decode_loco,
    _members_for_consist,
)


def _pack_addr(address: int, *, is_long: bool) -> bytes:
    """Encode a loco address in the NCE consist-table format:
    ``(hi & 0x3F) << 8 | lo`` for the 14-bit address, plus
    ``0xC0`` in the top two bits when it's a long address."""
    hi = (address >> 8) & 0x3F
    if is_long:
        hi |= 0xC0
    lo = address & 0xFF
    return bytes([hi, lo])


def _regions(
    per_consist: dict[int, tuple[
        tuple[int, bool] | None,
        tuple[int, bool] | None,
        list[tuple[int, bool]],
    ]],
    mid_entries: int = 4,
) -> tuple[bytes, bytes, bytes]:
    """Build fake (head, tail, mid) region buffers from a dict keyed
    by consist number. Each value is (lead, rear, [mids...]) -- any
    of the parts can be missing (None or empty). Consists not in the
    dict get zero-filled slots.

    Matches how the real CS lays memory out: three parallel arrays
    indexed by consist number."""
    head = bytearray(NCE_CONSIST_HEAD_REGION_BYTES)
    tail = bytearray(NCE_CONSIST_TAIL_REGION_BYTES)
    mid = bytearray(mid_entries * 2 * (NCE_CONSIST_ADDR_MAX + 1))
    for n, (lead, rear, mids) in per_consist.items():
        if lead is not None:
            head[n * 2 : n * 2 + 2] = _pack_addr(lead[0], is_long=lead[1])
        if rear is not None:
            tail[n * 2 : n * 2 + 2] = _pack_addr(rear[0], is_long=rear[1])
        for i, m in enumerate(mids):
            base = n * mid_entries * 2 + i * 2
            mid[base : base + 2] = _pack_addr(m[0], is_long=m[1])
    return bytes(head), bytes(tail), bytes(mid)


# ---- _decode_loco ---------------------------------------------------------


def test_decode_loco_zero_is_empty() -> None:
    assert _decode_loco(0, 0) == (0, False)


def test_decode_loco_short_address() -> None:
    hi, lo = _pack_addr(42, is_long=False)
    assert _decode_loco(hi, lo) == (42, False)


def test_decode_loco_long_address() -> None:
    """0xC0 in the top two bits of the high byte signals a long
    address."""
    hi, lo = _pack_addr(3601, is_long=True)
    assert _decode_loco(hi, lo) == (3601, True)


# ---- _members_for_consist -------------------------------------------------


def test_members_for_empty_consist() -> None:
    head, tail, mid = _regions({})
    assert _members_for_consist(15, head, tail, mid, mid_entries=4) == []


def test_members_lead_only() -> None:
    head, tail, mid = _regions({20: ((111, False), None, [])})
    assert _members_for_consist(20, head, tail, mid, mid_entries=4) == [
        (111, False),
    ]


def test_members_lead_and_rear() -> None:
    head, tail, mid = _regions({
        20: ((111, False), (222, False), []),
    })
    assert _members_for_consist(20, head, tail, mid, mid_entries=4) == [
        (111, False), (222, False),
    ]


def test_members_with_mids_and_mixed_addressing() -> None:
    """LEAD + REAR + 4 MIDs, some long -- full consist. The three
    parallel arrays each hold their slice."""
    head, tail, mid = _regions({
        30: (
            (111, False),
            (3601, True),
            [(3602, True), (5, False), (2048, True), (127, False)],
        ),
    })
    assert _members_for_consist(30, head, tail, mid, mid_entries=4) == [
        (111, False),
        (3601, True),
        (3602, True), (5, False), (2048, True), (127, False),
    ]


def test_members_skip_empty_mid_gaps() -> None:
    """Empty (zero) MID slots between populated ones are filtered
    out -- the CS doesn't compact but our reconstructed membership
    only cares about live members."""
    head, tail, mid = _regions({
        30: (
            (111, False),
            (222, False),
            [(333, False), (0, False), (444, False), (0, False)],
        ),
    })
    # (0, False) mids are packed as zero bytes and re-parsed to zero,
    # so they're dropped by the "addr == 0" filter.
    assert _members_for_consist(30, head, tail, mid, mid_entries=4) == [
        (111, False), (222, False), (333, False), (444, False),
    ]


def test_members_isolate_consists_by_number() -> None:
    """Two consists at different numbers do not bleed into each
    other's membership even though the three regions are one flat
    buffer each."""
    head, tail, mid = _regions({
        10: ((111, False), None, []),
        20: ((222, False), (333, False), [(444, False)]),
    })
    assert _members_for_consist(10, head, tail, mid, mid_entries=4) == [
        (111, False),
    ]
    assert _members_for_consist(20, head, tail, mid, mid_entries=4) == [
        (222, False), (333, False), (444, False),
    ]
    # A consist we didn't populate reads as empty.
    assert _members_for_consist(50, head, tail, mid, mid_entries=4) == []


def test_members_out_of_range_raises() -> None:
    """Any indexing past the region buffers is a caller bug -- raise
    rather than returning silently wrong data."""
    head, tail, mid = _regions({})
    # Consist number well past the table.
    with pytest.raises(ValueError, match="past the region"):
        _members_for_consist(
            NCE_CONSIST_ADDR_MAX + 5, head, tail, mid, mid_entries=4,
        )


def test_constants_match_pmri_java() -> None:
    """Regression pin against the JMRI Java magic numbers:
    addresses 1..127 (CV19's 7-bit range), 256-byte HEAD/TAIL
    regions (2 B x 128 consists)."""
    assert NCE_CONSIST_ADDR_MIN == 1
    assert NCE_CONSIST_ADDR_MAX == 127
    assert NCE_CONSIST_HEAD_REGION_BYTES == 256
    assert NCE_CONSIST_TAIL_REGION_BYTES == 256
