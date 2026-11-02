"""``ServerContext`` -- the shared state every service can see.

Owns the connection, service registry, WebSocket connection manager, and
metadata (railroad name, node identity, active profile). Constructed once
at app startup and passed into each :class:`JsonService` instance.
"""
from __future__ import annotations

import platform
import uuid
from dataclasses import dataclass, field

from jmri_core.hal.connection import SystemConnection


@dataclass
class ServerMetadata:
    """Server-identity metadata returned by the hello/version/railroad services."""

    jmri_version: str = "5.14.0-py"
    json_protocol_version: str = "5.7.0"
    railroad: str = "Python JMRI"
    node: str = field(default_factory=lambda: f"node-{platform.node()}-{uuid.uuid4().hex[:8]}")
    active_profile: str | None = "default"
    heartbeat_ms: int = 15000


@dataclass
class ServerContext:
    """Container for everything a service needs at runtime."""

    connection: SystemConnection
    metadata: ServerMetadata = field(default_factory=ServerMetadata)
    # Set during app startup by ``create_app``. Bean services use this to
    # persist created/edited beans to PanelPro.xml. Optional so tests
    # that build the app via ``build_app`` directly leave it ``None`` for
    # hermeticity (no writes to the user's real settings dir).
    config_store: object | None = None
    # JMRI's ``profiles.xml``-driven multi-profile model. Populated by
    # ``create_app`` when the settings directory has a ``profiles.xml``;
    # tests can leave it None.
    profile_registry: object | None = None
    # Profile-scoped registry of LogixNG programs. Constructed lazily in
    # :func:`build_app` so every ServerContext (including the ones
    # tests spin up via ``build_app``) has a real manager without
    # needing a profile on disk. ``create_app`` additionally
    # populates it from ``<profile>/logixng/*.yaml`` at startup.
    logix_ng: object | None = None
    # Auth subsystem -- populated by ``attach_auth`` when
    # ``JMRI_AUTH_ENABLED=1``, else stays ``None`` (auth off, every
    # request is admin-equivalent). Typed as ``object`` here so the
    # ``jmri_server.auth`` module isn't imported when auth is off.
    # See :class:`jmri_server.auth.manager.AuthManager` for the
    # runtime shape.
    auth: object | None = None
    # PMRI install-level config (``<settings>/pmri.xml``) loaded by
    # ``attach_profile_persistence``. Set to the default-constructed
    # :class:`jmri_core.persistence.PmriConfig` when the file is
    # absent, so services can always read it safely; left ``None`` on
    # ``build_app``-only (tests) installs that skip the profile wire.
    pmri_config: object | None = None
    # PMRI profile-local config (``<profile>/profile/profile.xml``).
    # ONLY populated when ``profile.xml`` exists on disk -- services
    # that key off this field (connection selection, script
    # autoloader) use ``None`` to distinguish a PMRI install from a
    # pre-PMRI JMRI install opened by PMRI, so the fallback paths
    # (env vars, dir-glob script loader) keep working.
    profile_settings: object | None = None
