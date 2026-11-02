from __future__ import annotations

import pytest

from jmri_core.beans.consist import Consist, ConsistLoco
from jmri_core.beans.throttle import NUM_FUNCTIONS, Throttle
from jmri_core.state import ThrottleDirection


async def test_throttle_default_construction() -> None:
    t = Throttle(address=3601)
    assert t.address == 3601
    assert t.speed == 0.0
    assert t.direction == ThrottleDirection.FORWARD
    assert len(t.functions) == NUM_FUNCTIONS
    assert not any(t.functions)


async def test_throttle_speed_validation() -> None:
    with pytest.raises(ValueError):
        Throttle(address=1, speed=2.0)
    t = Throttle(address=1)
    with pytest.raises(ValueError):
        await t.set_speed(2.0)


async def test_throttle_state_change_emissions() -> None:
    t = Throttle(address=1)
    events = []
    t.state_changed.connect(events.append)
    await t.set_speed(0.5)
    await t.set_direction(ThrottleDirection.REVERSE)
    await t.set_function(2, True)
    kinds = [e.kind for e in events]
    assert kinds == ["speed", "direction", "function"]
    assert events[2].index == 2


async def test_throttle_function_index_validation() -> None:
    t = Throttle(address=1)
    with pytest.raises(ValueError):
        await t.set_function(-1, True)
    with pytest.raises(ValueError):
        await t.set_function(NUM_FUNCTIONS, True)


async def test_throttle_emergency_stop() -> None:
    t = Throttle(address=1, speed=0.5)
    await t.emergency_stop()
    assert t.speed == -1.0


# --- Consist -------------------------------------------------------------


def test_consist_add_and_remove() -> None:
    c = Consist(address=42)
    c.add(ConsistLoco(address=3601, position=0))
    c.add(ConsistLoco(address=3602, position=1))
    assert len(c.locos) == 2
    assert c.remove(3601) is True
    assert len(c.locos) == 1
    assert c.remove(9999) is False  # not in consist


def test_consist_address_validation() -> None:
    with pytest.raises(ValueError):
        Consist(address=0)
    with pytest.raises(ValueError):
        Consist(address=20000)
