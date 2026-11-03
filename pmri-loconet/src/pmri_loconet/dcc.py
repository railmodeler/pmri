"""NMRA DCC function-group packet encoding for OPC_IMM_PACKET.

LocoNet's ``OPC_IMM_PACKET`` (``0xED``) wraps a raw NMRA DCC packet for
the command station to send to the layout. We use this for function
groups F9..F28 because the slot-based ``OPC_LOCO_DIRF``/``OPC_LOCO_SND``
opcodes only cover F0..F8.

This module duplicates the NMRA encoding from :mod:`pmri_nce.dcc` so the
two drivers stay decoupled. Both produce identical wire bytes (NMRA is
the standard); only the carrier opcode differs.
"""
from __future__ import annotations


def _address_bytes(address: int, long_address: bool) -> bytes:
    if long_address:
        if not 1 <= address <= 10239:
            raise ValueError(f"long address out of range: {address}")
        return bytes([0xC0 | ((address >> 8) & 0x3F), address & 0xFF])
    if not 1 <= address <= 127:
        raise ValueError(f"short address out of range: {address}")
    return bytes([address])


def _xor(packet: bytes) -> int:
    out = 0
    for b in packet:
        out ^= b
    return out


def function_high_packet_dcc(
    *,
    address: int,
    long_address: bool,
    functions: list[bool],
    group: int,
) -> bytes:
    """NMRA function-group 3 (F13..F20) or 4 (F21..F28) instruction.

    Wire form: ``[addr...] [0xDE | 0xDF] [8-bit mask] [xor]``.
    """
    addr = _address_bytes(address, long_address)
    if group == 3:
        opcode = 0xDE
        bits = functions[13:21]
    elif group == 4:
        opcode = 0xDF
        bits = functions[21:29]
    else:
        raise ValueError(f"function group must be 3 or 4, got {group}")
    mask = 0
    for i, on in enumerate(bits):
        if on:
            mask |= 1 << i
    body = addr + bytes([opcode, mask])
    return body + bytes([_xor(body)])


def wrap_in_imm_packet(dcc: bytes, *, repeat: int = 2) -> bytes:
    """Wrap a complete NMRA DCC packet in an ``OPC_IMM_PACKET`` frame.

    Layout: ``0xED LEN 0x7F repeat_byte D[0]...D[n] checksum``.

    The ``IM1`` byte (``0x7F``) and ``IM2`` (repeat encoding) follow the
    Digitrax IMM packet spec. ``repeat`` is sent into ``IM2``'s low bits
    (1-4 typical for function packets). The checksum is the standard
    LocoNet XOR over the body -- but this helper returns only the body
    bytes; the traffic controller adds the checksum via its frame writer.

    *dcc* must be a complete NMRA packet **including** the trailing XOR
    error byte -- we'll embed it as-is and strip the high bit per LocoNet
    spec (DCC bytes go on the wire with high bits packed into IM1/IM2).

    For Stage 9.1 we ship a simplified encoding: each DCC byte's high bit
    is OR'd into a dedicated high-bit byte, and the low 7 bits go on the
    wire directly. Sufficient for function packets where the DCC bytes
    are typically 0x80-0xFF (high bit always set).
    """
    if not 3 <= len(dcc) <= 5:
        raise ValueError(f"IMM packet wraps 3..5 DCC bytes, got {len(dcc)}")
    # Length byte: total frame length (including OPC + LEN + body + cs).
    # IM1: bit 4 = number of repeats (low 3 bits), bits 0-3 = high bits of
    # data bytes 0-3. JMRI's standard layout for accessory/function packets
    # uses repeat=0x71 to mean "send twice".
    body = bytearray([0xED, 0x0B, 0x7F])
    # repeat encoding: bits 4-6 = repeat count, bits 0-3 = high bits of
    # data bytes (high bit of DCC byte i goes into bit i of IM2).
    im2 = (repeat & 0x07) << 4
    for i, b in enumerate(dcc):
        if b & 0x80:
            im2 |= (1 << i) & 0x0F
    body.append(im2)
    # Add the 7-bit data bytes (high bit cleared).
    for b in dcc:
        body.append(b & 0x7F)
    # Pad to fixed 5 data bytes (LocoNet expects 11-byte fixed IMM packet body).
    while len(body) < 10:
        body.append(0x00)
    return bytes(body)
