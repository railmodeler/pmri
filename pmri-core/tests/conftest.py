"""Shared pytest fixtures for pmri_core tests.

Also carries the shared ``@pytest.mark.hardware`` skip-by-default
hook -- duplicated in every package's ``tests/conftest.py`` because
each package sets its own pytest rootdir via its
``[tool.pytest.ini_options]`` block, so a repo-root conftest isn't
discovered.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pmri_core.hal.internal import InternalConnection


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-hardware", action="store_true", default=False,
        help="Include @pytest.mark.hardware tests (workbench Pi only).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item],
) -> None:
    if config.getoption("--run-hardware"):
        return
    skip = pytest.mark.skip(
        reason="hardware test - pass --run-hardware to include",
    )
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture()
async def internal_connection() -> AsyncIterator[InternalConnection]:
    conn = InternalConnection()
    await conn.open({})
    try:
        yield conn
    finally:
        await conn.close()
