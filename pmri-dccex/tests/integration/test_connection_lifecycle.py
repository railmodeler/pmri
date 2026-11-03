from __future__ import annotations

import pytest

from pmri_dccex import DccexConnection, loopback_pair


async def test_idempotent_close() -> None:
    a, _ = loopback_pair()
    conn = DccexConnection()
    await conn.open({"kind": "loopback", "loopback_port": a})
    await conn.close()
    await conn.close()  # second close must not raise


async def test_serial_settings_validation() -> None:
    conn = DccexConnection()
    with pytest.raises(ValueError, match="'port'"):
        await conn.open({"kind": "serial"})


async def test_tcp_settings_validation() -> None:
    conn = DccexConnection()
    with pytest.raises(ValueError, match="host"):
        await conn.open({"kind": "tcp"})


async def test_loopback_settings_validation() -> None:
    conn = DccexConnection()
    with pytest.raises(ValueError, match="loopback_port"):
        await conn.open({"kind": "loopback"})
