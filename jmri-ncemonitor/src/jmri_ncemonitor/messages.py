"""Typed Cab Bus observation messages.

The NCE Cab Bus has many message types; this module covers the ones a
monitor user actually wants to see decoded. Anything else surfaces as
:class:`CabBusUnknown` carrying the raw bytes so callers don't lose it.

Addresses observed on the bus:

* ``0x00..0x3F`` (with high bit set on the wire -> ``0x80..0xBF``) -- cab IDs.
* ``0x60..0x7F`` -- fast clock, accessory packets, master traffic.
"""
from __future__ import annotations

from dataclasses import dataclass

from jmri_ncemonitor.codec import address_of, is_address_byte

# --- base + unknown ---------------------------------------------------------


class CabBusMessage:
    """Marker base for every decoded message."""
    address: int


@dataclass(frozen=True, slots=True)
class CabBusUnknown(CabBusMessage):
    """A well-formed frame whose body we don't have a typed decoder for."""
    address: int
    data: bytes  # raw bytes (excluding the address marker byte itself)


# --- cab traffic ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CabPoll(CabBusMessage):
    """Master polling cab *address* for its current state.

    Data is the 1-byte poll command (typically 0x00 = "send state").
    """
    address: int
    command: int


@dataclass(frozen=True, slots=True)
class CabResponse(CabBusMessage):
    """Cab *address* responding with its keyboard / encoder state.

    Fields are decoded from the standard 8-byte response payload:

    * ``key`` -- last key code pressed (0 if none)
    * ``speed`` -- encoder speed value 0..127
    * ``direction`` -- 0 = reverse, 1 = forward
    * ``function_low`` -- bit-packed F0..F4
    * ``function_high`` -- bit-packed F5..F8
    """
    address: int
    key: int
    speed: int
    direction: int
    function_low: int
    function_high: int


# --- master traffic ---------------------------------------------------------


# Master "fake address" used for non-cab messages on the bus.
ADDR_FAST_CLOCK = 0x60
ADDR_ACCESSORY  = 0x68
ADDR_AIU_UPDATE = 0x70


@dataclass(frozen=True, slots=True)
class FastClockUpdate(CabBusMessage):
    """Fast-clock packet observed on the bus."""
    address: int  # always ADDR_FAST_CLOCK
    hour: int
    minute: int


@dataclass(frozen=True, slots=True)
class AccessoryObserved(CabBusMessage):
    """An accessory-decoder command seen on the bus.

    The monitor captures the address that was thrown/closed and which way.
    """
    address: int  # always ADDR_ACCESSORY
    accessory_address: int
    closed: bool


@dataclass(frozen=True, slots=True)
class AiuUpdate(CabBusMessage):
    """An AIU input update observed on the bus."""
    address: int  # always ADDR_AIU_UPDATE
    aiu_module: int
    input_word: int  # 16-bit raw input word


# --- new observable messages (Stage 4.5) -----------------------------------


ADDR_CONSIST_CMD  = 0x79   # consist add/remove observed on master -> cab
ADDR_MACRO_CMD    = 0x7B   # accessory macro triggered observed
ADDR_MASTER_ACK   = 0x7C   # master acknowledgement of cab-sent command
ADDR_THROTTLE_CMD = 0x78   # master-side throttle commands
ADDR_PROG_RESP    = 0x7A   # programming response (ops + service mode)


@dataclass(frozen=True, slots=True)
class ThrottleObserved(CabBusMessage):
    """A throttle command seen on the bus.

    Observable form (master -> cab broadcast):

    * ``dcc_address`` -- loco address the command targets
    * ``speed`` -- 0..127 wire form, 1 = e-stop
    * ``forward`` -- direction bit
    * ``functions`` -- 5-bit F0..F4 mask (the common short form)
    """
    address: int   # always ADDR_THROTTLE_CMD
    dcc_address: int
    speed: int
    forward: bool
    functions: int   # F0..F4 packed


@dataclass(frozen=True, slots=True)
class ProgrammingResponse(CabBusMessage):
    """A response to a programming-track read seen on the bus.

    Carries the CV number being read/written and the value byte. The
    direction of the operation (read vs write) is encoded in the
    ``operation`` byte the CS sends along.
    """
    address: int   # always ADDR_PROG_RESP
    cv: int
    value: int
    operation: int   # 0 = read result, 1 = write ack, 2 = error


@dataclass(frozen=True, slots=True)
class ConsistObserved(CabBusMessage):
    """A consist add / remove observed on the bus.

    ``operation`` is 0 for add-to-consist, 1 for remove-from-consist,
    2 for kill-consist. Address bytes carry the consist number
    (1..127; matches the CV19 wire range).
    """
    address: int   # always ADDR_CONSIST_CMD
    consist_number: int
    loco_address: int
    operation: int


@dataclass(frozen=True, slots=True)
class MacroObserved(CabBusMessage):
    """A macro trigger observed on the bus.

    Macros are pre-programmed sequences of accessory operations the
    command station holds in memory. This message captures which
    macro number was fired.
    """
    address: int   # always ADDR_MACRO_CMD
    macro_number: int


@dataclass(frozen=True, slots=True)
class MasterAck(CabBusMessage):
    """An acknowledgement byte from the master to a cab.

    The master emits these one-byte ACKs after processing a cab
    command. ``code`` is the raw ack code -- non-zero values indicate
    an error condition specific to whatever the cab last sent.
    """
    address: int   # always ADDR_MASTER_ACK
    code: int


# --- decode -----------------------------------------------------------------


def decode(frame: bytes) -> CabBusMessage:
    """Decode one Cab Bus frame into a typed message.

    *frame* starts with the address byte (high bit set). Everything after
    is body. Returns :class:`CabBusUnknown` for unrecognized frames.
    """
    if not frame or not is_address_byte(frame[0]):
        raise ValueError("Cab Bus frame must start with an address byte")
    addr = address_of(frame[0])
    body = frame[1:]

    # Cab range
    if addr < 0x40:
        if len(body) == 1:
            return CabPoll(address=addr, command=body[0])
        # JMRI's Cab Bus reference documents a fixed 9-byte cab
        # response frame (5 state bytes + 3 pad + 1 checksum).
        # Require the full length so short frames fall through to
        # CabBusUnknown instead of silently succeeding on truncated
        # data -- the monitor operator wants to see truncation, not
        # a plausible-looking half-decoded message.
        if len(body) >= 9:
            return CabResponse(
                address=addr,
                key=body[0],
                speed=body[1] & 0x7F,
                direction=(body[2] >> 0) & 0x01,
                function_low=body[3] & 0x1F,
                function_high=body[4] & 0x0F,
            )

    # Master-side traffic.
    if addr == ADDR_FAST_CLOCK and len(body) >= 2:
        return FastClockUpdate(address=addr, hour=body[0] & 0x7F, minute=body[1] & 0x7F)

    if addr == ADDR_ACCESSORY and len(body) >= 3:
        accy_addr = ((body[0] & 0x7F) << 7) | (body[1] & 0x7F)
        closed = bool(body[2] & 0x01)
        return AccessoryObserved(
            address=addr, accessory_address=accy_addr + 1, closed=closed
        )

    if addr == ADDR_AIU_UPDATE and len(body) >= 3:
        return AiuUpdate(
            address=addr,
            aiu_module=body[0] & 0x7F,
            input_word=((body[1] & 0x7F) << 7) | (body[2] & 0x7F),
        )

    if addr == ADDR_THROTTLE_CMD and len(body) >= 4:
        return ThrottleObserved(
            address=addr,
            dcc_address=((body[0] & 0x7F) << 7) | (body[1] & 0x7F),
            speed=body[2] & 0x7F,
            forward=bool(body[3] & 0x40),
            functions=body[3] & 0x1F,
        )

    if addr == ADDR_PROG_RESP and len(body) >= 4:
        return ProgrammingResponse(
            address=addr,
            cv=((body[0] & 0x7F) << 7) | (body[1] & 0x7F),
            value=body[2] & 0xFF,
            operation=body[3] & 0x03,
        )

    if addr == ADDR_CONSIST_CMD and len(body) >= 4:
        return ConsistObserved(
            address=addr,
            consist_number=body[0] & 0x7F,
            loco_address=((body[1] & 0x7F) << 7) | (body[2] & 0x7F),
            operation=body[3] & 0x03,
        )

    if addr == ADDR_MACRO_CMD and len(body) >= 1:
        return MacroObserved(
            address=addr,
            macro_number=body[0] & 0x7F,
        )

    if addr == ADDR_MASTER_ACK and len(body) >= 1:
        return MasterAck(
            address=addr,
            code=body[0] & 0x7F,
        )

    return CabBusUnknown(address=addr, data=body)


def encode(msg: CabBusMessage) -> bytes:
    """Inverse of :func:`decode` -- used only by tests / synthetic playback.

    The real monitor never writes to the bus (it's passive); we provide
    ``encode`` to construct test fixtures that feed the monitor through a
    loopback transport.
    """
    if isinstance(msg, CabPoll):
        return bytes([0x80 | msg.address, msg.command])
    if isinstance(msg, CabResponse):
        # 9-byte body: 5 state bytes + 3 pad + 1 checksum. The
        # checksum is the XOR of the 5 state bytes on the wire -- we
        # emit it here so encode() -> decode() round-trips against
        # the tightened 9-byte length check in ``decode``.
        state = (
            msg.key & 0x7F,
            msg.speed & 0x7F,
            msg.direction & 0x7F,
            msg.function_low & 0x7F,
            msg.function_high & 0x7F,
        )
        checksum = 0
        for b in state:
            checksum ^= b
        return bytes([
            0x80 | msg.address,
            *state,
            0x00, 0x00, 0x00,
            checksum & 0x7F,
        ])
    if isinstance(msg, FastClockUpdate):
        return bytes([0x80 | ADDR_FAST_CLOCK, msg.hour & 0x7F, msg.minute & 0x7F])
    if isinstance(msg, AccessoryObserved):
        a = msg.accessory_address - 1
        return bytes([
            0x80 | ADDR_ACCESSORY,
            (a >> 7) & 0x7F,
            a & 0x7F,
            0x01 if msg.closed else 0x00,
        ])
    if isinstance(msg, AiuUpdate):
        return bytes([
            0x80 | ADDR_AIU_UPDATE,
            msg.aiu_module & 0x7F,
            (msg.input_word >> 7) & 0x7F,
            msg.input_word & 0x7F,
        ])
    if isinstance(msg, ThrottleObserved):
        return bytes([
            0x80 | ADDR_THROTTLE_CMD,
            (msg.dcc_address >> 7) & 0x7F,
            msg.dcc_address & 0x7F,
            msg.speed & 0x7F,
            (msg.functions & 0x1F) | (0x40 if msg.forward else 0),
        ])
    if isinstance(msg, ProgrammingResponse):
        return bytes([
            0x80 | ADDR_PROG_RESP,
            (msg.cv >> 7) & 0x7F,
            msg.cv & 0x7F,
            msg.value & 0x7F,
            msg.operation & 0x7F,
        ])
    if isinstance(msg, ConsistObserved):
        return bytes([
            0x80 | ADDR_CONSIST_CMD,
            msg.consist_number & 0x7F,
            (msg.loco_address >> 7) & 0x7F,
            msg.loco_address & 0x7F,
            msg.operation & 0x7F,
        ])
    if isinstance(msg, MacroObserved):
        return bytes([
            0x80 | ADDR_MACRO_CMD,
            msg.macro_number & 0x7F,
        ])
    if isinstance(msg, MasterAck):
        return bytes([
            0x80 | ADDR_MASTER_ACK,
            msg.code & 0x7F,
        ])
    if isinstance(msg, CabBusUnknown):
        return bytes([0x80 | msg.address]) + msg.data
    raise NotImplementedError(f"encode not implemented for {type(msg).__name__}")
