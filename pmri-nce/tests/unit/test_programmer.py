"""NCE CV programmer tests via fake CS.

Direct-mode paths hit ``[OP_WR_CV_DIRECT_MODE, ...]`` / ``[OP_RD_CV_DIRECT_MODE, ...]``.
Ops-mode POM uses ``QUEUEn_TMP_CMD`` (opcodes 0x93..0x96 depending on
DCC packet length) -- see :func:`pmri_nce.messages.ops_mode_write_cv` for
the packet layout. The fake CS below decodes both.
"""
from __future__ import annotations

import asyncio

import pytest
from pmri_nce.opcodes import (
    ACK_OK,
    OP_ENTER_PROG_MODE,
    OP_EXIT_PROG_MODE,
    OP_QUEUE_PACKET_BASE,
    OP_RD_CV_DIRECT_MODE,
    OP_WR_CV_DIRECT_MODE,
)
from pmri_nce.programmer import (
    NceOpsProgrammer,
    NceProgrammer,
    NceProgrammerManager,
    NceServiceProgrammer,
)
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import loopback_pair


async def _fake_cs(
    port, cv_values: dict[int, int],   # noqa: ANN001
    ops_packets: list[bytes] | None = None,
) -> None:
    """Tiny CS decoding direct-mode CV opcodes + ops-mode packet queue.

    * ``0xA8/0xA9`` = direct-mode write/read (mutates *cv_values*).
    * ``0x93..0x96`` = ops-mode QUEUEn_TMP_CMD -- the CS relays a raw
      DCC packet of length ``opcode - 0x93 + 3``. We just capture the
      packet body into *ops_packets* so tests can assert on the wire
      shape without having to model a fake DCC decoder loco.
    """
    async def run() -> None:
        while True:
            head = await port.read_exact(1)
            op = head[0]
            if op == OP_ENTER_PROG_MODE or op == OP_EXIT_PROG_MODE:
                # Programming-mode state transitions: single-byte
                # request, single ACK reply. Real NCE PowerPro firmware
                # requires ENTER_PROG before direct-mode CV ops.
                await port.write(ACK_OK)
            elif op == OP_WR_CV_DIRECT_MODE:
                rest = await port.read_exact(3)
                cv = (rest[0] << 8) | rest[1]
                cv_values[cv] = rest[2]
                await port.write(ACK_OK)
            elif op == OP_RD_CV_DIRECT_MODE:
                rest = await port.read_exact(2)
                cv = (rest[0] << 8) | rest[1]
                value = cv_values.get(cv, 0)
                await port.write(bytes([value]))
            elif 0x93 <= op <= 0x96:
                # Wire form: [opcode, send_count, ...packet...]
                packet_len = op - OP_QUEUE_PACKET_BASE + 3
                _send_count = (await port.read_exact(1))[0]  # noqa: F841
                packet = await port.read_exact(packet_len)
                if ops_packets is not None:
                    ops_packets.append(bytes(packet))
                await port.write(ACK_OK)
            else:
                await port.write(ACK_OK)

    asyncio.create_task(run())


async def test_direct_mode_write_and_read() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    cv_state: dict[int, int] = {}
    await _fake_cs(b, cv_state)
    prog = NceProgrammer(tc)

    await prog.write_cv_direct(cv=1, value=42)
    assert cv_state[1] == 42
    value = await prog.read_cv_direct(cv=1)
    assert value == 42
    await tc.aclose()


async def test_programming_in_progress_flag() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    cv_state: dict[int, int] = {}
    await _fake_cs(b, cv_state)
    prog = NceProgrammer(tc)

    assert not prog.is_programming
    # Run direct-mode write, check flag transitions correctly.
    fut = asyncio.create_task(prog.write_cv_direct(cv=1, value=99))
    # Give the task a tick to set the flag before the fake CS replies.
    await asyncio.sleep(0)
    # The flag may or may not be set at this instant depending on scheduling;
    # we mainly want to assert that after the call completes it's clear.
    await fut
    assert not prog.is_programming
    await tc.aclose()


async def test_direct_mode_op_wraps_with_enter_and_exit_prog() -> None:
    """NCE PowerPro requires ENTER_PROG (0x9E) before direct-mode CV
    ops and EXIT_PROG (0x9F) after -- outside programming mode the CS
    NoAcks the CV command immediately (~ms, not the 2-6 s of a real
    programming attempt). Regression guard: capture every opcode the
    CS sees during a single ``write_cv_direct`` and confirm the
    sequence is ENTER -> WRITE -> EXIT."""
    seen_opcodes: list[int] = []
    a, b = loopback_pair()
    tc = NceTrafficController(a)

    async def run() -> None:
        while True:
            head = await b.read_exact(1)
            op = head[0]
            seen_opcodes.append(op)
            if op == OP_ENTER_PROG_MODE or op == OP_EXIT_PROG_MODE:
                await b.write(ACK_OK)
            elif op == OP_WR_CV_DIRECT_MODE:
                await b.read_exact(3)
                await b.write(ACK_OK)
            elif op == OP_RD_CV_DIRECT_MODE:
                await b.read_exact(2)
                await b.write(bytes([42]))
            else:
                await b.write(ACK_OK)
    asyncio.create_task(run())

    prog = NceProgrammer(tc)
    await prog.write_cv_direct(cv=1, value=3)
    assert seen_opcodes == [OP_ENTER_PROG_MODE, OP_WR_CV_DIRECT_MODE, OP_EXIT_PROG_MODE]
    # Same for reads.
    seen_opcodes.clear()
    value = await prog.read_cv_direct(cv=1)
    assert value == 42
    assert seen_opcodes == [OP_ENTER_PROG_MODE, OP_RD_CV_DIRECT_MODE, OP_EXIT_PROG_MODE]
    await tc.aclose()


async def test_direct_mode_read_still_exits_prog_on_noack() -> None:
    """When the decoder NoAcks a read (byte ``'0'``), we still send
    EXIT_PROG so the CS returns to normal operation. If we skipped
    EXIT_PROG after failures, the CS would stay in programming mode
    and layout traffic would remain paused until the next successful
    op -- a UX cliff we want to avoid."""
    from pmri_nce.messages import NceCommandError  # noqa: PLC0415
    seen_opcodes: list[int] = []
    a, b = loopback_pair()
    tc = NceTrafficController(a)

    async def run() -> None:
        while True:
            head = await b.read_exact(1)
            op = head[0]
            seen_opcodes.append(op)
            if op == OP_ENTER_PROG_MODE or op == OP_EXIT_PROG_MODE:
                await b.write(ACK_OK)
            elif op == OP_RD_CV_DIRECT_MODE:
                await b.read_exact(2)
                await b.write(b"0")   # NoAck
            else:
                await b.write(ACK_OK)
    asyncio.create_task(run())

    prog = NceProgrammer(tc)
    with pytest.raises(NceCommandError):
        await prog.read_cv_direct(cv=1)
    # EXIT_PROG must appear in the trace even though the read failed.
    assert OP_EXIT_PROG_MODE in seen_opcodes
    assert seen_opcodes.index(OP_EXIT_PROG_MODE) > seen_opcodes.index(OP_RD_CV_DIRECT_MODE)
    await tc.aclose()


async def test_ops_mode_write_ships_dcc_packet_via_queue_cmd() -> None:
    """Short-address POM write puts a 5-byte NMRA CV Access packet on the
    wire wrapped in QUEUE5_TMP_CMD. Verify the CS sees the right bytes."""
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    ops_packets: list[bytes] = []
    await _fake_cs(b, {}, ops_packets)
    prog = NceProgrammer(tc)

    await prog.write_cv_ops(loco_address=3, cv=1, value=42)
    # 5-byte NMRA packet: [addr, 0xEC, 0x00, value, XOR].
    body = ops_packets[0]
    assert body[0] == 0x03           # short address 3
    assert body[1] == 0xEC           # CV Access byte-write, cv_index=0
    assert body[2] == 0x00           # cv_lo
    assert body[3] == 42             # value
    # XOR error-detection byte covers all preceding bytes.
    xor = 0
    for x in body[:-1]:
        xor ^= x
    assert body[-1] == xor
    await tc.aclose()


async def test_ops_mode_write_long_address_uses_6_byte_packet() -> None:
    """Long address 2065 -> prefix 0xC8 0x11 in the DCC packet; wrapped in
    QUEUE6_TMP_CMD (opcode 0x96)."""
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    ops_packets: list[bytes] = []
    await _fake_cs(b, {}, ops_packets)
    prog = NceProgrammer(tc)

    await prog.write_cv_ops(
        loco_address=2065, cv=29, value=34, long_address=True,
    )
    body = ops_packets[0]
    assert len(body) == 6
    assert body[0:2] == bytes([0xC8, 0x11])   # long-address prefix
    assert body[2] == 0xEC                    # CV Access byte-write
    assert body[3] == 28                      # cv_index for CV29
    assert body[4] == 34                      # value
    await tc.aclose()


def test_manager_returns_service_and_ops_programmers() -> None:
    """``NceProgrammerManager`` hands out the right adapters:

    * ``global_programmer()`` -> :class:`NceServiceProgrammer` singleton.
    * ``ops_programmer(addr)`` -> fresh :class:`NceOpsProgrammer` bound to
      the address, mode = OPS_MODE. No wire traffic yet.
    """
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    inner = NceProgrammer(tc)
    mgr = NceProgrammerManager(inner)

    service = mgr.global_programmer()
    assert isinstance(service, NceServiceProgrammer)
    assert service is mgr.global_programmer()   # singleton

    ops = mgr.ops_programmer(3, long_address=False)
    assert isinstance(ops, NceOpsProgrammer)
    assert ops.address == 3
    assert ops.long_address is False


async def test_ops_programmer_write_hits_wire() -> None:
    """Round-trip through the manager: ops adapter's ``write_cv`` produces
    the QUEUE5_TMP_CMD wire form on the far side of the loopback."""
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    ops_packets: list[bytes] = []
    await _fake_cs(b, {}, ops_packets)
    mgr = NceProgrammerManager(NceProgrammer(tc))

    ops = mgr.ops_programmer(3)
    await ops.write_cv(1, 42)
    assert len(ops_packets) == 1
    assert ops_packets[0][0] == 0x03          # short address matches
    assert ops_packets[0][3] == 42            # value
    await tc.aclose()


async def test_ops_programmer_read_raises() -> None:
    """PowerPro has no ops-mode CV read (no RailCom decode). The adapter
    surfaces that as a clear error rather than a mystery timeout."""
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceProgrammerManager(NceProgrammer(tc))

    ops = mgr.ops_programmer(3)
    with pytest.raises(NotImplementedError):
        await ops.read_cv(1)
    await tc.aclose()


async def test_direct_mode_read_treats_reply_byte_0x30_as_no_ack() -> None:
    """NCE PowerPro sends ``0x30`` (`'0'`) when a direct-mode read has no
    decoder to answer. Old code returned that as "CV value = 48" -- every
    read pinning the same wrong answer. Matches JMRI's ``NceProgrammer``
    where reply byte ``'0'`` maps to ``NoAck``."""
    from pmri_nce.messages import NceCommandError  # noqa: PLC0415
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    # CS answers every read with 0x30 (the NoAck sentinel).
    async def cs_answers_no_ack() -> None:
        while True:
            head = await b.read_exact(1)
            if head[0] == OP_RD_CV_DIRECT_MODE:
                await b.read_exact(2)
                await b.write(b"0")
            else:
                await b.write(ACK_OK)
    asyncio.create_task(cs_answers_no_ack())
    prog = NceProgrammer(tc)
    with pytest.raises(NceCommandError):
        await prog.read_cv_direct(cv=1)
    assert not prog.is_programming
    await tc.aclose()


async def test_direct_mode_read_error_raises() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    # Real CS behaviour when no decoder is on the program track: the read
    # request is acknowledged but no data byte ever comes back. We model
    # that by accepting + discarding the request and never replying.
    async def cs_silent_on_read() -> None:
        while True:
            head = await b.read_exact(1)
            if head[0] == OP_RD_CV_DIRECT_MODE:
                await b.read_exact(2)
                # Intentionally no write -- the read should time out.
            else:
                await b.write(ACK_OK)
    asyncio.create_task(cs_silent_on_read())
    prog = NceProgrammer(tc)
    with pytest.raises(TimeoutError):
        await prog.read_cv_direct(cv=1)
    assert not prog.is_programming
    await tc.aclose()
