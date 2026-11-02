"""Read-only NamedBean views for the passive Cab Bus monitor.

The monitor doesn't *control* the layout -- it only *observes* it. The
beans this module exposes follow that constraint:

* :class:`PassiveSensor` -- same shape as a normal Sensor but only the
  AIU poller (via :class:`~jmri_ncemonitor.monitor.CabBusMonitor`)
  updates its state.
* :class:`PassiveTurnout` -- set_commanded_state raises; the monitor sets
  *known* state from accessory traffic it observes on the bus.
* :class:`PassivePowerManager` -- set_power raises; power state is
  observed-only.

All three demonstrate that a fully-passive driver fits inside the
existing :class:`SystemConnection` ABC without requiring API changes --
clients are free to query state but commands fail loudly.
"""
from __future__ import annotations

from jmri_core.beans.power import PowerManager
from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.manager import Manager
from jmri_core.state import PowerState, SensorState, TurnoutState

from jmri_ncemonitor.messages import (
    AccessoryObserved,
    AiuUpdate,
    CabBusMessage,
)
from jmri_ncemonitor.monitor import CabBusMonitor

# --- helper ----------------------------------------------------------------


PASSIVE_REASON = "NCE Cab Bus monitor is passive -- commands are not supported"


# --- Sensor ----------------------------------------------------------------


class PassiveSensor(Sensor):
    """Read-only sensor -- its known state is observed off the bus."""

    __slots__ = ("_address",)

    writable = False

    def __init__(self, system_name: str, address: int) -> None:
        super().__init__(system_name)
        self._address = address

    @property
    def address(self) -> int:
        return self._address


class PassiveSensorManager(Manager[PassiveSensor]):
    """Sensor manager driven by observed :class:`AiuUpdate` events."""

    type_letter = "S"

    def __init__(self, system_prefix: str, monitor: CabBusMonitor) -> None:
        super().__init__(system_prefix)
        self._by_address: dict[int, PassiveSensor] = {}
        self._unsub = monitor.inbound.connect(self._on_message)

    def _create(self, system_name: str) -> PassiveSensor:
        address = _parse_address(system_name, self.full_prefix)
        bean = PassiveSensor(system_name, address)
        self._by_address[address] = bean
        return bean

    def get_by_address(self, address: int) -> PassiveSensor | None:
        return self._by_address.get(address)

    async def _on_message(self, msg: CabBusMessage) -> None:
        if not isinstance(msg, AiuUpdate):
            return
        for bit in range(14):
            sensor_addr = (msg.aiu_module - 1) * 14 + bit + 1
            sensor = self._by_address.get(sensor_addr)
            if sensor is None:
                continue
            state = (
                SensorState.ACTIVE
                if (msg.input_word & (1 << bit))
                else SensorState.INACTIVE
            )
            await sensor._set_known_state(state)


# --- Turnout ---------------------------------------------------------------


class PassiveTurnout(Turnout):
    """Read-only turnout -- commanded state cannot be set from this side."""

    __slots__ = ("_address",)

    writable = False

    def __init__(self, system_name: str, address: int) -> None:
        super().__init__(system_name)
        self._address = address

    @property
    def address(self) -> int:
        return self._address

    async def set_commanded_state(self, state: TurnoutState) -> None:
        raise RuntimeError(PASSIVE_REASON)


class PassiveTurnoutManager(Manager[PassiveTurnout]):
    """Turnout manager driven by observed :class:`AccessoryObserved` events."""

    type_letter = "T"

    def __init__(self, system_prefix: str, monitor: CabBusMonitor) -> None:
        super().__init__(system_prefix)
        self._by_address: dict[int, PassiveTurnout] = {}
        self._unsub = monitor.inbound.connect(self._on_message)

    def _create(self, system_name: str) -> PassiveTurnout:
        address = _parse_address(system_name, self.full_prefix)
        bean = PassiveTurnout(system_name, address)
        self._by_address[address] = bean
        return bean

    def get_by_address(self, address: int) -> PassiveTurnout | None:
        return self._by_address.get(address)

    async def _on_message(self, msg: CabBusMessage) -> None:
        if not isinstance(msg, AccessoryObserved):
            return
        bean = self._by_address.get(msg.accessory_address)
        if bean is None:
            return
        state = TurnoutState.CLOSED if msg.closed else TurnoutState.THROWN
        await bean._set_known_state(state)


# --- Power -----------------------------------------------------------------


class PassivePowerManager(PowerManager):
    """Power state is observed-only; ``set_power`` raises."""

    writable = False

    async def set_power(self, state: PowerState) -> None:
        raise RuntimeError(PASSIVE_REASON)


# --- helpers ---------------------------------------------------------------


def _parse_address(system_name: str, full_prefix: str) -> int:
    rest = system_name.removeprefix(full_prefix)
    try:
        return int(rest)
    except ValueError as e:
        raise ValueError(
            f"NCE Cab Bus monitor system name {system_name!r} "
            f"must end in a numeric address"
        ) from e
