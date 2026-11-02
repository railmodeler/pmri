"""Unit tests for :mod:`jmri_openlcb.memory`.

Uses a two-node loopback bus. One side runs a real
:class:`~jmri_openlcb.memory.MemoryClient`; the other side plays a
minimal in-memory "device" that answers read/write requests. The
tests exercise the command-byte encoding, address round-trip,
success + failure replies, and multi-chunk splitting.
"""
from __future__ import annotations

import asyncio

import pytest

from jmri_openlcb import (
    DatagramEngine,
    MemoryClient,
    MemoryError_,
    OlcbTrafficController,
    SPACE_CDI,
    SPACE_CONFIG,
    loopback_pair,
)
from jmri_openlcb.datagrams import Datagram
from jmri_openlcb.memory import (
    CMD_FACTORY_RESET,
    CMD_FREEZE,
    CMD_REBOOT,
    CMD_UNFREEZE,
    FAIL_OUT_OF_BOUNDS,
    FAIL_PERMANENT,
    FAIL_READ_ONLY,
    FAIL_UNKNOWN_SPACE,
    MEMORY_CONFIG,
    MemoryConfigResponder,
    _compact_space_bits,
)


async def _make_bus():
    a, b = loopback_pair()
    tc_a = OlcbTrafficController(a, source_alias=0x111)
    tc_b = OlcbTrafficController(b, source_alias=0x222)
    await tc_a.start()
    await tc_b.start()
    eng_a = DatagramEngine(tc_a)
    eng_b = DatagramEngine(tc_b)
    await eng_a.attach()
    await eng_b.attach()
    return tc_a, tc_b, eng_a, eng_b


async def _teardown(*things) -> None:  # noqa: ANN002
    for thing in things:
        if isinstance(thing, MemoryClient):
            thing.close()
    for thing in things:
        if isinstance(thing, DatagramEngine):
            await thing.detach()
    for thing in things:
        if isinstance(thing, OlcbTrafficController):
            await thing.aclose()


class _FakeMemoryDevice:
    """Answer memory reads/writes on the peer side.

    Backing store is a dict keyed by ``(space, address)`` -> byte, so
    tests can seed / inspect arbitrary sparse addresses without
    caring about layout.
    """

    def __init__(self, engine: DatagramEngine, tc: OlcbTrafficController) -> None:
        self._engine = engine
        self._tc = tc
        self.store: dict[tuple[int, int], int] = {}
        self._unsubscribe = engine.inbound.connect(self._on_inbound)

    def close(self) -> None:
        self._unsubscribe()

    async def _on_inbound(self, dg: Datagram) -> None:
        if len(dg.data) < 6 or dg.data[0] != MEMORY_CONFIG:
            return
        command = dg.data[1]
        addr = int.from_bytes(dg.data[2:6], "big")
        # Space encoding -- compact form when low 3 bits map to
        # 0xFD..0xFF; otherwise the space byte follows the address.
        compact_bits = command & 0b111
        if compact_bits == 0b101:
            space = 0xFD
            payload_start = 6
        elif compact_bits == 0b110:
            space = 0xFE
            payload_start = 6
        elif compact_bits == 0b111:
            space = 0xFF
            payload_start = 6
        else:
            space = dg.data[6]
            payload_start = 7

        if (command & 0xF0) == 0x40:  # read
            count = dg.data[payload_start] if payload_start < len(dg.data) else 0
            data_out = bytes(
                self.store.get((space, addr + i), 0) for i in range(count)
            )
            reply = self._build_reply(0x50, command, addr, space, data_out)
        elif (command & 0xF0) == 0x00:  # write
            payload = dg.data[payload_start:]
            for i, b in enumerate(payload):
                self.store[(space, addr + i)] = b
            reply = self._build_reply(0x10, command, addr, space, b"")
        else:
            return
        # Reply is itself a datagram from us to the sender.
        await self._engine.send(
            dg.source_alias, reply, wait_for_reply=False,
        )

    def _build_reply(
        self, base_cmd: int, request_cmd: int, addr: int,
        space: int, payload: bytes,
    ) -> bytes:
        # Reply command mirrors the request's low 3 bits so the
        # client knows which space it was.
        compact_bits = request_cmd & 0b111
        cmd = base_cmd | compact_bits
        body = bytes([MEMORY_CONFIG, cmd]) + addr.to_bytes(4, "big")
        if compact_bits not in (0b101, 0b110, 0b111):
            body += bytes([space])
        body += payload
        return body


# ---- happy paths -------------------------------------------------------


async def test_read_small_range_from_cdi_space() -> None:
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        for i in range(10):
            device.store[(SPACE_CDI, i)] = 0xA0 + i
        data = await client.read(tc_b.source_alias, SPACE_CDI, 0, 10)
        assert data == bytes(range(0xA0, 0xAA))
    finally:
        device.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_write_then_read_roundtrip() -> None:
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        payload = bytes(range(50))
        await client.write(tc_b.source_alias, SPACE_CONFIG, 0x100, payload)
        got = await client.read(
            tc_b.source_alias, SPACE_CONFIG, 0x100, 50,
        )
        assert got == payload
    finally:
        device.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_read_splits_into_max_chunks() -> None:
    """A 100-byte read exceeds the 64-byte per-request cap; the
    client fragments transparently."""
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        for i in range(100):
            device.store[(SPACE_CDI, i)] = i & 0xFF
        data = await client.read(tc_b.source_alias, SPACE_CDI, 0, 100)
        assert data == bytes(range(100))
    finally:
        device.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_write_splits_into_max_chunks() -> None:
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        payload = bytes((i * 3) & 0xFF for i in range(100))
        await client.write(tc_b.source_alias, SPACE_CONFIG, 0, payload)
        got = await client.read(tc_b.source_alias, SPACE_CONFIG, 0, 100)
        assert got == payload
    finally:
        device.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_generic_space_uses_explicit_byte() -> None:
    """Space 0x51 doesn't have a compact-form encoding; the client
    must emit the explicit space byte after the address, and the
    device (a full request/reply implementation of the protocol)
    must round-trip that too."""
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    device = _FakeMemoryDevice(eng_b, tc_b)
    client = MemoryClient(eng_a)
    try:
        await client.write(tc_b.source_alias, 0x51, 0, b"\x11\x22\x33")
        got = await client.read(tc_b.source_alias, 0x51, 0, 3)
        assert got == b"\x11\x22\x33"
    finally:
        device.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


# ---- failure paths -----------------------------------------------------


async def test_failure_reply_raises_memory_error() -> None:
    """A device reply with the failure bit set (bit 3 of the command
    byte) must surface as :class:`MemoryError_` on the client."""
    tc_a, tc_b, eng_a, eng_b = await _make_bus()

    async def rejecter(dg: Datagram) -> None:
        if len(dg.data) < 6 or dg.data[0] != MEMORY_CONFIG:
            return
        # Compose a failure reply: 0x58 = read+fail, matching the low
        # bits of the request command. Error code 0x1234.
        cmd = 0x58 | (dg.data[1] & 0b111)
        addr = dg.data[2:6]
        payload = bytes([MEMORY_CONFIG, cmd]) + addr + b"\x12\x34"
        await eng_b.send(dg.source_alias, payload, wait_for_reply=False)

    eng_b.inbound.connect(rejecter)
    client = MemoryClient(eng_a)
    try:
        with pytest.raises(MemoryError_) as exc:
            await client.read(tc_b.source_alias, SPACE_CDI, 0, 4)
        assert exc.value.error_code == 0x1234
    finally:
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


# ---- helper coverage ---------------------------------------------------


def test_compact_space_bits_maps_known_spaces() -> None:
    """Regression against the S-9.7.4.2 table: 0xFD -> 5, 0xFE -> 6,
    0xFF -> 7. Unknown spaces return None."""
    assert _compact_space_bits(0xFD) == 0b101
    assert _compact_space_bits(0xFE) == 0b110
    assert _compact_space_bits(0xFF) == 0b111
    assert _compact_space_bits(0x00) is None
    assert _compact_space_bits(0x42) is None


# ---- MemoryConfigResponder ---------------------------------------------


async def _bring_up_client_responder() -> tuple[
    OlcbTrafficController, OlcbTrafficController,
    DatagramEngine, DatagramEngine,
    MemoryClient, MemoryConfigResponder,
]:
    """Two-sided bus: a client on side A, a responder on side B."""
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    client = MemoryClient(eng_a)
    responder = MemoryConfigResponder(eng_b)
    return tc_a, tc_b, eng_a, eng_b, client, responder


async def test_responder_serves_read_from_populated_space() -> None:
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CDI, b"hello, world!")
        data = await client.read(tc_b.source_alias, SPACE_CDI, 0, 13)
        assert data == b"hello, world!"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_returns_short_read_at_end_of_space() -> None:
    """Reading past the end returns what's available up to the
    boundary -- MemoryClient interprets a short reply as
    end-of-space."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CDI, b"abc")
        data = await client.read(tc_b.source_alias, SPACE_CDI, 1, 10)
        assert data == b"bc"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_write_updates_space_bytes() -> None:
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CONFIG, bytes(16))
        await client.write(
            tc_b.source_alias, SPACE_CONFIG, 4, b"WXYZ",
        )
        got = responder.get_space(SPACE_CONFIG)
        assert got == b"\x00\x00\x00\x00WXYZ" + bytes(8)
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_write_beyond_space_is_clipped_not_expanded() -> None:
    """Writes past the end are clipped rather than expanding the
    space -- the operator sizes each space explicitly, and silent
    growth is a footgun for peers reading known-size fields."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CONFIG, bytes(4))
        await client.write(
            tc_b.source_alias, SPACE_CONFIG, 2, b"ABCDEFGH",
        )
        # Space stays at 4 bytes; only 2 of the 8 written bytes
        # land (bytes 2..3), the rest are dropped.
        got = responder.get_space(SPACE_CONFIG)
        assert got == b"\x00\x00AB"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_read_unknown_space_returns_failure() -> None:
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        # No spaces declared -> any read fails with UNKNOWN_SPACE.
        with pytest.raises(MemoryError_) as exc:
            await client.read(tc_b.source_alias, SPACE_CDI, 0, 4)
        assert exc.value.error_code == FAIL_UNKNOWN_SPACE
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_write_to_read_only_space_fails() -> None:
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CDI, b"static", read_only=True)
        with pytest.raises(MemoryError_) as exc:
            await client.write(tc_b.source_alias, SPACE_CDI, 0, b"XXXX")
        assert exc.value.error_code == FAIL_READ_ONLY
        # And the space is unchanged.
        assert responder.get_space(SPACE_CDI) == b"static"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_read_out_of_bounds_returns_failure() -> None:
    """Address strictly past the end (not just count past the end)
    is a hard error rather than a short read."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CONFIG, bytes(4))
        with pytest.raises(MemoryError_) as exc:
            await client.read(tc_b.source_alias, SPACE_CONFIG, 100, 1)
        assert exc.value.error_code == FAIL_OUT_OF_BOUNDS
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_handles_explicit_space_form() -> None:
    """Spaces outside 0xFD..0xFF use explicit-space encoding
    (extra byte after the address). Round-trip a non-compact space."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(0x51, b"vendor-specific")
        data = await client.read(tc_b.source_alias, 0x51, 0, 15)
        assert data == b"vendor-specific"
        # And write in that space.
        await client.write(tc_b.source_alias, 0x51, 0, b"OVERWRITE!!!!!!")
        assert responder.get_space(0x51) == b"OVERWRITE!!!!!!"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_set_space_replace_hot_reloads_content() -> None:
    """Setting a space that already exists overwrites it wholesale
    -- hot-reload path for a CDI after operator saves changes."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CDI, b"first")
        assert await client.read(tc_b.source_alias, SPACE_CDI, 0, 5) == b"first"
        responder.set_space(SPACE_CDI, b"second-longer")
        assert await client.read(
            tc_b.source_alias, SPACE_CDI, 0, 13,
        ) == b"second-longer"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


# ---- lifecycle upcodes (S-9.10.1 sec.3.3) ----------------------------------


async def test_client_reboot_sends_correct_upcode() -> None:
    """MemoryClient.reboot() sends the exact protocol byte + command
    byte the spec calls for. Verified by intercepting the inbound
    datagram on the peer side."""
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    client = MemoryClient(eng_a)
    seen: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: seen.append(dg))
    try:
        await client.reboot(tc_b.source_alias)
        await asyncio.sleep(0.01)
        assert len(seen) == 1
        assert seen[0].data == bytes([MEMORY_CONFIG, CMD_REBOOT])
    finally:
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_client_factory_reset_carries_target_node_id() -> None:
    """Per S-9.10.1 sec.3.3, Factory Reset payload includes the target's
    6-byte NodeID. Passing a NodeId object round-trips its .bytes."""
    from jmri_openlcb import NodeId  # noqa: PLC0415
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    client = MemoryClient(eng_a)
    seen: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: seen.append(dg))
    try:
        nid = NodeId.parse("02.02.02.02.02.02")
        await client.factory_reset(tc_b.source_alias, nid)
        await asyncio.sleep(0.01)
        assert len(seen) == 1
        assert seen[0].data == (
            bytes([MEMORY_CONFIG, CMD_FACTORY_RESET]) + nid.bytes
        )
    finally:
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_client_freeze_and_unfreeze_carry_space_byte() -> None:
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    client = MemoryClient(eng_a)
    seen: list[Datagram] = []
    eng_b.inbound.connect(lambda dg: seen.append(dg))
    try:
        await client.freeze(tc_b.source_alias, SPACE_CDI)
        await client.unfreeze(tc_b.source_alias, SPACE_CDI)
        await asyncio.sleep(0.01)
        assert len(seen) == 2
        assert seen[0].data == bytes([MEMORY_CONFIG, CMD_FREEZE, SPACE_CDI])
        assert seen[1].data == bytes([MEMORY_CONFIG, CMD_UNFREEZE, SPACE_CDI])
    finally:
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_fires_on_reboot_callback() -> None:
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    client = MemoryClient(eng_a)
    called = asyncio.Event()

    async def _on_reboot() -> None:
        called.set()

    responder = MemoryConfigResponder(eng_b, on_reboot=_on_reboot)
    try:
        await client.reboot(tc_b.source_alias)
        await asyncio.wait_for(called.wait(), timeout=1.0)
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_factory_reset_matches_target_nid() -> None:
    """Factory reset with a matching NodeID fires the callback; a
    mismatched NodeID is silently ignored (safety guard per spec)."""
    from jmri_openlcb import NodeId  # noqa: PLC0415
    tc_a, tc_b, eng_a, eng_b = await _make_bus()
    client = MemoryClient(eng_a)
    our_nid = NodeId.parse("02.02.02.02.02.02")
    fired: list[bytes] = []

    async def _on_reset(payload: bytes) -> None:
        fired.append(payload)

    responder = MemoryConfigResponder(
        eng_b,
        on_factory_reset=_on_reset,
        our_node_id_bytes=our_nid.bytes,
    )
    try:
        # Correct NID -> fires.
        await client.factory_reset(tc_b.source_alias, our_nid)
        await asyncio.sleep(0.02)
        assert fired == [our_nid.bytes]
        # Wrong NID -> doesn't fire.
        other = NodeId.parse("03.03.03.03.03.03")
        await client.factory_reset(tc_b.source_alias, other)
        await asyncio.sleep(0.02)
        assert fired == [our_nid.bytes]   # unchanged
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_freeze_blocks_reads_and_writes() -> None:
    """Once frozen, reads and writes on the same space fail with
    the permanent-error family. Unfreezing lets them succeed again."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        responder.set_space(SPACE_CONFIG, b"initial-data")
        # Baseline: reads work.
        assert await client.read(
            tc_b.source_alias, SPACE_CONFIG, 0, 7,
        ) == b"initial"

        await client.freeze(tc_b.source_alias, SPACE_CONFIG)
        # Now the responder rejects reads on the frozen space.
        with pytest.raises(MemoryError_) as exc:
            await client.read(tc_b.source_alias, SPACE_CONFIG, 0, 7)
        assert exc.value.error_code == FAIL_PERMANENT
        # Writes are blocked too.
        with pytest.raises(MemoryError_):
            await client.write(
                tc_b.source_alias, SPACE_CONFIG, 0, b"XXX",
            )

        # Unfreeze restores access + data is intact.
        await client.unfreeze(tc_b.source_alias, SPACE_CONFIG)
        assert await client.read(
            tc_b.source_alias, SPACE_CONFIG, 0, 7,
        ) == b"initial"
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)


async def test_responder_is_frozen_query() -> None:
    """After freeze(), is_frozen() returns True; unfreeze clears
    the flag."""
    tc_a, tc_b, eng_a, eng_b, client, responder = await _bring_up_client_responder()
    try:
        assert responder.is_frozen(SPACE_CONFIG) is False
        await client.freeze(tc_b.source_alias, SPACE_CONFIG)
        await asyncio.sleep(0.02)
        assert responder.is_frozen(SPACE_CONFIG) is True
        await client.unfreeze(tc_b.source_alias, SPACE_CONFIG)
        await asyncio.sleep(0.02)
        assert responder.is_frozen(SPACE_CONFIG) is False
    finally:
        responder.close()
        await _teardown(client, eng_a, eng_b, tc_a, tc_b)
