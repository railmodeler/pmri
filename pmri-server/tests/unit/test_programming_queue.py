"""ProgrammingQueue -- serial CV writes with progress + cancel."""
from __future__ import annotations

import asyncio

import pytest

from pmri_server.programming_queue import (
    BatchInFlightError,
    BatchState,
    BatchWrite,
    ProgrammingQueue,
    WriteResult,
    get_or_create_queue,
)


def _writes(*pairs: tuple[int, int]) -> list[BatchWrite]:
    return [BatchWrite(cv=cv, value=val, item=f"CV{cv}") for cv, val in pairs]


async def test_empty_batch_completes_as_done() -> None:
    q = ProgrammingQueue("L1", lambda *a, **kw: _noop())
    batch = await q.start([])
    await asyncio.wait_for(batch._task, timeout=1.0)
    # No writes -> the terminal state is DONE (nothing to do, nothing failed).
    assert batch.state is BatchState.DONE
    assert batch.completed == 0
    assert batch.total == 0


async def _noop() -> int:
    return 0


async def test_batch_writes_serially_in_declared_order() -> None:
    order: list[int] = []

    async def cb(cv: int, value: int, item: str, extra: dict) -> int:
        order.append(cv)
        return value

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3), (2, 5), (29, 38)))
    await asyncio.wait_for(batch._task, timeout=1.0)
    assert batch.state is BatchState.DONE
    # Order preserved -- that's the whole point of a serial queue.
    assert order == [1, 2, 29]
    # All writes reported DONE.
    assert [w.result for w in batch.writes] == [WriteResult.DONE] * 3


async def test_batch_captures_write_failures_but_continues() -> None:
    """One CV failing must not abort the whole batch -- the user needs
    every not-failing write to complete so a retry only picks up the
    truly stuck writes."""
    async def cb(cv: int, value: int, item: str, extra: dict) -> int:
        if cv == 2:
            raise RuntimeError("hardware not ready")
        return value

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3), (2, 5), (29, 38)))
    await asyncio.wait_for(batch._task, timeout=1.0)
    # Terminal DONE because not all failed.
    assert batch.state is BatchState.DONE
    results = [w.result for w in batch.writes]
    assert results == [WriteResult.DONE, WriteResult.ERROR, WriteResult.DONE]
    assert "hardware" in batch.writes[1].error


async def test_batch_state_is_error_when_every_write_fails() -> None:
    async def cb(*args, **kwargs) -> int:  # noqa: ANN002, ANN003
        raise RuntimeError("boom")

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3), (2, 5)))
    await asyncio.wait_for(batch._task, timeout=1.0)
    assert batch.state is BatchState.ERROR
    assert batch.error == "all writes failed"


async def test_cancel_stops_after_current_write_completes() -> None:
    """User's spec: 'cancel after the completion of the in-flight
    operation'. The current write finishes, everything after it is
    marked cancelled, batch state becomes CANCELLED."""
    write_completed = asyncio.Event()
    can_finish = asyncio.Event()

    async def cb(cv: int, value: int, item: str, extra: dict) -> int:
        if cv == 1:
            # Signal the test to fire cancel, then hold until released.
            write_completed.set()
            await can_finish.wait()
        return value

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3), (2, 5), (29, 38)))
    # First write is holding -- fire cancel now.
    await write_completed.wait()
    q.cancel()
    can_finish.set()
    await asyncio.wait_for(batch._task, timeout=1.0)
    assert batch.state is BatchState.CANCELLED
    # First write finished normally; the rest were skipped.
    assert batch.writes[0].result is WriteResult.DONE
    assert batch.writes[1].result is WriteResult.CANCELLED
    assert batch.writes[2].result is WriteResult.CANCELLED


async def test_second_start_before_first_finishes_raises() -> None:
    """A queue holds one batch at a time. Overlapping starts must fail
    loudly so the UI knows to cancel or wait rather than silently
    corrupting the running batch."""
    hold = asyncio.Event()

    async def cb(*a, **kw) -> int:  # noqa: ANN002, ANN003
        await hold.wait()
        return 0

    q = ProgrammingQueue("L1", cb)
    _ = await q.start(_writes((1, 3)))
    with pytest.raises(BatchInFlightError):
        await q.start(_writes((2, 5)))
    hold.set()   # let the first batch finish so pytest cleans up.


async def test_start_after_terminal_batch_replaces_it() -> None:
    """A completed batch stays on the queue for status polling; the
    next start silently supersedes it (semantically, cleared)."""
    async def cb(cv: int, value: int, item: str, extra: dict) -> int:
        return value

    q = ProgrammingQueue("L1", cb)
    b1 = await q.start(_writes((1, 3)))
    await asyncio.wait_for(b1._task, timeout=1.0)
    b2 = await q.start(_writes((2, 5)))
    await asyncio.wait_for(b2._task, timeout=1.0)
    assert q.current is b2
    assert b2.state is BatchState.DONE


async def test_clear_removes_terminal_batch() -> None:
    async def cb(cv: int, value: int, item: str, extra: dict) -> int:
        return value

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3)))
    await asyncio.wait_for(batch._task, timeout=1.0)
    q.clear()
    assert q.current is None


async def test_clear_refuses_when_batch_still_running() -> None:
    hold = asyncio.Event()

    async def cb(*a, **kw) -> int:  # noqa: ANN002, ANN003
        await hold.wait()
        return 0

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3)))
    with pytest.raises(BatchInFlightError):
        q.clear()
    hold.set()
    await asyncio.wait_for(batch._task, timeout=1.0)


async def test_status_snapshot_reflects_in_flight_write() -> None:
    hold = asyncio.Event()
    started = asyncio.Event()

    async def cb(cv: int, value: int, item: str, extra: dict) -> int:
        started.set()
        await hold.wait()
        return value

    q = ProgrammingQueue("L1", cb)
    batch = await q.start(_writes((1, 3)))
    await started.wait()
    snap = batch.to_status()
    assert snap["state"] == "in_progress"
    assert snap["completed"] == 0
    assert snap["total"] == 1
    assert snap["in_flight"] == {"cv": 1, "item": "CV1", "value": 3}
    hold.set()
    await asyncio.wait_for(batch._task, timeout=1.0)


async def test_get_or_create_queue_reuses_same_instance() -> None:
    """One queue per loco. Different locos get different queues."""
    class _Ctx: pass
    ctx = _Ctx()

    async def cb(*a, **kw) -> int:  # noqa: ANN002, ANN003
        return 0

    q1 = get_or_create_queue(ctx, "L1", cb)
    q2 = get_or_create_queue(ctx, "L1", cb)
    q3 = get_or_create_queue(ctx, "L2", cb)
    assert q1 is q2
    assert q3 is not q1


async def test_batch_reads_dispatch_through_read_callback() -> None:
    """Read ops go to ``read_callback`` (not ``write_callback``), and
    the returned byte is stored back into ``op.value`` so the status
    snapshot's shape stays uniform with writes."""
    async def write_cb(cv, value, item, extra):   # noqa: ANN001, ANN202
        raise AssertionError("write callback should not fire for read ops")

    read_values = {1: 3, 7: 151, 29: 34}
    async def read_cb(cv, item, extra):   # noqa: ANN001, ANN202
        return read_values.get(cv, 0)

    q = ProgrammingQueue("L1", write_cb, read_callback=read_cb)
    ops = [
        BatchWrite(cv=1, kind="read", item="Short Address"),
        BatchWrite(cv=7, kind="read", item="Version"),
        BatchWrite(cv=29, kind="read", item="Config"),
    ]
    batch = await q.start(ops)
    await asyncio.wait_for(batch._task, timeout=1.0)
    assert batch.state is BatchState.DONE
    assert [op.value for op in batch.writes] == [3, 151, 34]
    assert [op.result for op in batch.writes] == [WriteResult.DONE] * 3


async def test_read_op_without_read_callback_fails_that_op() -> None:
    """A queue configured for writes only must fail read ops cleanly
    (not crash the whole batch) so callers get a specific error rather
    than a mysterious NoneType."""
    async def write_cb(*a, **kw):   # noqa: ANN002, ANN003, ANN202
        return 0

    q = ProgrammingQueue("L1", write_cb)   # no read_callback
    batch = await q.start([BatchWrite(cv=1, kind="read", item="X")])
    await asyncio.wait_for(batch._task, timeout=1.0)
    # Single-op batch, all failed -> terminal ERROR.
    assert batch.state is BatchState.ERROR
    assert batch.writes[0].result is WriteResult.ERROR
    assert "read" in batch.writes[0].error.lower()


async def test_mixed_read_and_write_ops_route_correctly() -> None:
    """A single batch can mix reads and writes; each op picks the right
    callback based on ``kind``. Serialisation is preserved."""
    written: list[tuple[int, int]] = []
    async def write_cb(cv, value, item, extra):   # noqa: ANN001, ANN202
        written.append((cv, value))
        return value

    async def read_cb(cv, item, extra):   # noqa: ANN001, ANN202
        return 99   # sentinel

    q = ProgrammingQueue("L1", write_cb, read_callback=read_cb)
    ops = [
        BatchWrite(cv=1, value=3, kind="write", item="Short Address"),
        BatchWrite(cv=7, kind="read", item="Version"),
        BatchWrite(cv=29, value=6, kind="write", item="Config"),
    ]
    batch = await q.start(ops)
    await asyncio.wait_for(batch._task, timeout=1.0)
    assert batch.state is BatchState.DONE
    assert written == [(1, 3), (29, 6)]
    assert batch.writes[1].value == 99   # read result landed


async def test_cancel_when_no_batch_is_a_noop() -> None:
    q = ProgrammingQueue(
        "L1", lambda *a, **kw: _noop(),  # type: ignore[arg-type,return-value]
    )
    assert q.cancel() is None
