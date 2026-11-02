"""Jython namespace shim -- ``from jmri import ...`` works for real scripts."""
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


# ----- module registration --------------------------------------------------


def test_jmri_module_is_registered() -> None:
    """After a ScriptRunner exists, ``import jmri`` works from any thread."""
    import sys
    assert "jmri" in sys.modules
    assert "jmri.jmrit.automat" in sys.modules
    assert "jmri.util" in sys.modules


def test_from_jmri_import_instance_manager() -> None:
    from jmri import InstanceManager
    assert InstanceManager is not None
    assert hasattr(InstanceManager, "getDefault")


def test_from_jmri_jmrit_automat_import_abstract_automaton() -> None:
    from jmri.jmrit.automat import AbstractAutomaton
    # It's the same class our runner injects into globals.
    from jmri_scripting.automaton import AbstractAutomaton as OurClass
    assert AbstractAutomaton is OurClass


def test_jmri_path_constants() -> None:
    from jmri import Path
    assert Path.NORTH == 1
    assert Path.SOUTH == 2
    assert Path.EAST == 4
    assert Path.WEST == 8
    assert Path.NONE == 0
    # decodeDirection returns a human-readable name.
    assert Path.decodeDirection(Path.EAST) == "East"
    assert Path.decodeDirection(999) == "Unknown(999)"


def test_jmri_power_manager_constants() -> None:
    from jmri import PowerManager
    from jmri_core.state import PowerState
    assert PowerManager.ON == PowerState.ON
    assert PowerManager.OFF == PowerState.OFF


def test_jmri_dcc_loco_address() -> None:
    from jmri import DccLocoAddress
    a = DccLocoAddress(3601, True)
    assert a.getNumber() == 3601
    assert a.isLongAddress() is True
    assert DccLocoAddress(3601, True) == DccLocoAddress(3601, True)
    assert DccLocoAddress(3, False) != DccLocoAddress(3, True)


def test_unshimmed_attribute_raises_helpful_error() -> None:
    """An attribute we don't cover surfaces a clear message pointing at
    the coverage ranking -- so a script author knows it's a shim gap."""
    import jmri
    with pytest.raises(AttributeError, match="top-200"):
        _ = jmri.SomethingWeDidNotShim


# ----- InstanceManager end-to-end via a running script ---------------------


async def test_instance_manager_get_default_returns_shim_manager(
    runner: ScriptRunner,
) -> None:
    """A script that uses the JMRI-Java idiom
    ``sensors = InstanceManager.getDefault(Sensor)`` gets our shim's
    sensor manager and can drive it."""
    source = """
from jmri import InstanceManager, Sensor
mgr = InstanceManager.getDefault(Sensor)
s = mgr.provide('IS_JAVA_STYLE')
s.setKnownState(ACTIVE)
"""
    runner.run_source(source, name="im-get-default")
    await _wait_until_finished(runner, "im-get-default")
    status = runner.get("im-get-default")
    assert status is not None
    assert status.error is None
    conn = runner.shim.connection
    assert conn.sensors.get("IS_JAVA_STYLE").known_state == SensorState.ACTIVE


async def test_java_style_full_import(runner: ScriptRunner) -> None:
    """The complete Jython idiom -- full package path, single script."""
    source = """
import jmri
from jmri.jmrit.automat import AbstractAutomaton

class Mine(AbstractAutomaton):
    def init(self):
        self.mgr = jmri.InstanceManager.getDefault(jmri.Turnout)
        self.t = self.mgr.provide('IT_JAVA')

    def handle(self):
        self.t.setCommandedState(jmri.TurnoutState.CLOSED)
        return False
"""
    runner.run_source(source, name="java-style-full")
    await _wait_until_finished(runner, "java-style-full")
    status = runner.get("java-style-full")
    assert status is not None
    assert status.error is None
    conn = runner.shim.connection
    assert conn.turnouts.get("IT_JAVA").commanded_state == TurnoutState.CLOSED


async def test_instance_manager_unknown_class_raises(
    runner: ScriptRunner,
) -> None:
    """Requesting the shim manager for a class we don't cover gives a
    clear script-level error rather than a mysterious None."""
    source = """
from jmri import InstanceManager
class NotShimmed: pass
InstanceManager.getDefault(NotShimmed)
"""
    runner.run_source(source, name="im-unknown")
    await _wait_until_finished(runner, "im-unknown")
    status = runner.get("im-unknown")
    assert status is not None
    assert status.error is not None
    assert "not shimmed" in status.error.lower()


def test_instance_manager_outside_script_raises() -> None:
    """Direct access from a non-script thread must fail loudly -- there's
    no ambient shim to look up."""
    from jmri import InstanceManager, Sensor
    with pytest.raises(RuntimeError, match="outside a running script"):
        InstanceManager.getDefault(Sensor)
