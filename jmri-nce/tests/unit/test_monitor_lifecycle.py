"""Short-lived monitor lifecycle tests -- exercise run() and stop()."""
from __future__ import annotations

import asyncio

from jmri_nce.beans import NceSensorManager, NceTurnoutManager
from jmri_nce.command_set import NCE_PH_PRO_2006
from jmri_nce.monitors import NceAiuPoller, NceTurnoutMonitor
from jmri_nce.opcodes import ACK_OK, OP_AIU_BASE_QUERY, OP_READ_16_MEM
from jmri_nce.traffic import NceTrafficController
from jmri_nce.transport import loopback_pair


async def test_turnout_monitor_runs_and_stops_cleanly() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceTurnoutManager("N", tc=tc)

    async def fake_cs() -> None:
        while True:
            head = await b.read_exact(1)
            if head[0] == OP_READ_16_MEM:
                await b.read_exact(2)
                await b.write(bytes(16))

    cs_task = asyncio.create_task(fake_cs())
    monitor = NceTurnoutMonitor(tc, mgr, NCE_PH_PRO_2006)
    # Make the poll interval very short so we run at least one cycle quickly.
    monitor.POLL_INTERVAL_S = 0.01
    run_task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.05)
    monitor.stop()
    await asyncio.wait_for(run_task, timeout=1.0)
    cs_task.cancel()
    await tc.aclose()


async def test_aiu_poller_runs_and_stops_cleanly() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceSensorManager("N")

    async def fake_cs() -> None:
        while True:
            head = await b.read_exact(1)
            if head[0] == OP_AIU_BASE_QUERY:
                await b.read_exact(1)
                await b.write(bytes([0, 0]))

    cs_task = asyncio.create_task(fake_cs())
    poller = NceAiuPoller(tc, mgr, aiu_addresses=[1])
    poller.POLL_INTERVAL_S = 0.01
    run_task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await asyncio.wait_for(run_task, timeout=1.0)
    cs_task.cancel()
    await tc.aclose()
