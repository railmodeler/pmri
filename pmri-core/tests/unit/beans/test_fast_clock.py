"""Tests for the ``FastClock`` bean."""
from __future__ import annotations

import pytest

from pmri_core.beans.fast_clock import FastClock, SECONDS_PER_HOUR


# ----- defaults ------------------------------------------------------------


def test_defaults_are_midnight_stopped_one_rate() -> None:
    c = FastClock()
    assert c.rate == 1.0
    assert c.running is False
    assert c.time_seconds() == 0.0
    assert c.time_hhmm() == (0, 0)


# ----- set_time ------------------------------------------------------------


async def test_set_time_updates_display() -> None:
    c = FastClock()
    await c.set_time(8, 30)
    assert c.time_hhmm() == (8, 30)


async def test_set_time_validates_range() -> None:
    c = FastClock()
    with pytest.raises(ValueError):
        await c.set_time(24, 0)
    with pytest.raises(ValueError):
        await c.set_time(-1, 0)
    with pytest.raises(ValueError):
        await c.set_time(0, 60)


async def test_set_time_does_not_start() -> None:
    """Setting time leaves running unchanged."""
    c = FastClock()
    await c.set_time(8, 30)
    assert c.running is False


# ----- start/stop ----------------------------------------------------------


async def test_start_advances_clock(monkeypatch) -> None:  # noqa: ANN001
    """While running, the displayed time advances by ``rate x elapsed``."""
    fake_now = [100.0]

    def now() -> float:
        return fake_now[0]
    monkeypatch.setattr("time.monotonic", now)

    c = FastClock()
    await c.set_time(8, 0)
    await c.start()
    # 60 real seconds at rate=1 -> 60 fast seconds = 1 fast minute.
    fake_now[0] = 160.0
    assert c.time_hhmm() == (8, 1)


async def test_rate_speeds_up_clock(monkeypatch) -> None:  # noqa: ANN001
    fake_now = [100.0]

    def now() -> float:
        return fake_now[0]
    monkeypatch.setattr("time.monotonic", now)

    c = FastClock()
    await c.set_time(8, 0)
    await c.set_rate(4.0)
    await c.start()
    # 60 real seconds x 4x = 4 fast minutes.
    fake_now[0] = 160.0
    assert c.time_hhmm() == (8, 4)


async def test_stop_holds_time(monkeypatch) -> None:  # noqa: ANN001
    fake_now = [100.0]

    def now() -> float:
        return fake_now[0]
    monkeypatch.setattr("time.monotonic", now)

    c = FastClock()
    await c.set_time(8, 0)
    await c.set_rate(4.0)
    await c.start()
    fake_now[0] = 160.0  # 1 real minute = 4 fast minutes
    await c.stop()
    # Time stops advancing after stop().
    fake_now[0] = 200.0
    assert c.time_hhmm() == (8, 4)


async def test_set_rate_preserves_current_display(monkeypatch) -> None:  # noqa: ANN001
    """Changing rate mid-run must not jump the displayed time."""
    fake_now = [100.0]

    def now() -> float:
        return fake_now[0]
    monkeypatch.setattr("time.monotonic", now)

    c = FastClock()
    await c.set_time(10, 0)
    await c.set_rate(1.0)
    await c.start()
    fake_now[0] = 160.0   # +1 min at 1x
    before_h, before_m = c.time_hhmm()
    await c.set_rate(10.0)
    after_h, after_m = c.time_hhmm()
    assert (before_h, before_m) == (after_h, after_m)


async def test_set_rate_validates_range() -> None:
    c = FastClock()
    with pytest.raises(ValueError):
        await c.set_rate(0.0)
    with pytest.raises(ValueError):
        await c.set_rate(500.0)


# ----- 24-hour wrap --------------------------------------------------------


async def test_clock_wraps_at_midnight(monkeypatch) -> None:  # noqa: ANN001
    fake_now = [100.0]

    def now() -> float:
        return fake_now[0]
    monkeypatch.setattr("time.monotonic", now)

    c = FastClock()
    await c.set_time(23, 59)
    await c.set_rate(1.0)
    await c.start()
    # Advance 120 real seconds -> 2 fast minutes; should land at 00:01.
    fake_now[0] = 220.0
    assert c.time_hhmm() == (0, 1)


# ----- signal emission -----------------------------------------------------


async def test_state_change_fires_signal() -> None:
    c = FastClock()
    events: list[float] = []

    async def listener(change) -> None:  # noqa: ANN001
        events.append(change.rate)
    c.state_changed.connect(listener)

    await c.set_rate(2.0)
    await c.start()
    await c.set_time(12, 0)
    await c.stop()
    assert events == [2.0, 2.0, 2.0, 2.0]


# ----- pure utility --------------------------------------------------------


def test_seconds_per_hour_constant() -> None:
    assert SECONDS_PER_HOUR == 3600
