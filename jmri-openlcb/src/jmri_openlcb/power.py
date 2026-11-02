"""Well-known-event power manager for OpenLCB.

OpenLCB reserves two event IDs for global track power (S-9.7.4.3 and
the JMRI Java ``OlcbPowerManager`` implementation):

* ``01.00.00.00.00.00.FF.FE`` -- POWER ON
* ``01.00.00.00.00.00.FF.FF`` -- POWER OFF

Any node that observes those events treats them as authoritative for
global power state. Any node that sets power emits the corresponding
PCER so every listener converges. This class:

* On :meth:`set_power`, sends the ON or OFF PCER via the shared
  :class:`~jmri_openlcb.traffic.OlcbTrafficController`.
* On observed PCER of either well-known event, flips its own
  in-memory state so anyone watching the connection's power
  manager sees the change without polling.

An UNKNOWN request is treated as a no-op -- there's no "please forget"
event on the wire.
"""
from __future__ import annotations

from jmri_core.beans.power import PowerManager
from jmri_core.state import PowerState

from jmri_openlcb.events import EventId, EventReport
from jmri_openlcb.traffic import OlcbTrafficController

POWER_ON = EventId.parse("01.00.00.00.00.00.FF.FE")
POWER_OFF = EventId.parse("01.00.00.00.00.00.FF.FF")


class OlcbPowerManager(PowerManager):
    """Power manager that produces + consumes the well-known events."""

    def __init__(self, tc: OlcbTrafficController) -> None:
        super().__init__()
        self._tc = tc
        self._unsubscribe = tc.events.connect(self._on_event)

    async def set_power(self, state: PowerState) -> None:
        # Emit the well-known event so every other node on the bus
        # observes the transition. Local state is updated by the same
        # inbound-event handler when our own broadcast comes back
        # around, keeping the "we just heard about it from the bus"
        # semantics uniform whether the change came from us or a peer.
        if state is PowerState.ON:
            await self._tc.produce_event(POWER_ON)
        elif state is PowerState.OFF:
            await self._tc.produce_event(POWER_OFF)
        else:
            # UNKNOWN is a "we don't know" state, not a command; no
            # matching event exists.
            return
        # Some loopback / test transports don't echo our own frames
        # back, so update the internal state explicitly. If the bus
        # DOES echo, the inbound handler will see it and no-op on
        # the already-current state (see ``_set_state`` in the base).
        await self._set_state(state)

    def close(self) -> None:
        """Detach the inbound listener. Idempotent."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def _on_event(self, report: EventReport) -> None:
        if report.event == POWER_ON:
            await self._set_state(PowerState.ON)
        elif report.event == POWER_OFF:
            await self._set_state(PowerState.OFF)
