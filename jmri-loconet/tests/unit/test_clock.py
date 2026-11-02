"""Unit tests for :mod:`jmri_loconet.clock`.

The byte-level encode/decode functions are pinned against JMRI Java's
``LocoNetSlot`` getters (see the cross-reference table in
:mod:`jmri_loconet.clock`) -- if any test here starts failing, the
first thing to check is whether the wire format changed on a JMRI
release, not whether the tests are wrong.
"""
from __future__ import annotations

import pytest

from jmri_core.beans.fast_clock import HardwareClockState

from jmri_loconet.clock import (
    FAST_CLOCK_SLOT,
    build_slot123_write,
    decode_slot123,
)
from jmri_loconet.messages import OpcSlRdData


@pytest.mark.parametrize("hours", [0, 1, 6, 12, 18, 23])
def test_hours_roundtrip(hours: int) -> None:
    """Every valid 24-hour value must survive an encode+decode cycle
    unchanged. Uses the LocoNet weird "256 - 24 - h & 0x7F" encoding."""
    msg = build_slot123_write(HardwareClockState(
        hours=hours, minutes=0, seconds=0, rate=1.0, running=True,
        twenty_four_hour=True,
    ))
    decoded = decode_slot123(msg)
    assert decoded.hours == hours


@pytest.mark.parametrize("minutes", [0, 15, 30, 45, 59])
def test_minutes_roundtrip(minutes: int) -> None:
    msg = build_slot123_write(HardwareClockState(
        hours=12, minutes=minutes, seconds=0, rate=1.0, running=True,
        twenty_four_hour=True,
    ))
    assert decode_slot123(msg).minutes == minutes


@pytest.mark.parametrize("rate", [1.0, 2.0, 4.0, 8.0, 60.0])
def test_running_rate_roundtrip(rate: float) -> None:
    """Non-zero rates round-trip; the ``running`` flag is derived from
    the rate on the LocoNet side (rate=0 means stopped)."""
    msg = build_slot123_write(HardwareClockState(
        hours=12, minutes=30, seconds=0, rate=rate, running=True,
        twenty_four_hour=True,
    ))
    decoded = decode_slot123(msg)
    assert decoded.rate == rate
    assert decoded.running is True


def test_stopped_state_encodes_rate_zero() -> None:
    """``running=False`` must write rate=0 -- that's LocoNet's stopped
    convention. If we ever encode as running=True + rate=X, the CS
    would keep the clock advancing."""
    msg = build_slot123_write(HardwareClockState(
        hours=8, minutes=15, seconds=0, rate=4.0, running=False,
        twenty_four_hour=True,
    ))
    assert msg.stat == 0
    decoded = decode_slot123(msg)
    assert decoded.running is False
    assert decoded.rate == 0.0


def test_running_true_zero_rate_defaults_to_one() -> None:
    """Guard against the "running at 0x" contradiction -- asking to
    start while ``rate=0`` should send rate=1 rather than a stopped
    frame (which would be an outright lie about intent)."""
    msg = build_slot123_write(HardwareClockState(
        hours=0, minutes=0, seconds=0, rate=0.0, running=True,
        twenty_four_hour=True,
    ))
    assert msg.stat == 1


def test_decode_rejects_wrong_slot() -> None:
    """Feeding a non-clock slot into the decoder is a caller bug --
    surface it loudly so we don't silently misinterpret a real
    loco-slot reply as clock data."""
    msg = OpcSlRdData(
        slot=5, stat=0x30, address=1234, speed=0, dirf=0, trk=0, snd=0,
    )
    with pytest.raises(ValueError, match="slot=123"):
        decode_slot123(msg)


def test_slot_field_is_123() -> None:
    """Anything we build must target slot 123 -- sending to slot 5
    would clobber a loco slot with garbage."""
    msg = build_slot123_write(HardwareClockState(
        hours=0, minutes=0, seconds=0, rate=1.0, running=True,
        twenty_four_hour=True,
    ))
    assert msg.slot == FAST_CLOCK_SLOT
