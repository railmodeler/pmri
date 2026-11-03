"""``attach_withrottle_server`` wires the TCP server into the lifespan."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from pmri_server.app import attach_withrottle_server, build_app


def test_withrottle_server_starts_in_lifespan() -> None:
    """When attached, the WiThrottle TCP server is up after lifespan
    enters and down after lifespan exits."""
    handle = build_app()
    attach_withrottle_server(handle, host="127.0.0.1", port=0)
    ws = handle.ctx.withrottle
    assert ws is not None
    # Not running before lifespan.
    assert ws.is_running is False
    with TestClient(handle.app):
        # Inside the context: server is running.
        assert ws.is_running is True
        # And the sockets list reflects an open listener.
        assert ws.sockets
    # After exit: stopped.
    assert ws.is_running is False


def test_no_withrottle_when_not_attached() -> None:
    """build_app alone doesn't start a WiThrottle server."""
    handle = build_app()
    assert getattr(handle.ctx, "withrottle", None) is None
    with TestClient(handle.app):
        pass  # Lifespan runs without WiThrottle, no errors.


async def test_attached_server_accepts_throttle_command(
    monkeypatch: object,  # noqa: ANN001
) -> None:
    """End-to-end: attach + use TestClient to enter the lifespan + connect."""
    handle = build_app()
    attach_withrottle_server(handle, host="127.0.0.1", port=0)
    ws = handle.ctx.withrottle

    with TestClient(handle.app):
        # Now the TCP server is up. Connect and acquire.
        host, port = ws.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        try:
            # Drain greeting (6) + RL0 (always sent now).
            for _ in range(7):
                await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"MT+S3<;>\n")
            await writer.drain()
            ack = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
            assert ack == "MT+S3<;>"
            assert handle.ctx.connection.throttles.get(3) is not None
        finally:
            writer.close()
            await writer.wait_closed()


async def test_turnout_action_from_withrottle_drives_the_bean() -> None:
    """A WiThrottle ``PTATIT1`` command must throw the actual IT1 turnout
    bean, and the resulting state change must broadcast back to the client."""
    handle = build_app()
    # Pre-create a turnout so the server has something to broadcast.
    await handle.ctx.connection.turnouts.provide("IT1")
    attach_withrottle_server(handle, host="127.0.0.1", port=0)
    ws = handle.ctx.withrottle

    with TestClient(handle.app):
        host, port = ws.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        try:
            # Drain greeting (6) + RL0 (always) + PTT + PTL.
            for _ in range(9):
                await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"PTATIT1\n")   # throw IT1
            await writer.drain()
            # Server broadcasts the resulting state change back to us.
            line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
            assert line == "PTA4IT1"   # 4 = THROWN
            # And the bean's known_state moved (DIRECT feedback mirrors immediately).
            from pmri_core.state import TurnoutState  # noqa: PLC0415
            bean = handle.ctx.connection.turnouts.get_by_system_name("IT1")
            assert bean is not None
            assert bean.known_state == TurnoutState.THROWN
        finally:
            writer.close()
            await writer.wait_closed()
