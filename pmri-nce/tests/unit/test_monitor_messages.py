from __future__ import annotations

from pmri_ncemonitor.messages import (
    ADDR_ACCESSORY,
    ADDR_AIU_UPDATE,
    ADDR_FAST_CLOCK,
    AccessoryObserved,
    AiuUpdate,
    CabBusUnknown,
    CabPoll,
    CabResponse,
    FastClockUpdate,
    decode,
    encode,
)


def test_cab_poll_roundtrip() -> None:
    msg = CabPoll(address=3, command=0x00)
    out = decode(encode(msg))
    assert isinstance(out, CabPoll)
    assert out.address == 3
    assert out.command == 0x00


def test_cab_response_roundtrip() -> None:
    msg = CabResponse(
        address=5, key=0x10, speed=64, direction=1,
        function_low=0b10001, function_high=0b0101,
    )
    out = decode(encode(msg))
    assert isinstance(out, CabResponse)
    assert out.address == 5
    assert out.speed == 64
    assert out.direction == 1
    assert out.function_low == 0b10001
    assert out.function_high == 0b0101


def test_fast_clock_roundtrip() -> None:
    msg = FastClockUpdate(address=ADDR_FAST_CLOCK, hour=14, minute=35)
    out = decode(encode(msg))
    assert isinstance(out, FastClockUpdate)
    assert out.hour == 14 and out.minute == 35


def test_accessory_observed_roundtrip() -> None:
    msg = AccessoryObserved(address=ADDR_ACCESSORY, accessory_address=257, closed=True)
    out = decode(encode(msg))
    assert isinstance(out, AccessoryObserved)
    assert out.accessory_address == 257
    assert out.closed is True


def test_aiu_update_roundtrip() -> None:
    msg = AiuUpdate(address=ADDR_AIU_UPDATE, aiu_module=2, input_word=0x0F03)
    out = decode(encode(msg))
    assert isinstance(out, AiuUpdate)
    assert out.aiu_module == 2
    # input_word is reconstructed from 7-bit nibbles so we lose the MSB --
    # verify low 14 bits round-trip cleanly.
    assert out.input_word & 0x3FFF == 0x0F03 & 0x3FFF


def test_unknown_decodes_as_cabbusunknown() -> None:
    # Master fast-clock-like address but unexpected length.
    frame = bytes([0x80 | ADDR_FAST_CLOCK])  # no data bytes
    out = decode(frame)
    assert isinstance(out, CabBusUnknown)
    assert out.address == ADDR_FAST_CLOCK


def test_decode_rejects_non_address_first_byte() -> None:
    import pytest  # noqa: PLC0415
    with pytest.raises(ValueError, match="address byte"):
        decode(bytes([0x00, 0x01]))
