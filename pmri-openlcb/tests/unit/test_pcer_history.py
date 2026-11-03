"""PcerHistory -- per-peer PCER event count + last-seen tracker."""
from __future__ import annotations

import asyncio

from pmri_core.events import Signal

from pmri_openlcb import EventId, PcerHistory
from pmri_openlcb.codec import (
    OlcbFrame,
    make_pcer_header,
)


class _FakeTc:
    def __init__(self, source_alias: int = 0x111) -> None:
        self.source_alias = source_alias
        self.frames: Signal[OlcbFrame] = Signal()


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_history_counts_repeated_events_per_alias() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    hist = PcerHistory(tc, clock=clock)
    try:
        ev_a = EventId.parse("05.01.01.01.4C.43.00.01")
        ev_b = EventId.parse("05.01.01.01.4C.43.00.02")
        # 0x222 emits A twice, B once.
        for _ in range(2):
            clock.advance(1.0)
            await tc.frames.emit(OlcbFrame(
                header=make_pcer_header(0x222), data=ev_a.bytes,
            ))
        clock.advance(1.0)
        await tc.frames.emit(OlcbFrame(
            header=make_pcer_header(0x222), data=ev_b.bytes,
        ))
        await asyncio.sleep(0)
        entries = hist.for_alias(0x222)
        by_event = {e.event: e for e in entries}
        assert by_event[ev_a].count == 2
        assert by_event[ev_b].count == 1
    finally:
        hist.close()


async def test_history_orders_newest_first() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    hist = PcerHistory(tc, clock=clock)
    try:
        ev_a = EventId.parse("05.01.01.01.4C.43.00.01")
        ev_b = EventId.parse("05.01.01.01.4C.43.00.02")
        await tc.frames.emit(OlcbFrame(
            header=make_pcer_header(0x222), data=ev_a.bytes,
        ))
        clock.advance(5.0)
        await tc.frames.emit(OlcbFrame(
            header=make_pcer_header(0x222), data=ev_b.bytes,
        ))
        await asyncio.sleep(0)
        entries = hist.for_alias(0x222)
        assert entries[0].event == ev_b
        assert entries[1].event == ev_a
    finally:
        hist.close()


async def test_history_lru_evicts_when_over_cap() -> None:
    """A peer emitting more distinct events than the cap loses the
    least-recently-seen entries."""
    tc = _FakeTc()
    hist = PcerHistory(tc, max_events_per_alias=3)
    try:
        for i in range(5):
            ev = EventId(0x0501010100000000 | i)
            await tc.frames.emit(OlcbFrame(
                header=make_pcer_header(0x222), data=ev.bytes,
            ))
        await asyncio.sleep(0)
        entries = hist.for_alias(0x222)
        assert len(entries) == 3
        # Kept: events 2, 3, 4 (newest three).
        events = {e.event.value for e in entries}
        assert events == {0x0501010100000002, 0x0501010100000003,
                          0x0501010100000004}
    finally:
        hist.close()


async def test_history_ignores_non_pcer_frames() -> None:
    """Verified NID, AMD, etc. must not show up in PCER history."""
    from pmri_openlcb.codec import (  # noqa: PLC0415
        FT_MTI,
        _pack_header,
        make_amd_header,
    )
    tc = _FakeTc()
    hist = PcerHistory(tc)
    try:
        # AMD from 0x222 -- should NOT count.
        await tc.frames.emit(OlcbFrame(
            header=make_amd_header(0x222),
            data=bytes.fromhex("020202020202"),
        ))
        # Verified NID (MTI 0x0170) -- also should NOT count.
        await tc.frames.emit(OlcbFrame(
            header=_pack_header(FT_MTI, 0x0170, 0x222),
            data=bytes.fromhex("020202020202"),
        ))
        await asyncio.sleep(0)
        assert hist.for_alias(0x222) == []
    finally:
        hist.close()


async def test_history_forget_drops_alias() -> None:
    tc = _FakeTc()
    hist = PcerHistory(tc)
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.01")
        await tc.frames.emit(OlcbFrame(
            header=make_pcer_header(0x222), data=ev.bytes,
        ))
        await asyncio.sleep(0)
        assert 0x222 in hist.known_aliases()
        hist.forget(0x222)
        assert 0x222 not in hist.known_aliases()
        assert hist.for_alias(0x222) == []
    finally:
        hist.close()


async def test_history_short_data_frames_ignored() -> None:
    """A malformed PCER with fewer than 8 bytes of data shouldn't
    crash or spuriously appear in history."""
    tc = _FakeTc()
    hist = PcerHistory(tc)
    try:
        await tc.frames.emit(OlcbFrame(
            header=make_pcer_header(0x222), data=b"\x05\x01",
        ))
        await asyncio.sleep(0)
        assert hist.for_alias(0x222) == []
    finally:
        hist.close()


# ---- connection wiring --------------------------------------------------


async def test_connection_exposes_pcer_history_by_default() -> None:
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_init_complete": True,
    })
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.01")
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_pcer_header(0x222), data=ev.bytes,
        ))
        await asyncio.sleep(0)
        entries = conn.pcer_history.for_alias(0x222)
        assert len(entries) == 1
        assert entries[0].event == ev
        assert entries[0].count == 1
    finally:
        await conn.close()


async def test_amr_clears_pcer_history_for_that_peer() -> None:
    """PCER history is wired into the release fan-out, so a peer's
    AMR drops its history alongside NID lookup + roster."""
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    from pmri_openlcb.codec import make_amr_header  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "node_id": "02.01.12.FE.DC.BA",
        "skip_init_complete": True,
    })
    try:
        ev = EventId.parse("05.01.01.01.4C.43.00.01")
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_pcer_header(0x222), data=ev.bytes,
        ))
        await asyncio.sleep(0)
        assert conn.pcer_history.for_alias(0x222)
        # AMR from 0x222 -> release fan-out fires.
        await conn._tc.frames.emit(OlcbFrame(  # type: ignore[union-attr]
            header=make_amr_header(0x222), data=b"",
        ))
        await asyncio.sleep(0)
        assert conn.pcer_history.for_alias(0x222) == []
    finally:
        await conn.close()
