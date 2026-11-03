"""ProgrammerService tests with the InternalConnection (no programmer) + an injected one."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pmri_core.beans.programmer import Programmer, ProgrammerManager, ProgrammingMode

from pmri_server.app import build_app


class FakeProgrammer(Programmer):
    def __init__(self, mode: ProgrammingMode = ProgrammingMode.SERVICE_DIRECT) -> None:
        self._mode = mode
        self.cvs: dict[int, int] = {}

    @property
    def mode(self) -> ProgrammingMode:
        return self._mode

    async def read_cv(self, cv: int) -> int:
        return self.cvs.get(cv, 0)

    async def write_cv(self, cv: int, value: int) -> None:
        self.cvs[cv] = value


class FakeProgrammerManager(ProgrammerManager):
    def __init__(self) -> None:
        self.service = FakeProgrammer()
        self.ops_by_address: dict[int, FakeProgrammer] = {}

    def global_programmer(self) -> FakeProgrammer:
        return self.service

    def ops_programmer(
        self, address: int, *, long_address: bool = False
    ) -> FakeProgrammer:
        return self.ops_by_address.setdefault(address, FakeProgrammer(ProgrammingMode.OPS_MODE))


def test_list_on_internal_reports_unavailable() -> None:
    handle = build_app()
    with TestClient(handle.app) as client:
        r = client.post("/json/", json={"type": "programmer", "method": "list"})
        r.raise_for_status()
        modes = {entry["mode"]: entry["available"] for entry in r.json()["data"]}
        assert modes["service_direct"] is False
        assert modes["ops_mode"] is False


@pytest.fixture()
def client_with_programmer():
    handle = build_app()
    handle.ctx.connection.programmers = FakeProgrammerManager()  # type: ignore[assignment]
    with TestClient(handle.app) as client:
        yield client, handle


def test_list_with_programmer(client_with_programmer):  # noqa: ANN001
    client, _ = client_with_programmer
    r = client.post("/json/", json={"type": "programmer", "method": "list"})
    r.raise_for_status()
    modes = {entry["mode"]: entry["available"] for entry in r.json()["data"]}
    assert modes["service_direct"] is True
    assert modes["ops_mode"] is True


def test_service_mode_write_then_read(client_with_programmer):  # noqa: ANN001
    client, _ = client_with_programmer
    # Write CV29 = 6.
    r = client.post("/json/", json={
        "type": "programmer", "method": "post",
        "data": {"cv": 29, "value": 6, "mode": "service"},
    })
    r.raise_for_status()
    assert r.json()["data"] == {"cv": 29, "value": 6, "mode": "service_direct"}
    # Read it back.
    r = client.post("/json/", json={
        "type": "programmer", "method": "get",
        "data": {"cv": 29, "mode": "service"},
    })
    r.raise_for_status()
    assert r.json()["data"]["value"] == 6


def test_ops_mode_requires_address(client_with_programmer):  # noqa: ANN001
    client, _ = client_with_programmer
    r = client.post("/json/", json={
        "type": "programmer", "method": "get",
        "data": {"cv": 1, "mode": "ops"},
    })
    assert r.status_code == 400


def test_ops_mode_write(client_with_programmer):  # noqa: ANN001
    client, _ = client_with_programmer
    r = client.post("/json/", json={
        "type": "programmer", "method": "post",
        "data": {"cv": 1, "value": 99, "mode": "ops", "address": 3601},
    })
    r.raise_for_status()
    assert r.json()["data"]["mode"] == "ops_mode"


def test_get_without_programmer_returns_503() -> None:
    handle = build_app()
    with TestClient(handle.app) as client:
        r = client.post("/json/", json={
            "type": "programmer", "method": "get",
            "data": {"cv": 1, "mode": "service"},
        })
        assert r.status_code == 503


def test_write_validates_value(client_with_programmer):  # noqa: ANN001
    client, _ = client_with_programmer
    r = client.post("/json/", json={
        "type": "programmer", "method": "post",
        "data": {"cv": 1, "value": 256, "mode": "service"},
    })
    assert r.status_code == 400


def test_unknown_mode_returns_400(client_with_programmer):  # noqa: ANN001
    client, _ = client_with_programmer
    r = client.post("/json/", json={
        "type": "programmer", "method": "get",
        "data": {"cv": 1, "mode": "magic"},
    })
    assert r.status_code == 400
