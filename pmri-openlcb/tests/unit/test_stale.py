"""LastSeenTracker + StalePeerSweeper -- timestamp bookkeeping and
periodic pruning of silently-vanished peers.
"""
from __future__ import annotations

import asyncio

from pmri_core.events import Signal

from pmri_openlcb.alias import AliasReleaseWatcher
from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
)
from pmri_openlcb.stale import (
    DEFAULT_STALE_THRESHOLD,
    LastSeenTracker,
    StalePeerSweeper,
)
from pmri_openlcb.verify import MTI_VERIFIED_NID


class _FakeClock:
    """Test double for wall-clock time: caller advances it explicitly.
    Keeps the sweep logic free of real sleeps in the fast unit tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTc:
    def __init__(self, source_alias: int = 0x111) -> None:
        self.frames: Signal[OlcbFrame] = Signal()
        self.source_alias = source_alias


def _peer_frame(alias: int) -> OlcbFrame:
    return OlcbFrame(
        header=_pack_header(FT_MTI, MTI_VERIFIED_NID, alias),
        data=b"\x02\x01\x12\x00\x00\x02",
    )


# ---- LastSeenTracker -----------------------------------------------------


async def test_tracker_records_peer_last_seen() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    tracker = LastSeenTracker(tc, clock=clock)
    try:
        await tc.frames.emit(_peer_frame(0x222))
        await asyncio.sleep(0)
        assert tracker.last_seen(0x222) == 1000.0
        # Later frame refreshes the timestamp.
        clock.advance(30.0)
        await tc.frames.emit(_peer_frame(0x222))
        await asyncio.sleep(0)
        assert tracker.last_seen(0x222) == 1030.0
    finally:
        tracker.close()


async def test_tracker_ignores_our_own_alias() -> None:
    """Frames we sent loop back through the tc; the tracker mustn't
    treat us as a peer, or the sweeper could try to prune the local
    node."""
    tc = _FakeTc(source_alias=0x111)
    tracker = LastSeenTracker(tc, clock=_FakeClock())
    try:
        await tc.frames.emit(_peer_frame(0x111))
        await asyncio.sleep(0)
        assert tracker.last_seen(0x111) is None
    finally:
        tracker.close()


async def test_tracker_stale_aliases_are_oldest_first() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    tracker = LastSeenTracker(tc, clock=clock)
    try:
        # 0x222 seen at t=1000, 0x333 at t=1010, 0x444 at t=1050.
        await tc.frames.emit(_peer_frame(0x222))
        clock.advance(10.0)
        await tc.frames.emit(_peer_frame(0x333))
        clock.advance(40.0)
        await tc.frames.emit(_peer_frame(0x444))
        await asyncio.sleep(0)
        # At t=1200, threshold 180s -> 0x222 (200s old) + 0x333 (190s old)
        # stale, 0x444 (150s old) still fresh.
        clock.advance(150.0)
        stale = tracker.stale_aliases(threshold=180.0)
        assert stale == [0x222, 0x333]
    finally:
        tracker.close()


async def test_tracker_forget_drops_the_entry() -> None:
    tc = _FakeTc()
    tracker = LastSeenTracker(tc, clock=_FakeClock())
    try:
        await tc.frames.emit(_peer_frame(0x222))
        await asyncio.sleep(0)
        tracker.forget(0x222)
        assert tracker.last_seen(0x222) is None
    finally:
        tracker.close()


async def test_tracker_touch_records_current_time() -> None:
    tc = _FakeTc()
    clock = _FakeClock(start=5000.0)
    tracker = LastSeenTracker(tc, clock=clock)
    try:
        tracker.touch(0x222)
        assert tracker.last_seen(0x222) == 5000.0
    finally:
        tracker.close()


# ---- StalePeerSweeper ----------------------------------------------------


async def test_sweep_once_fires_release_for_stale_peers() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    tracker = LastSeenTracker(tc, clock=clock)
    watcher = AliasReleaseWatcher(tc)
    released: list[int] = []
    watcher.on_release(released.append)
    sweeper = StalePeerSweeper(
        tracker, watcher, threshold=100.0, interval=1000.0,
    )
    try:
        await tc.frames.emit(_peer_frame(0x222))
        await asyncio.sleep(0)
        # Not yet stale.
        clock.advance(50.0)
        pruned = sweeper.sweep_once()
        assert pruned == []
        assert released == []
        # Now stale.
        clock.advance(60.0)
        pruned = sweeper.sweep_once()
        assert pruned == [0x222]
        assert released == [0x222]
        # And the tracker is cleared so a repeat sweep is a no-op.
        assert tracker.last_seen(0x222) is None
        assert sweeper.sweep_once() == []
    finally:
        watcher.close()
        tracker.close()


async def test_sweep_prunes_multiple_peers_in_one_pass() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    tracker = LastSeenTracker(tc, clock=clock)
    watcher = AliasReleaseWatcher(tc)
    released: list[int] = []
    watcher.on_release(released.append)
    sweeper = StalePeerSweeper(
        tracker, watcher, threshold=100.0, interval=1000.0,
    )
    try:
        await tc.frames.emit(_peer_frame(0x222))
        await tc.frames.emit(_peer_frame(0x333))
        await asyncio.sleep(0)
        clock.advance(200.0)
        pruned = sweeper.sweep_once()
        assert sorted(pruned) == [0x222, 0x333]
        assert sorted(released) == [0x222, 0x333]
    finally:
        watcher.close()
        tracker.close()


async def test_periodic_sweep_task_fires_on_interval() -> None:
    tc = _FakeTc()
    clock = _FakeClock()
    tracker = LastSeenTracker(tc, clock=clock)
    watcher = AliasReleaseWatcher(tc)
    released: list[int] = []
    watcher.on_release(released.append)
    # Very short interval so the test doesn't wait long.
    sweeper = StalePeerSweeper(
        tracker, watcher, threshold=0.05, interval=0.02,
    )
    try:
        await tc.frames.emit(_peer_frame(0x222))
        await asyncio.sleep(0)
        clock.advance(0.1)
        sweeper.start()
        # Give the loop at least one tick.
        await asyncio.sleep(0.08)
        assert released == [0x222]
    finally:
        await sweeper.close()
        watcher.close()
        tracker.close()


async def test_sweep_defaults_match_module_constants() -> None:
    tc = _FakeTc()
    tracker = LastSeenTracker(tc)
    watcher = AliasReleaseWatcher(tc)
    sweeper = StalePeerSweeper(tracker, watcher)
    try:
        assert sweeper._threshold == DEFAULT_STALE_THRESHOLD
    finally:
        watcher.close()
        tracker.close()


# ---- Connection wiring ---------------------------------------------------


async def test_connection_wires_stale_sweep_by_default() -> None:
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        # Aggressive so the test finishes fast.
        "stale_threshold": 0.05,
        "sweep_interval": 0.02,
    })
    try:
        # Peer identifies itself; then we simulate silent power-off
        # by never sending anything else.
        peer_alias = 0x222
        await conn._tc.frames.emit(_peer_frame(peer_alias))  # type: ignore[union-attr]
        await asyncio.sleep(0)
        assert conn.node_id_lookup.get(peer_alias) is not None
        # Wait through a couple of sweep intervals.
        await asyncio.sleep(0.15)
        assert conn.node_id_lookup.get(peer_alias) is None
        assert conn.last_seen.last_seen(peer_alias) is None
    finally:
        await conn.close()


async def test_connection_skip_stale_sweep_leaves_peers_alone() -> None:
    """``skip_stale_sweep=True`` keeps the tracker but never runs the
    background task, so a silent peer remains in the map."""
    from pmri_openlcb import OlcbConnection, loopback_pair  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x111,
        "skip_stale_sweep": True,
    })
    try:
        peer_alias = 0x222
        await conn._tc.frames.emit(_peer_frame(peer_alias))  # type: ignore[union-attr]
        await asyncio.sleep(0.05)
        assert conn.node_id_lookup.get(peer_alias) is not None
        # But the tracker is still recording, so operators can query
        # last_seen via the API even without an auto-prune.
        assert conn.last_seen.last_seen(peer_alias) is not None
    finally:
        await conn.close()
