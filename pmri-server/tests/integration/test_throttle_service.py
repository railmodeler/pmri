"""ThrottleService -- full JSON wire shape validation."""
from __future__ import annotations

import jsonschema
from fastapi.testclient import TestClient

from tests.conftest import post


def test_put_acquires_throttle_via_address(client: TestClient, server_schemas: dict) -> None:
    resp = post(client, "throttle", method="put", data={"address": 3, "name": "3"})
    jsonschema.validate(resp["data"], server_schemas["throttle"])
    assert resp["data"]["address"] == 3
    assert resp["data"]["name"] == "3"
    assert resp["data"]["throttle"] == "3"   # deprecated alias still present


def test_put_then_get(client: TestClient, server_schemas: dict) -> None:
    post(client, "throttle", method="put", data={"address": 5, "name": "5"})
    resp = post(client, "throttle", method="get", data={"name": "5"})
    jsonschema.validate(resp["data"], server_schemas["throttle"])
    assert resp["data"]["address"] == 5


def test_post_mutates_speed_dir_funcs(client: TestClient, server_schemas: dict) -> None:
    post(client, "throttle", method="put", data={"address": 7, "name": "7"})
    resp = post(client, "throttle", method="post", data={
        "name": "7", "speed": 0.5, "forward": False, "F1": True,
    })
    jsonschema.validate(resp["data"], server_schemas["throttle"])
    assert resp["data"]["speed"] == 0.5
    assert resp["data"]["forward"] is False
    assert resp["data"]["F1"] is True


def test_emergency_stop_via_post(client: TestClient) -> None:
    post(client, "throttle", method="put", data={"address": 8, "name": "8"})
    resp = post(client, "throttle", method="post", data={"name": "8", "speed": -1.0})
    assert resp["data"]["speed"] == -1.0


def test_release_marks_throttle_released(client: TestClient, server_schemas: dict) -> None:
    post(client, "throttle", method="put", data={"address": 9, "name": "9"})
    resp = post(client, "throttle", method="post", data={"name": "9", "release": None})
    jsonschema.validate(resp["data"], server_schemas["throttle"])
    assert resp["data"]["release"] is None


def test_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "throttle", "method": "get", "data": {"name": "9999"},
    })
    assert r.status_code == 404


def test_list_returns_array(client: TestClient, server_schemas: dict) -> None:
    post(client, "throttle", method="put", data={"address": 10, "name": "10"})
    post(client, "throttle", method="put", data={"address": 11, "name": "11"})
    resp = post(client, "throttle", method="list")
    assert resp["type"] == "list"
    assert {t["address"] for t in resp["data"]} == {10, 11}
    for item in resp["data"]:
        jsonschema.validate(item, server_schemas["throttle"])


def test_throttle_alias_field_works(client: TestClient) -> None:
    """The deprecated ``throttle`` field is accepted in addition to ``name``."""
    post(client, "throttle", method="put", data={"address": 13, "name": "13"})
    resp = post(client, "throttle", method="get", data={"throttle": "13"})
    assert resp["data"]["address"] == 13


def test_put_without_address_or_name_returns_400(client: TestClient) -> None:
    r = client.post("/json/", json={"type": "throttle", "method": "put", "data": {}})
    assert r.status_code == 400


def test_delete_releases(client: TestClient) -> None:
    post(client, "throttle", method="put", data={"address": 14, "name": "14"})
    resp = post(client, "throttle", method="delete", data={"name": "14"})
    assert resp["data"]["release"] is None
    # Subsequent get returns 404 because the throttle is gone.
    r = client.post("/json/", json={
        "type": "throttle", "method": "get", "data": {"name": "14"},
    })
    assert r.status_code == 404
