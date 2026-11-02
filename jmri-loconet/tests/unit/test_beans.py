"""Tests for LnTurnout / LnSensor / LnPowerManager wired through a real TC."""
from __future__ import annotations

import asyncio

from jmri_core.state import PowerState, SensorState, TurnoutState

from jmri_loconet.beans import (
    LnPowerManager,
    LnSensorManager,
    LnTurnoutManager,
)
from jmri_loconet.messages import (
    LocoNetMessage,
    OpcGpOff,
    OpcGpOn,
    OpcInputRep,
    OpcSwRep,
    OpcSwReq,
)
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import loopback_pair


async def _pair() -> tuple[LocoNetTrafficController, LocoNetTrafficController]:
    a, b = loopback_pair()
    tc_a = LocoNetTrafficController(a, min_send_interval=0.0)
    tc_b = LocoNetTrafficController(b, min_send_interval=0.0)
    await tc_a.start()
    await tc_b.start()
    return tc_a, tc_b


async def test_turnout_set_commanded_state_sends_opc_sw_req() -> None:
    tc, layout = await _pair()
    captured: list[LocoNetMessage] = []
    layout.inbound.connect(captured.append)
    mgr = LnTurnoutManager("L", tc=tc)

    t = await mgr.provide("LT12")
    await t.set_commanded_state(TurnoutState.THROWN)

    for _ in range(10):
        if any(isinstance(m, OpcSwReq) for m in captured):
            break
        await asyncio.sleep(0.01)

    sw_reqs = [m for m in captured if isinstance(m, OpcSwReq)]
    assert sw_reqs, "expected at least one OPC_SW_REQ on the wire"
    assert sw_reqs[0].address == 12
    assert sw_reqs[0].closed is False
    assert sw_reqs[0].output_on is True

    await tc.aclose()
    await layout.aclose()


async def test_turnout_known_state_updates_on_opc_sw_rep() -> None:
    tc, layout = await _pair()
    mgr = LnTurnoutManager("L", tc=tc)
    t = await mgr.provide("LT12")

    # Layout reports the turnout as THROWN.
    await layout.send(OpcSwRep(address=12, closed=False, thrown=True))

    for _ in range(20):
        if t.known_state == TurnoutState.THROWN:
            break
        await asyncio.sleep(0.01)
    assert t.known_state == TurnoutState.THROWN

    await tc.aclose()
    await layout.aclose()


async def test_sensor_updates_on_opc_input_rep() -> None:
    tc, layout = await _pair()
    mgr = LnSensorManager("L", tc=tc)
    s = await mgr.provide("LS5")

    await layout.send(OpcInputRep(address=5, active=True))
    for _ in range(20):
        if s.is_active:
            break
        await asyncio.sleep(0.01)
    assert s.known_state == SensorState.ACTIVE

    await layout.send(OpcInputRep(address=5, active=False))
    for _ in range(20):
        if s.known_state == SensorState.INACTIVE:
            break
        await asyncio.sleep(0.01)
    assert s.known_state == SensorState.INACTIVE

    await tc.aclose()
    await layout.aclose()


async def test_power_manager_sends_gpon_and_gpoff() -> None:
    tc, layout = await _pair()
    captured: list[LocoNetMessage] = []
    layout.inbound.connect(captured.append)
    pm = LnPowerManager(tc)

    await pm.set_power(PowerState.ON)
    await pm.set_power(PowerState.OFF)

    for _ in range(20):
        if any(isinstance(m, OpcGpOff) for m in captured):
            break
        await asyncio.sleep(0.01)
    kinds = [type(m).__name__ for m in captured]
    assert "OpcGpOn" in kinds
    assert "OpcGpOff" in kinds
    assert pm.state == PowerState.OFF

    await tc.aclose()
    await layout.aclose()


async def test_power_manager_state_follows_inbound_gpon() -> None:
    tc, layout = await _pair()
    pm = LnPowerManager(tc)

    await layout.send(OpcGpOn())
    for _ in range(20):
        if pm.state == PowerState.ON:
            break
        await asyncio.sleep(0.01)
    assert pm.state == PowerState.ON

    await tc.aclose()
    await layout.aclose()


async def test_turnout_manager_rejects_non_numeric_addresses() -> None:
    tc, _ = await _pair()
    mgr = LnTurnoutManager("L", tc=tc)
    try:
        await mgr.provide("LTabc")
    except ValueError as e:
        assert "numeric" in str(e)
    else:
        raise AssertionError("expected ValueError on non-numeric address")
    await tc.aclose()
