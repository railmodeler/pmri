"""DCC-EX throttle wiring.

DCC-EX uses bracket-delimited ASCII for throttle commands:

* ``<t cab speed direction>`` -- set speed/direction for a cab.
  * ``cab`` = DCC address (long addresses >=128 work directly).
  * ``speed`` = 0..127 for step 0..127, OR ``-1`` for **emergency stop**.
    Speed 0 = normal stop (motor coast); speed -1 = e-stop (motor cut).
    Verified against https://dcc-ex.com/reference/software/command-summary-consolidated.html
  * ``direction`` = 1 (forward) or 0 (reverse).
* ``<F cab function 1|0>`` -- set function on/off.

The DCC-EX command station tracks loco state internally; we treat
throttle commands as fire-and-forget. Subsequent ``<l>`` notifications
from the CS update bean state if a feedback channel is wired.
"""
from __future__ import annotations

from pmri_core.beans.throttle import Throttle
from pmri_core.beans.throttle_manager import ThrottleManager
from pmri_core.state import ThrottleDirection

from pmri_dccex.codec import DccexMessage
from pmri_dccex.traffic import DccexTrafficController


def speed_to_wire_128(speed: float) -> int:
    """Map throttle speed to DCC-EX's wire form.

    * ``-1.0`` -> -1 (DCC-EX emergency stop -- decoder cuts motor)
    * ``0.0`` -> 0 (normal stop -- motor coasts to rest)
    * positive [0..1] maps linearly to steps 1..127

    Note the wire form is not a straight 128-step DCC value: DCC-EX
    reserves ``-1`` as an out-of-band e-stop signal, so we cannot use
    step 1 as the e-stop code the way the NMRA 128-step encoding does.
    Sending wire=1 to DCC-EX runs the loco at its slowest step, which
    is why our earlier "1 == e-stop" mapping left locomotives creeping
    after the user hit e-stop.
    """
    if speed == -1.0:
        return -1
    if speed == 0.0:
        return 0
    return min(127, max(1, round(abs(speed) * 127)))


class DccexThrottle(Throttle):
    """DCC-EX-aware throttle. Sends ``<t cab speed dir>`` on speed/direction."""

    def __init__(
        self, address: int, tc: DccexTrafficController, *, long_address: bool = False
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
        # DCC-EX <F cab function 1|0>.
        await self._tc.send(DccexMessage(
            command="F", args=(str(self.address), str(index), "1" if on else "0"),
        ))

    async def _push_speed(self) -> None:
        direction = "1" if self.direction == ThrottleDirection.FORWARD else "0"
        wire = speed_to_wire_128(self.speed)
        await self._tc.send(DccexMessage(
            command="t", args=(str(self.address), str(wire), direction),
        ))


class DccexThrottleManager(ThrottleManager):
    """DCC-EX throttle manager -- uses the core ThrottleManager hook."""

    def __init__(self, system_prefix: str, *, tc: DccexTrafficController) -> None:
        super().__init__(system_prefix)
        self._tc = tc

    async def _construct(self, address: int, *, long_address: bool) -> DccexThrottle:
        return DccexThrottle(address=address, tc=self._tc, long_address=long_address)
