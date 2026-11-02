"""Turnout/sensor bean auto-registration with ProducerConsumerService.

When a manager is constructed with a
:class:`~jmri_openlcb.event_ident.ProducerConsumerService`, every
``configure`` call auto-registers the bean's event IDs and hooks up
a ``state_changed`` listener so the advertised state (VALID vs
INVALID) tracks the bean's ``known_state``.
"""
from __future__ import annotations

from jmri_core.state import SensorState, TurnoutState

from jmri_openlcb.beans import OlcbSensorManager, OlcbTurnoutManager
from jmri_openlcb.event_ident import EventState, ProducerConsumerService
from jmri_openlcb.events import EventId
from jmri_openlcb.traffic import OlcbTrafficController
from jmri_openlcb.transport import loopback_pair


EV_CLOSED = EventId.parse("05.01.01.01.4C.43.00.01")
EV_THROWN = EventId.parse("05.01.01.01.4C.43.00.02")
EV_ACTIVE = EventId.parse("05.01.01.01.4C.43.10.01")
EV_INACTIVE = EventId.parse("05.01.01.01.4C.43.10.02")


async def _tc() -> OlcbTrafficController:
    a, _ = loopback_pair()
    tc = OlcbTrafficController(a, source_alias=0x111)
    await tc.start()
    return tc


# ---- turnout wiring ------------------------------------------------------


async def test_configure_registers_turnout_events_both_ways() -> None:
    """A configured turnout should show up as both producer and
    consumer of both its events. The bean produces via
    ``set_commanded_state`` and the manager consumes via
    ``_on_event`` -- the P/C service needs to advertise both roles."""
    tc = await _tc()
    pc = ProducerConsumerService(tc)
    mgr = OlcbTurnoutManager("X", tc=tc, pc_service=pc)
    try:
        await mgr.configure("XT1", EV_CLOSED, EV_THROWN)
        assert EV_CLOSED in pc.produced
        assert EV_THROWN in pc.produced
        assert EV_CLOSED in pc.consumed
        assert EV_THROWN in pc.consumed
        # Initial state -- UNKNOWN, per the P/C default.
        assert pc.produced[EV_CLOSED] == EventState.UNKNOWN
        assert pc.produced[EV_THROWN] == EventState.UNKNOWN
    finally:
        pc.close()
        await tc.aclose()


async def test_turnout_known_state_flips_advertised_state() -> None:
    """When the turnout goes CLOSED, closed_event should be VALID and
    thrown_event INVALID -- both on the producer and consumer sides."""
    tc = await _tc()
    pc = ProducerConsumerService(tc)
    mgr = OlcbTurnoutManager("X", tc=tc, pc_service=pc)
    try:
        t = await mgr.configure("XT1", EV_CLOSED, EV_THROWN)
        await t.set_commanded_state(TurnoutState.CLOSED)
        # DIRECT feedback in the base Turnout makes known_state
        # follow commanded_state immediately.
        assert pc.produced[EV_CLOSED] == EventState.VALID
        assert pc.produced[EV_THROWN] == EventState.INVALID
        assert pc.consumed[EV_CLOSED] == EventState.VALID
        assert pc.consumed[EV_THROWN] == EventState.INVALID
        # Flipping to THROWN inverts the pair.
        await t.set_commanded_state(TurnoutState.THROWN)
        assert pc.produced[EV_CLOSED] == EventState.INVALID
        assert pc.produced[EV_THROWN] == EventState.VALID
    finally:
        pc.close()
        await tc.aclose()


async def test_turnout_manager_without_pc_service_still_works() -> None:
    """``pc_service`` is optional -- the existing bean/managing behaviour
    must not regress when it's omitted."""
    tc = await _tc()
    mgr = OlcbTurnoutManager("X", tc=tc)  # no pc_service
    try:
        t = await mgr.configure("XT1", EV_CLOSED, EV_THROWN)
        # Basic set_commanded_state still runs without exploding.
        await t.set_commanded_state(TurnoutState.CLOSED)
        assert t.known_state == TurnoutState.CLOSED
    finally:
        await tc.aclose()


# ---- sensor wiring -------------------------------------------------------


async def test_configure_registers_sensor_events_as_consumed_only() -> None:
    """Sensors have no ``set_commanded_state`` -> they never produce
    events, so both events register as consumed only. If they showed
    up as produced, an Identify Producers probe for the event would
    misidentify us as a producer."""
    tc = await _tc()
    pc = ProducerConsumerService(tc)
    mgr = OlcbSensorManager("X", tc=tc, pc_service=pc)
    try:
        await mgr.configure("XS1", EV_ACTIVE, EV_INACTIVE)
        assert EV_ACTIVE in pc.consumed
        assert EV_INACTIVE in pc.consumed
        assert EV_ACTIVE not in pc.produced
        assert EV_INACTIVE not in pc.produced
    finally:
        pc.close()
        await tc.aclose()


async def test_sensor_known_state_flips_advertised_consumer_state() -> None:
    tc = await _tc()
    pc = ProducerConsumerService(tc)
    mgr = OlcbSensorManager("X", tc=tc, pc_service=pc)
    try:
        s = await mgr.configure("XS1", EV_ACTIVE, EV_INACTIVE)
        # No public API on Sensor to set known_state; drivers use the
        # protected hook. Direct call is legit here -- that's how the
        # manager itself updates state on inbound PCER.
        await s._set_known_state(SensorState.ACTIVE)
        assert pc.consumed[EV_ACTIVE] == EventState.VALID
        assert pc.consumed[EV_INACTIVE] == EventState.INVALID
        await s._set_known_state(SensorState.INACTIVE)
        assert pc.consumed[EV_ACTIVE] == EventState.INVALID
        assert pc.consumed[EV_INACTIVE] == EventState.VALID
    finally:
        pc.close()
        await tc.aclose()


async def test_sensor_manager_without_pc_service_still_works() -> None:
    tc = await _tc()
    mgr = OlcbSensorManager("X", tc=tc)  # no pc_service
    try:
        s = await mgr.configure("XS1", EV_ACTIVE, EV_INACTIVE)
        await s._set_known_state(SensorState.ACTIVE)
        assert s.known_state == SensorState.ACTIVE
    finally:
        await tc.aclose()


# ---- end-to-end via OlcbConnection --------------------------------------


async def test_connection_wired_managers_advertise_events() -> None:
    """OlcbConnection's default wiring should hand the pc_service to
    both bean managers so a bean created via ``conn.turnouts.configure``
    ends up advertised."""
    from jmri_openlcb.connection import OlcbConnection  # noqa: PLC0415
    a, _ = loopback_pair()
    conn = OlcbConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "skip_alias_claim": True, "source_alias": 0x123,
    })
    try:
        await conn.turnouts.configure("XT1", EV_CLOSED, EV_THROWN)  # type: ignore[attr-defined]
        await conn.sensors.configure("XS1", EV_ACTIVE, EV_INACTIVE)  # type: ignore[attr-defined]
        pc = conn.pc_service
        # Turnout: produced + consumed (both roles). Sensor: consumed only.
        assert set(pc.produced) == {EV_CLOSED, EV_THROWN}
        assert set(pc.consumed) == {EV_CLOSED, EV_THROWN, EV_ACTIVE, EV_INACTIVE}
    finally:
        await conn.close()
