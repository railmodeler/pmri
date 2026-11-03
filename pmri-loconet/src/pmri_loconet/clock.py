"""LocoNet fast-clock slot 123 (0x7B) support.

The command station keeps its fast-clock state in slot 123. Reading it
is a normal ``OPC_RQ_SL_DATA`` for that slot, whose reply is a
14-byte ``OPC_SL_RD_DATA`` frame carrying the hours / minutes /
fractional-minutes / rate / days encoded across the general
slot-record fields.

Byte-layout mapping -- cross-checked against JMRI Java's
``jmri.jmrix.loconet.LocoNetSlot`` getters (see ``getFcHours``,
``getFcMinutes``, ``getFcFracMins``, ``getFcRate``, ``getFcDays``)
and ``LnClockControl`` for the roles those fields play:

* ``stat``            -> fast-clock rate (0 = stopped, 1..N = running at Nx).
* ``dirf``            -> minutes, encoded as ``(255 - 60 - minutes) & 0x7F``.
* ``ss2``             -> hours,   encoded as ``(256 - 24 - hours)   & 0x7F``.
* ``addr`` low 7 bits -> fractional-minutes low byte (a 14-bit
                        counter counting down from ~915 within the
                        current minute).
* ``addr`` bits 7..13 -> days (7-bit day counter).
* ``spd`` low 7 bits  -> fractional-minutes high byte.
* ``trk`` bit 0       -> track power echo (retain what the CS sent).
* ``snd``, ``id1``,
  ``id2``             -> unused for slot 123; preserve on write.
"""
from __future__ import annotations

from pmri_core.beans.fast_clock import HardwareClockState

from pmri_loconet.messages import OpcSlRdData, OpcWrSlData

FAST_CLOCK_SLOT = 123

# Fractional-minute counter roughly counts down from 915 over the
# course of one CS minute. Precise ceiling is CS-firmware-dependent
# but 915 is what JMRI Java uses. We only use it to derive the
# seconds field of :class:`HardwareClockState`.
_FRAC_MIN_MAX = 915


def _decode_hours(ss2: int) -> int:
    """Decode the hours field from the ``ss2`` byte.

    LocoNet's fast-clock convention: hours stored as ``(24 - h) % 24``.
    Symmetric on both sides so encode + decode round-trip cleanly.
    The exact wire values match JMRI Java's ``LocoNetSlot.setFcHours``
    on the write path (0 encodes as 24 & 0x7F = 24, 12 encodes as 12,
    23 encodes as 1); JMRI Java's ``getFcHours`` decompiles into a
    formula that doesn't cleanly invert its own ``setFcHours`` for
    non-canonical hours, so we favour a self-consistent pair here and
    match its wire form on encode. Real CS behaviour has always been
    the ground truth -- a decoded value that displays wrong on a
    Digitrax throttle is the signal to revisit."""
    return (24 - (ss2 & 0x7F)) % 24


def _encode_hours(hours: int) -> int:
    return (24 - hours) & 0x7F


def _decode_minutes(dirf: int) -> int:
    return (60 - (dirf & 0x7F)) % 60


def _encode_minutes(minutes: int) -> int:
    return (60 - minutes) & 0x7F


def _decode_seconds(addr: int, spd: int) -> int:
    """Approximate seconds from the fractional-minute counter.

    JMRI Java's ``LnClockControl`` uses this same formula (multiplied
    by 60000ms) when synchronising the internal clock -- we return
    integer seconds because :class:`HardwareClockState` doesn't carry
    sub-second precision.
    """
    frac = (addr & 0x7F) | ((spd & 0x7F) << 8)
    if frac >= _FRAC_MIN_MAX:
        return 0
    remainder = (_FRAC_MIN_MAX - frac) / _FRAC_MIN_MAX * 60.0
    return max(0, min(59, int(remainder)))


def _encode_days_and_frac(days: int, frac: int) -> int:
    """Combine the 7-bit days field + 14-bit fractional counter into
    the 14-bit ``addr`` value the wire carries. Both are masked."""
    return ((days & 0x7F) << 7) | (frac & 0x7F)


def _encode_frac_high(frac: int) -> int:
    """The high 7 bits of the 14-bit fractional counter land in the
    ``spd`` byte."""
    return (frac >> 8) & 0x7F


def decode_slot123(msg: OpcSlRdData | OpcWrSlData) -> HardwareClockState:
    """Turn a slot-123 record into a :class:`HardwareClockState`.

    LocoNet doesn't carry a separate "running" flag -- the CS reports
    running as ``rate > 0`` and stopped as ``rate == 0``. We map that
    into ``HardwareClockState.running`` accordingly. LocoNet's clock
    is always 24-hour (JMRI Java hardcodes ``twenty_four_hour = True``
    in :class:`LnClockControl`).
    """
    if msg.slot != FAST_CLOCK_SLOT:
        raise ValueError(
            f"decode_slot123 expects slot={FAST_CLOCK_SLOT}, got {msg.slot}"
        )
    rate_byte = msg.stat & 0x7F
    return HardwareClockState(
        hours=_decode_hours(msg.ss2),
        minutes=_decode_minutes(msg.dirf),
        seconds=_decode_seconds(msg.address, msg.speed),
        # LocoNet stores rate as an integer; expose it as a float so
        # :class:`HardwareClockState` stays uniform across drivers.
        rate=float(rate_byte),
        running=rate_byte > 0,
        twenty_four_hour=True,
    )


def build_slot123_write(
    state: HardwareClockState,
    *,
    days: int = 0,
    trk: int = 0,
    snd: int = 0,
) -> OpcWrSlData:
    """Turn a :class:`HardwareClockState` into a slot-123 write frame.

    Optional ``days`` / ``trk`` / ``snd`` let callers preserve the
    non-clock fields from a previous read -- the CS keeps track of
    those and we shouldn't stomp them by writing zeros. Default 0
    is safe for a first-time initialization.

    ``state.running=False`` writes rate=0 (LocoNet's stopped
    convention). ``running=True`` with ``rate=0`` writes rate=1 to
    avoid a "running at 0x" contradiction.
    """
    if state.running:
        rate = int(state.rate) or 1
    else:
        rate = 0
    # Fractional-minute counter -- set to zero on write, meaning the
    # CS treats the current minute as "just started".
    frac = 0
    return OpcWrSlData(
        slot=FAST_CLOCK_SLOT,
        stat=rate & 0x7F,
        address=_encode_days_and_frac(days, frac),
        speed=_encode_frac_high(frac),
        dirf=_encode_minutes(state.minutes),
        trk=trk & 0x7F,
        ss2=_encode_hours(state.hours),
        snd=snd & 0x7F,
    )
