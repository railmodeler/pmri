"""End-to-end smoke tests: JMRI-shaped scripts run against the shim.

Each test compiles a script string and runs it through the real
:class:`ScriptRunner`, then asserts the observable side effect on
the underlying :class:`InternalConnection`. Covers the common
Jython patterns real user scripts use -- the point is to catch
regressions in the shim wire-up (Jython namespace, injected
globals, InstanceManager routing) as scripts written in the JMRI
style would.

Not a full script API compatibility suite -- those live in the
per-shim unit tests. This is the "does the top-of-the-stack still
plug together" canary.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest
from jmri_core.hal.internal import InternalConnection
from jmri_core.state import SensorState, TurnoutState

from jmri_scripting import ScriptRunner, ScriptStatus


async def _wait_finished(status: ScriptStatus, timeout: float = 3.0) -> None:
    """Poll until the script's ``finished`` flag flips (or timeout).

    Must ``await asyncio.sleep()`` rather than block: script threads
    call ``_block()`` which schedules coroutines onto our event
    loop, and a blocking sleep here would starve those scheduled
    coroutines and hang the script forever."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status.finished:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"script {status.name!r} did not finish within {timeout}s "
        f"(error={status.error!r}, stdout={status.stdout!r})"
    )


# ---- injected globals: bare names --------------------------------------


async def test_bare_globals_turnout_toggle(
    conn: InternalConnection, runner: ScriptRunner,
) -> None:
    """A script using bare ``turnouts`` + ``CLOSED``/``THROWN`` (the
    JMRI Jython convention) toggles a turnout through the shim's
    ManagerShim."""
    source = """
t = turnouts.provideTurnout("IT10")
t.setCommandedState(THROWN)
print("set to", THROWN)
"""
    status = runner.run_source(source, name="bare-globals")
    await _wait_finished(status)
    assert status.error is None, status.error
    turnout = conn.turnouts.get_by_system_name("IT10")
    assert turnout is not None
    assert turnout.commanded_state is TurnoutState.THROWN
    assert "set to" in status.stdout


# ---- jython namespace: from jmri import ... ----------------------------


async def test_from_jmri_import_instance_manager(
    conn: InternalConnection, runner: ScriptRunner,
) -> None:
    """The synthetic ``jmri.*`` package tree lets JMRI Java-style
    scripts import unchanged. Exercises the full
    ``from jmri import InstanceManager, Sensor`` path."""
    source = """
from jmri import InstanceManager, Sensor
mgr = InstanceManager.getDefault(Sensor)
s = mgr.provideSensor("IS42")
s.setKnownState(ACTIVE)
"""
    status = runner.run_source(source, name="import-jmri")
    await _wait_finished(status)
    assert status.error is None, status.error
    sensor = conn.sensors.get_by_system_name("IS42")
    assert sensor is not None
    assert sensor.known_state is SensorState.ACTIVE


# ---- new: ConsistManager shim ------------------------------------------


async def test_consist_manager_shim(
    conn: InternalConnection, runner: ScriptRunner,
) -> None:
    """Beta 4 addition -- ``InstanceManager.getDefault(ConsistManager)``
    routes to the consist shim; scripts can create + populate consists
    exactly as they would under JMRI Jython."""
    source = """
from jmri import InstanceManager, ConsistManager
cm = InstanceManager.getDefault(ConsistManager)
c = cm.getConsist(21)
c.add(3601)
c.add(3602, direction_normal=False)
print("members:", c.members)
"""
    status = runner.run_source(source, name="consists")
    await _wait_finished(status)
    assert status.error is None, status.error
    consist = conn.consists.get(21)
    assert consist is not None
    addresses = [m.address for m in consist.locos]
    assert 3601 in addresses
    assert 3602 in addresses


# ---- new: ProgrammerManager shim ---------------------------------------


async def test_programmer_manager_shim(
    conn: InternalConnection, runner: ScriptRunner,
) -> None:
    """``InstanceManager.getDefault(ProgrammerManager)`` should resolve;
    calling ``getGlobalProgrammer()`` returns None on the internal
    connection (no programming track), which the shim should surface
    as ``None`` rather than crashing.

    The important assertion is that the lookup itself works -- a real
    JMRI Java script hitting a non-programmer driver expects None
    too. Programming test coverage against a real programmer lives
    in the driver test suites."""
    source = """
from jmri import InstanceManager, ProgrammerManager
pm = InstanceManager.getDefault(ProgrammerManager)
prog = pm.getGlobalProgrammer()
result = "None" if prog is None else "programmer"
print("global_programmer:", result)
"""
    status = runner.run_source(source, name="prog")
    await _wait_finished(status)
    assert status.error is None, status.error
    assert "global_programmer:" in status.stdout


# ---- traceback quality ------------------------------------------------


async def test_traceback_captures_full_stack(runner: ScriptRunner) -> None:
    """A script that raises inside a nested call must surface the
    full formatted traceback in status.error -- not just the
    exception message. Otherwise operators can't debug from the
    /scripts page."""
    source = """
def inner():
    raise ValueError("something specific")


def outer():
    inner()


outer()
"""
    status = runner.run_source(source, name="traceback")
    await _wait_finished(status)
    assert status.error is not None
    # Full traceback includes both call frames + the message.
    assert "inner" in status.error
    assert "outer" in status.error
    assert "something specific" in status.error
    assert "Traceback" in status.error


# ---- stdout capture per script ----------------------------------------


async def test_stdout_isolated_per_script(runner: ScriptRunner) -> None:
    """Two scripts print different things; each status carries only
    its own output. Guards against a regression in the per-thread
    dispatch stream where output bleeds across scripts."""
    s1 = runner.run_source(
        'print("hello from A")', name="script-a",
    )
    s2 = runner.run_source(
        'print("hello from B")', name="script-b",
    )
    await _wait_finished(s1)
    await _wait_finished(s2)
    assert "hello from A" in s1.stdout
    assert "hello from B" not in s1.stdout
    assert "hello from B" in s2.stdout
    assert "hello from A" not in s2.stdout


# ---- shim ownership --------------------------------------------------


async def test_from_jmri_scripting_api_import(
    conn: InternalConnection, runner: ScriptRunner,
) -> None:
    """Alternative to the injected globals: ``from jmri_scripting.api
    import turnouts`` -- resolves to the same shim through the module
    proxy, so a user's helper module can capture the reference at
    import time and still hit the current script's shim."""
    source = """
from jmri_scripting.api import turnouts, THROWN
t = turnouts.provideTurnout("IT_API")
t.setCommandedState(THROWN)
"""
    status = runner.run_source(source, name="api-import")
    await _wait_finished(status)
    assert status.error is None, status.error
    turnout = conn.turnouts.get_by_system_name("IT_API")
    assert turnout is not None
    assert turnout.commanded_state is TurnoutState.THROWN
