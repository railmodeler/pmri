"""FastAPI app factory -- mounts jmri-server's JSON API + adds template routes."""
from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jmri_server.app import AppHandle
from jmri_server.app import build_app as build_server_app
from jmri_server.context import ServerMetadata

from jmri_web.routes import install_template_routes

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

    install_template_routes(app, server, templates)

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
    from jmri_web.catalog_autoload import schedule_autoload  # noqa: PLC0415

    _server_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _web_lifespan(_app):  # type: ignore[no-untyped-def]
        async with _server_lifespan(_app):
            schedule_autoload(server)
            yield

    app.router.lifespan_context = _web_lifespan

    return WebHandle(app=app, server=server, templates=templates)


def create_app() -> FastAPI:
    """Default ASGI entry point. ``uvicorn jmri_web:create_app --factory``.

    Production wiring: builds the full web + server stack and attaches
    the multi-profile registry + ``ConfigStore`` (reading
    ``<settings>/profiles.xml``, honouring ``JMRI_SETTINGS_DIR``).
    Tests that go through :func:`build_web_app` directly skip this for
    hermeticity.
    """
    from jmri_core.persistence import default_settings_dir  # noqa: PLC0415
    from jmri_server.app import (  # noqa: PLC0415
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
