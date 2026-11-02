"""``Light`` -- a controllable layout light (yard light, building, etc.).

A light may be plain on/off or dimmable. ``intensity`` is in the range
``[0.0, 1.0]``; when set to a non-extreme value, ``state`` becomes
``INTERMEDIATE``.
"""
from __future__ import annotations

from dataclasses import dataclass

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager
from jmri_core.state import LightState


@dataclass(frozen=True, slots=True)
class LightChange:
    previous_state: LightState
    current_state: LightState
    intensity: float


class Light(AbstractNamedBean):
    """Layout light. Supports binary or dimmable control."""

    __slots__ = ("_intensity", "_state", "supports_dimming")

    type_letter = "L"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._state: LightState = LightState.UNKNOWN
        self._intensity: float = 0.0
        self.supports_dimming: bool = False

    @property
    def state(self) -> LightState:
        return self._state

    @property
    def intensity(self) -> float:
        return self._intensity

    async def set_state(self, state: LightState) -> None:
        prev = self._state
        if state == LightState.ON:
            self._intensity = 1.0
        elif state == LightState.OFF:
            self._intensity = 0.0
        self._state = state
        if prev != state:
            await self.state_changed.emit(LightChange(prev, state, self._intensity))

    async def set_intensity(self, intensity: float) -> None:
        if not 0.0 <= intensity <= 1.0:
            raise ValueError(f"intensity must be in [0,1], got {intensity}")
        if not self.supports_dimming and intensity not in (0.0, 1.0):
            raise ValueError("this light does not support dimming")
        prev = self._state
        self._intensity = intensity
        if intensity == 0.0:
            self._state = LightState.OFF
        elif intensity == 1.0:
            self._state = LightState.ON
        else:
            self._state = LightState.INTERMEDIATE
        await self.state_changed.emit(LightChange(prev, self._state, intensity))


class LightManager(Manager[Light]):
    type_letter = "L"

    def _create(self, system_name: str) -> Light:
        return Light(system_name)
