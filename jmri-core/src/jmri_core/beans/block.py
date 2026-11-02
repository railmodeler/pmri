"""``Block`` -- a track section with optional occupancy detection.

A block carries:
  * a state (UNOCCUPIED/OCCUPIED/UNKNOWN/UNDETECTED);
  * an opaque value (typically a loco identifier, set when occupancy detection
    + reporters can identify the train);
  * an optional bound sensor; when bound, the block follows the sensor's
    active/inactive state automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jmri_core.bean import AbstractNamedBean
from jmri_core.beans.sensor import Sensor, SensorStateChange
from jmri_core.manager import Manager
from jmri_core.state import BlockState, SensorState


@dataclass(frozen=True, slots=True)
class BlockChange:
    kind: str  # "state" | "value"
    previous: Any
    current: Any


class Block(AbstractNamedBean):
    """Track block -- possibly with an attached occupancy sensor."""

    __slots__ = ("_disconnect_sensor", "_sensor", "_state", "_value")

    type_letter = "B"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._state: BlockState = BlockState.UNKNOWN
        self._value: Any = None
        self._sensor: Sensor | None = None
        self._disconnect_sensor: Any = None

    @property
    def state(self) -> BlockState:
        return self._state

    @property
    def value(self) -> Any:
        return self._value

    @property
    def sensor(self) -> Sensor | None:
        return self._sensor

    async def set_value(self, value: Any) -> None:
        prev = self._value
        if prev == value:
            return
        self._value = value
        await self.state_changed.emit(BlockChange("value", prev, value))

    async def _set_state(self, state: BlockState) -> None:
        prev = self._state
        if prev == state:
            return
        self._state = state
        await self.state_changed.emit(BlockChange("state", prev, state))

    def attach_sensor(self, sensor: Sensor | None) -> None:
        """Bind a sensor whose state drives this block's state.

        Pass ``None`` to detach. Detaching disconnects the listener so the
        sensor reference can be garbage-collected.
        """
        if self._disconnect_sensor is not None:
            self._disconnect_sensor()
            self._disconnect_sensor = None
        self._sensor = sensor
        if sensor is not None:
            self._disconnect_sensor = sensor.state_changed.connect(self._on_sensor_change)
            # Seed initial state from the sensor.
            self._state = _sensor_to_block_state(sensor.known_state)

    async def _on_sensor_change(self, change: SensorStateChange) -> None:
        await self._set_state(_sensor_to_block_state(change.current))


def _sensor_to_block_state(s: SensorState) -> BlockState:
    if s == SensorState.ACTIVE:
        return BlockState.OCCUPIED
    if s == SensorState.INACTIVE:
        return BlockState.UNOCCUPIED
    return BlockState.UNKNOWN


class BlockManager(Manager[Block]):
    type_letter = "B"

    def _create(self, system_name: str) -> Block:
        return Block(system_name)
