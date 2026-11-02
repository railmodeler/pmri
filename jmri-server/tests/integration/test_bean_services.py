"""Integration tests for the bean services -- turnout, sensor, light, etc.

Each test validates the emitted JSON against the topic's server schema from
``stage-0/contracts/json-schema/``.
"""
from __future__ import annotations

import jsonschema
import pytest
from fastapi.testclient import TestClient

from tests.conftest import post


# (topic, sample_name, mutation_data)
BEAN_CASES = [
    ("turnout",     "IT1",   {"state": 2}),                 # CLOSED
    ("sensor",      "IS2",   {"state": 4}),                 # INACTIVE
    ("light",       "IL3",   {"state": 4}),                 # OFF
    ("signalHead",  "IH4",   {"appearance": 1}),            # RED
    ("signalMast",  "IF$shsm:basic:(1)", {"aspect": "Clear"}),
    ("memory",      "IM5",   {"value": "loco-3601"}),
    ("reporter",    "IR6",   {"report": "loco-3601"}),
    ("block",       "IB7",   {"value": "loco-3601"}),
    ("route",       "IO8",   None),
]


@pytest.mark.parametrize("topic,name,mutation", BEAN_CASES, ids=[c[0] for c in BEAN_CASES])
def test_create_then_get_returns_schema_valid(
    client: TestClient, server_schemas: dict, topic: str, name: str, mutation: dict | None,
) -> None:
    # put-create
    resp = post(client, topic, method="put", data={"name": name})
    assert resp["type"] == topic
    jsonschema.validate(resp["data"], server_schemas[topic])

    # get
    resp = post(client, topic, method="get", data={"name": name})
    jsonschema.validate(resp["data"], server_schemas[topic])
    assert resp["data"]["name"] == name


@pytest.mark.parametrize("topic,name,mutation", BEAN_CASES, ids=[c[0] for c in BEAN_CASES])
def test_post_applies_mutation(
    client: TestClient, server_schemas: dict, topic: str, name: str, mutation: dict | None,
) -> None:
    post(client, topic, method="put", data={"name": name})
    if mutation is None:
        pytest.skip(f"{topic} has no plain-state mutation in this test matrix")
    resp = post(client, topic, method="post", data={"name": name, **mutation})
    jsonschema.validate(resp["data"], server_schemas[topic])


@pytest.mark.parametrize("topic,name,mutation", BEAN_CASES, ids=[c[0] for c in BEAN_CASES])
def test_list_returns_array(
    client: TestClient, server_schemas: dict, topic: str, name: str, mutation: dict | None,
) -> None:
    post(client, topic, method="put", data={"name": name})
    post(client, topic, method="put", data={"name": name.replace("1", "9").replace("2", "9")})

    r = client.post("/json/", json={"type": "list", "list": topic})
    r.raise_for_status()
    resp = r.json()
    assert resp["type"] == "list"
    assert isinstance(resp["data"], list)
    for item in resp["data"]:
        jsonschema.validate(item, server_schemas[topic])


def test_get_unknown_bean_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "turnout", "method": "get", "data": {"name": "IT999"}
    })
    assert r.status_code == 404


def test_post_unknown_bean_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "sensor", "method": "post", "data": {"name": "IS999", "state": 2}
    })
    assert r.status_code == 404


def test_turnout_state_round_trip(client: TestClient) -> None:
    """Set state via post, then get must show it back."""
    post(client, "turnout", method="put", data={"name": "IT100"})
    post(client, "turnout", method="post", data={"name": "IT100", "state": 4})  # THROWN
    resp = post(client, "turnout", method="get", data={"name": "IT100"})
    assert resp["data"]["state"] == 4


def test_delete_removes_bean(client: TestClient) -> None:
    post(client, "turnout", method="put", data={"name": "IT200"})
    resp = post(client, "turnout", method="delete", data={"name": "IT200"})
    assert resp["data"]["name"] == "IT200"
    r = client.post(
        "/json/", json={"type": "turnout", "method": "get", "data": {"name": "IT200"}}
    )
    assert r.status_code == 404


def test_request_missing_name_returns_400(client: TestClient) -> None:
    r = client.post("/json/", json={"type": "turnout", "method": "get"})
    assert r.status_code == 400


def test_signal_mast_held_state_is_held_string(client: TestClient) -> None:
    name = "IF$shsm:basic:test:(2)"
    post(client, "signalMast", method="put", data={"name": name})
    post(client, "signalMast", method="post", data={"name": name, "aspect": "Clear"})
    resp = post(client, "signalMast", method="post", data={"name": name, "held": True})
    assert resp["data"]["state"] == "Held"
    assert resp["data"]["held"] is True


def test_signal_head_held_state_is_100(client: TestClient) -> None:
    name = "IH50"
    post(client, "signalHead", method="put", data={"name": name})
    post(client, "signalHead", method="post", data={"name": name, "appearance": 4})  # GREEN
    resp = post(client, "signalHead", method="post", data={"name": name, "held": True})
    assert resp["data"]["state"] == 100  # JMRI's "held" marker


def test_route_fire_via_state_change(client: TestClient) -> None:
    name = "IO60"
    post(client, "route", method="put", data={"name": name})
    # Routes with no steps fire successfully (zero steps executed).
    resp = post(client, "route", method="post", data={"name": name, "state": 4})
    assert resp["type"] == "route"


def test_roster_list_is_empty(client: TestClient) -> None:
    r = client.post("/json/", json={"type": "list", "list": "roster"})
    r.raise_for_status()
    resp = r.json()
    assert resp["data"] == []


# ---- writable capability flag ---------------------------------------------
#
# A standard writable bean must not carry ``writable`` on the wire -- the
# canonical JMRI schemas use ``additionalProperties: false`` and would
# reject the key. The field only appears when a passive driver flips the
# flag to False, which the test fixtures don't exercise (that's an
# ncemonitor-only concern); the omission is what we assert here.


def test_writable_bean_omits_writable_field(client: TestClient) -> None:
    """A regular, writable turnout bean must not emit ``writable`` on the
    JSON payload -- the schema's ``additionalProperties: false`` would
    reject the key, and clients default to writable=True when absent."""
    name = "IT77"
    resp = post(client, "turnout", method="put", data={"name": name})
    assert "writable" not in resp["data"]
    get_resp = post(client, "turnout", method="get", data={"name": name})
    assert "writable" not in get_resp["data"]


def test_writable_power_omits_writable_field(client: TestClient) -> None:
    """Default (writable) power manager must not emit ``writable`` on the
    power snapshot. Mirrors the bean rule for the single-instance manager."""
    resp = post(client, "power", method="get")
    assert "writable" not in resp["data"]
