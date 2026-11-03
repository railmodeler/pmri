"""Shared fixtures."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pmri_core.hal.internal import InternalConnection

from pmri_scripting import JmriShim, ScriptRunner


@pytest.fixture()
async def conn() -> AsyncIterator[InternalConnection]:
    c = InternalConnection()
    await c.open({})
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture()
def shim(conn: InternalConnection) -> JmriShim:
    return JmriShim(asyncio.get_event_loop(), conn)


@pytest.fixture()
def runner(conn: InternalConnection) -> ScriptRunner:
    return ScriptRunner.for_connection(asyncio.get_event_loop(), conn)



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
