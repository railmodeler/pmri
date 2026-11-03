from __future__ import annotations

from pmri_core.hal.internal import InternalConnection
from pmri_core.plugins import load_connections
from pmri_core.state import SensorState, TurnoutState


async def test_internal_connection_lifecycle() -> None:
    conn = InternalConnection()
    assert conn.prefix == "I"
    assert not conn.is_open
    await conn.open({})
    assert conn.is_open
    await conn.close()
    assert not conn.is_open


async def test_internal_connection_managers_all_present() -> None:
    conn = InternalConnection()
    await conn.open({})
    assert conn.turnouts is not None
    assert conn.sensors is not None
    assert conn.signal_heads is not None
    assert conn.signal_masts is not None
    assert conn.blocks is not None
    assert conn.memories is not None
    assert conn.reporters is not None
    assert conn.lights is not None
    assert conn.routes is not None
    assert conn.power is not None


async def test_command_station_power_defaults_on_when_open() -> None:
    """Base ``is_command_station_on`` is optimistic -- drivers without
    heartbeat detection leave the connection reachable-by-default so
    behaviour matches ``is_open`` exactly. No regression for
    InternalConnection or driver ports that haven't gained detection
    yet."""
    conn = InternalConnection()
    assert conn.is_command_station_on is False   # not yet open
    await conn.open({})
    assert conn.is_command_station_on is True
    await conn.close()
    assert conn.is_command_station_on is False


async def test_set_cs_reachable_emits_signal_on_transition() -> None:
    """``_set_cs_reachable`` fires the ``command_station_power_changed``
    signal exactly on transitions, with the effective on/off value
    (folding in the ``is_open`` gate)."""
    conn = InternalConnection()
    await conn.open({})
    seen: list[bool] = []

    async def listener(on: bool) -> None:
        seen.append(on)

    conn.command_station_power_changed.connect(listener)
    # Same-state calls are no-ops.
    await conn._set_cs_reachable(True)
    assert seen == []
    # Real transition: on -> off.
    await conn._set_cs_reachable(False)
    assert seen == [False]
    assert conn.is_command_station_on is False
    # Repeated off -- no extra emit.
    await conn._set_cs_reachable(False)
    assert seen == [False]
    # Back on.
    await conn._set_cs_reachable(True)
    assert seen == [False, True]
    assert conn.is_command_station_on is True
    await conn.close()


async def test_is_command_station_on_ands_with_is_open() -> None:
    """A driver that has flipped ``_cs_reachable`` True still reports
    Off once the connection closes -- closed always wins."""
    conn = InternalConnection()
    await conn.open({})
    await conn._set_cs_reachable(True)
    assert conn.is_command_station_on is True
    await conn.close()
    # Closed connection is always Off regardless of the last reachable
    # value. This is what protects the UI from showing "On" during a
    # graceful shutdown mid-refresh.
    assert conn.is_command_station_on is False


async def test_internal_connection_provides_beans_with_correct_prefix() -> None:
    conn = InternalConnection()
    await conn.open({})
    t = await conn.turnouts.provide("3")
    s = await conn.sensors.provide("4")
    assert t.system_name == "IT3"
    assert s.system_name == "IS4"


async def test_internal_connection_state_flows_end_to_end() -> None:
    conn = InternalConnection()
    await conn.open({})
    t = await conn.turnouts.provide("3")
    await t.set_commanded_state(TurnoutState.THROWN)
    assert t.known_state == TurnoutState.THROWN

    s = await conn.sensors.provide("4")
    await s._set_known_state(SensorState.ACTIVE)
    assert s.is_active


def test_entry_point_discovers_internal_connection() -> None:
    """The pyproject entry point must make `internal` findable."""
    connections = load_connections()
    assert "internal" in connections
    assert connections["internal"] is InternalConnection
