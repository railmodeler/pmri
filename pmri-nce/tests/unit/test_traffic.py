"""Traffic-controller tests using a fake-CS coroutine on the other side."""
from __future__ import annotations

import asyncio

import pytest

from pmri_nce.messages import NceCommandError, power_on, read_16_memory
from pmri_nce.opcodes import ACK_ERROR, ACK_OK, OP_READ_16_MEM
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import loopback_pair


async def test_send_command_returns_reply_on_ack_ok() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)

    async def fake_cs() -> None:
        # Read the 3-byte READ16_CMD request, then send back 16 bytes of data.
        req = await b.read_exact(3)
        assert req[0] == OP_READ_16_MEM
        await b.write(bytes(range(16)))

    asyncio.create_task(fake_cs())
    reply = await tc.send_command(read_16_memory(0xEC00))
    assert reply.data == bytes(range(16))

    await tc.aclose()


async def test_send_command_raises_on_ack_error() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)

    async def fake_cs() -> None:
        await b.read_exact(1)        # single-byte POWER_ON request
        await b.write(ACK_ERROR)

    asyncio.create_task(fake_cs())
    with pytest.raises(NceCommandError):
        await tc.send_command(power_on())

    await tc.aclose()


async def test_send_command_times_out() -> None:
    import dataclasses  # noqa: PLC0415
    a, _b = loopback_pair()
    tc = NceTrafficController(a)
    fast = dataclasses.replace(power_on(), timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await tc.send_command(fast)
    await tc.aclose()


async def test_commands_serialize_through_lock() -> None:
    """Two concurrent sends must complete one after the other, not interleaved."""
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    order: list[str] = []

    async def fake_cs() -> None:
        for label in ("first", "second"):
            await b.read_exact(1)  # POWER_ON request
            order.append(label)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await asyncio.gather(tc.send_command(power_on()), tc.send_command(power_on()))
    assert order == ["first", "second"]
    await tc.aclose()


async def test_aclose_blocks_further_sends() -> None:
    a, _b = loopback_pair()
    tc = NceTrafficController(a)
    await tc.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await tc.send_command(power_on())


async def test_short_circuits_when_cs_has_been_silent() -> None:
    """Once the CS has been silent longer than ``SILENCE_THRESHOLD_S`` the
    controller must refuse to touch the port and raise immediately --
    otherwise queued commands each pay 500 ms of serial timeout and stall
    /home renders and profile-loads.

    We simulate silence by hand-writing ``last_success_at`` into the past.
    The first attempt still goes through as a probe; the second must
    short-circuit while the probe window is still armed.
    """
    import time  # noqa: PLC0415

    a, _b = loopback_pair()
    tc = NceTrafficController(a)
    # Simulate: last success was 10 s ago (> 4 s threshold), no probe due.
    tc.last_success_at = time.monotonic() - 10.0
    tc._next_probe_at = None

    # Probe attempt actually reaches the port and times out on read.
    import dataclasses  # noqa: PLC0415
    fast = dataclasses.replace(power_on(), timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await tc.send_command(fast)
    # The probe re-armed the interval.
    assert tc._next_probe_at is not None

    # A second attempt in the same window must NOT reach the port; it
    # short-circuits with a message that names the state, not a serial
    # timeout. The distinction matters: the caller can treat "silent"
    # differently from "one command timed out."
    with pytest.raises(TimeoutError, match="short-circuited"):
        await tc.send_command(power_on())

    await tc.aclose()


async def test_short_circuits_when_cs_off_since_startup() -> None:
    """The regression that shipped in a11: when the CS is off from the
    moment uvicorn starts, ``last_success_at`` stays ``None`` forever,
    so a naive ``if last_success_at is not None`` guard defeats the
    short-circuit and every command still pays a serial timeout.

    Silence must be measured from ``_start_time`` when no success has
    ever landed, so an always-off CS trips the short-circuit as soon
    as startup grace elapses. Simulated here by rewinding
    ``_start_time`` far into the past.
    """
    import time  # noqa: PLC0415

    a, _b = loopback_pair()
    tc = NceTrafficController(a)
    # CS off since startup: no success ever. Simulate the server
    # having been running for 30 s.
    assert tc.last_success_at is None
    tc._start_time = time.monotonic() - 30.0

    # First attempt is a probe: reaches the port, times out on read.
    import dataclasses  # noqa: PLC0415
    fast = dataclasses.replace(power_on(), timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await tc.send_command(fast)
    assert tc._next_probe_at is not None

    # Every subsequent attempt in the probe window short-circuits.
    with pytest.raises(TimeoutError, match="short-circuited"):
        await tc.send_command(power_on())

    await tc.aclose()


async def test_no_short_circuit_during_startup_grace() -> None:
    """Right after ``__init__`` the controller has ``last_success_at =
    None`` but ``_start_time`` is essentially "now". Silence is
    below threshold, so the very first command must go through as a
    normal send -- no premature short-circuit.
    """
    import dataclasses  # noqa: PLC0415

    a, _b = loopback_pair()
    tc = NceTrafficController(a)

    fast = dataclasses.replace(power_on(), timeout_s=0.05)
    with pytest.raises(TimeoutError):
        try:
            await tc.send_command(fast)
        except TimeoutError as e:
            # A "short-circuited" error would be premature -- startup
            # grace hasn't elapsed. Must be a real read-timeout.
            assert "short-circuited" not in str(e)
            raise

    await tc.aclose()
