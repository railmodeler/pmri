"""``SignalHead`` -- a single signal-light unit with a fixed appearance set."""
from __future__ import annotations

from dataclasses import dataclass

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager
from jmri_core.state import SignalAppearance


@dataclass(frozen=True, slots=True)
class SignalHeadChange:
    previous: SignalAppearance
    current: SignalAppearance


class SignalHead(AbstractNamedBean):
    """A single signal head (one physical signal, one face).

    The ``held`` flag forces the head to RED regardless of its commanded
    appearance -- used by the dispatcher to override aspect logic.
    """

    __slots__ = ("_appearance", "_held")

    type_letter = "H"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._appearance: SignalAppearance = SignalAppearance.DARK
        self._held: bool = False

    @property
    def appearance(self) -> SignalAppearance:
        return SignalAppearance.RED if self._held else self._appearance

    @property
    def commanded_appearance(self) -> SignalAppearance:
        """The appearance ignoring ``held`` -- what was last set explicitly."""
        return self._appearance

    @property
    def held(self) -> bool:
        return self._held

    async def set_appearance(self, appearance: SignalAppearance) -> None:
        prev_effective = self.appearance
        self._appearance = appearance
        new_effective = self.appearance
        if prev_effective != new_effective:
            await self.state_changed.emit(SignalHeadChange(prev_effective, new_effective))

    async def set_held(self, held: bool) -> None:
        if held == self._held:
            return
        prev_effective = self.appearance
        self._held = held
        new_effective = self.appearance
        if prev_effective != new_effective:
            await self.state_changed.emit(SignalHeadChange(prev_effective, new_effective))


class SignalHeadManager(Manager[SignalHead]):
    type_letter = "H"

    def _create(self, system_name: str) -> SignalHead:
        return SignalHead(system_name)
