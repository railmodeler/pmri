"""``FastClock`` -- model-railroad fast clock.

Mirrors JMRI's ``Timebase``. Operators use a fast clock to compress
prototypical operating sessions (e.g. an 8-hour timetable runs in
2 real hours at rate=4). The bean holds:

* ``rate`` -- clock speedup multiplier (1.0 = real time, 4.0 = 4x faster);
* ``running`` -- whether the clock is advancing or held at a fixed time;
* the *displayed time* at the last anchor (set/start/stop/rate-change)
  paired with the monotonic real-clock timestamp at that anchor.

Reads are computed on demand from those anchors, so there's no
background task: ``time_seconds()`` returns ``base + (now - anchor) *
rate`` while running, or ``base`` while stopped. This keeps the bean
trivial to reason about and means a server restart that loses the
in-memory state simply resets to midnight, stopped -- a sensible
default for alpha.

Notifications: a :class:`Signal` fires whenever the bean is mutated,
so listeners (web UI poll, future LogixNG triggers) can react.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from jmri_core.events import Signal

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

# JMRI's Timebase clamps rate to ``[0.1, 100]``; we mirror that.
MIN_RATE = 0.1
MAX_RATE = 100.0


@dataclass
class FastClockChange:
    """Emitted whenever the fast clock is mutated. Listeners can poll
    :meth:`FastClock.time_seconds` directly if they need the current value."""

    rate: float
    running: bool
    seconds_since_midnight: float


@dataclass(frozen=True, slots=True)
class HardwareClockState:
    """Snapshot of a command-station-driven fast clock.

    Drivers whose command station has an authoritative fast clock (NCE,
    LocoNet's LNCP FastClock, etc.) return this from an async
    ``read_hardware_clock()`` method; the JSON ``TimeService`` prefers
    the hardware value over the software :class:`FastClock` bean when
    the connection exposes one.

    * *hours* / *minutes* / *seconds* -- the displayed 24-hour time
    * *rate* -- the CS's fast-clock ratio (typically integer)
    * *running* -- whether the CS clock is advancing
    * *twenty_four_hour* -- the CS's display mode (12h with AM/PM vs 24h)
    """

    hours: int
    minutes: int
    seconds: int
    rate: float
    running: bool
    twenty_four_hour: bool


@dataclass
class FastClock:
    """Software fast clock.

    Default state: midnight, stopped, 1x rate. Real-world time source
    is ``time.monotonic`` so the bean is unaffected by wall-clock
    adjustments (NTP step, manual changes, DST).
    """

    rate: float = 1.0
    running: bool = False
    _base_seconds: float = 0.0       # displayed fast-clock time at the anchor
    _real_anchor: float = field(default_factory=time.monotonic)
    state_changed: Signal[FastClockChange] = field(default_factory=Signal)

    # ----- read --------------------------------------------------------

    def time_seconds(self, *, now: float | None = None) -> float:
        """Current displayed time, in seconds since midnight (0..86399)."""
        if not self.running:
            return self._base_seconds % SECONDS_PER_DAY
        if now is None:
            now = time.monotonic()
        elapsed = (now - self._real_anchor) * self.rate
        return (self._base_seconds + elapsed) % SECONDS_PER_DAY

    def time_hhmm(self, *, now: float | None = None) -> tuple[int, int]:
        """Convenience: ``(hours, minutes)`` ready for display."""
        secs = int(self.time_seconds(now=now))
        return (secs // SECONDS_PER_HOUR, (secs % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE)

    # ----- mutate ------------------------------------------------------

    async def set_time(self, hours: int, minutes: int) -> None:
        """Set the displayed time. Does not affect the running flag."""
        if not 0 <= hours <= 23:
            raise ValueError(f"hours must be 0..23, got {hours}")
        if not 0 <= minutes <= 59:
            raise ValueError(f"minutes must be 0..59, got {minutes}")
        self._base_seconds = hours * SECONDS_PER_HOUR + minutes * SECONDS_PER_MINUTE
        self._real_anchor = time.monotonic()
        await self._notify()

    async def set_rate(self, rate: float) -> None:
        """Change the speedup multiplier without losing the displayed time."""
        if not MIN_RATE <= rate <= MAX_RATE:
            raise ValueError(f"rate must be {MIN_RATE}..{MAX_RATE}, got {rate}")
        # Capture current displayed time first so the change doesn't jump.
        current = self.time_seconds()
        self._base_seconds = current
        self._real_anchor = time.monotonic()
        self.rate = rate
        await self._notify()

    async def start(self) -> None:
        """Begin advancing the clock. Idempotent."""
        if self.running:
            return
        # Keep the displayed time stable across the start.
        self._real_anchor = time.monotonic()
        self.running = True
        await self._notify()

    async def stop(self) -> None:
        """Hold the clock at the current displayed time. Idempotent."""
        if not self.running:
            return
        self._base_seconds = self.time_seconds()
        self.running = False
        await self._notify()

    # ----- internal ----------------------------------------------------

    async def _notify(self) -> None:
        await self.state_changed.emit(FastClockChange(
            rate=self.rate,
            running=self.running,
            seconds_since_midnight=self.time_seconds(),
        ))
