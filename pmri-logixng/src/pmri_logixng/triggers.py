"""Trigger binding -- fire a :class:`ConditionalNG` when watched beans change.

A trigger is a small adapter that:

1. Subscribes to the ``state_changed`` Signal of every watched bean.
2. On any change, evaluates an optional ``guard`` expression.
3. If the guard returns true (or is absent), executes the conditional.

Triggers are constructed and bound via :class:`AutomationEngine`, which
holds a list of triggers and the disconnect callbacks needed to tear
them down cleanly on shutdown.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pmri_logixng.context import ExecutionContext
from pmri_logixng.logix import ConditionalNG
from pmri_logixng.tree import Expression

BeanKind = Literal["sensor", "turnout", "light", "memory", "block", "reporter"]


@dataclass
class TriggerBinding:
    """One trigger -- watched beans + optional guard + the conditional to run."""

    conditional: ConditionalNG
    watches: list[tuple[BeanKind, str]]  # (kind, system_name)
    guard: Expression[bool] | None = None
    user_name: str = ""


@dataclass
class AutomationEngine:
    """Runs a set of triggers against an :class:`ExecutionContext`."""

    ctx: ExecutionContext
    triggers: list[TriggerBinding] = field(default_factory=list)
    _disconnects: list[Callable[[], None]] = field(default_factory=list)

    def add(self, trigger: TriggerBinding) -> None:
        self.triggers.append(trigger)

    def start(self) -> None:
        """Wire every trigger's watched beans up to the firing handler."""
        for trigger in self.triggers:
            self._wire(trigger)

    def stop(self) -> None:
        """Disconnect every listener. Idempotent."""
        for disconnect in self._disconnects:
            disconnect()
        self._disconnects.clear()

    def _wire(self, trigger: TriggerBinding) -> None:
        handler = self._make_handler(trigger)
        for kind, system_name in trigger.watches:
            bean = self._lookup_bean(kind, system_name)
            if bean is None:
                self.ctx.log.warning(
                    "trigger %r: no %s named %r -- skipping watch",
                    trigger.user_name or trigger.conditional.id, kind, system_name,
                )
                continue
            self._disconnects.append(bean.state_changed.connect(handler))

    def _make_handler(self, trigger: TriggerBinding) -> Callable[[Any], Any]:
        async def handle(_payload: Any) -> None:
            if trigger.guard is not None:
                guard_result = await trigger.guard.evaluate(self.ctx)
                if not guard_result:
                    self.ctx.emit_trace(
                        f"trigger {trigger.user_name or trigger.conditional.id}: guard false"
                    )
                    return
            self.ctx.emit_trace(
                f"trigger {trigger.user_name or trigger.conditional.id}: firing"
            )
            await trigger.conditional.execute(self.ctx)
        return handle

    def _lookup_bean(self, kind: BeanKind, system_name: str) -> Any:
        # Each manager has a ``.get(name)`` method but they're different
        # generic types; an Any-typed bag keeps the union friendly.
        managers: dict[str, Any] = {
            "sensor": self.ctx.connection.sensors,
            "turnout": self.ctx.connection.turnouts,
            "light": self.ctx.connection.lights,
            "memory": self.ctx.connection.memories,
            "block": self.ctx.connection.blocks,
            "reporter": self.ctx.connection.reporters,
        }
        manager = managers.get(kind)
        if manager is None:
            return None
        return manager.get(system_name)


# Touch unused imports so static analyzers don't flag them.
_ = asyncio
