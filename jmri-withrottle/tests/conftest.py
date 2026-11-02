"""Shared pytest hooks for this package.

The ``hardware`` marker is used across the driver packages to gate
tests that need a real command station attached. The hook here
skips those tests by default; pass ``--run-hardware`` on the pytest
command line to include them.

Duplicated in each package's ``tests/conftest.py`` because each
package sets its own ``rootdir`` via ``[tool.pytest.ini_options]``,
which prevents a repo-root conftest from being discovered.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    # ``getoption("--run-hardware")`` is safe even if multiple
    # packages define the same option -- pytest deduplicates.
    if not any(o.dest == "run_hardware" for o in parser._anonymous.options):
        parser.addoption(
            "--run-hardware",
            action="store_true",
            default=False,
            help=(
                "Include tests marked with @pytest.mark.hardware. "
                "Requires physical DCC hardware attached."
            ),
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
