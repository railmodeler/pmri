"""``Memory`` -- generic named-value holder.

Memory beans are the universal mutable variable in JMRI: panels, Logix,
scripts, and operations all write arbitrary values to memories and listen
for changes. The value type is intentionally unconstrained.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager


@dataclass(frozen=True, slots=True)
class MemoryChange:
    previous: Any
    current: Any


class Memory(AbstractNamedBean):
    """A named slot holding any value. ``None`` means unset."""

    __slots__ = ("_value",)

    type_letter = "M"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._value: Any = None

    @property
    def value(self) -> Any:
        return self._value

    async def set_value(self, value: Any) -> None:
        prev = self._value
        if prev == value:
            return
        self._value = value
        await self.state_changed.emit(MemoryChange(prev, value))


class MemoryManager(Manager[Memory]):
    type_letter = "M"

    def _create(self, system_name: str) -> Memory:
        return Memory(system_name)
