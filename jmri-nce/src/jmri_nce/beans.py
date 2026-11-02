"""NCE-aware bean classes + managers + power manager."""
from __future__ import annotations

from jmri_core.beans.power import PowerManager
from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.manager import Manager
from jmri_core.state import PowerState, TurnoutState

from jmri_nce.messages import accessory_decoder_normal_ops, power_off, power_on
from jmri_nce.traffic import NceTrafficController

# ----- Turnout -------------------------------------------------------------


class NceTurnout(Turnout):
    """Sends an accessory-decoder normal-ops packet on commanded-state change.

    Known state arrives via :class:`~jmri_nce.monitors.NceTurnoutMonitor`
    polling, not in-line, so :meth:`set_commanded_state` does **not** mirror
    commanded into known. The monitor sets known state on the next poll
    cycle (default 1.5s).
    """

    __slots__ = ("_address", "_tc")

    def __init__(self, system_name: str, address: int, tc: NceTrafficController) -> None:
        super().__init__(system_name)
        self._address = address
        self._tc = tc

    @property
    def address(self) -> int:
        return self._address

    async def set_commanded_state(self, state: TurnoutState) -> None:
        if state in (TurnoutState.CLOSED, TurnoutState.THROWN):
            await self._tc.send_command(
                accessory_decoder_normal_ops(self._address, on=(state == TurnoutState.CLOSED))
            )
        prev = self._commanded_state
        if prev == state:
            return
        self._commanded_state = state
        # Mirror commanded -> known for immediate UI feedback. JMRI Java
        # does the same. The next monitor poll will correct this back if
        # the real CS state disagrees (e.g., motor stall, no decoder).
        if state in (TurnoutState.CLOSED, TurnoutState.THROWN):
            await self._set_known_state(state)


class NceTurnoutManager(Manager[NceTurnout]):
    """Constructs and indexes NCE turnouts by accessory address."""

    type_letter = "T"

    def __init__(self, system_prefix: str, *, tc: NceTrafficController) -> None:
        super().__init__(system_prefix)
        self._tc = tc
        self._by_address: dict[int, NceTurnout] = {}

    def _create(self, system_name: str) -> NceTurnout:
        address = _parse_address(system_name, self.full_prefix)
        bean = NceTurnout(system_name, address, self._tc)
        self._by_address[address] = bean
        return bean

    async def deregister(self, bean: NceTurnout) -> None:
        self._by_address.pop(bean.address, None)
        await super().deregister(bean)

    def get_by_address(self, address: int) -> NceTurnout | None:
        return self._by_address.get(address)


# ----- Sensor --------------------------------------------------------------


class NceSensor(Sensor):
    """Passive -- its state is set by the AIU poller.

    Sensor system names embed the AIU address + input number: ``NS<n>``
    where *n* = (aiu_addr-1) * 14 + input_bit + 1. The poller does the
    decoding; the bean is just storage.
    """

    __slots__ = ("_address",)

    def __init__(self, system_name: str, address: int) -> None:
        super().__init__(system_name)
        self._address = address

    @property
    def address(self) -> int:
        return self._address


class NceSensorManager(Manager[NceSensor]):
    type_letter = "S"

    def __init__(self, system_prefix: str) -> None:
        super().__init__(system_prefix)
        self._by_address: dict[int, NceSensor] = {}

    def _create(self, system_name: str) -> NceSensor:
        address = _parse_address(system_name, self.full_prefix)
        bean = NceSensor(system_name, address)
        self._by_address[address] = bean
        return bean

    def get_by_address(self, address: int) -> NceSensor | None:
        return self._by_address.get(address)


# ----- Power ---------------------------------------------------------------


class NcePowerManager(PowerManager):
    """Send OP_POWER_ON / OP_POWER_OFF; state is set on successful reply."""

    def __init__(self, tc: NceTrafficController) -> None:
        super().__init__()
        self._tc = tc

    async def set_power(self, state: PowerState) -> None:
        if state == PowerState.ON:
            await self._tc.send_command(power_on())
        elif state == PowerState.OFF:
            await self._tc.send_command(power_off())
        # NCE has no IDLE wire command; ignore other targets here.
        await self._set_state(state)


# ----- helpers --------------------------------------------------------------


def _parse_address(system_name: str, full_prefix: str) -> int:
    rest = system_name.removeprefix(full_prefix)
    try:
        return int(rest)
    except ValueError as e:
        raise ValueError(
            f"NCE system name {system_name!r} must end in a numeric address"
        ) from e
