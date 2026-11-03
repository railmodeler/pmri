from __future__ import annotations

import pytest

from pmri_loconet.transport import loopback_pair


async def test_loopback_round_trip() -> None:
    a, b = loopback_pair()
    await a.write_frame(b"\x83\x7c")
    # b should now be able to read those two bytes back.
    received = await b.read_exact(2)
    assert received == b"\x83\x7c"


async def test_loopback_both_directions() -> None:
    a, b = loopback_pair()
    await a.write_frame(b"\x01\x02")
    await b.write_frame(b"\x03\x04")
    assert await b.read_exact(2) == b"\x01\x02"
    assert await a.read_exact(2) == b"\x03\x04"


async def test_loopback_aclose_rejects_writes() -> None:
    a, _ = loopback_pair()
    await a.aclose()
    assert not a.is_open
    with pytest.raises(RuntimeError, match="closed"):
        await a.write_frame(b"\x83\x7c")
