"""WebSocket-specific tests: hello-on-connect, dispatch, and state-change push."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_ws_hello_on_connect(client: TestClient) -> None:
    with client.websocket_connect("/json") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hello"
        assert "JMRI" in msg["data"]


def test_ws_request_response_cycle(client: TestClient) -> None:
    with client.websocket_connect("/json") as ws:
        ws.receive_text()  # discard hello
        ws.send_text(json.dumps({"type": "ping", "id": 7}))
        resp = json.loads(ws.receive_text())
        assert resp["type"] == "ping"
        assert resp["id"] == 7
        assert resp["data"] == {}


def test_ws_invalid_json_returns_error(client: TestClient) -> None:
    with client.websocket_connect("/json") as ws:
        ws.receive_text()
        ws.send_text("not json{")
        resp = json.loads(ws.receive_text())
        assert resp["type"] == "error"
        assert resp["data"]["code"] == 400


def test_ws_unknown_topic_returns_error(client: TestClient) -> None:
    with client.websocket_connect("/json") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "no_such_topic", "method": "get"}))
        resp = json.loads(ws.receive_text())
        assert resp["type"] == "error"
        assert resp["data"]["code"] == 404


def test_ws_state_change_is_pushed(client: TestClient) -> None:
    """When a turnout's state changes, every WebSocket client receives an
    unsolicited push with the new data.

    We connect the WS *first* (so PushManager wires its listeners), then
    drive state changes purely via HTTP so the WS only sees push frames.
    """
    with client.websocket_connect("/json") as ws:
        ws.receive_text()  # discard hello

        # Create + drain the add-push via HTTP.
        client.post("/json/", json={
            "type": "turnout", "method": "put", "data": {"name": "IT500"},
        })
        add_push = json.loads(ws.receive_text())
        assert add_push["type"] == "turnout"
        assert add_push.get("method") == "put"
        assert add_push["data"]["name"] == "IT500"

        # Mutate state via HTTP. With DIRECT feedback the bean emits two
        # state_changed events (commanded then known) -- both produce a push.
        client.post("/json/", json={
            "type": "turnout", "method": "post",
            "data": {"name": "IT500", "state": 4},
        })
        first = json.loads(ws.receive_text())
        second = json.loads(ws.receive_text())
        # The final push must show the target state.
        assert first["type"] == "turnout"
        assert second["type"] == "turnout"
        assert second["data"]["state"] == 4
        assert second["data"]["name"] == "IT500"


def test_ws_command_station_power_change_is_pushed(client: TestClient) -> None:
    """When the driver flips ``_cs_reachable`` (via the reachability
    monitor detecting CS off/on), the PushManager broadcasts a
    ``commandStationPower`` frame to every connected client.

    Guards against the whole chain -- Signal wiring on the connection,
    PushManager subscription, WebSocket broadcast -- because a break at
    any link silently drops the badge update.
    """
    from pmri_server.app import build_app
    handle = build_app()
    conn = handle.ctx.connection
    with TestClient(handle.app) as tc, tc.websocket_connect("/json") as ws:
        ws.receive_text()   # discard hello

        # Simulate the driver's heartbeat monitor deciding the CS went dark.
        import asyncio
        asyncio.run(conn._set_cs_reachable(False))

        frame = json.loads(ws.receive_text())
        assert frame["type"] == "commandStationPower"
        assert frame["data"]["on"] is False

        # And back on.
        asyncio.run(conn._set_cs_reachable(True))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "commandStationPower"
        assert frame["data"]["on"] is True


def test_ws_throttle_state_change_is_pushed(client: TestClient) -> None:
    """When a throttle bean's speed or direction changes -- from ANY
    source, be it the /throttle page, the consist fan-out, WiThrottle,
    or a script -- every connected client must receive a
    ``{type: "throttle"}`` frame. Without this the /throttle page for
    a member loco never sees changes driven by the consist card, and
    the two pages look disconnected.
    """
    from pmri_server.app import build_app
    handle = build_app()
    with TestClient(handle.app) as tc, tc.websocket_connect("/json") as ws:
        ws.receive_text()  # discard hello

        # Acquire a throttle via HTTP so the push wiring runs through
        # the ``acquired`` signal path (the only path a real client
        # will follow).
        tc.post("/json/", json={
            "type": "throttle", "method": "put",
            "data": {"address": 42},
        })
        first = json.loads(ws.receive_text())
        assert first["type"] == "throttle"
        assert first["data"]["address"] == 42

        # Now mutate speed via HTTP -- must produce a fresh push frame.
        tc.post("/json/", json={
            "type": "throttle", "method": "post",
            "data": {"name": "42", "speed": 0.5},
        })
        second = json.loads(ws.receive_text())
        assert second["type"] == "throttle"
        assert second["data"]["speed"] == 0.5

        # And direction -- this is the specific flip the consist page's
        # Forward/Reverse fan-out needs to reach the /throttle page.
        tc.post("/json/", json={
            "type": "throttle", "method": "post",
            "data": {"name": "42", "forward": False},
        })
        third = json.loads(ws.receive_text())
        assert third["type"] == "throttle"
        assert third["data"]["forward"] is False


def test_ws_batched_requests(client: TestClient) -> None:
    """JMRI clients can send an array of envelopes in one frame."""
    with client.websocket_connect("/json") as ws:
        ws.receive_text()
        ws.send_text(json.dumps([
            {"type": "ping", "id": 1},
            {"type": "version", "id": 2},
        ]))
        a = json.loads(ws.receive_text())
        b = json.loads(ws.receive_text())
        ids = sorted([a.get("id"), b.get("id")])
        assert ids == [1, 2]
