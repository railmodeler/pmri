"""Tests for the Stage 4.5 additions: ThrottleObserved and
ProgrammingResponse, plus Tier 4 #13 additions ConsistObserved,
MacroObserved, MasterAck.
"""
from __future__ import annotations

from jmri_ncemonitor.messages import (
    ADDR_CONSIST_CMD,
    ADDR_MACRO_CMD,
    ADDR_MASTER_ACK,
    ADDR_PROG_RESP,
    ADDR_THROTTLE_CMD,
    ConsistObserved,
    MacroObserved,
    MasterAck,
    ProgrammingResponse,
    ThrottleObserved,
    decode,
    encode,
)


def test_throttle_observed_roundtrip() -> None:
    msg = ThrottleObserved(
        address=ADDR_THROTTLE_CMD,
        dcc_address=3601,
        speed=64,
        forward=True,
        functions=0b01010,
    )
    out = decode(encode(msg))
    assert isinstance(out, ThrottleObserved)
    assert out.dcc_address == 3601
    assert out.speed == 64
    assert out.forward is True
    assert out.functions == 0b01010


def test_throttle_observed_reverse() -> None:
    msg = ThrottleObserved(
        address=ADDR_THROTTLE_CMD,
        dcc_address=3, speed=0, forward=False, functions=0,
    )
    out = decode(encode(msg))
    assert isinstance(out, ThrottleObserved)
    assert out.forward is False


def test_programming_response_roundtrip() -> None:
    msg = ProgrammingResponse(
        address=ADDR_PROG_RESP, cv=29, value=6, operation=0,
    )
    out = decode(encode(msg))
    assert isinstance(out, ProgrammingResponse)
    assert out.cv == 29
    assert out.value == 6
    assert out.operation == 0


def test_programming_response_with_higher_cv() -> None:
    msg = ProgrammingResponse(
        address=ADDR_PROG_RESP, cv=257, value=99, operation=1,
    )
    out = decode(encode(msg))
    assert isinstance(out, ProgrammingResponse)
    assert out.cv == 257
    assert out.operation == 1


# ---- Tier 4 #13 additions ----------------------------------------------


def test_consist_observed_roundtrip() -> None:
    """Consist number (1..127) + loco address (14-bit) + operation
    round-trip through encode/decode."""
    msg = ConsistObserved(
        address=ADDR_CONSIST_CMD,
        consist_number=42,
        loco_address=3601,
        operation=0,
    )
    out = decode(encode(msg))
    assert isinstance(out, ConsistObserved)
    assert out.consist_number == 42
    assert out.loco_address == 3601
    assert out.operation == 0


def test_consist_observed_remove_operation() -> None:
    """Operation code 1 = remove-from-consist."""
    msg = ConsistObserved(
        address=ADDR_CONSIST_CMD,
        consist_number=5, loco_address=99, operation=1,
    )
    out = decode(encode(msg))
    assert isinstance(out, ConsistObserved)
    assert out.operation == 1


def test_macro_observed_roundtrip() -> None:
    msg = MacroObserved(address=ADDR_MACRO_CMD, macro_number=17)
    out = decode(encode(msg))
    assert isinstance(out, MacroObserved)
    assert out.macro_number == 17


def test_master_ack_roundtrip() -> None:
    msg = MasterAck(address=ADDR_MASTER_ACK, code=0)
    out = decode(encode(msg))
    assert isinstance(out, MasterAck)
    assert out.code == 0


def test_master_ack_non_zero_code() -> None:
    """A non-zero ack indicates the master saw an error condition."""
    msg = MasterAck(address=ADDR_MASTER_ACK, code=0x42)
    out = decode(encode(msg))
    assert isinstance(out, MasterAck)
    assert out.code == 0x42
