"""``ExecutionContext`` -- the runtime environment a LogixNG sees.

Every Action.execute / Expression.evaluate call receives the context. It
exposes:

* ``connection`` -- the active :class:`SystemConnection` (so actions can
  reach the bean managers).
* ``log`` -- a logger for the LogAction.
* ``trace`` -- opt-in evaluation trace, useful for debugging conditionals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from jmri_core.hal.connection import SystemConnection


@dataclass
class ExecutionContext:
    """Runtime context passed through every node visit."""

    connection: SystemConnection
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("jmri_logixng"))
    trace_enabled: bool = False
    trace: list[str] = field(default_factory=list)

    def emit_trace(self, message: str) -> None:
        if self.trace_enabled:
            self.trace.append(message)
