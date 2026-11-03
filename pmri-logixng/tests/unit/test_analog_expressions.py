"""Analog (numeric) expressions."""
from __future__ import annotations

import pytest

from pmri_logixng import (
    ConstantNumber,
    ExecutionContext,
    MemoryValueNumber,
    NumericCompare,
    NumericOp,
    from_yaml,
    to_yaml,
)


async def test_constant_number(ctx: ExecutionContext) -> None:
    assert await ConstantNumber(value=3.5).evaluate(ctx) == 3.5


async def test_memory_value_number_coerces(ctx: ExecutionContext) -> None:
    m = await ctx.connection.memories.provide("IM10")
    await m.set_value(42)
    assert await MemoryValueNumber(memory="IM10").evaluate(ctx) == 42.0
    await m.set_value("3.14")
    assert await MemoryValueNumber(memory="IM10").evaluate(ctx) == 3.14
    await m.set_value("not a number")
    assert await MemoryValueNumber(memory="IM10").evaluate(ctx) == 0.0
    await m.set_value(True)
    assert await MemoryValueNumber(memory="IM10").evaluate(ctx) == 1.0


async def test_memory_value_number_missing(ctx: ExecutionContext) -> None:
    assert await MemoryValueNumber(memory="IM999").evaluate(ctx) == 0.0


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("add", 2.0, 3.0, 5.0),
        ("sub", 5.0, 3.0, 2.0),
        ("mul", 4.0, 2.5, 10.0),
        ("div", 10.0, 4.0, 2.5),
        ("div", 1.0, 0.0, 0.0),   # div-by-zero -> 0
        ("mod", 10.0, 3.0, 1.0),
        ("mod", 1.0, 0.0, 0.0),   # mod-by-zero -> 0
        ("min", 4.0, 7.0, 4.0),
        ("max", 4.0, 7.0, 7.0),
    ],
)
async def test_numeric_op(
    ctx: ExecutionContext, op: str, a: float, b: float, expected: float,
) -> None:
    expr = NumericOp(
        op=op, left=ConstantNumber(value=a), right=ConstantNumber(value=b),
    )
    assert await expr.evaluate(ctx) == expected


def test_numeric_op_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown numeric op"):
        NumericOp(
            op="bogus",
            left=ConstantNumber(value=1),
            right=ConstantNumber(value=1),
        )


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("lt", 1.0, 2.0, True),
        ("lt", 2.0, 2.0, False),
        ("le", 2.0, 2.0, True),
        ("eq", 3.0, 3.0, True),
        ("ne", 3.0, 4.0, True),
        ("ge", 5.0, 5.0, True),
        ("gt", 6.0, 5.0, True),
    ],
)
async def test_numeric_compare(
    ctx: ExecutionContext, op: str, a: float, b: float, expected: bool,
) -> None:
    expr = NumericCompare(
        op=op, left=ConstantNumber(value=a), right=ConstantNumber(value=b),
    )
    assert await expr.evaluate(ctx) is expected


async def test_numeric_op_via_memory(ctx: ExecutionContext) -> None:
    """Real use: divide two memory values, compare against a constant."""
    m1 = await ctx.connection.memories.provide("IM_A")
    m2 = await ctx.connection.memories.provide("IM_B")
    await m1.set_value(30)
    await m2.set_value(4)
    ratio = NumericOp(
        op="div",
        left=MemoryValueNumber(memory="IM_A"),
        right=MemoryValueNumber(memory="IM_B"),
    )
    assert await ratio.evaluate(ctx) == 7.5
    threshold = NumericCompare(
        op="gt", left=ratio, right=ConstantNumber(value=5.0),
    )
    assert await threshold.evaluate(ctx) is True


def test_analog_round_trip_yaml() -> None:
    """A tree with analog + digital nodes round-trips through YAML."""
    tree = NumericCompare(
        op="ge",
        left=NumericOp(
            op="add",
            left=MemoryValueNumber(memory="IM1"),
            right=ConstantNumber(value=1.0),
        ),
        right=ConstantNumber(value=10.0),
    )
    text = to_yaml(tree)
    restored = from_yaml(text)
    assert isinstance(restored, NumericCompare)
    assert restored.op == "ge"
    assert isinstance(restored.left, NumericOp)
    assert restored.left.op == "add"
