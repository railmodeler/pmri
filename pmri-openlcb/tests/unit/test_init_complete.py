"""Node Initialization Complete PCER emission on connection open.

The event ID varies by protocol subset; we default to the "simple"
variant since our stock PIP flag set advertises the simple subset.
Callers can override via the ``init_complete_event`` setting.
"""
from __future__ import annotations

import asyncio

from pmri_openlcb import (
    EVENT_INIT_COMPLETE,
    EVENT_INIT_COMPLETE_SIMPLE,
    OlcbConnection,
    loopback_pair,
)
from pmri_openlcb.codec import MTI_PCER, OlcbFrame, decode_frame, extract_mti


async def _drain_bytes(port, deadline: float) -> list[OlcbFrame]:
    """Read raw frames off the peer side of a loopback pair until
    the coroutine hits *deadline* seconds. Returns whatever
    accumulated -- the tests don't need every frame, only the boot
    announcement."""
    frames: list[OlcbFrame] = []
    buf = bytearray()

    async def _run() -> None:
        while True:
            b = await port.read_byte()
            buf.append(b)
            if b == ord(";"):
                try:
                    frames.append(decode_frame(bytes(buf).decode("ascii")))
                except Exception:
                    pass
                buf.clear()

    try:
        await asyncio.wait_for(_run(), timeout=deadline)
    except asyncio.TimeoutError:
        pass
    return frames


async def test_init_complete_pcer_emits_on_open() -> None:
    """By default we broadcast the Simple Init Complete PCER after
    open -- peers listening on the bus (side B here) see it."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
    })
    try:
        frames = await _drain_bytes(b, deadline=0.05)
        pcers = [
            f for f in frames if extract_mti(f.header) == MTI_PCER
        ]
        # At least the boot announcement. Other subsystems can
        # send more PCERs, so we don't count-check strictly.
        assert any(
            f.data == EVENT_INIT_COMPLETE_SIMPLE.bytes for f in pcers
        ), f"no Simple Init Complete PCER in {pcers}"
    finally:
        await conn.close()


async def test_init_complete_full_variant_selectable() -> None:
    """``init_complete_event="full"`` emits the full-node variant
    instead of the simple one -- for callers advertising the full
    protocol PIP subset."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "init_complete_event": "full",
    })
    try:
        frames = await _drain_bytes(b, deadline=0.05)
        pcers = [f for f in frames if extract_mti(f.header) == MTI_PCER]
        assert any(f.data == EVENT_INIT_COMPLETE.bytes for f in pcers)
    finally:
        await conn.close()


async def test_skip_init_complete_setting_suppresses_boot_pcer() -> None:
    """``skip_init_complete=True`` keeps the wire silent on open --
    useful for stress tests and loopback captures that don't want
    to see the boot PCER in their timeline."""
    a, b = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_init_complete": True,
    })
    try:
        frames = await _drain_bytes(b, deadline=0.05)
        pcers = [f for f in frames if extract_mti(f.header) == MTI_PCER]
        assert not any(
            f.data == EVENT_INIT_COMPLETE_SIMPLE.bytes for f in pcers
        )
        assert not any(
            f.data == EVENT_INIT_COMPLETE.bytes for f in pcers
        )
    finally:
        await conn.close()
