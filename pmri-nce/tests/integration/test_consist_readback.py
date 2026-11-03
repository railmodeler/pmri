"""End-to-end refresh_from_cs test against a fake NCE command station.

The fake CS answers ``READ16(addr)`` at every consist-table offset
across the three regions (HEAD / TAIL / MID) with the pre-planted
bytes for each consist. ``refresh_from_cs`` walks the range, parses
the assembled per-consist member list, and reconciles the in-memory
state -- this test verifies the full pipeline.
"""
from __future__ import annotations

import asyncio

from pmri_nce.connection import NceConnection
from pmri_nce.consist import (
    NCE_CONSIST_ADDR_MAX,
    NCE_CONSIST_HEAD_REGION_BYTES,
    NCE_CONSIST_TAIL_REGION_BYTES,
)
from pmri_nce.opcodes import OP_READ_16_MEM
from pmri_nce.transport import loopback_pair

HEAD_ADDR = 0xF500
TAIL_ADDR = 0xF600
MID_ADDR = 0xF700
MID_ENTRIES = 4
MID_REGION_BYTES = MID_ENTRIES * 2 * (NCE_CONSIST_ADDR_MAX + 1)


def _pack(addr: int, *, is_long: bool = False) -> bytes:
    hi = (addr >> 8) & 0x3F
    if is_long:
        hi |= 0xC0
    return bytes([hi, addr & 0xFF])


def _build_regions(
    entries: dict[int, tuple[
        tuple[int, bool] | None,
        tuple[int, bool] | None,
        list[tuple[int, bool]],
    ]],
) -> tuple[bytearray, bytearray, bytearray]:
    """Build (head, tail, mid) fake regions from ``entries`` keyed
    by consist number. Each value is (lead, rear, mids)."""
    head = bytearray(NCE_CONSIST_HEAD_REGION_BYTES)
    tail = bytearray(NCE_CONSIST_TAIL_REGION_BYTES)
    mid = bytearray(MID_REGION_BYTES)
    for n, (lead, rear, mids) in entries.items():
        if lead is not None:
            head[n * 2 : n * 2 + 2] = _pack(lead[0], is_long=lead[1])
        if rear is not None:
            tail[n * 2 : n * 2 + 2] = _pack(rear[0], is_long=rear[1])
        for i, m in enumerate(mids):
            base = n * MID_ENTRIES * 2 + i * 2
            mid[base : base + 2] = _pack(m[0], is_long=m[1])
    return head, tail, mid


async def _open_conn_with_fake_cs(entries: dict[int, tuple[
    tuple[int, bool] | None,
    tuple[int, bool] | None,
    list[tuple[int, bool]],
]]):
    """Open an NceConnection wired to a loopback pair; the far end
    plays fake CS that answers READ16 with pre-planted bytes across
    the three consist regions.

    Any address outside a region returns all zeros.
    """
    head, tail, mid = _build_regions(entries)
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })

    def _lookup(address: int) -> bytes:
        # Which region does this 16-byte read hit? Regions are
        # contiguous 0xF500..0xFB00 on stock NCE firmware.
        for base, buf in (
            (HEAD_ADDR, head), (TAIL_ADDR, tail), (MID_ADDR, mid),
        ):
            offset = address - base
            if 0 <= offset < len(buf):
                return bytes(buf[offset : offset + 16])
        return bytes(16)

    async def fake_cs() -> None:
        try:
            while True:
                req = await b.read_exact(3)
                if req[0] != OP_READ_16_MEM:
                    continue
                addr = (req[1] << 8) | req[2]
                await b.write(_lookup(addr))
        except (asyncio.CancelledError, Exception):
            return

    task = asyncio.create_task(fake_cs())
    return conn, task


async def test_refresh_recovers_single_consist() -> None:
    """A CS with one populated consist at slot 20 (lead+rear) is
    reconstructed into a Consist bean with two members."""
    entries = {
        20: ((111, False), (222, False), []),
    }
    conn, task = await _open_conn_with_fake_cs(entries)
    try:
        changed = await conn.consists.refresh_from_cs(
            min_consist=1, max_consist=25,
        )
        assert 20 in changed
        consist = conn.consists.get(20)
        assert consist is not None
        assert consist.kind == "command_station"
        addrs = [m.address for m in consist.locos]
        assert 111 in addrs
        assert 222 in addrs
    finally:
        task.cancel()
        await conn.close()


async def test_refresh_preserves_long_address_flag() -> None:
    entries = {
        30: ((3601, True), (222, False), []),
    }
    conn, task = await _open_conn_with_fake_cs(entries)
    try:
        await conn.consists.refresh_from_cs(min_consist=1, max_consist=35)
        consist = conn.consists.get(30)
        assert consist is not None
        by_addr = {m.address: m for m in consist.locos}
        assert by_addr[3601].long_address is True
        assert by_addr[222].long_address is False
    finally:
        task.cancel()
        await conn.close()


async def test_refresh_recovers_multiple_consists() -> None:
    entries = {
        10: ((111, False), (222, False), []),
        20: ((3601, True), (3602, True), [(3603, True)]),
        50: ((5, False), (6, False), [(7, False), (8, False)]),
    }
    conn, task = await _open_conn_with_fake_cs(entries)
    try:
        changed = await conn.consists.refresh_from_cs(
            min_consist=1, max_consist=60,
        )
        assert set(changed) == {10, 20, 50}
        assert conn.consists.get(10) is not None
        assert len(conn.consists.get(20).locos) == 3
        assert len(conn.consists.get(50).locos) == 4
    finally:
        task.cancel()
        await conn.close()


async def test_refresh_skips_empty_consists() -> None:
    """A CS with no consists at all -> refresh returns no changes and
    creates no beans. Guards against a shape-change accidentally
    materializing 127 empty Consist objects."""
    conn, task = await _open_conn_with_fake_cs({})
    try:
        changed = await conn.consists.refresh_from_cs(
            min_consist=1, max_consist=10,
        )
        assert changed == []
        assert list(conn.consists) == []
    finally:
        task.cancel()
        await conn.close()


async def test_refresh_ignores_data_past_the_consist_table() -> None:
    """Regression pin for the beta 4 bug the user reported: consist
    127's memory used to be looked up at ``base + 127*16 = 0xFCF0``,
    well past the 0xF500..0xFB00 consist table, which produced a
    phantom loco address on read-back. The correct layout indexes
    HEAD[127*2] / TAIL[127*2] / MID[127*8], all safely inside the
    consist regions."""
    conn, task = await _open_conn_with_fake_cs({})
    try:
        # Consist 127 (the previous bug's trigger address): CS shows
        # nothing, refresh must not manifest a bogus member for it.
        changed = await conn.consists.refresh_from_cs(
            min_consist=127, max_consist=127,
        )
        assert changed == []
        assert conn.consists.get(127) is None
    finally:
        task.cancel()
        await conn.close()


async def test_refresh_reconciles_missing_and_extra_members() -> None:
    """A consist we already track has 111, 222, 333. The CS reports
    222 and 444. Reconciliation drops 111 + 333 and adds 444 so
    our state matches what the hardware actually thinks."""
    entries = {
        15: ((222, False), (444, False), []),
    }
    conn, task = await _open_conn_with_fake_cs(entries)
    try:
        # Pre-seed by hand-mutating consist.locos to simulate stale
        # in-memory state without firing the NCE wire ops (which
        # our fake CS won't answer).
        from pmri_core.beans.consist import Consist, ConsistLoco  # noqa: PLC0415
        stale = Consist(address=15, kind="command_station", locos=[
            ConsistLoco(address=111, position=0),
            ConsistLoco(address=222, position=1),
            ConsistLoco(address=333, position=2),
        ])
        conn.consists._consists[15] = stale

        await conn.consists.refresh_from_cs(min_consist=1, max_consist=20)
        consist = conn.consists.get(15)
        addrs = {m.address for m in consist.locos}
        assert addrs == {222, 444}
    finally:
        task.cancel()
        await conn.close()
