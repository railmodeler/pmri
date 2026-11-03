"""Shared fixtures: an open InternalConnection + ExecutionContext."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pmri_core.hal.internal import InternalConnection

from pmri_logixng import ExecutionContext


@pytest.fixture()
async def conn() -> AsyncIterator[InternalConnection]:
    c = InternalConnection()
    await c.open({})
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture()
def ctx(conn: InternalConnection) -> ExecutionContext:
    return ExecutionContext(connection=conn, trace_enabled=True)



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
