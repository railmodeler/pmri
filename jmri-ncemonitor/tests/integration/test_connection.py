"""End-to-end: NceMonitorConnection + entry point + observed traffic."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.plugins import load_connections
from jmri_core.state import TurnoutState

from jmri_ncemonitor import NceMonitorConnection, loopback_pair
from jmri_ncemonitor.messages import ADDR_ACCESSORY, AccessoryObserved, encode


async def test_entry_point_discovers_ncemonitor() -> None:
    connections = load_connections()
    assert "ncemonitor" in connections
    assert connections["ncemonitor"] is NceMonitorConnection


async def test_open_close_lifecycle() -> None:
    a, _ = loopback_pair()
    conn = NceMonitorConnection()
    assert not conn.is_open
    await conn.open({"kind": "loopback", "loopback_port": a})
    assert conn.is_open
    assert conn.prefix == "M"
    assert conn.monitor is not None
    await conn.close()
    assert not conn.is_open


async def test_end_to_end_observation_via_connection() -> None:
    a, b = loopback_pair()
    conn = NceMonitorConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})

    t = await conn.turnouts.provide("MT42")
    await b.write(encode(AccessoryObserved(
        address=ADDR_ACCESSORY, accessory_address=42, closed=False
    )))
    await b.write(bytes([0x80]))   # boundary

    for _ in range(40):
        if t.known_state == TurnoutState.THROWN:
            break
        await asyncio.sleep(0.005)
    assert t.known_state == TurnoutState.THROWN

    await conn.close()


async def test_unknown_transport_kind_raises() -> None:
    conn = NceMonitorConnection()
    with pytest.raises(ValueError, match="transport"):
        await conn.open({"kind": "wifi"})


async def test_monitor_accessor_before_open_raises() -> None:
    conn = NceMonitorConnection()
    with pytest.raises(RuntimeError, match="not open"):
        _ = conn.monitor
