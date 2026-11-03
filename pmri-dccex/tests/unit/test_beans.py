from __future__ import annotations

import asyncio

import pytest
from pmri_core.state import PowerState, SensorState, TurnoutState

from pmri_dccex.beans import DccexPowerManager, DccexSensorManager, DccexTurnoutManager
from pmri_dccex.codec import DccexMessage, decode, read_message
from pmri_dccex.traffic import DccexTrafficController
from pmri_dccex.transport import loopback_pair


async def _pair() -> tuple[DccexTrafficController, DccexTrafficController]:
    a, b = loopback_pair()
    tc_a = DccexTrafficController(a)
    tc_b = DccexTrafficController(b)
    await tc_a.start()
    await tc_b.start()
    return tc_a, tc_b


async def _drain_one_message_raw(port) -> str:  # noqa: ANN001
    """Read one bracketed message off *port* as plain text."""
    return await read_message(port.read_byte)


async def test_turnout_send_command_closed_is_zero() -> None:
    """DCC-EX wire convention: <T id 0> = CLOSED, <T id 1> = THROWN.
    Verified against
    https://dcc-ex.com/reference/software/command-summary-consolidated.html
    -- this is opposite the DCC accessory-packet convention, so it's a
    common thing to get backwards. This pin protects the fix."""
    tc, layout = await _pair()
    captured: list[DccexMessage] = []
    layout.inbound.connect(captured.append)
    mgr = DccexTurnoutManager("D", tc=tc)
    t = await mgr.provide("DT5")
    await t.set_commanded_state(TurnoutState.CLOSED)

    for _ in range(20):
        if any(m.command == "T" for m in captured):
            break
        await asyncio.sleep(0.005)
    sent = [m for m in captured if m.command == "T"]
    assert sent and sent[0].args == ("5", "0")

    await tc.aclose()
    await layout.aclose()


async def test_turnout_send_command_thrown_is_one() -> None:
    tc, layout = await _pair()
    captured: list[DccexMessage] = []
    layout.inbound.connect(captured.append)
    mgr = DccexTurnoutManager("D", tc=tc)
    t = await mgr.provide("DT6")
    await t.set_commanded_state(TurnoutState.THROWN)

    for _ in range(20):
        if any(m.command == "T" for m in captured):
            break
        await asyncio.sleep(0.005)
    sent = [m for m in captured if m.command == "T"]
    assert sent and sent[0].args == ("6", "1")

    await tc.aclose()
    await layout.aclose()


async def test_turnout_known_state_from_h_message() -> None:
    """<H id 1> = THROWN (matches the DCC-EX wire encoding)."""
    tc, layout = await _pair()
    mgr = DccexTurnoutManager("D", tc=tc)
    t = await mgr.provide("DT9")
    await layout.send(DccexMessage(command="H", args=("9", "1")))
    for _ in range(20):
        if t.known_state == TurnoutState.THROWN:
            break
        await asyncio.sleep(0.005)
    assert t.known_state == TurnoutState.THROWN
    # And 0 -> CLOSED.
    await layout.send(DccexMessage(command="H", args=("9", "0")))
    for _ in range(20):
        if t.known_state == TurnoutState.CLOSED:
            break
        await asyncio.sleep(0.005)
    assert t.known_state == TurnoutState.CLOSED
    await tc.aclose()
    await layout.aclose()


async def test_sensor_q_and_lower_q_update_state() -> None:
    tc, layout = await _pair()
    mgr = DccexSensorManager("D", tc=tc)
    s = await mgr.provide("DS3")
    await layout.send(DccexMessage(command="Q", args=("3",)))
    for _ in range(20):
        if s.is_active:
            break
        await asyncio.sleep(0.005)
    assert s.known_state == SensorState.ACTIVE
    await layout.send(DccexMessage(command="q", args=("3",)))
    for _ in range(20):
        if s.known_state == SensorState.INACTIVE:
            break
        await asyncio.sleep(0.005)
    assert s.known_state == SensorState.INACTIVE
    await tc.aclose()
    await layout.aclose()


async def test_power_on_off_sent() -> None:
    tc, layout = await _pair()
    captured: list[DccexMessage] = []
    layout.inbound.connect(captured.append)
    pm = DccexPowerManager(tc)
    await pm.set_power(PowerState.ON)
    await pm.set_power(PowerState.OFF)
    for _ in range(20):
        kinds = {m.command for m in captured}
        if "1" in kinds and "0" in kinds:
            break
        await asyncio.sleep(0.005)
    kinds = {m.command for m in captured}
    assert "1" in kinds
    assert "0" in kinds
    assert pm.state == PowerState.OFF
    await tc.aclose()
    await layout.aclose()


async def test_power_state_from_p_message() -> None:
    tc, layout = await _pair()
    pm = DccexPowerManager(tc)
    await layout.send(DccexMessage(command="p", args=("1",)))
    for _ in range(20):
        if pm.state == PowerState.ON:
            break
        await asyncio.sleep(0.005)
    assert pm.state == PowerState.ON
    await tc.aclose()
    await layout.aclose()


async def test_turnout_manager_rejects_non_numeric() -> None:
    tc, _ = await _pair()
    mgr = DccexTurnoutManager("D", tc=tc)
    with pytest.raises(ValueError, match="numeric"):
        await mgr.provide("DTabc")
    await tc.aclose()


# Touch the unused decode import to keep coverage honest.
_ = decode
