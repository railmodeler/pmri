"""Unit tests for :mod:`pmri_openlcb.power`.

Two-node bus: node A owns an :class:`OlcbPowerManager`, node B
observes the bus. Setting power on A must produce a PCER of the
well-known event; B must see it. Vice-versa: B emits the event, A
must reflect the state change without polling.
"""
from __future__ import annotations

import asyncio

from pmri_core.state import PowerState

from pmri_openlcb import (
    EventReport,
    OlcbPowerManager,
    OlcbTrafficController,
    POWER_OFF,
    POWER_ON,
    loopback_pair,
)


async def _pair(alias_a: int = 0x111, alias_b: int = 0x222):
    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=alias_a)
    tc_b = OlcbTrafficController(b, source_alias=alias_b)
    await tc_a.start()
    await tc_b.start()
    return tc_a, tc_b


async def test_set_power_on_emits_pcer() -> None:
    """When the local power manager is switched ON, a PCER of the
    well-known ON event is on the wire. Any node observing the bus
    (here, tc_b) sees it via the events signal."""
    tc_a, tc_b = await _pair()
    pm = OlcbPowerManager(tc_a)
    seen: list[EventReport] = []
    tc_b.events.connect(lambda r: seen.append(r))
    try:
        await pm.set_power(PowerState.ON)
        await asyncio.sleep(0.02)
        assert any(r.event == POWER_ON for r in seen)
    finally:
        pm.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_set_power_off_emits_pcer() -> None:
    tc_a, tc_b = await _pair()
    pm = OlcbPowerManager(tc_a)
    seen: list[EventReport] = []
    tc_b.events.connect(lambda r: seen.append(r))
    try:
        await pm.set_power(PowerState.OFF)
        await asyncio.sleep(0.02)
        assert any(r.event == POWER_OFF for r in seen)
    finally:
        pm.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_inbound_power_on_event_updates_state() -> None:
    """Another node on the bus emits the ON event -> our local power
    manager should flip to ON without an explicit set_power call."""
    tc_a, tc_b = await _pair()
    pm = OlcbPowerManager(tc_a)
    try:
        assert pm.state is PowerState.UNKNOWN
        # tc_b (the "other node") emits the well-known ON event.
        await tc_b.produce_event(POWER_ON)
        await asyncio.sleep(0.02)
        assert pm.state is PowerState.ON
    finally:
        pm.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_inbound_power_off_event_updates_state() -> None:
    tc_a, tc_b = await _pair()
    pm = OlcbPowerManager(tc_a)
    try:
        await tc_b.produce_event(POWER_OFF)
        await asyncio.sleep(0.02)
        assert pm.state is PowerState.OFF
    finally:
        pm.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_state_changed_signal_fires_on_transition() -> None:
    """External subscribers (WebSocket push, etc.) need
    ``state_changed`` to fire on transitions, not just the internal
    state to update."""
    tc_a, tc_b = await _pair()
    pm = OlcbPowerManager(tc_a)
    changes = []
    pm.state_changed.connect(lambda c: changes.append(c))
    try:
        await pm.set_power(PowerState.ON)
        await asyncio.sleep(0.02)
        assert len(changes) == 1
        assert changes[0].current is PowerState.ON
    finally:
        pm.close()
        await tc_a.aclose()
        await tc_b.aclose()


async def test_close_detaches_listener() -> None:
    """After close(), inbound events shouldn't touch the manager
    state anymore. Otherwise a torn-down manager could still race
    the connection close and mutate stale state."""
    tc_a, tc_b = await _pair()
    pm = OlcbPowerManager(tc_a)
    pm.close()
    await tc_b.produce_event(POWER_ON)
    await asyncio.sleep(0.02)
    assert pm.state is PowerState.UNKNOWN
    await tc_a.aclose()
    await tc_b.aclose()
