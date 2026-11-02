"""Shared fixtures for jmri-server tests."""
from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jmri_server.app import AppHandle, build_app

SCHEMAS_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "stage-0"
    / "contracts"
    / "json-schema"
)


@pytest.fixture()
def handle() -> Iterator[AppHandle]:
    h = build_app()
    yield h


@pytest.fixture()
def client(handle: AppHandle) -> Iterator[TestClient]:
    with TestClient(handle.app) as tc:
        yield tc


@pytest.fixture(scope="session")
def server_schemas() -> dict[str, dict]:
    """Map of topic -> JSON Schema (server-side data payload)."""
    out: dict[str, dict] = {}
    for schema_file in SCHEMAS_DIR.rglob("*-server.json"):
        topic = schema_file.stem.removesuffix("-server")
        out[topic] = json.loads(schema_file.read_text(encoding="utf-8"))
    return out


def post(client: TestClient, type_: str, *, method: str | None = None,
         data: dict | None = None, id_: int | None = None) -> dict:
    """Helper: build envelope, POST it, return the JSON response."""
    body: dict = {"type": type_}
    if data is not None:
        body["data"] = data
    if method is not None:
        body["method"] = method
    if id_ is not None:
        body["id"] = id_
    r = client.post("/json/", json=body)
    r.raise_for_status()
    return r.json()



def pytest_addoption(parser):
    parser.addoption(
        "--run-hardware", action="store_true", default=False,
        help="Include @pytest.mark.hardware tests (workbench Pi only).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-hardware"):
        return
    import pytest as _pytest
    skip = _pytest.mark.skip(
        reason="hardware test - pass --run-hardware to include",
    )
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)
