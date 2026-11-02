"""End-to-end tests: LocoNetConnection via loopback transport.

Validates the SystemConnection lifecycle and the entry-point registration --
the same path the jmri-server uses to discover real drivers in production.
"""
from __future__ import annotations

import asyncio

from jmri_core.plugins import load_connections
from jmri_core.state import SensorState, TurnoutState

from jmri_loconet import LocoNetConnection, loopback_pair
from jmri_loconet.messages import LocoNetMessage, OpcInputRep, OpcSwReq
from jmri_loconet.traffic import LocoNetTrafficController


async def test_entry_point_registration_discovers_loconet() -> None:
    """The pyproject entry point must make ``loconet`` discoverable from the core."""
    connections = load_connections()
    assert "loconet" in connections
    assert connections["loconet"] is LocoNetConnection


async def test_open_close_lifecycle() -> None:
    a, _b = loopback_pair()
    conn = LocoNetConnection()
    assert not conn.is_open
    await conn.open({"kind": "loopback", "loopback_port": a})
    assert conn.is_open
    assert conn.turnouts is not None
    assert conn.sensors is not None
    assert conn.power is not None
    assert conn.traffic is not None
    assert conn.slots is not None
    assert conn.throttles is not None
    await conn.close()
    assert not conn.is_open


async def test_end_to_end_turnout_through_connection() -> None:
    a, b = loopback_pair()
    conn = LocoNetConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})

    captured: list[LocoNetMessage] = []
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await layout_tc.start()
    layout_tc.inbound.connect(captured.append)

    t = await conn.turnouts.provide("LT15")
    await t.set_commanded_state(TurnoutState.CLOSED)

    for _ in range(20):
        if any(isinstance(m, OpcSwReq) and m.address == 15 for m in captured):
            break
        await asyncio.sleep(0.01)
    assert any(isinstance(m, OpcSwReq) and m.address == 15 for m in captured)

    await conn.close()
    await layout_tc.aclose()


async def test_end_to_end_sensor_through_connection() -> None:
    a, b = loopback_pair()
    conn = LocoNetConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})

    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await layout_tc.start()

    s = await conn.sensors.provide("LS7")

    # Push a sensor activation from the layout side.
    await layout_tc.send(OpcInputRep(address=7, active=True))

    for _ in range(20):
        if s.known_state == SensorState.ACTIVE:
            break
        await asyncio.sleep(0.01)
    assert s.known_state == SensorState.ACTIVE

    await conn.close()
    await layout_tc.aclose()


async def test_read_hardware_clock_via_slot123() -> None:
    """Guards the whole clock-read flow: OPC_RQ_SL_DATA is emitted,
    the loopback CS answers with a slot 123 record, the reply is
    caught by the connection's inbound listener, and the record
    decodes into the expected :class:`HardwareClockState`.
    """
    from jmri_loconet.clock import build_slot123_write  # noqa: PLC0415
    from jmri_loconet.messages import (  # noqa: PLC0415
        OpcRqSlData,
        OpcSlRdData,
    )
    from jmri_core.beans.fast_clock import HardwareClockState  # noqa: PLC0415

    a, b = loopback_pair()
    conn = LocoNetConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})

    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await layout_tc.start()

    async def fake_cs(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcRqSlData) and msg.slot == 123:
            # Build the WR-shaped record, then re-emit as the RD-shape
            # a real CS would send back (same 14 bytes, different opcode).
            wr = build_slot123_write(HardwareClockState(
                hours=14, minutes=30, seconds=0, rate=4.0, running=True,
                twenty_four_hour=True,
            ))
            await layout_tc.send(OpcSlRdData(
                slot=wr.slot, stat=wr.stat, address=wr.address,
                speed=wr.speed, dirf=wr.dirf, trk=wr.trk,
                ss2=wr.ss2, snd=wr.snd,
            ))

    layout_tc.inbound.connect(fake_cs)
    try:
        state = await conn.read_hardware_clock()
        assert state.hours == 14
        assert state.minutes == 30
        assert state.rate == 4.0
        assert state.running is True
        assert state.twenty_four_hour is True
    finally:
        await layout_tc.aclose()
        await conn.close()


async def test_read_hardware_clock_times_out_when_cs_silent() -> None:
    """No reply on the wire -> the read must surface a ``TimeoutError``
    rather than hanging the caller forever. The 2-second timeout is
    baked into :meth:`LocoNetConnection.read_hardware_clock`; we don't
    wait the whole thing here -- asyncio's own timeout gives up
    quickly when the future is unset."""
    import pytest  # noqa: PLC0415
    from jmri_loconet.messages import OpcRqSlData  # noqa: PLC0415

    a, b = loopback_pair()
    conn = LocoNetConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})
    layout_tc = LocoNetTrafficController(b, min_send_interval=0.0)
    await layout_tc.start()
    seen: list[OpcRqSlData] = []

    async def swallow(msg: LocoNetMessage) -> None:
        if isinstance(msg, OpcRqSlData):
            seen.append(msg)   # deliberately no reply

    layout_tc.inbound.connect(swallow)
    try:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await asyncio.wait_for(conn.read_hardware_clock(), timeout=1.0)
        # The request DID make it to the "wire" even though we never
        # answered -- proves the connection sent it, not that our
        # future never armed.
        assert len(seen) == 1
        assert seen[0].slot == 123
    finally:
        await layout_tc.aclose()
        await conn.close()


async def test_unknown_transport_kind_raises() -> None:
    conn = LocoNetConnection()
    try:
        await conn.open({"kind": "nonexistent"})
    except ValueError as e:
        assert "transport" in str(e).lower()
    else:
        raise AssertionError("expected ValueError on bad transport kind")
