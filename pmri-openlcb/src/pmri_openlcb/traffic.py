"""``OlcbTrafficController`` -- read GridConnect frames, emit decoded messages.

OpenLCB is fully async + peer-to-peer; there is no master-slave shape.
Every node both produces and consumes events. The traffic controller
just reads the byte stream, decodes frames, and emits an
:class:`~pmri_openlcb.events.EventReport` (for PCER frames) or the raw
:class:`~pmri_openlcb.codec.OlcbFrame` (for everything else).
"""
from __future__ import annotations

import asyncio
import logging

from pmri_core.events import Signal

from pmri_openlcb.codec import (
    OlcbFrame,
    decode_frame,
    decode_pcer_if_pcer,
    encode_frame,
    encode_pcer,
    make_pcer_header,
    read_frame,
)
from pmri_openlcb.events import EventId, EventReport
from pmri_openlcb.transport.port import BytePort

_log = logging.getLogger(__name__)


class OlcbTrafficController:
    def __init__(
        self, port: BytePort, *, source_alias: int | None = None,
    ) -> None:
        """*source_alias* is left ``None`` when the caller intends to
        run the alias-claim handshake and set it via
        :attr:`source_alias` afterward. Callers that skip the
        handshake (loopback tests) can pass a fixed alias directly."""
        self._port = port
        self._source_alias = source_alias
        self._send_lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False
        self.events: Signal[EventReport] = Signal()
        self.frames: Signal[OlcbFrame] = Signal()
        # Separate signal for frames WE sent -- kept apart from the
        # inbound ``frames`` signal so protocol subscribers don't
        # self-confuse (an EventRoster listening to both would see
        # our own P/C Identified as if a peer sent them).
        self.sent_frames: Signal[OlcbFrame] = Signal()

    @property
    def source_alias(self) -> int:
        if self._source_alias is None:
            raise RuntimeError(
                "OlcbTrafficController has no source alias yet -- call "
                "claim_alias() first or construct with an explicit alias"
            )
        return self._source_alias

    @source_alias.setter
    def source_alias(self, value: int) -> None:
        if not 0 < value <= 0xFFF:
            raise ValueError(
                f"source alias must be 12-bit and non-zero, got {value:#x}",
            )
        self._source_alias = value

    async def start(self) -> None:
        if self._read_task is not None:
            return
        self._read_task = asyncio.create_task(self._read_loop(), name="olcb-read")

    async def aclose(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            import contextlib  # noqa: PLC0415
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        await self._port.aclose()

    async def produce_event(self, event: EventId) -> None:
        """Emit a PCER for *event* with our source alias. Errors out
        loudly if the alias claim hasn't completed -- the property
        getter raises rather than emitting a frame with alias 0."""
        alias = self.source_alias
        async with self._send_lock:
            await self._port.write(encode_pcer(event, alias))
        # Announce on sent_frames so the monitor logs our own PCER.
        await self.sent_frames.emit(OlcbFrame(
            header=make_pcer_header(alias), data=event.bytes,
        ))

    async def send_frame(self, frame: OlcbFrame) -> None:
        """Send a raw frame (escape hatch for unsupported MTIs).

        Also emits ``sent_frames`` (separate from ``frames``, which
        stays inbound-only so subscribers like EventRoster and
        NodeIdLookup can't confuse themselves with our own AMR /
        Verified NID / P/C Identified messages). Only the monitor
        currently observes both -- it wants a complete log.
        """
        async with self._send_lock:
            await self._port.write(encode_frame(frame))
        await self.sent_frames.emit(frame)

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    text = await read_frame(self._port.read_byte)
                except asyncio.CancelledError:
                    return
                except Exception as e:  # noqa: BLE001
                    _log.warning("OpenLCB read error: %s", e)
                    continue
                try:
                    frame = decode_frame(text)
                except ValueError as e:
                    _log.warning("OpenLCB bad frame %r: %s", text, e)
                    continue
                await self.frames.emit(frame)
                pcer = decode_pcer_if_pcer(frame)
                if pcer is not None:
                    await self.events.emit(pcer)
        except asyncio.CancelledError:
            return
