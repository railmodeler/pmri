"""FirmwareUpgradeClient orchestration -- end-to-end loopback tests.

Runs the full CT-side flow (Freeze -> Init Complete -> PIP probe ->
Data Transfer -> Unfreeze) against a fake target. The fake target
uses the existing MemoryConfigResponder for its Firmware Space
storage and manually emits the Init Complete PCER when it sees a
Freeze command land on its datagram engine.
"""
from __future__ import annotations

import asyncio

import pytest

from pmri_openlcb import (
    EVENT_INIT_COMPLETE_SIMPLE,
    SPACE_FIRMWARE,
    DatagramEngine,
    FirmwareUpgradeClient,
    FirmwareUpgradeError,
    MemoryClient,
    MemoryConfigResponder,
    OlcbTrafficController,
    PipClient,
    PipService,
    ProtocolFlag,
    StreamClient,
    StreamResponder,
    loopback_pair,
)


async def _bus():
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
        try:
            close_fn = getattr(thing, "close", None)
            if close_fn is not None and asyncio.iscoroutinefunction(close_fn):
                await close_fn()
            elif close_fn is not None:
                close_fn()
        except Exception:
            pass
    for thing in things:
        if isinstance(thing, DatagramEngine):
            await thing.detach()
    for thing in things:
        if isinstance(thing, OlcbTrafficController):
            await thing.aclose()


async def _wire_init_complete_on_freeze(
    tc_target: OlcbTrafficController,
    engine_target: DatagramEngine,
    responder: MemoryConfigResponder,
) -> None:
    """Emit a Simple Init Complete PCER whenever the target sees a
    Freeze(SPACE_FIRMWARE) datagram -- mimics a real firmware-upgrade
    node's behavior per S-9.7.4.3 sec.5.2 (state transition ACK). Also
    clears the responder's per-space freeze flag: for Firmware Space
    the spec's Freeze means "enter upgrade mode + accept writes", not
    "pause I/O" (which is the responder's default behavior)."""

    async def _on_dg(dg) -> None:  # noqa: ANN001
        if (
            len(dg.data) >= 3
            and dg.data[0] == 0x20    # MEMORY_CONFIG
            and dg.data[1] == 0xA1    # CMD_FREEZE
            and dg.data[2] == SPACE_FIRMWARE
        ):
            # The responder already added SPACE_FIRMWARE to _frozen;
            # a real firmware-mode target keeps it writable, so undo
            # the block.
            responder._frozen.discard(SPACE_FIRMWARE)  # noqa: SLF001
            await tc_target.produce_event(EVENT_INIT_COMPLETE_SIMPLE)

    engine_target.inbound.connect(_on_dg)


# ---- datagram transfer path ---------------------------------------------


async def test_datagram_path_writes_firmware_and_unfreezes() -> None:
    """End-to-end: CT freezes, awaits Init Complete, skips stream
    (no PIP responder -> falls back to datagram), writes firmware
    bytes in 64-byte chunks, unfreezes. The target's Firmware Space
    ends up with the exact bytes."""
    tc_a, tc_b, eng_a, eng_b = await _bus()
    responder = MemoryConfigResponder(eng_b)
    # Firmware Space must exist as writable on the target.
    responder.set_space(SPACE_FIRMWARE, bytes(4096))
    await _wire_init_complete_on_freeze(tc_b, eng_b, responder)

    memory = MemoryClient(eng_a)
    pip = PipClient(tc_a)
    fw = FirmwareUpgradeClient(memory, pip, tc_a)
    try:
        # 150 bytes -> 3 chunks (64 + 64 + 22).
        firmware = bytes(range(150))
        progress: list[tuple[int, int]] = []
        await fw.upgrade(
            0x222, firmware,
            progress=lambda sent, total: progress.append((sent, total)),
            pip_timeout=0.1,     # short -- no PIP responder to answer
        )
        # Data landed in the target's Firmware Space verbatim.
        got = responder.get_space(SPACE_FIRMWARE)
        assert got is not None
        assert got[:150] == firmware
        # Progress fired for each on-wire datagram.
        assert progress
        assert progress[-1] == (150, 150)
    finally:
        memory.close()
        pip.close()
        responder.close()
        await _teardown(eng_a, eng_b, tc_a, tc_b)


async def test_datagram_path_uses_exactly_64_byte_chunks() -> None:
    """sec.5.4.2 requires exactly 64 bytes per datagram except the last.
    Verified by watching the actual write payloads landing on the
    target's datagram engine."""
    tc_a, tc_b, eng_a, eng_b = await _bus()
    responder = MemoryConfigResponder(eng_b)
    responder.set_space(SPACE_FIRMWARE, bytes(2048))
    await _wire_init_complete_on_freeze(tc_b, eng_b, responder)

    # Intercept write datagrams to count payload sizes.
    write_payload_sizes: list[int] = []

    async def _watch(dg) -> None:  # noqa: ANN001
        # Memory-config write to SPACE_FIRMWARE: 0x20 <cmd> <addr:4>
        # <space> <payload...> -- generic form for 0xEF.
        if (
            len(dg.data) >= 7
            and dg.data[0] == 0x20
            and (dg.data[1] & 0xF0) == 0x00   # write family
            and dg.data[6] == SPACE_FIRMWARE
        ):
            write_payload_sizes.append(len(dg.data) - 7)

    eng_b.inbound.connect(_watch)

    memory = MemoryClient(eng_a)
    pip = PipClient(tc_a)
    fw = FirmwareUpgradeClient(memory, pip, tc_a)
    try:
        # 200 bytes -> 64, 64, 64, 8 = four datagrams. Last chunk is 8.
        firmware = bytes(range(200))
        await fw.upgrade(0x222, firmware, pip_timeout=0.1)
        assert write_payload_sizes == [64, 64, 64, 8]
    finally:
        memory.close()
        pip.close()
        responder.close()
        await _teardown(eng_a, eng_b, tc_a, tc_b)


async def test_upgrade_fails_when_target_never_initializes() -> None:
    """If the target doesn't emit Init Complete after Freeze, the
    client raises FirmwareUpgradeError rather than hanging forever."""
    tc_a, tc_b, eng_a, eng_b = await _bus()
    # No responder wiring -> the target never emits Init Complete.
    memory = MemoryClient(eng_a)
    pip = PipClient(tc_a)
    fw = FirmwareUpgradeClient(memory, pip, tc_a)
    try:
        with pytest.raises(
            FirmwareUpgradeError, match="Node Initialization Complete",
        ):
            await fw.upgrade(
                0x222, b"firmware",
                init_complete_timeout=0.1,
                pip_timeout=0.1,
            )
    finally:
        memory.close()
        pip.close()
        await _teardown(eng_a, eng_b, tc_a, tc_b)


async def test_upgrade_rejects_empty_firmware() -> None:
    tc_a, _, eng_a, _ = await _bus()
    memory = MemoryClient(eng_a)
    pip = PipClient(tc_a)
    fw = FirmwareUpgradeClient(memory, pip, tc_a)
    try:
        with pytest.raises(ValueError, match="empty"):
            await fw.upgrade(0x222, b"")
    finally:
        memory.close()
        pip.close()
        await _teardown(eng_a, tc_a)


# ---- stream transfer path ------------------------------------------------


async def test_stream_path_used_when_target_advertises_stream() -> None:
    """sec.5.4.1: if the target's PIP reply sets the Stream Protocol
    bit, we use Stream Write instead of chunked datagram writes.
    Verified by watching that no memory-config write datagrams for
    SPACE_FIRMWARE land at the target (all data goes via the stream
    frame kind instead)."""
    tc_a, tc_b, eng_a, eng_b = await _bus()
    responder = MemoryConfigResponder(eng_b)
    responder.set_space(SPACE_FIRMWARE, bytes(4096))
    await _wire_init_complete_on_freeze(tc_b, eng_b, responder)

    # Target advertises Stream + Memory Config + Firmware Upgrade.
    pip_service = PipService(
        tc_b,
        ProtocolFlag.STREAM
        | ProtocolFlag.MEMORY_CONFIG
        | ProtocolFlag.FIRMWARE_UPGRADE_ACTIVE,
    )
    received_streams: list[tuple[int, bytes]] = []
    stream_responder = StreamResponder(
        tc_b, buffer_size=1024,
        on_stream=lambda alias, data: received_streams.append((alias, data)),
    )

    # No memory-config write should ever land at the target if the
    # Stream path activates.
    seen_writes: list[bytes] = []

    async def _watch(dg) -> None:  # noqa: ANN001
        if (
            len(dg.data) >= 7
            and dg.data[0] == 0x20
            and (dg.data[1] & 0xF0) == 0x00
            and dg.data[6] == SPACE_FIRMWARE
        ):
            seen_writes.append(bytes(dg.data))

    eng_b.inbound.connect(_watch)

    memory = MemoryClient(eng_a)
    pip = PipClient(tc_a)
    stream = StreamClient(tc_a)
    fw = FirmwareUpgradeClient(memory, pip, tc_a, stream=stream)
    try:
        firmware = bytes(range(256)) * 2   # 512 bytes
        await fw.upgrade(0x222, firmware)
        # The stream landed at the target with the exact bytes.
        assert received_streams == [(0x111, firmware)]
        # No memory-config write datagrams to Firmware Space.
        assert seen_writes == []
    finally:
        stream.close()
        stream_responder.close()
        pip_service.close()
        memory.close()
        pip.close()
        responder.close()
        await _teardown(eng_a, eng_b, tc_a, tc_b)


# ---- constants + module surface -----------------------------------------


def test_space_firmware_matches_spec() -> None:
    assert SPACE_FIRMWARE == 0xEF


def test_error_codes_match_spec() -> None:
    from pmri_openlcb import (
        ERROR_INCOMPATIBLE,
        ERROR_INVALID,
        ERROR_TRANSFER_CHECKSUM,
    )
    # S-9.7.4.3 sec.6.2 exact values.
    assert ERROR_TRANSFER_CHECKSUM == 0x2088
    assert ERROR_INCOMPATIBLE == 0x1088
    assert ERROR_INVALID == 0x1089


def test_pip_flags_include_firmware_upgrade_bits() -> None:
    # S-9.7.4.3 sec.5.3 exact values.
    assert ProtocolFlag.FIRMWARE_UPGRADE_PROTOCOL.value == 0x000020
    assert ProtocolFlag.FIRMWARE_UPGRADE_ACTIVE.value == 0x000010


def test_well_known_firmware_events_registered() -> None:
    from pmri_openlcb import (
        EVENT_FIRMWARE_CORRUPTED,
        EVENT_FIRMWARE_UPGRADE_REQUESTED,
        WELL_KNOWN_EVENTS,
    )
    assert WELL_KNOWN_EVENTS[EVENT_FIRMWARE_CORRUPTED] == "Firmware Corrupted"
    assert "Hardware Switch" in WELL_KNOWN_EVENTS[EVENT_FIRMWARE_UPGRADE_REQUESTED]
