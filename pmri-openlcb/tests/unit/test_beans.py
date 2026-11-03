from __future__ import annotations

import asyncio

import pytest
from pmri_core.state import SensorState, TurnoutState

from pmri_openlcb.beans import OlcbSensorManager, OlcbTurnoutManager
from pmri_openlcb.codec import decode_frame, decode_pcer_if_pcer
from pmri_openlcb.events import EventId, EventReport
from pmri_openlcb.traffic import OlcbTrafficController
from pmri_openlcb.transport import loopback_pair


async def _pair() -> tuple[OlcbTrafficController, OlcbTrafficController]:
    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=0x111)
    tc_b = OlcbTrafficController(b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    return tc_a, tc_b


EV_CLOSED = EventId.parse("05.01.01.01.4C.43.00.01")
EV_THROWN = EventId.parse("05.01.01.01.4C.43.00.02")
EV_ACTIVE = EventId.parse("05.01.01.01.4C.43.10.01")
EV_INACTIVE = EventId.parse("05.01.01.01.4C.43.10.02")


async def test_turnout_set_commanded_produces_event() -> None:
    tc, layout = await _pair()
    pcer_events: list[EventReport] = []
    layout.events.connect(pcer_events.append)

    mgr = OlcbTurnoutManager("X", tc=tc)
    t = await mgr.configure("XT1", EV_CLOSED, EV_THROWN)

    await t.set_commanded_state(TurnoutState.CLOSED)
    for _ in range(20):
        if pcer_events:
            break
        await asyncio.sleep(0.005)
    assert pcer_events[0].event == EV_CLOSED
    assert pcer_events[0].source_alias == 0x111

    await tc.aclose()
    await layout.aclose()


async def test_turnout_known_state_from_inbound_pcer() -> None:
    tc, layout = await _pair()
    mgr = OlcbTurnoutManager("X", tc=tc)
    t = await mgr.configure("XT1", EV_CLOSED, EV_THROWN)

    # Layout produces the THROWN event.
    await layout.produce_event(EV_THROWN)
    for _ in range(20):
        if t.known_state == TurnoutState.THROWN:
            break
        await asyncio.sleep(0.005)
    assert t.known_state == TurnoutState.THROWN

    await tc.aclose()
    await layout.aclose()


async def test_sensor_state_from_inbound_pcer() -> None:
    tc, layout = await _pair()
    mgr = OlcbSensorManager("X", tc=tc)
    s = await mgr.configure("XS5", EV_ACTIVE, EV_INACTIVE)

    await layout.produce_event(EV_ACTIVE)
    for _ in range(20):
        if s.known_state == SensorState.ACTIVE:
            break
        await asyncio.sleep(0.005)
    assert s.known_state == SensorState.ACTIVE

    await layout.produce_event(EV_INACTIVE)
    for _ in range(20):
        if s.known_state == SensorState.INACTIVE:
            break
        await asyncio.sleep(0.005)
    assert s.known_state == SensorState.INACTIVE

    await tc.aclose()
    await layout.aclose()


async def test_unrelated_event_does_not_change_state() -> None:
    tc, layout = await _pair()
    mgr = OlcbTurnoutManager("X", tc=tc)
    t = await mgr.configure("XT1", EV_CLOSED, EV_THROWN)
    await layout.produce_event(EventId.parse("00.00.00.00.00.00.00.99"))
    await asyncio.sleep(0.02)
    assert t.known_state == TurnoutState.UNKNOWN
    await tc.aclose()
    await layout.aclose()


async def test_manager_provide_raises() -> None:
    """``provide()`` always raises -- event-binding is required."""
    a, _ = loopback_pair()
    tc = OlcbTrafficController(a)
    mgr = OlcbTurnoutManager("X", tc=tc)
    with pytest.raises(NotImplementedError, match="configure"):
        await mgr.provide("XT1")
    mgr2 = OlcbSensorManager("X", tc=tc)
    with pytest.raises(NotImplementedError, match="configure"):
        await mgr2.provide("XS1")


# Touch unused imports to keep coverage honest.
_ = (decode_frame, decode_pcer_if_pcer)
