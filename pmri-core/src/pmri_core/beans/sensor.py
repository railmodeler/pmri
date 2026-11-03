"""``Sensor`` -- a layout input (block detector, button, switch)."""
from __future__ import annotations

from dataclasses import dataclass

from pmri_core.bean import AbstractNamedBean
from pmri_core.manager import Manager
from pmri_core.state import SensorState


@dataclass(frozen=True, slots=True)
class SensorStateChange:
    previous: SensorState
    current: SensorState


class Sensor(AbstractNamedBean):
    """Generic layout input. Drivers call :meth:`_set_known_state` on receipt.

    Sensors are write-only from the layout's perspective -- the application
    doesn't *command* them, only observes. The ``inverted`` flag flips the
    semantic meaning of the raw hardware bit.
    """

    __slots__ = ("_known_state", "inverted")

    type_letter = "S"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._known_state: SensorState = SensorState.UNKNOWN
        self.inverted: bool = False

    @property
    def known_state(self) -> SensorState:
        return self._known_state

    async def _set_known_state(self, state: SensorState) -> None:
        prev = self._known_state
        if prev == state:
            return
        self._known_state = state
        await self.state_changed.emit(SensorStateChange(prev, state))

    @property
    def is_active(self) -> bool:
        return self._known_state == SensorState.ACTIVE


class SensorManager(Manager[Sensor]):
    type_letter = "S"

    def _create(self, system_name: str) -> Sensor:
        return Sensor(system_name)
