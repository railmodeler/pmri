"""OpenLCB Memory Configuration Protocol (S-9.7.4.2).

Memory Config sits on top of the datagram layer. Every request has a
1-byte protocol id (``0x20`` = Memory Configuration), a 1-byte
command, a 4-byte address, and a variable-length data / count field.
Replies mirror the request shape with a success/failure bit set in
the command byte.

Command byte layout::

    bits 6..7  = category (00 read, 01 write, 10 configuration)
    bits 4..5  = 0 for read/write; other values for config commands
    bit 3      = 0 = success reply, 1 = failure reply
                 (only meaningful on reply frames)
    bits 0..2  = space info; the low three bits encode one of the
                 well-known spaces (0xFD..0xFF -> 5..7) when we use
                 the compact form, or 0 to signal "explicit space
                 byte follows the address" for generic addressing.

We ship the compact form for the three commonly-used spaces
(``CDI = 0xFF``, ``All = 0xFD``, ``Config = 0xFE``) and the
generic form for anything else.

Reads are capped at 64 bytes per request by the protocol; larger
transfers are split into 64-byte chunks by :meth:`MemoryClient.read`.
Writes obey the datagram cap (72 bytes total minus 6/7 bytes of
header -> ~64 bytes of payload); the client fragments similarly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pmri_openlcb.datagrams import Datagram, DatagramEngine

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# ---- constants ---------------------------------------------------------

MEMORY_CONFIG = 0x20   # protocol id shared by every request/reply

# Well-known memory spaces per S-9.7.4.2.
SPACE_CDI = 0xFF
SPACE_ALL = 0xFD
SPACE_CONFIG = 0xFE

# Read/write are capped by the datagram size (72 bytes) minus the
# protocol/command/address header (6 bytes for compact-space form,
# 7 bytes for explicit-space form). 64 bytes is the conservative
# maximum every node must honour.
MAX_MEMORY_CHUNK = 64

# ---- lifecycle commands (S-9.10.1 sec.3.3) ---------------------------------
#
# CROSS-CHECK BEFORE BETA: these constants come from S-9.10.1 sec.3.3
# "Command Set". Any decoder that answers Memory Config today
# probably lands on the same values, but re-verify with the vendor
# decoder table when hardware testing starts.
#
# Freeze/Unfreeze pause a specific space during firmware download;
# Update Complete signals "download finished, ready to apply"; Reboot
# and Factory Reset are self-explanatory. Factory Reset carries the
# target's 6-byte NodeID as a safety payload so a stray click can't
# reset a random peer (sec.3.3, "Reinitialize Node").
CMD_UNFREEZE = 0xA0
CMD_FREEZE = 0xA1
CMD_UPDATE_COMPLETE = 0xA8
CMD_REBOOT = 0xA9
CMD_FACTORY_RESET = 0xAA


# ---- command-byte helpers ----------------------------------------------


def _compact_space_bits(space: int) -> int | None:
    """Compact-form encoding maps 0xFD -> 5, 0xFE -> 6, 0xFF -> 7.

    Any other space uses the generic form (explicit space byte after
    the address); returns ``None`` in that case.
    """
    if space == 0xFD:
        return 0b101
    if space == 0xFE:
        return 0b110
    if space == 0xFF:
        return 0b111
    return None


def _encode_read_command(space: int) -> tuple[int, bool]:
    """Return ``(command_byte, uses_explicit_space)`` for a read
    request targeting *space*."""
    compact = _compact_space_bits(space)
    if compact is not None:
        return (0b01000000 | compact, False)
    return (0b01000000, True)


def _encode_write_command(space: int) -> tuple[int, bool]:
    compact = _compact_space_bits(space)
    if compact is not None:
        return (0b00000000 | compact, False)
    return (0b00000000, True)


def _coerce_node_id_bytes(nid) -> bytes:  # noqa: ANN001
    """Accept either a :class:`~pmri_openlcb.alias.NodeId` or raw
    6-byte bytes and return the wire form. Anything else raises."""
    if isinstance(nid, bytes | bytearray):
        if len(nid) != 6:
            raise ValueError(
                f"NodeID must be exactly 6 bytes, got {len(nid)}",
            )
        return bytes(nid)
    # Duck-type NodeId -- it exposes ``.bytes``.
    b = getattr(nid, "bytes", None)
    if isinstance(b, bytes) and len(b) == 6:
        return b
    raise TypeError(
        f"target_node_id must be NodeId or 6 bytes, got {type(nid).__name__}",
    )


def _is_read_reply(command: int) -> bool:
    return (command & 0xF0) == 0x50


def _is_write_reply(command: int) -> bool:
    return (command & 0xF0) == 0x10


def _is_failure_reply(command: int) -> bool:
    return bool(command & 0x08)


# ---- exceptions --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryError_(Exception):
    """Raised when a node returns a failure reply for a memory op.

    Name has an underscore so it doesn't shadow Python's built-in.
    """

    error_code: int

    def __str__(self) -> str:
        return f"memory operation failed (code {self.error_code:#06x})"


# ---- client ------------------------------------------------------------


class MemoryClient:
    """Convenience API over the memory-config protocol.

    Wraps a :class:`~pmri_openlcb.datagrams.DatagramEngine` so callers
    talk to remote nodes in terms of ``read(space, address, count)`` /
    ``write(space, address, data)`` and get bytes / None back
    respectively. Failures raise :class:`MemoryError_`.

    Each client instance registers ONE inbound listener on the
    datagram engine and dispatches incoming replies to whatever
    request is waiting. That means callers can await multiple
    read/write operations against the same peer serially -- a single
    outstanding operation per peer is the assumption (matches the
    protocol's implicit half-duplex nature at the memory layer).
    """

    def __init__(self, engine: DatagramEngine) -> None:
        self._engine = engine
        self._pending: dict[int, asyncio.Future[bytes]] = {}
        self._unsubscribe = engine.inbound.connect(self._on_inbound)

    def close(self) -> None:
        self._unsubscribe()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("memory client closed"))
        self._pending.clear()

    async def read(
        self,
        peer_alias: int,
        space: int,
        address: int,
        count: int,
        *,
        timeout: float = 3.0,
    ) -> bytes:
        """Read *count* bytes starting at *address* in *space* from
        the peer. Automatically splits reads over
        :data:`MAX_MEMORY_CHUNK` into multiple requests.
        """
        if count < 0:
            raise ValueError(f"count must be non-negative: {count}")
        chunks: list[bytes] = []
        remaining = count
        cursor = address
        while remaining > 0:
            batch = min(remaining, MAX_MEMORY_CHUNK)
            chunk = await self._read_one(
                peer_alias, space, cursor, batch, timeout=timeout,
            )
            chunks.append(chunk)
            # A short reply signals end-of-space; stop early so we
            # don't spin at the peer's expense.
            if len(chunk) < batch:
                break
            cursor += batch
            remaining -= batch
        return b"".join(chunks)

    async def write(
        self,
        peer_alias: int,
        space: int,
        address: int,
        data: bytes,
        *,
        timeout: float = 3.0,
    ) -> None:
        """Write *data* starting at *address* in *space* on the peer.
        Split into :data:`MAX_MEMORY_CHUNK`-byte writes."""
        cursor = address
        pos = 0
        while pos < len(data):
            batch = data[pos:pos + MAX_MEMORY_CHUNK]
            await self._write_one(
                peer_alias, space, cursor, batch, timeout=timeout,
            )
            cursor += len(batch)
            pos += len(batch)

    # ---- lifecycle commands (S-9.10.1 sec.3.3) --------------------------

    async def reboot(
        self, peer_alias: int, *, timeout: float = 3.0,
    ) -> None:
        """Send the Reboot upcode (0xA9). Target restarts. Datagram
        Received OK confirms the target accepted the request; the
        peer typically drops off the bus immediately after."""
        await self._send_lifecycle(peer_alias, CMD_REBOOT, timeout=timeout)

    async def factory_reset(
        self,
        peer_alias: int,
        target_node_id: "NodeIdLike",  # noqa: F821 - forward ref
        *,
        timeout: float = 3.0,
    ) -> None:
        """Send the Factory Reset upcode (0xAA) carrying the target's
        6-byte NodeID as a safety payload. Per S-9.10.1 sec.3.3, the
        target rejects the command if the payload doesn't match its
        own NodeID -- protects against accidental cross-node resets.

        Accepts either a :class:`NodeId` or raw 6-byte bytes.
        """
        nid_bytes = _coerce_node_id_bytes(target_node_id)
        body = bytes([MEMORY_CONFIG, CMD_FACTORY_RESET]) + nid_bytes
        await self._engine.send(peer_alias, body, timeout=timeout)

    async def freeze(
        self, peer_alias: int, space: int, *, timeout: float = 3.0,
    ) -> None:
        """Freeze a memory space (0xA1). The target stops accepting
        reads/writes on *space* until unfreeze -- used during firmware
        download to keep application code from seeing half-flashed
        state."""
        body = bytes([MEMORY_CONFIG, CMD_FREEZE, space & 0xFF])
        await self._engine.send(peer_alias, body, timeout=timeout)

    async def unfreeze(
        self, peer_alias: int, space: int, *, timeout: float = 3.0,
    ) -> None:
        """Unfreeze a memory space (0xA0). Pair with :meth:`freeze`."""
        body = bytes([MEMORY_CONFIG, CMD_UNFREEZE, space & 0xFF])
        await self._engine.send(peer_alias, body, timeout=timeout)

    async def update_complete(
        self, peer_alias: int, *, timeout: float = 3.0,
    ) -> None:
        """Update Complete upcode (0xA8) -- signals the end of a
        firmware upload. Typical sequence: freeze -> write -> unfreeze
        -> update_complete -> reboot."""
        await self._send_lifecycle(
            peer_alias, CMD_UPDATE_COMPLETE, timeout=timeout,
        )

    async def _send_lifecycle(
        self, peer_alias: int, command: int, *, timeout: float,
    ) -> None:
        body = bytes([MEMORY_CONFIG, command])
        await self._engine.send(peer_alias, body, timeout=timeout)

    # ---- one-shot request path ----------------------------------------

    async def _read_one(
        self, peer_alias: int, space: int, address: int, count: int,
        *, timeout: float,
    ) -> bytes:
        cmd, explicit = _encode_read_command(space)
        body = self._build_request(cmd, address, explicit, space)
        # count byte last; single byte, 1..64.
        body += bytes([count & 0xFF])
        return await self._request(peer_alias, body, timeout=timeout)

    async def _write_one(
        self, peer_alias: int, space: int, address: int, data: bytes,
        *, timeout: float,
    ) -> None:
        cmd, explicit = _encode_write_command(space)
        body = self._build_request(cmd, address, explicit, space) + data
        await self._request(peer_alias, body, timeout=timeout)

    def _build_request(
        self, command: int, address: int, explicit_space: bool, space: int,
    ) -> bytes:
        addr_bytes = address.to_bytes(4, "big")
        body = bytes([MEMORY_CONFIG, command]) + addr_bytes
        if explicit_space:
            body += bytes([space & 0xFF])
        return body

    async def _request(
        self, peer_alias: int, body: bytes, *, timeout: float,
    ) -> bytes:
        """Send *body* as a datagram and await the peer's memory-config
        reply (which arrives as an inbound datagram after the peer's
        Received-OK)."""
        if peer_alias in self._pending:
            raise RuntimeError(
                f"a memory op to alias {peer_alias:#x} is already in flight",
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        self._pending[peer_alias] = fut
        try:
            # The datagram engine's send() awaits Received OK before
            # returning; the peer's memory-config *reply* arrives as
            # a subsequent inbound datagram.
            await self._engine.send(peer_alias, body, timeout=timeout)
            data = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(peer_alias, None)
        return data

    # ---- inbound reply handler ----------------------------------------

    async def _on_inbound(self, dg: Datagram) -> None:
        if len(dg.data) < 6:
            return
        if dg.data[0] != MEMORY_CONFIG:
            return
        command = dg.data[1]
        # We match on peer alias (source of the datagram).
        fut = self._pending.get(dg.source_alias)
        if fut is None or fut.done():
            return
        if _is_failure_reply(command):
            code = 0
            # Optional 2-byte error code follows the address (6 bytes in).
            if len(dg.data) >= 8:
                code = (dg.data[6] << 8) | dg.data[7]
            fut.set_exception(MemoryError_(error_code=code))
            return
        if _is_read_reply(command):
            # Layout: proto, cmd, addr(4), [space], data...
            # Compact spaces skip the extra space byte.
            compact = _compact_space_bits_from_command(command)
            data_start = 6 if compact is not None else 7
            fut.set_result(bytes(dg.data[data_start:]))
            return
        if _is_write_reply(command):
            # Write success carries no body; empty bytes signals done.
            fut.set_result(b"")


def _compact_space_bits_from_command(command: int) -> int | None:
    """Decode the low 3 bits of a reply command byte back into a
    space number, or ``None`` for generic (explicit-space) form."""
    space_bits = command & 0b111
    if space_bits == 0b101:
        return 0xFD
    if space_bits == 0b110:
        return 0xFE
    if space_bits == 0b111:
        return 0xFF
    return None


# ---- responder ---------------------------------------------------------


# Failure error codes per S-9.7.4.2 sec.5.5 -- small subset we actually
# emit; a real spec-complete responder covers many more.
FAIL_PERMANENT = 0x1000   # generic "no can do" -- spec says this is
                           # the safe default for "unspecified error"
FAIL_UNKNOWN_SPACE = 0x1082    # requested space not implemented
FAIL_OUT_OF_BOUNDS = 0x1083    # address past end of space
FAIL_READ_ONLY = 0x1084        # write to a read-only space


class MemoryConfigResponder:
    """Serve inbound memory-config reads/writes for one alias.

    Backing store is per-space (``dict[space, bytearray]``); each
    space is a flat byte array indexed by the on-wire address. A
    caller pre-loads spaces via :meth:`set_space` (typically
    ``SPACE_CDI`` = 0xFF with an XML blob, ``SPACE_CONFIG`` = 0xFE
    with initial-value bytes, ``SPACE_FDI`` = 0xFA when applicable).

    Reads that fall past a space's end return the bytes available up
    to the boundary and a short-payload signal (matches how
    :meth:`MemoryClient.read` interprets it -- end-of-space is a
    normal condition, not an error). Reads targeting a space we
    haven't declared return a failure reply with
    :data:`FAIL_UNKNOWN_SPACE`.

    Writes clip the same way -- bytes past the space's end are
    silently dropped rather than expanding the space; callers who
    want a growable space allocate a larger initial buffer. Writes
    to a space marked read-only fail with :data:`FAIL_READ_ONLY`.

    ``target_alias`` -- the alias this responder serves. Defaults to
    the TC's primary alias (our own node). :class:`~CommandStationNode`
    passes each hosted train's alias so the responder answers on
    behalf of that virtual node. Replies are sourced from the same
    alias -- a peer talking to a hosted train alias sees the reply
    come from that train, not from our root node.

    Attach to a :class:`DatagramEngine`; the engine routes inbound
    datagrams to whichever alias(es) it's been told to accept, and
    the responder further filters by target_alias. Detach via
    :meth:`close`.
    """

    def __init__(
        self,
        engine: DatagramEngine,
        *,
        target_alias: int | None = None,
        on_write: (
            Callable[[int, int, bytes], Awaitable[None]] | None
        ) = None,
        on_reboot: Callable[[], Awaitable[None]] | None = None,
        on_factory_reset: (
            Callable[[bytes], Awaitable[None]] | None
        ) = None,
        our_node_id_bytes: bytes | None = None,
    ) -> None:
        """*on_write* fires after a successful write with
        ``(space, offset, data_written)``. Called AFTER the reply is
        sent, so a slow callback doesn't hold the peer up. Used by
        :class:`~CommandStationNode` to persist user_name /
        user_description edits without polling.

        *on_reboot* -- invoked when a peer sends the Reboot upcode
        (0xA9). Typically the caller re-initializes state or triggers
        a config reload; the datagram engine still ACKs regardless so
        the peer's send() returns cleanly.

        *on_factory_reset* -- invoked with the payload NodeID bytes
        when a peer sends the Factory Reset upcode (0xAA). Called
        only when *our_node_id_bytes* is set AND the payload matches
        (per S-9.10.1 sec.3.3 the target rejects mismatched Factory
        Reset requests as a safety guard).

        *our_node_id_bytes* -- 6-byte NodeID for this responder's
        node. Required for :attr:`on_factory_reset` to fire; leave
        unset and the upcode is silently ACKed without any hook
        running.
        """
        self._engine = engine
        self._target_alias = target_alias
        self._on_write = on_write
        self._on_reboot = on_reboot
        self._on_factory_reset = on_factory_reset
        self._our_nid_bytes = our_node_id_bytes
        # frozen: per-space "read/write is currently suppressed"
        # flag set by inbound Freeze (0xA1) commands, cleared by
        # Unfreeze (0xA0). Firmware download workflow only.
        self._frozen: set[int] = set()
        self._spaces: dict[int, bytearray] = {}
        self._read_only: set[int] = set()
        self._unsubscribe = engine.inbound.connect(self._on_datagram)

    def is_frozen(self, space: int) -> bool:
        """Whether *space* is currently frozen. Reads and writes on a
        frozen space return :data:`FAIL_PERMANENT` per the firmware-
        upgrade sub-protocol."""
        return space in self._frozen

    def _serves_alias(self) -> int:
        """Return the alias this responder answers for -- the caller-
        provided target or the TC's primary alias by default."""
        if self._target_alias is not None:
            return self._target_alias
        return self._engine._tc.source_alias  # noqa: SLF001

    def close(self) -> None:
        self._unsubscribe()

    # ---- backing store ----------------------------------------------

    def set_space(
        self, space: int, data: bytes, *, read_only: bool = False,
    ) -> None:
        """Install *data* as space *space*'s contents. Setting a
        space that already exists overwrites it wholesale -- useful
        for hot-reloading a CDI after the operator saves changes."""
        self._spaces[space] = bytearray(data)
        if read_only:
            self._read_only.add(space)
        else:
            self._read_only.discard(space)

    def get_space(self, space: int) -> bytes | None:
        """Return the current contents of *space*, or ``None`` when
        undeclared. Useful for tests to observe write side effects
        without having to go through a datagram round-trip."""
        buf = self._spaces.get(space)
        return bytes(buf) if buf is not None else None

    def has_space(self, space: int) -> bool:
        return space in self._spaces

    # ---- inbound dispatch --------------------------------------------

    async def _on_datagram(self, dg: Datagram) -> None:
        # Filter by target alias -- the engine may be routing datagrams
        # for multiple aliases (our own plus every CS-hosted train),
        # but each responder only serves one.
        if dg.dest_alias != self._serves_alias():
            return
        if len(dg.data) < 2 or dg.data[0] != MEMORY_CONFIG:
            return
        command = dg.data[1]

        # Lifecycle upcodes (S-9.10.1 sec.3.3): reboot / factory-reset /
        # freeze / unfreeze / update-complete. These don't carry the
        # address+space header of read/write, so they get dispatched
        # before the family check below.
        if command == CMD_REBOOT:
            await self._handle_reboot()
            return
        if command == CMD_FACTORY_RESET:
            await self._handle_factory_reset(dg.data[2:])
            return
        if command == CMD_FREEZE and len(dg.data) >= 3:
            self._frozen.add(dg.data[2])
            return
        if command == CMD_UNFREEZE and len(dg.data) >= 3:
            self._frozen.discard(dg.data[2])
            return
        if command == CMD_UPDATE_COMPLETE:
            # No-op at the responder layer; callers who care about
            # the download-complete signal add their own listener on
            # the datagram engine. The datagram itself is already
            # ACKed by the engine before we get here.
            return

        # Only handle read (0x40) and write (0x00) command families
        # below; other config-space commands (options query, space
        # info query, etc.) fall through to a no-op -- TODO if a peer
        # needs them.
        family = command & 0xF0
        if family not in (0x00, 0x40):
            return
        if len(dg.data) < 6:
            return
        addr = int.from_bytes(dg.data[2:6], "big")
        # Space encoding: compact form when low 3 bits map to a
        # well-known space; otherwise the byte after the address
        # names the space explicitly.
        compact_bits = command & 0b111
        space: int
        payload_start: int
        if compact_bits == 0b101:
            space = 0xFD
            payload_start = 6
        elif compact_bits == 0b110:
            space = 0xFE
            payload_start = 6
        elif compact_bits == 0b111:
            space = 0xFF
            payload_start = 6
        else:
            if len(dg.data) < 7:
                return
            space = dg.data[6]
            payload_start = 7

        if family == 0x40:
            count = dg.data[payload_start] if payload_start < len(dg.data) else 0
            await self._handle_read(
                dg.source_alias, command, addr, space, count,
            )
            return
        # family == 0x00 -> write
        payload = bytes(dg.data[payload_start:])
        await self._handle_write(
            dg.source_alias, command, addr, space, payload,
        )

    # ---- lifecycle handlers -----------------------------------------

    async def _handle_reboot(self) -> None:
        if self._on_reboot is not None:
            try:
                await self._on_reboot()
            except Exception:
                _log.exception("on_reboot callback failed")

    async def _handle_factory_reset(self, payload: bytes) -> None:
        """Per S-9.10.1 sec.3.3, the target rejects the Factory Reset if
        the payload NodeID doesn't match its own. We enforce the same
        guard so a peer scanning the bus can't reset us by accident.
        """
        if self._our_nid_bytes is None:
            # No NID configured -> we don't own an identity to match
            # against, so treat as no-op rather than silently
            # accepting.
            return
        if len(payload) < 6 or payload[:6] != self._our_nid_bytes:
            return
        if self._on_factory_reset is not None:
            try:
                await self._on_factory_reset(payload[:6])
            except Exception:
                _log.exception("on_factory_reset callback failed")

    # ---- reads / writes ---------------------------------------------

    async def _handle_read(
        self, peer_alias: int, command: int,
        addr: int, space: int, count: int,
    ) -> None:
        if space in self._frozen:
            await self._send_failure(
                peer_alias, 0x50, command, addr, space, FAIL_PERMANENT,
            )
            return
        buf = self._spaces.get(space)
        if buf is None:
            await self._send_failure(
                peer_alias, 0x50, command, addr, space, FAIL_UNKNOWN_SPACE,
            )
            return
        if addr > len(buf):
            await self._send_failure(
                peer_alias, 0x50, command, addr, space, FAIL_OUT_OF_BOUNDS,
            )
            return
        # Short read at end-of-space isn't an error; return whatever
        # bytes are available up to the boundary.
        end = min(addr + count, len(buf))
        data = bytes(buf[addr:end])
        reply = self._build_reply(0x50, command, addr, space, data)
        await self._send_datagram(peer_alias, reply)

    async def _handle_write(
        self, peer_alias: int, command: int,
        addr: int, space: int, payload: bytes,
    ) -> None:
        if space in self._frozen:
            await self._send_failure(
                peer_alias, 0x10, command, addr, space, FAIL_PERMANENT,
            )
            return
        buf = self._spaces.get(space)
        if buf is None:
            await self._send_failure(
                peer_alias, 0x10, command, addr, space, FAIL_UNKNOWN_SPACE,
            )
            return
        if space in self._read_only:
            await self._send_failure(
                peer_alias, 0x10, command, addr, space, FAIL_READ_ONLY,
            )
            return
        if addr > len(buf):
            await self._send_failure(
                peer_alias, 0x10, command, addr, space, FAIL_OUT_OF_BOUNDS,
            )
            return
        end = min(addr + len(payload), len(buf))
        written = payload[: end - addr]
        buf[addr:end] = written
        # Success ack -- write reply command 0x10 | space_bits + address.
        reply = self._build_reply(0x10, command, addr, space, b"")
        await self._send_datagram(peer_alias, reply)
        # Notify observers AFTER the reply is on the wire so the peer
        # isn't held up by a slow callback. Failures are logged and
        # swallowed -- a persistence hiccup shouldn't wedge future
        # writes.
        if self._on_write is not None and written:
            try:
                await self._on_write(space, addr, bytes(written))
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "memory-config on_write callback raised: %s", e,
                )

    # ---- reply-frame construction -----------------------------------

    def _build_reply(
        self, base_cmd: int, request_cmd: int, addr: int,
        space: int, payload: bytes,
    ) -> bytes:
        """Reply mirrors the request's low 3 bits so the client
        knows which space it was."""
        compact_bits = request_cmd & 0b111
        cmd = base_cmd | compact_bits
        body = bytes([MEMORY_CONFIG, cmd]) + addr.to_bytes(4, "big")
        if compact_bits not in (0b101, 0b110, 0b111):
            body += bytes([space])
        body += payload
        return body

    async def _send_failure(
        self, peer_alias: int,
        base_cmd: int, request_cmd: int, addr: int,
        space: int, error_code: int,
    ) -> None:
        """Failure replies set bit 3 (0x08) of the command byte;
        the 2-byte error code follows the address (+ optional space
        byte). Matches how :meth:`MemoryClient._on_inbound` decodes
        failures via :data:`_is_failure_reply`."""
        compact_bits = request_cmd & 0b111
        cmd = base_cmd | compact_bits | 0x08
        body = bytes([MEMORY_CONFIG, cmd]) + addr.to_bytes(4, "big")
        if compact_bits not in (0b101, 0b110, 0b111):
            body += bytes([space])
        body += error_code.to_bytes(2, "big")
        await self._send_datagram(peer_alias, body)

    async def _send_datagram(
        self, dest_alias: int, body: bytes,
    ) -> None:
        """Send a datagram from :meth:`_serves_alias` to *dest_alias*.

        Bypasses :meth:`DatagramEngine.send` -- that method sources
        every datagram from ``tc.source_alias``, which is wrong for
        CS-hosted train aliases. We build the raw frames ourselves
        using ``make_datagram_header`` so the peer sees the reply
        coming back from the same alias it addressed.

        Reply datagrams don't need the received-OK/rejected round-
        trip (the peer is expecting our reply, not asking for an
        ack), so this is fire-and-forget.
        """
        from pmri_openlcb.datagrams import (  # noqa: PLC0415
            DATAGRAM_COMPLETE,
            DATAGRAM_FINAL,
            DATAGRAM_FIRST,
            DATAGRAM_MIDDLE,
            make_datagram_header,
        )
        from pmri_openlcb.codec import OlcbFrame  # noqa: PLC0415
        source = self._serves_alias()
        if len(body) <= 8:
            frame = OlcbFrame(
                header=make_datagram_header(
                    DATAGRAM_COMPLETE, dest_alias, source,
                ),
                data=body,
            )
            await self._engine._tc.send_frame(frame)  # noqa: SLF001
            return
        chunks = [body[i:i + 8] for i in range(0, len(body), 8)]
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            if i == 0:
                kind = DATAGRAM_FIRST
            elif i == last:
                kind = DATAGRAM_FINAL
            else:
                kind = DATAGRAM_MIDDLE
            frame = OlcbFrame(
                header=make_datagram_header(kind, dest_alias, source),
                data=chunk,
            )
            await self._engine._tc.send_frame(frame)  # noqa: SLF001
