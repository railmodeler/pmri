"""Service registry -- topic -> JsonService instance.

A single :class:`ServiceRegistry` owns one :class:`JsonService` instance per
topic, all sharing the same :class:`ServerContext`. The registry is the
dispatch surface used by both the HTTP route and the WebSocket handler.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmri_server.auth.models import Role
from jmri_server.envelope import JmriRequest, JmriResponse, Method, normalize_request
from jmri_server.service import JsonService, JsonServiceError

if TYPE_CHECKING:
    from jmri_server.context import ServerContext


class ServiceRegistry:
    """Holds and dispatches to one service per topic."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx
        self._services: dict[str, JsonService] = {}

    def register(self, service_cls: type[JsonService]) -> JsonService:
        """Construct *service_cls*, share the context, and register it.

        Returns the instance so the caller can hook into it (e.g. to wire
        state-change listeners for WebSocket push).
        """
        svc = service_cls(self.ctx)
        if svc.topic in self._services:
            raise ValueError(f"duplicate service for topic {svc.topic!r}")
        self._services[svc.topic] = svc
        return svc

    def get(self, topic: str) -> JsonService | None:
        return self._services.get(topic)

    def topics(self) -> list[str]:
        return sorted(self._services)

    async def dispatch(
        self,
        req: JmriRequest,
        *,
        role: Role = Role.ADMIN,
        username: str | None = None,
    ) -> JmriResponse:
        """Resolve method + topic, call the service, return the wrapped response.

        *role* is the authenticated caller's role -- defaults to
        :data:`Role.ADMIN` so callers (tests, WebSocket handshake
        with auth off) that don't wire the middleware still work.
        When ``JMRI_AUTH_ENABLED=1`` the HTTP route reads
        ``request.state.role`` and passes it in; a method requiring
        a higher role than the caller has raises
        :class:`JsonServiceError` with code 403.

        Raises :class:`JsonServiceError` on unknown topics, insufficient
        role, or service-level errors; the caller (HTTP route /
        WebSocket handler) decides whether to translate to an HTTP
        error or a JMRI error message.
        """
        topic, method, data = normalize_request(req)
        svc = self._services.get(topic)
        if svc is None:
            raise JsonServiceError(404, f"unknown type {topic!r}")
        needed = svc.required_role(method)
        if role < needed:
            raise JsonServiceError(
                403,
                f"role {role.value!r} cannot invoke {topic!r}.{method} "
                f"(requires {needed.value!r})",
            )
        # A service that needs the caller's identity (e.g.
        # ``configUser`` for self-service password change) reads
        # from the context; stash it there so we don't broaden
        # the JsonService signature for a feature only one
        # service uses.
        self.ctx._current_username = username  # type: ignore[attr-defined]
        payload = await _call_service_method(svc, method, data)
        # ``list`` requests always answer with a "list" type wrapper:
        # ``{"type": "list", "data": [...]}``. JMRI's convention.
        response_type = "list" if method == "list" else topic
        return JmriResponse(
            type=response_type,
            data=payload,
            method=method,
            id=req.id,
        )


async def _call_service_method(
    svc: JsonService, method: Method, data: dict[str, Any] | None
) -> dict[str, Any] | list[dict[str, Any]]:
    if method == "get":
        return await svc.on_get(data)
    if method == "list":
        return await svc.on_list(data)
    if method == "put":
        return await svc.on_put(data)
    if method == "post":
        return await svc.on_post(data)
    if method == "delete":
        return await svc.on_delete(data)
    raise JsonServiceError(400, f"unknown method {method!r}")
