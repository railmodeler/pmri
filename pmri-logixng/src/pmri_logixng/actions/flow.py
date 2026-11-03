"""Flow-control actions: sequence + delay."""
from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pmri_logixng.context import ExecutionContext
from pmri_logixng.registry import register
from pmri_logixng.tree import Action


@register
class Sequence(Action):
    """Run a list of actions in order."""

    type_tag: ClassVar[str] = "action:sequence"

    def __init__(
        self,
        actions: list[Action],
        *,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.actions = list(actions)

    async def execute(self, ctx: ExecutionContext) -> None:
        for action in self.actions:
            await action.execute(ctx)

    def extra_fields(self) -> dict[str, Any]:
        return {"actions": [a.to_dict() for a in self.actions]}


@register
class Delay(Action):
    """Sleep for *seconds*. Useful for timed sequences (signal aspect change -> hold)."""

    type_tag: ClassVar[str] = "action:delay"

    def __init__(
        self,
        *,
        seconds: float,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        if seconds < 0:
            raise ValueError(f"delay seconds must be non-negative, got {seconds}")
        self.seconds = seconds

    async def execute(self, ctx: ExecutionContext) -> None:
        ctx.emit_trace(f"delay {self.seconds}s")
        await asyncio.sleep(self.seconds)

    def extra_fields(self) -> dict[str, Any]:
        return {"seconds": self.seconds}
