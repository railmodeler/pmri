"""String expressions + SetMemoryFromExpression action."""
from __future__ import annotations

import pytest

from jmri_logixng import (
    ConstantNumber,
    ConstantString,
    ExecutionContext,
    MemoryValueNumber,
    MemoryValueString,
    NumericOp,
    SetMemoryFromExpression,
    StringCompare,
    StringConcat,
    from_yaml,
    to_yaml,
)


async def test_constant_string(ctx: ExecutionContext) -> None:
    assert await ConstantString(value="hello").evaluate(ctx) == "hello"


async def test_memory_value_string(ctx: ExecutionContext) -> None:
    m = await ctx.connection.memories.provide("IM_S")
    await m.set_value("loco-3601")
    assert await MemoryValueString(memory="IM_S").evaluate(ctx) == "loco-3601"
    await m.set_value(42)
    assert await MemoryValueString(memory="IM_S").evaluate(ctx) == "42"
    await m.set_value(None)
    assert await MemoryValueString(memory="IM_S").evaluate(ctx) == ""


async def test_memory_value_string_missing(ctx: ExecutionContext) -> None:
    assert await MemoryValueString(memory="IM_missing").evaluate(ctx) == ""


async def test_string_concat_no_separator(ctx: ExecutionContext) -> None:
    expr = StringConcat([
        ConstantString(value="foo"),
        ConstantString(value="bar"),
    ])
    assert await expr.evaluate(ctx) == "foobar"


async def test_string_concat_with_separator(ctx: ExecutionContext) -> None:
    expr = StringConcat(
        [ConstantString(value="a"), ConstantString(value="b"),
         ConstantString(value="c")],
        separator="-",
    )
    assert await expr.evaluate(ctx) == "a-b-c"


@pytest.mark.parametrize(
    ("op", "a", "b", "expected", "ci"),
    [
        ("eq", "abc", "abc", True, False),
        ("eq", "abc", "ABC", False, False),
        ("eq", "abc", "ABC", True, True),
        ("ne", "abc", "abd", True, False),
        ("contains", "hello world", "wor", True, False),
        ("contains", "hello world", "WOR", False, False),
        ("contains", "hello world", "WOR", True, True),
        ("startswith", "abcdef", "abc", True, False),
        ("endswith", "abcdef", "def", True, False),
        ("regex", "abc123", r"[a-z]+\d+", True, False),
        ("regex", "abc", "[unclosed", False, False),   # bad regex -> False
    ],
)
async def test_string_compare(
    ctx: ExecutionContext, op: str, a: str, b: str, expected: bool, ci: bool,
) -> None:
    expr = StringCompare(
        op=op,
        left=ConstantString(value=a),
        right=ConstantString(value=b),
        case_insensitive=ci,
    )
    assert await expr.evaluate(ctx) is expected


def test_string_compare_rejects_unknown_op() -> None:
    with pytest.raises(ValueError, match="unknown string compare op"):
        StringCompare(
            op="bogus",
            left=ConstantString(value=""),
            right=ConstantString(value=""),
        )


async def test_set_memory_from_expression_string(
    ctx: ExecutionContext,
) -> None:
    src = await ctx.connection.memories.provide("IM_SRC")
    dst = await ctx.connection.memories.provide("IM_DST")
    await src.set_value("loco-3601")

    action = SetMemoryFromExpression(
        memory="IM_DST",
        source=StringConcat(
            [ConstantString(value="active: "),
             MemoryValueString(memory="IM_SRC")],
        ),
    )
    await action.execute(ctx)
    assert dst.value == "active: loco-3601"


async def test_set_memory_from_expression_number(
    ctx: ExecutionContext,
) -> None:
    src = await ctx.connection.memories.provide("IM_N")
    dst = await ctx.connection.memories.provide("IM_OUT")
    await src.set_value(10)

    action = SetMemoryFromExpression(
        memory="IM_OUT",
        source=NumericOp(
            op="mul",
            left=MemoryValueNumber(memory="IM_N"),
            right=ConstantNumber(value=2.5),
        ),
    )
    await action.execute(ctx)
    assert dst.value == 25.0


async def test_set_memory_from_expression_missing_memory(
    ctx: ExecutionContext,
) -> None:
    """Missing target memory logs but doesn't raise -- matches SetMemory."""
    action = SetMemoryFromExpression(
        memory="IM_absent",
        source=ConstantString(value="whatever"),
    )
    await action.execute(ctx)   # must not raise


def test_string_round_trip_yaml() -> None:
    tree = SetMemoryFromExpression(
        memory="IM_DST",
        source=StringConcat(
            [ConstantString(value="hello"), ConstantString(value="world")],
            separator=" ",
        ),
    )
    text = to_yaml(tree)
    restored = from_yaml(text)
    assert isinstance(restored, SetMemoryFromExpression)
    assert restored.memory == "IM_DST"
    assert isinstance(restored.source, StringConcat)
    assert restored.source.separator == " "
