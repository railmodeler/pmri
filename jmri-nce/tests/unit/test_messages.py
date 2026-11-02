"""Unit tests for :mod:`jmri_nce.messages` -- verify wire-level opcode
bytes against JMRI Java's canonical ``NceMessage.java`` constants.

Every ``assert cmd.request == bytes([...])`` here is a load-bearing
contract with the NCE command station. Changing an opcode value
without updating both this file AND the audit table in
``opcodes.py`` is what caused the 2026-07-07 EPROM corruption.
"""
from __future__ import annotations

import pytest

from jmri_nce.messages import (
    accessory_decoder_normal_ops,
    direct_mode_read_cv,
    direct_mode_write_cv,
    power_off,
    power_on,
    query_command_version,
    read_16_memory,
    read_accessory_memory,
    read_aiu,
    read_clock,
    set_clock_1224,
    set_clock_ratio,
    start_clock,
    stop_clock,
    write_clock_time,
    write_memory,
)
from jmri_nce.opcodes import (
    CS_CLOCK_ADDR,
    OP_ACCY_DECODER_OPS,
    OP_AIU_BASE_QUERY,
    OP_CLOCK_SET_1224,
    OP_CLOCK_SET_RATIO,
    OP_CLOCK_START,
    OP_CLOCK_STOP,
    OP_POWER_OFF,
    OP_POWER_ON,
    OP_QUERY_SW_REV,
    OP_RD_CV_DIRECT_MODE,
    OP_READ_16_MEM,
    OP_WR_CV_DIRECT_MODE,
    OP_WRITE_N_MEM,
)


# ----- opcode-constant sanity checks (guards against off-by-one edits) -----


def test_opcode_constants_match_jmri_canonical_values() -> None:
    """Every constant must equal the JMRI ``NceMessage.java`` decimal value
    converted to hex. Cross-checked against ``NceMonBinaryBundle.properties``.
    """
    assert OP_POWER_ON == 0x89           # ENABLE_MAIN_CMD  = 137
    assert OP_POWER_OFF == 0x8B          # KILL_MAIN_CMD    = 139
    assert OP_WRITE_N_MEM == 0x8E        # WRITE_N_CMD      = 142
    assert OP_READ_16_MEM == 0x8F        # READ16_CMD       = 143
    assert OP_CLOCK_STOP == 0x83         # STOP_CLOCK_CMD   = 131
    assert OP_CLOCK_START == 0x84        # START_CLOCK_CMD  = 132
    assert OP_CLOCK_SET_1224 == 0x86     # CLOCK_1224_CMD   = 134
    assert OP_CLOCK_SET_RATIO == 0x87    # CLOCK_RATIO_CMD  = 135
    assert OP_AIU_BASE_QUERY == 0x9B     # READ_AUI2_CMD    = 155
    assert OP_ACCY_DECODER_OPS == 0xAD   # ACC_CMD          = 173
    assert OP_WR_CV_DIRECT_MODE == 0xA8  # WRITE_DIR_CV_CMD = 168
    assert OP_RD_CV_DIRECT_MODE == 0xA9  # READ_DIR_CV_CMD  = 169
    assert OP_QUERY_SW_REV == 0xAA       # SW_REV_CMD       = 170


def test_withdrawn_opcode_names_error_at_import() -> None:
    """Referencing a pre-audit opcode name must raise so no caller accidentally
    revives a wrong byte."""
    import jmri_nce.opcodes as opcodes_module  # noqa: PLC0415
    for withdrawn in (
        "OP_READ_N_MEM", "OP_READ_ACCY_MEM", "OP_READ_ACCY_MEM_1",
        "OP_ACCY_WRITE_N", "OP_QUERY_COMMAND_VER",
        "OP_RD_CV_OPS_MODE", "OP_WR_CV_OPS_MODE",
    ):
        with pytest.raises(ImportError, match="withdrawn"):
            getattr(opcodes_module, withdrawn)


# ----- power ---------------------------------------------------------------


def test_power_on_off_encode() -> None:
    assert power_on().request == bytes([OP_POWER_ON])
    assert power_off().request == bytes([OP_POWER_OFF])
    # Both expect a single ACK (reply_len=0) with default timeout.
    assert power_on().reply_len == 0


# ----- memory read/write ---------------------------------------------------


def test_read_16_memory_encode() -> None:
    cmd = read_16_memory(0xEC00)
    assert cmd.request == bytes([OP_READ_16_MEM, 0xEC, 0x00])
    assert cmd.reply_len == 16
    assert cmd.is_read is True


def test_read_accessory_memory_is_alias_for_read_16() -> None:
    """Turnout-monitor call site uses the old name -- it now delegates."""
    assert read_accessory_memory(0xEC00).request == read_16_memory(0xEC00).request


def test_write_memory_encode_is_padded_to_20_bytes() -> None:
    """NCE's ``accMemoryWriteN`` requires a **fixed 20-byte** request --
    JMRI Java allocates ``new byte[20]`` and fills only ``4 + data.length``.
    Without the pad the CS waits forever for the remaining bytes and
    the ACK never arrives (this was the 2026-07-07 write-timeout bug).
    """
    cmd = write_memory(0xEC00, bytes([0x01, 0x02, 0x03]))
    expected = (
        bytes([OP_WRITE_N_MEM, 0xEC, 0x00, 3, 1, 2, 3])
        + bytes(13)   # zero-padding out to 20 bytes total
    )
    assert cmd.request == expected
    assert len(cmd.request) == 20
    assert cmd.reply_len == 0


def test_write_memory_rejects_empty_or_oversize() -> None:
    with pytest.raises(ValueError):
        write_memory(0, b"")
    with pytest.raises(ValueError):
        write_memory(0, bytes(17))


# ----- accessory decoder ---------------------------------------------------


def test_accessory_decoder_ops_encode_closed() -> None:
    cmd = accessory_decoder_normal_ops(12, on=True)
    assert cmd.request == bytes([OP_ACCY_DECODER_OPS, 0x00, 12, 3, 0x00])


def test_accessory_decoder_ops_encode_thrown() -> None:
    cmd = accessory_decoder_normal_ops(513, on=False)
    assert cmd.request == bytes([OP_ACCY_DECODER_OPS, 0x02, 0x01, 4, 0x00])


@pytest.mark.parametrize("addr", [0, 2045, -1])
def test_accessory_decoder_address_validated(addr: int) -> None:
    with pytest.raises(ValueError):
        accessory_decoder_normal_ops(addr, on=True)


# ----- AIU -----------------------------------------------------------------


def test_read_aiu_encode() -> None:
    cmd = read_aiu(7)
    assert cmd.request == bytes([OP_AIU_BASE_QUERY, 7])
    assert cmd.reply_len == 2


@pytest.mark.parametrize("aiu", [0, 64, -1])
def test_read_aiu_address_validated(aiu: int) -> None:
    with pytest.raises(ValueError):
        read_aiu(aiu)


# ----- CV programming ------------------------------------------------------


def test_direct_mode_write_cv_encode() -> None:
    # JMRI: setOpCode(168) -> [0xA8, cv_hi, cv_lo, value].
    cmd = direct_mode_write_cv(3, 42)
    assert cmd.request == bytes([OP_WR_CV_DIRECT_MODE, 0x00, 0x03, 42])


def test_direct_mode_read_cv_encode() -> None:
    # JMRI: setOpCode(169) -> [0xA9, cv_hi, cv_lo].
    cmd = direct_mode_read_cv(29)
    assert cmd.request == bytes([OP_RD_CV_DIRECT_MODE, 0x00, 0x1D])
    assert cmd.reply_len == 1
    assert cmd.is_read is True


# ----- ops-mode POM (Programming On the Main) -----------------------------
#
# Wire form: raw NMRA CV Access byte-write packet wrapped in
# QUEUEn_TMP_CMD. Opcode = 0x93 + (packet_len - 3). NMRA CV Access
# instruction: 1110 CCVV VVVVVVVV DDDDDDDD (CC=11 for byte-write).


def test_ops_mode_write_cv_short_address_encode() -> None:
    """Short address (1..127), CV1 = 3.

    Wire form (verified against JMRI Java NCE Command Monitor on real
    PowerPro hardware):

        [0x95, send_count=5, addr, 0xEC|cv_hi, cv_lo, data, xor]

    * ``0x95`` = QUEUE5_TMP_CMD (queue 5-byte DCC packet)
    * ``send_count=5`` -- how many times the CS retransmits the packet
      on the wire (matches JMRI Java's default)
    * CV1 -> cv_index=0 -> 0xEC + 0 = 0xEC (inst_hi), 0x00 (inst_lo)
    * XOR: 0x03 ^ 0xEC ^ 0x00 ^ 0x00 ^ 0x03 = 0xEC
    """
    from jmri_nce.messages import ops_mode_write_cv  # noqa: PLC0415
    cmd = ops_mode_write_cv(3, cv=1, value=3)
    assert cmd.request == bytes([
        0x95,               # QUEUE5_TMP_CMD (5-byte payload)
        0x05,               # send-count = 5 transmissions
        0x03,               # short address 3
        0xEC, 0x00,         # CV Access byte-write, cv_index=0
        0x03,               # value 3
        0x03 ^ 0xEC ^ 0x00 ^ 0x00 ^ 0x03,   # XOR checksum
    ])
    assert cmd.reply_len == 0
    assert cmd.is_read is False


def test_ops_mode_write_cv_long_address_encode() -> None:
    """Long address 2065 (dec) = 0x811, CV29 = 34.

    * addr_hi = 0xC0 | (0x811 >> 8 & 0x3F) = 0xC0 | 0x08 = 0xC8
    * addr_lo = 0x811 & 0xFF = 0x11
    * CV29 -> cv_index=28 -> inst_hi=0xEC, inst_lo=0x1C
    * value 34 = 0x22
    * 6-byte packet -> QUEUE6_TMP_CMD = 0x93 + 3 = 0x96
    * send-count = 5 (byte 1)
    """
    from jmri_nce.messages import ops_mode_write_cv  # noqa: PLC0415
    cmd = ops_mode_write_cv(2065, cv=29, value=34, long_address=True)
    body = [0xC8, 0x11, 0xEC, 0x1C, 0x22]
    xor = 0
    for b in body:
        xor ^= b
    assert cmd.request == bytes([0x96, 0x05, *body, xor])


def test_ops_mode_write_cv_high_cv_uses_upper_bits_in_inst_hi() -> None:
    """CV > 256 pushes bits into the low 2 of the instruction byte.

    CV 512 -> cv_index=511=0x1FF -> inst_hi = 0xEC | 0x01 = 0xED,
    inst_lo = 0xFF. Byte offsets shift by 1 because of the send-count
    byte at index 1.
    """
    from jmri_nce.messages import ops_mode_write_cv  # noqa: PLC0415
    cmd = ops_mode_write_cv(3, cv=512, value=7)
    # [0]=opcode, [1]=send_count, [2]=addr, [3]=inst_hi, [4]=inst_lo
    assert cmd.request[2] == 0x03          # short address
    assert cmd.request[3] == 0xED          # inst_hi with high bit 1
    assert cmd.request[4] == 0xFF          # inst_lo


def test_ops_mode_write_cv_uses_generous_timeout() -> None:
    """Ops-mode POM writes queue at the CS, get transmitted on the
    main-track DCC wire, and the ack comes back after transmission.
    On busy hardware (active throttles + fast-clock + accessory
    refresh) we've observed >2 s of latency. JMRI Java uses ~5 s for
    equivalent operations. Anchor here so a future regression doesn't
    silently drop it below what real hardware needs."""
    from jmri_nce.messages import ops_mode_write_cv  # noqa: PLC0415
    cmd = ops_mode_write_cv(3, cv=29, value=3)
    assert cmd.timeout_s >= 5.0


def test_ops_mode_write_cv_xor_covers_every_byte_before_checksum() -> None:
    """The XOR is over the DCC-packet bytes only (address + instruction
    + data). The queue opcode and send-count byte are NCE envelope,
    not part of the DCC packet, and don't participate in the XOR."""
    from jmri_nce.messages import ops_mode_write_cv  # noqa: PLC0415
    cmd = ops_mode_write_cv(3, cv=17, value=200)
    # request = [opcode, send_count, ...dcc_packet...]
    # dcc_packet = [addr, inst_hi, inst_lo, value, xor]
    dcc_body = cmd.request[2:-1]   # drop opcode, send_count, trailing XOR
    xor = 0
    for b in dcc_body:
        xor ^= b
    assert cmd.request[-1] == xor


@pytest.mark.parametrize(
    "loco,cv,value,long_addr",
    [
        (0,   1, 0, False),      # short 0 is illegal (broadcast)
        (128, 1, 0, False),      # 128+ requires long address flag
        (1,   0, 0, False),      # CV 0 illegal (< 1)
        (1,   1025, 0, False),   # CV > 1024 illegal
        (1,   1, -1, False),     # value < 0
        (1,   1, 256, False),    # value > 255
        (127, 1, 0, True),       # long 127 too small (< 128)
        (10000, 1, 0, True),     # long > 9999
    ],
)
def test_ops_mode_write_cv_validates_ranges(
    loco: int, cv: int, value: int, long_addr: bool,
) -> None:
    from jmri_nce.messages import ops_mode_write_cv  # noqa: PLC0415
    with pytest.raises(ValueError):
        ops_mode_write_cv(loco, cv=cv, value=value, long_address=long_addr)


# ----- firmware version ----------------------------------------------------


def test_query_command_version_encode() -> None:
    # JMRI: getNceEpromRev = single opcode 0xAA, reply 3 bytes.
    cmd = query_command_version()
    assert cmd.request == bytes([OP_QUERY_SW_REV])
    assert cmd.reply_len == 3
    assert cmd.is_read is True


# ----- fast-clock commands -------------------------------------------------


def test_read_clock_encode() -> None:
    cmd = read_clock()
    # 3-byte request reading 16 bytes at 0xDC00 -- byte-for-byte identical
    # to JMRI Java's NceClockControl clock read (opcode 0x8F = READ16_CMD).
    assert cmd.request == bytes([OP_READ_16_MEM, 0xDC, 0x00])
    assert cmd.reply_len == 16
    assert cmd.is_read is True
    # sanity: the constant matches the encoded address.
    assert CS_CLOCK_ADDR == 0xDC00


def test_write_clock_time_encode() -> None:
    # JMRI writes [ss, mm, hh] at clock_addr + 2 via opcode 0x8E and
    # pads the request to 20 bytes total. Match that byte-for-byte.
    cmd = write_clock_time(7, 30, 0)
    expected = (
        bytes([OP_WRITE_N_MEM, 0xDC, 0x02, 3, 0, 30, 7])
        + bytes(13)
    )
    assert cmd.request == expected
    assert len(cmd.request) == 20


@pytest.mark.parametrize(
    ("h", "m", "s"),
    [(-1, 0, 0), (24, 0, 0), (0, -1, 0), (0, 60, 0), (0, 0, -1), (0, 0, 60)],
)
def test_write_clock_time_validated(h: int, m: int, s: int) -> None:
    with pytest.raises(ValueError):
        write_clock_time(h, m, s)


def test_set_clock_ratio_encode() -> None:
    assert set_clock_ratio(8).request == bytes([OP_CLOCK_SET_RATIO, 8])


@pytest.mark.parametrize("ratio", [0, 16, -1])
def test_set_clock_ratio_validated(ratio: int) -> None:
    with pytest.raises(ValueError):
        set_clock_ratio(ratio)


def test_set_clock_1224_encode() -> None:
    assert set_clock_1224(True).request == bytes([OP_CLOCK_SET_1224, 1])
    assert set_clock_1224(False).request == bytes([OP_CLOCK_SET_1224, 0])


def test_start_stop_clock_encode() -> None:
    assert start_clock().request == bytes([OP_CLOCK_START])
    assert stop_clock().request == bytes([OP_CLOCK_STOP])
