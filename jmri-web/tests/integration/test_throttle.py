"""End-to-end throttle tests -- picking a loco, driving it, releasing.

Uses the default ``InternalConnection``, whose ``ThrottleManager`` is the
in-memory base. That keeps the test independent of any real driver while
still exercising the same code path a LocoNet throttle would take.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_picking_throttle_acquires_handle(client: TestClient) -> None:
    """Visiting /throttle?address=42 should acquire the throttle on the server."""
    r = client.get("/throttle?address=42")
    r.raise_for_status()
    # Confirm via the JSON API that the throttle now exists.
    resp = client.post("/json/", json={
        "type": "throttle", "method": "list",
    })
    resp.raise_for_status()
    addresses = [t["address"] for t in resp.json()["data"]]
    assert 42 in addresses


def test_set_speed_via_htmx(client: TestClient) -> None:
    client.get("/throttle?address=3")
    r = client.post("/throttle/3/state", data={"speed": "0.5"})
    r.raise_for_status()
    # The fragment shows the new speed.
    assert "50" in r.text   # speed*100 rounded


def test_set_forward_via_htmx(client: TestClient) -> None:
    client.get("/throttle?address=3")
    # First go reverse, then forward -- both succeed.
    r1 = client.post("/throttle/3/state", data={"forward": "0"})
    r1.raise_for_status()
    assert "REV" in r1.text
    r2 = client.post("/throttle/3/state", data={"forward": "1"})
    r2.raise_for_status()
    assert "FWD" in r2.text


def test_set_function_via_htmx(client: TestClient) -> None:
    client.get("/throttle?address=3")
    r = client.post("/throttle/3/function/0", data={"on": "1"})
    r.raise_for_status()
    # No exception is sufficient; the response is the status fragment.


def test_release_via_htmx(client: TestClient) -> None:
    client.get("/throttle?address=3")
    r = client.post("/throttle/3/release")
    r.raise_for_status()
    assert "Released" in r.text


def test_throttle_state_via_json_api(client: TestClient) -> None:
    """Drive a throttle through the JSON API and read it back."""
    # Put-create.
    r = client.post("/json/", json={
        "type": "throttle", "method": "put",
        "data": {"address": 12, "name": "12"},
    })
    r.raise_for_status()
    assert r.json()["data"]["address"] == 12
    # Post-mutate.
    r = client.post("/json/", json={
        "type": "throttle", "method": "post",
        "data": {"name": "12", "speed": 0.25, "forward": True, "F0": True},
    })
    r.raise_for_status()
    data = r.json()["data"]
    assert data["speed"] == 0.25
    assert data["forward"] is True
    assert data["F0"] is True
    # Get.
    r = client.post("/json/", json={
        "type": "throttle", "method": "get",
        "data": {"name": "12"},
    })
    assert r.json()["data"]["F0"] is True


def test_throttle_post_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "throttle", "method": "post",
        "data": {"name": "9999", "speed": 0.5},
    })
    assert r.status_code == 404
