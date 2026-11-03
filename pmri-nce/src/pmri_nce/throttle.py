"""NCE throttle support -- modern firmware (commandOptions >= 10) path.

The throttle is driven via the NCE binary ``nceLocoCmd`` (opcode 0xA2),
not raw DCC packets. JMRI Java's ``NceThrottle.sendA2command`` defaults
to True for any firmware option >= 10, so this is the right path on
PowerCab, PowerPro, and SB5 with current firmware. The legacy
``sendPacketMessage`` opcodes (0x93..0x96) collide with the CV
write/read commands at the same opcode values; sending throttle
packets that way confuses the CS.

Per-message wire form (5 bytes):

    [0xA2] [addr_hi] [addr_lo] [sub_cmd] [data]

* speed change -> sub_cmd 3/4 (rev/fwd 128-step), data = 0..126
* emergency stop -> sub_cmd 5/6 (rev/fwd estop), data = 0
* F0..F4 -> sub_cmd 7, data bits: F0=bit4, F1=bit0, F2=bit1, F3=bit2, F4=bit3
* F5..F8 -> sub_cmd 8, data bits: F5=bit0, F6=bit1, F7=bit2, F8=bit3
* F9..F12 -> sub_cmd 9, same bit ordering
* F13..F20 -> sub_cmd 21, data = bit-per-function (F13=bit0..F20=bit7)
* F21..F28 -> sub_cmd 22, data = bit-per-function (F21=bit0..F28=bit7)
"""
from __future__ import annotations

import logging

from pmri_core.beans.throttle import Throttle
from pmri_core.beans.throttle_manager import ThrottleManager
from pmri_core.state import ThrottleDirection

from pmri_nce.messages import nce_loco_cmd
from pmri_nce.opcodes import (
    LOCO_SUB_128_FWD,
    LOCO_SUB_128_REV,
    LOCO_SUB_ESTOP_FWD,
    LOCO_SUB_ESTOP_REV,
    LOCO_SUB_FUNC_GROUP_1,
    LOCO_SUB_FUNC_GROUP_2,
    LOCO_SUB_FUNC_GROUP_3,
    LOCO_SUB_FUNC_GROUP_4,
    LOCO_SUB_FUNC_GROUP_5,
)
from pmri_nce.traffic import NceTrafficController

_log = logging.getLogger(__name__)


def speed_to_wire_128(speed: float) -> int:
    """Map [-1.0, 1.0] throttle speed to NCE 128-step magnitude.

    NCE uses 0..126 (not 0..127 like NMRA raw packet). Direction is in
    the sub-command byte. ``speed == -1.0`` is the e-stop sentinel and
    is handled by the throttle's e-stop path, not via this mapping.
    """
    mag = abs(speed)
    if mag == 0.0:
        return 0
    value = round(126 * mag)
    return max(1, min(126, value))


def _func_group_1_data(f: list[bool]) -> int:
    """F0..F4 bitmask -- F0 is bit 4, F1..F4 are bits 0..3 (NCE convention)."""
    return (
        (16 if f[0] else 0)
        | (1 if f[1] else 0)
        | (2 if f[2] else 0)
        | (4 if f[3] else 0)
        | (8 if f[4] else 0)
    )


def _func_group_2_data(f: list[bool]) -> int:
    """F5..F8 -- F5..F8 in bits 0..3."""
    return (
        (1 if f[5] else 0)
        | (2 if f[6] else 0)
        | (4 if f[7] else 0)
        | (8 if f[8] else 0)
    )


def _func_group_3_data(f: list[bool]) -> int:
    """F9..F12 -- F9..F12 in bits 0..3."""
    return (
        (1 if f[9] else 0)
        | (2 if f[10] else 0)
        | (4 if f[11] else 0)
        | (8 if f[12] else 0)
    )


def _func_group_4_data(f: list[bool]) -> int:
    """F13..F20 -- bit-per-function, F13=bit0..F20=bit7."""
    data = 0
    for i in range(8):
        if f[13 + i]:
            data |= 1 << i
    return data


def _func_group_5_data(f: list[bool]) -> int:
    """F21..F28 -- bit-per-function, F21=bit0..F28=bit7."""
    data = 0
    for i in range(8):
        if f[21 + i]:
            data |= 1 << i
    return data


class NceThrottle(Throttle):
    """Throttle driving an NCE command station via the 0xA2 loco command."""

    def __init__(
        self, address: int, tc: NceTrafficController, *, long_address: bool = False
    ) -> None:
        super().__init__(address=address, long_address=long_address)
        self._tc = tc

    async def set_speed(self, speed: float) -> None:
        await super().set_speed(speed)
        await self._push_speed()

    async def set_direction(self, direction: ThrottleDirection) -> None:
        await super().set_direction(direction)
        await self._push_speed()

    async def set_function(self, index: int, on: bool) -> None:
        await super().set_function(index, on)
        if index <= 4:
            await self._push_group(LOCO_SUB_FUNC_GROUP_1, _func_group_1_data(self.functions))
        elif index <= 8:
            await self._push_group(LOCO_SUB_FUNC_GROUP_2, _func_group_2_data(self.functions))
        elif index <= 12:
            await self._push_group(LOCO_SUB_FUNC_GROUP_3, _func_group_3_data(self.functions))
        elif index <= 20:
            await self._push_group(LOCO_SUB_FUNC_GROUP_4, _func_group_4_data(self.functions))
        else:
            await self._push_group(LOCO_SUB_FUNC_GROUP_5, _func_group_5_data(self.functions))

    # ----- helpers --------------------------------------------------------

    async def _push_speed(self) -> None:
        forward = self.direction == ThrottleDirection.FORWARD
        if self.speed == -1.0:
            sub = LOCO_SUB_ESTOP_FWD if forward else LOCO_SUB_ESTOP_REV
            await self._send(sub, 0)
            return
        sub = LOCO_SUB_128_FWD if forward else LOCO_SUB_128_REV
        await self._send(sub, speed_to_wire_128(self.speed))

    async def _push_group(self, sub_cmd: int, data: int) -> None:
        await self._send(sub_cmd, data)

    async def _send(self, sub_cmd: int, data: int) -> None:
        cmd = nce_loco_cmd(
            self.address,
            long_address=self.long_address,
            sub_cmd=sub_cmd,
            data=data,
        )
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug(
                "NCE throttle out: %s bytes=%s",
                cmd.name, " ".join(f"{b:02X}" for b in cmd.request),
            )
        await self._tc.send_command(cmd)


class NceThrottleManager(ThrottleManager):
    """NCE throttle manager -- uses the core ThrottleManager hook."""

    def __init__(self, system_prefix: str, *, tc: NceTrafficController) -> None:
        super().__init__(system_prefix)
        self._tc = tc

    async def _construct(self, address: int, *, long_address: bool) -> NceThrottle:
        return NceThrottle(address=address, tc=self._tc, long_address=long_address)
