"""Verify the ``@pytest.mark.hardware`` skip-by-default convention.

The root ``conftest.py`` auto-skips hardware-marked tests unless
``--run-hardware`` is passed. That behaviour is what lets us commit
tests that expect a live command station attached -- they don't run
on CI or a developer laptop, only on the workbench Pi where the
maintainer opts in explicitly.

This one canary test is enough to catch a regression in the shared
mechanism; the driver packages don't need their own copy.
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
def test_hardware_marker_canary() -> None:
    """Canary that proves the hardware marker + auto-skip work.

    In a normal ``pytest`` run this test is auto-skipped by the
    ``conftest.py`` hook -- you'll see one ``SKIPPED`` line for it.
    That's the desired state.

    Under ``pytest --run-hardware`` this test runs and passes,
    confirming the opt-in path also works. If it stops appearing
    in either mode, the marker registration has regressed."""
    assert True
