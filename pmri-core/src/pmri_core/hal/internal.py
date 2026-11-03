"""``InternalConnection`` -- in-memory loopback for tests and the default
"internal" system.

JMRI ships a built-in connection with no hardware behind it: turnouts and
sensors created against the internal prefix change state purely in memory.
This is invaluable for tests, the JSON server's contract tests, and any
"layout-less" scripting.

The internal prefix is ``"I"`` to match JMRI's convention.
"""
from __future__ import annotations

from typing import ClassVar

from pmri_core.hal.connection import ConnectionSettings, SystemConnection


class InternalConnection(SystemConnection):
    """Pure in-memory system connection. No I/O. No background tasks."""

    name: ClassVar[str] = "Internal"
    prefix: ClassVar[str] = "I"

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        await super().open(settings)

    async def close(self) -> None:
        await super().close()
