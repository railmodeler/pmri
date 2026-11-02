"""Shared fixtures."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jmri_web.app import WebHandle, build_web_app


@pytest.fixture()
def handle() -> Iterator[WebHandle]:
    h = build_web_app()
    yield h


@pytest.fixture()
def client(handle: WebHandle) -> Iterator[TestClient]:
    with TestClient(handle.app) as tc:
        yield tc



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
