from __future__ import annotations

import asyncio

import pytest

from pmri_core.events import Signal


async def test_signal_emit_to_sync_listener() -> None:
    sig: Signal[int] = Signal()
    received: list[int] = []
    sig.connect(received.append)
    await sig.emit(42)
    assert received == [42]


async def test_signal_emit_to_async_listener() -> None:
    sig: Signal[int] = Signal()
    received: list[int] = []

    async def handler(value: int) -> None:
        await asyncio.sleep(0)
        received.append(value)

    sig.connect(handler)
    await sig.emit(7)
    assert received == [7]


async def test_signal_disconnect_returns_callable() -> None:
    sig: Signal[int] = Signal()
    seen: list[int] = []
    disconnect = sig.connect(seen.append)
    assert sig.listener_count == 1
    disconnect()
    assert sig.listener_count == 0
    await sig.emit(1)
    assert seen == []


async def test_signal_disconnect_is_idempotent() -> None:
    sig: Signal[int] = Signal()
    disconnect = sig.connect(lambda _: None)
    disconnect()
    disconnect()  # second call must not raise


async def test_signal_preserves_listener_ordering() -> None:
    sig: Signal[int] = Signal()
    order: list[int] = []
    sig.connect(lambda _: order.append(1))
    sig.connect(lambda _: order.append(2))
    sig.connect(lambda _: order.append(3))
    await sig.emit(0)
    assert order == [1, 2, 3]


async def test_signal_disconnect_during_emit_does_not_skip() -> None:
    sig: Signal[int] = Signal()
    received: list[str] = []

    def first(_: int) -> None:
        received.append("first")
        disconnect_self()

    def second(_: int) -> None:
        received.append("second")

    disconnect_self = sig.connect(first)
    sig.connect(second)

    await sig.emit(0)
    # Snapshot semantics: first run, then second still runs even though
    # first removed itself mid-dispatch.
    assert received == ["first", "second"]


async def test_signal_collects_errors_and_reraises_first() -> None:
    sig: Signal[int] = Signal()
    later_ran = False

    def fail(_: int) -> None:
        raise RuntimeError("boom")

    def later(_: int) -> None:
        nonlocal later_ran
        later_ran = True

    sig.connect(fail)
    sig.connect(later)

    with pytest.raises(RuntimeError, match="boom"):
        await sig.emit(0)
    assert later_ran, "subsequent listener must still run despite earlier error"


async def test_signal_disconnect_all_drops_every_listener() -> None:
    sig: Signal[int] = Signal()
    received: list[int] = []
    sig.connect(received.append)
    sig.connect(received.append)
    sig.disconnect_all()
    await sig.emit(99)
    assert received == []


async def test_signal_emit_concurrent_dispatches_async_in_parallel() -> None:
    sig: Signal[int] = Signal()
    order: list[str] = []

    async def slow(_: int) -> None:
        await asyncio.sleep(0.02)
        order.append("slow")

    async def fast(_: int) -> None:
        await asyncio.sleep(0)
        order.append("fast")

    sig.connect(slow)
    sig.connect(fast)
    await sig.emit_concurrent(0)
    # In sequential emit() order would be ["slow", "fast"]; concurrent gather
    # lets the fast one finish first.
    assert order == ["fast", "slow"]
