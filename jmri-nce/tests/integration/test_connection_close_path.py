"""Connection close path tests -- verifies background tasks shut down cleanly."""
from __future__ import annotations

import asyncio

import pytest

from jmri_nce import NceConnection, loopback_pair
from jmri_nce.opcodes import ACK_OK, OP_AIU_BASE_QUERY, OP_READ_16_MEM


async def test_close_cancels_polling_tasks() -> None:
    a, b = loopback_pair()

    async def fake_cs() -> None:
        while True:
            head = await b.read_exact(1)
            if head[0] == OP_READ_16_MEM:
                await b.read_exact(2)
                await b.write(bytes(16))
            elif head[0] == OP_AIU_BASE_QUERY:
                await b.read_exact(1)
                await b.write(bytes([0, 0]))

    cs_task = asyncio.create_task(fake_cs())
    conn = NceConnection()
    # Aggressive AIU list to exercise the poller branch.
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "aiu_addresses": [1, 2], "auto_detect": False,
    })

    # Let one poll cycle elapse.
    await asyncio.sleep(0.05)

    await conn.close()
    assert not conn.is_open

    cs_task.cancel()


async def test_traffic_accessor_before_open_raises() -> None:
    conn = NceConnection()
    with pytest.raises(RuntimeError, match="not open"):
        _ = conn.traffic


async def test_command_set_accessor_before_open_raises() -> None:
    conn = NceConnection()
    with pytest.raises(RuntimeError, match="not open"):
        _ = conn.command_set
