"""Traffic-controller tests over a loopback transport."""
from __future__ import annotations

import asyncio

from pmri_loconet.messages import (
    LocoNetMessage,
    OpcGpOn,
    OpcInputRep,
    OpcSwReq,
)
from pmri_loconet.traffic import LocoNetTrafficController
from pmri_loconet.transport import loopback_pair


async def test_tc_emits_decoded_message_on_inbound_frame() -> None:
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.0)
    await tc.start()
    received: list[LocoNetMessage] = []
    tc.inbound.connect(received.append)

    # Send an OPC_GPON frame in from "the layout side".
    other = LocoNetTrafficController(b, min_send_interval=0.0)
    await other.send(OpcGpOn())

    # Give the read loop a tick to process.
    for _ in range(5):
        if received:
            break
        await asyncio.sleep(0.01)

    assert any(isinstance(m, OpcGpOn) for m in received)
    await tc.aclose()
    await other.aclose()


async def test_tc_round_trip_through_two_controllers() -> None:
    """A -> B -> A round trip: each side sends two distinct messages, each
    side receives the other's messages."""
    a_port, b_port = loopback_pair()
    a = LocoNetTrafficController(a_port, min_send_interval=0.0)
    b = LocoNetTrafficController(b_port, min_send_interval=0.0)
    await a.start()
    await b.start()

    a_inbox: list[LocoNetMessage] = []
    b_inbox: list[LocoNetMessage] = []
    a.inbound.connect(a_inbox.append)
    b.inbound.connect(b_inbox.append)

    await a.send(OpcSwReq(address=12, closed=True, output_on=True))
    await b.send(OpcInputRep(address=5, active=True))

    for _ in range(10):
        if a_inbox and b_inbox:
            break
        await asyncio.sleep(0.01)

    assert any(isinstance(m, OpcInputRep) and m.address == 5 for m in a_inbox)
    assert any(isinstance(m, OpcSwReq) and m.address == 12 for m in b_inbox)

    await a.aclose()
    await b.aclose()


async def test_tc_send_interval_throttles() -> None:
    """With min_send_interval=10ms, three back-to-back sends must take >= 20ms.

    The first send fires immediately; sends 2 and 3 each wait one interval.
    """
    a, _b = loopback_pair()
    tc = LocoNetTrafficController(a, min_send_interval=0.010)
    start = asyncio.get_event_loop().time()
    await tc.send(OpcGpOn())
    await tc.send(OpcGpOn())
    await tc.send(OpcGpOn())
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.020 - 0.003  # 3ms tolerance for scheduler jitter
    await tc.aclose()
