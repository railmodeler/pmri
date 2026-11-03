"""Tests for NceTurnoutMonitor + NceAiuPoller -- the polling layer.

We stand up a "fake command station" as a coroutine that responds to
``read_16_memory`` requests from canned memory state. Mutating the canned
memory between polls simulates a layout cab throwing a turnout -- and the
monitor must detect the change and update the right bean.
"""
from __future__ import annotations

import asyncio

from pmri_core.state import SensorState, TurnoutState

from pmri_nce.beans import NceSensorManager, NceTurnoutManager
from pmri_nce.command_set import NCE_PH_PRO_2006
from pmri_nce.monitors import NceAiuPoller, NceTurnoutMonitor
from pmri_nce.opcodes import (
    ACK_OK,
    OP_AIU_BASE_QUERY,
    OP_READ_16_MEM,
)
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import loopback_pair


# ----- fake command station --------------------------------------------------


class FakeCS:
    """Minimal CS simulator: hosts a memory dict + AIU dict; answers reads."""

    def __init__(self, port) -> None:  # noqa: ANN001
        self._port = port
        self.memory: dict[int, int] = {}        # address -> byte
        self.aiu_words: dict[int, int] = {}     # aiu addr -> 16-bit input word
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while True:
            head = await self._port.read_exact(1)
            op = head[0]
            if op == OP_READ_16_MEM:
                rest = await self._port.read_exact(2)
                addr = (rest[0] << 8) | rest[1]
                payload = bytes(self.memory.get(addr + i, 0) for i in range(16))
                await self._port.write(payload)
            elif op == OP_AIU_BASE_QUERY:
                rest = await self._port.read_exact(1)
                aiu = rest[0]
                word = self.aiu_words.get(aiu, 0)
                await self._port.write(bytes([(word >> 8) & 0xFF, word & 0xFF]))
            else:
                # Unrecognized -- assume single-byte request, ACK it.
                await self._port.write(ACK_OK)


# ----- turnout monitor ------------------------------------------------------


async def test_turnout_monitor_detects_state_flip() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    cs = FakeCS(b)
    cs.start()

    mgr = NceTurnoutManager("N", tc=tc)
    monitor = NceTurnoutMonitor(tc, mgr, NCE_PH_PRO_2006)

    # Pre-create turnout NT1; tell the fake CS its underlying bit is CLEAR (THROWN).
    t = await mgr.provide("NT1")
    # Turnout 1 is bit 0 of byte 0 at memory_base. With value 0, state = THROWN.
    cs.memory[NCE_PH_PRO_2006.turnout_memory_base] = 0x00

    # First poll: snapshot is all zeros, fresh is all zeros -> no diff, no event.
    # But the snapshot is initialized to zeros, so we need a *change* to trigger an event.
    # Run poll, then flip the bit, then poll again.
    await monitor._poll_once()
    assert t.known_state == TurnoutState.UNKNOWN  # no change since startup

    # Flip turnout 1's bit to CLOSED (1).
    cs.memory[NCE_PH_PRO_2006.turnout_memory_base] = 0x01
    await monitor._poll_once()
    # Give the listener microtask a tick.
    for _ in range(10):
        if t.known_state == TurnoutState.CLOSED:
            break
        await asyncio.sleep(0.001)
    assert t.known_state == TurnoutState.CLOSED

    await cs.stop()
    await tc.aclose()


async def test_turnout_monitor_handles_byte_with_multiple_changes() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    cs = FakeCS(b)
    cs.start()
    mgr = NceTurnoutManager("N", tc=tc)
    monitor = NceTurnoutMonitor(tc, mgr, NCE_PH_PRO_2006)

    # Three turnouts in the first byte: bits 0, 3, 7 -> addresses 1, 4, 8.
    t1 = await mgr.provide("NT1")
    t4 = await mgr.provide("NT4")
    t8 = await mgr.provide("NT8")
    await monitor._poll_once()  # baseline snapshot

    # Set bits 0, 3, 7 -> 0b10001001 = 0x89.
    cs.memory[NCE_PH_PRO_2006.turnout_memory_base] = 0x89
    await monitor._poll_once()
    for _ in range(20):
        if (
            t1.known_state == TurnoutState.CLOSED
            and t4.known_state == TurnoutState.CLOSED
            and t8.known_state == TurnoutState.CLOSED
        ):
            break
        await asyncio.sleep(0.001)
    assert t1.known_state == TurnoutState.CLOSED
    assert t4.known_state == TurnoutState.CLOSED
    assert t8.known_state == TurnoutState.CLOSED

    await cs.stop()
    await tc.aclose()


# ----- AIU poller -----------------------------------------------------------


async def test_aiu_poller_detects_input_changes() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    cs = FakeCS(b)
    cs.start()
    mgr = NceSensorManager("N")
    # AIU 1, inputs 1..14 -> sensors 1..14. Pre-create sensor for input 3.
    s3 = await mgr.provide("NS3")
    poller = NceAiuPoller(tc, mgr, aiu_addresses=[1])

    # First poll: word = 0 -> bit 2 (input 3) is INACTIVE.
    cs.aiu_words[1] = 0x0000
    await poller._poll_one(1)
    for _ in range(20):
        if s3.known_state == SensorState.INACTIVE:
            break
        await asyncio.sleep(0.001)
    assert s3.known_state == SensorState.INACTIVE

    # Now activate bit 2 (input 3): word = 0x0004.
    cs.aiu_words[1] = 0x0004
    await poller._poll_one(1)
    for _ in range(20):
        if s3.known_state == SensorState.ACTIVE:
            break
        await asyncio.sleep(0.001)
    assert s3.known_state == SensorState.ACTIVE

    await cs.stop()
    await tc.aclose()
