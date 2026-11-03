"""``DccexTrafficController`` -- async read loop + Signal fan-out.

DCC-EX is "fire-and-respond": clients send commands, the command station
responds asynchronously with state-change messages. Multiple commands may
be in flight; we don't track request/response pairing here -- the driver
just emits everything that comes back on the :attr:`inbound` Signal and
lets the per-bean handlers correlate.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pmri_core.events import Signal

from pmri_dccex.codec import DccexMessage, decode, encode, read_message
from pmri_dccex.transport.port import BytePort

_log = logging.getLogger(__name__)


class DccexTrafficController:
    def __init__(self, port: BytePort) -> None:
        self._port = port
        self._send_lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False
        self.inbound: Signal[DccexMessage] = Signal()
        # Timestamp of the most recent inbound message. Read by
        # :class:`DccexConnection`'s reachability monitor. DCC-EX
        # doesn't chatter without solicitation, so the connection also
        # runs a periodic ``<s>`` (status) ping to guarantee traffic.
        self.last_inbound_at: float | None = None

    async def start(self) -> None:
        if self._read_task is not None:
            return
        self._read_task = asyncio.create_task(self._read_loop(), name="dccex-read")

    async def aclose(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            import contextlib  # noqa: PLC0415
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        await self._port.aclose()

    async def send(self, msg: DccexMessage) -> None:
        async with self._send_lock:
            await self._port.write(encode(msg))

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    text = await read_message(self._port.read_byte)
                except asyncio.CancelledError:
                    return
                except Exception as e:  # noqa: BLE001
                    _log.warning("DCC-EX read error: %s", e)
                    continue
                try:
                    msg = decode(text)
                except ValueError as e:
                    _log.warning("DCC-EX bad message %r: %s", text, e)
                    continue
                self.last_inbound_at = time.monotonic()
                await self.inbound.emit(msg)
        except asyncio.CancelledError:
            return
