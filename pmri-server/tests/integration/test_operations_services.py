"""Integration tests for the Operations Pro CRUD services."""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from pmri_core.persistence.profile import Profile

from pmri_server.operations_store import OperationsStore


def _wire_store(handle, tmp_path: pathlib.Path) -> pathlib.Path:
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    handle.ctx._operations_store = OperationsStore(profile=profile)
    return profile.operations_dir


def _post(tc: TestClient, topic: str, *, method: str,
          data: dict | None = None) -> dict:
    body: dict = {"type": topic, "method": method}
    if data is not None:
        body["data"] = data
    r = tc.post("/json/", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_cars_put_then_list(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-cars", method="put", data={
            "id": "c1", "roadName": "SP", "roadNumber": "4449",
            "type": "boxcar", "length": "40",
        })
        _post(tc, "operations-cars", method="put", data={
            "id": "c2", "roadName": "UP", "roadNumber": "844",
        })
        r = _post(tc, "operations-cars", method="list")
    cars = r["data"]
    assert len(cars) == 2
    assert {c["id"] for c in cars} == {"c1", "c2"}
    sp = next(c for c in cars if c["id"] == "c1")
    assert sp["roadName"] == "SP"
    assert sp["type"] == "boxcar"


def test_cars_get_404_on_missing(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "operations-cars", "method": "get",
            "data": {"id": "nope"},
        })
    assert r.status_code == 404


def test_cars_delete_round_trip(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-cars", method="put", data={"id": "c1"})
        r = _post(tc, "operations-cars", method="delete", data={"id": "c1"})
        assert r["data"]["deleted"] is True
        r = _post(tc, "operations-cars", method="list")
    assert r["data"] == []


def test_engines_crud(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-engines", method="put", data={
            "id": "eng-1", "roadName": "SP", "model": "FEF-3",
        })
        r = _post(tc, "operations-engines", method="get", data={"id": "eng-1"})
    assert r["data"]["model"] == "FEF-3"
    assert r["data"]["roadName"] == "SP"


def test_locations_crud(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-locations", method="put", data={
            "id": "loc-1", "name": "Oakland Yard",
        })
        r = _post(tc, "operations-locations", method="list")
    locs = r["data"]
    assert len(locs) == 1
    assert locs[0]["name"] == "Oakland Yard"


def test_unmodeled_fields_round_trip_via_extras(
    tmp_path: pathlib.Path, handle,
) -> None:
    """Keys the Pydantic model doesn't recognise (e.g. ``weightTons``)
    should land in ``extra`` on parse and appear again on list output."""
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-cars", method="put", data={
            "id": "c1", "roadName": "SP",
            "weightTons": "35.5", "color": "boxcar red",
        })
        r = _post(tc, "operations-cars", method="get", data={"id": "c1"})
    assert r["data"]["weightTons"] == "35.5"
    assert r["data"]["color"] == "boxcar red"


def test_routes_crud_with_stops(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        r = _post(tc, "operations-routes", method="put", data={
            "id": "r-1", "name": "Local Line", "comment": "weekday",
            "stops": [
                {"stationId": "stn-a", "name": "Oakland",
                 "pickups": "yes", "drops": "yes"},
                {"stationId": "stn-b", "name": "Fremont",
                 "pickups": "yes", "drops": "no"},
            ],
        })
        route = r["data"]
        assert route["name"] == "Local Line"
        assert [s["stationId"] for s in route["stops"]] == [
            "stn-a", "stn-b",
        ]
        assert [s["sequenceId"] for s in route["stops"]] == ["1", "2"]
        r = _post(tc, "operations-routes", method="list")
        assert len(r["data"]) == 1
        _post(tc, "operations-routes", method="put", data={
            "id": "r-1", "name": "Local Line",
            "stops": [{"stationId": "stn-c"}],
        })
        r = _post(tc, "operations-routes", method="get", data={"id": "r-1"})
        assert [s["stationId"] for s in r["data"]["stops"]] == ["stn-c"]


def test_route_delete_round_trip(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-routes", method="put", data={"id": "r-1"})
        r = _post(tc, "operations-routes", method="delete", data={"id": "r-1"})
        assert r["data"]["deleted"] is True
        r = _post(tc, "operations-routes", method="list")
    assert r["data"] == []


def test_trains_crud(tmp_path: pathlib.Path, handle) -> None:
    _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-trains", method="put", data={
            "id": "t-1", "name": "Local 101",
            "route": "r-1",
            "departHour": "09", "departMinute": "30",
        })
        r = _post(tc, "operations-trains", method="get", data={"id": "t-1"})
    train = r["data"]
    assert train["name"] == "Local 101"
    assert train["route"] == "r-1"
    assert train["departHour"] == "09"
    assert train["departMinute"] == "30"


def test_car_persists_through_restart(
    tmp_path: pathlib.Path, handle,
) -> None:
    """Writes land on disk; a fresh OperationsStore reads them back."""
    ops_dir = _wire_store(handle, tmp_path)
    with TestClient(handle.app) as tc:
        _post(tc, "operations-cars", method="put", data={
            "id": "c1", "roadName": "SP",
        })
    assert (ops_dir / "OperationsCarRoster.xml").is_file()
    # Instantiate a fresh store over the same profile; the car survives.
    profile = Profile(root=tmp_path, name="dev")
    fresh = OperationsStore(profile=profile)
    cars = list(fresh.cars())
    assert len(cars) == 1
    assert cars[0].id == "c1"
    assert cars[0].road_name == "SP"
