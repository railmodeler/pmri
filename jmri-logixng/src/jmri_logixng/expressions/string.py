"""String expressions: constants, memory reads, concat, comparisons.

String expressions evaluate to a ``str``. They complement the digital
and analog families and let LogixNG programs describe conditions like
"memory IM1 contains 'foo'" or emit derived text into a memory bean via
:class:`~jmri_logixng.actions.digital.SetMemoryFromExpression`.

:class:`StringCompare` returns ``Expression[bool]`` so it plugs into the
existing boolean combinators (``And`` / ``Or``) or ``IfThenElse``.
"""
from __future__ import annotations

import re
from typing import Any, ClassVar

from jmri_logixng.context import ExecutionContext
from jmri_logixng.registry import register
from jmri_logixng.tree import Expression


def _coerce_string(value: Any) -> str:
    """Stringify a memory value. None -> empty string; everything else
    goes through ``str()``. Matches the ``LogAction`` treatment."""
    if value is None:
        return ""
    return str(value)


@register
class ConstantString(Expression[str]):
    """Always evaluates to *value*."""

    type_tag: ClassVar[str] = "expr:const-str"

    def __init__(
        self,
        *,
        value: str,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.value = str(value)

    async def evaluate(self, ctx: ExecutionContext) -> str:
        return self.value

    def extra_fields(self) -> dict[str, Any]:
        return {"value": self.value}


@register
class MemoryValueString(Expression[str]):
    """Read memory bean *memory* and stringify its value.

    Missing bean -> empty string with a debug log line.
    """

    type_tag: ClassVar[str] = "expr:memory-value-str"

    def __init__(
        self,
        *,
        memory: str,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.memory = memory

    async def evaluate(self, ctx: ExecutionContext) -> str:
        bean = ctx.connection.memories.get(self.memory)
        if bean is None:
            ctx.log.warning("MemoryValueString: no memory %r", self.memory)
            return ""
        return _coerce_string(bean.value)

    def extra_fields(self) -> dict[str, Any]:
        return {"memory": self.memory}


@register
class StringConcat(Expression[str]):
    """Concatenate the string values of every child, with *separator*
    inserted between adjacent children."""

    type_tag: ClassVar[str] = "expr:string-concat"

    def __init__(
        self,
        children: list[Expression[str]],
        *,
        separator: str = "",
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.children = list(children)
        self.separator = separator

    async def evaluate(self, ctx: ExecutionContext) -> str:
        parts = [await c.evaluate(ctx) for c in self.children]
        return self.separator.join(parts)

    def extra_fields(self) -> dict[str, Any]:
        return {
            "separator": self.separator,
            "children": [c.to_dict() for c in self.children],
        }


def _op_regex(a: str, b: str, case_insensitive: bool) -> bool:
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        return re.search(b, a, flags=flags) is not None
    except re.error:
        return False


_STRING_COMPARE_OPS: dict[str, Any] = {
    "eq": lambda a, b, ci: (a.lower() == b.lower()) if ci else (a == b),
    "ne": lambda a, b, ci: (a.lower() != b.lower()) if ci else (a != b),
    "contains": lambda a, b, ci: (b.lower() in a.lower()) if ci else (b in a),
    "startswith": lambda a, b, ci: (
        a.lower().startswith(b.lower()) if ci else a.startswith(b)
    ),
    "endswith": lambda a, b, ci: (
        a.lower().endswith(b.lower()) if ci else a.endswith(b)
    ),
    "regex": _op_regex,
}


@register
class StringCompare(Expression[bool]):
    """Compare two string expressions; returns bool.

    *op* is one of ``eq``, ``ne``, ``contains``, ``startswith``,
    ``endswith``, ``regex``. Set *case_insensitive* to compare
    case-blind. For ``regex``, the *right* side is a Python regex and
    invalid patterns evaluate to False.
    """

    type_tag: ClassVar[str] = "expr:string-compare"

    def __init__(
        self,
        *,
        op: str,
        left: Expression[str],
        right: Expression[str],
        case_insensitive: bool = False,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        if op not in _STRING_COMPARE_OPS:
            raise ValueError(
                f"unknown string compare op {op!r}; "
                f"expected one of {sorted(_STRING_COMPARE_OPS)}"
            )
        super().__init__(user_name=user_name, node_id=node_id)
        self.op = op
        self.left = left
        self.right = right
        self.case_insensitive = case_insensitive

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        a = await self.left.evaluate(ctx)
        b = await self.right.evaluate(ctx)
        return bool(_STRING_COMPARE_OPS[self.op](a, b, self.case_insensitive))

    def extra_fields(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "case_insensitive": self.case_insensitive,
        }
