"""``JsonService`` -- base class for one topic's request handlers.

Each topic (``turnout``, ``sensor``, ``power``, ...) is one subclass. The
dispatcher resolves the topic from the request envelope's ``type`` and
calls one of :meth:`on_get`, :meth:`on_list`, :meth:`on_put`,
:meth:`on_post`, :meth:`on_delete` based on the method.

Sub-classes return Python dicts; the dispatcher wraps them in the response
envelope. Errors are raised as :class:`JsonServiceError`; the dispatcher
converts them to the JMRI error response format.

Role gating (when ``JMRI_AUTH_ENABLED=1``): every method has a minimum
role required to invoke it. Defaults live in :attr:`REQUIRED_ROLE` --
``viewer`` for reads (``get`` / ``list``), ``operator`` for mutations
(``post`` / ``put`` / ``delete``). Individual services override by
setting :attr:`REQUIRED_ROLE` with their own per-method map -- e.g.
``configProfile`` requires ``admin`` for everything. The dispatcher
enforces the check before invoking the handler; a role-gated failure
raises :class:`JsonServiceError` with code 403.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pmri_server.auth.models import Role

if TYPE_CHECKING:
    from pmri_server.context import ServerContext
    from pmri_server.envelope import Method


class JsonServiceError(Exception):
    """Raised by a service to produce a JMRI-format error response."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class JsonService:
    """Base class for a topic's service handlers.

    Subclasses set :attr:`topic` and override the methods they support;
    unsupported methods raise 405 by default. Subclasses can also
    override :attr:`REQUIRED_ROLE` to change per-method role gates.
    """

    topic: str = ""  # subclass must set

    # Minimum role required to invoke each method. Defaults: reads
    # are ``viewer``, mutations are ``operator``. Override on a
    # per-service basis -- the class-level dict is merged with the
    # base defaults so a subclass only lists the methods it changes.
    REQUIRED_ROLE: dict[str, Role] = {
        "get": Role.VIEWER,
        "list": Role.VIEWER,
        "post": Role.OPERATOR,
        "put": Role.OPERATOR,
        "delete": Role.OPERATOR,
    }

    def required_role(self, method: str) -> Role:
        """Look up the role required to invoke *method* on this
        service, defaulting to :data:`Role.OPERATOR` for unknown
        methods (defense against a future custom verb landing
        without a role declaration)."""
        return self.REQUIRED_ROLE.get(method, Role.OPERATOR)

    def __init__(self, ctx: ServerContext) -> None:
        if not self.topic:
            raise TypeError(f"{type(self).__name__}.topic must be set")
        self.ctx = ctx

    # Each method receives the request `data` dict (may be None) and returns
    # either a dict (single-object reply) or a list of dicts (list reply).
    Payload = dict[str, Any] | list[dict[str, Any]]

    async def on_get(self, data: dict[str, Any] | None) -> Payload:
        raise JsonServiceError(405, f"{self.topic!r} does not support method 'get'")

    async def on_list(self, data: dict[str, Any] | None) -> Payload:
        raise JsonServiceError(405, f"{self.topic!r} does not support method 'list'")

    async def on_put(self, data: dict[str, Any] | None) -> Payload:
        raise JsonServiceError(405, f"{self.topic!r} does not support method 'put'")

    async def on_post(self, data: dict[str, Any] | None) -> Payload:
        raise JsonServiceError(405, f"{self.topic!r} does not support method 'post'")

    async def on_delete(self, data: dict[str, Any] | None) -> Payload:
        raise JsonServiceError(405, f"{self.topic!r} does not support method 'delete'")
