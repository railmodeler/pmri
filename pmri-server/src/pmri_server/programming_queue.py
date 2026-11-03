"""``ProgrammingQueue`` -- serialized per-loco CV writes with progress + cancel.

DCC programming is slow: direct-mode writes on the service track take
several seconds *per CV*. A full loco can have dozens of CVs the user
wants to write. Firing them all off in parallel would overwhelm the
programming track (which serializes at the CS anyway) and race the
LocoCvStore atomic writes.

This module owns a per-loco queue. One batch runs at a time. A cancel
flag causes the queue to stop after the *in-flight* write completes --
matching the user's spec ("cancel after the completion of the in-flight
operation") because DCC hardware can't safely abort a write already on
the wire.

Client flow:

    POST /programmer/loco/<id>/write-batch  -> 202 + batch id
    GET  /programmer/loco/<id>/batch/status -> progress
    POST /programmer/loco/<id>/batch/cancel -> stop after current write
    POST /programmer/loco/<id>/batch/clear  -> clear terminal-state batch
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_log = logging.getLogger(__name__)


class BatchState(StrEnum):
    """High-level lifecycle for a queued batch."""
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    DONE        = "done"
    CANCELLED   = "cancelled"
    ERROR       = "error"


class WriteResult(StrEnum):
    """Per-write outcome inside a batch."""
    PENDING   = "pending"
    WRITING   = "writing"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"


@dataclass
class BatchWrite:
    """One CV operation inside a batch.

    * ``kind`` -- ``"write"`` (default) or ``"read"``. Selects which
      callback the queue dispatches to. Reads leave ``value`` as 0 on
      the request side and store the read byte in ``value`` once the
      operation completes so downstream consumers see a uniform
      ``{cv, value, item, result}`` snapshot.
    * ``cv`` -- target CV number
    * ``value`` -- byte value: for writes this is the requested value;
      for reads it starts at 0 and holds the read result on completion.
    * ``item`` -- display label for the progress UI (variable ``item``
      name from the decoder catalog)
    * ``result`` -- updated as the queue processes this op
    * ``error`` -- filled when ``result == ERROR``
    """
    cv: int
    value: int = 0
    item: str = ""
    kind: str = "write"
    result: WriteResult = WriteResult.PENDING
    error: str = ""


# Write callback: returns the value the CS confirmed committed.
WriteCallback = Callable[[int, int, str, dict[str, Any]], Awaitable[int]]

# Read callback: returns the byte read from the decoder.
ReadCallback = Callable[[int, str, dict[str, Any]], Awaitable[int]]


@dataclass
class Batch:
    """One in-flight batch of writes."""

    batch_id: str
    loco_id: str
    writes: list[BatchWrite]
    mode: str = "service"
    address: int | None = None
    long_address: bool = False
    file_name: str | None = None
    state: BatchState = BatchState.PENDING
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None

    @property
    def completed(self) -> int:
        return sum(
            1 for w in self.writes
            if w.result in (WriteResult.DONE, WriteResult.ERROR, WriteResult.CANCELLED)
        )

    @property
    def total(self) -> int:
        return len(self.writes)

    @property
    def in_flight(self) -> BatchWrite | None:
        for w in self.writes:
            if w.result is WriteResult.WRITING:
                return w
        return None

    def is_terminal(self) -> bool:
        return self.state in (
            BatchState.DONE, BatchState.CANCELLED, BatchState.ERROR,
        )

    def request_cancel(self) -> None:
        """Set the cancel flag; the worker stops after the current write."""
        self._cancel.set()

    def to_status(self) -> dict[str, Any]:
        """Serializable snapshot for the polling endpoint."""
        in_flight = self.in_flight
        return {
            "batch_id": self.batch_id,
            "loco_id": self.loco_id,
            "state": self.state.value,
            "completed": self.completed,
            "total": self.total,
            "in_flight": {
                "cv": in_flight.cv, "item": in_flight.item,
                "value": in_flight.value,
            } if in_flight else None,
            "writes": [
                {
                    "cv": w.cv, "value": w.value, "item": w.item,
                    "result": w.result.value, "error": w.error,
                }
                for w in self.writes
            ],
            "error": self.error,
            "elapsed_s": (
                (self.finished_at or time.monotonic()) - self.started_at
            ),
        }


class BatchInFlightError(Exception):
    """Raised when a caller tries to start a new batch while one is running."""

    def __init__(self, loco_id: str, batch_id: str) -> None:
        super().__init__(
            f"loco {loco_id!r} already has batch {batch_id!r} in flight; "
            f"cancel or wait for it to finish first",
        )
        self.loco_id = loco_id
        self.batch_id = batch_id


class ProgrammingQueue:
    """Holds the active :class:`Batch` for one loco.

    A completed batch stays visible on the queue until the caller either
    starts a new batch (which replaces it) or explicitly clears it. That
    lets the UI show the final result without racing the completion
    notification.

    ``read_callback`` is optional; when omitted, only write batches can
    run (a read op in the batch will fail with a NotImplementedError).
    """

    def __init__(
        self, loco_id: str, write_callback: WriteCallback,
        read_callback: ReadCallback | None = None,
    ) -> None:
        self._loco_id = loco_id
        self._write_callback = write_callback
        self._read_callback = read_callback
        self._current: Batch | None = None

    @property
    def loco_id(self) -> str:
        return self._loco_id

    @property
    def current(self) -> Batch | None:
        return self._current

    def is_busy(self) -> bool:
        return (
            self._current is not None
            and self._current.state is BatchState.IN_PROGRESS
        )

    def clear(self) -> None:
        """Drop a terminal-state batch. Raises if a batch is still running."""
        if self._current is not None and not self._current.is_terminal():
            raise BatchInFlightError(self._loco_id, self._current.batch_id)
        self._current = None

    def cancel(self) -> Batch | None:
        """Signal cancel on the current batch. Returns it (or None)."""
        if self._current is None or self._current.is_terminal():
            return self._current
        self._current.request_cancel()
        return self._current

    async def start(
        self,
        writes: list[BatchWrite],
        *,
        mode: str = "service",
        address: int | None = None,
        long_address: bool = False,
        file_name: str | None = None,
    ) -> Batch:
        """Queue *writes* and start the worker.

        Raises :class:`BatchInFlightError` if a batch is already running.
        Callers should either await its completion, cancel it, or clear
        it (if terminal) before starting a new one.
        """
        if self._current is not None and not self._current.is_terminal():
            raise BatchInFlightError(self._loco_id, self._current.batch_id)
        batch = Batch(
            batch_id=uuid.uuid4().hex[:12],
            loco_id=self._loco_id,
            writes=list(writes),
            mode=mode,
            address=address,
            long_address=long_address,
            file_name=file_name,
        )
        self._current = batch
        batch.state = BatchState.IN_PROGRESS
        batch._task = asyncio.create_task(
            self._run(batch), name=f"batch-{self._loco_id}-{batch.batch_id}",
        )
        return batch

    async def _run(self, batch: Batch) -> None:
        """Iterate ops serially, honouring the cancel flag between each."""
        try:
            for op in batch.writes:
                if batch._cancel.is_set():
                    op.result = WriteResult.CANCELLED
                    continue
                op.result = WriteResult.WRITING
                extra: dict[str, Any] = {
                    "mode": batch.mode,
                    "file_name": batch.file_name,
                }
                if batch.address is not None:
                    extra["address"] = batch.address
                    extra["long_address"] = batch.long_address
                try:
                    if op.kind == "read":
                        if self._read_callback is None:
                            raise NotImplementedError(
                                "read op requested but no read_callback configured",
                            )
                        value = await self._read_callback(
                            op.cv, op.item, extra,
                        )
                        # Reads land the byte back into ``value`` so
                        # the status snapshot's shape stays uniform
                        # with writes.
                        op.value = int(value)
                    else:
                        await self._write_callback(
                            op.cv, op.value, op.item, extra,
                        )
                except Exception as e:  # noqa: BLE001
                    op.result = WriteResult.ERROR
                    op.error = str(e)
                    _log.warning(
                        "batch %s: CV %d %s failed: %s",
                        batch.batch_id, op.cv, op.kind, e,
                    )
                    continue
                op.result = WriteResult.DONE
            # Terminal state: cancelled if flag was set at any point,
            # error if all failed, otherwise done.
            any_pending = any(w.result is WriteResult.PENDING for w in batch.writes)
            any_cancelled = any(w.result is WriteResult.CANCELLED for w in batch.writes)
            all_errored = batch.writes and all(
                w.result is WriteResult.ERROR for w in batch.writes
            )
            if batch._cancel.is_set() or any_cancelled or any_pending:
                batch.state = BatchState.CANCELLED
            elif all_errored:
                batch.state = BatchState.ERROR
                batch.error = "all writes failed"
            else:
                batch.state = BatchState.DONE
        except asyncio.CancelledError:
            batch.state = BatchState.CANCELLED
            raise
        except Exception as e:  # noqa: BLE001
            batch.state = BatchState.ERROR
            batch.error = str(e)
            _log.exception("batch %s crashed", batch.batch_id)
        finally:
            batch.finished_at = time.monotonic()


def get_or_create_queue(
    ctx: Any, loco_id: str, write_callback: WriteCallback,
    read_callback: ReadCallback | None = None,
) -> ProgrammingQueue:
    """Fetch the queue for *loco_id* off *ctx*, creating on first use.

    The queue registry lives on ``ctx._programming_queues`` (dict). One
    queue per loco keeps batches scoped so cancelling loco A doesn't
    stop loco B's operation. When an existing queue is returned and a
    non-None *read_callback* is supplied, the queue's read callback is
    updated in place -- lets a route that didn't set up reads on first
    use enable them later without swapping the queue instance.
    """
    queues: dict[str, ProgrammingQueue] = getattr(ctx, "_programming_queues", None)
    if queues is None:
        queues = {}
        ctx._programming_queues = queues  # type: ignore[attr-defined]
    if loco_id not in queues:
        queues[loco_id] = ProgrammingQueue(
            loco_id, write_callback, read_callback=read_callback,
        )
    elif read_callback is not None and queues[loco_id]._read_callback is None:
        queues[loco_id]._read_callback = read_callback
    return queues[loco_id]
