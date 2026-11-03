from __future__ import annotations

from pmri_core.state import LightState, SensorState, TurnoutState

from pmri_logixng import (
    And,
    ConstantBool,
    ExecutionContext,
    LightIsOn,
    MemoryEquals,
    Not,
    Or,
    SensorIsActive,
    TurnoutIsClosed,
)


async def test_constant_bool(ctx: ExecutionContext) -> None:
    assert await ConstantBool(value=True).evaluate(ctx) is True
    assert await ConstantBool(value=False).evaluate(ctx) is False


async def test_sensor_is_active(ctx: ExecutionContext) -> None:
    s = await ctx.connection.sensors.provide("IS5")
    expr = SensorIsActive(sensor="IS5")
    assert await expr.evaluate(ctx) is False
    await s._set_known_state(SensorState.ACTIVE)
    assert await expr.evaluate(ctx) is True


async def test_sensor_missing_evaluates_false(ctx: ExecutionContext) -> None:
    assert await SensorIsActive(sensor="IS999").evaluate(ctx) is False


async def test_turnout_is_closed(ctx: ExecutionContext) -> None:
    t = await ctx.connection.turnouts.provide("IT5")
    expr = TurnoutIsClosed(turnout="IT5")
    assert await expr.evaluate(ctx) is False
    await t.set_commanded_state(TurnoutState.CLOSED)
    assert await expr.evaluate(ctx) is True


async def test_light_is_on(ctx: ExecutionContext) -> None:
    light = await ctx.connection.lights.provide("IL2")
    expr = LightIsOn(light="IL2")
    assert await expr.evaluate(ctx) is False
    await light.set_state(LightState.ON)
    assert await expr.evaluate(ctx) is True


async def test_memory_equals(ctx: ExecutionContext) -> None:
    m = await ctx.connection.memories.provide("IM5")
    await m.set_value("loco-3601")
    assert await MemoryEquals(memory="IM5", value="loco-3601").evaluate(ctx) is True
    assert await MemoryEquals(memory="IM5", value="other").evaluate(ctx) is False


async def test_and_combinator(ctx: ExecutionContext) -> None:
    t = ConstantBool(value=True)
    f = ConstantBool(value=False)
    assert await And([t, t, t]).evaluate(ctx) is True
    assert await And([t, f, t]).evaluate(ctx) is False
    assert await And([]).evaluate(ctx) is True   # vacuous truth


async def test_or_combinator(ctx: ExecutionContext) -> None:
    t = ConstantBool(value=True)
    f = ConstantBool(value=False)
    assert await Or([f, f, t]).evaluate(ctx) is True
    assert await Or([f, f, f]).evaluate(ctx) is False
    assert await Or([]).evaluate(ctx) is False


async def test_not_combinator(ctx: ExecutionContext) -> None:
    assert await Not(ConstantBool(value=True)).evaluate(ctx) is False
    assert await Not(ConstantBool(value=False)).evaluate(ctx) is True


async def test_and_short_circuits(ctx: ExecutionContext) -> None:
    """And should stop at the first False -- no side-effects after."""
    evaluated: list[str] = []

    class Tap(ConstantBool):
        def __init__(self, *, value: bool, label: str) -> None:
            super().__init__(value=value)
            self._label = label

        async def evaluate(self, c: ExecutionContext) -> bool:
            evaluated.append(self._label)
            return self.value

    expr = And([Tap(value=False, label="a"), Tap(value=True, label="b")])
    assert await expr.evaluate(ctx) is False
    assert evaluated == ["a"]   # b never ran
