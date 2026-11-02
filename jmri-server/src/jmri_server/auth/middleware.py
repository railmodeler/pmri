"""ASGI middleware that enforces auth on every request.

Reads a Bearer token from ``Authorization`` first (mobile / JSON
clients), then falls back to a signed session cookie (browser).
On success populates ``request.state.user`` (:class:`~jmri_server.
auth.models.User`) and ``request.state.role`` (:class:`~jmri_server.
auth.models.Role`) so downstream routes and the JSON registry can
gate on role.

The middleware short-circuits when :attr:`ServerContext.auth` is
``None`` -- the "auth disabled" default. That path sets ``user=None``
and ``role=Role.ADMIN`` so the JSON registry's role check is a
no-op. Keeps the code path uniform whether auth is on or off; the
role gate lives in ONE place (the registry) rather than being
duplicated at every check point.

**Exempt paths** always bypass the credential check:

* ``/login`` and ``/logout`` -- the login form itself can't require
  auth, obviously.
* ``/static/*`` and ``/docs/*`` -- public assets. Static docs
  shouldn't need a session cookie to render.
* ``/json/health`` (if it exists) -- for load balancers.

The exempt list is regex-matched against the raw path so a
misconfigured route registrant can't accidentally require auth on
``/login``.

**Failure responses**:

* JSON API (``/json/`` or ``Content-Type: application/json``) ->
  ``401`` with a JMRI-shaped error body.
* Browser (``Accept: text/html``) -> 302 to ``/login`` with a
  ``next=`` query so post-login redirect works.
* Everything else -> plain ``401``.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from jmri_server.auth.models import Role

if TYPE_CHECKING:
    from jmri_server.context import ServerContext

_log = logging.getLogger(__name__)

# Cookie name for the signed session. Prefixed with ``__Host-`` when
# HTTPS becomes the norm -- for the current LAN-only deployment,
# ``JMRISESSION`` (no prefix, works over plain HTTP) suffices.
SESSION_COOKIE = "JMRISESSION"


# Paths that never require auth. Compiled once at module load.
_EXEMPT_PATTERNS = [
    re.compile(r"^/login/?$"),
    re.compile(r"^/logout/?$"),
    re.compile(r"^/static/"),
    re.compile(r"^/docs/"),
    # The FastAPI /docs and /redoc (OpenAPI UI) -- same URL but
    # different meaning; harmless to exempt for beta.
    re.compile(r"^/openapi\.json$"),
    # WebSocket handshake carries auth via query string or first
    # message; the middleware can't intercept that easily, so we
    # exempt the path itself and check inside the WS handler.
    re.compile(r"^/ws/"),
]


def _is_exempt(path: str) -> bool:
    return any(p.match(path) for p in _EXEMPT_PATTERNS)


class AuthMiddleware(BaseHTTPMiddleware):
    """Populate ``request.state.user`` / ``request.state.role`` from
    the request's credentials."""

    def __init__(self, app, ctx: ServerContext) -> None:
        super().__init__(app)
        self._ctx = ctx

    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        request.state.role = Role.ADMIN  # default when auth is off

        auth_mgr = getattr(self._ctx, "auth", None)
        if auth_mgr is None:
            # Auth disabled -- every request runs as admin. Downstream
            # role check is a no-op.
            return await call_next(request)

        # Auth enabled. Try bearer, then cookie.
        user = _try_bearer(request, auth_mgr)
        if user is None:
            user = _try_cookie(request, auth_mgr)

        if user is None:
            if _is_exempt(request.url.path):
                # Exempt path with no valid credentials -- anonymous
                # viewer for the response, since nothing here should
                # actually depend on auth state.
                request.state.role = Role.VIEWER
                return await call_next(request)
            return _unauthorized(request)

        request.state.user = user
        request.state.role = user.role
        return await call_next(request)


def _try_bearer(request: Request, auth_mgr) -> object | None:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    result = auth_mgr.login_token(token)
    return result.user if result else None


def _try_cookie(request: Request, auth_mgr) -> object | None:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie:
        return None
    return auth_mgr.verify_session_cookie(cookie)


def _unauthorized(request: Request) -> Response:
    path = request.url.path
    accept = request.headers.get("Accept", "")
    # JSON endpoints or JSON-only clients get a JSON 401 --
    # otherwise browser clients get redirected to /login with a
    # next= hint so they end up back where they were.
    if (
        path.startswith("/json")
        or "application/json" in request.headers.get("Content-Type", "")
        or "application/json" in accept
    ):
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "data": {
                    "code": 401,
                    "message": "authentication required",
                },
            },
        )
    if "text/html" in accept:
        redirect_target = f"/login?next={path}"
        return RedirectResponse(url=redirect_target, status_code=302)
    return Response(status_code=401, content="authentication required")
