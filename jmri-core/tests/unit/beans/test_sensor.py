from __future__ import annotations

from jmri_core.beans.sensor import Sensor, SensorStateChange
from jmri_core.state import SensorState


async def test_sensor_initial_state_is_unknown() -> None:
    s = Sensor("LS1")
    assert s.known_state == SensorState.UNKNOWN
    assert not s.is_active


async def test_sensor_state_transition_emits_change() -> None:
    s = Sensor("LS1")
    events: list[SensorStateChange] = []
    s.state_changed.connect(events.append)
    await s._set_known_state(SensorState.ACTIVE)
    assert s.is_active
    assert events == [SensorStateChange(SensorState.UNKNOWN, SensorState.ACTIVE)]


async def test_sensor_idempotent_set_no_emit() -> None:
    s = Sensor("LS1")
    await s._set_known_state(SensorState.ACTIVE)
    events: list[SensorStateChange] = []
    s.state_changed.connect(events.append)
    await s._set_known_state(SensorState.ACTIVE)
    assert events == []
