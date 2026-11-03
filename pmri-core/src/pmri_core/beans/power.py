"""Track power control.

Each :class:`~pmri_core.hal.connection.SystemConnection` exposes a single
:class:`PowerManager` instance -- there is no per-bean Power object because
the layout track has exactly one power state per connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pmri_core.events import Signal
from pmri_core.state import PowerState


@dataclass(frozen=True, slots=True)
class PowerChange:
    previous: PowerState
    current: PowerState


class PowerManager:
    """Track power state for one system connection.

    Not a NamedBean -- it has no system name and there is exactly one per
    connection. Drivers override :meth:`set_power` to send the hardware
    command and call :meth:`_set_state` when the layout reports a change.
    """

    writable: ClassVar[bool] = True
    """Whether the client can command track power. Observe-only drivers
    (``pmri-ncemonitor``) override to False so the UI can present a
    read-only affordance; the authoritative refusal still lives in the
    overridden ``set_power``."""

    def __init__(self) -> None:
        self._state: PowerState = PowerState.UNKNOWN
        self.state_changed: Signal[PowerChange] = Signal()

    @property
    def state(self) -> PowerState:
        return self._state

    async def set_power(self, state: PowerState) -> None:
        """Command the layout to *state*. Base impl just updates in-memory."""
        await self._set_state(state)

    async def _set_state(self, state: PowerState) -> None:
        prev = self._state
        if prev == state:
            return
        self._state = state
        await self.state_changed.emit(PowerChange(prev, state))
