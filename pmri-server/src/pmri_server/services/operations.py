"""JSON services for the Operations Pro roster files.

Topics ``operations-cars`` / ``operations-engines`` /
``operations-locations`` each expose a plain CRUD surface over the
matching :class:`~pmri_server.operations_store.OperationsStore` roster.

Wire shape per topic::

    # list:
    { "type": "<topic>", "method": "list" }
    # get:
    { "type": "<topic>", "method": "get", "data": { "id": "..." } }
    # create / update:
    { "type": "<topic>", "method": "put",
      "data": { "id": "...", "<field>": "...", ... } }
    # delete:
    { "type": "<topic>", "method": "delete",
      "data": { "id": "..." } }

Fields map to the Pydantic model's field names (``roadName``,
``roadNumber``, ``locationId``, etc. -- JMRI's wire shape). Any
unknown keys in the ``put`` payload land in the model's ``extra``
dict and round-trip verbatim.

The store is attached to the server context by
:func:`attach_operations_store` on the first call that needs it; tests
can also wire ``ctx._operations_store`` directly for isolation.
"""
from __future__ import annotations

from typing import Any

from pmri_core.persistence.operations_xml import (
    Car, Engine, Location, Route, RouteLocation, Station, Train, TrainStop,
)

from pmri_server.operations_store import OperationsStore
from pmri_server.service import JsonService, JsonServiceError


def _resolve_store(ctx: Any) -> OperationsStore:
    """Return the ctx's OperationsStore, constructing one lazily if the
    caller only wired a Profile. Raises 500 when neither is available."""
    override = getattr(ctx, "_operations_store", None)
    if isinstance(override, OperationsStore):
        return override
    # Fall back: if a profile is present on the ctx (via config_store
    # or _roster_profile) build the store on first use.
    store = getattr(ctx, "config_store", None)
    profile = getattr(store, "profile", None) if store is not None else None
    if profile is None:
        profile = getattr(ctx, "_roster_profile", None)
    if profile is None:
        raise JsonServiceError(500, "no active profile for operations store")
    built = OperationsStore(profile=profile)
    ctx._operations_store = built
    return built


def _require(data: dict[str, Any] | None, *keys: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise JsonServiceError(400, "operations request requires a JSON object")
    missing = [k for k in keys if not data.get(k)]
    if missing:
        raise JsonServiceError(
            400, f"operations request missing field(s): {', '.join(missing)}",
        )
    return data


def _model_payload[M](cls: type[M], data: dict[str, Any]) -> M:
    """Construct a Pydantic model from the wire payload.

    Unknown keys go into ``extra`` so JMRI-level attributes we don't yet
    model (schedules, kernels, loads, ...) still round-trip.
    """
    known_fields = set(cls.model_fields)   # type: ignore[attr-defined]
    aliases = {
        (info.alias or name): name
        for name, info in cls.model_fields.items()   # type: ignore[attr-defined]
    }
    init: dict[str, Any] = {"extra": {}}
    for k, v in data.items():
        if k in known_fields or k in aliases:
            init[k] = v
        else:
            init["extra"][k] = str(v)
    return cls.model_validate(init)   # type: ignore[attr-defined]


def _to_wire[M](model: M) -> dict[str, Any]:
    """Shape the model back to the wire JSON. Uses JMRI's attribute
    aliases (``roadName`` etc.) so clients that read our output see the
    same keys they'd use on write."""
    raw = model.model_dump(mode="json", by_alias=True, exclude_none=False)  # type: ignore[attr-defined]
    extras = raw.pop("extra", {}) or {}
    for k, v in extras.items():
        raw.setdefault(k, v)
    return raw


# ----- cars ---------------------------------------------------------------


class OperationsCarsService(JsonService):
    topic = "operations-cars"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        store = _resolve_store(self.ctx)
        return [_to_wire(c) for c in store.cars()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        car = store.car(str(data["id"]))
        if car is None:
            raise JsonServiceError(404, f"no car with id {data['id']!r}")
        return _to_wire(car)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        model = _model_payload(Car, data)
        store = _resolve_store(self.ctx)
        store.upsert_car(model)
        return _to_wire(model)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        existed = store.remove_car(str(data["id"]))
        if not existed:
            raise JsonServiceError(404, f"no car with id {data['id']!r}")
        return {"id": data["id"], "deleted": True}


# ----- engines ------------------------------------------------------------


class OperationsEnginesService(JsonService):
    topic = "operations-engines"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        store = _resolve_store(self.ctx)
        return [_to_wire(e) for e in store.engines()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        engine = store.engine(str(data["id"]))
        if engine is None:
            raise JsonServiceError(404, f"no engine with id {data['id']!r}")
        return _to_wire(engine)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        model = _model_payload(Engine, data)
        store = _resolve_store(self.ctx)
        store.upsert_engine(model)
        return _to_wire(model)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        existed = store.remove_engine(str(data["id"]))
        if not existed:
            raise JsonServiceError(404, f"no engine with id {data['id']!r}")
        return {"id": data["id"], "deleted": True}


# ----- locations ----------------------------------------------------------


class OperationsLocationsService(JsonService):
    topic = "operations-locations"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        store = _resolve_store(self.ctx)
        return [_to_wire(l) for l in store.locations()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        location = store.location(str(data["id"]))
        if location is None:
            raise JsonServiceError(
                404, f"no location with id {data['id']!r}",
            )
        return _to_wire(location)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        model = _model_payload(Location, data)
        store = _resolve_store(self.ctx)
        store.upsert_location(model)
        return _to_wire(model)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        existed = store.remove_location(str(data["id"]))
        if not existed:
            raise JsonServiceError(
                404, f"no location with id {data['id']!r}",
            )
        return {"id": data["id"], "deleted": True}


# ----- stations -----------------------------------------------------------


class OperationsStationsService(JsonService):
    topic = "operations-stations"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        store = _resolve_store(self.ctx)
        return [_to_wire(s) for s in store.stations()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        station = store.station(str(data["id"]))
        if station is None:
            raise JsonServiceError(
                404, f"no station with id {data['id']!r}",
            )
        return _to_wire(station)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        model = _model_payload(Station, data)
        store = _resolve_store(self.ctx)
        store.upsert_station(model)
        return _to_wire(model)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        existed = store.remove_station(str(data["id"]))
        if not existed:
            raise JsonServiceError(
                404, f"no station with id {data['id']!r}",
            )
        return {"id": data["id"], "deleted": True}


# ----- routes -------------------------------------------------------------


def _route_to_wire(route: Route) -> dict[str, Any]:
    """Shape a Route for the wire, flattening its ``locations`` list
    into per-stop dicts. Mirrors the dedicated field name rather than
    relying on the generic ``_to_wire`` helper so clients get a stable
    ``stops`` key rather than a Pydantic-dumped camelCase list."""
    raw = _to_wire(route)
    stops = raw.pop("locations", None) or []
    # Each stop already in alias form via model_dump(by_alias=True).
    raw["stops"] = stops
    return raw


def _route_from_payload(data: dict[str, Any]) -> Route:
    """Build a Route from the wire payload, pulling ``stops`` out as
    the ordered RouteLocation list.

    Accepts either ``stops`` (our preferred key) or ``locations`` (what
    the Pydantic model dump happens to emit under ``by_alias=True``) so
    clients can re-post what they received.
    """
    stops_raw = data.get("stops")
    if stops_raw is None:
        stops_raw = data.get("locations") or []
    stops: list[RouteLocation] = []
    if isinstance(stops_raw, list):
        for row in stops_raw:
            if isinstance(row, dict):
                stops.append(_model_payload(RouteLocation, row))
    # Build the Route from the remaining scalar fields.
    scalar_data = {
        k: v for k, v in data.items()
        if k not in ("stops", "locations")
    }
    route = _model_payload(Route, scalar_data)
    route.locations = stops
    return route


class OperationsRoutesService(JsonService):
    topic = "operations-routes"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        store = _resolve_store(self.ctx)
        return [_route_to_wire(r) for r in store.routes()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        route = store.route(str(data["id"]))
        if route is None:
            raise JsonServiceError(404, f"no route with id {data['id']!r}")
        return _route_to_wire(route)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        route = _route_from_payload(data)
        store = _resolve_store(self.ctx)
        store.upsert_route(route)
        # Re-read to get the stops with their server-assigned sequence
        # numbers + synthesised ids so the client sees the authoritative
        # shape on the response.
        fresh = store.route(route.id)
        return _route_to_wire(fresh or route)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        existed = store.remove_route(str(data["id"]))
        if not existed:
            raise JsonServiceError(404, f"no route with id {data['id']!r}")
        return {"id": data["id"], "deleted": True}


# ----- trains -------------------------------------------------------------


def _train_from_payload(data: dict[str, Any]) -> Train:
    """Build a Train from the wire payload, extracting the per-stop
    schedule list from the ``stops`` key (fallback ``trainStops``).
    Mirrors :func:`_route_from_payload`.
    """
    stops_raw = data.get("stops")
    if stops_raw is None:
        stops_raw = data.get("trainStops") or []
    stops: list[TrainStop] = []
    if isinstance(stops_raw, list):
        for row in stops_raw:
            if isinstance(row, dict):
                stops.append(_model_payload(TrainStop, row))
    scalar_data = {
        k: v for k, v in data.items()
        if k not in ("stops", "trainStops")
    }
    train = _model_payload(Train, scalar_data)
    train.stops = stops
    return train


class OperationsTrainsService(JsonService):
    topic = "operations-trains"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        store = _resolve_store(self.ctx)
        return [_to_wire(t) for t in store.trains()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        train = store.train(str(data["id"]))
        if train is None:
            raise JsonServiceError(404, f"no train with id {data['id']!r}")
        return _to_wire(train)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        model = _train_from_payload(data)
        store = _resolve_store(self.ctx)
        store.upsert_train(model)
        fresh = store.train(model.id)
        return _to_wire(fresh or model)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = _require(data, "id")
        store = _resolve_store(self.ctx)
        existed = store.remove_train(str(data["id"]))
        if not existed:
            raise JsonServiceError(404, f"no train with id {data['id']!r}")
        return {"id": data["id"], "deleted": True}
