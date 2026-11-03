"""NCE bean tests -- turnout sends accessory command; sensor is passive;
power manager translates state to OP_POWER_ON/OFF."""
from __future__ import annotations

import asyncio

from pmri_core.state import PowerState, SensorState, TurnoutState

from pmri_nce.beans import NcePowerManager, NceSensorManager, NceTurnoutManager
from pmri_nce.opcodes import ACK_OK, OP_ACCY_DECODER_OPS, OP_POWER_OFF, OP_POWER_ON
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import loopback_pair


async def _drain_one_cmd(b, request_size: int = 5) -> bytes:
    """Read one request from the CS side and return ACK_OK."""
    req = await b.read_exact(request_size)
    await b.write(ACK_OK)
    return req


async def test_turnout_sends_accessory_decoder_command_when_closed() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceTurnoutManager("N", tc=tc)

    captured: list[bytes] = []

    async def fake_cs() -> None:
        captured.append(await _drain_one_cmd(b))

    task = asyncio.create_task(fake_cs())
    t = await mgr.provide("NT12")
    await t.set_commanded_state(TurnoutState.CLOSED)
    await task

    assert captured[0][0] == OP_ACCY_DECODER_OPS
    assert captured[0][2] == 12      # address low byte
    assert captured[0][3] == 3       # NMRA direction nibble: 3 = closed
    assert t.commanded_state == TurnoutState.CLOSED
    # Commanded is mirrored to known for immediate UI feedback; the
    # monitor poll will correct it back if the real CS state differs.
    assert t.known_state == TurnoutState.CLOSED

    await tc.aclose()


async def test_turnout_sends_command_when_thrown() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceTurnoutManager("N", tc=tc)
    captured: list[bytes] = []

    async def fake_cs() -> None:
        captured.append(await _drain_one_cmd(b))

    task = asyncio.create_task(fake_cs())
    t = await mgr.provide("NT100")
    await t.set_commanded_state(TurnoutState.THROWN)
    await task
    assert captured[0][3] == 4       # NMRA direction nibble: 4 = thrown

    await tc.aclose()


async def test_sensor_state_assigned_by_external_caller() -> None:
    mgr = NceSensorManager("N")
    s = await mgr.provide("NS5")
    await s._set_known_state(SensorState.ACTIVE)
    assert s.is_active


async def test_power_manager_sends_correct_opcodes() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    captured: list[int] = []

    async def fake_cs() -> None:
        for _ in range(2):
            req = await b.read_exact(1)
            captured.append(req[0])
            await b.write(ACK_OK)

    task = asyncio.create_task(fake_cs())
    pm = NcePowerManager(tc)
    await pm.set_power(PowerState.ON)
    await pm.set_power(PowerState.OFF)
    await task

    assert captured == [OP_POWER_ON, OP_POWER_OFF]
    assert pm.state == PowerState.OFF

    await tc.aclose()


async def test_turnout_manager_rejects_non_numeric_addresses() -> None:
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceTurnoutManager("N", tc=tc)
    try:
        await mgr.provide("NTabc")
    except ValueError as e:
        assert "numeric" in str(e)
    else:
        raise AssertionError("expected ValueError")
    await tc.aclose()
