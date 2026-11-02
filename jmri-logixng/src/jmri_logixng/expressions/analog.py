"""Analog (numeric) expressions: constants, memory reads, arithmetic, comparisons.

Analog expressions evaluate to a ``float``. They complement the digital
(boolean) expressions in :mod:`~jmri_logixng.expressions.digital` and let
LogixNG programs describe conditions like "memory IM1 value > 5.0" or
"turnout count / total > 0.5".

The comparison node (:class:`NumericCompare`) yields ``Expression[bool]``
so it can be dropped straight into a boolean combinator (``And`` / ``Or``)
or into ``IfThenElse.condition``.
"""
from __future__ import annotations

from typing import Any, ClassVar

from jmri_logixng.context import ExecutionContext
from jmri_logixng.registry import register
from jmri_logixng.tree import Expression


def _coerce_number(value: Any) -> float:
    """Best-effort coercion of a memory value to float.

    Booleans, ints, and float-parseable strings all coerce; anything else
    returns 0.0. This matches JMRI Java's forgiving numeric handling in
    the analog logix -- the alternative (raising) would abort a
    conditional mid-flight over a stringly-typed memory value.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


@register
class ConstantNumber(Expression[float]):
    """Always evaluates to *value*."""

    type_tag: ClassVar[str] = "expr:const-num"

    def __init__(
        self,
        *,
        value: float,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.value = float(value)

    async def evaluate(self, ctx: ExecutionContext) -> float:
        return self.value

    def extra_fields(self) -> dict[str, Any]:
        return {"value": self.value}


@register
class MemoryValueNumber(Expression[float]):
    """Read memory bean *memory* and coerce its value to a float.

    Missing bean or non-numeric value -> 0.0 with a debug log line.
    """

    type_tag: ClassVar[str] = "expr:memory-value-num"

    def __init__(
        self,
        *,
        memory: str,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.memory = memory

    async def evaluate(self, ctx: ExecutionContext) -> float:
        bean = ctx.connection.memories.get(self.memory)
        if bean is None:
            ctx.log.warning("MemoryValueNumber: no memory %r", self.memory)
            return 0.0
        return _coerce_number(bean.value)

    def extra_fields(self) -> dict[str, Any]:
        return {"memory": self.memory}


_NUMERIC_OPS: dict[str, Any] = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / b if b != 0 else 0.0,
    "mod": lambda a, b: a % b if b != 0 else 0.0,
    "min": min,
    "max": max,
}


@register
class NumericOp(Expression[float]):
    """Binary numeric operator over two analog children.

    *op* is one of ``add``, ``sub``, ``mul``, ``div``, ``mod``, ``min``,
    ``max``. Division and modulo by zero return 0.0 rather than raising --
    matches JMRI Java's defensive analog logic which treats ``x/0`` as a
    non-fatal "no result".
    """

    type_tag: ClassVar[str] = "expr:num-op"

    def __init__(
        self,
        *,
        op: str,
        left: Expression[float],
        right: Expression[float],
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        if op not in _NUMERIC_OPS:
            raise ValueError(
                f"unknown numeric op {op!r}; "
                f"expected one of {sorted(_NUMERIC_OPS)}"
            )
        super().__init__(user_name=user_name, node_id=node_id)
        self.op = op
        self.left = left
        self.right = right

    async def evaluate(self, ctx: ExecutionContext) -> float:
        a = await self.left.evaluate(ctx)
        b = await self.right.evaluate(ctx)
        return float(_NUMERIC_OPS[self.op](a, b))

    def extra_fields(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }


_COMPARE_OPS: dict[str, Any] = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "ge": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
}


@register
class NumericCompare(Expression[bool]):
    """Compare two analog expressions; returns bool.

    *op* is one of ``lt`` / ``le`` / ``eq`` / ``ne`` / ``ge`` / ``gt``.
    Equality is exact-float; wrap either side in a rounding op if you
    need tolerance.
    """

    type_tag: ClassVar[str] = "expr:num-compare"

    def __init__(
        self,
        *,
        op: str,
        left: Expression[float],
        right: Expression[float],
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        if op not in _COMPARE_OPS:
            raise ValueError(
                f"unknown compare op {op!r}; "
                f"expected one of {sorted(_COMPARE_OPS)}"
            )
        super().__init__(user_name=user_name, node_id=node_id)
        self.op = op
        self.left = left
        self.right = right

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        a = await self.left.evaluate(ctx)
        b = await self.right.evaluate(ctx)
        return bool(_COMPARE_OPS[self.op](a, b))

    def extra_fields(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }
