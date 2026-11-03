"""Addressed OpenLCB messages: encoding + multi-frame reassembly.

Global OpenLCB frames (PCER, Verify Node, etc.) fit in one CAN frame
because their payload is small. Addressed messages target a specific
peer via a 12-bit destination alias and can carry payloads well past
the 8-byte CAN limit -- SNIP replies routinely run 30+ bytes; CDI
reads are capped at 64 bytes; datagram MTIs get their own separate
frame types entirely. Fragmentation is defined in
S-9.7.0.1 sec.3.3.4.

Wire layout of a single frame carrying an addressed message
(after the CAN header, which uses ``FT_MTI`` with the 12-bit MTI in
the variable field)::

    byte 0  bits 7..6  = 2-bit multi-frame flag
            bits 5..4  = reserved (0)
            bits 3..0  = destination alias bits 11..8
    byte 1             = destination alias bits 7..0
    bytes 2..7         = up to 6 bytes of payload

Multi-frame flag values:

* ``0b00`` -- only frame (whole message fits in one CAN frame)
* ``0b01`` -- first frame
* ``0b10`` -- middle frame
* ``0b11`` -- last frame

An MTI is "addressed" iff bit 3 of the MTI value is set -- global
messages have it clear. Cross-checked against S-9.7.4.5 (SNIP MTIs
0x0DE8/0x0A08 both have bit 3 = 1) and S-9.7.0.4 (Verify Node ID
addressed variant has it set too).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
    _pack_header,
)

if TYPE_CHECKING:
    from pmri_core.events import Signal

# The 6 payload bytes per addressed CAN frame (the first 2 bytes are
# the flag+dest-alias prefix).
ADDRESSED_PAYLOAD_PER_FRAME = 6

# Multi-frame flag values, one per position in the CAN sequence.
FLAG_ONLY = 0b00
FLAG_FIRST = 0b01
FLAG_MIDDLE = 0b10
FLAG_LAST = 0b11


def is_addressed_mti(mti: int) -> bool:
    """True iff *mti* denotes an addressed message. Bit 3 of the
    12-bit MTI is the address-type flag (0=global, 1=addressed)
    per S-9.7.0.4."""
    return bool(mti & 0x008)


def _prefix(flag: int, dest_alias: int) -> bytes:
    """Encode the 2-byte flag+dest-alias prefix that every addressed
    CAN frame starts with."""
    if not 0 <= dest_alias <= 0xFFF:
        raise ValueError(
            f"destination alias must be 12-bit, got {dest_alias:#x}",
        )
    if flag not in (FLAG_ONLY, FLAG_FIRST, FLAG_MIDDLE, FLAG_LAST):
        raise ValueError(f"invalid multi-frame flag: {flag:#x}")
    prefix = (flag << 14) | (dest_alias & 0xFFF)
    return prefix.to_bytes(2, "big")


def _parse_prefix(data: bytes) -> tuple[int, int]:
    """Reverse of :func:`_prefix` -- return ``(flag, dest_alias)``."""
    if len(data) < 2:
        raise ValueError(
            f"addressed frame needs 2-byte prefix, got {len(data)} bytes",
        )
    prefix = int.from_bytes(data[:2], "big")
    flag = (prefix >> 14) & 0x3
    dest_alias = prefix & 0xFFF
    return flag, dest_alias


def encode_addressed_frames(
    mti: int, source_alias: int, dest_alias: int, body: bytes,
) -> list[OlcbFrame]:
    """Fragment *body* into CAN frames carrying an addressed MTI.

    Every returned frame uses the same header (frame type ``FT_MTI``,
    variable field = *mti*, source alias = *source_alias*). The 2-byte
    prefix on each frame's data carries the multi-frame flag +
    *dest_alias*. Payload is split into 6-byte chunks.

    An empty *body* still emits one frame with the ONLY flag -- that's
    how SNIP requests (no payload beyond the dest alias) travel.
    """
    if not is_addressed_mti(mti):
        raise ValueError(
            f"MTI {mti:#05x} is not an addressed MTI (bit 3 must be set)",
        )
    header = _pack_header(FT_MTI, mti, source_alias)
    if len(body) <= ADDRESSED_PAYLOAD_PER_FRAME:
        return [OlcbFrame(
            header=header, data=_prefix(FLAG_ONLY, dest_alias) + body,
        )]
    frames: list[OlcbFrame] = []
    chunks = [
        body[i:i + ADDRESSED_PAYLOAD_PER_FRAME]
        for i in range(0, len(body), ADDRESSED_PAYLOAD_PER_FRAME)
    ]
    last_i = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        if i == 0:
            flag = FLAG_FIRST
        elif i == last_i:
            flag = FLAG_LAST
        else:
            flag = FLAG_MIDDLE
        frames.append(OlcbFrame(
            header=header, data=_prefix(flag, dest_alias) + chunk,
        ))
    return frames


@dataclass(frozen=True, slots=True)
class AddressedMessage:
    """A fully-assembled addressed OpenLCB message."""

    mti: int
    source_alias: int
    dest_alias: int
    body: bytes


class AddressedReassembler:
    """Assemble multi-frame addressed messages back into complete
    :class:`AddressedMessage` values.

    Keyed by ``(mti, source_alias)`` -- a peer can be mid-flight on
    at most one addressed MTI at a time per S-9.7.0.1 sec.3.3.4, so this
    doesn't over-index. If a middle or last frame arrives without a
    preceding first, it's silently discarded (rather than raising) --
    that's the spec's recommendation for lost-frame recovery.

    Attach via :meth:`receive` from the traffic-controller's frame
    signal, or use :meth:`bind` to auto-subscribe.
    """

    def __init__(self) -> None:
        # Per (mti, source_alias) buffer of assembled bytes.
        self._buffers: dict[tuple[int, int], bytearray] = {}
        # Latched destination alias for each in-flight message so the
        # completion event can report it; dest changes mid-message are
        # a spec violation and are ignored.
        self._dest: dict[tuple[int, int], int] = {}

    def receive(self, frame: OlcbFrame) -> AddressedMessage | None:
        """Feed one frame in; return a complete
        :class:`AddressedMessage` when the message finishes, else
        ``None`` for non-addressed frames and mid-message frames."""
        if extract_frame_type(frame.header) != FT_MTI:
            return None
        mti = extract_mti(frame.header)
        if not is_addressed_mti(mti):
            return None
        if len(frame.data) < 2:
            return None
        source_alias = extract_source_alias(frame.header)
        flag, dest_alias = _parse_prefix(frame.data)
        chunk = bytes(frame.data[2:])
        key = (mti, source_alias)
        if flag == FLAG_ONLY:
            # Drop any stale in-flight state for this key -- a peer
            # that abandoned a multi-frame message and starts fresh
            # shouldn't leak the old buffer.
            self._buffers.pop(key, None)
            self._dest.pop(key, None)
            return AddressedMessage(mti, source_alias, dest_alias, chunk)
        if flag == FLAG_FIRST:
            self._buffers[key] = bytearray(chunk)
            self._dest[key] = dest_alias
            return None
        if flag == FLAG_MIDDLE:
            buf = self._buffers.get(key)
            if buf is None:
                return None
            buf.extend(chunk)
            return None
        # FLAG_LAST
        buf = self._buffers.pop(key, None)
        dest = self._dest.pop(key, dest_alias)
        if buf is None:
            return None
        buf.extend(chunk)
        return AddressedMessage(mti, source_alias, dest, bytes(buf))

    def bind(
        self,
        frames_signal: Signal[OlcbFrame],
        on_message: Callable[[AddressedMessage], Awaitable[None]],
    ) -> Callable[[], None]:
        """Auto-subscribe: every complete message on the wire fires
        *on_message*. Returns an unsubscribe callable."""
        async def _adapter(frame: OlcbFrame) -> None:
            msg = self.receive(frame)
            if msg is not None:
                await on_message(msg)
        return frames_signal.connect(_adapter)


@dataclass
class _PendingAddressed:
    """Per-target waiter used by :func:`await_addressed_reply`."""

    future: asyncio.Future[AddressedMessage]
    reassembler: AddressedReassembler = field(
        default_factory=AddressedReassembler,
    )
