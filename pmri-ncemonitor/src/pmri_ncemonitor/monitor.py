"""``CabBusMonitor`` -- passive read loop + Signal fan-out.

Reads Cab Bus messages from the transport, decodes them, and emits each
on the :attr:`inbound` Signal. There is no send path. There is no Lock.
The whole module is shorter than either of the bidirectional drivers
because passivity removes most of the complexity.
"""
from __future__ import annotations

import asyncio
import logging

from pmri_core.events import Signal

from pmri_ncemonitor.codec import (
    BodyOverflowError,
    PeekableReader,
    read_message_peekable,
)
from pmri_ncemonitor.messages import CabBusMessage, decode
from pmri_ncemonitor.transport.port import BytePort

_log = logging.getLogger(__name__)


class CabBusMonitor:
    """Owns one :class:`BytePort` and a background read task.

    Listeners connect to :attr:`inbound` and receive every decoded message
    in the order it appeared on the bus.
    """

    def __init__(self, port: BytePort) -> None:
        self._port = port
        self._reader = PeekableReader(port.read_byte)
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False
        self.inbound: Signal[CabBusMessage] = Signal()

    async def start(self) -> None:
        if self._read_task is not None:
            return
        self._read_task = asyncio.create_task(self._read_loop(), name="cabbus-monitor")

    async def aclose(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            import contextlib  # noqa: PLC0415
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        await self._port.aclose()

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    frame = await read_message_peekable(self._reader)
                except asyncio.CancelledError:
                    return
                except BodyOverflowError as e:
                    # A stuck bus buffered past the cap. Log the
                    # partial for diagnostics and drop it -- the next
                    # read_message call resyncs on the wire.
                    _log.warning(
                        "Cab Bus body overflow (%d bytes buffered): %s",
                        len(e.partial), e.partial.hex(),
                    )
                    continue
                except Exception as e:  # noqa: BLE001
                    _log.warning("Cab Bus read error: %s", e)
                    continue
                try:
                    msg = decode(frame)
                except ValueError as e:
                    _log.warning("Cab Bus bad frame %s: %s", frame.hex(), e)
                    continue
                await self.inbound.emit(msg)
        except asyncio.CancelledError:
            return
