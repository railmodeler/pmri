"""``Throttle`` -- a runtime control handle for one locomotive (or consist).

A throttle is *not* a NamedBean -- it is acquired and released against a
ThrottleManager (in the HAL), and represents a transient hold on a slot in
the command station rather than a configured piece of layout equipment.

Attributes:

* ``address`` -- DCC address of the loco (or consist) under control.
* ``long_address`` -- whether the address is a 14-bit (long) value.
* ``speed`` -- normalized to ``[0.0, 1.0]``. ``-1.0`` means emergency stop.
* ``direction`` -- FORWARD or REVERSE.
* ``functions`` -- list of 32 booleans (F0..F31).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from jmri_core.events import Signal
from jmri_core.state import ThrottleDirection

NUM_FUNCTIONS = 32


@dataclass(frozen=True, slots=True)
class ThrottleChange:
    kind: str   # "speed" | "direction" | "function"
    index: int  # function index if kind=="function", else -1


@dataclass(slots=True)
class Throttle:
    """One handle on one locomotive."""

    address: int
    long_address: bool = False
    speed: float = 0.0
    direction: ThrottleDirection = ThrottleDirection.FORWARD
    functions: list[bool] = field(default_factory=lambda: [False] * NUM_FUNCTIONS)
    state_changed: Signal[ThrottleChange] = field(default_factory=Signal)

    def __post_init__(self) -> None:
        if len(self.functions) != NUM_FUNCTIONS:
            raise ValueError(f"functions must be length {NUM_FUNCTIONS}")
        if not (-1.0 <= self.speed <= 1.0):
            raise ValueError("speed must be in [-1, 1]")

    async def set_speed(self, speed: float) -> None:
        if not (-1.0 <= speed <= 1.0):
            raise ValueError("speed must be in [-1, 1]")
        if speed == self.speed:
            return
        self.speed = speed
        await self.state_changed.emit(ThrottleChange("speed", -1))

    async def set_direction(self, direction: ThrottleDirection) -> None:
        if direction == self.direction:
            return
        self.direction = direction
        await self.state_changed.emit(ThrottleChange("direction", -1))

    async def set_function(self, index: int, on: bool) -> None:
        if not 0 <= index < NUM_FUNCTIONS:
            raise ValueError(f"function index out of range: {index}")
        if self.functions[index] == on:
            return
        self.functions[index] = on
        await self.state_changed.emit(ThrottleChange("function", index))

    async def emergency_stop(self) -> None:
        await self.set_speed(-1.0)
