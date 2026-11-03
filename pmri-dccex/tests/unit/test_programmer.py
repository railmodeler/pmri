"""DCC-EX CV programmer tests."""
from __future__ import annotations

import asyncio

import pytest
from pmri_core.beans.programmer import ProgrammingMode

from pmri_dccex.codec import DccexMessage
from pmri_dccex.programmer import (
    DccexProgrammer,
    DccexProgrammerManager,
)
from pmri_dccex.traffic import DccexTrafficController
from pmri_dccex.transport import loopback_pair


async def _build_with_fake_cs(
    cv_state: dict[int, int], *, railcom: bool = False,
):
    """Fake CS mirroring the reply shapes real DCC-EX firmware sends.

    * ``<W cv val>`` -> ``<r cv val>`` (write ack -- from callback_W)
    * ``<R cv>``     -> ``<v cv val>`` (read reply -- from callback_Vbyte)
    * ``<w cab cv val>`` -> no reply
    * ``<r cab cv>`` -> ``<r cab cv val>`` iff *railcom* is True; else
      the CS silently drops the request (matches the firmware's
      ``if (!DCCWaveform::isRailcom()) break;`` guard).

    See :mod:`pmri_dccex.programmer` docstring for the upstream
    parser reference these callbacks are cited from.
    """
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    layout = DccexTrafficController(b)
    await tc.start()
    await layout.start()

    async def cs(msg: DccexMessage) -> None:
        if msg.command == "W" and len(msg.args) == 2:
            cv = int(msg.args[0])
            value = int(msg.args[1])
            cv_state[cv] = value
            # Write ack -- callback_W -> <r cv val>.
            await layout.send(DccexMessage(command="r", args=(str(cv), str(value))))
        elif msg.command == "R" and len(msg.args) == 1:
            cv = int(msg.args[0])
            value = cv_state.get(cv, 0)
            # Read reply -- callback_Vbyte -> <v cv val>.
            await layout.send(DccexMessage(command="v", args=(str(cv), str(value))))
        elif msg.command == "w":
            cv = int(msg.args[1])
            value = int(msg.args[2])
            cv_state[(int(msg.args[0]), cv)] = value  # type: ignore[index]
        elif msg.command == "r" and len(msg.args) == 2 and railcom:
            # Ops-mode read via RailCom -- callback_r -> <r cab cv val>.
            cab = int(msg.args[0])
            cv = int(msg.args[1])
            value = cv_state.get((cab, cv), 0)  # type: ignore[call-overload]
            await layout.send(DccexMessage(
                command="r", args=(str(cab), str(cv), str(value)),
            ))
        # else: RailCom disabled -- silently drop, matching CS behaviour.
    layout.inbound.connect(cs)
    return tc, layout


async def test_service_write_then_read_round_trip() -> None:
    cv_state: dict[int, int] = {}
    tc, layout = await _build_with_fake_cs(cv_state)
    prog = DccexProgrammer(tc)
    await prog.write_cv_service(29, 6)
    assert cv_state[29] == 6
    value = await prog.read_cv_service(29)
    assert value == 6
    await tc.aclose()
    await layout.aclose()


async def test_read_failure_raises() -> None:
    """If the CS replies with negative value, the programmer raises."""
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    layout = DccexTrafficController(b)
    await tc.start()
    await layout.start()

    async def cs(msg: DccexMessage) -> None:
        if msg.command == "R":
            # <v cv -1> is the failure form for service-mode reads.
            await layout.send(DccexMessage(command="v", args=(msg.args[0], "-1")))
    layout.inbound.connect(cs)

    prog = DccexProgrammer(tc)
    with pytest.raises(RuntimeError, match="failed"):
        await prog.read_cv_service(1, timeout=1.0)
    await tc.aclose()
    await layout.aclose()


async def test_read_accepts_legacy_r_reply() -> None:
    """Some older firmwares (and third-party reimplementations) emit
    ``<r cv val>`` for service-mode reads instead of the canonical
    ``<v cv val>``. Accept both so we don't break against those."""
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    layout = DccexTrafficController(b)
    await tc.start()
    await layout.start()

    async def cs(msg: DccexMessage) -> None:
        if msg.command == "R":
            await layout.send(DccexMessage(
                command="r", args=(msg.args[0], "77"),
            ))
    layout.inbound.connect(cs)

    prog = DccexProgrammer(tc)
    value = await prog.read_cv_service(29, timeout=1.0)
    assert value == 77
    await tc.aclose()
    await layout.aclose()


async def test_ops_mode_write_fire_and_forget() -> None:
    cv_state: dict[tuple[int, int], int] = {}
    tc, layout = await _build_with_fake_cs(cv_state)   # type: ignore[arg-type]
    prog = DccexProgrammer(tc)
    await prog.write_cv_ops(3, cv=1, value=42)
    await asyncio.sleep(0.02)
    assert cv_state.get((3, 1)) == 42
    await tc.aclose()
    await layout.aclose()


async def test_programmer_manager_modes() -> None:
    a, _ = loopback_pair()
    tc = DccexTrafficController(a)
    mgr = DccexProgrammerManager(DccexProgrammer(tc))
    assert mgr.global_programmer().mode == ProgrammingMode.SERVICE_DIRECT
    ops = mgr.ops_programmer(3, long_address=False)
    assert ops.mode == ProgrammingMode.OPS_MODE
    await tc.aclose()


async def test_ops_read_via_railcom_success() -> None:
    """RailCom-enabled CS returns <r cab cv val> -- the ops programmer
    should surface that as a plain int."""
    cv_state: dict[tuple[int, int], int] = {(3, 29): 6}
    tc, layout = await _build_with_fake_cs(cv_state, railcom=True)  # type: ignore[arg-type]
    mgr = DccexProgrammerManager(DccexProgrammer(tc))
    ops = mgr.ops_programmer(3)
    value = await ops.read_cv(29)
    assert value == 6
    await tc.aclose()
    await layout.aclose()


async def test_ops_read_failure_raises() -> None:
    """When RailCom returns -1 (decoder didn't answer via the cutout),
    the ops programmer raises RuntimeError so the caller can report
    'no decoder saw the request' without confusing it with a comms
    timeout."""
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    layout = DccexTrafficController(b)
    await tc.start()
    await layout.start()

    async def cs(msg: DccexMessage) -> None:
        if msg.command == "r" and len(msg.args) == 2:
            # Simulate a RailCom-enabled CS whose decoder didn't answer.
            await layout.send(DccexMessage(
                command="r", args=(msg.args[0], msg.args[1], "-1"),
            ))
    layout.inbound.connect(cs)

    mgr = DccexProgrammerManager(DccexProgrammer(tc))
    ops = mgr.ops_programmer(3)
    with pytest.raises(RuntimeError, match="no RailCom response"):
        await ops.read_cv(29)
    await tc.aclose()
    await layout.aclose()


async def test_ops_read_timeout_when_railcom_disabled() -> None:
    """Firmware built without RailCom silently drops <r cab cv>. From
    the client side that's an ordinary timeout, so we surface it as
    a RuntimeError whose message points at the RailCom config bit --
    otherwise the operator can't tell 'CS crashed' from 'firmware
    doesn't have RailCom'."""
    cv_state: dict[tuple[int, int], int] = {}
    tc, layout = await _build_with_fake_cs(cv_state, railcom=False)  # type: ignore[arg-type]
    mgr = DccexProgrammerManager(DccexProgrammer(tc))
    # Use the inner programmer with a short timeout so the test is fast.
    with pytest.raises(RuntimeError, match="RailCom"):
        await mgr.inner.read_cv_ops(3, 29, timeout=0.15)
    await tc.aclose()
    await layout.aclose()
