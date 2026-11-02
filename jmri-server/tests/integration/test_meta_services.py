"""Integration tests for meta services (hello/ping/version/railroad/node/power)."""
from __future__ import annotations

import jsonschema
from fastapi.testclient import TestClient

from tests.conftest import post


def test_hello_returns_required_fields(client: TestClient, server_schemas: dict) -> None:
    resp = post(client, "hello", method="get")
    data = resp["data"]
    jsonschema.validate(data, server_schemas["hello"])
    assert "JMRI" in data
    assert "json" in data
    assert "heartbeat" in data


def test_ping_returns_empty_data(client: TestClient) -> None:
    resp = post(client, "ping")
    assert resp["data"] == {}


def test_version_returns_jmri_and_version(client: TestClient) -> None:
    resp = post(client, "version")
    assert "JMRI" in resp["data"]
    assert "version" in resp["data"]


def test_railroad_returns_name(client: TestClient) -> None:
    resp = post(client, "railroad")
    assert "name" in resp["data"]


def test_node_returns_identifier(client: TestClient) -> None:
    resp = post(client, "node")
    assert "node" in resp["data"]
    assert isinstance(resp["data"]["formerNodes"], list)


def test_id_echoed_unchanged(client: TestClient) -> None:
    resp = post(client, "ping", id_=12345)
    assert resp["id"] == 12345


def test_unknown_topic_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={"type": "nonsense_does_not_exist"})
    assert r.status_code == 404


def test_power_get_returns_schema_valid(client: TestClient, server_schemas: dict) -> None:
    resp = post(client, "power", method="get")
    jsonschema.validate(resp["data"], server_schemas["power"])


def test_power_post_sets_state(client: TestClient, server_schemas: dict) -> None:
    resp = post(client, "power", method="post", data={"state": 2})  # ON
    jsonschema.validate(resp["data"], server_schemas["power"])
    assert resp["data"]["state"] == 2

    resp = post(client, "power", method="post", data={"state": 4})  # OFF
    assert resp["data"]["state"] == 4
