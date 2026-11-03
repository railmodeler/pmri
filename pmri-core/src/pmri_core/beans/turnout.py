"""``Turnout`` -- a layout switch (point) with CLOSED/THROWN states."""
from __future__ import annotations

from dataclasses import dataclass

from pmri_core.bean import AbstractNamedBean
from pmri_core.manager import Manager
from pmri_core.state import TurnoutFeedback, TurnoutState


@dataclass(frozen=True, slots=True)
class TurnoutStateChange:
    """Payload emitted on :attr:`Turnout.state_changed`."""

    kind: str          # "commanded" or "known"
    previous: TurnoutState
    current: TurnoutState


class Turnout(AbstractNamedBean):
    """Layout turnout (point switch).

    A turnout has two state values:

    * ``commanded_state`` -- what we last *asked* the layout to do.
    * ``known_state``     -- what the layout reports back.

    Drivers update ``known_state`` when their feedback channel reports a
    change; this may differ from ``commanded_state`` until the move completes
    or if motorised feedback says the switch ended up in an INCONSISTENT pos.
    """

    __slots__ = (
        "_commanded_state",
        "_known_state",
        "feedback_mode",
        "inverted",
        "locked",
    )

    type_letter = "T"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._commanded_state: TurnoutState = TurnoutState.UNKNOWN
        self._known_state: TurnoutState = TurnoutState.UNKNOWN
        self.feedback_mode: TurnoutFeedback = TurnoutFeedback.DIRECT
        self.inverted: bool = False
        self.locked: bool = False

    @property
    def commanded_state(self) -> TurnoutState:
        return self._commanded_state

    @property
    def known_state(self) -> TurnoutState:
        return self._known_state

    async def set_commanded_state(self, state: TurnoutState) -> None:
        """Ask the layout to throw or close the switch.

        The base implementation only updates the in-memory commanded state
        and emits the signal. Driver subclasses override this to push a
        hardware message *and then* call :meth:`super().set_commanded_state`.
        """
        prev = self._commanded_state
        if prev == state:
            return
        self._commanded_state = state
        await self.state_changed.emit(TurnoutStateChange("commanded", prev, state))
        # For DIRECT feedback, known state mirrors commanded immediately.
        if self.feedback_mode == TurnoutFeedback.DIRECT:
            await self._set_known_state(state)

    async def _set_known_state(self, state: TurnoutState) -> None:
        """Driver hook: layout reported this state."""
        prev = self._known_state
        if prev == state:
            return
        self._known_state = state
        await self.state_changed.emit(TurnoutStateChange("known", prev, state))


class TurnoutManager(Manager[Turnout]):
    type_letter = "T"

    def _create(self, system_name: str) -> Turnout:
        return Turnout(system_name)
