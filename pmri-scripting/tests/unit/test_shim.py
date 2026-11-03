"""ScriptShim tests -- exercised from the event-loop thread.

The shim is designed for scripts running on a *different* thread, but
its accessor methods (get/get_by_*) and the property reads on wrapped
beans work from any thread. Write methods bridge to the event loop and
would deadlock if called from the loop thread -- those are tested in
test_runner.py via real worker threads.
"""
from __future__ import annotations

import pytest
from pmri_core.state import LightState, PowerState, SensorState, TurnoutState

from pmri_scripting.shim import JmriShim


def test_globals_includes_all_managers(shim: JmriShim) -> None:
    g = shim.globals()
    for name in (
        "sensors", "turnouts", "lights", "memories", "reporters",
        "blocks", "routes", "signals", "masts", "powermanager",
    ):
        assert name in g


def test_globals_includes_state_constants(shim: JmriShim) -> None:
    g = shim.globals()
    assert g["ACTIVE"] == SensorState.ACTIVE
    assert g["CLOSED"] == TurnoutState.CLOSED
    assert g["ON"] == LightState.ON
    assert g["SensorState"] is SensorState


async def test_shim_returns_wrappers_not_raw_beans(shim: JmriShim, conn) -> None:  # noqa: ANN001
    # Pre-create a sensor on the loop thread so the script doesn't need to.
    await conn.sensors.provide("IS1")
    wrapped = shim.sensors.get("IS1")
    assert wrapped is not None
    # The wrapper exposes JMRI-style camelCase methods.
    assert hasattr(wrapped, "setKnownState")
    assert hasattr(wrapped, "isActive")
    assert wrapped.system_name == "IS1"


async def test_shim_get_unknown_returns_none(shim: JmriShim) -> None:
    assert shim.sensors.get("IS999") is None


async def test_named_bean_set_returns_wrappers(shim: JmriShim, conn) -> None:  # noqa: ANN001
    await conn.sensors.provide("IS1")
    await conn.sensors.provide("IS2")
    all_sensors = shim.sensors.get_named_bean_set()
    assert len(all_sensors) == 2
    for s in all_sensors:
        assert hasattr(s, "setKnownState")


async def test_power_constants(shim: JmriShim) -> None:
    assert shim.powermanager.ON == PowerState.ON
    assert shim.powermanager.OFF == PowerState.OFF


async def test_command_station_power_helpers(shim: JmriShim, conn) -> None:
    """Command-station power is a distinct concept from track power.

    Track power = ON/OFF/IDLE/UNKNOWN, the DCC signal on the rails.
    CS power = strict bool, is the CS itself powered / talking to us.

    Base drivers tie CS-on to the connection being open. A driver with
    a real heartbeat can override :attr:`is_command_station_on` for
    tighter reporting.
    """
    # Connection is open (the fixture opened it) -> CS is on.
    assert conn.is_command_station_on is True
    assert shim.powermanager.isCommandStationOn is True
    assert shim.powermanager.getCommandStationPower() is True
    # Track power is separate -- starts UNKNOWN even when CS is on.
    assert shim.powermanager.isCommandStationOn is True
    assert shim.powermanager.isUnknown is True


async def test_command_station_power_off_when_connection_closed(shim: JmriShim, conn) -> None:
    """Closing the connection flips CS-power to False without touching
    the track-power state (which is a separate driver concern)."""
    await conn.close()
    assert shim.powermanager.isCommandStationOn is False
    assert shim.powermanager.getCommandStationPower() is False


async def test_power_state_query_helpers(shim: JmriShim, conn) -> None:
    """Scripts should be able to query 'is the command station on?' via
    a Pythonic property, not just via ``getPower() == powermanager.ON``.
    Missing from JMRI Java's Jython interface; added here to make
    conditional automation less verbose."""
    # Fresh connection state starts UNKNOWN.
    assert shim.powermanager.state == PowerState.UNKNOWN
    assert shim.powermanager.isUnknown is True
    assert shim.powermanager.isOn is False

    # Drive the underlying power manager and verify the shim sees it.
    await conn.power.set_power(PowerState.ON)
    assert shim.powermanager.state == PowerState.ON
    assert shim.powermanager.isOn is True
    assert shim.powermanager.isOff is False
    # ``getPower()`` still works -- Jython-compat surface preserved.
    assert shim.powermanager.getPower() == PowerState.ON

    await conn.power.set_power(PowerState.OFF)
    assert shim.powermanager.isOff is True
    assert shim.powermanager.isOn is False

    await conn.power.set_power(PowerState.IDLE)
    assert shim.powermanager.isIdle is True


def test_shim_constants_alias_module_constants() -> None:
    """Power-shim constants live on the *class* so scripts can do
    ``powermanager.setPower(powermanager.ON)``."""
    from pmri_scripting.shim import _PowerShim  # noqa: PLC0415
    assert _PowerShim.ON == PowerState.ON
    assert _PowerShim.OFF == PowerState.OFF


@pytest.mark.parametrize("manager_name,expected_alias", [
    ("sensors", "provideSensor"),
    ("turnouts", "provideTurnout"),
    ("lights", "provideLight"),
    ("memories", "provideMemory"),
])
def test_pmri_camelcase_aliases_present(
    shim: JmriShim, manager_name: str, expected_alias: str
) -> None:
    mgr = getattr(shim, manager_name)
    assert hasattr(mgr, expected_alias)
    assert hasattr(mgr, "getNamedBeanSet")
