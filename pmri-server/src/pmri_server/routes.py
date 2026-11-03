"""FastAPI routes -- HTTP ``POST /json`` and WebSocket ``WS /json``.

The HTTP endpoint accepts one request, dispatches, returns one response.
The WebSocket endpoint accepts a stream of requests (and pushes unsolicited
state updates) for the lifetime of the connection.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from pmri_server.envelope import JmriRequest, JmriResponse
from pmri_server.roster_media import (
    media_content_type,
    resolve_media_path,
)
from pmri_server.service import JsonServiceError

if TYPE_CHECKING:
    from pmri_server.context import ServerContext
    from pmri_server.push import PushManager
    from pmri_server.registry import ServiceRegistry


def install_routes(
    app: FastAPI,
    ctx: ServerContext,
    registry: ServiceRegistry,
    push: PushManager,
) -> None:
    router = APIRouter()

    @router.get("/api")
    async def root() -> dict[str, str]:
        """Status endpoint. Lives at ``/api`` (not ``/``) so it doesn't shadow a
        web UI home page when pmri-web mounts on the same app."""
        return {"service": "pmri-server", "version": ctx.metadata.pmri_version}

    @router.get("/json/hello")
    async def hello() -> dict[str, object]:
        svc = registry.get("hello")
        assert svc is not None
        return await svc.on_get(None)  # type: ignore[return-value]

    @router.post("/json/")
    async def post_json(req: JmriRequest, request: Request) -> JmriResponse:
        # Role + username populated by AuthMiddleware. Both fall
        # back to admin / None when the middleware short-circuits
        # (auth off) or when a test calls the route without going
        # through middleware -- the registry's ``dispatch``
        # defaults match, so this doesn't leak permission over a
        # naked test call.
        role = getattr(request.state, "role", None)
        user = getattr(request.state, "user", None)
        username = user.username if user is not None else None
        try:
            if role is None:
                return await registry.dispatch(req, username=username)
            return await registry.dispatch(req, role=role, username=username)
        except JsonServiceError as e:
            raise HTTPException(status_code=e.code, detail=e.message) from None

    async def _serve_roster_media(entry_id: str, kind: str) -> FileResponse:
        """Shared implementation for ``/roster/{id}/image`` and
        ``/roster/{id}/icon``. 404 on any missing piece; never leaks a
        server-side error message about the underlying filesystem."""
        from pmri_server.services.roster import _lazy_roster_store  # noqa: PLC0415
        store = _lazy_roster_store(ctx)
        if store is None:
            raise HTTPException(status_code=404, detail="roster unavailable")
        entry = store.get(entry_id)
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"no roster entry {entry_id!r}",
            )
        raw = (
            entry.image_file_path if kind == "image"
            else entry.icon_file_path
        )
        resolved = resolve_media_path(store.profile, raw)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail=f"no {kind} media for roster entry {entry_id!r}",
            )
        return FileResponse(
            path=resolved,
            media_type=media_content_type(resolved),
            filename=resolved.name,
        )

    @router.get("/roster/{entry_id}/image")
    async def roster_image(entry_id: str) -> FileResponse:
        return await _serve_roster_media(entry_id, "image")

    @router.get("/roster/{entry_id}/icon")
    async def roster_icon(entry_id: str) -> FileResponse:
        return await _serve_roster_media(entry_id, "icon")

    @app.websocket("/json")
    async def ws_json(ws: WebSocket) -> None:
        await ws.accept()
        await push.attach(ws)
        # Greet on connect, per JMRI's convention.
        hello_svc = registry.get("hello")
        if hello_svc is not None:
            greeting = await hello_svc.on_get(None)
            await ws.send_text(json.dumps({"type": "hello", "data": greeting}))
        try:
            while True:
                raw = await ws.receive_text()
                await _handle_ws_text(ws, registry, raw)
        except WebSocketDisconnect:
            pass
        finally:
            await push.detach(ws)

    app.include_router(router)


async def _handle_ws_text(
    ws: WebSocket, registry: ServiceRegistry, raw: str
) -> None:
    """Parse one WebSocket text frame, dispatch, write back the response."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        await ws.send_text(json.dumps({
            "type": "error",
            "data": {"code": 400, "message": "invalid JSON"},
        }))
        return

    # JMRI allows batched messages: either a single envelope or a list.
    items = parsed if isinstance(parsed, list) else [parsed]
    for item in items:
        try:
            req = JmriRequest.model_validate(item)
            resp = await registry.dispatch(req)
            await ws.send_text(resp.model_dump_json(exclude_none=True))
        except JsonServiceError as e:
            await ws.send_text(json.dumps({
                "type": "error",
                "data": {"code": e.code, "message": e.message},
                "id": item.get("id") if isinstance(item, dict) else None,
            }))
        except Exception as e:  # noqa: BLE001 -- turn into wire-format error
            await ws.send_text(json.dumps({
                "type": "error",
                "data": {"code": 500, "message": str(e)},
            }))
