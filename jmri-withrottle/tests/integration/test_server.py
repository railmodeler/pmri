"""End-to-end WiThrottle TCP server tests."""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.hal.internal import InternalConnection

from jmri_withrottle.server import WiThrottleServer


async def _read_lines(reader: asyncio.StreamReader, n: int) -> list[str]:
    """Read *n* newline-terminated lines, decode as UTF-8."""
    out: list[str] = []
    for _ in range(n):
        raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
        out.append(raw.decode("utf-8").rstrip("\r\n"))
    return out


async def _connect(server: WiThrottleServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]
    return await asyncio.open_connection(host, port)


@pytest.fixture()
async def server() -> WiThrottleServer:
    conn = InternalConnection()
    await conn.open()
    s = WiThrottleServer(connection=conn, railroad="TestRR")
    # Port 0 = pick any free port.
    await s.start(host="127.0.0.1", port=0)
    yield s
    await s.stop()
    await conn.close()


async def test_server_sends_greeting_on_connect(server: WiThrottleServer) -> None:
    reader, writer = await _connect(server)
    try:
        lines = await _read_lines(reader, 6)
        assert any(l.startswith("VN") for l in lines)
        assert any("TestRR" in l for l in lines)
        assert any(l.startswith("*") for l in lines)
    finally:
        writer.close()
        await writer.wait_closed()


# After acquire, the server broadcasts the throttle's initial state so
# Engine Driver / WiThrottle iOS know how to render the throttle UI:
#   1 ack + 1 labels + 29 function states + 1 speed + 1 direction + 1 speed-step
# The labels line is always sent with non-empty defaults (``F0``..``F28``) --
# empty labels made Engine Driver hide the function bar for manual
# acquires; a missing line made it hide the bar for roster acquires.
_LINES_AFTER_ACQUIRE = 1 + 1 + 29 + 1 + 1 + 1


async def test_acquire_release_round_trip(server: WiThrottleServer) -> None:
    reader, writer = await _connect(server)
    try:
        # Drain greeting + RL0.
        await _read_lines(reader, 7)
        # Acquire short address 3.
        writer.write(b"MT+S3<;>label\n")
        await writer.drain()
        lines = await _read_lines(reader, _LINES_AFTER_ACQUIRE)
        # First line is the acquire ack.
        assert lines[0] == "MT+S3<;>"
        # Then the labels line with cosmetic defaults matching Engine
        # Driver (Light/Bell/Horn for F0..F2).
        assert lines[1].startswith("MTLS3<;>]\\[Light]\\[Bell]\\[Horn")
        # Then 29 per-function state lines, all off, in order F0..F28.
        assert lines[2] == "MTAS3<;>F00"
        assert lines[30] == "MTAS3<;>F028"
        # Then speed, direction, speed-step mode.
        assert lines[31] == "MTAS3<;>V0"
        assert lines[32] == "MTAS3<;>R1"
        assert lines[33] == "MTAS3<;>s1"
        # Release.
        writer.write(b"MT-S3<;>\n")
        await writer.drain()
        rel = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert rel == "MT-S3<;>"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_speed_and_function_round_trip(server: WiThrottleServer) -> None:
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 7)  # greeting + RL0
        writer.write(b"MT+L3601<;>\n")
        await writer.drain()
        await _read_lines(reader, _LINES_AFTER_ACQUIRE)   # ack + initial state

        writer.write(b"MTAL3601<;>V64\n")
        await writer.drain()
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line == "MTAL3601<;>V64"

        writer.write(b"MTAL3601<;>F18\n")  # F8 press
        await writer.drain()
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line == "MTAL3601<;>F18"

        writer.write(b"MTAL3601<;>X\n")    # e-stop
        await writer.drain()
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line == "MTAL3601<;>V1"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_roster_provider_emits_RL(server: WiThrottleServer) -> None:
    # Inject a roster provider.
    server.roster_provider = lambda: [
        {"id": "UP_3601", "dccAddress": "3601", "longAddress": True},
    ]
    reader, writer = await _connect(server)
    try:
        # Greeting is 6 lines; roster is the next one.
        await _read_lines(reader, 6)
        rl = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert rl.startswith("RL1")
        assert "UP_3601" in rl
    finally:
        writer.close()
        await writer.wait_closed()


async def test_empty_roster_still_emits_RL0(server: WiThrottleServer) -> None:
    """JMRI Java always sends RL, even for an empty roster. Engine
    Driver treats "no RL received" differently from "RL0 received":
    the former leaves the roster tab in an ambiguous "not loaded"
    state, the latter shows a definitive empty roster. Regression
    pin -- earlier versions skipped RL entirely when roster was empty
    and Engine Driver showed "no locomotive roster available."""
    server.roster_provider = lambda: []
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 6)
        rl = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert rl == "RL0"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_broadcast_roster_pushes_to_connected_sessions(
    server: WiThrottleServer,
) -> None:
    """A roster change (add/remove) broadcasts a fresh RL to every
    connected client so Engine Driver reflects it without reconnect."""
    entries: list[dict] = []
    server.roster_provider = lambda: list(entries)
    reader, writer = await _connect(server)
    try:
        # Drain greeting + initial RL0.
        await _read_lines(reader, 7)
        # Simulate a roster mutation on the server side.
        entries.append({"id": "NEW_5", "dccAddress": "5", "longAddress": False})
        await server.broadcast_roster()
        rl = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert rl.startswith("RL1")
        assert "NEW_5" in rl
    finally:
        writer.close()
        await writer.wait_closed()


async def test_quit_closes_connection(server: WiThrottleServer) -> None:
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 7)   # greeting + RL0
        writer.write(b"Q\n")
        await writer.drain()
        # After Q the server closes; readline returns EOF.
        eof = await asyncio.wait_for(reader.readline(), timeout=2.0)
        assert eof == b""
    finally:
        writer.close()
        await writer.wait_closed()


async def test_periodic_pft_broadcast(server: WiThrottleServer) -> None:
    """Every ~15 s (default) the server pushes a fresh PFT to each client.

    Engine Driver / iOS extrapolate their clock locally from the last
    sync -- this broadcast keeps them aligned when the server-side clock
    state changes (start/stop/rate/big-jump) between the client's own
    heartbeats.
    """
    # Speed up the broadcast so the test doesn't wait 15s.
    server.fast_clock_broadcast_s = 0.05
    server.fast_clock_provider = lambda: "PFT1700000000<;>4.0"
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 7)   # greeting + RL0
        # Wait for at least one broadcast to arrive.
        found = False
        for _ in range(20):
            line = (await asyncio.wait_for(reader.readline(), timeout=1.0)).decode().rstrip()
            if line == "PFT1700000000<;>4.0":
                found = True
                break
        assert found, "expected a PFT broadcast within the observation window"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_turnout_list_and_broadcast(server: WiThrottleServer) -> None:
    """Server sends PTT + PTL on connect and can broadcast state changes
    to every connected client via ``broadcast_turnout_state``."""
    server.turnout_provider = lambda: [
        {"system_name": "NT1", "user_name": "West Yard", "state": 2},
        {"system_name": "NT2", "user_name": "", "state": 4},
    ]
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 7)   # greeting + RL0

        # PTT + PTL come next.
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line.startswith("PTT")
        assert "Closed}|{2" in line
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line.startswith("PTL")
        assert "NT1}|{West Yard}|{2" in line
        assert "NT2}|{}|{4" in line

        # Now trigger a state broadcast -- every session receives it.
        await server.broadcast_turnout_state("NT1", 4)
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line == "PTA4NT1"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_route_list_and_fire_broadcast(server: WiThrottleServer) -> None:
    """PRT + PRL on connect; PRA from the client triggers the
    route_action_handler + a PRA ACTIVE echoed to every session."""
    server.route_provider = lambda: [
        {"system_name": "IO1", "user_name": "Yard", "state": 0},
        {"system_name": "IO2", "user_name": "", "state": 0},
    ]
    calls: list[tuple[str, str]] = []

    async def handler(sysname: str, action: str) -> None:
        calls.append((sysname, action))
        # Also broadcast the active state so the client sees its
        # fire reflected -- JMRI RouteController does the same.
        await server.broadcast_route_state(sysname, 2)

    server.route_action_handler = handler

    reader, writer = await _connect(server)
    try:
        # Drain greeting (6) + RL0 (always).
        await _read_lines(reader, 7)
        # Routes title + list come next.
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line.startswith("PRT")
        assert "Active}|{2" in line
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line.startswith("PRL")
        assert "IO1}|{Yard}|{0" in line
        assert "IO2}|{}|{0" in line

        # Client fires IO1.
        writer.write(b"PRA2IO1\n")
        await writer.drain()
        line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().rstrip()
        assert line == "PRA2IO1"
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.05)
        assert calls == [("IO1", "2")]
    finally:
        writer.close()
        await writer.wait_closed()


async def test_turnout_action_from_client_reaches_handler(server: WiThrottleServer) -> None:
    """Client's ``PTATNT1`` (throw NT1) dispatches to the server's
    turnout_action_handler."""
    calls: list[tuple[str, str]] = []

    async def handler(sys_name: str, action: str) -> None:
        calls.append((sys_name, action))

    server.turnout_action_handler = handler
    # Even with a handler set, if the provider returns no turnouts we
    # skip PTT + PTL -- check the handler dispatch path independently.
    server.turnout_provider = lambda: [
        {"system_name": "NT1", "user_name": "", "state": 2},
    ]
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 7)   # greeting + RL0
        # Consume PTT + PTL.
        await asyncio.wait_for(reader.readline(), timeout=2.0)
        await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.write(b"PTATNT1\n")
        await writer.drain()
        # Give the server a tick to dispatch.
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.05)
        assert calls == [("NT1", "T")]
    finally:
        writer.close()
        await writer.wait_closed()


async def test_disconnect_releases_throttles(server: WiThrottleServer) -> None:
    reader, writer = await _connect(server)
    try:
        await _read_lines(reader, 7)   # greeting + RL0
        writer.write(b"MT+S3<;>\n")
        await writer.drain()
        await _read_lines(reader, _LINES_AFTER_ACQUIRE)
    finally:
        writer.close()
        await writer.wait_closed()
    # Give the server a moment to react to the FIN.
    await asyncio.sleep(0.1)
    assert server.connection.throttles.get(3) is None
