"""Digital actions -- set bean state, conditionally execute, log.

Each action holds a serializable description of *what* to do; the actual
lookup of the bean happens at :meth:`execute` time against the active
connection in the :class:`ExecutionContext`. This decouples authoring
from binding: the same automation file can run against
``InternalConnection`` in tests and a real LocoNet/NCE connection in
production.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pmri_core.state import LightState, SensorState, TurnoutState

from pmri_logixng.context import ExecutionContext
from pmri_logixng.registry import register
from pmri_logixng.tree import Action, Expression

if TYPE_CHECKING:
    pass


@register
class SetTurnoutState(Action):
    """Set a turnout's commanded state."""

    type_tag: ClassVar[str] = "action:set-turnout"

    def __init__(
        self,
        *,
        turnout: str,
        state: TurnoutState,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.turnout = turnout
        self.state = state

    async def execute(self, ctx: ExecutionContext) -> None:
        bean = ctx.connection.turnouts.get(self.turnout)
        if bean is None:
            ctx.log.warning("SetTurnoutState: no turnout %r", self.turnout)
            return
        ctx.emit_trace(f"set turnout {self.turnout} -> {self.state.name}")
        await bean.set_commanded_state(self.state)

    def extra_fields(self) -> dict[str, Any]:
        return {"turnout": self.turnout, "state": self.state.name}


@register
class SetSensorState(Action):
    """Set a sensor's known state (mostly for tests/simulation)."""

    type_tag: ClassVar[str] = "action:set-sensor"

    def __init__(
        self,
        *,
        sensor: str,
        state: SensorState,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.sensor = sensor
        self.state = state

    async def execute(self, ctx: ExecutionContext) -> None:
        bean = ctx.connection.sensors.get(self.sensor)
        if bean is None:
            ctx.log.warning("SetSensorState: no sensor %r", self.sensor)
            return
        ctx.emit_trace(f"set sensor {self.sensor} -> {self.state.name}")
        await bean._set_known_state(self.state)

    def extra_fields(self) -> dict[str, Any]:
        return {"sensor": self.sensor, "state": self.state.name}


@register
class SetLightState(Action):
    """Set a light's state."""

    type_tag: ClassVar[str] = "action:set-light"

    def __init__(
        self,
        *,
        light: str,
        state: LightState,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.light = light
        self.state = state

    async def execute(self, ctx: ExecutionContext) -> None:
        bean = ctx.connection.lights.get(self.light)
        if bean is None:
            ctx.log.warning("SetLightState: no light %r", self.light)
            return
        ctx.emit_trace(f"set light {self.light} -> {self.state.name}")
        await bean.set_state(self.state)

    def extra_fields(self) -> dict[str, Any]:
        return {"light": self.light, "state": self.state.name}


@register
class SetMemory(Action):
    """Write a value into a memory bean."""

    type_tag: ClassVar[str] = "action:set-memory"

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

    async def execute(self, ctx: ExecutionContext) -> None:
        bean = ctx.connection.memories.get(self.memory)
        if bean is None:
            ctx.log.warning("SetMemory: no memory %r", self.memory)
            return
        ctx.emit_trace(f"set memory {self.memory} -> {self.value!r}")
        await bean.set_value(self.value)

    def extra_fields(self) -> dict[str, Any]:
        return {"memory": self.memory, "value": self.value}


@register
class SetMemoryFromExpression(Action):
    """Evaluate *source* and write the result into memory *memory*.

    Companion to :class:`SetMemory` when the value depends on layout
    state -- e.g. write "average speed" into IM1 by wrapping a
    :class:`~pmri_logixng.expressions.analog.NumericOp` division, or
    write a formatted status string with
    :class:`~pmri_logixng.expressions.string.StringConcat`.
    """

    type_tag: ClassVar[str] = "action:set-memory-from-expression"

    def __init__(
        self,
        *,
        memory: str,
        source: Expression[Any],
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.memory = memory
        self.source = source

    async def execute(self, ctx: ExecutionContext) -> None:
        bean = ctx.connection.memories.get(self.memory)
        if bean is None:
            ctx.log.warning(
                "SetMemoryFromExpression: no memory %r", self.memory,
            )
            return
        value = await self.source.evaluate(ctx)
        ctx.emit_trace(f"set memory {self.memory} <- expr = {value!r}")
        await bean.set_value(value)

    def extra_fields(self) -> dict[str, Any]:
        return {"memory": self.memory, "source": self.source.to_dict()}


@register
class IfThenElse(Action):
    """Evaluate *condition*; run *then_action* if true, *else_action* otherwise."""

    type_tag: ClassVar[str] = "action:if-then-else"

    def __init__(
        self,
        *,
        condition: Expression[bool],
        then_action: Action,
        else_action: Action | None = None,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.condition = condition
        self.then_action = then_action
        self.else_action = else_action

    async def execute(self, ctx: ExecutionContext) -> None:
        result = await self.condition.evaluate(ctx)
        ctx.emit_trace(f"if-then-else condition={result}")
        if result:
            await self.then_action.execute(ctx)
        elif self.else_action is not None:
            await self.else_action.execute(ctx)

    def extra_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "condition": self.condition.to_dict(),
            "then": self.then_action.to_dict(),
        }
        if self.else_action is not None:
            out["else"] = self.else_action.to_dict()
        return out


@register
class LogAction(Action):
    """Emit a log message. Mostly useful for debugging automations."""

    type_tag: ClassVar[str] = "action:log"

    def __init__(
        self,
        *,
        message: str,
        level: str = "info",
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.message = message
        self.level = level

    async def execute(self, ctx: ExecutionContext) -> None:
        ctx.emit_trace(f"log[{self.level}]: {self.message}")
        getattr(ctx.log, self.level, ctx.log.info)(self.message)

    def extra_fields(self) -> dict[str, Any]:
        return {"message": self.message, "level": self.level}
