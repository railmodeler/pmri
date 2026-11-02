"""Digital (boolean) expressions over layout state + boolean algebra."""
from __future__ import annotations

from typing import Any, ClassVar

from jmri_core.state import LightState, SensorState, TurnoutState

from jmri_logixng.context import ExecutionContext
from jmri_logixng.registry import register
from jmri_logixng.tree import Expression


@register
class ConstantBool(Expression[bool]):
    """Always evaluates to ``value``. Used for testing + as the leaf of trees."""

    type_tag: ClassVar[str] = "expr:const-bool"

    def __init__(
        self, *, value: bool, user_name: str = "", node_id: str | None = None
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.value = value

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        return self.value

    def extra_fields(self) -> dict[str, Any]:
        return {"value": self.value}


@register
class SensorIsActive(Expression[bool]):
    """True when sensor *sensor* has state ACTIVE."""

    type_tag: ClassVar[str] = "expr:sensor-active"

    def __init__(
        self, *, sensor: str, user_name: str = "", node_id: str | None = None
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.sensor = sensor

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        bean = ctx.connection.sensors.get(self.sensor)
        if bean is None:
            ctx.log.warning("SensorIsActive: no sensor %r", self.sensor)
            return False
        return bean.known_state == SensorState.ACTIVE

    def extra_fields(self) -> dict[str, Any]:
        return {"sensor": self.sensor}


@register
class TurnoutIsClosed(Expression[bool]):
    """True when turnout *turnout* has known state CLOSED."""

    type_tag: ClassVar[str] = "expr:turnout-closed"

    def __init__(
        self, *, turnout: str, user_name: str = "", node_id: str | None = None
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.turnout = turnout

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        bean = ctx.connection.turnouts.get(self.turnout)
        if bean is None:
            ctx.log.warning("TurnoutIsClosed: no turnout %r", self.turnout)
            return False
        return bean.known_state == TurnoutState.CLOSED

    def extra_fields(self) -> dict[str, Any]:
        return {"turnout": self.turnout}


@register
class LightIsOn(Expression[bool]):
    """True when light *light* has state ON."""

    type_tag: ClassVar[str] = "expr:light-on"

    def __init__(
        self, *, light: str, user_name: str = "", node_id: str | None = None
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.light = light

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        bean = ctx.connection.lights.get(self.light)
        if bean is None:
            ctx.log.warning("LightIsOn: no light %r", self.light)
            return False
        return bean.state == LightState.ON

    def extra_fields(self) -> dict[str, Any]:
        return {"light": self.light}


@register
class MemoryEquals(Expression[bool]):
    """True when memory *memory*'s value compares equal to *value*."""

    type_tag: ClassVar[str] = "expr:memory-equals"

    def __init__(
        self,
        *,
        memory: str,
        value: Any,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.memory = memory
        self.value = value

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        bean = ctx.connection.memories.get(self.memory)
        if bean is None:
            ctx.log.warning("MemoryEquals: no memory %r", self.memory)
            return False
        return bool(bean.value == self.value)

    def extra_fields(self) -> dict[str, Any]:
        return {"memory": self.memory, "value": self.value}


# --- boolean combinators ---------------------------------------------------


@register
class And(Expression[bool]):
    """True iff every child expression evaluates true. Short-circuits."""

    type_tag: ClassVar[str] = "expr:and"

    def __init__(
        self,
        children: list[Expression[bool]],
        *,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.children = list(children)

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        for child in self.children:
            if not await child.evaluate(ctx):
                return False
        return True

    def extra_fields(self) -> dict[str, Any]:
        return {"children": [c.to_dict() for c in self.children]}


@register
class Or(Expression[bool]):
    """True if any child expression evaluates true. Short-circuits."""

    type_tag: ClassVar[str] = "expr:or"

    def __init__(
        self,
        children: list[Expression[bool]],
        *,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.children = list(children)

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        for child in self.children:
            if await child.evaluate(ctx):
                return True
        return False

    def extra_fields(self) -> dict[str, Any]:
        return {"children": [c.to_dict() for c in self.children]}


@register
class Not(Expression[bool]):
    """Negates its child expression."""

    type_tag: ClassVar[str] = "expr:not"

    def __init__(
        self,
        child: Expression[bool],
        *,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.child = child

    async def evaluate(self, ctx: ExecutionContext) -> bool:
        return not await self.child.evaluate(ctx)

    def extra_fields(self) -> dict[str, Any]:
        return {"child": self.child.to_dict()}
