from __future__ import annotations

import asyncio

import pytest
from jmri_core.plugins import load_connections
from jmri_core.state import TurnoutState

from jmri_dccex import DccexConnection, loopback_pair
from jmri_dccex.codec import DccexMessage
from jmri_dccex.traffic import DccexTrafficController


async def test_entry_point_discovers_dccex() -> None:
    connections = load_connections()
    assert "dccex" in connections
    assert connections["dccex"] is DccexConnection


async def test_open_close_lifecycle() -> None:
    a, _ = loopback_pair()
    conn = DccexConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})
    assert conn.is_open
    assert conn.prefix == "D"
    assert conn.traffic is not None
    await conn.close()
    assert not conn.is_open


async def test_end_to_end_turnout() -> None:
    a, b = loopback_pair()
    conn = DccexConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})

    layout_tc = DccexTrafficController(b)
    await layout_tc.start()
    captured: list[DccexMessage] = []
    layout_tc.inbound.connect(captured.append)

    t = await conn.turnouts.provide("DT12")
    await t.set_commanded_state(TurnoutState.CLOSED)

    for _ in range(20):
        if any(m.command == "T" and m.args == ("12", "0") for m in captured):
            break
        await asyncio.sleep(0.005)
    assert any(m.command == "T" and m.args == ("12", "0") for m in captured)

    await conn.close()
    await layout_tc.aclose()


async def test_unknown_transport_raises() -> None:
    conn = DccexConnection()
    with pytest.raises(ValueError, match="transport"):
        await conn.open({"kind": "wifi"})


async def test_traffic_before_open_raises() -> None:
    conn = DccexConnection()
    with pytest.raises(RuntimeError, match="not open"):
        _ = conn.traffic
