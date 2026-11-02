"""DCC-EX-aware beans + their managers + power manager.

Wire-protocol summary (CommandStation-EX docs, command-summary-consolidated):

* Turnouts: ``<T id state>`` where state 0 = CLOSED, 1 = THROWN.
  Response: ``<H id state>`` using the same encoding.
  Note: this is the OPPOSITE of the NMRA DCC packet convention where
  bit 0 of the accessory data byte is 1 = "closed / normal". DCC-EX
  chose the human-facing "0 = at rest, 1 = deflected" mapping instead.
  Do not "correct" this to match DCC -- verified against
  https://dcc-ex.com/reference/software/command-summary-consolidated.html
* Sensors:  ``<Q id>`` = active, ``<q id>`` = inactive (server -> client).
* Power:    ``<1>`` = on, ``<0>`` = off. Response: ``<p state>``.
"""
from __future__ import annotations

import contextlib

from jmri_core.beans.power import PowerManager
from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.manager import Manager
from jmri_core.state import PowerState, SensorState, TurnoutState

from jmri_dccex.codec import DccexMessage
from jmri_dccex.traffic import DccexTrafficController

# ----- Turnout -------------------------------------------------------------


class DccexTurnout(Turnout):
    """Sends ``<T id state>`` on commanded-state change."""

    __slots__ = ("_address", "_tc")

    def __init__(self, system_name: str, address: int, tc: DccexTrafficController) -> None:
        super().__init__(system_name)
        self._address = address
        self._tc = tc

    @property
    def address(self) -> int:
        return self._address

    async def set_commanded_state(self, state: TurnoutState) -> None:
        # DCC-EX: 0 = CLOSED, 1 = THROWN (opposite of the DCC accessory
        # packet convention -- see module docstring).
        wire = 0 if state == TurnoutState.CLOSED else 1
        await self._tc.send(DccexMessage(command="T", args=(str(self._address), str(wire))))
        await super().set_commanded_state(state)


class DccexTurnoutManager(Manager[DccexTurnout]):
    type_letter = "T"

    def __init__(self, system_prefix: str, *, tc: DccexTrafficController) -> None:
        super().__init__(system_prefix)
        self._tc = tc
        self._by_address: dict[int, DccexTurnout] = {}
        self._unsub = tc.inbound.connect(self._on_message)

    def _create(self, system_name: str) -> DccexTurnout:
        address = _parse_address(system_name, self.full_prefix)
        bean = DccexTurnout(system_name, address, self._tc)
        self._by_address[address] = bean
        return bean

    def get_by_address(self, address: int) -> DccexTurnout | None:
        return self._by_address.get(address)

    async def _on_message(self, msg: DccexMessage) -> None:
        # <H id state> -- turnout state echo from CS.
        if msg.command != "H" or len(msg.args) < 2:
            return
        try:
            addr = int(msg.args[0])
            state_int = int(msg.args[1])
        except ValueError:
            return
        bean = self._by_address.get(addr)
        if bean is None:
            return
        # DCC-EX response encoding matches the command: 0 = CLOSED, 1 = THROWN.
        state = TurnoutState.THROWN if state_int else TurnoutState.CLOSED
        await bean._set_known_state(state)


# ----- Sensor --------------------------------------------------------------


class DccexSensor(Sensor):
    __slots__ = ("_address",)

    def __init__(self, system_name: str, address: int) -> None:
        super().__init__(system_name)
        self._address = address

    @property
    def address(self) -> int:
        return self._address


class DccexSensorManager(Manager[DccexSensor]):
    type_letter = "S"

    def __init__(self, system_prefix: str, *, tc: DccexTrafficController) -> None:
        super().__init__(system_prefix)
        self._by_address: dict[int, DccexSensor] = {}
        self._unsub = tc.inbound.connect(self._on_message)

    def _create(self, system_name: str) -> DccexSensor:
        address = _parse_address(system_name, self.full_prefix)
        bean = DccexSensor(system_name, address)
        self._by_address[address] = bean
        return bean

    def get_by_address(self, address: int) -> DccexSensor | None:
        return self._by_address.get(address)

    async def _on_message(self, msg: DccexMessage) -> None:
        # <Q id> active, <q id> inactive.
        if msg.command not in ("Q", "q") or not msg.args:
            return
        with contextlib.suppress(ValueError):
            addr = int(msg.args[0])
            bean = self._by_address.get(addr)
            if bean is not None:
                state = SensorState.ACTIVE if msg.command == "Q" else SensorState.INACTIVE
                await bean._set_known_state(state)


# ----- Power ---------------------------------------------------------------


class DccexPowerManager(PowerManager):
    """``<1>`` on, ``<0>`` off."""

    def __init__(self, tc: DccexTrafficController) -> None:
        super().__init__()
        self._tc = tc
        self._unsub = tc.inbound.connect(self._on_message)

    async def set_power(self, state: PowerState) -> None:
        cmd = "1" if state == PowerState.ON else "0"
        await self._tc.send(DccexMessage(command=cmd, args=()))
        await self._set_state(state)

    async def _on_message(self, msg: DccexMessage) -> None:
        # <p state> -- power state echo. state=1 means on, 0 off.
        if msg.command != "p" or not msg.args:
            return
        with contextlib.suppress(ValueError):
            wire = int(msg.args[0])
            await self._set_state(PowerState.ON if wire else PowerState.OFF)


# ----- helpers --------------------------------------------------------------


def _parse_address(system_name: str, full_prefix: str) -> int:
    rest = system_name.removeprefix(full_prefix)
    try:
        return int(rest)
    except ValueError as e:
        raise ValueError(
            f"DCC-EX system name {system_name!r} must end in a numeric address"
        ) from e
