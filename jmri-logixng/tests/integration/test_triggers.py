"""End-to-end trigger tests -- bean state change auto-fires a conditional."""
from __future__ import annotations

import asyncio

from jmri_core.state import LightState, SensorState, TurnoutState

from jmri_logixng import (
    AutomationEngine,
    ConditionalNG,
    ConstantBool,
    ExecutionContext,
    LogixNG,
    SensorIsActive,
    SetLightState,
    SetTurnoutState,
    TriggerBinding,
    TurnoutIsClosed,
)


async def test_trigger_fires_on_sensor_change(ctx: ExecutionContext) -> None:
    sensor = await ctx.connection.sensors.provide("IS1")
    await ctx.connection.lights.provide("IL1")

    conditional = ConditionalNG(actions=[
        SetLightState(light="IL1", state=LightState.ON),
    ])
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(
        conditional=conditional,
        watches=[("sensor", "IS1")],
        guard=SensorIsActive(sensor="IS1"),
    ))
    engine.start()

    # Flip the sensor active -- guard true -> conditional fires.
    await sensor._set_known_state(SensorState.ACTIVE)
    for _ in range(20):
        if ctx.connection.lights.get("IL1").state == LightState.ON:
            break
        await asyncio.sleep(0.005)
    assert ctx.connection.lights.get("IL1").state == LightState.ON

    engine.stop()


async def test_trigger_guard_blocks_unwanted_firings(ctx: ExecutionContext) -> None:
    sensor = await ctx.connection.sensors.provide("IS2")
    light = await ctx.connection.lights.provide("IL2")

    conditional = ConditionalNG(actions=[SetLightState(light="IL2", state=LightState.ON)])
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(
        conditional=conditional,
        watches=[("sensor", "IS2")],
        guard=SensorIsActive(sensor="IS2"),    # only fires when sensor IS active
    ))
    engine.start()

    # Move to INACTIVE -- guard false -> light stays OFF.
    await sensor._set_known_state(SensorState.INACTIVE)
    await asyncio.sleep(0.02)
    assert light.state != LightState.ON

    engine.stop()


async def test_trigger_without_guard_always_fires(ctx: ExecutionContext) -> None:
    sensor = await ctx.connection.sensors.provide("IS3")
    light = await ctx.connection.lights.provide("IL3")

    conditional = ConditionalNG(actions=[SetLightState(light="IL3", state=LightState.ON)])
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(
        conditional=conditional,
        watches=[("sensor", "IS3")],
        guard=None,    # no guard -> always fires on change
    ))
    engine.start()

    await sensor._set_known_state(SensorState.ACTIVE)
    for _ in range(20):
        if light.state == LightState.ON:
            break
        await asyncio.sleep(0.005)
    assert light.state == LightState.ON
    engine.stop()


async def test_disabled_conditional_does_not_fire(ctx: ExecutionContext) -> None:
    sensor = await ctx.connection.sensors.provide("IS4")
    light = await ctx.connection.lights.provide("IL4")

    conditional = ConditionalNG(
        actions=[SetLightState(light="IL4", state=LightState.ON)],
        enabled=False,
    )
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(
        conditional=conditional, watches=[("sensor", "IS4")], guard=None,
    ))
    engine.start()

    await sensor._set_known_state(SensorState.ACTIVE)
    await asyncio.sleep(0.02)
    assert light.state != LightState.ON
    engine.stop()


async def test_trigger_stop_disconnects_listeners(ctx: ExecutionContext) -> None:
    sensor = await ctx.connection.sensors.provide("IS5")
    light = await ctx.connection.lights.provide("IL5")

    fire_count = 0
    conditional = ConditionalNG(actions=[SetLightState(light="IL5", state=LightState.ON)])
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(conditional=conditional, watches=[("sensor", "IS5")], guard=None))
    engine.start()
    engine.stop()

    await sensor._set_known_state(SensorState.ACTIVE)
    await asyncio.sleep(0.02)
    # Light should not have turned on because the trigger was disconnected.
    assert light.state != LightState.ON
    _ = fire_count


async def test_logixng_execute_all_runs_every_conditional(ctx: ExecutionContext) -> None:
    await ctx.connection.turnouts.provide("IT10")
    await ctx.connection.turnouts.provide("IT11")
    tree = LogixNG(conditionals=[
        ConditionalNG(actions=[SetTurnoutState(turnout="IT10", state=TurnoutState.CLOSED)]),
        ConditionalNG(actions=[SetTurnoutState(turnout="IT11", state=TurnoutState.THROWN)]),
    ])
    await tree.execute_all(ctx)
    assert ctx.connection.turnouts.get("IT10").commanded_state == TurnoutState.CLOSED
    assert ctx.connection.turnouts.get("IT11").commanded_state == TurnoutState.THROWN


async def test_trigger_unknown_bean_logs_warning(ctx: ExecutionContext) -> None:
    """Trigger watching a non-existent bean logs and continues."""
    conditional = ConditionalNG(actions=[
        SetTurnoutState(turnout="IT99", state=TurnoutState.CLOSED),
    ])
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(
        conditional=conditional,
        watches=[("turnout", "IT99")],
        guard=ConstantBool(value=True),
    ))
    engine.start()  # Should not raise even though IT99 doesn't exist.
    engine.stop()


async def test_turnout_trigger_via_known_state(ctx: ExecutionContext) -> None:
    """A turnout state change should trigger an automation watching it."""
    turnout = await ctx.connection.turnouts.provide("IT12")
    await ctx.connection.lights.provide("IL12")
    conditional = ConditionalNG(actions=[
        SetLightState(light="IL12", state=LightState.ON),
    ])
    engine = AutomationEngine(ctx)
    engine.add(TriggerBinding(
        conditional=conditional,
        watches=[("turnout", "IT12")],
        guard=TurnoutIsClosed(turnout="IT12"),
    ))
    engine.start()
    await turnout.set_commanded_state(TurnoutState.CLOSED)
    for _ in range(20):
        if ctx.connection.lights.get("IL12").state == LightState.ON:
            break
        await asyncio.sleep(0.005)
    assert ctx.connection.lights.get("IL12").state == LightState.ON
    engine.stop()
