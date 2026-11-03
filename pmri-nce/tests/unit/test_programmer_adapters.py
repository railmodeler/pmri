"""Tests for the Programmer-ABC adapters on top of NceProgrammer.

Ops-mode tests removed by the 2026-07-07 opcode audit -- see the note on
:mod:`pmri_nce.programmer` about the withdrawn ``NceOpsProgrammer``.
"""
from __future__ import annotations

import asyncio

import pytest
from pmri_core.beans.programmer import ProgrammingMode

from pmri_nce.opcodes import (
    ACK_OK,
    OP_RD_CV_DIRECT_MODE,
    OP_WR_CV_DIRECT_MODE,
)
from pmri_nce.programmer import (
    NceProgrammer,
    NceProgrammerManager,
    NceServiceProgrammer,
)
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import loopback_pair


async def _fake_cs(port, cv_state: dict[int, int]) -> None:  # noqa: ANN001
    async def run() -> None:
        while True:
            head = await port.read_exact(1)
            op = head[0]
            if op == OP_WR_CV_DIRECT_MODE:
                rest = await port.read_exact(3)
                cv = (rest[0] << 8) | rest[1]
                cv_state[cv] = rest[2]
                await port.write(ACK_OK)
            elif op == OP_RD_CV_DIRECT_MODE:
                rest = await port.read_exact(2)
                cv = (rest[0] << 8) | rest[1]
                await port.write(bytes([cv_state.get(cv, 0)]))
            else:
                await port.write(ACK_OK)
    asyncio.create_task(run())


async def test_service_programmer_satisfies_abc_mode() -> None:
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    inner = NceProgrammer(tc)
    service = NceServiceProgrammer(inner)
    assert service.mode == ProgrammingMode.SERVICE_DIRECT
    await tc.aclose()


async def test_service_read_and_write_via_abc() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    cv_state: dict[int, int] = {}
    await _fake_cs(b, cv_state)
    inner = NceProgrammer(tc)
    mgr = NceProgrammerManager(inner)
    service = mgr.global_programmer()

    await service.write_cv(29, 6)
    assert cv_state[29] == 6
    value = await service.read_cv(29)
    assert value == 6
    await tc.aclose()


async def test_ops_programmer_returns_bound_adapter() -> None:
    """Ops-mode support was re-added on the audited QUEUEn_TMP_CMD path
    (2026-07-14). ``ops_programmer(addr)`` now returns an adapter bound
    to that address rather than raising."""
    from pmri_core.beans.programmer import ProgrammingMode  # noqa: PLC0415
    from pmri_nce.programmer import NceOpsProgrammer  # noqa: PLC0415
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceProgrammerManager(NceProgrammer(tc))
    ops = mgr.ops_programmer(3, long_address=False)
    assert isinstance(ops, NceOpsProgrammer)
    assert ops.mode is ProgrammingMode.OPS_MODE
    assert ops.address == 3
    await tc.aclose()


async def test_global_programmer_is_a_singleton() -> None:
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    mgr = NceProgrammerManager(NceProgrammer(tc))
    a_prog = mgr.global_programmer()
    b_prog = mgr.global_programmer()
    assert a_prog is b_prog
    await tc.aclose()
