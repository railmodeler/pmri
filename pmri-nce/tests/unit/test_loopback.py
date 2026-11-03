from __future__ import annotations

import pytest

from pmri_nce.transport import loopback_pair


async def test_round_trip() -> None:
    a, b = loopback_pair()
    await a.write(b"hello")
    assert await b.read_exact(5) == b"hello"


async def test_aclose_blocks_writes() -> None:
    a, _ = loopback_pair()
    await a.aclose()
    assert not a.is_open
    with pytest.raises(RuntimeError, match="closed"):
        await a.write(b"x")
