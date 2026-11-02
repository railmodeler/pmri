"""``Reporter`` -- a layout input that delivers identification data.

Where a :class:`Sensor` reports a boolean, a Reporter reports a string (or
other structured value) -- typically a loco ID from a RailCom reader, RFID
tag, or transponder. Reporters keep both a *current* report (the most recent
value) and a *last* report (the value before that), per JMRI semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager


@dataclass(frozen=True, slots=True)
class ReporterChange:
    previous: Any
    current: Any


class Reporter(AbstractNamedBean):
    """Identification-data input. ``current_report`` is None when no read yet."""

    __slots__ = ("_current_report", "_last_report")

    type_letter = "R"

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        super().__init__(system_name, user_name)
        self._current_report: Any = None
        self._last_report: Any = None

    @property
    def current_report(self) -> Any:
        return self._current_report

    @property
    def last_report(self) -> Any:
        return self._last_report

    async def set_report(self, value: Any) -> None:
        prev = self._current_report
        self._last_report = prev
        self._current_report = value
        await self.state_changed.emit(ReporterChange(prev, value))


class ReporterManager(Manager[Reporter]):
    type_letter = "R"

    def _create(self, system_name: str) -> Reporter:
        return Reporter(system_name)
