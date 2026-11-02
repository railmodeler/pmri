"""WebSocket bridge -- exposes the same /json endpoint jmri-server does."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_ws_hello_on_connect(client: TestClient) -> None:
    with client.websocket_connect("/json") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hello"


def test_ws_state_change_pushes_through_to_web_clients(client: TestClient) -> None:
    """A turnout created over HTTP must produce a push on every WS client."""
    with client.websocket_connect("/json") as ws:
        ws.receive_text()  # hello

        # Create a turnout via the web's HTTP action -- that goes through the
        # same registry that fires push listeners.
        client.post("/turnouts/create", data={"name": "IT77"})
        pushed = json.loads(ws.receive_text())
        # First push is the "added" notification.
        assert pushed["type"] == "turnout"
        assert pushed["data"]["name"] == "IT77"
