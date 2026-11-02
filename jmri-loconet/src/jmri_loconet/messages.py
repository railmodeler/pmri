"""Typed LocoNet message dataclasses + encode/decode dispatch.

Each opcode JMRI's ``LnConstants`` declares maps to one frozen dataclass.
:func:`decode` turns frame bytes into the matching dataclass;
:func:`encode` does the reverse (returns frame bytes including checksum).

Unrecognized but well-formed frames decode to :class:`OpcUnknown` so
listeners can still observe them -- JMRI exposes everything on the bus.
"""
from __future__ import annotations

from dataclasses import dataclass

from jmri_loconet.codec import append_checksum, compute_checksum, verify_checksum
from jmri_loconet.opcodes import (
    OPC_GPOFF,
    OPC_GPON,
    OPC_IDLE,
    OPC_INPUT_REP,
    OPC_LINK_SLOTS,
    OPC_LOCO_ADR,
    OPC_LOCO_DIRF,
    OPC_LOCO_SND,
    OPC_LOCO_SPD,
    OPC_LONG_ACK,
    OPC_MOVE_SLOTS,
    OPC_RQ_SL_DATA,
    OPC_SL_RD_DATA,
    OPC_SLOT_STAT1,
    OPC_SW_REP,
    OPC_SW_REQ,
    OPC_UNLINK_SLOTS,
    OPC_WR_SL_DATA,
)

# ---------- base + unknown ---------------------------------------------------


class LocoNetMessage:
    """Marker base type. Subclasses are frozen dataclasses."""
    opcode: int


@dataclass(frozen=True, slots=True)
class OpcUnknown(LocoNetMessage):
    """A well-formed frame whose opcode we don't have a typed wrapper for."""
    opcode: int
    payload: bytes  # everything between opcode and checksum (exclusive)


# ---------- 2-byte messages --------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpcGpOn(LocoNetMessage):
    opcode: int = OPC_GPON


@dataclass(frozen=True, slots=True)
class OpcGpOff(LocoNetMessage):
    opcode: int = OPC_GPOFF


@dataclass(frozen=True, slots=True)
class OpcIdle(LocoNetMessage):
    opcode: int = OPC_IDLE


# ---------- 4-byte messages --------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpcSwReq(LocoNetMessage):
    """Switch request -- throw or close a turnout.

    Address is 1..2048 (LocoNet packs it across two bytes with bit layout
    that JMRI documents explicitly).
    """
    address: int
    closed: bool       # True = closed direction, False = thrown
    output_on: bool    # True = energize, False = de-energize
    opcode: int = OPC_SW_REQ


@dataclass(frozen=True, slots=True)
class OpcSwRep(LocoNetMessage):
    """Switch report -- turnout state feedback from the layout."""
    address: int
    closed: bool
    thrown: bool
    opcode: int = OPC_SW_REP


@dataclass(frozen=True, slots=True)
class OpcInputRep(LocoNetMessage):
    """Sensor input report."""
    address: int
    active: bool
    opcode: int = OPC_INPUT_REP


@dataclass(frozen=True, slots=True)
class OpcLongAck(LocoNetMessage):
    """Long-form acknowledgment -- sent in response to many request opcodes."""
    responding_opcode: int
    ack_code: int
    opcode: int = OPC_LONG_ACK


@dataclass(frozen=True, slots=True)
class OpcSlotStat1(LocoNetMessage):
    slot: int
    stat: int
    opcode: int = OPC_SLOT_STAT1


@dataclass(frozen=True, slots=True)
class OpcMoveSlots(LocoNetMessage):
    """Move slot src -> dest. Same src+dest=null move (refresh); 0=dispatch."""
    src: int
    dest: int
    opcode: int = OPC_MOVE_SLOTS


@dataclass(frozen=True, slots=True)
class OpcLinkSlots(LocoNetMessage):
    """Link ``src`` as a follower of ``dest`` -- build a consist chain.

    After a LINK, the command station broadcasts speed and direction
    commands from *dest* to *src* automatically; the client only needs
    to drive the lead loco. Functions remain per-loco.
    """
    src: int
    dest: int
    opcode: int = OPC_LINK_SLOTS


@dataclass(frozen=True, slots=True)
class OpcUnlinkSlots(LocoNetMessage):
    """Break the LINK between *src* and *dest*.

    Ordering: *dest* is the slot higher in the chain, *src* is the
    follower being detached. After UNLINK, *src* is a standalone slot
    again and stops receiving speed/direction fan-out from *dest*.
    """
    src: int
    dest: int
    opcode: int = OPC_UNLINK_SLOTS


@dataclass(frozen=True, slots=True)
class OpcRqSlData(LocoNetMessage):
    """Request slot data -- server replies with OpcSlRdData."""
    slot: int
    opcode: int = OPC_RQ_SL_DATA


@dataclass(frozen=True, slots=True)
class OpcLocoAdr(LocoNetMessage):
    """Request the slot currently holding *address*.

    Long addresses use the high byte; short addresses set high=0.
    """
    address: int
    opcode: int = OPC_LOCO_ADR


@dataclass(frozen=True, slots=True)
class OpcLocoSpd(LocoNetMessage):
    """Set speed for a slot. ``speed`` is 0..127."""
    slot: int
    speed: int
    opcode: int = OPC_LOCO_SPD


@dataclass(frozen=True, slots=True)
class OpcLocoDirF(LocoNetMessage):
    """Set direction + F0..F4 for a slot.

    ``dirf`` is the wire byte: bit 5 = direction (0 = forward), bits 0..4 = F0..F4.
    """
    slot: int
    dirf: int
    opcode: int = OPC_LOCO_DIRF


@dataclass(frozen=True, slots=True)
class OpcLocoSnd(LocoNetMessage):
    """Set F5..F8 for a slot. ``snd`` packs F5..F8 in bits 0..3."""
    slot: int
    snd: int
    opcode: int = OPC_LOCO_SND


# ---------- variable-length messages -----------------------------------------


@dataclass(frozen=True, slots=True)
class OpcSlRdData(LocoNetMessage):
    """Slot read data -- 14-byte response carrying full slot state.

    ``ss2`` (byte index 8 on the wire) holds the "STAT2" byte for
    regular slots, and the encoded hours field for the fast-clock
    slot (123). It defaults to 0 so existing callers don't need to
    care; the clock module fills it in.
    """
    slot: int
    stat: int
    address: int        # composed from ADR + ADR2
    speed: int
    dirf: int
    trk: int
    snd: int
    ss2: int = 0
    opcode: int = OPC_SL_RD_DATA


@dataclass(frozen=True, slots=True)
class OpcWrSlData(LocoNetMessage):
    """Write slot data -- same shape as OpcSlRdData but client -> master."""
    slot: int
    stat: int
    address: int
    speed: int
    dirf: int
    trk: int
    snd: int
    ss2: int = 0
    opcode: int = OPC_WR_SL_DATA


# ---------- decode -----------------------------------------------------------


def decode(frame: bytes) -> LocoNetMessage:
    """Turn a complete, checksum-verified frame into a typed message.

    Raises :class:`ValueError` if the checksum is invalid.
    """
    if not verify_checksum(frame):
        raise ValueError("LocoNet frame has invalid checksum")
    op = frame[0]

    # 2-byte
    if op == OPC_GPON:
        return OpcGpOn()
    if op == OPC_GPOFF:
        return OpcGpOff()
    if op == OPC_IDLE:
        return OpcIdle()

    # 4-byte: layout is [op, b1, b2, cs]; b1/b2 vary per opcode.
    if op == OPC_SW_REQ:
        return OpcSwReq(
            address=_decode_switch_address(frame[1], frame[2]),
            closed=bool(frame[2] & 0x20),
            output_on=bool(frame[2] & 0x10),
        )
    if op == OPC_SW_REP:
        return OpcSwRep(
            address=_decode_switch_address(frame[1], frame[2]),
            closed=bool(frame[2] & 0x20),
            thrown=not bool(frame[2] & 0x20),
        )
    if op == OPC_INPUT_REP:
        # JMRI's mapping:
        #   pair = ((SN2 & 0x0F) << 7) | (SN1 & 0x7F)
        #   sensor (1-based) = pair * 2 + ((SN2 & 0x20) >> 5) + 1
        pair = ((frame[2] & 0x0F) << 7) | (frame[1] & 0x7F)
        sensor_addr = (pair * 2) + ((frame[2] & 0x20) >> 5) + 1
        active = bool(frame[2] & 0x10)
        return OpcInputRep(address=sensor_addr, active=active)
    if op == OPC_LONG_ACK:
        return OpcLongAck(responding_opcode=frame[1] | 0x80, ack_code=frame[2])
    if op == OPC_SLOT_STAT1:
        return OpcSlotStat1(slot=frame[1], stat=frame[2])
    if op == OPC_MOVE_SLOTS:
        return OpcMoveSlots(src=frame[1], dest=frame[2])
    if op == OPC_LINK_SLOTS:
        return OpcLinkSlots(src=frame[1], dest=frame[2])
    if op == OPC_UNLINK_SLOTS:
        return OpcUnlinkSlots(src=frame[1], dest=frame[2])
    if op == OPC_RQ_SL_DATA:
        return OpcRqSlData(slot=frame[1])
    if op == OPC_LOCO_ADR:
        return OpcLocoAdr(address=(frame[1] << 7) | frame[2])
    if op == OPC_LOCO_SPD:
        return OpcLocoSpd(slot=frame[1], speed=frame[2])
    if op == OPC_LOCO_DIRF:
        return OpcLocoDirF(slot=frame[1], dirf=frame[2])
    if op == OPC_LOCO_SND:
        return OpcLocoSnd(slot=frame[1], snd=frame[2])

    # Variable-length: 14-byte slot data frames.
    if op == OPC_SL_RD_DATA and len(frame) == 14:
        return OpcSlRdData(
            slot=frame[2], stat=frame[3],
            address=(frame[9] << 7) | frame[4],
            speed=frame[5], dirf=frame[6], trk=frame[7],
            ss2=frame[8], snd=frame[10],
        )
    if op == OPC_WR_SL_DATA and len(frame) == 14:
        return OpcWrSlData(
            slot=frame[2], stat=frame[3],
            address=(frame[9] << 7) | frame[4],
            speed=frame[5], dirf=frame[6], trk=frame[7],
            ss2=frame[8], snd=frame[10],
        )

    return OpcUnknown(opcode=op, payload=bytes(frame[1:-1]))


# ---------- encode -----------------------------------------------------------


def encode(msg: LocoNetMessage) -> bytes:
    """Turn *msg* into a complete frame (opcode + payload + checksum)."""
    op = msg.opcode

    # 2-byte
    if op in (OPC_GPON, OPC_GPOFF, OPC_IDLE):
        return append_checksum(bytes([op]))

    # 4-byte -- isinstance narrows the type cleanly per branch.
    if isinstance(msg, OpcSwReq):
        b1, b2 = _encode_switch_address(
            msg.address, closed=msg.closed, output_on=msg.output_on
        )
        return append_checksum(bytes([op, b1, b2]))
    if isinstance(msg, OpcSwRep):
        b1, b2 = _encode_switch_address(
            msg.address, closed=msg.closed, output_on=False, report_mode=True
        )
        return append_checksum(bytes([op, b1, b2]))
    if isinstance(msg, OpcInputRep):
        addr = msg.address - 1
        sensor_bit = addr & 0x01
        addr_words = addr >> 1
        b1 = addr_words & 0x7F
        b2 = ((addr_words >> 7) & 0x0F) | (sensor_bit << 5) | (0x10 if msg.active else 0)
        return append_checksum(bytes([op, b1, b2]))
    if isinstance(msg, OpcLongAck):
        return append_checksum(
            bytes([op, msg.responding_opcode & 0x7F, msg.ack_code & 0x7F])
        )
    if isinstance(msg, OpcSlotStat1):
        return append_checksum(bytes([op, msg.slot & 0x7F, msg.stat & 0x7F]))
    if isinstance(msg, OpcMoveSlots):
        return append_checksum(bytes([op, msg.src & 0x7F, msg.dest & 0x7F]))
    if isinstance(msg, OpcLinkSlots):
        return append_checksum(bytes([op, msg.src & 0x7F, msg.dest & 0x7F]))
    if isinstance(msg, OpcUnlinkSlots):
        return append_checksum(bytes([op, msg.src & 0x7F, msg.dest & 0x7F]))
    if isinstance(msg, OpcRqSlData):
        return append_checksum(bytes([op, msg.slot & 0x7F, 0x00]))
    if isinstance(msg, OpcLocoAdr):
        return append_checksum(
            bytes([op, (msg.address >> 7) & 0x7F, msg.address & 0x7F])
        )
    if isinstance(msg, OpcLocoSpd):
        return append_checksum(bytes([op, msg.slot & 0x7F, msg.speed & 0x7F]))
    if isinstance(msg, OpcLocoDirF):
        return append_checksum(bytes([op, msg.slot & 0x7F, msg.dirf & 0x7F]))
    if isinstance(msg, OpcLocoSnd):
        return append_checksum(bytes([op, msg.slot & 0x7F, msg.snd & 0x7F]))

    # 14-byte slot data frames.
    if isinstance(msg, (OpcSlRdData, OpcWrSlData)):
        body = bytes([
            op,
            14,                  # length byte
            msg.slot & 0x7F,
            msg.stat & 0x7F,
            msg.address & 0x7F,    # ADR (low)
            msg.speed & 0x7F,
            msg.dirf & 0x7F,
            msg.trk & 0x7F,
            msg.ss2 & 0x7F,      # SS2 -- hours for slot 123, STAT2 elsewhere
            (msg.address >> 7) & 0x7F,  # ADR2 (high)
            msg.snd & 0x7F,
            0x00,                # ID1
            0x00,                # ID2
        ])
        return append_checksum(body)

    if isinstance(msg, OpcUnknown):
        body = bytes([msg.opcode]) + msg.payload
        return append_checksum(body)

    raise NotImplementedError(f"encode not implemented for opcode 0x{op:02X}")


# ---------- internals --------------------------------------------------------


def _decode_switch_address(b1: int, b2: int) -> int:
    """Decode a LocoNet switch (turnout) address from the two body bytes."""
    # b1 = lower 7 bits, b2 low 4 bits = upper 4 bits; address is 1-based.
    return ((b2 & 0x0F) << 7 | (b1 & 0x7F)) + 1


def _encode_switch_address(
    address: int, *, closed: bool, output_on: bool, report_mode: bool = False
) -> tuple[int, int]:
    """Encode a switch address into the two body bytes.

    *report_mode* is True for OPC_SW_REP frames (bit-3 layout differs slightly
    in JMRI; the visible difference is which bits carry feedback flags).
    """
    addr0 = address - 1
    b1 = addr0 & 0x7F
    b2 = (addr0 >> 7) & 0x0F
    if closed:
        b2 |= 0x20
    if output_on and not report_mode:
        b2 |= 0x10
    return b1, b2


# Re-export so the test suite can compute its own checksums.
__all__ = [
    "LocoNetMessage",
    "OpcGpOff",
    "OpcGpOn",
    "OpcIdle",
    "OpcInputRep",
    "OpcLinkSlots",
    "OpcLocoAdr",
    "OpcLocoDirF",
    "OpcLocoSnd",
    "OpcLocoSpd",
    "OpcLongAck",
    "OpcMoveSlots",
    "OpcUnlinkSlots",
    "OpcRqSlData",
    "OpcSlRdData",
    "OpcSlotStat1",
    "OpcSwRep",
    "OpcSwReq",
    "OpcUnknown",
    "OpcWrSlData",
    "compute_checksum",
    "decode",
    "encode",
]

# Re-export used internally so static analyzers don't flag the import.
_ = compute_checksum
