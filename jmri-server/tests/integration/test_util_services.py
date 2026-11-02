"""Integration tests for ``metadata`` / ``networkService`` / ``systemConnection``.

Locks the wire shape against the schemas at
``jmri/server/json/util/{metadata,networkService,systemConnection}-server.json``
so mobile clients (Engine Driver, JMRI iOS) that probe these on connect
see the same fields JMRI Java would send.
"""
from __future__ import annotations

import platform
import sys

from fastapi.testclient import TestClient

from tests.conftest import post


# ---------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------


def test_metadata_list_returns_the_full_name_set(client: TestClient) -> None:
    resp = post(client, "metadata", method="list")
    names = {item["name"] for item in resp["data"]}
    # Java's ``Metadata.getSystemNameArray()`` -- every name here must
    # come back or a mobile client that iterates the list will miss
    # something.
    assert names == {
        "JMRIVERSION", "JMRIVERCANON",
        "JMRIVERMAJOR", "JMRIVERMINOR", "JMRIVERTEST",
        "JVMVERSION", "JVMVENDOR",
        "activeProfile", "copyright",
    }


def test_metadata_get_jmriversion_matches_hello(client: TestClient) -> None:
    """The version reported by ``metadata`` must match the ``JMRI``
    field the hello frame ships with -- otherwise a client caches one
    version at connect time and reads a mismatched one later."""
    hello = post(client, "hello", method="get")
    meta = post(client, "metadata", method="get", data={"name": "JMRIVERSION"})
    assert meta["data"]["value"] == hello["data"]["JMRI"]


def test_metadata_get_jvm_reports_python_runtime(client: TestClient) -> None:
    """Field name is JMRI's -- the *value* is our Python runtime so
    scripts using ``jmri.getMetadata('JVMVERSION')`` see something
    useful instead of ``<unknown>``."""
    version = post(
        client, "metadata", method="get", data={"name": "JVMVERSION"},
    )
    vendor = post(
        client, "metadata", method="get", data={"name": "JVMVENDOR"},
    )
    assert version["data"]["value"] == platform.python_version()
    assert vendor["data"]["value"] == sys.implementation.name


def test_metadata_version_parts_split_from_the_string(
    client: TestClient,
) -> None:
    """Major / minor / test come from splitting the version string
    on ``.``. Non-numeric suffixes (``-py``, ``-alpha3``, ``+build``)
    are dropped before splitting."""
    major = post(
        client, "metadata", method="get", data={"name": "JMRIVERMAJOR"},
    )
    minor = post(
        client, "metadata", method="get", data={"name": "JMRIVERMINOR"},
    )
    test = post(
        client, "metadata", method="get", data={"name": "JMRIVERTEST"},
    )
    # Version is ``5.14.0-py`` in the default ServerMetadata.
    assert major["data"]["value"] == "5"
    assert minor["data"]["value"] == "14"
    assert test["data"]["value"] == "0"


def test_metadata_canonical_strips_suffix(client: TestClient) -> None:
    r = post(
        client, "metadata", method="get", data={"name": "JMRIVERCANON"},
    )
    assert r["data"]["value"] == "5.14.0"


def test_metadata_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "metadata", "method": "get",
        "data": {"name": "NOT_A_REAL_METADATA_NAME"},
    })
    assert r.status_code == 404


def test_metadata_get_without_name_400s(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "metadata", "method": "get", "data": {},
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------
# networkService
# ---------------------------------------------------------------------


def test_network_service_list_advertises_json_endpoint(
    client: TestClient,
) -> None:
    """The FastAPI/JSON service is always advertised -- mobile clients
    need at minimum this entry to route JSON traffic."""
    resp = post(client, "networkService", method="list")
    types = [svc["type"] for svc in resp["data"]]
    assert "_jmri-json._tcp.local." in types


def test_network_service_entry_has_required_fields(
    client: TestClient,
) -> None:
    """The schema requires name/userName/port/type/version/jmri/node."""
    resp = post(client, "networkService", method="list")
    svc = resp["data"][0]
    for required in ("name", "userName", "port", "type",
                     "version", "jmri", "node"):
        assert required in svc, f"missing field {required!r}"
    assert isinstance(svc["port"], int)


def test_network_service_get_by_type(client: TestClient) -> None:
    r = post(
        client, "networkService", method="get",
        data={"name": "_jmri-json._tcp.local."},
    )
    assert r["data"]["type"] == "_jmri-json._tcp.local."


def test_network_service_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "networkService", "method": "get",
        "data": {"name": "_ntp._udp.local."},
    })
    assert r.status_code == 404


def test_network_service_withrottle_included_when_running(handle) -> None:
    """If ``_withrottle_port`` is stashed on the ctx (as
    ``attach_withrottle_server`` does), it appears in the list."""
    handle.ctx._withrottle_port = 12099
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "networkService", "method": "list",
        })
        types = [svc["type"] for svc in r.json()["data"]]
        assert "_withrottle._tcp.local." in types
        for svc in r.json()["data"]:
            if svc["type"] == "_withrottle._tcp.local.":
                assert svc["port"] == 12099


# ---------------------------------------------------------------------
# systemConnection
# ---------------------------------------------------------------------


def test_system_connection_list_reports_active_and_internal(
    client: TestClient,
) -> None:
    """List must contain the active hardware connection AND the
    Internal one -- clients build a prefix picker from this and need
    'I' to route to software-only beans."""
    resp = post(client, "systemConnection", method="list")
    prefixes = {c["prefix"] for c in resp["data"]}
    assert "I" in prefixes


def test_system_connection_entry_matches_schema_shape(
    client: TestClient,
) -> None:
    """Schema requires name / prefix / mfg / description (last two
    nullable). No additionalProperties."""
    resp = post(client, "systemConnection", method="list")
    for entry in resp["data"]:
        assert set(entry.keys()) == {"name", "prefix", "mfg", "description"}
        assert isinstance(entry["prefix"], str)
        # mfg and description may be None; name may be None per the
        # schema's nullable declaration.


def test_system_connection_get_by_name(client: TestClient) -> None:
    resp = post(client, "systemConnection", method="get", data={"name": "Internal"})
    assert resp["data"]["prefix"] == "I"


def test_system_connection_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "systemConnection", "method": "get",
        "data": {"name": "Nonexistent"},
    })
    assert r.status_code == 404
