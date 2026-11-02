"""Traffic-controller lifecycle + error paths."""
from __future__ import annotations

import asyncio

from jmri_dccex.codec import DccexMessage
from jmri_dccex.traffic import DccexTrafficController
from jmri_dccex.transport import loopback_pair


async def test_start_idempotent() -> None:
    a, _ = loopback_pair()
    tc = DccexTrafficController(a)
    await tc.start()
    await tc.start()
    await tc.aclose()


async def test_close_with_no_start_is_noop() -> None:
    a, _ = loopback_pair()
    tc = DccexTrafficController(a)
    await tc.aclose()


async def test_bad_message_swallowed() -> None:
    """A malformed body in brackets should log a warning, not propagate."""
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    await tc.start()
    received: list[DccexMessage] = []
    tc.inbound.connect(received.append)

    # Write a malformed message (empty body), then a good one.
    await b.write(b"<>")
    await b.write(b"<p 1>")

    for _ in range(20):
        if any(m.command == "p" for m in received):
            break
        await asyncio.sleep(0.005)
    assert any(m.command == "p" for m in received)
    await tc.aclose()
