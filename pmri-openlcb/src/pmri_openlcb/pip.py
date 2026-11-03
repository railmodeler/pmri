"""Protocol Identification Protocol (PIP) -- S-9.7.4.2.

Every OpenLCB node publishes which of the higher-level protocols
(datagram, memory config, SNIP, CDI, event exchange, traction, ...)
it supports so peers can adapt without probing each protocol in turn
and interpreting timeouts.

Wire protocol:

* **PIP Inquiry** (MTI ``0x0828``) -- addressed message; empty
  payload beyond the standard 2-byte dest-alias prefix.
* **PIP Reply** (MTI ``0x0668``) -- addressed message. Payload is a
  24-bit big-endian bit-mask of supported protocols (some nodes
  extend to 32 or 48 bits; older nodes may truncate to 16 bits --
  we tolerate both).

Bit assignments per S-9.7.4.2 sec.5, byte 0 -> byte 1 (bit 7 = MSB
inside each byte):

byte 0 -- core protocol group:
    0x80 Simple Protocol Subset          0x08 Reservation
    0x40 Datagram                        0x04 Event Exchange
    0x20 Stream                          0x02 Identification (PIP itself)
    0x10 Memory Configuration            0x01 Learn Configuration

byte 1 -- service protocol group:
    0x80 Remote Button                   0x08 CDI
    0x40 Abbreviated Default CDI         0x04 Traction Control
    0x20 Display                         0x02 FDI (Function Desc Info)
    0x10 SNIP                            0x01 DCC Command Station

Byte 2+ is reserved for firmware upgrade / vendor extensions we
don't advertise. We emit 3 bytes so peers reading a 16-bit slice
still get bytes 0..1 intact and the extra zero byte is spec-compliant.

Cross-referenced against openlcb-python's ``PIP`` bit constants and
JMRI Java's ``org.openlcb.ProtocolIdentification``.
"""
from __future__ import annotations

import asyncio
import logging
from enum import IntFlag
from typing import TYPE_CHECKING

from pmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from pmri_openlcb.codec import OlcbFrame

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# ---- MTIs ---------------------------------------------------------------

MTI_PIP_INQUIRY = 0x0828
MTI_PIP_REPLY = 0x0668

# Default probe timeout. Real nodes answer in ms on a healthy bus.
PIP_TIMEOUT = 2.0


class ProtocolFlag(IntFlag):
    """Bit flags for the PIP reply mask.

    Values are the mask position in the 24-bit big-endian encoding
    (so ``ProtocolFlag.DATAGRAM.value == 0x400000`` = bit 6 of the
    highest byte). Combine with ``|`` to build a supported set, test
    with ``in`` for membership, and encode with :func:`encode_flags`.
    """

    # Byte 0 -- core protocol group.
    SIMPLE = 0x800000
    DATAGRAM = 0x400000
    STREAM = 0x200000
    MEMORY_CONFIG = 0x100000
    RESERVATION = 0x080000
    EVENT_EXCHANGE = 0x040000
    IDENTIFICATION = 0x020000
    LEARN_CONFIG = 0x010000
    # Byte 1 -- service protocol group.
    REMOTE_BUTTON = 0x008000
    ABBREV_DEFAULT_CDI = 0x004000
    DISPLAY = 0x002000
    SNIP = 0x001000
    CDI = 0x000800
    TRACTION = 0x000400
    FDI = 0x000200
    COMMAND_STATION = 0x000100
    # Byte 2 -- firmware upgrade signalling (S-9.7.4.3 sec.5.3).
    # A node sets FIRMWARE_UPGRADE_PROTOCOL when in Operating state
    # to signal it supports the upgrade flow; it swaps to
    # FIRMWARE_UPGRADE_ACTIVE while in Firmware Upgrade state.
    FIRMWARE_UPGRADE_PROTOCOL = 0x000020
    FIRMWARE_UPGRADE_ACTIVE = 0x000010


# Default advertised flags for a JMRI-Python OpenLCB node in its
# current shape (throttle + discovery, not a loco or command station):
#
# * Datagram + Memory Config -- powers the CDI fetch path.
# * Event Exchange -- Producer/Consumer identification service.
# * Identification -- PIP itself; trivially true if we're answering.
# * SNIP -- SnipResponder replies to identity probes.
# * CDI -- we can parse and read/write CDI config on our own node
#         (empty-CDI default; a real node with editable config would
#         override this).
#
# We do NOT advertise:
# * Stream, Reservation, Learn Config -- not implemented.
# * Remote Button, Display, FDI, Abbreviated CDI -- no.
# * Traction Control -- that's for loco nodes (respond to TC requests);
#   we're the throttle side (we send TC requests), which is unrelated
#   to PIP's "supports traction" advertisement.
# * DCC Command Station -- we're not one.
DEFAULT_JMRI_FLAGS = (
    ProtocolFlag.DATAGRAM
    | ProtocolFlag.MEMORY_CONFIG
    | ProtocolFlag.EVENT_EXCHANGE
    | ProtocolFlag.IDENTIFICATION
    | ProtocolFlag.SNIP
    | ProtocolFlag.CDI
)


def encode_flags(flags: ProtocolFlag) -> bytes:
    """Encode *flags* as the 3-byte big-endian mask that fills a PIP
    Reply body. Fits three bytes since :class:`ProtocolFlag` values
    never touch bits above bit 23."""
    return int(flags).to_bytes(3, "big")


def decode_flags(data: bytes) -> ProtocolFlag:
    """Decode a PIP Reply body into :class:`ProtocolFlag`.

    Tolerant of size variations: some old nodes reply with a 2-byte
    mask (byte 0 + byte 1), newer nodes send 3+ bytes. We zero-pad
    short buffers up to 3 bytes and drop anything beyond -- we don't
    have a home for byte 2+ flags yet, so ignoring them is safer
    than partially-decoded state."""
    if not data:
        raise ValueError("PIP reply is empty")
    padded = data[:3].ljust(3, b"\x00")
    return ProtocolFlag(int.from_bytes(padded, "big"))


# ---- typing shim --------------------------------------------------------


class _TcLike:
    """See :class:`~pmri_openlcb.snip.SnipTrafficLike`."""


# ---- our side: PipService -----------------------------------------------


class PipService:
    """Answer PIP Inquiries with the configured flag set.

    A caller building a specialised node (a virtual loco, a hub with
    Stream support, etc.) can pass a custom ``flags`` argument at
    construction. Default is :data:`DEFAULT_JMRI_FLAGS` which covers
    the current JMRI-Python capabilities."""

    def __init__(
        self, tc: _TcLike, flags: ProtocolFlag = DEFAULT_JMRI_FLAGS,
    ) -> None:
        self._tc = tc
        self._flags = flags
        self._reassembler = AddressedReassembler()
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    @property
    def flags(self) -> ProtocolFlag:
        return self._flags

    def set_flags(self, flags: ProtocolFlag) -> None:
        """Change what we advertise. Useful when a subsystem starts
        up mid-session and adds a new supported protocol."""
        self._flags = flags

    async def _on_frame(self, frame: OlcbFrame) -> None:
        msg = self._reassembler.receive(frame)
        if msg is None or msg.mti != MTI_PIP_INQUIRY:
            return
        if msg.dest_alias != self._tc.source_alias:
            return
        payload = encode_flags(self._flags)
        for reply in encode_addressed_frames(
            MTI_PIP_REPLY, self._tc.source_alias, msg.source_alias, payload,
        ):
            await self._tc.send_frame(reply)


# ---- client side: PipClient ---------------------------------------------


class PipClient:
    """Query peers for their PIP flags.

    ``probe(alias)`` sends an addressed inquiry and awaits the reply
    as a future. Per-target future dict so multiple concurrent
    probes to different aliases can run in parallel; a duplicate
    probe to the same alias raises rather than racing.
    """

    def __init__(self, tc: _TcLike) -> None:
        self._tc = tc
        self._reassembler = AddressedReassembler()
        self._pending: dict[int, asyncio.Future[ProtocolFlag]] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("PIP client closed"))
        self._pending.clear()

    async def probe(
        self, dest_alias: int, *, timeout: float = PIP_TIMEOUT,
    ) -> ProtocolFlag:
        """Ask *dest_alias* for its PIP flags. Returns the decoded
        :class:`ProtocolFlag` when the reply arrives."""
        if dest_alias in self._pending:
            raise RuntimeError(
                f"PIP probe to alias {dest_alias:#05x} already in flight",
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[ProtocolFlag] = loop.create_future()
        self._pending[dest_alias] = fut
        try:
            # PIP Inquiry has no payload beyond the addressed prefix
            # (which encode_addressed_frames prepends automatically).
            for frame in encode_addressed_frames(
                MTI_PIP_INQUIRY, self._tc.source_alias, dest_alias, b"",
            ):
                await self._tc.send_frame(frame)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(dest_alias, None)

    async def _on_frame(self, frame: OlcbFrame) -> None:
        msg = self._reassembler.receive(frame)
        if msg is None or msg.mti != MTI_PIP_REPLY:
            return
        fut = self._pending.get(msg.source_alias)
        if fut is None or fut.done():
            return
        try:
            flags = decode_flags(msg.body)
        except ValueError as e:
            fut.set_exception(e)
            return
        fut.set_result(flags)
