"""NCE commands and replies.

Each command is a fixed-shape request -> fixed-shape reply pair. We model
this declaratively with :class:`NceCommand` and a small set of factory
functions that build commands for the opcodes the rest of the package
needs. New commands can be added without touching the traffic controller.

**Every factory in this file must trace to a specific JMRI Java
``NceMessage`` or ``NceBinaryCommand`` factory.** The wire format is
audit-checked against JMRI's ``NceMessage.java`` opcode constants and
``NceMonBinaryBundle.properties``. Do not add a factory without citing
the JMRI source it mirrors.
"""
from __future__ import annotations

from dataclasses import dataclass

from pmri_nce.opcodes import (
    CS_CLOCK_ADDR,
    LONG_ADDRESS_OFFSET,
    OP_ACCY_DECODER_OPS,
    OP_AIU_BASE_QUERY,
    OP_CLOCK_SET_1224,
    OP_CLOCK_SET_RATIO,
    OP_CLOCK_START,
    OP_CLOCK_STOP,
    OP_ENTER_PROG_MODE,
    OP_EXIT_PROG_MODE,
    OP_NCE_LOCO_CMD,
    OP_POWER_OFF,
    OP_POWER_ON,
    OP_QUERY_SW_REV,
    OP_QUEUE_PACKET_BASE,
    OP_RD_CV_DIRECT_MODE,
    OP_READ_16_MEM,
    OP_WR_CV_DIRECT_MODE,
    OP_WRITE_N_MEM,
)


@dataclass(frozen=True, slots=True)
class NceCommand:
    """One outbound request.

    NCE has two distinct reply shapes:

    * **Action commands** (power on/off, accessory throw, write CV, ...)
      reply with a single ACK byte (``!`` on success). ``reply_len`` is
      the number of *data* bytes that precede the ACK -- usually 0.
    * **Read commands** (read memory, read AIU, ...) reply with exactly
      *reply_len* bytes of raw data, no trailing ACK. Set ``is_read=True``
      and the traffic controller will not consume an ACK byte after.
    """

    name: str
    request: bytes
    reply_len: int = 0
    timeout_s: float = 0.5
    is_read: bool = False


@dataclass(frozen=True, slots=True)
class NceReply:
    """A successful response. ``data`` excludes the ACK byte itself."""

    command: NceCommand
    data: bytes


class NceCommandError(Exception):
    """The command station replied with a non-OK ACK byte."""

    def __init__(self, command: NceCommand, ack: bytes, data: bytes) -> None:
        super().__init__(
            f"NCE command {command.name!r} failed: ack={ack!r} data={data!r}"
        )
        self.command = command
        self.ack = ack
        self.data = data


# --- factory functions ------------------------------------------------------


def power_on() -> NceCommand:
    """JMRI: ``NceMessage.getEnableMain`` (opcode 137)."""
    return NceCommand(name="POWER_ON", request=bytes([OP_POWER_ON]))


def power_off() -> NceCommand:
    """JMRI: ``NceMessage.getKillMain`` (opcode 139)."""
    return NceCommand(name="POWER_OFF", request=bytes([OP_POWER_OFF]))


def read_16_memory(address: int) -> NceCommand:
    """Read exactly 16 bytes at *address*.

    JMRI: ``NceBinaryCommand.accMemoryRead`` (opcode 0x8F, READ16_CMD).
    NCE has no "read N bytes" opcode -- only fixed 16-byte or 1-byte
    reads. For arbitrary lengths, issue multiple 16-byte reads.
    """
    return NceCommand(
        name=f"READ16({address:#06x})",
        request=bytes([OP_READ_16_MEM, (address >> 8) & 0xFF, address & 0xFF]),
        reply_len=16,
        is_read=True,
    )


# Backwards-compatible alias for the turnout monitor's existing call site.
# The name emphasises that the accessory-state memory is the typical
# caller, but the opcode itself is address-agnostic.
def read_accessory_memory(address: int) -> NceCommand:
    """Read 16 bytes of accessory-state memory at *address* (turnout poll).

    Alias for :func:`read_16_memory` -- the underlying opcode 0x8F works
    at arbitrary addresses despite the historical "accessory" name.
    """
    return read_16_memory(address)


def write_memory(address: int, data: bytes) -> NceCommand:
    """Write *data* (1..16 bytes) starting at the 16-bit *address*.

    JMRI: ``NceBinaryCommand.accMemoryWriteN`` (opcode 0x8E, WRITE_N_CMD).

    IMPORTANT: The CS expects a **fixed 20-byte request** regardless of
    payload length -- JMRI Java allocates ``new byte[20]`` and fills only
    the first ``4 + data.length`` bytes, leaving the rest zero-padded.
    We match that shape byte-for-byte; without the pad the CS waits
    forever for the remaining bytes and the ACK never arrives.
    """
    if not 1 <= len(data) <= 16:
        raise ValueError(f"write length must be 1..16, got {len(data)}")
    header = bytes([
        OP_WRITE_N_MEM,
        (address >> 8) & 0xFF, address & 0xFF,
        len(data) & 0xFF,
    ])
    padding = bytes(20 - len(header) - len(data))
    return NceCommand(
        name=f"WRITE_N_MEM({address:#06x},{len(data)})",
        request=header + data + padding,
    )


def accessory_decoder_normal_ops(address: int, on: bool) -> NceCommand:
    """Operate an accessory via the NCE binary accessory-decoder command.

    JMRI: ``NceBinaryCommand.accDecoder`` (opcode 0xAD, ACC_CMD).

    *address* is the accessory address (1..2044). ``on=True`` selects the
    "closed" (normal) output; ``on=False`` selects "thrown" (reverse).
    Wire form: ``[0xAD, addr_hi, addr_lo, dir, 0x00]`` where *dir* is
    the NMRA accessory-direction nibble (3=closed, 4=thrown).
    """
    if not 1 <= address <= 2044:
        raise ValueError(f"accessory address must be 1..2044, got {address}")
    direction = 3 if on else 4
    return NceCommand(
        name=f"ACCY({address},{'closed' if on else 'thrown'})",
        request=bytes([
            OP_ACCY_DECODER_OPS,
            (address >> 8) & 0xFF,
            address & 0xFF,
            direction,
            0x00,
        ]),
    )


def nce_loco_cmd(
    loco_address: int, *, long_address: bool, sub_cmd: int, data: int
) -> NceCommand:
    """Throttle command via NCE binary ``nceLocoCmd``.

    JMRI: ``NceBinaryCommand.nceLocoCmd`` (opcode 0xA2, LOCO_CMD).

    Modern firmware (commandOptions >= 10) uses this 5-byte command for
    speed, direction, and function changes.

        [0xA2] [addr_hi] [addr_lo] [sub_cmd] [data]   (5 bytes)

    For long addresses, the wire address is ``loco_address + 0xC000``
    so the CS can disambiguate from short addresses with the same value.

    *sub_cmd* values come from :mod:`pmri_nce.opcodes`:

    * ``LOCO_SUB_128_FWD`` / ``LOCO_SUB_128_REV`` -- 128-step speed
    * ``LOCO_SUB_ESTOP_FWD`` / ``LOCO_SUB_ESTOP_REV`` -- emergency stop
    * ``LOCO_SUB_FUNC_GROUP_1..5`` -- function bitmasks
    """
    if not 0 <= sub_cmd <= 23:
        raise ValueError(f"sub_cmd must be 0..23, got {sub_cmd}")
    if not 0 <= data <= 255:
        raise ValueError(f"data byte must be 0..255, got {data}")
    wire = loco_address + LONG_ADDRESS_OFFSET if long_address else loco_address
    return NceCommand(
        name=f"LOCO({loco_address}{'L' if long_address else 'S'},sub={sub_cmd},data={data})",
        request=bytes([
            OP_NCE_LOCO_CMD,
            (wire >> 8) & 0xFF,
            wire & 0xFF,
            sub_cmd & 0xFF,
            data & 0xFF,
        ]),
    )


def query_command_version() -> NceCommand:
    """Read the command station firmware version. Reply is 3 bytes.

    JMRI: ``NceBinaryCommand.getNceEpromRev`` (opcode 0xAA, SW_REV_CMD).
    Safe: read-only, no side effects.
    """
    return NceCommand(
        name="QUERY_SW_REV",
        request=bytes([OP_QUERY_SW_REV]),
        reply_len=3,
        is_read=True,
    )


def enter_programming_mode() -> NceCommand:
    """Put the CS into programming mode.

    JMRI: ``NceMessage.getEnterProgMode`` (opcode 0x9E). Direct-mode
    CV read/write commands (``0xA8``, ``0xA9``) require the CS to be
    in this mode -- outside it, the CS immediately NoAcks the request
    with ``'0'`` (0x30) without running any DCC packets on the prog
    track. Java JMRI's ``NceProgrammer`` wraps every direct-mode op
    with this + :func:`exit_programming_mode`.

    Reply is a single ACK byte (``!`` on success).
    """
    return NceCommand(
        name="ENTER_PROG",
        request=bytes([OP_ENTER_PROG_MODE]),
        # Fast: CS just flips a state flag. Give it a generous window
        # for slow USB-serial adapters.
        timeout_s=1.0,
    )


def exit_programming_mode() -> NceCommand:
    """Take the CS out of programming mode.

    JMRI: ``NceMessage.getExitProgMode`` (opcode 0x9F). Send this after
    a direct-mode CV op so the CS returns to normal operation and
    layout traffic resumes. Reply is ``!`` on success.
    """
    return NceCommand(
        name="EXIT_PROG",
        request=bytes([OP_EXIT_PROG_MODE]),
        timeout_s=1.0,
    )


def direct_mode_write_cv(cv: int, value: int) -> NceCommand:
    """Write a CV on the programming track (direct mode).

    JMRI: ``NceMessage.getWriteDirectCV`` (opcode 0xA8, WRITE_DIR_CV_CMD).
    Layout: ``[0xA8, cv_hi, cv_lo, value]``.
    """
    if not 1 <= cv <= 1024:
        raise ValueError(f"CV must be 1..1024, got {cv}")
    if not 0 <= value <= 255:
        raise ValueError(f"value must be 0..255, got {value}")
    return NceCommand(
        name=f"WR_CV_DIRECT({cv},{value})",
        request=bytes([OP_WR_CV_DIRECT_MODE, (cv >> 8) & 0xFF, cv & 0xFF, value]),
        timeout_s=6.0,  # service-mode writes take seconds
    )


def direct_mode_read_cv(cv: int) -> NceCommand:
    """Read a CV on the programming track.

    JMRI: ``NceMessage.getReadDirectCV`` (opcode 0xA9, READ_DIR_CV_CMD).
    Reply is 1 byte (the CV value).
    """
    if not 1 <= cv <= 1024:
        raise ValueError(f"CV must be 1..1024, got {cv}")
    return NceCommand(
        name=f"RD_CV_DIRECT({cv})",
        request=bytes([OP_RD_CV_DIRECT_MODE, (cv >> 8) & 0xFF, cv & 0xFF]),
        reply_len=1,
        timeout_s=6.0,
        is_read=True,
    )


def ops_mode_write_cv(
    loco_address: int, cv: int, value: int, *, long_address: bool = False,
) -> NceCommand:
    """Ops-mode CV byte-write (Programming On the Main) to a specific loco.

    JMRI: ``NceMessage.sendPacketMessage`` builds the equivalent wrapped
    packet in ``NceProgrammer.writeCV`` when ops mode is selected.

    Wire form: build a raw NMRA CV Access instruction packet, then wrap
    it in the appropriate ``QUEUEn_TMP_CMD`` envelope where ``n`` is the
    packet's byte length (opcode ``0x93 + (packet_len - 3)`` covers
    3..6-byte packets, per :data:`OP_QUEUE_PACKET_BASE`). NCE has no
    native ops-mode CV *read* (RailCom is out of PowerPro's scope) -- do
    not add ``ops_mode_read_cv``; use direct mode for verified reads.

    NMRA S-9.2.1 CV Access -- Long Form / Byte Write:

        <address bytes>  1110 CCVV   VVVVVVVV   DDDDDDDD  <XOR>

    * short address (1..127): 1 address byte ``0AAAAAAA``
    * long address (128..9999): 2 address bytes ``11AAAAAA AAAAAAAA``
    * ``CC = 11`` -> byte-write (10 = verify, 01 = bit-manipulate)
    * ``VVVVVVVVVV`` = CV number minus 1, 10-bit
    * ``DDDDDDDD`` = value
    * trailing XOR of all preceding bytes

    Reply: single ACK byte from the CS (``!`` on success). The packet
    goes on the wire from the CS; no decoder ack round-trip.
    """
    if not 1 <= cv <= 1024:
        raise ValueError(f"CV must be 1..1024, got {cv}")
    if not 0 <= value <= 255:
        raise ValueError(f"value must be 0..255, got {value}")

    if long_address:
        if not 128 <= loco_address <= 9999:
            raise ValueError(
                f"long address must be 128..9999, got {loco_address}"
            )
        # 14-bit long address in the DCC packet: top 6 bits go into the
        # low 6 bits of 0xC0, low 8 bits stand alone.
        addr_bytes = [
            0xC0 | ((loco_address >> 8) & 0x3F),
            loco_address & 0xFF,
        ]
    else:
        if not 1 <= loco_address <= 127:
            raise ValueError(
                f"short address must be 1..127, got {loco_address}"
            )
        addr_bytes = [loco_address & 0x7F]

    # NMRA encodes CV number minus one, spread over two bytes: 2 high
    # bits ride the instruction opcode, 8 low bits follow.
    cv_index = cv - 1
    inst_hi = 0xEC | ((cv_index >> 8) & 0x03)   # 1110_1100 with CC=11 + VV
    inst_lo = cv_index & 0xFF

    packet = bytes(addr_bytes + [inst_hi, inst_lo, value & 0xFF])
    xor = 0
    for b in packet:
        xor ^= b
    dcc_packet = packet + bytes([xor])

    # QUEUEn_TMP_CMD wire form (verified against JMRI Java NCE Command
    # Monitor trace on real PowerPro hardware, 2026-07-25):
    #
    #   [queue_opcode] [send_count] [dcc_packet_bytes...]
    #
    # * ``queue_opcode`` = 0x93..0x96 (packet length 3..6)
    # * ``send_count``   = number of times the CS retransmits the
    #                      packet on the wire. NMRA POM recommends
    #                      3-5 repeats for reliability; JMRI Java uses
    #                      5, which we mirror. The CS reads this byte
    #                      as part of the command -- earlier versions
    #                      of this code omitted it, so the CS would
    #                      read our first packet byte as the send
    #                      count and then time out ~5 s waiting for a
    #                      packet byte that never came, returning
    #                      NoAck (``0``).
    # * ``dcc_packet``   = 3..6 bytes: address + CV Access instruction
    #                      + XOR checksum, per NMRA S-9.2.1.
    packet_len = len(dcc_packet)
    if not 3 <= packet_len <= 6:
        # Defensive: the packet builder should never produce a length
        # outside 3..6, but if a future edit changes the encoding this
        # catches the mistake before the wrong opcode goes out.
        raise ValueError(
            f"internal error: DCC packet length {packet_len} outside 3..6",
        )
    queue_opcode = OP_QUEUE_PACKET_BASE + (packet_len - 3)
    send_count = 5   # matches JMRI Java default

    return NceCommand(
        name=f"WR_CV_OPS({loco_address}{'L' if long_address else 'S'},{cv},{value})",
        request=bytes([queue_opcode, send_count]) + dcc_packet,
        # Ops-mode writes are fire-and-forget on the wire; only the CS
        # queue-accept ACK ('!') comes back. No data bytes.
        #
        # ``timeout_s`` is 5.0 s -- matches JMRI Java's timeout for
        # equivalent operations. NCE PowerPro's actual queue-then-ack
        # latency depends on the CS's current DCC output load: with
        # active throttles + fast-clock ticks + accessory refresh, we
        # observed >2 s on real hardware even after bumping from the
        # 0.5 s default. 5 s gives comfortable headroom; a real hang
        # (packet dropped, CS off-line) still surfaces before the user
        # gives up on the batch.
        timeout_s=5.0,
    )


def read_clock() -> NceCommand:
    """Read the CS fast clock (16 bytes at ``CS_CLOCK_ADDR``).

    JMRI: ``NceClockControl.issueReadOnlyRequest`` uses
    ``NceBinaryCommand.accMemoryRead(getClockAddr())`` (opcode 0x8F).
    Wire request: 3 bytes ``[0x8F, addr_hi, addr_lo]``; reply is 16
    data bytes. Passive read -- safe to send even when the clock isn't
    running.

    Reply byte layout mirrors ``NceClockControl.readClockPacket``:

    * ``[0]`` scale -- CS-internal divisor, ``ratio = 250 / scale``
    * ``[1]`` tick
    * ``[2]`` seconds (binary, not BCD)
    * ``[3]`` minutes
    * ``[4]`` hours (1..12 in 12h mode; 0..23 in 24h)
    * ``[5]`` AM/PM flag (ASCII ``'A'`` = 65 -> AM)
    * ``[6]`` 12/24 mode (1 = 24h, else 12h)
    * ``[13]`` status

    1.5 s timeout (vs the 0.5 s default) leaves headroom on slow USB-serial
    adapters where a tightly-scheduled read can otherwise time out while
    its 16 bytes are still in flight.
    """
    return NceCommand(
        name="READ_CLOCK",
        request=bytes([
            OP_READ_16_MEM,
            (CS_CLOCK_ADDR >> 8) & 0xFF, CS_CLOCK_ADDR & 0xFF,
        ]),
        reply_len=16,
        is_read=True,
        timeout_s=1.5,
    )


def write_clock_time(hours: int, minutes: int, seconds: int) -> NceCommand:
    """Write hh/mm/ss to the CS clock.

    JMRI: ``NceClockControl.issueClockSetMem`` writes ``[ss, mm, hh]`` at
    ``CS_CLOCK_ADDR + 2`` via ``accMemoryWriteN`` (opcode 0x8E, WRITE_N_CMD).
    Delegates to :func:`write_memory` so the fixed-20-byte wire pad is
    applied in one place. 1.5 s timeout mirrors the clock read.
    """
    if not 0 <= hours <= 23:
        raise ValueError(f"hours must be 0..23, got {hours}")
    if not 0 <= minutes <= 59:
        raise ValueError(f"minutes must be 0..59, got {minutes}")
    if not 0 <= seconds <= 59:
        raise ValueError(f"seconds must be 0..59, got {seconds}")
    payload = bytes([seconds & 0xFF, minutes & 0xFF, hours & 0xFF])
    inner = write_memory(CS_CLOCK_ADDR + 2, payload)
    # Reuse the well-formed request but with a clock-specific name + timeout.
    return NceCommand(
        name=f"WRITE_CLOCK_TIME({hours:02d}:{minutes:02d}:{seconds:02d})",
        request=inner.request,
        timeout_s=1.5,
    )


def set_clock_ratio(ratio: int) -> NceCommand:
    """Set the CS fast-clock ratio (integer, 1..15).

    JMRI: ``NceBinaryCommand.accSetClockRatio`` (opcode 0x87, CLOCK_RATIO_CMD).
    Sends ``[0x87, ratio]``. Read-back later reports ``scale = 250 / ratio``.
    """
    if not 1 <= ratio <= 15:
        raise ValueError(f"clock ratio must be 1..15, got {ratio}")
    return NceCommand(
        name=f"SET_CLOCK_RATIO({ratio})",
        request=bytes([OP_CLOCK_SET_RATIO, ratio & 0xFF]),
        timeout_s=1.5,
    )


def set_clock_1224(twenty_four_hour: bool) -> NceCommand:
    """Switch the CS clock between 12h and 24h display.

    JMRI: ``NceBinaryCommand.accSetClock1224`` (opcode 0x86, CLOCK_1224_CMD).
    """
    return NceCommand(
        name=f"SET_CLOCK_1224({'24' if twenty_four_hour else '12'})",
        request=bytes([OP_CLOCK_SET_1224, 1 if twenty_four_hour else 0]),
        timeout_s=1.5,
    )


def start_clock() -> NceCommand:
    """Start the CS fast clock.

    JMRI: ``NceBinaryCommand.accStartClock`` (opcode 0x84, START_CLOCK_CMD).
    """
    return NceCommand(
        name="START_CLOCK",
        request=bytes([OP_CLOCK_START]),
        timeout_s=1.5,
    )


def stop_clock() -> NceCommand:
    """Stop (hold) the CS fast clock.

    JMRI: ``NceBinaryCommand.accStopClock`` (opcode 0x83, STOP_CLOCK_CMD).
    """
    return NceCommand(
        name="STOP_CLOCK",
        request=bytes([OP_CLOCK_STOP]),
        timeout_s=1.5,
    )


def read_aiu(aiu_address: int) -> NceCommand:
    """Read one AIU module's 16-bit input state.

    JMRI: ``NceBinaryCommand.accAiu2Read`` (opcode 0x9B, READ_AUI2_CMD).
    Returns the AIU's two-byte word: bits 0..13 are inputs 1..14.
    """
    if not 1 <= aiu_address <= 63:
        raise ValueError(f"AIU address must be 1..63, got {aiu_address}")
    return NceCommand(
        name=f"AIU_QUERY({aiu_address})",
        request=bytes([OP_AIU_BASE_QUERY, aiu_address & 0xFF]),
        reply_len=2,
        is_read=True,
    )
