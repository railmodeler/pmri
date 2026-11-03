"""LocoNet opcode constants and helpers.

LocoNet message bytes have the high bit set on opcodes (``0x80-0xFF``) and
clear on data bytes (``0x00-0x7F``). Bits 5-6 of the opcode encode the
message length class:

    0b00 -> 2 bytes (opcode + checksum)
    0b01 -> 4 bytes
    0b10 -> 6 bytes
    0b11 -> variable: the byte *after* the opcode is the total length

Checksum = ``0xFF XOR`` of every preceding byte. This module exposes the
opcodes JMRI's ``LnConstants`` declares, plus :func:`length_class_of` and
:func:`expected_length`.
"""
from __future__ import annotations

# 2-byte messages -----------------------------------------------------------
OPC_BUSY       = 0x81  # master is busy
OPC_GPOFF      = 0x82  # global power off
OPC_GPON       = 0x83  # global power on
OPC_IDLE       = 0x85  # force idle

# 4-byte messages -----------------------------------------------------------
OPC_LOCO_SPD   = 0xA0  # set loco speed
OPC_LOCO_DIRF  = 0xA1  # set loco direction + F0..F4
OPC_LOCO_SND   = 0xA2  # set loco F5..F8
OPC_SW_REQ     = 0xB0  # switch request (turnout)
OPC_SW_REP     = 0xB1  # switch report (turnout feedback)
OPC_INPUT_REP  = 0xB2  # input report (sensor)
OPC_LONG_ACK   = 0xB4  # long-form acknowledgment
OPC_SLOT_STAT1 = 0xB5  # write slot status
OPC_CONSIST_FUNC = 0xB6
OPC_UNLINK_SLOTS = 0xB8
OPC_LINK_SLOTS   = 0xB9
OPC_MOVE_SLOTS   = 0xBA  # move slots (used to dispatch / release)
OPC_RQ_SL_DATA   = 0xBB  # request slot data
OPC_SW_STATE     = 0xBC
OPC_SW_ACK       = 0xBD
OPC_LOCO_ADR     = 0xBF  # request slot for a DCC address

# Variable-length messages --------------------------------------------------
OPC_PEER_XFER  = 0xE5  # peer transfer (programmer / utility)
OPC_SL_RD_DATA = 0xE7  # slot read data (14-byte response to OPC_LOCO_ADR)
OPC_IMM_PACKET = 0xED  # immediate DCC packet
OPC_WR_SL_DATA = 0xEF  # write slot data (14-byte)


# Slot status constants (high nibble of slot stat byte) ---------------------
LOCO_FREE      = 0x00
LOCO_COMMON    = 0x10
LOCO_IDLE      = 0x20
LOCO_IN_USE    = 0x30
LOCO_STAT_MASK = 0x30

DEC_MODE_128         = 0x07  # 128 step decoder
DEC_MODE_28          = 0x02  # 28 step decoder
DEC_MODE_14          = 0x01  # 14 step decoder
DEC_MODE_128A        = 0x06  # 128 step + advanced consist
DEC_MODE_28A         = 0x03  # 28 step + advanced consist


# Length helpers -----------------------------------------------------------


def length_class_of(opcode: int) -> int:
    """Return 0, 1, 2, or 3 for the length-class bits of *opcode*."""
    return (opcode >> 5) & 0b11


def expected_length(opcode: int) -> int | None:
    """Bytes including the opcode and checksum for a fixed-length frame.

    Returns ``None`` for variable-length opcodes (length class 3) where the
    length lives in the byte after the opcode.
    """
    cls = length_class_of(opcode)
    if cls == 0b00:
        return 2
    if cls == 0b01:
        return 4
    if cls == 0b10:
        return 6
    return None


def is_valid_opcode_byte(b: int) -> bool:
    """True if *b* could be the first byte of a LocoNet message."""
    return 0x80 <= b <= 0xFF
