from __future__ import annotations

import pytest

from jmri_openlcb.codec import (
    CTRL_AMD,
    CTRL_AME,
    CTRL_AMR,
    CTRL_RID,
    FT_CID_1,
    FT_CID_2,
    FT_CID_3,
    FT_CID_4,
    FT_CONTROL,
    FT_MTI,
    MTI_PCER,
    OlcbFrame,
    cid_seq,
    decode_frame,
    decode_pcer_if_pcer,
    encode_frame,
    encode_pcer,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
    extract_variable_field,
    is_cid,
    is_control,
    make_amd_header,
    make_ame_header,
    make_amr_header,
    make_cid_header,
    make_pcer_header,
    make_rid_header,
    read_frame,
)
from jmri_openlcb.events import EventId


def test_encode_decode_frame_round_trip() -> None:
    frame = OlcbFrame(header=0x195B4123, data=bytes.fromhex("050101014C430001"))
    text = encode_frame(frame).decode("ascii")
    parsed = decode_frame(text)
    assert parsed.header == frame.header
    assert parsed.data == frame.data


def test_decode_data_less_frame() -> None:
    parsed = decode_frame(":X195B4123;")
    assert parsed.header == 0x195B4123
    assert parsed.data == b""


def test_decode_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="not a GridConnect"):
        decode_frame("XYZ")
    with pytest.raises(ValueError, match="not a GridConnect"):
        decode_frame(":X195B4123")  # missing semicolon


def test_decode_rejects_bad_hex() -> None:
    with pytest.raises(ValueError, match="invalid"):
        decode_frame(":XZZZZZZZZ;")


def test_extract_mti_and_alias() -> None:
    header = make_pcer_header(0x456)
    assert extract_mti(header) == MTI_PCER
    assert extract_source_alias(header) == 0x456


def test_pcer_header_wire_bit_pattern() -> None:
    """Regression pin. A PCER header must be 0x195B4XXX per S-9.7.0.1:
    bit 28 (extended) + bit 27 (OpenLCB) + type nibble 001 (Global/
    Addressed) + MTI 0x5B4 + 12-bit alias. The old ``0x105B4XXX``
    encoding missing bit 27 was silently invalid on the wire -- real
    nodes classified it as a CAN control frame with an unknown
    variable field."""
    assert make_pcer_header(0x123) == 0x195B4123
    assert make_pcer_header(0x000) == 0x195B4000
    assert make_pcer_header(0xFFF) == 0x195B4FFF


def test_make_pcer_header_validates_alias() -> None:
    with pytest.raises(ValueError):
        make_pcer_header(-1)
    with pytest.raises(ValueError):
        make_pcer_header(0x1000)


# ---- frame-type / variable-field decode ----------------------------------


def test_extract_frame_type_reads_bits_24_to_27() -> None:
    """A PCER's frame type is 0x9 (bit 27=1 + bits 26..24=001)."""
    assert extract_frame_type(0x195B4123) == FT_MTI
    # RID header (control frame, variable 0x700):
    assert extract_frame_type(0x10700123) == FT_CONTROL


def test_extract_variable_field_matches_mti_bits() -> None:
    """Same bits as extract_mti -- the different name signals intent
    for control/CID frames where those 12 bits aren't an MTI."""
    assert extract_variable_field(0x10700123) == CTRL_RID
    assert extract_variable_field(0x195B4123) == MTI_PCER


# ---- CID frame headers ----------------------------------------------------


def test_make_cid_headers_have_correct_frame_type_nibbles() -> None:
    """CID 1..4 use frame types 0x7..0x4 respectively (descending).
    Locks in the sequence so a swap can't silently break alias claim."""
    alias = 0x123
    nid_slice = 0xABC
    assert (make_cid_header(1, nid_slice, alias) >> 24) & 0xF == FT_CID_1
    assert (make_cid_header(2, nid_slice, alias) >> 24) & 0xF == FT_CID_2
    assert (make_cid_header(3, nid_slice, alias) >> 24) & 0xF == FT_CID_3
    assert (make_cid_header(4, nid_slice, alias) >> 24) & 0xF == FT_CID_4


def test_make_cid_header_full_bit_pattern() -> None:
    """CID 1 with NID top-12-bits = 0x050 and alias 0x123 -> 0x17050123.
    The variable field carries the raw 12-bit NID slice, not an MTI."""
    assert make_cid_header(1, 0x050, 0x123) == 0x17050123
    assert make_cid_header(4, 0xFFF, 0xABC) == 0x14FFFABC


def test_make_cid_header_rejects_bad_seq() -> None:
    with pytest.raises(ValueError, match="CID seq"):
        make_cid_header(0, 0, 0x123)
    with pytest.raises(ValueError, match="CID seq"):
        make_cid_header(5, 0, 0x123)


def test_is_cid_recognises_all_four_variants() -> None:
    for seq in (1, 2, 3, 4):
        assert is_cid(make_cid_header(seq, 0x123, 0x456))
        assert cid_seq(make_cid_header(seq, 0x123, 0x456)) == seq


def test_is_cid_rejects_non_cid_frames() -> None:
    assert not is_cid(make_pcer_header(0x123))
    assert not is_cid(make_rid_header(0x123))


# ---- Control frame headers (RID, AMD, AME, AMR) --------------------------


def test_make_control_frame_headers_pin_bit_pattern() -> None:
    """Regression pin. Frame type nibble is 0x0 (bit 27=0, sub-type=0)
    and the variable field distinguishes the four control frames."""
    assert make_rid_header(0x123) == 0x10700123
    assert make_amd_header(0x123) == 0x10701123
    assert make_ame_header(0x123) == 0x10702123
    assert make_amr_header(0x123) == 0x10703123


def test_is_control_matches_variable_field() -> None:
    assert is_control(make_rid_header(0x1), CTRL_RID)
    assert is_control(make_amd_header(0x1), CTRL_AMD)
    assert is_control(make_ame_header(0x1), CTRL_AME)
    assert is_control(make_amr_header(0x1), CTRL_AMR)
    # Wrong variable field -> no match.
    assert not is_control(make_rid_header(0x1), CTRL_AMD)
    # PCER has frame type 0x9, not 0x0 -> not a control frame.
    assert not is_control(make_pcer_header(0x1), CTRL_RID)


def test_encode_pcer_round_trips_through_decode() -> None:
    event = EventId.parse("05.01.01.01.4C.43.00.01")
    text = encode_pcer(event, source_alias=0x456).decode()
    frame = decode_frame(text)
    pcer = decode_pcer_if_pcer(frame)
    assert pcer is not None
    assert pcer.event == event
    assert pcer.source_alias == 0x456


def test_decode_pcer_returns_none_for_non_pcer() -> None:
    frame = OlcbFrame(header=0x10000123, data=b"\x01\x02")  # not PCER MTI
    assert decode_pcer_if_pcer(frame) is None


def test_decode_pcer_returns_none_for_wrong_data_length() -> None:
    frame = OlcbFrame(header=make_pcer_header(0), data=b"\x01")
    assert decode_pcer_if_pcer(frame) is None


# --- read_frame --------------------------------------------------------------


class _Src:
    def __init__(self, data: bytes) -> None:
        self._data = list(data)
        self._i = 0

    async def read_byte(self) -> int:
        if self._i >= len(self._data):
            raise EOFError
        b = self._data[self._i]
        self._i += 1
        return b


async def test_read_frame_extracts_one() -> None:
    src = _Src(b":X195B4123N050101014C430001;")
    text = await read_frame(src.read_byte)
    assert text == ":X195B4123N050101014C430001;"


async def test_read_frame_skips_leading_noise() -> None:
    src = _Src(b"\r\nstatus\n:X1234;")
    text = await read_frame(src.read_byte)
    assert text == ":X1234;"
