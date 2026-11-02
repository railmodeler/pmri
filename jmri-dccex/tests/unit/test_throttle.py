"""DCC-EX throttle tests."""
from __future__ import annotations

import asyncio

from jmri_core.state import ThrottleDirection

from jmri_dccex.codec import DccexMessage
from jmri_dccex.throttle import DccexThrottleManager, speed_to_wire_128
from jmri_dccex.traffic import DccexTrafficController
from jmri_dccex.transport import loopback_pair


def test_speed_to_wire_extremes() -> None:
    """DCC-EX wire form: -1 = emergency stop, 0 = normal stop,
    1..127 = speed steps. Confirmed against DCC-EX command reference;
    the earlier "wire=1 for e-stop" mapping was wrong and left the
    locomotive creeping at step 1 when the operator hit e-stop."""
    assert speed_to_wire_128(0.0) == 0
    assert speed_to_wire_128(-1.0) == -1
    assert speed_to_wire_128(1.0) == 127
    assert speed_to_wire_128(0.5) == 64


def test_estop_emits_negative_one_on_wire() -> None:
    """A throttle e-stop must produce ``<t cab -1 dir>``, not ``<t cab 1 dir>``.
    Regression pin for the DCC-EX-specific e-stop encoding."""
    assert speed_to_wire_128(-1.0) == -1


async def _build_pair() -> tuple[
    DccexTrafficController, DccexTrafficController, list[DccexMessage],
]:
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    layout = DccexTrafficController(b)
    await tc.start()
    await layout.start()
    captured: list[DccexMessage] = []
    layout.inbound.connect(captured.append)
    return tc, layout, captured


async def test_throttle_set_speed_sends_t_command() -> None:
    tc, layout, captured = await _build_pair()
    mgr = DccexThrottleManager("D", tc=tc)
    t = await mgr.acquire(3)
    await t.set_speed(0.5)
    for _ in range(20):
        if any(m.command == "t" for m in captured):
            break
        await asyncio.sleep(0.005)
    sent = [m for m in captured if m.command == "t"]
    assert sent
    # Forward by default -> direction=1; speed wire=64
    assert sent[-1].args == ("3", "64", "1")
    await tc.aclose()
    await layout.aclose()


async def test_throttle_set_direction_reverses() -> None:
    tc, layout, captured = await _build_pair()
    mgr = DccexThrottleManager("D", tc=tc)
    t = await mgr.acquire(3)
    await t.set_speed(0.5)
    captured.clear()
    await t.set_direction(ThrottleDirection.REVERSE)
    for _ in range(20):
        if any(m.command == "t" for m in captured):
            break
        await asyncio.sleep(0.005)
    sent = [m for m in captured if m.command == "t"]
    assert sent and sent[-1].args[2] == "0"
    await tc.aclose()
    await layout.aclose()


async def test_throttle_set_function_sends_F_command() -> None:
    tc, layout, captured = await _build_pair()
    mgr = DccexThrottleManager("D", tc=tc)
    t = await mgr.acquire(3)
    captured.clear()
    await t.set_function(0, True)
    for _ in range(20):
        if any(m.command == "F" for m in captured):
            break
        await asyncio.sleep(0.005)
    sent = [m for m in captured if m.command == "F"]
    assert sent
    assert sent[-1].args == ("3", "0", "1")
    await tc.aclose()
    await layout.aclose()


async def test_throttle_acquire_idempotent() -> None:
    a, _ = loopback_pair()
    tc = DccexTrafficController(a)
    mgr = DccexThrottleManager("D", tc=tc)
    a_t = await mgr.acquire(3)
    b_t = await mgr.acquire(3)
    assert a_t is b_t
    await tc.aclose()
