"""Stream Transport (S-9.7.4.9) -- bulk data transfer over OpenLCB.

Values in this module are verified against S-9.7.4.9 rev
2015-02-15 (Stuart Baker). Notable specifics:

* **Message MTIs (common form):**
  ``STREAM_INIT_REQUEST = 0x0CC8``,
  ``STREAM_INIT_REPLY = 0x0868``,
  ``STREAM_PROCEED = 0x0888``,
  ``STREAM_CONTROL = 0x08A8`` (used for Complete + Abort + Ping/Pong).
* **CAN frame type for Stream Data Send = 7** (encodes as
  :data:`~pmri_openlcb.codec.FT_STREAM` in our 4-bit form).
* Stream Data Send frames carry ``[dest_stream_id, ...payload...]``,
  so up to 7 payload bytes per CAN frame.
* Initiate Reply flags: ``0x8000`` = accepted; ``0x1xxx`` = permanent
  reject (``0x1010`` unimplemented, ``0x1020`` source not permitted,
  ``0x1040`` streams not supported); ``0x2xxx`` = temporary reject
  (``0x2040`` out of order, ``0x2020`` buffer unavailable);
  ``0x0001`` OR-flag = logged.

Wire flow (client -> server):

1. Client sends Stream Initiate Request with a proposed max buffer
   size + source stream ID. Server replies with Stream Initiate
   Reply carrying the negotiated buffer size, an assigned dest
   stream ID, and an error code (``0x8000`` = accepted).
2. Client sends stream data frames (CAN frame type 7) each carrying
   ``[dest_stream_id, ...up to 7 payload bytes...]``.
3. After every buffer's worth of data, server sends Stream Proceed
   so the client's next window can go out.
4. Client sends Stream Control with type=Completed when the last
   byte is on the wire.

Scope caveats:

* One outstanding stream per (peer alias) per side. Multi-stream
  demultiplexing per S-9.7.4.9 sec.6 works but isn't the primary use
  case here.
* Ping/Pong (Control types 0x1/0x2) not implemented.
* Optional Stream Content UID prefix is supported.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pmri_openlcb.addressed import (
    AddressedMessage,
    AddressedReassembler,
    encode_addressed_frames,
)
from pmri_openlcb.codec import (
    FT_STREAM,
    OlcbFrame,
    _pack_header,
    extract_frame_type,
    extract_source_alias,
)

if TYPE_CHECKING:
    from pmri_core.events import Signal  # noqa: F401

_log = logging.getLogger(__name__)


# ---- MTIs (verified against S-9.7.4.9 rev 2015-02-15) --------------------

MTI_STREAM_INIT_REQUEST = 0x0CC8
MTI_STREAM_INIT_REPLY = 0x0868
MTI_STREAM_PROCEED = 0x0888
MTI_STREAM_CONTROL = 0x08A8    # used for Complete, Abort, Ping/Pong

# Reply error-code family. ``0x8000`` = accepted; anything else with
# the top nibble in 0x1 (permanent) or 0x2 (temporary) is a reject.
STREAM_REPLY_ACCEPTED = 0x8000
STREAM_REPLY_UNIMPLEMENTED = 0x1010
STREAM_REPLY_SOURCE_NOT_PERMITTED = 0x1020
STREAM_REPLY_STREAMS_NOT_SUPPORTED = 0x1040
STREAM_REPLY_BUFFER_UNAVAILABLE = 0x2020
STREAM_REPLY_OUT_OF_ORDER = 0x2040

# Stream Control type nibble (bits 0..3 of Control body byte 2).
STREAM_CONTROL_TYPE_PING = 0x1
STREAM_CONTROL_TYPE_PONG = 0x2
STREAM_CONTROL_TYPE_COMPLETED = 0x3
STREAM_CONTROL_TYPE_ABORT = 0x4

# Stream Control flag bits (bits 4..7 of Control body byte 2).
STREAM_CONTROL_FLAG_SOURCE_TO_DEST = 0x10   # bit 4 (spec numbers from 0)
STREAM_CONTROL_FLAG_COUNT_VALID = 0x20      # bit 5

# Reserved stream ID (0xFF) per sec.3.
STREAM_ID_RESERVED = 0xFF

# Sentinel for the Initiate Request "no destination stream ID
# suggestion" case (sec.2.4.1 byte 0).
STREAM_ID_NONE_SUGGESTED = 0xFF

# Default per-window buffer size in bytes. Kept modest -- a peer with
# a bigger receive buffer will negotiate up via the Reply.
DEFAULT_STREAM_BUFFER = 256

# Stream Data Send frame layout (sec.3.1): CAN frame type 7 with
# ``[dest_stream_id, ...payload...]`` -- 7 payload bytes per frame.
STREAM_DATA_PER_FRAME = 8 - 1


# ---- exceptions ---------------------------------------------------------


class StreamRejected(Exception):
    """The peer rejected our Stream Initiate Request. ``reason`` is
    the raw 2-byte error code so callers can interpret it against
    S-9.7.4.9 sec.2.4.2. Log-flag bit (``0x0001``) is preserved."""

    def __init__(self, reason: int) -> None:
        self.reason = reason
        super().__init__(f"stream rejected (reason {reason:#06x})")


# ---- helpers ------------------------------------------------------------


def _pack_stream_frame(
    source_alias: int, dest_alias: int,
    dest_stream_id: int, chunk: bytes,
) -> OlcbFrame:
    """Build one CAN frame-type-7 (Stream Data Send) frame. The
    variable field carries the dest alias per sec.3.1; the payload is
    ``[dest_stream_id, ...data...]`` -- up to 7 data bytes."""
    header = _pack_header(FT_STREAM, dest_alias & 0xFFF, source_alias)
    return OlcbFrame(
        header=header,
        data=bytes([dest_stream_id & 0xFF]) + chunk,
    )


def _split_stream_data(data: bytes) -> list[bytes]:
    return [
        data[i:i + STREAM_DATA_PER_FRAME]
        for i in range(0, len(data), STREAM_DATA_PER_FRAME)
    ]


# ---- typing shim --------------------------------------------------------


class _StreamTcLike:
    """Structural type for the traffic controller bits we use:

    * ``source_alias: int``
    * ``frames: Signal[OlcbFrame]``
    * ``send_frame(frame) -> None`` -- coroutine
    """


# ---- client (send-side) -------------------------------------------------


@dataclass
class _PendingReply:
    dest_alias: int
    source_stream_id: int
    fut: "asyncio.Future[_ReplyPayload]"


@dataclass
class _ReplyPayload:
    dest_stream_id: int
    negotiated_buffer: int
    flags: int


@dataclass
class _PendingProceed:
    dest_alias: int
    fut: "asyncio.Future[None]"


class StreamClient:
    """Send arbitrary-length data to a peer as a single stream.

    Instantiate once per traffic controller; call :meth:`send` for
    each stream. Concurrent streams to *different* peer aliases are
    fine -- pending state is keyed by peer alias. Two overlapping
    streams to the *same* peer alias are rejected.
    """

    def __init__(self, tc: _StreamTcLike) -> None:
        self._tc = tc
        self._pending_replies: dict[int, _PendingReply] = {}
        self._pending_proceeds: dict[int, _PendingProceed] = {}
        # Source stream IDs -- cycle through [1..254], skipping 0xFF
        # (reserved per sec.3).
        self._next_stream_id = 1
        self._reassembler = AddressedReassembler()
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        for p in self._pending_replies.values():
            if not p.fut.done():
                p.fut.set_exception(RuntimeError("stream client closed"))
        for p in self._pending_proceeds.values():
            if not p.fut.done():
                p.fut.set_exception(RuntimeError("stream client closed"))
        self._pending_replies.clear()
        self._pending_proceeds.clear()

    async def send(
        self,
        dest_alias: int,
        data: bytes,
        *,
        buffer_size: int = DEFAULT_STREAM_BUFFER,
        content_type: bytes | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Send *data* to *dest_alias* as a single stream.

        Runs the full flow: Initiate Request -> wait for Reply, then
        chunk into stream frames grouped into buffer-sized windows,
        awaiting a Proceed after each window, then send Stream
        Control(type=Completed).

        *content_type* -- optional 6-byte Stream Content UID per
        sec.2.4.1. When set, it's appended to the Initiate Request so
        the destination knows what format to expect.

        Raises :class:`StreamRejected` if the peer refuses,
        :class:`asyncio.TimeoutError` on any step exceeding
        *timeout*."""
        if dest_alias in self._pending_replies:
            raise RuntimeError(
                f"stream to {dest_alias:#05x} already in flight",
            )
        if content_type is not None and len(content_type) != 6:
            raise ValueError(
                f"content_type must be exactly 6 bytes, got {len(content_type)}",
            )
        source_id = self._alloc_stream_id()

        loop = asyncio.get_event_loop()
        reply_fut: "asyncio.Future[_ReplyPayload]" = loop.create_future()
        self._pending_replies[dest_alias] = _PendingReply(
            dest_alias=dest_alias,
            source_stream_id=source_id,
            fut=reply_fut,
        )
        try:
            # ---- 1. Initiate Request (sec.2.4.1) -----------------------
            # Byte 0: suggested dest stream id (0xFF = no suggestion)
            # Byte 1: source stream id
            # Bytes 2-3: proposed max buffer size (big-endian)
            # Byte 4: flags (bit 0 deprecated, 0)
            # Byte 5: additional flags (reserved, 0)
            # Bytes 6-11: optional 6-byte Stream Content UID
            init_body = bytearray()
            init_body.append(STREAM_ID_NONE_SUGGESTED)
            init_body.append(source_id)
            init_body.extend(buffer_size.to_bytes(2, "big"))
            init_body.extend(b"\x00\x00")   # flags + additional flags
            if content_type is not None:
                init_body.extend(content_type)
            for frame in encode_addressed_frames(
                MTI_STREAM_INIT_REQUEST,
                self._tc.source_alias, dest_alias, bytes(init_body),
            ):
                await self._tc.send_frame(frame)
            reply = await asyncio.wait_for(reply_fut, timeout=timeout)
        finally:
            self._pending_replies.pop(dest_alias, None)

        if reply.flags != STREAM_REPLY_ACCEPTED and not (
            reply.flags & STREAM_REPLY_ACCEPTED
        ):
            # Accept requires exactly bit 15 set (0x8000). Any error
            # family (0x1xxx / 0x2xxx) or an unexpected zero is a
            # reject.
            raise StreamRejected(reason=reply.flags)

        window = reply.negotiated_buffer
        if window <= 0:
            # Non-zero negotiated buffer is required per sec.2.4.2.
            raise StreamRejected(reason=reply.flags)

        # ---- 2. Push data in windows, await Proceed between ---------
        cursor = 0
        total_sent = 0
        while cursor < len(data):
            chunk_window = data[cursor:cursor + window]
            proceed_fut: "asyncio.Future[None]" = loop.create_future()
            self._pending_proceeds[dest_alias] = _PendingProceed(
                dest_alias=dest_alias, fut=proceed_fut,
            )
            try:
                for piece in _split_stream_data(chunk_window):
                    await self._tc.send_frame(_pack_stream_frame(
                        self._tc.source_alias, dest_alias,
                        reply.dest_stream_id, piece,
                    ))
                    total_sent += len(piece)
                # Only await Proceed if more data remains after this
                # window -- final window closes with Complete instead.
                if cursor + window < len(data):
                    await asyncio.wait_for(
                        proceed_fut, timeout=timeout,
                    )
            finally:
                self._pending_proceeds.pop(dest_alias, None)
            cursor += window

        # ---- 3. Stream Control: type=Completed (sec.2.4.5) -------------
        # Byte 0: dest stream id
        # Byte 1: source stream id
        # Byte 2: bit 4 (source->dest) | bit 5 (count valid) | type=3
        # Bytes 3-5: total bytes sent (mod 2^24)
        control_byte = (
            STREAM_CONTROL_FLAG_SOURCE_TO_DEST
            | STREAM_CONTROL_FLAG_COUNT_VALID
            | STREAM_CONTROL_TYPE_COMPLETED
        )
        complete_body = (
            bytes([
                reply.dest_stream_id,
                source_id,
                control_byte,
            ])
            + (total_sent & 0xFFFFFF).to_bytes(3, "big")
        )
        for frame in encode_addressed_frames(
            MTI_STREAM_CONTROL,
            self._tc.source_alias, dest_alias, complete_body,
        ):
            await self._tc.send_frame(frame)

    def _alloc_stream_id(self) -> int:
        """Return a source stream ID in [1..254], skipping 0xFF."""
        sid = self._next_stream_id
        self._next_stream_id += 1
        if self._next_stream_id >= STREAM_ID_RESERVED:
            self._next_stream_id = 1
        return sid

    async def _on_frame(self, frame: OlcbFrame) -> None:
        msg = self._reassembler.receive(frame)
        if msg is None:
            return
        if msg.mti == MTI_STREAM_INIT_REPLY:
            self._handle_init_reply(msg)
            return
        if msg.mti == MTI_STREAM_PROCEED:
            self._handle_proceed(msg)

    def _handle_init_reply(self, msg: AddressedMessage) -> None:
        # Reply body (sec.2.4.2):
        # Byte 0: dest stream id
        # Byte 1: source stream id (echoed)
        # Bytes 2-3: max buffer size (0 if rejected)
        # Bytes 4-5: error code (0x8000 = accepted)
        peer = msg.source_alias
        pending = self._pending_replies.get(peer)
        if pending is None or pending.fut.done():
            return
        if len(msg.body) < 6:
            pending.fut.set_exception(
                ValueError(f"stream init reply too short: {msg.body!r}"),
            )
            return
        dest_id = msg.body[0]
        source_id = msg.body[1]
        negotiated = int.from_bytes(msg.body[2:4], "big")
        flags = int.from_bytes(msg.body[4:6], "big")
        if source_id != pending.source_stream_id:
            # Reply for a different stream; ignore.
            return
        pending.fut.set_result(_ReplyPayload(
            dest_stream_id=dest_id,
            negotiated_buffer=negotiated,
            flags=flags,
        ))

    def _handle_proceed(self, msg: AddressedMessage) -> None:
        peer = msg.source_alias
        pending = self._pending_proceeds.get(peer)
        if pending is None or pending.fut.done():
            return
        pending.fut.set_result(None)


# ---- responder (receive-side) -------------------------------------------


@dataclass
class _InboundStream:
    source_alias: int
    source_stream_id: int
    dest_stream_id: int
    buffer_size: int
    accumulated: bytearray
    window_used: int


class StreamResponder:
    """Answer incoming Stream Initiate Requests and reassemble
    stream data.

    Fires ``on_stream(source_alias, bytes)`` when a stream finishes
    (Stream Control type=Completed observed). The Proceed cadence is
    automatic -- every ``buffer_size`` bytes accepted, we send Proceed
    so the sender's window opens.
    """

    def __init__(
        self,
        tc: _StreamTcLike,
        *,
        buffer_size: int = DEFAULT_STREAM_BUFFER,
        on_stream: (
            Callable[[int, bytes], None] | None
        ) = None,
    ) -> None:
        self._tc = tc
        self._buffer_size = buffer_size
        self._on_stream = on_stream
        self._streams: dict[int, _InboundStream] = {}
        # Destination stream IDs, cycling through [1..254].
        self._next_dest_id = 1
        self._reassembler = AddressedReassembler()
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        self._streams.clear()

    async def _on_frame(self, frame: OlcbFrame) -> None:
        # Stream Data Send is CAN frame type 7 (0xF in our 4-bit
        # convention). MTI frames go through the reassembler.
        if extract_frame_type(frame.header) == FT_STREAM:
            await self._on_stream_data(frame)
            return
        msg = self._reassembler.receive(frame)
        if msg is None:
            return
        if msg.mti == MTI_STREAM_INIT_REQUEST:
            await self._on_init_request(msg)
            return
        if msg.mti == MTI_STREAM_CONTROL:
            self._on_control(msg)

    async def _on_init_request(self, msg: AddressedMessage) -> None:
        # Request body (sec.2.4.1) -- bytes 0..5 minimum.
        if len(msg.body) < 6:
            return
        # Byte 0 = suggested dest id (we may ignore).
        source_id = msg.body[1]
        proposed = int.from_bytes(msg.body[2:4], "big")
        # Clamp to our capacity -- the spec explicitly permits us to
        # reduce the buffer size (sec.2.6, "Gateways may reduce...").
        negotiated = min(proposed, self._buffer_size) or self._buffer_size
        dest_id = self._alloc_dest_id()
        self._streams[msg.source_alias] = _InboundStream(
            source_alias=msg.source_alias,
            source_stream_id=source_id,
            dest_stream_id=dest_id,
            buffer_size=negotiated,
            accumulated=bytearray(),
            window_used=0,
        )
        # Send the accept reply (sec.2.4.2).
        reply_body = (
            bytes([dest_id, source_id])
            + negotiated.to_bytes(2, "big")
            + STREAM_REPLY_ACCEPTED.to_bytes(2, "big")
        )
        for f in encode_addressed_frames(
            MTI_STREAM_INIT_REPLY,
            self._tc.source_alias, msg.source_alias, reply_body,
        ):
            await self._tc.send_frame(f)

    def _alloc_dest_id(self) -> int:
        did = self._next_dest_id
        self._next_dest_id += 1
        if self._next_dest_id >= STREAM_ID_RESERVED:
            self._next_dest_id = 1
        return did

    async def _on_stream_data(self, frame: OlcbFrame) -> None:
        source = extract_source_alias(frame.header)
        stream = self._streams.get(source)
        if stream is None or len(frame.data) < 1:
            return
        # Verify dest stream id matches the stream we allocated for
        # this peer -- a mismatch could mean a stale gateway frame.
        if frame.data[0] != stream.dest_stream_id:
            return
        payload = bytes(frame.data[1:])
        stream.accumulated.extend(payload)
        stream.window_used += len(payload)
        if stream.window_used >= stream.buffer_size:
            stream.window_used = 0
            await self._send_proceed(stream)

    async def _send_proceed(self, stream: _InboundStream) -> None:
        # Proceed body (sec.2.4.4): [dest_stream_id, source_stream_id].
        body = bytes([stream.dest_stream_id, stream.source_stream_id])
        for f in encode_addressed_frames(
            MTI_STREAM_PROCEED,
            self._tc.source_alias, stream.source_alias, body,
        ):
            await self._tc.send_frame(f)

    def _on_control(self, msg: AddressedMessage) -> None:
        # Control body (sec.2.4.5): dest_id, source_id, type/flags byte,
        # optional 3-byte count.
        if len(msg.body) < 3:
            return
        stream = self._streams.get(msg.source_alias)
        if stream is None:
            return
        control_byte = msg.body[2]
        type_nibble = control_byte & 0x0F
        if type_nibble == STREAM_CONTROL_TYPE_COMPLETED:
            self._streams.pop(msg.source_alias, None)
            if self._on_stream is not None:
                try:
                    self._on_stream(
                        stream.source_alias,
                        bytes(stream.accumulated),
                    )
                except Exception:
                    _log.exception("stream completion callback failed")
        elif type_nibble == STREAM_CONTROL_TYPE_ABORT:
            # Drop state; caller sees nothing further about this
            # stream. A future revision could surface an on_abort
            # callback.
            self._streams.pop(msg.source_alias, None)
