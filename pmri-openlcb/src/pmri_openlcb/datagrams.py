"""OpenLCB Datagram Protocol (per S-9.7.4.1).

Datagrams are variable-length messages up to 72 bytes carried across
1..N CAN frames. Every datagram has one *source* node and one
*destination* node; each direction must be acknowledged by a
"Datagram Received OK" reply or rejected with "Datagram Rejected".

CAN header layout (29-bit ID, high bits first) for datagram frames::

    bit 28       = 1  (extended CAN frame)
    bits 24..27  = frame kind:
                     0x2  Complete datagram in one frame
                     0x3  First frame of a multi-frame datagram
                     0x4  Middle frame(s)
                     0x5  Final frame
    bits 12..23  = destination node alias
    bits 0..11   = source node alias

Reply MTIs use the *global-messages* framing (bits 24..27 = 0x1,
bits 12..23 = 12-bit MTI). The two reply MTIs we care about are:

    0x4C48  Datagram Received OK
    0x4E48  Datagram Rejected

The engine below reads inbound frames from an
:class:`~pmri_openlcb.traffic.OlcbTrafficController` via its
``frames`` :class:`~pmri_core.events.Signal`, reassembles them, and
either exposes the resulting :class:`Datagram` to callers via
:meth:`DatagramEngine.wait_for` or dispatches to a registered
handler. Send side is symmetric: split the payload into 8-byte
chunks, emit the right frame kinds, and (optionally) wait for the
peer's Datagram Received OK reply.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pmri_core.events import Signal

from pmri_openlcb.codec import OlcbFrame, encode_frame

if TYPE_CHECKING:
    from pmri_openlcb.traffic import OlcbTrafficController

_log = logging.getLogger(__name__)

# ---- header helpers ----------------------------------------------------

DATAGRAM_COMPLETE = 0x2
DATAGRAM_FIRST = 0x3
DATAGRAM_MIDDLE = 0x4
DATAGRAM_FINAL = 0x5

# Global-message MTIs used for datagram replies (per S-9.7.4.1).
MTI_DATAGRAM_RECEIVED_OK = 0xA28
MTI_DATAGRAM_REJECTED = 0xA48

# Max datagram body size -- the standard caps at 72 bytes (9 frames of 8).
MAX_DATAGRAM_BYTES = 72

# Retry behaviour on transient Datagram Rejected replies. Per
# S-9.7.4.1 sec.5, rejected codes 0x2xxx are temporary (buffer full,
# out-of-order) and the sender may resend after a delay; 0x1xxx
# codes are permanent. The default backoff doubles per attempt so
# a busy peer gets exponential relief without ballooning latency
# on the common single-retry case.
DEFAULT_DATAGRAM_RETRIES = 3
DEFAULT_DATAGRAM_RETRY_BACKOFF = 0.05


def is_permanent_rejection(error_code: int) -> bool:
    """Return True when the ``Datagram Rejected`` error code marks
    a permanent failure -- top nibble 0x1 per S-9.7.4.1 sec.5.1. Any
    other family (including the "temporary" 0x2xxx codes) is
    considered retryable."""
    return (error_code >> 12) == 0x1


def is_temporary_rejection(error_code: int) -> bool:
    """The 0x2xxx family: buffer full, out of order, etc. Callers
    can retry after a short delay."""
    return (error_code >> 12) == 0x2


# ---- error-code taxonomy (S-9.7.4.1 sec.5.1) --------------------------------
#
# CROSS-CHECK BEFORE BETA: the specific codes below come from the
# Datagram Transport standard (S-9.7.4.1 sec.5.1) and the memory-config
# subset (S-9.10.1 sec.3.6). Any decoder that ships a datagram rejection
# ought to use one of these; a value not in the table is still a
# valid rejection -- we just describe it as "unspecified" plus family
# ("permanent" or "temporary") so a UI can still say something useful.

_REJECTION_DESCRIPTIONS: dict[int, str] = {
    # Permanent (0x1xxx family)
    0x1000: "Permanent error (unspecified)",
    0x1020: "Source not permitted",
    0x1040: "Datagrams not accepted from source",
    0x1080: "Not implemented",
    0x1081: "Not implemented (protocol version)",
    0x1082: "Address space unknown",
    0x1083: "Address out of bounds",
    0x1084: "Write to read-only space",
    0x1088: "Datagram format not accepted",
    # Temporary (0x2xxx family)
    0x2000: "Temporary error (unspecified)",
    0x2020: "Out of order -- datagram arrived before expected",
    0x2040: "Buffer unavailable -- peer busy, retry",
    0x2041: "Node in use -- locked by another client",
}


def describe_rejection(error_code: int) -> str:
    """Return a human-readable description for a Datagram Rejected
    error code, with fallback to family + hex when the specific
    code isn't in the taxonomy.

    Examples::

        >>> describe_rejection(0x1082)
        'Address space unknown'
        >>> describe_rejection(0x2040)
        'Buffer unavailable -- peer busy, retry'
        >>> describe_rejection(0x1234)   # unknown permanent code
        'Permanent error 0x1234 (unspecified)'
    """
    if error_code in _REJECTION_DESCRIPTIONS:
        return _REJECTION_DESCRIPTIONS[error_code]
    if is_permanent_rejection(error_code):
        return f"Permanent error {error_code:#06x} (unspecified)"
    if is_temporary_rejection(error_code):
        return f"Temporary error {error_code:#06x} (unspecified)"
    return f"Rejected (unknown family) {error_code:#06x}"


def _extract_frame_kind(header: int) -> int:
    """Bits 24..27 identify the frame kind."""
    return (header >> 24) & 0xF


def _extract_dest_alias(header: int) -> int:
    return (header >> 12) & 0xFFF


def _extract_source_alias(header: int) -> int:
    return header & 0xFFF


def _extract_mti(header: int) -> int:
    """The 12-bit MTI (bits 12..23), used for message-type frames.

    Only meaningful when the frame kind (bits 24..27) is 0x1
    (OpenLCB global/addressed message). For datagram-kind frames the
    same bit range holds the destination alias, not an MTI.
    """
    return (header >> 12) & 0xFFF


def make_datagram_header(
    kind: int, dest_alias: int, source_alias: int,
) -> int:
    if kind not in (
        DATAGRAM_COMPLETE, DATAGRAM_FIRST,
        DATAGRAM_MIDDLE, DATAGRAM_FINAL,
    ):
        raise ValueError(f"invalid datagram frame kind: {kind:#x}")
    if not 0 <= dest_alias <= 0xFFF:
        raise ValueError(f"dest alias must be 12-bit: {dest_alias:#x}")
    if not 0 <= source_alias <= 0xFFF:
        raise ValueError(f"source alias must be 12-bit: {source_alias:#x}")
    return (
        (1 << 28)
        | (kind << 24)
        | (dest_alias << 12)
        | source_alias
    )


def make_reply_header(
    mti: int, source_alias: int,
) -> int:
    """CAN header for a global/addressed message frame (frame kind 1).

    For addressed messages, the destination alias travels in the
    *data* payload (bytes 0..1) -- the header itself only carries
    the MTI + source alias.
    """
    return (
        (1 << 28)
        | (0x1 << 24)
        | (mti << 12)
        | source_alias
    )


def _encode_dest_prefix(dest_alias: int) -> bytes:
    """The 2-byte destination-alias prefix that leads every addressed
    message's data field."""
    return bytes([(dest_alias >> 8) & 0xFF, dest_alias & 0xFF])


def _decode_dest_alias_from_body(data: bytes) -> int | None:
    """Peel the destination alias off the first two bytes of an
    addressed-message body. Returns None when the frame doesn't
    carry one (short frame, corrupted header)."""
    if len(data) < 2:
        return None
    return ((data[0] & 0x0F) << 8) | data[1]


# ---- dataclasses --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Datagram:
    """A fully-reassembled datagram -- the body plus the two node aliases."""

    source_alias: int
    dest_alias: int
    data: bytes


@dataclass(frozen=True, slots=True)
class DatagramRejected(Exception):
    """Raised when the peer replies with ``Datagram Rejected`` to a
    :meth:`DatagramEngine.send` call. The 2-byte error code is
    exposed so callers can distinguish permanent from temporary
    failures via :func:`is_permanent_rejection` /
    :func:`is_temporary_rejection`, or fetch a friendly description
    via :func:`describe_rejection`.

    The ``__str__`` includes both the description and the hex code
    so log lines carry the operator-readable reason without losing
    the raw value cross-references need."""

    error_code: int

    @property
    def description(self) -> str:
        """Human-readable description of the rejection reason."""
        return describe_rejection(self.error_code)

    def __str__(self) -> str:
        return (
            f"datagram rejected ({self.description}; "
            f"code {self.error_code:#06x})"
        )


# ---- engine -------------------------------------------------------------


@dataclass
class _Pending:
    """One in-flight outgoing datagram, waiting for the peer's reply."""

    fut: asyncio.Future[None]
    # The engine assumes one outstanding datagram per (src -> dst)
    # pair; the standard allows more but no known real node needs it
    # and the extra bookkeeping is fragile.
    dest_alias: int


class DatagramEngine:
    """Reassembles inbound datagrams, sends outbound ones with
    fragmentation, and pairs sends with the peer's ``Received OK`` /
    ``Rejected`` reply.

    The reader hooks into an :class:`OlcbTrafficController`'s frames
    signal via :meth:`attach`; the caller is responsible for
    :meth:`detach` (or calling ``aclose``) at shutdown so the signal
    listener doesn't outlive the engine.
    """

    def __init__(
        self, tc: OlcbTrafficController,
    ) -> None:
        self._tc = tc
        self._unsubscribe: Callable[[], None] | None = None
        # Per-peer reassembly buffers keyed by source alias.
        self._partial: dict[int, bytearray] = {}
        # Pending sends keyed by destination alias.
        self._pending: dict[int, _Pending] = {}
        # Aliases we're prepared to accept datagrams for. The primary
        # alias (our own node) is added lazily on first frame -- see
        # :meth:`_target_aliases` -- because the TC's source_alias may
        # not be finalized yet at engine-construction time. Additional
        # aliases (Command Station hosted train nodes) are registered
        # via :meth:`accept_alias`.
        self._extra_aliases: set[int] = set()
        # Fires with every fully-received *inbound* datagram after the
        # engine has already sent Received OK to the peer.
        self.inbound: Signal[Datagram] = Signal()

    def _target_aliases(self) -> set[int]:
        """The set of dest aliases we accept datagrams for. Union of
        the TC's primary source alias plus any extras registered via
        :meth:`accept_alias`."""
        return {self._tc.source_alias, *self._extra_aliases}

    def accept_alias(self, alias: int) -> None:
        """Route inbound datagrams whose dest alias matches *alias*
        to our :attr:`inbound` signal in addition to the TC's primary
        alias. Used by :class:`CommandStationNode` to receive
        datagrams targeting hosted train aliases so it can serve
        FDI/CDI on their behalf."""
        if not 0 < alias <= 0xFFF:
            raise ValueError(f"alias must be 12-bit and non-zero: {alias:#x}")
        self._extra_aliases.add(alias)

    def stop_accepting(self, alias: int) -> None:
        """Reverse of :meth:`accept_alias`. Silent no-op when the
        alias was never registered -- callers can invoke unconditionally
        during a hosted loco's teardown."""
        self._extra_aliases.discard(alias)
        # Also drop any half-assembled datagrams keyed by peers that
        # were mid-flight into this alias -- the target's gone, no
        # point holding onto the fragments.
        # (Reassembly buffers key by *source* not *dest*, so nothing
        # to do here; kept as a note for future refactors.)

    async def attach(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._tc.frames.connect(self._on_frame)

    async def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        # Fail any outstanding sends so callers don't hang forever.
        for pending in list(self._pending.values()):
            if not pending.fut.done():
                pending.fut.set_exception(
                    RuntimeError("datagram engine closed"),
                )
        self._pending.clear()
        self._partial.clear()

    # ---- send -----------------------------------------------------------

    async def send(
        self,
        dest_alias: int,
        data: bytes,
        *,
        timeout: float = 3.0,
        wait_for_reply: bool = True,
        max_retries: int = DEFAULT_DATAGRAM_RETRIES,
        retry_backoff: float = DEFAULT_DATAGRAM_RETRY_BACKOFF,
    ) -> None:
        """Send *data* to *dest_alias*. Awaits the peer's Datagram
        Received OK when ``wait_for_reply`` is True (default). Raises
        :class:`DatagramRejected` on Rejected, :class:`TimeoutError`
        on no reply, :class:`ValueError` on bad inputs.

        Data over 8 bytes is fragmented into First / Middle... / Final
        frames per S-9.7.4.1. Zero-byte payloads are permitted and
        travel as a single Complete frame with an empty data field.

        On a temporary rejection (error code 0x2xxx -- buffer full,
        out of order) the datagram is resent up to *max_retries*
        times with exponentially-growing backoff starting from
        *retry_backoff* seconds. Permanent rejections (0x1xxx) and
        unrelated exceptions raise immediately.
        """
        if not 0 <= dest_alias <= 0xFFF:
            raise ValueError(f"dest alias must be 12-bit: {dest_alias:#x}")
        if len(data) > MAX_DATAGRAM_BYTES:
            raise ValueError(
                f"datagram body {len(data)} exceeds "
                f"{MAX_DATAGRAM_BYTES}-byte cap",
            )
        if dest_alias in self._pending:
            raise RuntimeError(
                f"a datagram to alias {dest_alias:#x} is already in flight",
            )
        backoff = retry_backoff
        for attempt in range(max_retries + 1):
            try:
                await self._send_once(
                    dest_alias, data, timeout=timeout,
                    wait_for_reply=wait_for_reply,
                )
                return
            except DatagramRejected as exc:
                # Permanent rejects: never retry. Temporary rejects
                # on the final attempt also propagate -- retries
                # exhausted, let the caller decide.
                if is_permanent_rejection(exc.error_code):
                    raise
                if attempt >= max_retries:
                    raise
                _log.info(
                    "datagram to %#05x rejected (code %#06x) -- "
                    "retry %d/%d after %.0fms",
                    dest_alias, exc.error_code,
                    attempt + 1, max_retries, backoff * 1000,
                )
                await asyncio.sleep(backoff)
                backoff *= 2

    async def _send_once(
        self,
        dest_alias: int,
        data: bytes,
        *,
        timeout: float,
        wait_for_reply: bool,
    ) -> None:
        source_alias = self._tc.source_alias

        # Arm the reply-waiter BEFORE sending so a very fast peer
        # can't beat us to it. Skipped when the caller opts out.
        pending: _Pending | None = None
        if wait_for_reply:
            loop = asyncio.get_event_loop()
            pending = _Pending(fut=loop.create_future(), dest_alias=dest_alias)
            self._pending[dest_alias] = pending

        try:
            await self._transmit(source_alias, dest_alias, data)
            if pending is not None:
                await asyncio.wait_for(pending.fut, timeout=timeout)
        finally:
            if pending is not None:
                self._pending.pop(dest_alias, None)

    async def _transmit(
        self, source_alias: int, dest_alias: int, data: bytes,
    ) -> None:
        """Write out the datagram frames. Single Complete when body <= 8,
        First + N x Middle + Final otherwise."""
        if len(data) <= 8:
            # Zero-length datagrams still go on the wire as a Complete
            # frame -- the protocol permits them even if no real
            # sub-protocol uses them.
            frame = OlcbFrame(
                header=make_datagram_header(
                    DATAGRAM_COMPLETE, dest_alias, source_alias,
                ),
                data=data,
            )
            await self._tc.send_frame(frame)
            return

        # Multi-frame: First, then Middle...Middle, then Final.
        chunks: list[bytes] = [
            data[i:i + 8] for i in range(0, len(data), 8)
        ]
        for i, chunk in enumerate(chunks):
            if i == 0:
                kind = DATAGRAM_FIRST
            elif i == len(chunks) - 1:
                kind = DATAGRAM_FINAL
            else:
                kind = DATAGRAM_MIDDLE
            frame = OlcbFrame(
                header=make_datagram_header(kind, dest_alias, source_alias),
                data=chunk,
            )
            await self._tc.send_frame(frame)

    # ---- receive --------------------------------------------------------

    async def _on_frame(self, frame: OlcbFrame) -> None:
        kind = _extract_frame_kind(frame.header)
        if kind in (
            DATAGRAM_COMPLETE, DATAGRAM_FIRST,
            DATAGRAM_MIDDLE, DATAGRAM_FINAL,
        ):
            await self._handle_datagram_frame(kind, frame)
            return
        # Not a datagram frame -- is it a reply to something WE sent?
        if kind == 0x1:
            mti = _extract_mti(frame.header)
            if mti in (MTI_DATAGRAM_RECEIVED_OK, MTI_DATAGRAM_REJECTED):
                await self._handle_reply(mti, frame)

    async def _handle_datagram_frame(
        self, kind: int, frame: OlcbFrame,
    ) -> None:
        dest_alias = _extract_dest_alias(frame.header)
        source_alias = _extract_source_alias(frame.header)

        # We accept datagrams only when they're addressed to one of
        # the aliases we serve (our own + any hosted train aliases
        # the CS has registered). Everything else is on the bus but
        # not for us.
        if dest_alias not in self._target_aliases():
            return

        if kind == DATAGRAM_COMPLETE:
            body = bytes(frame.data)
            await self._deliver(source_alias, dest_alias, body)
            return

        buf = self._partial.setdefault(source_alias, bytearray())
        if kind == DATAGRAM_FIRST:
            # Reset any dangling partial (peer aborted a previous send).
            buf.clear()
            buf.extend(frame.data)
            return
        if kind == DATAGRAM_MIDDLE:
            buf.extend(frame.data)
            return
        if kind == DATAGRAM_FINAL:
            buf.extend(frame.data)
            body = bytes(buf)
            self._partial.pop(source_alias, None)
            await self._deliver(source_alias, dest_alias, body)

    async def _deliver(
        self, source_alias: int, dest_alias: int, body: bytes,
    ) -> None:
        # Send Received OK before firing our own signal so the reply
        # is on the wire even if a subscriber raises. Source the OK
        # reply from the alias the peer targeted -- a peer talking to
        # a hosted train alias expects the OK to come back from that
        # same alias, not from our primary node.
        await self._send_received_ok(source_alias, from_alias=dest_alias)
        dg = Datagram(
            source_alias=source_alias,
            dest_alias=dest_alias,
            data=body,
        )
        await self.inbound.emit(dg)

    async def _send_received_ok(
        self, dest_alias: int, *, from_alias: int | None = None,
    ) -> None:
        """*from_alias* defaults to our primary alias -- override so
        the OK reply is sourced from whichever alias the peer
        addressed. Matters for CS-served aliases where a mismatched
        source would confuse the peer's reply routing."""
        source = from_alias if from_alias is not None else self._tc.source_alias
        frame = OlcbFrame(
            header=make_reply_header(
                MTI_DATAGRAM_RECEIVED_OK,
                source,
            ),
            # Addressed messages carry the destination alias in the
            # first two body bytes, followed by protocol-specific
            # fields (here: a single 0x00 = "no reply pending").
            data=_encode_dest_prefix(dest_alias) + b"\x00",
        )
        await self._tc.send_frame(frame)

    async def _handle_reply(self, mti: int, frame: OlcbFrame) -> None:
        # The reply's *source* is the peer we sent to. Match on that.
        peer_alias = _extract_source_alias(frame.header)
        # For addressed messages, the destination alias travels in
        # the first 2 body bytes, not the header.
        addressed_dest = _decode_dest_alias_from_body(frame.data)
        if addressed_dest != self._tc.source_alias:
            return
        pending = self._pending.get(peer_alias)
        if pending is None or pending.fut.done():
            return
        # The status/error bytes follow the 2-byte destination
        # prefix. Received OK carries one byte (0x00 or reply-pending
        # flag); Rejected carries the 2-byte error code.
        body = frame.data[2:]
        if mti == MTI_DATAGRAM_RECEIVED_OK:
            pending.fut.set_result(None)
        else:  # MTI_DATAGRAM_REJECTED
            code = 0
            if len(body) >= 2:
                code = (body[0] << 8) | body[1]
            pending.fut.set_exception(DatagramRejected(error_code=code))
