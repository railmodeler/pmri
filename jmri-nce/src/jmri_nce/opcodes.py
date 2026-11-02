"""NCE binary command opcodes -- audited against JMRI Java's
``NceMessage.java`` constants and ``NceMonBinaryBundle.properties``
(the wire-decoder bundle).

**Every constant in this file must trace to a specific JMRI ``setOpCode``
call site or ``NceBinaryCommand`` factory.** If it doesn't, it is
NOT SAFE to send -- the NCE binary protocol is not orthogonal (adjacent
opcodes have very different effects; e.g. 0x97 is Write-1-byte-to-memory,
not Read-CV) and a wrong opcode can corrupt CS EEPROM.

Every unused / speculative opcode has been removed from this module.

Reference: ``NceMessage.java`` decimal -> hex conversion table used to
build this file:

    NOP_CMD              128 = 0x80
    READ_CLOCK_CMD       130 = 0x82
    STOP_CLOCK_CMD       131 = 0x83
    START_CLOCK_CMD      132 = 0x84
    SET_CLOCK_CMD        133 = 0x85   (set clock hr/min -- NOT memory write)
    CLOCK_1224_CMD       134 = 0x86
    CLOCK_RATIO_CMD      135 = 0x87   (set fast-clock ratio -- NOT version query)
    DEQUEUE_CMD          136 = 0x88   (dequeue byte -- NOT power off)
    ENABLE_MAIN_CMD      137 = 0x89   (power on)
    READ_AUI4_CMD        138 = 0x8A
    KILL_MAIN_CMD        139 = 0x8B   (power off)
    DUMMY_CMD            140 = 0x8C
    WRITE_N_CMD          142 = 0x8E   (write N bytes at address)
    READ16_CMD           143 = 0x8F   (read 16 bytes at address)
    DISPLAY3_CMD         144 = 0x90   (write 3 chars to CS LCD -- NOT CV read)
    DISPLAY4_CMD         145 = 0x91   (write 4 chars to CS LCD -- NOT CV write)
    DISPLAY2_CMD         146 = 0x92
    QUEUE3..6_TMP_CMD    147..150 = 0x93..0x96  (queue raw DCC packet)
    WRITE1_CMD           151 = 0x97   (write 1 byte at address -- NOT CV read)
    WRITE2/4/8_CMD       152..154 = 0x98..0x9A
    READ_AUI2_CMD        155 = 0x9B
    MACRO_CMD            156 = 0x9C
    READ1_CMD            157 = 0x9D
    ENTER_PROG_CMD       158 = 0x9E
    EXIT_PROG_CMD        159 = 0x9F
    WRITE_PAGED_CV_CMD   160 = 0xA0   (paged-mode CV write -- NOT memory read)
    READ_PAGED_CV_CMD    161 = 0xA1
    LOCO_CMD             162 = 0xA2   (throttle)
    WRITE_REG_CMD        166 = 0xA6
    READ_REG_CMD         167 = 0xA7
    WRITE_DIR_CV_CMD     168 = 0xA8   (direct-mode CV write on prog track)
    READ_DIR_CV_CMD      169 = 0xA9   (direct-mode CV read on prog track)
    SW_REV_CMD           170 = 0xAA   (firmware version -- this is the safe one)
    ACC_CMD              173 = 0xAD   (accessory decoder / signal / macro)
    OPS_PROG_LOCO_CMD    174 = 0xAE
    OPS_PROG_ACCY_CMD    175 = 0xAF
"""
from __future__ import annotations

# --- power (mains-track relay) -------------------------------------------

# JMRI: NceMessage.getEnableMain / getKillMain (setOpCode 137 / 139).
OP_POWER_ON             = 0x89   # ENABLE_MAIN_CMD
OP_POWER_OFF            = 0x8B   # KILL_MAIN_CMD

# --- CS memory read/write -------------------------------------------------

# JMRI: NceBinaryCommand.accMemoryWriteN / accMemoryRead / accMemoryRead1.
OP_WRITE_N_MEM          = 0x8E   # WRITE_N_CMD    -- write N bytes at addr
OP_READ_16_MEM          = 0x8F   # READ16_CMD     -- read exactly 16 bytes at addr
OP_READ_1_MEM           = 0x9D   # READ1_CMD      -- read 1 byte at addr

# NCE has no "read N bytes" opcode -- only fixed 16-byte or 1-byte reads.

# --- fast clock -----------------------------------------------------------
#
# JMRI: NceBinaryCommand.accStopClock / accStartClock / accSetClock1224 /
# accSetClockRatio, plus accMemoryReadN(getClockAddr()) for reads and
# accMemoryWriteN(getClockAddr()+2, [ss, mm, hh]) for time writes.

OP_CLOCK_STOP           = 0x83   # STOP_CLOCK_CMD
OP_CLOCK_START          = 0x84   # START_CLOCK_CMD
OP_CLOCK_SET_1224       = 0x86   # CLOCK_1224_CMD  -- arg: 1=24h, 0=12h
OP_CLOCK_SET_RATIO      = 0x87   # CLOCK_RATIO_CMD -- arg: integer ratio 1..15

# PowerPro clock lives at 0xDC00; PowerCab (Ph5) at 0x3E00.
CS_CLOCK_ADDR           = 0xDC00

# --- AIU (occupancy detector) --------------------------------------------

# JMRI: NceBinaryCommand.accAiu2Read.
OP_AIU_BASE_QUERY       = 0x9B   # READ_AUI2_CMD  -- read AIU 16-bit input word

# --- accessory decoders ---------------------------------------------------

# JMRI: NceBinaryCommand.accDecoder.
OP_ACCY_DECODER_OPS     = 0xAD   # ACC_CMD
                                  # wire form: [0xAD, addr_hi, addr_lo, dir, 0x00]
                                  # dir = 3 (closed) / 4 (thrown), per NMRA

# --- throttle (modern firmware, commandOptions >= 10) --------------------

# JMRI: NceBinaryCommand.nceLocoCmd.
OP_NCE_LOCO_CMD         = 0xA2   # LOCO_CMD
                                  # wire form: [0xA2, addr_hi, addr_lo, sub_cmd, data]

LOCO_SUB_28_REV         = 1
LOCO_SUB_28_FWD         = 2
LOCO_SUB_128_REV        = 3
LOCO_SUB_128_FWD        = 4
LOCO_SUB_ESTOP_REV      = 5
LOCO_SUB_ESTOP_FWD      = 6
LOCO_SUB_FUNC_GROUP_1   = 7    # F0..F4
LOCO_SUB_FUNC_GROUP_2   = 8    # F5..F8
LOCO_SUB_FUNC_GROUP_3   = 9    # F9..F12
LOCO_SUB_FUNC_GROUP_4   = 21   # F13..F20
LOCO_SUB_FUNC_GROUP_5   = 22   # F21..F28

# --- CS-managed consist sub-commands -----------------------------------
#
# NCE PowerPro tracks command-station consists internally (1..127, six
# members max: one LEAD + one REAR + up to four MIDs). The loco command
# above adds / removes locos to that consist by *sub-command byte*, with
# the target consist number in the data byte. Cross-referenced against
# JMRI Java ``jmri.jmrix.nce.NceConsist.add()`` -- see the values passed
# to ``addLocoToConsist(...)`` on the first / second / third+ add path,
# and the constants used by ``removeLocoFromConsist`` (16) and
# ``killConsist`` (17).
#
# Direction: FWD = loco oriented forward in the consist,
#            REV = loco oriented backwards. The CS translates that into
#            per-loco direction inversion when it fans out speed cmds.
#
# Position rules (enforced by ``NceConsistManager``):
# * First member added becomes LEAD (position 0).
# * Second member becomes REAR (position 255 in JMRI convention).
# * Third through sixth become MID (positions 1..4).
# * LEAD and REAR can NOT be removed with 0x10 -- dispose the whole
#   consist with 0x11 instead.

LOCO_SUB_CS_ADD_LEAD_REV = 0x0A  # Add LEAD, reversed orientation
LOCO_SUB_CS_ADD_LEAD_FWD = 0x0B  # Add LEAD, forward orientation
LOCO_SUB_CS_ADD_REAR_REV = 0x0C  # Add REAR, reversed
LOCO_SUB_CS_ADD_REAR_FWD = 0x0D  # Add REAR, forward
LOCO_SUB_CS_ADD_MID_REV  = 0x0E  # Add MID, reversed
LOCO_SUB_CS_ADD_MID_FWD  = 0x0F  # Add MID, forward
LOCO_SUB_CS_REMOVE       = 0x10  # Remove this loco from its consist (data=0)
LOCO_SUB_CS_KILL         = 0x11  # Kill the consist this loco leads (data=0)

# Long addresses are pre-encoded as ``addr + 0xC000`` so the CS can
# distinguish them from short addresses with the same numeric value.
LONG_ADDRESS_OFFSET     = 0xC000

# --- CV programming (programming track / ops mode) -----------------------
#
# JMRI: NceMessage.getWriteDirectCV / getReadDirectCV (setOpCode 168/169).
# Ops-mode CV writes use sendPacketMessage which wraps a raw NMRA DCC
# packet in a QUEUE_TMP command (opcodes 0x93+packet_len).

OP_ENTER_PROG_MODE      = 0x9E   # ENTER_PROG_CMD    -- put CS in programming mode
OP_EXIT_PROG_MODE       = 0x9F   # EXIT_PROG_CMD     -- leave programming mode
OP_WR_CV_DIRECT_MODE    = 0xA8   # WRITE_DIR_CV_CMD  -- direct-mode write, prog track
OP_RD_CV_DIRECT_MODE    = 0xA9   # READ_DIR_CV_CMD   -- direct-mode read, prog track

# Base opcode for the QUEUEn_TMP_CMD family: 0x93 = queue 3-byte packet,
# 0x94 = 4-byte, 0x95 = 5-byte, 0x96 = 6-byte. Ops-mode CV writes on the
# main track use this by building a raw DCC packet and shipping it via
# the appropriate variant. There is no NCE ops-mode CV *read* -- NCE
# PowerPro doesn't ship RailCom decoding; ops reads are not supported.
OP_QUEUE_PACKET_BASE    = 0x93

# --- firmware version query -----------------------------------------------

# JMRI: NceBinaryCommand.getNceEpromRev (opcode 0xAA).
OP_QUERY_SW_REV         = 0xAA   # SW_REV_CMD -- safe: read-only, no side effects

# --- ACK / reply byte values ---------------------------------------------

ACK_OK              = b"!"   # success
ACK_ERROR           = b"0"   # explicit NACK -- raises NceCommandError
ACK_OTHER           = b"$"   # other status
ACK_FRAMING_ERROR   = b"-"

# NCE's reply byte semantics vary by command. Many action commands return
# either ``!`` (ACK_OK) or an echo of the request's opcode byte -- JMRI
# treats both as success (its listener is null for these). The traffic
# controller validates ``ACK_ERROR`` only; any other byte is accepted.


# --- deprecated aliases (kept only long enough to update callers) --------
#
# Old names that were bound to WRONG opcodes. Importing any of these
# raises at import-time so a caller can't accidentally send the
# pre-audit byte.

def __getattr__(name: str) -> int:
    _WITHDRAWN = {
        # name -> (previous_value, what_it_actually_did, correct_symbol)
        "OP_READ_N_MEM":       (0xA0, "WRITE_PAGED_CV_CMD (paged-mode CV write)",
                                "OP_READ_16_MEM (fixed 16-byte read) or OP_READ_1_MEM"),
        "OP_READ_ACCY_MEM":    (0x8F, "correct opcode, misleading name",
                                "OP_READ_16_MEM"),
        "OP_READ_ACCY_MEM_1":  (0x9D, "correct opcode, misleading name",
                                "OP_READ_1_MEM"),
        "OP_ACCY_WRITE_N":     (0x8E, "correct opcode, misleading name",
                                "OP_WRITE_N_MEM"),
        "OP_QUERY_COMMAND_VER": (0x87, "CLOCK_RATIO_CMD (sets CS fast-clock rate)",
                                 "OP_QUERY_SW_REV"),
        "OP_RD_CV_OPS_MODE":   (0x90, "DISPLAY3_CMD (writes 3 chars to CS LCD)",
                                "not supported on NCE -- no ops-mode CV read"),
        "OP_WR_CV_OPS_MODE":   (0x91, "DISPLAY4_CMD (writes 4 chars to CS LCD)",
                                "OP_QUEUE_PACKET_BASE + raw DCC packet builder"),
    }
    if name in _WITHDRAWN:
        prev, was, correct = _WITHDRAWN[name]
        raise ImportError(
            f"{name} (={prev:#04x}) was withdrawn by the 2026-07-07 opcode "
            f"audit -- that byte is actually {was}. Use {correct}."
        )
    raise AttributeError(name)
