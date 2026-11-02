"""``TimeService`` -- JSON wire interface for the model-railroad Fast Clock.

JMRI exposes the fast clock under the ``time`` topic. Wire shape:

* ``method=get`` -> ``{"time": "HH:MM", "rate": float, "running": bool}``
* ``method=post``, ``data={"rate": float, "running": bool, "time": "HH:MM"}``
  -> applies any provided field; returns the new state. All fields optional.

When the active connection has an authoritative command-station clock
(NCE PowerPro, LocoNet with LNCP, ...) the driver exposes async methods
``read_hardware_clock`` / ``set_hardware_clock_time`` /
``set_hardware_clock_rate`` / ``start_hardware_clock`` /
``stop_hardware_clock``. The service prefers those over the software
:class:`FastClock` bean so ``Get`` returns the CS's real time and
``Apply`` pushes changes down to the command station.

Absent hardware support, the software bean is the source of truth. Its
state persists across GET/POST calls on the same server process; a
restart resets to midnight / 1x / stopped (the JMRI default).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from jmri_core.beans.fast_clock import FastClock, HardwareClockState

from jmri_server.service import JsonService, JsonServiceError

_log = logging.getLogger(__name__)

# How long a successful hardware-clock read stays authoritative. The web
# widget polls at 1 Hz; without a cache we'd hit the CS every second and
# starve co-tenant readers (turnout monitor, AIU poller). 5 s is enough
# for the software bean to advance visibly between resyncs.
HARDWARE_CLOCK_CACHE_S = 5.0


def _project_state(hours: int, minutes: int, rate: float, running: bool) -> dict[str, Any]:
    return {
        "time": f"{hours:02d}:{minutes:02d}",
        "rate": rate,
        "running": running,
    }


def _project(clock: FastClock) -> dict[str, Any]:
    h, m = clock.time_hhmm()
    return _project_state(h, m, clock.rate, clock.running)


def _project_hardware(state: HardwareClockState) -> dict[str, Any]:
    return _project_state(state.hours, state.minutes, state.rate, state.running)


def _get_or_create_clock(ctx: Any) -> FastClock:
    """Singleton :class:`FastClock` on the server context."""
    clock = getattr(ctx, "fast_clock", None)
    if clock is None:
        clock = FastClock()
        ctx.fast_clock = clock  # type: ignore[attr-defined]
    return clock


def _hardware_clock(ctx: Any) -> Any | None:
    """The active connection if it has a CS-driven fast clock, else None."""
    conn = getattr(ctx, "connection", None)
    if conn is None or not getattr(conn, "is_open", False):
        return None
    return conn if hasattr(conn, "read_hardware_clock") else None


def _mirror_to_bean(clock: FastClock, state: HardwareClockState) -> None:
    """Reset the software bean anchor so its display shows the CS time.

    Between GET calls the WebSocket poll re-renders from the bean; without
    this the bean would drift back to midnight while the widget waits
    for the next Get click.
    """
    # Use the underlying fields directly -- set_time/start/stop would emit
    # events and clip on validation. This is an internal cache update.
    clock.rate = state.rate
    clock.running = state.running
    clock._base_seconds = (
        state.hours * 3600 + state.minutes * 60 + state.seconds
    )
    clock._real_anchor = time.monotonic()


def _cache_is_fresh(ctx: Any) -> bool:
    """Have we successfully read the CS within ``HARDWARE_CLOCK_CACHE_S``?"""
    ts = getattr(ctx, "_hw_clock_read_at", None)
    return isinstance(ts, float) and (time.monotonic() - ts) < HARDWARE_CLOCK_CACHE_S


def _invalidate_cache(ctx: Any) -> None:
    if hasattr(ctx, "_hw_clock_read_at"):
        delattr(ctx, "_hw_clock_read_at")


def _record_cache(ctx: Any) -> None:
    ctx._hw_clock_read_at = time.monotonic()  # type: ignore[attr-defined]


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise JsonServiceError(400, f"time must be 'HH:MM', got {value!r}")
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        raise JsonServiceError(400, f"time must be 'HH:MM', got {value!r}") from None
    return hours, minutes


class TimeService(JsonService):
    """Fast Clock JSON service. Topic: ``time``."""

    topic = "time"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        conn = _hardware_clock(self.ctx)
        clock = _get_or_create_clock(self.ctx)
        if conn is not None:
            # When the CS cache is fresh, return the software bean --
            # which was mirrored from the last CS read and continues to
            # advance locally. This bounds CS-side traffic to one poll
            # per ``HARDWARE_CLOCK_CACHE_S`` so co-tenant readers (turnout
            # monitor, AIU poller) keep their airtime.
            if _cache_is_fresh(self.ctx):
                return _project(clock)
            try:
                state = await conn.read_hardware_clock()
            except (TimeoutError, OSError) as e:
                _log.warning("hardware clock read failed, using bean: %s", e)
            else:
                _mirror_to_bean(clock, state)
                _record_cache(self.ctx)
                return _project_hardware(state)
        return _project(clock)

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        clock = _get_or_create_clock(self.ctx)
        conn = _hardware_clock(self.ctx)

        # Apply each field if present. On hardware-backed connections,
        # write through to the CS *first* -- the software bean cache is
        # then refreshed from a follow-up read. Hardware writes can time
        # out under serial-link contention; surface those as a 504-ish
        # JsonServiceError instead of a bare 500 so the UI can show a
        # graceful message.
        if "rate" in data:
            try:
                rate = float(data["rate"])
            except (TypeError, ValueError):
                raise JsonServiceError(400, "rate must be numeric") from None
            if conn is not None:
                try:
                    await conn.set_hardware_clock_rate(rate)
                except TimeoutError:
                    raise JsonServiceError(
                        504, "command station did not acknowledge the rate change"
                    ) from None
            try:
                await clock.set_rate(rate)
            except ValueError as e:
                raise JsonServiceError(400, str(e)) from None

        if "time" in data:
            value = data["time"]
            if not isinstance(value, str):
                raise JsonServiceError(400, "time must be 'HH:MM' string")
            hours, minutes = _parse_hhmm(value)
            if conn is not None:
                try:
                    await conn.set_hardware_clock_time(hours, minutes, 0)
                except TimeoutError:
                    raise JsonServiceError(
                        504, "command station did not acknowledge the time change"
                    ) from None
            try:
                await clock.set_time(hours, minutes)
            except ValueError as e:
                raise JsonServiceError(400, str(e)) from None

        if "running" in data:
            run = bool(data["running"])
            if conn is not None:
                try:
                    if run:
                        await conn.start_hardware_clock()
                    else:
                        await conn.stop_hardware_clock()
                except TimeoutError:
                    raise JsonServiceError(
                        504, "command station did not acknowledge start/stop"
                    ) from None
            if run:
                await clock.start()
            else:
                await clock.stop()

        # Re-project from the CS if we have one so the caller sees the
        # authoritative state, not the (possibly stale) bean. A write
        # always invalidates the cache -- the caller needs the ground
        # truth to know its update took effect.
        if conn is not None:
            _invalidate_cache(self.ctx)
            try:
                state = await conn.read_hardware_clock()
            except (TimeoutError, OSError) as e:
                _log.warning("hardware clock read after write failed: %s", e)
            else:
                _mirror_to_bean(clock, state)
                _record_cache(self.ctx)
                return _project_hardware(state)
        return _project(clock)
