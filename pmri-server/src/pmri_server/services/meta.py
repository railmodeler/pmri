"""Meta services -- hello/ping/version/railroad/node/power.

These don't manage beans; they expose server identity and one global piece
of state (track power per connection).
"""
from __future__ import annotations

from typing import Any

from pmri_core.state import PowerState

from pmri_server.service import JsonService, JsonServiceError
from pmri_server.wire_states import power_from_wire, to_wire


class HelloService(JsonService):
    topic = "hello"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        m = self.ctx.metadata
        return {
            "JMRI": m.pmri_version,
            "json": m.json_protocol_version,
            "version": ".".join(m.json_protocol_version.split(".")[:2]),
            "heartbeat": m.heartbeat_ms,
            "railroad": m.railroad,
            "node": m.node,
            "activeProfile": m.active_profile,
        }


class PingService(JsonService):
    topic = "ping"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        # JMRI returns pong as the response type at the envelope level, but
        # the data slot for ping is empty. The dispatcher writes the envelope;
        # we just return an empty object.
        return {}


class VersionService(JsonService):
    topic = "version"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        m = self.ctx.metadata
        return {"JMRI": m.pmri_version, "version": m.json_protocol_version}


class RailroadService(JsonService):
    topic = "railroad"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        return {"name": self.ctx.metadata.railroad}


class NodeService(JsonService):
    topic = "node"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        return {"node": self.ctx.metadata.node, "formerNodes": []}


class PowerService(JsonService):
    """Track power for the (single) active connection."""

    topic = "power"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        return self._snapshot()

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        return [self._snapshot()]

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        if data is None:
            raise JsonServiceError(400, "power.post requires data")
        if "state" in data:
            await self.ctx.connection.power.set_power(power_from_wire(int(data["state"])))
        return self._snapshot()

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        return await self.on_post(data)

    def _snapshot(self) -> dict[str, Any]:
        conn = self.ctx.connection
        payload: dict[str, Any] = {
            "name": conn.prefix,
            "default": True,
            "state": to_wire(
                int(conn.power.state) if conn.power.state != PowerState.UNKNOWN else 1
            ),
        }
        # Observe-only drivers (pmri-ncemonitor) set ``writable=False`` so
        # the UI can hide the ON/OFF buttons. We only put the field on the
        # wire when it's False -- the canonical JMRI power schema uses
        # ``additionalProperties: false`` and would reject an unknown key
        # on the default (writable=True) path.
        if not getattr(conn.power, "writable", True):
            payload["writable"] = False
        return payload
