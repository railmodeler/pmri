"""``LogixNG`` and ``ConditionalNG`` containers.

* :class:`ConditionalNG` runs a sequence of actions when invoked.
* :class:`LogixNG` is a named collection of conditionals -- JMRI's
  user-facing organizational unit. Triggering one conditional is
  independent of the others; the LogixNG container is mainly for
  serialization grouping and enable/disable in bulk.
"""
from __future__ import annotations

from typing import Any, ClassVar

from pmri_logixng.context import ExecutionContext
from pmri_logixng.registry import register
from pmri_logixng.tree import Action, Node


@register
class ConditionalNG(Action):
    """A sequence of root actions executed in order."""

    type_tag: ClassVar[str] = "logixng:conditional"

    def __init__(
        self,
        actions: list[Action] | None = None,
        *,
        user_name: str = "",
        node_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.actions: list[Action] = list(actions or [])
        self.enabled = enabled

    async def execute(self, ctx: ExecutionContext) -> None:
        if not self.enabled:
            ctx.emit_trace(f"conditional {self.id!r} skipped (disabled)")
            return
        ctx.emit_trace(f"conditional {self.id!r} executing {len(self.actions)} actions")
        for action in self.actions:
            await action.execute(ctx)

    def extra_fields(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "actions": [a.to_dict() for a in self.actions],
        }


@register
class LogixNG(Node):
    """A named collection of :class:`ConditionalNG` instances."""

    type_tag: ClassVar[str] = "logixng:root"

    def __init__(
        self,
        conditionals: list[ConditionalNG] | None = None,
        *,
        user_name: str = "",
        node_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.conditionals: list[ConditionalNG] = list(conditionals or [])
        self.enabled = enabled

    def add(self, conditional: ConditionalNG) -> None:
        self.conditionals.append(conditional)

    async def execute_all(self, ctx: ExecutionContext) -> None:
        """Execute every conditional in order (for fire-and-forget runs)."""
        if not self.enabled:
            return
        for c in self.conditionals:
            await c.execute(ctx)

    def extra_fields(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "conditionals": [c.to_dict() for c in self.conditionals],
        }
