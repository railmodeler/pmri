"""``Route`` -- a named group of turnouts thrown together to a target state.

Setting a route fires each step: ``turnout.set_commanded_state(target)`` in
order. A route may have a *lock* sensor that, when active, prevents firing.
"""
from __future__ import annotations

from dataclasses import dataclass

from pmri_core.bean import AbstractNamedBean
from pmri_core.beans.sensor import Sensor
from pmri_core.beans.turnout import Turnout
from pmri_core.manager import Manager
from pmri_core.state import RouteState, SensorState, TurnoutState


@dataclass(slots=True)
class RouteStep:
    """One turnout-to-target pair in a route."""

    turnout: Turnout
    target: TurnoutState


@dataclass(frozen=True, slots=True)
class RouteFired:
    """Payload emitted on a successful :meth:`Route.fire`."""

    steps_executed: int


class Route(AbstractNamedBean):
    """Named collection of turnout commands. Fires in registration order."""

    __slots__ = ("_lock_sensor", "_state", "steps")

    type_letter = "O"  # JMRI uses 'O' for Route (no conflict with Output)

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self.steps: list[RouteStep] = []
        self._lock_sensor: Sensor | None = None
        self._state: RouteState = RouteState.UNLOCKED

    @property
    def state(self) -> RouteState:
        return self._state

    @property
    def lock_sensor(self) -> Sensor | None:
        return self._lock_sensor

    def set_lock_sensor(self, sensor: Sensor | None) -> None:
        self._lock_sensor = sensor

    def add_step(self, turnout: Turnout, target: TurnoutState) -> None:
        self.steps.append(RouteStep(turnout, target))

    async def fire(self) -> int:
        """Throw every step in order. Returns count of commands sent.

        Raises :class:`RuntimeError` if the lock sensor is ACTIVE.
        """
        if self._lock_sensor and self._lock_sensor.known_state == SensorState.ACTIVE:
            raise RuntimeError(
                f"route {self.system_name} is locked by {self._lock_sensor.system_name}"
            )
        count = 0
        for step in self.steps:
            await step.turnout.set_commanded_state(step.target)
            count += 1
        await self.state_changed.emit(RouteFired(count))
        return count


class RouteManager(Manager[Route]):
    type_letter = "O"

    def _create(self, system_name: str) -> Route:
        return Route(system_name)
