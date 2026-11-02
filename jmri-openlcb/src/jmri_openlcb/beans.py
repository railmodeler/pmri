"""Event-bound OpenLCB beans.

OpenLCB doesn't use addresses -- it uses 64-bit event IDs. Each bean
binds two event IDs (one per state) at construction:

* :class:`OlcbTurnout` -- ``closed_event`` and ``thrown_event``.
* :class:`OlcbSensor` -- ``active_event`` and ``inactive_event``.

When a turnout's commanded state changes, the bean *produces* the
matching event on the bus. When an inbound event matches a bean's
configured event ID, that bean's *known* state moves to the corresponding
value.

Because the event-ID mapping has to be provided at bean construction
time, the standard ``Manager.provide(system_name)`` shape doesn't fit:
there's no way to derive the events from a string alone. We expose a
``configure(system_name, closed_event, thrown_event)`` method on the
manager for that.

If the manager is constructed with a
:class:`~jmri_openlcb.event_ident.ProducerConsumerService`, every
configured bean's event IDs are automatically registered with it so
Identify Producers / Consumers / Events probes get the right
answers, and each bean's ``state_changed`` signal keeps the
advertised event state (VALID vs INVALID) in sync with its
``known_state``. Turnouts advertise their events as both produced
and consumed (bean fires PCER on ``set_commanded_state`` and the
manager consumes inbound PCER via ``_on_event``); sensors advertise
as consumed only (no produce path exists).
"""
from __future__ import annotations

from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.manager import Manager
from jmri_core.state import SensorState, TurnoutState

from jmri_openlcb.event_ident import EventState, ProducerConsumerService
from jmri_openlcb.events import EventId, EventReport
from jmri_openlcb.traffic import OlcbTrafficController

# ----- Turnout -------------------------------------------------------------


class OlcbTurnout(Turnout):
    """A turnout bound to two event IDs (closed/thrown)."""

    __slots__ = ("_closed_event", "_tc", "_thrown_event")

    def __init__(
        self,
        system_name: str,
        closed_event: EventId,
        thrown_event: EventId,
        tc: OlcbTrafficController,
    ) -> None:
        super().__init__(system_name)
        self._closed_event = closed_event
        self._thrown_event = thrown_event
        self._tc = tc

    @property
    def closed_event(self) -> EventId:
        return self._closed_event

    @property
    def thrown_event(self) -> EventId:
        return self._thrown_event

    async def set_commanded_state(self, state: TurnoutState) -> None:
        if state == TurnoutState.CLOSED:
            await self._tc.produce_event(self._closed_event)
        elif state == TurnoutState.THROWN:
            await self._tc.produce_event(self._thrown_event)
        await super().set_commanded_state(state)


class OlcbTurnoutManager(Manager[OlcbTurnout]):
    """Manager that creates turnouts only via :meth:`configure`."""

    type_letter = "T"

    def __init__(
        self,
        system_prefix: str,
        *,
        tc: OlcbTrafficController,
        pc_service: ProducerConsumerService | None = None,
    ) -> None:
        super().__init__(system_prefix)
        self._tc = tc
        self._pc = pc_service
        self._by_event: dict[EventId, tuple[OlcbTurnout, TurnoutState]] = {}
        self._unsub = tc.events.connect(self._on_event)

    def _create(self, system_name: str) -> OlcbTurnout:
        raise NotImplementedError(
            "OpenLCB turnouts require explicit event-ID configuration; "
            "use OlcbTurnoutManager.configure(...) instead of provide()"
        )

    async def configure(
        self,
        system_name: str,
        closed_event: EventId,
        thrown_event: EventId,
    ) -> OlcbTurnout:
        """Create and register a turnout with the given event bindings."""
        bean = OlcbTurnout(system_name, closed_event, thrown_event, self._tc)
        await self.register(bean)
        self._by_event[closed_event] = (bean, TurnoutState.CLOSED)
        self._by_event[thrown_event] = (bean, TurnoutState.THROWN)
        if self._pc is not None:
            # Turnouts are simultaneously producers (they fire PCER on
            # set_commanded_state) and consumers (the manager reflects
            # inbound PCER into known_state), so both events land in
            # both registries.
            self._pc.register_produced(closed_event)
            self._pc.register_produced(thrown_event)
            self._pc.register_consumed(closed_event)
            self._pc.register_consumed(thrown_event)
            bean.state_changed.connect(self._make_turnout_updater(bean))
        return bean

    def _make_turnout_updater(self, bean: OlcbTurnout):
        """Return a ``state_changed`` listener that keeps the two
        advertised event states opposite: whichever event names the
        current known_state is VALID, the other is INVALID. Ignores
        UNKNOWN and INCONSISTENT transitions -- the P/C protocol has
        no better representation for those than the default UNKNOWN,
        so we leave the last known valid/invalid pair in place until
        the state resolves."""
        pc = self._pc
        assert pc is not None

        async def _update(_change) -> None:  # noqa: ANN001
            state = bean.known_state
            if state == TurnoutState.CLOSED:
                closed, thrown = EventState.VALID, EventState.INVALID
            elif state == TurnoutState.THROWN:
                closed, thrown = EventState.INVALID, EventState.VALID
            else:
                return
            pc.set_produced_state(bean.closed_event, closed)
            pc.set_produced_state(bean.thrown_event, thrown)
            pc.set_consumer_state(bean.closed_event, closed)
            pc.set_consumer_state(bean.thrown_event, thrown)

        return _update

    async def _on_event(self, report: EventReport) -> None:
        binding = self._by_event.get(report.event)
        if binding is None:
            return
        bean, target_state = binding
        await bean._set_known_state(target_state)


# ----- Sensor --------------------------------------------------------------


class OlcbSensor(Sensor):
    """A sensor bound to two event IDs (active/inactive)."""

    __slots__ = ("_active_event", "_inactive_event")

    def __init__(
        self,
        system_name: str,
        active_event: EventId,
        inactive_event: EventId,
    ) -> None:
        super().__init__(system_name)
        self._active_event = active_event
        self._inactive_event = inactive_event

    @property
    def active_event(self) -> EventId:
        return self._active_event

    @property
    def inactive_event(self) -> EventId:
        return self._inactive_event


class OlcbSensorManager(Manager[OlcbSensor]):
    type_letter = "S"

    def __init__(
        self,
        system_prefix: str,
        *,
        tc: OlcbTrafficController,
        pc_service: ProducerConsumerService | None = None,
    ) -> None:
        super().__init__(system_prefix)
        self._pc = pc_service
        self._by_event: dict[EventId, tuple[OlcbSensor, SensorState]] = {}
        self._unsub = tc.events.connect(self._on_event)

    def _create(self, system_name: str) -> OlcbSensor:
        raise NotImplementedError(
            "OpenLCB sensors require explicit event-ID configuration; "
            "use OlcbSensorManager.configure(...)"
        )

    async def configure(
        self,
        system_name: str,
        active_event: EventId,
        inactive_event: EventId,
    ) -> OlcbSensor:
        bean = OlcbSensor(system_name, active_event, inactive_event)
        await self.register(bean)
        self._by_event[active_event] = (bean, SensorState.ACTIVE)
        self._by_event[inactive_event] = (bean, SensorState.INACTIVE)
        if self._pc is not None:
            # Sensors have no ``set_commanded_state`` -> they only
            # consume events, never produce. Register both event IDs
            # as consumed and mirror known_state onto their advertised
            # state so an Identify Consumers probe reports the current
            # reading.
            self._pc.register_consumed(active_event)
            self._pc.register_consumed(inactive_event)
            bean.state_changed.connect(self._make_sensor_updater(bean))
        return bean

    def _make_sensor_updater(self, bean: OlcbSensor):
        pc = self._pc
        assert pc is not None

        async def _update(_change) -> None:  # noqa: ANN001
            state = bean.known_state
            if state == SensorState.ACTIVE:
                active, inactive = EventState.VALID, EventState.INVALID
            elif state == SensorState.INACTIVE:
                active, inactive = EventState.INVALID, EventState.VALID
            else:
                return
            pc.set_consumer_state(bean.active_event, active)
            pc.set_consumer_state(bean.inactive_event, inactive)

        return _update

    async def _on_event(self, report: EventReport) -> None:
        binding = self._by_event.get(report.event)
        if binding is None:
            return
        bean, target_state = binding
        await bean._set_known_state(target_state)
