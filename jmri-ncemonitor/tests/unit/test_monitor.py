"""Monitor read-loop tests over a loopback transport."""
from __future__ import annotations

import asyncio

from jmri_ncemonitor.messages import (
    ADDR_ACCESSORY,
    ADDR_AIU_UPDATE,
    AccessoryObserved,
    AiuUpdate,
    CabBusMessage,
    CabPoll,
    encode,
)
from jmri_ncemonitor.monitor import CabBusMonitor
from jmri_ncemonitor.transport import loopback_pair


async def _capture(monitor: CabBusMonitor) -> list[CabBusMessage]:
    captured: list[CabBusMessage] = []
    monitor.inbound.connect(captured.append)
    return captured


async def test_monitor_decodes_cab_poll() -> None:
    a, b = loopback_pair()
    monitor = CabBusMonitor(a)
    await monitor.start()
    captured = await _capture(monitor)

    # Send the poll plus a trailing boundary byte so the monitor's
    # "read until next address" loop can complete.
    await b.write(encode(CabPoll(address=2, command=0x00)))
    await b.write(bytes([0x80]))      # next address byte = end-of-message marker

    for _ in range(40):
        if any(isinstance(m, CabPoll) and m.address == 2 for m in captured):
            break
        await asyncio.sleep(0.005)
    assert any(isinstance(m, CabPoll) and m.address == 2 for m in captured)

    await monitor.aclose()


async def test_monitor_decodes_accessory_and_aiu() -> None:
    a, b = loopback_pair()
    monitor = CabBusMonitor(a)
    await monitor.start()
    captured = await _capture(monitor)

    # Two messages back-to-back: accessory packet then AIU update.
    await b.write(encode(AccessoryObserved(
        address=ADDR_ACCESSORY, accessory_address=12, closed=True
    )))
    await b.write(encode(AiuUpdate(
        address=ADDR_AIU_UPDATE, aiu_module=1, input_word=0x0001
    )))
    # Send a trailing benign poll so the second message's boundary is hit.
    await b.write(encode(CabPoll(address=0, command=0x00)))

    for _ in range(40):
        if (
            any(isinstance(m, AccessoryObserved) for m in captured)
            and any(isinstance(m, AiuUpdate) for m in captured)
        ):
            break
        await asyncio.sleep(0.005)

    accy = next(m for m in captured if isinstance(m, AccessoryObserved))
    assert accy.accessory_address == 12
    assert accy.closed is True
    aiu = next(m for m in captured if isinstance(m, AiuUpdate))
    assert aiu.aiu_module == 1
    assert aiu.input_word == 0x0001

    await monitor.aclose()


async def test_monitor_close_idempotent() -> None:
    a, _ = loopback_pair()
    monitor = CabBusMonitor(a)
    await monitor.start()
    await monitor.aclose()
    # Second close should not raise.
    await monitor.aclose()


async def test_monitor_resyncs_after_body_overflow() -> None:
    """A stuck bus that emits an address byte followed by >32 data
    bytes with no next address must not kill the monitor. Instead
    the overflow is logged, the partial dropped, and the next
    address byte in the stream produces a valid message."""
    from jmri_ncemonitor.codec import MAX_BODY_BYTES  # noqa: PLC0415

    a, b = loopback_pair()
    monitor = CabBusMonitor(a)
    await monitor.start()
    captured = await _capture(monitor)

    # Address 0x80, then enough data bytes to overflow the cap,
    # then a legit CabPoll frame + closing address byte.
    await b.write(bytes([0x80]) + bytes([0x00] * (MAX_BODY_BYTES + 5)))
    await b.write(encode(CabPoll(address=3, command=0x00)))
    await b.write(bytes([0x80]))  # boundary for the CabPoll

    for _ in range(60):
        if any(isinstance(m, CabPoll) and m.address == 3 for m in captured):
            break
        await asyncio.sleep(0.005)

    # The CabPoll at address 3 must land regardless of the earlier
    # overflow -- that's the whole point of the resync.
    assert any(isinstance(m, CabPoll) and m.address == 3 for m in captured)

    await monitor.aclose()
