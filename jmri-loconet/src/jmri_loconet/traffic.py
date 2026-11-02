"""``LocoNetTrafficController`` -- async read loop + send queue fan-out.

Owns one :class:`LocoNetPort` and a background task that reads complete
frames, decodes them, and emits the typed message on the ``inbound`` Signal.
The :meth:`send` method serializes a typed message to bytes, attaches the
checksum, and writes to the port.

LocoNet runs at ~16.6 kbaud, so back-to-back writes need a small interframe
gap; this is configurable via ``min_send_interval``.
"""
from __future__ import annotations

import asyncio
import logging
import time

from jmri_core.events import Signal

from jmri_loconet.codec import frame_length, read_frame
from jmri_loconet.messages import LocoNetMessage, decode, encode
from jmri_loconet.transport.port import LocoNetPort

_log = logging.getLogger(__name__)


class LocoNetTrafficController:
    """The protocol-level conduit between transport bytes and typed messages.

    Listeners connect to :attr:`inbound`; senders call :meth:`send`. The
    read loop runs as a background asyncio task started by :meth:`start`
    and cancelled by :meth:`aclose`.
    """

    def __init__(
        self,
        port: LocoNetPort,
        *,
        min_send_interval: float = 0.005,
    ) -> None:
        self._port = port
        self._min_send_interval = min_send_interval
        self._send_lock = asyncio.Lock()
        self._last_send: float = 0.0
        self._read_task: asyncio.Task[None] | None = None
        self.inbound: Signal[LocoNetMessage] = Signal()
        self._closed = False
        # Timestamp (``time.monotonic()``) of the most recent inbound
        # frame. Read by :class:`LocoNetConnection`'s reachability
        # monitor to flip CS-power state. LocoNet is chatty -- the CS
        # emits ``OPC_IDLE`` and other frames periodically -- so a
        # multi-second silence is a strong signal the bus is dark.
        self.last_inbound_at: float | None = None

    async def start(self) -> None:
        if self._read_task is not None:
            return
        self._read_task = asyncio.create_task(self._read_loop(), name="loconet-read")

    async def aclose(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            import contextlib  # noqa: PLC0415
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        await self._port.aclose()

    async def send(self, msg: LocoNetMessage) -> None:
        """Encode and write *msg* to the port, with interframe pacing."""
        frame = encode(msg)
        async with self._send_lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_send_interval - (now - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            await self._port.write_frame(frame)
            self._last_send = asyncio.get_event_loop().time()

    # ----- internals --------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    frame = await read_frame(
                        self._port.read_byte, self._port.read_exact
                    )
                except asyncio.CancelledError:
                    return
                except Exception as e:  # noqa: BLE001
                    _log.warning("LocoNet read error: %s", e)
                    continue

                expected = frame_length(frame)
                if expected is None or expected != len(frame):
                    _log.warning(
                        "LocoNet frame length mismatch: got %d bytes for %s",
                        len(frame), frame.hex()
                    )
                    continue

                try:
                    msg = decode(frame)
                except ValueError as e:
                    _log.warning("LocoNet bad frame %s: %s", frame.hex(), e)
                    continue

                # Any well-formed inbound frame counts as evidence the
                # CS is alive -- even a bare OPC_IDLE. The reachability
                # monitor watches this timestamp.
                self.last_inbound_at = time.monotonic()
                await self.inbound.emit(msg)
        except asyncio.CancelledError:
            return
