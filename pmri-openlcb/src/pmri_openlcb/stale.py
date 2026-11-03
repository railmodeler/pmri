"""Track when we last heard from each peer alias + prune the silent ones.

AMR handling (:mod:`pmri_openlcb.alias`) drops peers that cleanly
release their alias at shutdown. A peer that loses power skips AMR
entirely -- it just goes silent -- so without a sweep, the discovery
UI keeps showing ghosts forever.

:class:`LastSeenTracker` records the wall-clock time of the most
recent inbound frame per alias. :class:`StalePeerSweeper` runs a
periodic asyncio task that finds aliases stale beyond a threshold
and hands them off to the AMR release fan-out -- so subscribers
(NID map, event roster, SNIP/PIP caches) forget the peer via the
same code path as a clean shutdown.

Wall-clock time (``time.time``) is deliberate: the sweeper checks
against ``now``, and every timestamp source needs to agree on that
frame of reference. Monotonic time would be safer against wall-
clock jumps but makes it harder to reason about in ops (an operator
wants to answer "when did node X last speak" against their watch).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from pmri_openlcb.codec import OlcbFrame, extract_source_alias

if TYPE_CHECKING:
    from pmri_openlcb.alias import AliasReleaseWatcher, OlcbTrafficLike

_log = logging.getLogger(__name__)


DEFAULT_STALE_THRESHOLD = 300.0
DEFAULT_SWEEP_INTERVAL = 30.0


class LastSeenTracker:
    """Record the wall-clock time each peer alias last put a frame on
    the wire. Every inbound frame updates the entry for its source
    alias; the local alias is excluded so we don't chase ourselves.
    """

    def __init__(
        self,
        tc: OlcbTrafficLike,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._tc = tc
        self._clock = clock
        self._last_seen: dict[int, float] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    def touch(self, alias: int) -> None:
        """Record a sighting of *alias* at ``clock()`` now. Public
        so callers who observe a peer through other channels (a
        datagram, a targeted MTI reply) can keep it fresh."""
        self._last_seen[alias] = self._clock()

    def last_seen(self, alias: int) -> float | None:
        return self._last_seen.get(alias)

    def known(self) -> dict[int, float]:
        """Snapshot of the alias->timestamp map."""
        return dict(self._last_seen)

    def stale_aliases(self, *, threshold: float) -> list[int]:
        """Aliases whose last sighting is older than *threshold*
        seconds. Returned in ascending-timestamp order (oldest first)
        so a caller can prune the deadest peers first."""
        cutoff = self._clock() - threshold
        stale = [
            (ts, alias) for alias, ts in self._last_seen.items()
            if ts < cutoff
        ]
        stale.sort()
        return [alias for _, alias in stale]

    def forget(self, alias: int) -> None:
        """Drop the entry for *alias*. Call this from an AMR forget
        hook so a released peer that later re-joins with the same
        alias starts fresh instead of looking instantly stale."""
        self._last_seen.pop(alias, None)

    async def _on_frame(self, frame: OlcbFrame) -> None:
        # Ignore frames from our own alias -- the traffic controller
        # loops back what we send, and we're not going to prune
        # ourselves.
        alias = extract_source_alias(frame.header)
        if alias == self._tc.source_alias:
            return
        self._last_seen[alias] = self._clock()


class StalePeerSweeper:
    """Periodically nudge :class:`AliasReleaseWatcher` about aliases
    that have gone silent past a threshold. Uses the same fan-out as
    clean AMR so subscribers can't tell the two cases apart -- a peer
    is either present or gone.

    The task runs at most once per *interval* seconds. On each tick
    it consults *tracker* and fires the watcher's release callbacks
    for every stale alias, then drops the entry from the tracker so
    subsequent ticks don't re-fire.
    """

    def __init__(
        self,
        tracker: LastSeenTracker,
        watcher: AliasReleaseWatcher,
        *,
        threshold: float = DEFAULT_STALE_THRESHOLD,
        interval: float = DEFAULT_SWEEP_INTERVAL,
    ) -> None:
        self._tracker = tracker
        self._watcher = watcher
        self._threshold = threshold
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> None:
        """Start the background sweep. Idempotent -- starting a
        running sweeper is a no-op."""
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(), name="olcb-stale-sweep",
        )

    async def close(self) -> None:
        """Cancel the background task. Safe to call more than once."""
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
        self._stop = None

    def sweep_once(self) -> list[int]:
        """Run one sweep synchronously. Fires release callbacks for
        each stale alias and returns the list of aliases that were
        pruned. Exposed for tests + operator-triggered "prune now"
        actions."""
        stale = self._tracker.stale_aliases(threshold=self._threshold)
        for alias in stale:
            # Drop from the tracker first so a callback that touches
            # the tracker (e.g. re-reading known()) sees the pruned
            # state, and so a re-join with the same alias starts
            # fresh instead of looking instantly stale again.
            self._tracker.forget(alias)
            self._watcher.notify_release(alias)
        return stale

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval,
                )
                return   # stop was set -> exit
            except asyncio.TimeoutError:
                pass
            try:
                pruned = self.sweep_once()
                if pruned:
                    _log.info(
                        "pruned %d stale peer(s): %s",
                        len(pruned),
                        [f"{a:#05x}" for a in pruned],
                    )
            except Exception:
                _log.exception("stale-peer sweep failed")
