"""jmri_scripting.api -- authored-import version of the script globals.

Verifies that ``from jmri_scripting.api import turnouts, sensors, ...``
resolves to the same shim managers that the injected globals expose,
and that peer scripts can ``import`` each other via the profile's
``jython/`` directory.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from jmri_core.state import SensorState, TurnoutState

from jmri_scripting import ScriptRunner


async def _wait_until_finished(
    runner: ScriptRunner, name: str, timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = runner.get(name)
        if status and status.finished:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"script {name!r} did not finish in {timeout}s")


# ----- authored-import API -------------------------------------------------


async def test_api_import_resolves_to_active_shim(runner: ScriptRunner) -> None:
    """A script that uses the explicit-import idiom
    ``from jmri_scripting.api import sensors`` gets the same live
    manager the runtime globals expose."""
    source = """
from jmri_scripting.api import sensors
sensors.provide('IS_API_STYLE').setKnownState(ACTIVE)
"""
    runner.run_source(source, name="api-style")
    await _wait_until_finished(runner, "api-style")
    status = runner.get("api-style")
    assert status is not None
    assert status.error is None, status.error
    conn = runner.shim.connection
    assert conn.sensors.get("IS_API_STYLE").known_state == SensorState.ACTIVE


async def test_api_proxy_supports_iter_and_len(runner: ScriptRunner) -> None:
    """The shim proxy delegates iteration + len so ``for s in sensors:``
    works exactly like the injected global does."""
    source = """
from jmri_scripting.api import sensors
sensors.provide('IS_A')
sensors.provide('IS_B')
count = len(sensors)
memories.provide('IM_COUNT').setValue(count)
"""
    runner.run_source(source, name="api-iter")
    await _wait_until_finished(runner, "api-iter")
    status = runner.get("api-iter")
    assert status is not None
    assert status.error is None, status.error
    m = runner.shim.connection.memories.get("IM_COUNT")
    assert m.value == 2


def test_api_proxy_outside_script_raises() -> None:
    """Accessing a proxy attribute from a non-script thread must fail
    loudly -- there's no ambient shim to look up."""
    from jmri_scripting.api import sensors
    with pytest.raises(RuntimeError, match="outside a running script"):
        _ = sensors.get("something")


# ----- recursive scripting: one file imports another -----------------------


async def test_script_can_import_peer_library(
    runner: ScriptRunner, tmp_path: pathlib.Path,
) -> None:
    """After :meth:`load_directory` runs, ``<profile>/jython/`` sits on
    ``sys.path`` -- so a script in that directory can ``import
    lib_helpers`` to reuse code from a sibling ``lib_*.py`` file."""
    (tmp_path / "lib_shared.py").write_text(
        "\n".join([
            "from jmri_scripting.api import turnouts, TurnoutState",
            "",
            "def close_all(names):",
            "    for n in names:",
            "        turnouts.provide(n).setCommandedState(TurnoutState.CLOSED)",
        ])
    )
    (tmp_path / "runner.py").write_text(
        "\n".join([
            "import lib_shared",
            "lib_shared.close_all(['IT_LIB_1', 'IT_LIB_2'])",
        ])
    )
    started = runner.load_directory(tmp_path)
    assert {s.name for s in started} == {"runner"}   # lib_shared not auto-run
    await _wait_until_finished(runner, "runner")
    status = runner.get("runner")
    assert status is not None
    assert status.error is None, status.error
    conn = runner.shim.connection
    assert conn.turnouts.get("IT_LIB_1").commanded_state == TurnoutState.CLOSED
    assert conn.turnouts.get("IT_LIB_2").commanded_state == TurnoutState.CLOSED
