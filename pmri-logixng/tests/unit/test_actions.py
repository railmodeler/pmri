"""Action tests against an InternalConnection."""
from __future__ import annotations

import asyncio
import logging

import pytest
from pmri_core.state import LightState, SensorState, TurnoutState

from pmri_logixng import (
    ConstantBool,
    Delay,
    ExecutionContext,
    IfThenElse,
    LogAction,
    Sequence,
    SetLightState,
    SetMemory,
    SetSensorState,
    SetTurnoutState,
)


async def test_set_turnout_state(ctx: ExecutionContext) -> None:
    t = await ctx.connection.turnouts.provide("IT1")
    await SetTurnoutState(turnout="IT1", state=TurnoutState.CLOSED).execute(ctx)
    assert t.commanded_state == TurnoutState.CLOSED


async def test_set_turnout_missing_bean_is_logged_not_raised(ctx: ExecutionContext) -> None:
    await SetTurnoutState(turnout="IT999", state=TurnoutState.CLOSED).execute(ctx)
    # No exception -- just a warning. Sanity-check by re-running:
    await SetTurnoutState(turnout="IT999", state=TurnoutState.THROWN).execute(ctx)


async def test_set_sensor_state(ctx: ExecutionContext) -> None:
    s = await ctx.connection.sensors.provide("IS1")
    await SetSensorState(sensor="IS1", state=SensorState.ACTIVE).execute(ctx)
    assert s.known_state == SensorState.ACTIVE


async def test_set_light_state(ctx: ExecutionContext) -> None:
    light = await ctx.connection.lights.provide("IL1")
    await SetLightState(light="IL1", state=LightState.ON).execute(ctx)
    assert light.state == LightState.ON


async def test_set_memory_value(ctx: ExecutionContext) -> None:
    m = await ctx.connection.memories.provide("IM1")
    await SetMemory(memory="IM1", value={"loco": "3601"}).execute(ctx)
    assert m.value == {"loco": "3601"}


async def test_log_action_emits(ctx: ExecutionContext, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        await LogAction(message="hello world").execute(ctx)
    assert any("hello world" in r.message for r in caplog.records)


async def test_log_action_respects_level(ctx: ExecutionContext, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        await LogAction(message="warn-me", level="warning").execute(ctx)
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_if_then_runs_then_when_true(ctx: ExecutionContext) -> None:
    await ctx.connection.turnouts.provide("IT2")
    await IfThenElse(
        condition=ConstantBool(value=True),
        then_action=SetTurnoutState(turnout="IT2", state=TurnoutState.CLOSED),
        else_action=SetTurnoutState(turnout="IT2", state=TurnoutState.THROWN),
    ).execute(ctx)
    assert ctx.connection.turnouts.get("IT2").commanded_state == TurnoutState.CLOSED


async def test_if_then_runs_else_when_false(ctx: ExecutionContext) -> None:
    await ctx.connection.turnouts.provide("IT3")
    await IfThenElse(
        condition=ConstantBool(value=False),
        then_action=SetTurnoutState(turnout="IT3", state=TurnoutState.CLOSED),
        else_action=SetTurnoutState(turnout="IT3", state=TurnoutState.THROWN),
    ).execute(ctx)
    assert ctx.connection.turnouts.get("IT3").commanded_state == TurnoutState.THROWN


async def test_if_then_without_else_is_noop_when_false(ctx: ExecutionContext) -> None:
    t = await ctx.connection.turnouts.provide("IT4")
    # Initial commanded_state is UNKNOWN -- confirm it's unchanged.
    await IfThenElse(
        condition=ConstantBool(value=False),
        then_action=SetTurnoutState(turnout="IT4", state=TurnoutState.CLOSED),
    ).execute(ctx)
    assert t.commanded_state == TurnoutState.UNKNOWN


async def test_sequence_runs_in_order(ctx: ExecutionContext) -> None:
    await ctx.connection.memories.provide("IM2")
    seq = Sequence([
        SetMemory(memory="IM2", value="a"),
        SetMemory(memory="IM2", value="b"),
        SetMemory(memory="IM2", value="c"),
    ])
    await seq.execute(ctx)
    assert ctx.connection.memories.get("IM2").value == "c"


async def test_delay_sleeps(ctx: ExecutionContext) -> None:
    start = asyncio.get_event_loop().time()
    await Delay(seconds=0.02).execute(ctx)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.015


def test_delay_rejects_negative() -> None:
    with pytest.raises(ValueError):
        Delay(seconds=-1)
