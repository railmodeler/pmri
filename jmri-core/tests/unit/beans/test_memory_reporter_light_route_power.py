from __future__ import annotations

import pytest

from jmri_core.beans.light import Light
from jmri_core.beans.memory import Memory
from jmri_core.beans.power import PowerManager
from jmri_core.beans.reporter import Reporter
from jmri_core.beans.route import Route
from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.state import LightState, PowerState, SensorState, TurnoutState


# --- Memory ---------------------------------------------------------------


async def test_memory_holds_arbitrary_value() -> None:
    m = Memory("IM1")
    await m.set_value({"loco": "3601", "block": "B12"})
    assert m.value == {"loco": "3601", "block": "B12"}


# --- Reporter -------------------------------------------------------------


async def test_reporter_tracks_last_and_current() -> None:
    r = Reporter("LR1")
    await r.set_report("loco-3601")
    assert r.current_report == "loco-3601"
    assert r.last_report is None

    await r.set_report("loco-3602")
    assert r.current_report == "loco-3602"
    assert r.last_report == "loco-3601"


# --- Light ---------------------------------------------------------------


async def test_light_on_off_basic() -> None:
    light = Light("LL1")
    await light.set_state(LightState.ON)
    assert light.state == LightState.ON
    assert light.intensity == 1.0


async def test_light_intensity_rejects_dimming_when_unsupported() -> None:
    light = Light("LL1")
    with pytest.raises(ValueError, match="does not support dimming"):
        await light.set_intensity(0.5)


async def test_light_dimmable_sets_intermediate() -> None:
    light = Light("LL1")
    light.supports_dimming = True
    await light.set_intensity(0.5)
    assert light.state == LightState.INTERMEDIATE
    assert light.intensity == 0.5


async def test_light_intensity_out_of_range_rejected() -> None:
    light = Light("LL1")
    light.supports_dimming = True
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        await light.set_intensity(1.5)


# --- Route ---------------------------------------------------------------


async def test_route_fires_all_steps() -> None:
    t1 = Turnout("LT1")
    t2 = Turnout("LT2")
    r = Route("LO1")
    r.add_step(t1, TurnoutState.THROWN)
    r.add_step(t2, TurnoutState.CLOSED)
    n = await r.fire()
    assert n == 2
    assert t1.commanded_state == TurnoutState.THROWN
    assert t2.commanded_state == TurnoutState.CLOSED


async def test_route_lock_sensor_blocks_fire() -> None:
    r = Route("LO1")
    s = Sensor("LS-LOCK")
    await s._set_known_state(SensorState.ACTIVE)
    r.set_lock_sensor(s)
    with pytest.raises(RuntimeError, match="locked"):
        await r.fire()


# --- Power ---------------------------------------------------------------


async def test_power_state_transitions() -> None:
    p = PowerManager()
    events = []
    p.state_changed.connect(events.append)
    await p.set_power(PowerState.ON)
    assert p.state == PowerState.ON
    await p.set_power(PowerState.OFF)
    assert p.state == PowerState.OFF
    # Two transitions = two events; third call with same state = no event.
    await p.set_power(PowerState.OFF)
    assert len(events) == 2
