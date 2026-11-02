"""Passive bean tests -- observed accessory traffic updates turnouts;
AIU traffic updates sensors; commands raise."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.state import PowerState, SensorState, TurnoutState

from jmri_ncemonitor.beans import (
    PassivePowerManager,
    PassiveSensorManager,
    PassiveTurnoutManager,
)
from jmri_ncemonitor.messages import (
    ADDR_ACCESSORY,
    ADDR_AIU_UPDATE,
    AccessoryObserved,
    AiuUpdate,
    encode,
)
from jmri_ncemonitor.monitor import CabBusMonitor
from jmri_ncemonitor.transport import loopback_pair


async def test_passive_turnout_set_commanded_state_raises() -> None:
    a, _ = loopback_pair()
    monitor = CabBusMonitor(a)
    mgr = PassiveTurnoutManager("M", monitor)
    t = await mgr.provide("MT12")
    with pytest.raises(RuntimeError, match="passive"):
        await t.set_commanded_state(TurnoutState.CLOSED)


async def test_observed_accessory_updates_passive_turnout() -> None:
    a, b = loopback_pair()
    monitor = CabBusMonitor(a)
    await monitor.start()
    mgr = PassiveTurnoutManager("M", monitor)
    t = await mgr.provide("MT12")

    await b.write(encode(AccessoryObserved(
        address=ADDR_ACCESSORY, accessory_address=12, closed=True
    )))
    # Trailing boundary marker.
    await b.write(bytes([0x80]))

    for _ in range(40):
        if t.known_state == TurnoutState.CLOSED:
            break
        await asyncio.sleep(0.005)
    assert t.known_state == TurnoutState.CLOSED

    await monitor.aclose()


async def test_observed_aiu_updates_passive_sensors() -> None:
    a, b = loopback_pair()
    monitor = CabBusMonitor(a)
    await monitor.start()
    mgr = PassiveSensorManager("M", monitor)
    # Sensor 1 == AIU 1, input 1 (bit 0).
    s1 = await mgr.provide("MS1")

    await b.write(encode(AiuUpdate(
        address=ADDR_AIU_UPDATE, aiu_module=1, input_word=0x0001
    )))
    await b.write(bytes([0x80]))

    for _ in range(40):
        if s1.known_state == SensorState.ACTIVE:
            break
        await asyncio.sleep(0.005)
    assert s1.known_state == SensorState.ACTIVE

    await monitor.aclose()


async def test_passive_power_manager_raises_on_set() -> None:
    pm = PassivePowerManager()
    with pytest.raises(RuntimeError, match="passive"):
        await pm.set_power(PowerState.ON)


async def test_turnout_manager_rejects_non_numeric_address() -> None:
    a, _ = loopback_pair()
    monitor = CabBusMonitor(a)
    mgr = PassiveTurnoutManager("M", monitor)
    with pytest.raises(ValueError, match="numeric"):
        await mgr.provide("MTabc")


# --- writable capability flag -----------------------------------------------
#
# ``writable=False`` is the UI capability signal passive drivers emit so
# clients can hide command controls -- it does NOT replace the
# authoritative refusal that lives in the overridden ``set_commanded_state``
# / ``set_power`` method. Both must stay aligned: a bean that raises on
# write without carrying ``writable=False`` would surface in the UI as a
# normal controllable row, inviting the exception the user just hit.


async def test_passive_turnout_declares_writable_false() -> None:
    a, _ = loopback_pair()
    monitor = CabBusMonitor(a)
    mgr = PassiveTurnoutManager("M", monitor)
    t = await mgr.provide("MT12")
    assert t.writable is False


async def test_passive_sensor_declares_writable_false() -> None:
    a, _ = loopback_pair()
    monitor = CabBusMonitor(a)
    mgr = PassiveSensorManager("M", monitor)
    s = await mgr.provide("MS1")
    assert s.writable is False


async def test_passive_power_manager_declares_writable_false() -> None:
    pm = PassivePowerManager()
    assert pm.writable is False
