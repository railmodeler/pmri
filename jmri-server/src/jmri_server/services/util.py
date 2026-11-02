"""Introspection JSON services -- ``metadata``, ``networkService``,
``systemConnection``.

These three sit under ``jmri.server.json.util`` in the Java source
(``JsonUtilHttpService``). They're grouped here for the same reason
they're grouped there -- none of them own beans, none of them mutate
state, and every JMRI mobile client probes them on connect to decide
what the server can do. Reference material:

* metadata schema: ``jmri/server/json/util/metadata-server.json``
* networkService schema: ``jmri/server/json/util/networkService-server.json``
* systemConnection schema: ``jmri/server/json/util/systemConnection-server.json``

All three support ``get`` (by name) and ``list``; none support
``put`` / ``post`` / ``delete``.
"""
from __future__ import annotations

import os
import platform
import sys
from typing import Any

from jmri_server.service import JsonService, JsonServiceError


# ---------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------
#
# Java's ``Metadata`` interface enumerates eight names plus a passthrough
# to system properties for anything else. We honour the eight and 404 on
# unknown names -- the passthrough would leak arbitrary process env, which
# isn't a shape mobile clients depend on.

_METADATA_NAMES = (
    "JMRIVERSION",
    "JMRIVERCANON",
    "JMRIVERMAJOR",
    "JMRIVERMINOR",
    "JMRIVERTEST",
    "JVMVERSION",
    "JVMVENDOR",
    "activeProfile",
    "copyright",
)

_COPYRIGHT = (
    "Copyright (c) 1997-2026 JMRI Community. Python port "
    "Copyright (c) 2026."
)


def _resolve_metadata(ctx: Any, name: str) -> str | None:
    """Return the value for a metadata name, or ``None`` if unknown.

    The JMRI simple-version string (e.g. ``5.14.0-py``) lives on
    :class:`ServerMetadata`. Version parts are parsed lazily since the
    string shape is stable and we've never needed the components at
    runtime before now.
    """
    m = ctx.metadata
    version = str(m.jmri_version)
    if name == "JMRIVERSION":
        return version
    if name == "JMRIVERCANON":
        # Canonical == strip any suffix (``-py``, ``-alpha3``, ``+build.42``).
        return _canonical_version(version)
    if name in ("JMRIVERMAJOR", "JMRIVERMINOR", "JMRIVERTEST"):
        major, minor, test = _split_version(version)
        return {
            "JMRIVERMAJOR": str(major),
            "JMRIVERMINOR": str(minor),
            "JMRIVERTEST": str(test),
        }[name]
    if name == "JVMVERSION":
        return platform.python_version()
    if name == "JVMVENDOR":
        return sys.implementation.name  # "cpython", "pypy", ...
    if name == "activeProfile":
        return str(m.active_profile) if m.active_profile is not None else None
    if name == "copyright":
        return _COPYRIGHT
    return None


def _canonical_version(version: str) -> str:
    """Strip a prerelease / build suffix from *version*.

    ``5.14.0-py`` -> ``5.14.0``. ``5.14.0`` -> ``5.14.0``.
    """
    for sep in ("-", "+"):
        if sep in version:
            return version.split(sep, 1)[0]
    return version


def _split_version(version: str) -> tuple[int, int, int]:
    """Split a semver-ish string into ``(major, minor, test)``.

    Non-numeric parts default to 0. Absent components default to 0.
    Callers pre-strip the suffix via :func:`_canonical_version`.
    """
    parts = _canonical_version(version).split(".")

    def as_int(index: int) -> int:
        if index >= len(parts):
            return 0
        try:
            return int(parts[index])
        except ValueError:
            return 0

    return as_int(0), as_int(1), as_int(2)


class MetadataService(JsonService):
    """Server-identity strings the JMRI mobile clients query on connect."""

    topic = "metadata"

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return [
            {"name": name, "value": _resolve_metadata(self.ctx, name) or ""}
            for name in _METADATA_NAMES
        ]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise JsonServiceError(400, "metadata.get requires 'name'")
        value = _resolve_metadata(self.ctx, name)
        if value is None:
            raise JsonServiceError(404, f"no metadata for {name!r}")
        return {"name": name, "value": value}


# ---------------------------------------------------------------------
# networkService
# ---------------------------------------------------------------------
#
# Java lists real Zeroconf/mDNS advertisements. We don't run an mDNS
# responder yet, but mobile clients still expect to see the JSON server
# and (if enabled) the WiThrottle server here so they know where to
# reach us. We synthesise those two entries from the server metadata
# and the ports the operator started on. This is compatible with the
# schema (``additionalProperties: true``) -- mDNS-savvy clients see the
# entries as if they'd been discovered.

_JSON_SERVICE_TYPE = "_jmri-json._tcp.local."
_WITHROTTLE_SERVICE_TYPE = "_withrottle._tcp.local."

# Standard JMRI ports. Overridable at server start via env vars -- same
# convention the rest of the codebase uses for host / port config.
_DEFAULT_JSON_PORT = 12080
_DEFAULT_WITHROTTLE_PORT = 12090


def _json_port(ctx: Any) -> int:
    """Port the JSON HTTP + WebSocket service is bound to.

    We can't inspect uvicorn's live bindings from inside a request, so
    fall back through explicit ctx attribute -> env var -> default 12080.
    The env-var read goes through :mod:`jmri_core.env` for consistency
    with the rest of the ``JMRI_*`` knobs.
    """
    stored = getattr(ctx, "_json_port", None)
    if isinstance(stored, int):
        return stored
    from jmri_core.env import http_port  # noqa: PLC0415
    return http_port(default=_DEFAULT_JSON_PORT)


def _withrottle_port(ctx: Any) -> int | None:
    """Port the WiThrottle server is bound to, or ``None`` if it isn't
    running. ``attach_withrottle_server`` stores this on the ctx after
    a successful bind."""
    stored = getattr(ctx, "_withrottle_port", None)
    if isinstance(stored, int):
        return stored
    return None


def _service_entry(
    *,
    type_: str,
    ctx: Any,
    port: int,
) -> dict[str, Any]:
    m = ctx.metadata
    return {
        "name": type_,
        "userName": str(m.railroad),
        "port": port,
        "type": type_,
        "version": str(m.jmri_version),
        "jmri": str(m.jmri_version),
        "json": str(m.json_protocol_version),
        "node": str(m.node),
    }


def _all_services(ctx: Any) -> list[dict[str, Any]]:
    services = [
        _service_entry(
            type_=_JSON_SERVICE_TYPE, ctx=ctx, port=_json_port(ctx),
        )
    ]
    withrottle = _withrottle_port(ctx)
    if withrottle is not None:
        services.append(_service_entry(
            type_=_WITHROTTLE_SERVICE_TYPE, ctx=ctx, port=withrottle,
        ))
    return services


class NetworkServiceService(JsonService):
    """Advertised network endpoints. Mimics the Zeroconf list JMRI Java
    produces so mobile clients see JSON + WiThrottle in one place."""

    topic = "networkService"

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return _all_services(self.ctx)

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise JsonServiceError(400, "networkService.get requires 'name'")
        for svc in _all_services(self.ctx):
            if svc["name"] == name:
                return svc
        raise JsonServiceError(404, f"no networkService named {name!r}")


# ---------------------------------------------------------------------
# systemConnection
# ---------------------------------------------------------------------
#
# Java iterates over ``ConnectionConfigManager`` (configured drivers)
# plus any additional ``SystemConnectionMemo`` instances, always
# including the ``Internal`` connection so the client knows the prefix
# for software-only beans. We ship a single active connection at a
# time; report it, plus the internal-connection entry so any client
# expecting "I" in the list still finds it.

_INTERNAL_PREFIX = "I"
_INTERNAL_NAME = "Internal"


def _project_connection(conn: Any) -> dict[str, Any]:
    """Project a :class:`SystemConnection` (or the internal marker) to
    the wire shape. ``mfg`` and ``description`` are ``null`` -- our
    connections don't yet carry either field, and the schema allows
    both to be null."""
    return {
        "name": getattr(conn, "name", None) or None,
        "prefix": getattr(conn, "prefix", ""),
        "mfg": None,
        "description": None,
    }


def _all_connections(ctx: Any) -> list[dict[str, Any]]:
    active = ctx.connection
    projections = [_project_connection(active)]
    # Always include the internal connection so clients that expect an
    # "I" prefix in the list find it, even when the active hardware
    # connection is something else (NCE, LocoNet, DCC-EX). No duplicate
    # if the active is already Internal.
    if getattr(active, "prefix", "") != _INTERNAL_PREFIX:
        projections.append({
            "name": _INTERNAL_NAME,
            "prefix": _INTERNAL_PREFIX,
            "mfg": None,
            "description": None,
        })
    return projections


class SystemConnectionService(JsonService):
    """Active hardware connection(s). Enough for mobile clients to
    build system-name pickers and route bean commands."""

    topic = "systemConnection"

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return _all_connections(self.ctx)

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise JsonServiceError(400, "systemConnection.get requires 'name'")
        for conn in _all_connections(self.ctx):
            if conn["name"] == name:
                return conn
        raise JsonServiceError(
            404, f"no systemConnection named {name!r}",
        )
