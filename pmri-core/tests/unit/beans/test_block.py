from __future__ import annotations

from pmri_core.beans.block import Block, BlockChange
from pmri_core.beans.sensor import Sensor
from pmri_core.state import BlockState, SensorState


async def test_block_set_value_emits_change() -> None:
    b = Block("LB1")
    events: list[BlockChange] = []
    b.state_changed.connect(events.append)
    await b.set_value("Loco-3601")
    assert b.value == "Loco-3601"
    assert events == [BlockChange("value", None, "Loco-3601")]


async def test_attached_sensor_drives_block_state() -> None:
    b = Block("LB1")
    s = Sensor("LS1")
    b.attach_sensor(s)

    # Sensor going ACTIVE should make block OCCUPIED.
    await s._set_known_state(SensorState.ACTIVE)
    assert b.state == BlockState.OCCUPIED

    # And INACTIVE -> UNOCCUPIED.
    await s._set_known_state(SensorState.INACTIVE)
    assert b.state == BlockState.UNOCCUPIED


async def test_detaching_sensor_breaks_link() -> None:
    b = Block("LB1")
    s = Sensor("LS1")
    b.attach_sensor(s)
    await s._set_known_state(SensorState.ACTIVE)
    b.attach_sensor(None)

    # Now flipping the sensor should not affect the block.
    await s._set_known_state(SensorState.INACTIVE)
    assert b.state == BlockState.OCCUPIED  # unchanged
