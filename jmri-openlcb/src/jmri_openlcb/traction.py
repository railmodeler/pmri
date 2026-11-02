"""OpenLCB Traction protocol -- S-9.7.4.7.

Locomotive control over OpenLCB. Every command is an addressed message
carrying a discriminator byte + operation-specific payload:

* **Set Speed/Dir** (``0x00``) -- 2-byte half-precision float. Sign
  bit encodes direction (negative = reverse); magnitude is speed in
  mph.
* **Set Function** (``0x01``) -- 3-byte function number + 2-byte value.
  Values 0/1 for on/off; up to 65535 for analog functions.
* **Emergency Stop** (``0x02``) -- no payload; loco halts immediately.
* **Query Speed** (``0x10``) -- no payload; reply carries current
  speed / direction / status.
* **Query Function** (``0x11``) -- 3-byte function number; reply
  carries the 2-byte value.
* **Controller Config** (``0x20``) -- one-byte sub-command:

  - ``0x01`` Assign: claim this loco as ours. Flags byte + our
    6-byte NodeID.
  - ``0x02`` Release: hand it back.
  - ``0x03`` Query: reply carries current controller NodeID (all
    zeros if unclaimed).

Every operation is a wire-level primitive on this client -- a JMRI
throttle bean is a separate layer on top. Queries use a per-target
futures dict so multiple concurrent throttles can coexist without
their replies crossing wires.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jmri_core.events import Signal

from jmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import OlcbFrame

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# ---- MTIs ---------------------------------------------------------------

MTI_TRACTION_REQUEST = 0x05EB
MTI_TRACTION_REPLY = 0x01E9

# ---- command discriminator bytes ----------------------------------------

TC_SET_SPEED_DIR = 0x00
TC_SET_FUNCTION = 0x01
TC_EMERGENCY_STOP = 0x02
TC_QUERY_SPEED = 0x10
TC_QUERY_FUNCTION = 0x11
TC_CONTROLLER_CONFIG = 0x20
TC_CONSIST_CONFIG = 0x30
TC_MANAGEMENT = 0x40

# Sub-commands for TC_CONTROLLER_CONFIG (byte 1 of the payload).
CC_ASSIGN = 0x01
CC_RELEASE = 0x02
CC_QUERY = 0x03

# Sub-commands for TC_CONSIST_CONFIG (byte 1 of the payload).
CONSIST_ATTACH = 0x01
CONSIST_DETACH = 0x02
CONSIST_QUERY = 0x03

# Flag bits for CONSIST_ATTACH (2-byte flags between sub-cmd + NodeID).
# Bit 0 of the low byte: hidden -- don't show this loco in a throttle
# consist listing. Useful for a "helper" loco you don't want the
# operator to accidentally detach.
CONSIST_FLAG_HIDDEN = 0x01
# Bit 1: reverse direction -- the member runs backward relative to
# the consist's head direction. Matches the DCC consist "reverse"
# flag most NCE/Digitrax consists expose.
CONSIST_FLAG_REVERSE = 0x02

# Sentinel byte returned by a Consist Query reply when the requested
# index is past the end of the member list. Lets a client know to
# stop iterating without having to pre-query the size.
CONSIST_INDEX_INVALID = 0xFF

# Sub-commands for TC_MANAGEMENT (byte 1 of the payload).
# Noop is throttle -> loco, "I'm still here." Heartbeat Request is
# loco -> throttle, "I haven't heard from you in a while -- confirm
# you're still there or I'll drop you after ``timeout`` seconds."
MGMT_NOOP = 0x01
MGMT_HEARTBEAT_REQUEST = 0x02

# Default heartbeat window a loco advertises. Spec allows any value;
# most decoders default around 3..5 seconds.
DEFAULT_HEARTBEAT_TIMEOUT = 5

# Default wait for query replies. Loco decoders answer in ms on a
# healthy bus; 2 seconds accommodates WAN-tunneled hubs.
TRACTION_QUERY_TIMEOUT = 2.0


# ---- half-precision float speed encoding --------------------------------


def encode_speed(mph: float, forward: bool) -> bytes:
    """Encode *mph* + *forward* as a 2-byte half-precision IEEE 754
    float, big-endian. The sign bit encodes direction (negative =
    reverse), magnitude is mph. Reverse of :func:`decode_speed`.

    NaN and infinity are legal inputs on the wire (spec sec.5.1.1
    reserves them for special cases like "unknown") but we don't
    generate them here -- callers who need those cases can drop to
    :func:`struct.pack` directly."""
    value = mph if forward else -mph
    return struct.pack(">e", value)


def decode_speed(data: bytes) -> tuple[float, bool]:
    """Inverse of :func:`encode_speed`. Returns ``(mph, forward)``.

    Negative-zero decodes as reverse -- matches the S-9.7.4.7 sec.5.1
    convention that the sign bit alone carries direction, not the
    magnitude."""
    if len(data) < 2:
        raise ValueError(
            f"speed payload needs 2 bytes, got {len(data)}",
        )
    value = struct.unpack(">e", data[:2])[0]
    # struct.unpack for float16 preserves sign of zero; check bit 15.
    sign_bit_set = bool(data[0] & 0x80)
    return abs(value), not sign_bit_set


# ---- query-reply data classes -------------------------------------------


@dataclass(frozen=True, slots=True)
class SpeedReply:
    """Reply to :meth:`TractionClient.query_speed`.

    ``set_speed`` is the last-commanded speed; ``actual_speed`` is
    the loco's currently-realized speed (they diverge during ramp).
    Some decoders don't populate the extra fields -- those come back
    as NaN which we surface as ``None``."""

    set_speed_mph: float
    set_forward: bool
    commanded_speed_mph: float | None = None
    commanded_forward: bool | None = None
    actual_speed_mph: float | None = None
    actual_forward: bool | None = None
    status: int | None = None


@dataclass(frozen=True, slots=True)
class ControllerReply:
    """Reply to :meth:`TractionClient.query_controller`.

    ``controller`` is the NodeID of whichever throttle currently owns
    the loco. When the loco is unassigned, some decoders send all
    zeros -- decoded as :class:`NodeId(0)`."""

    controller: NodeId


@dataclass(frozen=True, slots=True)
class ConsistMember:
    """One loco attached to another loco's consist.

    ``hidden`` flags a "helper" member the throttle UI shouldn't
    expose for casual detach; ``reverse`` inverts direction relative
    to the consist head (matches the DCC "consist direction" flag
    NCE/Digitrax consists surface)."""

    node_id: NodeId
    hidden: bool = False
    reverse: bool = False


def encode_consist_flags(member: ConsistMember) -> bytes:
    """Encode the 2-byte flags block that precedes the member NodeID
    in an Attach payload. The high byte is reserved; every bit in
    the low byte is defined by S-9.7.4.7 sec.6.3.1."""
    low = 0
    if member.hidden:
        low |= CONSIST_FLAG_HIDDEN
    if member.reverse:
        low |= CONSIST_FLAG_REVERSE
    return bytes([0x00, low])


def decode_consist_flags(data: bytes) -> tuple[bool, bool]:
    """Inverse of :func:`encode_consist_flags`. Ignores unused bits
    so a spec extension setting a new flag doesn't crash us -- we
    just don't surface it."""
    if len(data) < 2:
        raise ValueError(
            f"consist flags need 2 bytes, got {len(data)}",
        )
    low = data[1]
    return bool(low & CONSIST_FLAG_HIDDEN), bool(low & CONSIST_FLAG_REVERSE)


@dataclass(frozen=True, slots=True)
class HeartbeatRequest:
    """Emitted by :attr:`TractionClient.heartbeat_requested` when a
    loco asks the controlling throttle to check in.

    ``timeout_seconds`` is the loco's grace window -- the controller
    must respond with any traction command (typically Noop) within
    that many seconds, or the loco will e-stop and drop the
    controller assignment. Zero means "reply immediately."
    """

    loco_alias: int
    timeout_seconds: int


# ---- client ------------------------------------------------------------


class TractionClient:
    """Send Traction commands to loco nodes on the bus.

    Every method that expects a reply uses a per-``(loco_alias,
    command_byte)`` futures dict so several concurrent throttles
    can share one client without their replies crossing wires.

    Non-reply commands (``set_speed``, ``set_function``,
    ``emergency_stop``, ``assign_controller``, ``release_controller``)
    return as soon as the frame is on the wire -- decoders don't
    ack these, so there's nothing meaningful to await.
    """

    def __init__(self, tc: _TcLike) -> None:
        self._tc = tc
        self._reassembler = AddressedReassembler()
        # keyed by (loco_alias, command byte) -> future for the reply payload.
        self._pending: dict[tuple[int, int], asyncio.Future[bytes]] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)
        # Fires when a loco sends a Heartbeat Request. Subscribers
        # can call :meth:`send_noop` (or any other traction command)
        # against the loco to answer, or ignore it if the loco isn't
        # ours. See :class:`HeartbeatKeeper` for a canned handler.
        self.heartbeat_requested: Signal[HeartbeatRequest] = Signal()

    def close(self) -> None:
        self._unsubscribe()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("traction client closed"))
        self._pending.clear()

    # ---- fire-and-forget commands ---------------------------------------

    async def set_speed(
        self, loco_alias: int, mph: float, *, forward: bool = True,
    ) -> None:
        """Command *loco_alias* to run at *mph* in *forward* direction."""
        payload = bytes([TC_SET_SPEED_DIR]) + encode_speed(mph, forward)
        await self._send_request(loco_alias, payload)

    async def set_function(
        self, loco_alias: int, function: int, value: int,
    ) -> None:
        """Set *function* on *loco_alias* to *value*.

        *function* is a 24-bit number (most decoders use 0..28);
        *value* is 16 bits (0/1 for on/off, or an analog value)."""
        if not 0 <= function <= 0xFFFFFF:
            raise ValueError(
                f"function number must be 24-bit, got {function}",
            )
        if not 0 <= value <= 0xFFFF:
            raise ValueError(
                f"function value must be 16-bit, got {value}",
            )
        payload = (
            bytes([TC_SET_FUNCTION])
            + function.to_bytes(3, "big")
            + value.to_bytes(2, "big")
        )
        await self._send_request(loco_alias, payload)

    async def emergency_stop(self, loco_alias: int) -> None:
        """Send emergency stop to *loco_alias* -- decoder halts
        immediately, no ramp. Speed is not preserved for a follow-up
        release; the throttle must re-send set_speed to move again."""
        await self._send_request(loco_alias, bytes([TC_EMERGENCY_STOP]))

    async def send_noop(self, loco_alias: int) -> None:
        """Send a Traction Management Noop -- the "I'm still here"
        keepalive. Sent proactively at a regular cadence (see
        :class:`HeartbeatKeeper`) and/or as a response to a
        Heartbeat Request from the loco. Noop is fire-and-forget;
        the loco resets its watchdog on receipt and doesn't ack."""
        await self._send_request(
            loco_alias, bytes([TC_MANAGEMENT, MGMT_NOOP]),
        )

    async def assign_controller(
        self, loco_alias: int, our_node_id: NodeId,
    ) -> None:
        """Claim *loco_alias* as controlled by our node. The loco
        will then broadcast a Controller Change Notify to any
        previous controller so they can release cleanly."""
        payload = (
            bytes([TC_CONTROLLER_CONFIG, CC_ASSIGN, 0x00])
            + our_node_id.bytes
        )
        await self._send_request(loco_alias, payload)

    async def release_controller(
        self, loco_alias: int, our_node_id: NodeId,
    ) -> None:
        """Release *loco_alias* -- decoder can now accept commands
        from a different controller."""
        payload = (
            bytes([TC_CONTROLLER_CONFIG, CC_RELEASE, 0x00])
            + our_node_id.bytes
        )
        await self._send_request(loco_alias, payload)

    # ---- request/reply queries ------------------------------------------

    async def query_speed(
        self, loco_alias: int, *, timeout: float = TRACTION_QUERY_TIMEOUT,
    ) -> SpeedReply:
        """Ask *loco_alias* for its current speed & direction. Awaits
        the Traction Control Reply with matching command byte."""
        reply = await self._request_reply(
            loco_alias, bytes([TC_QUERY_SPEED]),
            reply_command=TC_QUERY_SPEED, timeout=timeout,
        )
        return _parse_speed_reply(reply)

    async def query_function(
        self, loco_alias: int, function: int,
        *, timeout: float = TRACTION_QUERY_TIMEOUT,
    ) -> int:
        """Ask *loco_alias* for the current value of *function*."""
        if not 0 <= function <= 0xFFFFFF:
            raise ValueError(
                f"function number must be 24-bit, got {function}",
            )
        payload = bytes([TC_QUERY_FUNCTION]) + function.to_bytes(3, "big")
        reply = await self._request_reply(
            loco_alias, payload,
            reply_command=TC_QUERY_FUNCTION, timeout=timeout,
        )
        # Reply layout: cmd(1) + fn(3) + value(2). Verify function
        # number matches so a concurrent query for a different fn
        # doesn't sneak in.
        if len(reply) < 6:
            raise ValueError(
                f"short function reply: {len(reply)} bytes",
            )
        got_fn = int.from_bytes(reply[1:4], "big")
        if got_fn != function:
            raise ValueError(
                f"function reply mismatch: expected {function}, got {got_fn}",
            )
        return int.from_bytes(reply[4:6], "big")

    async def query_controller(
        self, loco_alias: int, *, timeout: float = TRACTION_QUERY_TIMEOUT,
    ) -> ControllerReply:
        """Ask *loco_alias* who its current controller is."""
        payload = bytes([TC_CONTROLLER_CONFIG, CC_QUERY])
        reply = await self._request_reply(
            loco_alias, payload,
            reply_command=TC_CONTROLLER_CONFIG, timeout=timeout,
        )
        # Reply: cmd(1) + sub_cmd(1) + flags(1) + nid(6) = 9 bytes.
        if len(reply) < 9:
            raise ValueError(
                f"short controller reply: {len(reply)} bytes",
            )
        controller = NodeId(int.from_bytes(reply[3:9], "big"))
        return ControllerReply(controller=controller)

    # ---- consist configuration ------------------------------------------

    async def attach_consist_member(
        self, loco_alias: int, member: ConsistMember,
    ) -> None:
        """Attach *member* to *loco_alias*'s consist. Fire-and-forget --
        the consist head decoder doesn't ack Attach on the wire (only
        Query round-trips), so callers verify by follow-up query."""
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_ATTACH])
            + encode_consist_flags(member)
            + member.node_id.bytes
        )
        await self._send_request(loco_alias, payload)

    async def detach_consist_member(
        self, loco_alias: int, member_node_id: NodeId,
    ) -> None:
        """Detach *member_node_id* from *loco_alias*'s consist. Also
        fire-and-forget; verify via query if you need to confirm."""
        payload = (
            bytes([TC_CONSIST_CONFIG, CONSIST_DETACH])
            + member_node_id.bytes
        )
        await self._send_request(loco_alias, payload)

    async def query_consist_size(
        self, loco_alias: int, *, timeout: float = TRACTION_QUERY_TIMEOUT,
    ) -> int:
        """Return the number of members in *loco_alias*'s consist.

        Queries index 0 and reads the size field from the reply.
        A consist with zero members still gets a size reply -- the
        loco just answers with ``consist_size=0`` and an invalid
        index sentinel."""
        member, size = await self._query_consist_at(
            loco_alias, index=0, timeout=timeout,
        )
        return size

    async def query_consist_member(
        self, loco_alias: int, index: int,
        *, timeout: float = TRACTION_QUERY_TIMEOUT,
    ) -> ConsistMember | None:
        """Return the member at *index*, or ``None`` if the index is
        past the end of the consist."""
        member, _ = await self._query_consist_at(
            loco_alias, index=index, timeout=timeout,
        )
        return member

    async def query_consist_members(
        self, loco_alias: int, *, timeout: float = TRACTION_QUERY_TIMEOUT,
    ) -> list[ConsistMember]:
        """Fetch the full member list. Iterates from index 0 upward,
        using the size reported by the first reply to bound the
        loop -- a rogue loco that lied about its size can't drag us
        into an infinite loop."""
        first, size = await self._query_consist_at(
            loco_alias, index=0, timeout=timeout,
        )
        if size == 0 or first is None:
            return []
        out = [first]
        for i in range(1, size):
            member, _ = await self._query_consist_at(
                loco_alias, index=i, timeout=timeout,
            )
            if member is None:
                break
            out.append(member)
        return out

    async def _query_consist_at(
        self, loco_alias: int, index: int, *, timeout: float,
    ) -> tuple[ConsistMember | None, int]:
        """Send one Consist Query and decode the reply. Returns
        ``(member, size)`` -- ``member`` is ``None`` when the
        replier signals "index out of range" via the ``0xFF``
        sentinel."""
        if not 0 <= index <= 0xFF:
            raise ValueError(f"consist index must be 0..255, got {index}")
        payload = bytes([TC_CONSIST_CONFIG, CONSIST_QUERY, index])
        reply = await self._request_reply(
            loco_alias, payload,
            reply_command=TC_CONSIST_CONFIG, timeout=timeout,
        )
        # Reply: cmd(1) + sub(1) + size(1) + index(1) + flags(2) + nid(6) = 12.
        if len(reply) < 4:
            raise ValueError(
                f"short consist query reply: {len(reply)} bytes",
            )
        size = reply[2]
        got_index = reply[3]
        if got_index == CONSIST_INDEX_INVALID:
            return None, size
        if len(reply) < 12:
            raise ValueError(
                f"consist query reply carries member marker but is "
                f"only {len(reply)} bytes -- needs 12",
            )
        hidden, reverse = decode_consist_flags(reply[4:6])
        member_nid = NodeId(int.from_bytes(reply[6:12], "big"))
        return (
            ConsistMember(node_id=member_nid, hidden=hidden, reverse=reverse),
            size,
        )

    # ---- transport plumbing ---------------------------------------------

    async def _send_request(
        self, dest_alias: int, payload: bytes,
    ) -> None:
        for frame in encode_addressed_frames(
            MTI_TRACTION_REQUEST, self._tc.source_alias, dest_alias, payload,
        ):
            await self._tc.send_frame(frame)

    async def _request_reply(
        self, dest_alias: int, payload: bytes,
        *, reply_command: int, timeout: float,
    ) -> bytes:
        key = (dest_alias, reply_command)
        if key in self._pending:
            raise RuntimeError(
                f"query {reply_command:#04x} to alias {dest_alias:#05x} "
                "already in flight",
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        self._pending[key] = fut
        try:
            await self._send_request(dest_alias, payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(key, None)

    async def _on_frame(self, frame: OlcbFrame) -> None:
        """Route inbound Traction frames.

        * Traction Reply -> dispatch to the matching pending future.
        * Traction Request from a loco with sub-command
          ``MGMT_HEARTBEAT_REQUEST`` -> emit :attr:`heartbeat_requested`
          so a subscriber can decide whether/how to answer. Real
          loco decoders send this when they haven't heard from the
          controlling throttle recently; the throttle needs to
          respond with ANY traction command (typically
          :meth:`send_noop`) within the advertised ``timeout``
          seconds or lose the assignment.
        """
        msg = self._reassembler.receive(frame)
        if msg is None:
            return
        if msg.mti == MTI_TRACTION_REQUEST:
            await self._on_traction_request(msg)
            return
        if msg.mti != MTI_TRACTION_REPLY or not msg.body:
            return
        command_byte = msg.body[0]
        # Some queries share a command byte in the reply -- Query
        # Controller replies at 0x20, same as any other Controller
        # Config sub-command. We disambiguate on the sub-command
        # too when needed.
        key = (msg.source_alias, command_byte)
        fut = self._pending.get(key)
        if fut is None or fut.done():
            return
        fut.set_result(msg.body)

    async def _on_traction_request(self, msg) -> None:
        """Filter inbound Traction Requests for Heartbeat notifications.

        The only legitimate traction-command direction is
        throttle -> loco; a loco sending 0x40 0x02 (Heartbeat Request)
        back at the controller is the one documented exception in
        S-9.7.4.7 sec.6.4.2. Anything else here is ignored -- we're not
        pretending to be a loco."""
        if len(msg.body) < 2:
            return
        if msg.body[0] != TC_MANAGEMENT or msg.body[1] != MGMT_HEARTBEAT_REQUEST:
            return
        # Byte 2: timeout seconds (spec says single unsigned byte).
        timeout = msg.body[2] if len(msg.body) >= 3 else DEFAULT_HEARTBEAT_TIMEOUT
        await self.heartbeat_requested.emit(HeartbeatRequest(
            loco_alias=msg.source_alias,
            timeout_seconds=timeout,
        ))


# ---- reply parsers -------------------------------------------------------


def _parse_speed_reply(body: bytes) -> SpeedReply:
    """Decode a Query Speed reply body.

    Layout per S-9.7.4.7 sec.5.10:

    * byte 0     = 0x10 (echoes the query command)
    * bytes 1..2 = set speed (float16, sign = direction)
    * byte 3     = status (bit flags)
    * bytes 4..5 = commanded speed (optional; NaN if not tracked)
    * bytes 6..7 = actual speed (optional; NaN if not tracked)

    Older / minimal decoders may truncate anywhere after byte 3.
    We tolerate that by leaving the tail fields ``None``.
    """
    if len(body) < 3:
        raise ValueError(f"short speed reply: {len(body)} bytes")
    set_speed_mph, set_forward = decode_speed(body[1:3])
    status: int | None = body[3] if len(body) >= 4 else None
    commanded_speed_mph: float | None = None
    commanded_forward: bool | None = None
    if len(body) >= 6:
        commanded_speed_mph, commanded_forward = _decode_speed_optional(body[4:6])
    actual_speed_mph: float | None = None
    actual_forward: bool | None = None
    if len(body) >= 8:
        actual_speed_mph, actual_forward = _decode_speed_optional(body[6:8])
    return SpeedReply(
        set_speed_mph=set_speed_mph, set_forward=set_forward,
        commanded_speed_mph=commanded_speed_mph,
        commanded_forward=commanded_forward,
        actual_speed_mph=actual_speed_mph,
        actual_forward=actual_forward,
        status=status,
    )


def _decode_speed_optional(data: bytes) -> tuple[float | None, bool | None]:
    """Same as :func:`decode_speed` but treats NaN as ``(None, None)``
    -- matches the S-9.7.4.7 convention that decoders emit NaN for
    "field not tracked."""
    import math  # noqa: PLC0415
    mph, forward = decode_speed(data)
    if math.isnan(mph):
        return None, None
    return mph, forward


# ---- heartbeat keeper ----------------------------------------------------


class HeartbeatKeeper:
    """Keep controller assignments alive across a set of locos.

    Two responsibilities:

    * **Auto-answer Heartbeat Requests** -- subscribes to the client's
      :attr:`~TractionClient.heartbeat_requested` signal and sends
      a Noop back for any loco in the tracked set. Ignores requests
      for locos we don't control (some other throttle's problem).
    * **Optional periodic Noop** -- when :meth:`start_periodic_noop`
      is running, sends a Noop to every tracked loco every
      *interval* seconds so the loco's watchdog stays reset even
      when the operator isn't actively driving. Idempotent -- a
      second start does nothing; stop is safe to call before start.

    Typical usage from a throttle app::

        keeper = HeartbeatKeeper(conn.traction)
        keeper.add(loco_alias)
        keeper.start_periodic_noop(interval=2.0)
        # ... drive the loco ...
        keeper.remove(loco_alias)
        await keeper.close()
    """

    def __init__(self, client: TractionClient) -> None:
        self._client = client
        self._locos: set[int] = set()
        self._noop_task: asyncio.Task[None] | None = None
        self._noop_stop: asyncio.Event | None = None
        self._interval = 2.0
        self._unsubscribe = client.heartbeat_requested.connect(
            self._on_heartbeat_request,
        )

    async def close(self) -> None:
        """Unsubscribe from heartbeat requests + stop the periodic
        Noop task if one is running. Safe to call more than once."""
        self._unsubscribe()
        await self.stop_periodic_noop()

    def add(self, loco_alias: int) -> None:
        """Start answering heartbeats for *loco_alias*. Idempotent."""
        self._locos.add(loco_alias)

    def remove(self, loco_alias: int) -> None:
        """Stop answering heartbeats for *loco_alias*. Safe to call
        for an alias we're not tracking."""
        self._locos.discard(loco_alias)

    def tracks(self, loco_alias: int) -> bool:
        return loco_alias in self._locos

    def tracked(self) -> set[int]:
        """Snapshot of currently-tracked loco aliases."""
        return set(self._locos)

    def start_periodic_noop(self, *, interval: float = 2.0) -> None:
        """Begin sending a Noop to every tracked loco every
        *interval* seconds. Idempotent -- a second call while one is
        already running is a no-op (does not change the interval)."""
        if self._noop_task is not None:
            return
        self._interval = interval
        self._noop_stop = asyncio.Event()
        self._noop_task = asyncio.create_task(
            self._noop_loop(), name="olcb-traction-noop",
        )

    async def stop_periodic_noop(self) -> None:
        """Cancel the periodic Noop task if running. Idempotent."""
        if self._noop_task is None:
            return
        assert self._noop_stop is not None
        self._noop_stop.set()
        self._noop_task.cancel()
        import contextlib  # noqa: PLC0415
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._noop_task
        self._noop_task = None
        self._noop_stop = None

    async def _on_heartbeat_request(self, req: HeartbeatRequest) -> None:
        if req.loco_alias not in self._locos:
            return
        # Immediate Noop -- the loco just told us "reply within N
        # seconds"; waiting even a tick is a needless risk.
        try:
            await self._client.send_noop(req.loco_alias)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "heartbeat noop to alias %#05x failed: %s",
                req.loco_alias, e,
            )

    async def _noop_loop(self) -> None:
        assert self._noop_stop is not None
        import contextlib  # noqa: PLC0415
        try:
            while not self._noop_stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._noop_stop.wait(), timeout=self._interval,
                    )
                if self._noop_stop.is_set():
                    return
                for alias in list(self._locos):
                    try:
                        await self._client.send_noop(alias)
                    except Exception as e:  # noqa: BLE001
                        _log.warning(
                            "periodic noop to alias %#05x failed: %s",
                            alias, e,
                        )
        except asyncio.CancelledError:
            return


# ---- typing shim --------------------------------------------------------


class _TcLike:
    """Same structural type used by the SNIP client -- see
    :class:`~jmri_openlcb.snip.SnipTrafficLike`."""
