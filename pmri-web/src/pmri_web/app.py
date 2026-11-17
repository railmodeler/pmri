"""FastAPI app factory -- mounts pmri-server's JSON API + adds template routes."""
from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pmri_server.app import AppHandle
from pmri_server.app import build_app as build_server_app
from pmri_server.context import ServerMetadata

from pmri_web.nudge import NudgeHub
from pmri_web.routes import install_template_routes

_PKG = pathlib.Path(__file__).resolve().parent
TEMPLATES_DIR = _PKG / "templates"
STATIC_DIR = _PKG / "static"


@dataclass
class WebHandle:
    """Bundle returned by :func:`build_web_app` -- exposes the server + templates
    so tests can drive both layers."""

    app: FastAPI
    server: AppHandle
    templates: Jinja2Templates
    # Default to a fresh hub so tests that build a WebHandle directly
    # (without wiring the ``/ws/nudge`` route) don't have to construct
    # one explicitly -- broadcasts become silent no-ops in that case.
    nudge: NudgeHub = field(default_factory=NudgeHub)


def build_web_app(*, metadata: ServerMetadata | None = None) -> WebHandle:
    server = build_server_app(metadata=metadata)
    app = server.app

    # Static assets + Jinja2 templates.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        # Generated pdoc reference lives at ``static/docs/``. Mounting
        # it directly at ``/docs/`` gives a cleaner URL for the "read
        # the docs" links in topic-help panels + the OVERVIEW page.
        # When docs haven't been built yet the directory is missing;
        # skip the mount silently so ``build_docs.py`` remains an
        # optional developer step, not a hard startup requirement.
        docs_dir = STATIC_DIR / "docs"
        if docs_dir.is_dir():
            app.mount(
                "/docs",
                StaticFiles(directory=str(docs_dir), html=True),
                name="docs",
            )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    nudge = NudgeHub()

    @app.websocket("/ws/nudge")
    async def _ws_nudge(ws: WebSocket) -> None:
        """Nudge-channel: server-initiated, "state changed" pushes.

        Clients attach on page load via ``pmri_push.js``; incoming
        frames are ignored (nudge is one-way, server -> client). The
        socket stays open for the page's lifetime; a disconnect
        (navigation, tab close) drops the registration. Reconnect
        is the client's responsibility.
        """
        await ws.accept()
        await nudge.attach(ws)
        try:
            # Drain client frames even though we don't act on them --
            # Starlette would otherwise flag the socket idle.
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            await nudge.detach(ws)

    install_template_routes(app, server, templates, nudge=nudge)

    # Auto-load the decoder catalog when ``JMRI_DECODER_XML_ROOT`` is
    # set. Chained onto the server's existing lifespan so it runs
    # AFTER server startup (config store, hardware connection, etc.
    # are all up) but before any user request lands. The load itself
    # runs in a background thread so startup stays fast -- pages that
    # check ``len(catalog) > 0`` see an empty catalog until the scan
    # completes (same as the "user hasn't clicked Load yet" state).
    #
    # FastAPI removed ``add_event_handler`` in newer releases; the
    # lifespan-chaining pattern is the supported replacement.
    from pmri_web.catalog_autoload import schedule_autoload  # noqa: PLC0415
    from pmri_web.openlcb_tail import run_openlcb_tail  # noqa: PLC0415
    from pmri_web.scripts_tail import run_scripts_tail  # noqa: PLC0415

    _server_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _web_lifespan(_app):  # type: ignore[no-untyped-def]
        async with _server_lifespan(_app):
            schedule_autoload(server)
            # Background tail tasks that nudge per-topic whenever
            # server-visible state changes. Replace client-side
            # polling with server-side diff-and-nudge loops -- each
            # task is one tick per second per server, not per client.
            import asyncio as _asyncio    # noqa: PLC0415
            tail_tasks = [
                _asyncio.create_task(run_scripts_tail(server, nudge)),
                _asyncio.create_task(run_openlcb_tail(server, nudge)),
            ]
            try:
                yield
            finally:
                for t in tail_tasks:
                    t.cancel()
                for t in tail_tasks:
                    try:
                        await t
                    except _asyncio.CancelledError:
                        pass

    app.router.lifespan_context = _web_lifespan

    return WebHandle(app=app, server=server, templates=templates, nudge=nudge)


def create_app() -> FastAPI:
    """Default ASGI entry point. ``uvicorn pmri_web:create_app --factory``.

    Production wiring: builds the full web + server stack and attaches
    the multi-profile registry + ``ConfigStore`` (reading
    ``<settings>/profiles.xml``, honouring ``JMRI_SETTINGS_DIR``).
    Tests that go through :func:`build_web_app` directly skip this for
    hermeticity.
    """
    from pmri_core.persistence import default_settings_dir  # noqa: PLC0415
    from pmri_server.app import (  # noqa: PLC0415
        attach_auth,
        attach_profile_persistence,
        attach_withrottle_server,
    )
    handle = build_web_app()
    attach_profile_persistence(handle.server)
    attach_withrottle_server(handle.server)
    # Auth is opt-in via ``JMRI_AUTH_ENABLED=1`` -- the attach is a
    # no-op when the env var isn't set, so this line is safe to
    # keep unconditionally.
    attach_auth(handle.server, default_settings_dir())
    return handle.app
