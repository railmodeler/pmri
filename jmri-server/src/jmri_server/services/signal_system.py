"""``SignalSystemService`` -- JSON topic ``signalSystem``.

Read-only enumeration of the JMRI signal systems parsed by
:class:`~jmri_core.persistence.signal_systems.SignalSystemLoader`.
Mobile clients use ``list`` to populate a picker; the panel viewer
uses ``get`` to look up an individual system's aspect table.

Wire shape::

    {
      "id": "basic",                   // directory name under xml/signals
      "name": "Basic Model Signals",   // <name> from aspects.xml
      "reference": "...",              // <reference> from aspects.xml
      "aspects": [
        {"name": "Clear", "indication": "Proceed",
         "speed": "Normal", "speed2": "Normal", "route": ""},
        ...
      ]
    }

No ``list`` of systems is 503 unless the loader itself couldn't be
constructed (which shouldn't happen). Callers configuring the env
var wrongly get an empty list back -- the ``/signalsystems`` web
page renders a hint in that case.
"""
from __future__ import annotations

from typing import Any

from jmri_core.env import signal_xml_root
from jmri_core.persistence.signal_systems import (
    SignalSystem,
    SignalSystemLoader,
)

from jmri_server.service import JsonService, JsonServiceError


def _project(system: SignalSystem) -> dict[str, Any]:
    return {
        "id": system.id,
        "name": system.name,
        "reference": system.reference,
        "aspects": [
            {
                "name": a.name,
                "indication": a.indication,
                "speed": a.speed,
                "speed2": a.speed2,
                "route": a.route,
            }
            for a in system.aspects
        ],
    }


def _loader(ctx: Any) -> SignalSystemLoader:
    """Lazy singleton on the ctx. Constructed the first time a
    caller asks for it; env-driven root path so operators don't have
    to edit code to point at their JMRI install."""
    existing = getattr(ctx, "_signal_system_loader", None)
    if isinstance(existing, SignalSystemLoader):
        return existing
    root = getattr(ctx, "_signal_xml_root", None) or signal_xml_root()
    loader = SignalSystemLoader(root)
    ctx._signal_system_loader = loader  # type: ignore[attr-defined]
    return loader


class SignalSystemService(JsonService):
    topic = "signalSystem"

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return [_project(s) for s in _loader(self.ctx).systems()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        system_id = data.get("id") or data.get("name")
        if not isinstance(system_id, str) or not system_id:
            raise JsonServiceError(
                400, "signalSystem.get requires 'id' (or legacy 'name')",
            )
        system = _loader(self.ctx).get(system_id)
        if system is None:
            raise JsonServiceError(
                404, f"no signal system with id {system_id!r}",
            )
        return _project(system)
