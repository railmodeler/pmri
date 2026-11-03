"""Command-set auto-detection tests."""
from __future__ import annotations

import asyncio

from pmri_nce.command_set import NCE_PH_PRO_2006, NCE_POWERCAB, NCE_SB5
from pmri_nce.detect import (
    MODEL_POWERCAB,
    MODEL_POWERPRO,
    MODEL_SB5,
    detect_command_set,
    model_to_command_set,
)
from pmri_nce.opcodes import ACK_OK, OP_QUERY_SW_REV
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import loopback_pair


def test_model_to_command_set_mapping() -> None:
    assert model_to_command_set(MODEL_POWERCAB) is NCE_POWERCAB
    assert model_to_command_set(MODEL_SB5) is NCE_SB5
    assert model_to_command_set(MODEL_POWERPRO) is NCE_PH_PRO_2006
    assert model_to_command_set(0xFF) is NCE_PH_PRO_2006   # unknown -> fallback


async def _fake_version_cs(port, version: tuple[int, int, int]) -> None:  # noqa: ANN001
    async def run() -> None:
        while True:
            head = await port.read_exact(1)
            if head[0] == OP_QUERY_SW_REV:
                await port.write(bytes(version))
            else:
                await port.write(ACK_OK)
    asyncio.create_task(run())


async def test_detect_returns_powercab_for_powercab_byte() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    await _fake_version_cs(b, (1, 28, MODEL_POWERCAB))
    cs = await detect_command_set(tc)
    assert cs is NCE_POWERCAB
    await tc.aclose()


async def test_detect_returns_sb5_for_sb5_byte() -> None:
    a, b = loopback_pair()
    tc = NceTrafficController(a)
    await _fake_version_cs(b, (1, 5, MODEL_SB5))
    cs = await detect_command_set(tc)
    assert cs is NCE_SB5
    await tc.aclose()


async def test_detect_falls_back_on_timeout() -> None:
    """No CS responding -> fallback to PH-Pro."""
    a, _ = loopback_pair()
    tc = NceTrafficController(a)
    # Use a very short timeout by patching the command's timeout via the
    # higher-level path. detect_command_set respects send_command's default
    # timeout, which is 0.5s. We just assert the fallback is correct.
    cs = await detect_command_set(tc, fallback=NCE_PH_PRO_2006)
    assert cs is NCE_PH_PRO_2006
    await tc.aclose()
