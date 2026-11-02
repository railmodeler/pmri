"""LocoNet-aware bean subclasses + their managers + LnPowerManager.

* :class:`LnTurnout` overrides ``set_commanded_state`` to push
  ``OPC_SW_REQ`` and updates ``known_state`` from inbound ``OPC_SW_REP``.
* :class:`LnSensor` updates its state from inbound ``OPC_INPUT_REP``.
* :class:`LnPowerManager` sends ``OPC_GPON`` / ``OPC_GPOFF`` / ``OPC_IDLE``
  and tracks state from inbound copies.
"""
from __future__ import annotations

from jmri_core.beans.power import PowerManager
from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.manager import Manager
from jmri_core.state import PowerState, SensorState, TurnoutState

from jmri_loconet.messages import (
    LocoNetMessage,
    OpcGpOff,
    OpcGpOn,
    OpcIdle,
    OpcInputRep,
    OpcSwRep,
    OpcSwReq,
)
from jmri_loconet.traffic import LocoNetTrafficController


class LnTurnout(Turnout):
    """LocoNet-aware turnout: sends OPC_SW_REQ on commanded-state change."""

    __slots__ = ("_address", "_tc")

    def __init__(self, system_name: str, address: int, tc: LocoNetTrafficController) -> None:
        super().__init__(system_name)
        self._address = address
        self._tc = tc

    @property
    def address(self) -> int:
        return self._address

    async def set_commanded_state(self, state: TurnoutState) -> None:
        if state in (TurnoutState.CLOSED, TurnoutState.THROWN):
            await self._tc.send(OpcSwReq(
                address=self._address,
                closed=(state == TurnoutState.CLOSED),
                output_on=True,
            ))
        # Update the in-memory state via the base implementation; for
        # DIRECT-feedback turnouts known_state will mirror this until/unless
        # a layout report overrides it.
        await super().set_commanded_state(state)


class LnTurnoutManager(Manager[LnTurnout]):
    """Manager that constructs LocoNet turnouts and tracks them by address."""

    type_letter = "T"

    def __init__(self, system_prefix: str, *, tc: LocoNetTrafficController) -> None:
        super().__init__(system_prefix)
        self._tc = tc
        self._by_address: dict[int, LnTurnout] = {}
        self._unsub = tc.inbound.connect(self._on_message)

    def _create(self, system_name: str) -> LnTurnout:
        address = _parse_address(system_name, self.full_prefix)
        bean = LnTurnout(system_name, address, self._tc)
        self._by_address[address] = bean
        return bean

    async def deregister(self, bean: LnTurnout) -> None:
        self._by_address.pop(bean.address, None)
        await super().deregister(bean)

    async def _on_message(self, msg: LocoNetMessage) -> None:
        if not isinstance(msg, OpcSwRep):
            return
        bean = self._by_address.get(msg.address)
        if bean is None:
            return
        state = TurnoutState.CLOSED if msg.closed else TurnoutState.THROWN
        await bean._set_known_state(state)


class LnSensor(Sensor):
    """LocoNet-aware sensor -- read-only from the layout."""

    __slots__ = ("_address",)

    def __init__(self, system_name: str, address: int) -> None:
        super().__init__(system_name)
        self._address = address

    @property
    def address(self) -> int:
        return self._address


class LnSensorManager(Manager[LnSensor]):
    """Manager that constructs LocoNet sensors and listens for OPC_INPUT_REP."""

    type_letter = "S"

    def __init__(self, system_prefix: str, *, tc: LocoNetTrafficController) -> None:
        super().__init__(system_prefix)
        self._tc = tc
        self._by_address: dict[int, LnSensor] = {}
        self._unsub = tc.inbound.connect(self._on_message)

    def _create(self, system_name: str) -> LnSensor:
        address = _parse_address(system_name, self.full_prefix)
        bean = LnSensor(system_name, address)
        self._by_address[address] = bean
        return bean

    async def _on_message(self, msg: LocoNetMessage) -> None:
        if not isinstance(msg, OpcInputRep):
            return
        bean = self._by_address.get(msg.address)
        if bean is None:
            # Sensors aren't always pre-created; clients can opt in via provide().
            return
        state = SensorState.ACTIVE if msg.active else SensorState.INACTIVE
        await bean._set_known_state(state)


class LnPowerManager(PowerManager):
    """LocoNet track power. Sends OPC_GPON/OPC_GPOFF/OPC_IDLE."""

    def __init__(self, tc: LocoNetTrafficController) -> None:
        super().__init__()
        self._tc = tc
        self._unsub = tc.inbound.connect(self._on_message)

    async def set_power(self, state: PowerState) -> None:
        if state == PowerState.ON:
            await self._tc.send(OpcGpOn())
        elif state == PowerState.OFF:
            await self._tc.send(OpcGpOff())
        elif state == PowerState.IDLE:
            await self._tc.send(OpcIdle())
        await self._set_state(state)

    async def _on_message(self, msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcGpOn):
            await self._set_state(PowerState.ON)
        elif isinstance(msg, OpcGpOff):
            await self._set_state(PowerState.OFF)
        elif isinstance(msg, OpcIdle):
            await self._set_state(PowerState.IDLE)


# ----- helpers --------------------------------------------------------------


def _parse_address(system_name: str, full_prefix: str) -> int:
    """Extract the numeric address from a LocoNet system name (``"LT12"`` -> 12)."""
    rest = system_name.removeprefix(full_prefix)
    try:
        return int(rest)
    except ValueError as e:
        raise ValueError(
            f"LocoNet system name {system_name!r} must end in a numeric address"
        ) from e
