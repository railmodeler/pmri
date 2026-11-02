"""``ThrottleService`` -- drives :class:`jmri_core.beans.throttle.Throttle`
instances via the active connection's :class:`ThrottleManager`.

Wire shape per ``contracts/json-schema/throttle/*-server.json``:

* ``name`` (required) -- identity string for the throttle handle. We use
  the DCC address as a string by default.
* ``throttle`` -- deprecated alias for ``name``; emitted on every response.
* ``address`` -- DCC address (int).
* ``speed`` -- -1.0..1.0 (number). -1 is emergency stop.
* ``forward`` -- bool.
* ``F0``..``F28`` -- booleans.
* ``release`` -- null marker on response when the throttle was released.

JSON method semantics:

* ``put``  -- acquire a throttle for ``address``.
* ``get``  -- return the current state of the throttle named ``name``.
* ``post`` -- apply mutations (speed/forward/F*).
* ``delete`` -- release.
* ``list`` -- return every active throttle.
"""
from __future__ import annotations

from typing import Any

from jmri_core.beans.throttle import NUM_FUNCTIONS, Throttle
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.state import ThrottleDirection

from jmri_server.service import JsonService, JsonServiceError


def _throttle_name(throttle: Throttle) -> str:
    """Identity string the wire format uses for a throttle handle."""
    return str(throttle.address)


def _project(throttle: Throttle, *, released: bool = False) -> dict[str, Any]:
    name = _throttle_name(throttle)
    out: dict[str, Any] = {
        "name": name,
        "throttle": name,
        "address": throttle.address,
        "speed": throttle.speed,
        "forward": throttle.direction == ThrottleDirection.FORWARD,
        "speedSteps": 128,
        "clients": 1,
    }
    for i in range(min(NUM_FUNCTIONS, 29)):
        out[f"F{i}"] = throttle.functions[i]
    if released:
        out["release"] = None
    return out


def _resolve_name(throttles_mgr: ThrottleManager, data: dict[str, Any]) -> Throttle:
    """Find the throttle the client refers to.

    Accepts either ``name`` or the deprecated ``throttle`` field. Returns
    the live ``Throttle`` instance.
    """
    name = data.get("name") or data.get("throttle")
    if name is None:
        raise JsonServiceError(400, "throttle request requires 'name' or 'throttle'")
    try:
        address = int(name)
    except (TypeError, ValueError):
        raise JsonServiceError(400, f"throttle name must be a DCC address, got {name!r}") from None
    throttle = throttles_mgr.get(address)
    if throttle is None:
        raise JsonServiceError(404, f"no throttle for address {address}")
    return throttle


class ThrottleService(JsonService):
    topic = "throttle"

    @property
    def _throttles(self) -> Any:
        """The ThrottleManager on the active system connection."""
        return self.ctx.connection.throttles

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        throttle = _resolve_name(self._throttles, data or {})
        return _project(throttle)

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        return [_project(t) for t in self._throttles]

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        if not data:
            raise JsonServiceError(400, "throttle.put requires data")
        # Accept either ``address`` directly or ``name`` as a stringified address.
        address = data.get("address")
        if address is None:
            name = data.get("name") or data.get("throttle")
            try:
                address = int(name) if name is not None else None
            except (TypeError, ValueError):
                address = None
        if not isinstance(address, int):
            raise JsonServiceError(400, "throttle.put requires 'address' or numeric 'name'")
        long_address = bool(data.get("isLongAddress", False))
        throttle = await self._throttles.acquire(address, long_address=long_address)
        await _apply_mutations(throttle, data)
        return _project(throttle)

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        if not data:
            raise JsonServiceError(400, "throttle.post requires data")
        throttle = _resolve_name(self._throttles, data)
        # ``release`` field (any value, including null) triggers release.
        if "release" in data:
            await self._throttles.release(throttle)
            return _project(throttle, released=True)
        await _apply_mutations(throttle, data)
        return _project(throttle)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        throttle = _resolve_name(self._throttles, data or {})
        await self._throttles.release(throttle)
        return _project(throttle, released=True)


async def _apply_mutations(throttle: Throttle, data: dict[str, Any]) -> None:
    if "speed" in data:
        speed = float(data["speed"])
        await throttle.set_speed(speed)
    if "forward" in data:
        direction = ThrottleDirection.FORWARD if data["forward"] else ThrottleDirection.REVERSE
        await throttle.set_direction(direction)
    for i in range(min(NUM_FUNCTIONS, 29)):
        key = f"F{i}"
        if key in data:
            await throttle.set_function(i, bool(data[key]))
