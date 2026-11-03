"""LocoNet service-mode CV programmer (slot 124).

LocoNet programming on the programming track works by writing the
programmer-mode command into "slot 124" (``PROGRAMMING_SLOT``) via
``OPC_WR_SL_DATA`` and waiting for the command station to reply with
either:

* ``OPC_LONG_ACK`` (write complete, or read failed), **or**
* ``OPC_SL_RD_DATA`` for slot 124 carrying the read result.

The 14-byte slot-124 frame layout (per Digitrax's published spec):

```
[0]  OPC_WR_SL_DATA (0xEF)
[1]  0x0E   (length = 14)
[2]  0x7C   (slot 124)
[3]  STAT   programming status (usually 0)
[4]  PCMD   programming command -- see PCMD_* constants
[5]  0x00   reserved
[6]  HOPSA  ops-mode address high byte (0 for service mode)
[7]  LOPSA  ops-mode address low byte  (0 for service mode)
[8]  TRK    track power state (0)
[9]  CVH    bit 0=CV7, bit 1=CV8, bit 2=CV9, bit 5=DATA bit 7
[10] CVL    CV bits 0..6
[11] DATA7  data bits 0..6
[12] ID1    session id (0)
[13] checksum (XOR, added by the codec)
```

For ops-mode programming the same frame is used with PCMD's ops-mode bit
set and HOPSA/LOPSA carrying the loco address.

For Stage 9.1 we ship service-mode-direct only (the modern, decoder-
universal mode). Service-mode paged/register and the bit-level operations
are deferred to Stage 11.
"""
from __future__ import annotations

import asyncio
import logging

from pmri_core.beans.programmer import (
    Programmer,
    ProgrammerManager,
    ProgrammingMode,
)

from pmri_loconet.messages import (
    LocoNetMessage,
    OpcLongAck,
    OpcSlRdData,
    OpcWrSlData,
)
from pmri_loconet.opcodes import OPC_WR_SL_DATA
from pmri_loconet.traffic import LocoNetTrafficController

_log = logging.getLogger(__name__)

PROGRAMMING_SLOT = 124

# Programming command (PCMD) bit layout:
#   bit 6 = byte vs bit mode  (1 = byte)
#   bit 5 = read vs write     (0 = write, 1 = read -- JMRI convention)
#   bit 4 = ops vs service    (1 = ops mode)
#   bits 3..0 = sub-mode
# Direct-mode write byte: 0b0110_1000 = 0x68 (byte, write, service, direct)
# Direct-mode read byte:  0b0010_1000 = 0x28 (byte, read,  service, direct)
# These differ from one popular doc by the JMRI convention; we match JMRI.
PCMD_SERVICE_DIRECT_WRITE = 0x68
PCMD_SERVICE_DIRECT_READ  = 0x28
PCMD_OPS_MODE_WRITE       = 0x6C   # byte-write, ops mode
PCMD_OPS_MODE_READ        = 0x2C   # byte-read,  ops mode

LONG_ACK_WRITE_DONE = 0x40   # successful write
LONG_ACK_READ_FAIL  = 0x00   # decoder didn't respond -- read failed


# ---- frame builders --------------------------------------------------------


def _pack_cv_and_data(cv: int, value: int) -> tuple[int, int, int]:
    """Pack the 1-based CV# + 8-bit value into the LocoNet (CVH, CVL, DATA7) triple."""
    if not 1 <= cv <= 1024:
        raise ValueError(f"CV must be 1..1024, got {cv}")
    if not 0 <= value <= 255:
        raise ValueError(f"value must be 0..255, got {value}")
    cv0 = cv - 1
    cvl = cv0 & 0x7F
    # CV high bits go into CVH bits 0..2; data bit 7 goes into CVH bit 5.
    cvh = ((cv0 >> 7) & 0x07) | ((value & 0x80) >> 2)
    data7 = value & 0x7F
    return cvh, cvl, data7


def _unpack_data(cvh: int, data7: int) -> int:
    """Reverse of :func:`_pack_cv_and_data` for the 8-bit data byte."""
    return ((cvh & 0x20) << 2) | (data7 & 0x7F)


def _build_prog_slot_message(
    pcmd: int,
    cv: int,
    value: int,
    *,
    ops_address: int = 0,
) -> OpcWrSlData:
    """Construct a slot-124 programming command as an :class:`OpcWrSlData`.

    The codec emits this as a 14-byte ``OPC_WR_SL_DATA`` frame; the dataclass
    fields below intentionally don't map 1:1 to throttle-slot fields, but
    the wire encoder works because every byte is at the correct offset.
    """
    cvh, cvl, data7 = _pack_cv_and_data(cv, value)
    # Field assignment per the slot-124 layout:
    #   stat  -> [3]    (programming status; 0 for outgoing requests)
    #   address low 7  -> [4] ADR (== PCMD goes here per JMRI spec)
    #   speed -> [5]    (always 0)
    #   dirf  -> [6]    (HOPSA)
    #   trk   -> [7]    (LOPSA -- but in OpcWrSlData encode this is bytes[7])
    #   addr high 7    -> [9] ADR2 (== CVH)
    #   snd   -> [10]   (CVL)
    # This is bending the dataclass to a different slot layout. We document
    # the byte positions explicitly so reviewers can map them.
    return OpcWrSlData(
        slot=PROGRAMMING_SLOT,
        stat=0,
        address=cvl | (cvh << 7),   # ADR=cvl, ADR2=cvh per OpcWrSlData encode
        speed=pcmd,                  # actually placed at body[4] (PCMD)
        dirf=(ops_address >> 7) & 0x7F,   # HOPSA
        trk=ops_address & 0x7F,           # LOPSA
        snd=data7,                         # DATA7
    )


# ---- main programmer ------------------------------------------------------


class LnProgrammer:
    """Service- and ops-mode CV access against a LocoNet command station."""

    def __init__(self, tc: LocoNetTrafficController) -> None:
        self._tc = tc
        # One waiter at a time (LocoNet programming is sequential).
        self._busy = asyncio.Lock()

    async def write_cv_direct(self, cv: int, value: int, *, timeout: float = 6.0) -> None:
        """Service-mode-direct CV write. Blocks until OPC_LONG_ACK arrives."""
        async with self._busy:
            await self._send_and_wait_ack(
                _build_prog_slot_message(PCMD_SERVICE_DIRECT_WRITE, cv, value),
                timeout=timeout,
            )

    async def read_cv_direct(self, cv: int, *, timeout: float = 6.0) -> int:
        """Service-mode-direct CV read. Returns the value byte."""
        async with self._busy:
            return await self._send_and_wait_read(
                _build_prog_slot_message(PCMD_SERVICE_DIRECT_READ, cv, 0),
                timeout=timeout,
            )

    async def write_cv_ops(
        self, address: int, cv: int, value: int, *, timeout: float = 2.0,
    ) -> None:
        """Ops-mode CV write to *address*. No reply needed; we fire-and-forget."""
        async with self._busy:
            await self._tc.send(_build_prog_slot_message(
                PCMD_OPS_MODE_WRITE, cv, value, ops_address=address,
            ))

    async def read_cv_ops(
        self, address: int, cv: int, *, timeout: float = 6.0,
    ) -> int:
        """Ops-mode CV read -- requires RailCom-capable decoder + readers."""
        async with self._busy:
            return await self._send_and_wait_read(
                _build_prog_slot_message(
                    PCMD_OPS_MODE_READ, cv, 0, ops_address=address,
                ),
                timeout=timeout,
            )

    # ----- internals -------------------------------------------------------

    async def _send_and_wait_ack(self, msg: OpcWrSlData, *, timeout: float) -> None:
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()

        async def listener(inbound: LocoNetMessage) -> None:
            if future.done():
                return
            if isinstance(inbound, OpcLongAck) and inbound.responding_opcode == OPC_WR_SL_DATA:
                if inbound.ack_code == LONG_ACK_WRITE_DONE:
                    future.set_result(None)
                else:
                    future.set_exception(
                        RuntimeError(f"LocoNet programming write failed: ack={inbound.ack_code:#x}")
                    )

        disconnect = self._tc.inbound.connect(listener)
        try:
            await self._tc.send(msg)
            await asyncio.wait_for(future, timeout=timeout)
        finally:
            disconnect()

    async def _send_and_wait_read(self, msg: OpcWrSlData, *, timeout: float) -> int:
        future: asyncio.Future[int] = asyncio.get_event_loop().create_future()

        async def listener(inbound: LocoNetMessage) -> None:
            if future.done():
                return
            if isinstance(inbound, OpcSlRdData) and inbound.slot == PROGRAMMING_SLOT:
                # The slot-rd frame carries (CVH, DATA7) at the same byte
                # positions our OpcSlRdData decoded as (address_high, snd).
                cvh = (inbound.address >> 7) & 0x7F
                value = _unpack_data(cvh, inbound.snd)
                future.set_result(value)
            elif (
                isinstance(inbound, OpcLongAck)
                and inbound.responding_opcode == OPC_WR_SL_DATA
                and inbound.ack_code == LONG_ACK_READ_FAIL
            ):
                future.set_exception(
                    RuntimeError("LocoNet programming read failed (decoder did not respond)")
                )

        disconnect = self._tc.inbound.connect(listener)
        try:
            await self._tc.send(msg)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            disconnect()


# ---- ABC adapters ---------------------------------------------------------


class LnServiceProgrammer(Programmer):
    """Service-mode-direct adapter satisfying the core ABC."""

    def __init__(self, inner: LnProgrammer) -> None:
        self._inner = inner

    @property
    def mode(self) -> ProgrammingMode:
        return ProgrammingMode.SERVICE_DIRECT

    async def read_cv(self, cv: int) -> int:
        return await self._inner.read_cv_direct(cv)

    async def write_cv(self, cv: int, value: int) -> None:
        await self._inner.write_cv_direct(cv, value)


class LnOpsProgrammer(Programmer):
    """Ops-mode adapter scoped to a specific DCC address."""

    def __init__(self, inner: LnProgrammer, address: int, *, long_address: bool) -> None:
        self._inner = inner
        self._address = address
        self._long = long_address

    @property
    def mode(self) -> ProgrammingMode:
        return ProgrammingMode.OPS_MODE

    @property
    def address(self) -> int:
        return self._address

    async def read_cv(self, cv: int) -> int:
        return await self._inner.read_cv_ops(self._address, cv)

    async def write_cv(self, cv: int, value: int) -> None:
        await self._inner.write_cv_ops(self._address, cv, value)


class LnProgrammerManager(ProgrammerManager):
    """Hands out LocoNet service/ops programmers backed by one :class:`LnProgrammer`."""

    def __init__(self, inner: LnProgrammer) -> None:
        self._inner = inner
        self._service_singleton: LnServiceProgrammer | None = None

    def global_programmer(self) -> LnServiceProgrammer:
        if self._service_singleton is None:
            self._service_singleton = LnServiceProgrammer(self._inner)
        return self._service_singleton

    def ops_programmer(self, address: int, *, long_address: bool = False) -> LnOpsProgrammer:
        return LnOpsProgrammer(self._inner, address, long_address=long_address)

    @property
    def inner(self) -> LnProgrammer:
        return self._inner
