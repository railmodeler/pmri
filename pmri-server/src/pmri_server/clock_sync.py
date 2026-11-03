"""Periodic hardware-clock sync task.

Reads ``connection.read_hardware_clock()`` on a fixed cadence and
mirrors the result into the software :class:`~pmri_core.beans.fast_clock.FastClock`
bean. The bean's ``state_changed`` signal fires on every mirror
write, which the WiThrottle server picks up and rebroadcasts as
PFT -- so a physical throttle running on the LocoNet bus and a
WiThrottle client end up converged on the CS clock rather than
drifting apart.

Attached in :func:`build_app`'s lifespan; skipped silently when the
active connection doesn't expose ``read_hardware_clock`` (Internal,
DCC-EX). Poll cadence is deliberately loose (30 s) to keep serial
bandwidth free for the turnout monitor + AIU poller -- a 30 s drift
window is invisible to anyone watching a fast clock.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from pmri_core.beans.fast_clock import FastClock, HardwareClockState

_log = logging.getLogger(__name__)

# Cadence. Long enough that the sync task never fights with the
# turnout monitor for bandwidth; short enough that fast-clock drift
# stays below a minute of face-value time.
_SYNC_INTERVAL_S = 30.0

# Grace after the connection opens before the first read attempt.
# NCE traffic controllers reject commands until the reachability
# monitor confirms the CS is up (~3 s post-open); LocoNet needs a
# similar window for the first inbound heartbeat.
_STARTUP_GRACE_S = 4.0


def _mirror_into_bean(bean: FastClock, state: HardwareClockState) -> None:
    """Update the software bean's anchor + rate + running flag from a
    hardware read. Fires ``state_changed`` -- same pattern
    :func:`pmri_server.services.fast_clock._mirror_to_bean` uses on
    the demand-read path."""
    bean.rate = state.rate
    bean.running = state.running
    bean._base_seconds = (      # noqa: SLF001 -- internal cache update
        state.hours * 3600 + state.minutes * 60 + state.seconds
    )
    bean._real_anchor = time.monotonic()   # noqa: SLF001


async def _sync_loop(
    ctx: Any,
    *,
    interval_s: float = _SYNC_INTERVAL_S,
    grace_s: float = _STARTUP_GRACE_S,
) -> None:
    """Background loop -- runs until the task is cancelled.

    Reads the hardware clock every *interval_s* seconds and mirrors
    the result into the ``FastClock`` bean on the context. Failures
    are logged at DEBUG and the loop continues; a persistently dark
    CS just means the software bean advances on its own until the CS
    comes back.
    """
    try:
        await asyncio.sleep(grace_s)
    except asyncio.CancelledError:
        return
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
        conn = getattr(ctx, "connection", None)
        if conn is None or not getattr(conn, "is_open", False):
            continue
        reader = getattr(conn, "read_hardware_clock", None)
        if reader is None:
            # Nothing to sync -- connection doesn't have a hardware
            # clock (Internal, DCC-EX). Exit; there's no point
            # burning wakeups.
            return
        try:
            state: HardwareClockState = await reader()
        except (TimeoutError, OSError, RuntimeError) as e:
            _log.debug("clock sync: hardware read failed (%s)", e)
            continue
        bean = getattr(ctx, "fast_clock", None)
        if bean is None:
            bean = FastClock()
            ctx.fast_clock = bean       # type: ignore[attr-defined]
        _mirror_into_bean(bean, state)
        try:
            await bean._notify()        # noqa: SLF001 -- fire state_changed
        except Exception:  # noqa: BLE001
            _log.exception("clock sync: FastClock notify failed")


def start_sync_task(ctx: Any) -> asyncio.Task[None] | None:
    """Kick off the periodic sync loop. Returns the task so the
    caller can cancel it on shutdown, or ``None`` if the active
    connection has no hardware clock."""
    conn = getattr(ctx, "connection", None)
    if conn is None or not hasattr(conn, "read_hardware_clock"):
        return None
    task = asyncio.create_task(_sync_loop(ctx), name="fast-clock-sync")
    _log.info("fast-clock sync task started (%.0fs cadence)", _SYNC_INTERVAL_S)
    return task


async def stop_sync_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
