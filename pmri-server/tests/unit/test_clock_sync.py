"""Unit tests for :mod:`pmri_server.clock_sync`.

The periodic sync loop is time-driven; we test it by handing it a
tiny interval and a fake connection so a single cycle runs
deterministically.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pmri_core.beans.fast_clock import FastClock, HardwareClockState

from pmri_server.clock_sync import _sync_loop, start_sync_task


class _FakeConn:
    """Minimal ``SystemConnection`` stand-in for the sync loop."""

    def __init__(self, state: HardwareClockState, *, is_open: bool = True) -> None:
        self._state = state
        self.is_open = is_open
        self.reads = 0

    async def read_hardware_clock(self) -> HardwareClockState:
        self.reads += 1
        return self._state


class _Ctx:
    """The bits of :class:`ServerContext` the sync loop reads."""

    def __init__(self, conn: Any) -> None:
        self.connection = conn
        self.fast_clock: FastClock | None = None


async def test_sync_loop_mirrors_hardware_state_into_bean() -> None:
    state = HardwareClockState(
        hours=8, minutes=15, seconds=30, rate=4.0, running=True,
        twenty_four_hour=True,
    )
    ctx = _Ctx(_FakeConn(state))

    task = asyncio.create_task(
        _sync_loop(ctx, interval_s=0.01, grace_s=0.0),
    )
    # Give the loop enough time for the first read to land.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ctx.fast_clock is not None
    assert ctx.fast_clock.rate == 4.0
    assert ctx.fast_clock.running is True
    # Base seconds equal the projected time -- allow small drift because
    # ``time_seconds()`` advances from the anchor.
    projected = ctx.fast_clock.time_seconds()
    expected = 8 * 3600 + 15 * 60 + 30
    assert abs(projected - expected) < 2


async def test_sync_loop_fires_state_changed_signal() -> None:
    """The sync loop's whole point is to trigger WiThrottle
    rebroadcasts via ``state_changed``. A missing emit here would
    mean throttles don't converge on CS time even though the bean
    is up to date -- silent regression, worth pinning."""
    state = HardwareClockState(
        hours=1, minutes=0, seconds=0, rate=1.0, running=True,
        twenty_four_hour=True,
    )
    ctx = _Ctx(_FakeConn(state))
    ctx.fast_clock = FastClock()
    seen = []
    ctx.fast_clock.state_changed.connect(lambda ev: seen.append(ev))

    task = asyncio.create_task(
        _sync_loop(ctx, interval_s=0.01, grace_s=0.0),
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert seen, "expected at least one FastClockChange"


async def test_sync_loop_exits_when_connection_has_no_hardware_clock() -> None:
    """Internal / DCC-EX / any driver without a hardware clock: the
    loop must give up rather than spin forever."""

    class _NoClockConn:
        is_open = True
        # Deliberately no ``read_hardware_clock`` method.

    ctx = _Ctx(_NoClockConn())
    task = asyncio.create_task(
        _sync_loop(ctx, interval_s=0.01, grace_s=0.0),
    )
    # Give it plenty of ticks to notice + exit.
    await asyncio.sleep(0.1)
    assert task.done()


async def test_sync_loop_survives_read_errors() -> None:
    """A raising ``read_hardware_clock`` (serial timeout, CS off)
    must not kill the loop -- the next cycle tries again."""

    class _FlakyConn:
        is_open = True
        calls = 0

        async def read_hardware_clock(self):  # noqa: ANN201
            _FlakyConn.calls += 1
            raise TimeoutError("no reply")

    ctx = _Ctx(_FlakyConn())
    task = asyncio.create_task(
        _sync_loop(ctx, interval_s=0.01, grace_s=0.0),
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Multiple cycles ran despite the errors.
    assert _FlakyConn.calls >= 2


async def test_start_sync_task_returns_none_when_unsupported() -> None:
    """``start_sync_task`` on a connection without ``read_hardware_clock``
    should give the caller ``None`` so the lifespan doesn't try to
    cancel a task that was never created."""

    class _Bare:
        is_open = True

    class _BareCtx:
        connection = _Bare()

    assert start_sync_task(_BareCtx()) is None
