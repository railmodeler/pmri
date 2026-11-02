"""Traction Management heartbeat tests -- send_noop + HeartbeatRequest
signal + HeartbeatKeeper + CS-side last_activity tracking."""
from __future__ import annotations

import asyncio
import time

import pytest
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.events import Signal

from jmri_openlcb.addressed import (
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.alias import NodeId
from jmri_openlcb.codec import OlcbFrame
from jmri_openlcb.command_station import CommandStationNode
from jmri_openlcb.traction import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    MGMT_HEARTBEAT_REQUEST,
    MGMT_NOOP,
    HeartbeatKeeper,
    HeartbeatRequest,
    MTI_TRACTION_REQUEST,
    TC_MANAGEMENT,
    TC_SET_SPEED_DIR,
    TractionClient,
    encode_speed,
)


STATION_NID = NodeId.parse("02.01.12.FE.DC.BA")


class _FakeTc:
    def __init__(self, source_alias: int) -> None:
        self.source_alias = source_alias
        self.frames: Signal[OlcbFrame] = Signal()
        self._peer: _FakeTc | None = None
        self.sent: list[OlcbFrame] = []

    def link(self, peer: _FakeTc) -> None:
        self._peer = peer

    async def send_frame(self, frame: OlcbFrame) -> None:
        self.sent.append(frame)
        if self._peer is not None:
            await self._peer.frames.emit(frame)


def _pair(alias_a: int, alias_b: int) -> tuple[_FakeTc, _FakeTc]:
    a = _FakeTc(alias_a)
    b = _FakeTc(alias_b)
    a.link(b)
    b.link(a)
    return a, b


def _last_request(tc: _FakeTc):
    r = AddressedReassembler()
    last = None
    for f in tc.sent:
        m = r.receive(f)
        if m is not None and m.mti == MTI_TRACTION_REQUEST:
            last = m
    return last


# ---- TractionClient.send_noop -------------------------------------------


async def test_send_noop_produces_management_noop_payload() -> None:
    """Payload = 0x40, 0x01. Fire-and-forget -- no reply expected."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    try:
        await client.send_noop(0x222)
        msg = _last_request(a)
        assert msg is not None
        assert msg.dest_alias == 0x222
        assert msg.body == bytes([TC_MANAGEMENT, MGMT_NOOP])
    finally:
        client.close()


# ---- heartbeat_requested signal from inbound frames --------------------


async def test_heartbeat_request_from_loco_fires_signal() -> None:
    """A loco emitting a Traction Control Command with 0x40 0x02 is
    the "controller, are you there?" request. Signal payload carries
    source alias + timeout window from byte 2."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    seen: list[HeartbeatRequest] = []
    client.heartbeat_requested.connect(seen.append)
    try:
        # Loco (aliased at 0x222) sends a Heartbeat Request to us (0x111)
        # with a 3-second timeout window.
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x111,
            bytes([TC_MANAGEMENT, MGMT_HEARTBEAT_REQUEST, 3]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert len(seen) == 1
        assert seen[0].loco_alias == 0x222
        assert seen[0].timeout_seconds == 3
    finally:
        client.close()


async def test_heartbeat_request_with_missing_timeout_uses_default() -> None:
    """A truncated Heartbeat Request that omits the timeout byte
    still fires the signal -- the client falls back to the spec's
    default window rather than dropping the request on the floor."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    seen: list[HeartbeatRequest] = []
    client.heartbeat_requested.connect(seen.append)
    try:
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x111,
            bytes([TC_MANAGEMENT, MGMT_HEARTBEAT_REQUEST]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert len(seen) == 1
        assert seen[0].timeout_seconds == DEFAULT_HEARTBEAT_TIMEOUT
    finally:
        client.close()


async def test_traction_request_that_is_not_a_heartbeat_ignored() -> None:
    """Only 0x40 0x02 fires the heartbeat signal -- a stray inbound
    Set Speed request (unusual but possible on a chatty bus) must
    not spuriously trigger it."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    seen: list[HeartbeatRequest] = []
    client.heartbeat_requested.connect(seen.append)
    try:
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x111,
            bytes([TC_SET_SPEED_DIR]) + encode_speed(10.0, forward=True),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert seen == []
    finally:
        client.close()


# ---- HeartbeatKeeper ----------------------------------------------------


async def test_keeper_answers_heartbeat_for_tracked_loco() -> None:
    """Loco 0x222 is in our tracked set; a Heartbeat Request from it
    should provoke an automatic Noop from us."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    keeper = HeartbeatKeeper(client)
    try:
        keeper.add(0x222)
        assert keeper.tracks(0x222)
        assert keeper.tracked() == {0x222}
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x111,
            bytes([TC_MANAGEMENT, MGMT_HEARTBEAT_REQUEST, 3]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        msg = _last_request(a)
        assert msg is not None
        assert msg.dest_alias == 0x222
        assert msg.body == bytes([TC_MANAGEMENT, MGMT_NOOP])
    finally:
        await keeper.close()
        client.close()


async def test_keeper_ignores_heartbeat_for_untracked_loco() -> None:
    """A Heartbeat Request from a loco we're not controlling must
    not draw a Noop -- otherwise every throttle on the bus would
    fight to answer every heartbeat."""
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    keeper = HeartbeatKeeper(client)
    try:
        # Don't add anyone.
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x111,
            bytes([TC_MANAGEMENT, MGMT_HEARTBEAT_REQUEST, 3]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert _last_request(a) is None
    finally:
        await keeper.close()
        client.close()


async def test_keeper_remove_stops_answering() -> None:
    a, b = _pair(0x111, 0x222)
    client = TractionClient(a)
    keeper = HeartbeatKeeper(client)
    try:
        keeper.add(0x222)
        keeper.remove(0x222)
        assert not keeper.tracks(0x222)
        assert keeper.tracked() == set()
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, 0x111,
            bytes([TC_MANAGEMENT, MGMT_HEARTBEAT_REQUEST, 3]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert _last_request(a) is None
    finally:
        await keeper.close()
        client.close()


async def test_periodic_noop_fires_for_every_tracked_loco() -> None:
    """When start_periodic_noop runs, every tracked loco gets a
    Noop each cycle. Two locos -> two Noops per tick."""
    a, _ = _pair(0x111, 0x222)
    client = TractionClient(a)
    keeper = HeartbeatKeeper(client)
    try:
        keeper.add(0x300)
        keeper.add(0x400)
        keeper.start_periodic_noop(interval=0.05)
        await asyncio.sleep(0.12)
        await keeper.stop_periodic_noop()
        # At least one tick fired -> two Noops per tick, so 2+ total.
        r = AddressedReassembler()
        noops = []
        for f in a.sent:
            m = r.receive(f)
            if m is None:
                continue
            if m.body == bytes([TC_MANAGEMENT, MGMT_NOOP]):
                noops.append(m.dest_alias)
        # Deduplicated by dest -- we should have hit both locos.
        assert set(noops) >= {0x300, 0x400}
    finally:
        await keeper.close()
        client.close()


async def test_start_periodic_noop_is_idempotent() -> None:
    """A second start while one is running should not spawn a
    second task -- else the tick rate would double silently."""
    tc = _FakeTc(0x111)
    client = TractionClient(tc)
    keeper = HeartbeatKeeper(client)
    try:
        keeper.start_periodic_noop(interval=1.0)
        keeper.start_periodic_noop(interval=0.01)   # ignored
        # Give it time to potentially do a tick at 0.01 (would be
        # ~10 noops) vs 1.0 (would be 0). We asserted 1.0 sticks.
        await asyncio.sleep(0.05)
        await keeper.stop_periodic_noop()
        # At 1.0-second interval with 0.05s of sleep, no noop should
        # have fired yet.
        assert not any(m for f in tc.sent for m in [f])
    finally:
        await keeper.close()


async def test_stop_before_start_is_safe() -> None:
    """Stopping the periodic Noop when none is running must not
    raise -- makes lifecycle code simpler for callers who bail early."""
    tc = _FakeTc(0x111)
    client = TractionClient(tc)
    keeper = HeartbeatKeeper(client)
    try:
        await keeper.stop_periodic_noop()   # no-op
    finally:
        await keeper.close()


async def test_close_is_idempotent() -> None:
    tc = _FakeTc(0x111)
    client = TractionClient(tc)
    keeper = HeartbeatKeeper(client)
    await keeper.close()
    await keeper.close()   # should not raise


# ---- CS-side: last_activity_at tracking ---------------------------------


async def _cs():
    a, b = _pair(0x111, 0x222)
    dcc = ThrottleManager("X")
    cs = CommandStationNode(a, dcc, station_node_id=STATION_NID)
    return a, b, dcc, cs


async def test_cs_bumps_last_activity_on_noop() -> None:
    """A Noop received by the CS should update the hosted loco's
    last_activity_at -- that's the whole point of the keepalive."""
    a, b, _, cs = await _cs()
    try:
        loco = await cs.add_loco(1)
        assert loco.last_activity_at == 0.0
        before = time.monotonic()
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_MANAGEMENT, MGMT_NOOP]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert before <= loco.last_activity_at <= time.monotonic()
    finally:
        cs.close()


async def test_cs_bumps_last_activity_on_any_traction_command() -> None:
    """Not just Noop -- every traction command counts as activity.
    The CS doesn't distinguish "keepalive-only" from real work; if
    the controller is talking to us at all, we treat the loco as
    live."""
    a, b, _, cs = await _cs()
    try:
        loco = await cs.add_loco(1)
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_SET_SPEED_DIR]) + encode_speed(10.0, forward=True),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert loco.last_activity_at > 0.0
    finally:
        cs.close()


async def test_cs_noop_does_not_acquire_dcc_throttle() -> None:
    """Noop is pure bookkeeping. It must not spuriously acquire a
    DCC slot for a loco no one has commanded yet -- otherwise a
    watchdog-only throttle would rack up phantom acquisitions."""
    a, b, _, cs = await _cs()
    try:
        loco = await cs.add_loco(1)
        assert loco.throttle is None
        for f in encode_addressed_frames(
            MTI_TRACTION_REQUEST, 0x222, loco.alias,
            bytes([TC_MANAGEMENT, MGMT_NOOP]),
        ):
            await b.send_frame(f)
        await asyncio.sleep(0)
        assert loco.throttle is None
    finally:
        cs.close()
