"""``SignalMast`` -- a logical signal that exposes aspect *names* defined by
a signal system (e.g. ``"Clear"``, ``"Approach Medium"``).

Where :class:`SignalHead` uses a fixed enum of colours, a SignalMast's set of
valid aspects is open-ended and loaded from XML at runtime. The mast is
composed of one or more underlying signal heads; here we only carry the
logical aspect -- the head-driving logic lives in a Stage 1.5 module.
"""
from __future__ import annotations

from dataclasses import dataclass

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager


@dataclass(frozen=True, slots=True)
class SignalMastChange:
    previous: str
    current: str


class SignalMast(AbstractNamedBean):
    """Logical signal exposing named aspects from a signal system.

    Attributes:

    * ``aspect`` -- current displayed aspect name; empty string means unknown.
    * ``signal_system`` -- the name of the signal-system XML this mast follows
      (e.g. ``"basic"``, ``"AAR-1946"``). Resolved against
      ``xml/signals/<system>/`` at load time by persistence code.
    * ``held`` -- forces ``"Stop"`` (or the system's most-restrictive aspect).
    """

    __slots__ = ("_aspect", "_held", "signal_system")

    type_letter = "F"  # JMRI uses "F" for SignalMast system-type letter

    def __init__(
        self,
        system_name: str,
        signal_system: str = "",
        user_name: str | None = None,
    ) -> None:
        super().__init__(system_name, user_name)
        self.signal_system = signal_system
        self._aspect: str = ""
        self._held: bool = False

    @property
    def aspect(self) -> str:
        return self._aspect

    @property
    def held(self) -> bool:
        return self._held

    async def set_aspect(self, aspect: str) -> None:
        prev = self._aspect
        if prev == aspect:
            return
        self._aspect = aspect
        await self.state_changed.emit(SignalMastChange(prev, aspect))

    async def set_held(self, held: bool) -> None:
        # Held flips a flag; aspect display is computed elsewhere by the
        # signal-mast logic. We emit a state-changed so listeners know
        # something changed even if `aspect` didn't.
        if held == self._held:
            return
        self._held = held
        await self.state_changed.emit(SignalMastChange(self._aspect, self._aspect))


class SignalMastManager(Manager[SignalMast]):
    type_letter = "F"

    def _create(self, system_name: str) -> SignalMast:
        return SignalMast(system_name)
