"""FastAPI app factory."""
from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from jmri_core.hal.connection import ConnectionSettings, SystemConnection
from jmri_core.hal.internal import InternalConnection
from jmri_core.persistence import (
    PmriConfig,
    Profile,
    ProfileRegistry,
    ProfileSettings,
    default_settings_dir,
    load_pmri_config,
    load_profile_registry,
    load_profile_xml,
    profile_xml_path,
)
from jmri_core.plugins import load_connections
from jmri_core.state import LightState, SensorState, TurnoutFeedback

from jmri_server.config_store import ConfigStore
from jmri_server.context import ServerContext, ServerMetadata
from jmri_server.push import PushManager
from jmri_server.registry import ServiceRegistry
from jmri_server.routes import install_routes
from jmri_server.services import register_all

_log = logging.getLogger(__name__)


def _autolaunch_python_dirs(ctx: ServerContext) -> list[pathlib.Path]:
    """Return the list of directories the autoloader scans, in order.

    Order matters -- files loaded later can rely on names/globals set
    up by files loaded earlier. Shared first because those are the
    operator-provided helpers; per-profile second so a profile's
    scripts can build on top.
    """
    p = getattr(ctx, "_roster_profile", None)
    if not isinstance(p, Profile):
        return []
    return [p.shared_python_dir, p.python_dir]


async def _autolaunch_profile_scripts(ctx: ServerContext) -> None:
    """Launch the active profile's startup scripts via the runner.

    Two code paths, mutually exclusive:

    * **PMRI install** -- when ``profile.xml`` exists (and so
      ``ctx.profile_settings`` is populated), the ``<scripts>`` list
      inside it is authoritative. Entries run in file order, each on
      its own thread (JMRI's fire-and-forget semantic per the launcher
      spec -- AbstractAutomaton loops don't block the next script).
      Per-entry ``dir`` attributes override the ``<scripts dir="...">``
      top-level default.

    * **Pre-PMRI install** -- when ``profile.xml`` is absent (operator
      is running PMRI against a JMRI-authored settings dir with no PMRI
      files yet), fall back to the directory-glob behaviour so existing
      scripts still auto-launch. Two dirs scanned in order:
      ``<settings-root>/python/`` then ``<profile>/python/``.

    Called on initial lifespan startup and again on runtime profile
    switch. Failures surface on each script's status entry (visible
    in the /scripts UI); one bad script never blocks the rest.
    """
    # Short-circuit BEFORE building the ScriptRunner -- the runner's
    # constructor expects a real connection with bean managers, and
    # tests that use a fake connection (test_time_service.py, say)
    # don't need the autoloader to run anyway. Walk the two possible
    # work sources first and bail if neither has anything.
    profile_settings = getattr(ctx, "profile_settings", None)
    if isinstance(profile_settings, ProfileSettings):
        if not profile_settings.scripts:
            return
    else:
        dirs = [p for p in _autolaunch_python_dirs(ctx) if p.is_dir()]
        if not dirs:
            return

    runner = _get_or_create_runner(ctx)
    if runner is None:
        return

    if isinstance(profile_settings, ProfileSettings):
        _autolaunch_from_profile_xml_list(ctx, runner, profile_settings)
        return
    # Fallback: dir-glob of the two python dirs.
    for d in [p for p in _autolaunch_python_dirs(ctx) if p.is_dir()]:
        for status in runner.load_directory(d):
            _log.info("autolaunched %s script: %s", d.name, status.name)


def _get_or_create_runner(ctx: ServerContext) -> object | None:
    """Lazy-build the shared :class:`ScriptRunner` on first use.

    Returns None when ``jmri_scripting`` isn't installed -- the
    autoloader degrades to a no-op in that case. Mirrors how the
    JSON ``scripts`` service lazily attaches the runner."""
    runner = getattr(ctx, "_script_runner", None)
    if runner is not None:
        return runner
    try:
        import asyncio  # noqa: PLC0415

        from jmri_scripting import ScriptRunner  # noqa: PLC0415
    except ImportError:  # pragma: no cover -- jmri-scripting is a required dep
        return None
    runner = ScriptRunner.for_connection(
        asyncio.get_event_loop(), ctx.connection,
    )
    ctx._script_runner = runner  # type: ignore[attr-defined]
    return runner


def _autolaunch_from_profile_xml_list(
    ctx: ServerContext,
    runner: object,
    profile_settings: ProfileSettings,
) -> None:
    """Launch each ``<script>`` entry in profile.xml order.

    Resolution for each entry's filesystem path:

    * absolute ``dir`` -> use as-is;
    * relative ``dir`` -> ``<profile>/<dir>/<name>``;
    * no per-entry ``dir`` -> ``<profile>/<name>`` (profile root).

    Missing / unreadable files log a warning and move on -- same
    tolerance as JMRI's own startup list.
    """
    if not profile_settings.scripts:
        return
    p = getattr(ctx, "_roster_profile", None)
    if not isinstance(p, Profile):
        _log.warning(
            "profile.xml scripts list present but no roster "
            "profile bound to the context; skipping autolaunch",
        )
        return
    profile_root = p.profile_dir
    # ``runner`` is typed object() to avoid importing jmri_scripting
    # at module scope -- cast locally for the call.
    for entry in profile_settings.scripts:
        if entry.dir:
            script_dir = pathlib.Path(entry.dir)
        else:
            script_dir = pathlib.Path()  # relative "." -> profile_root
        if not script_dir.is_absolute():
            script_dir = profile_root / script_dir
        script_path = script_dir / entry.name
        if not script_path.is_file():
            _log.warning(
                "profile.xml script %r not found at %s", entry.name, script_path,
            )
            continue
        try:
            status = runner.run_file(script_path)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 -- surface + continue, don't stop boot
            _log.warning("autolaunched script %r failed to start: %s", entry.name, e)
            continue
        _log.info("autolaunched profile.xml script: %s", getattr(status, "name", entry.name))


async def _restore_beans_from_config(
    conn: SystemConnection, store: ConfigStore
) -> None:
    """Pre-populate the connection's bean managers from PanelPro.xml.

    Restores only beans whose system-prefix matches the active connection
    (so an NCE driver picks up ``N``-prefixed beans, an internal
    connection picks up ``I``-prefixed, etc.). Beans from other prefixes
    persist in the config file but aren't materialized -- there's no
    runtime manager that knows how to drive them. They round-trip
    untouched on the next save.
    """
    accepted_prefixes = {conn.prefix}

    for row in store.turnouts():
        if not row.system_name or row.system_name[0] not in accepted_prefixes:
            continue
        try:
            bean = await conn.turnouts.provide(row.system_name)
        except (ValueError, NotImplementedError) as e:
            _log.warning("restore turnout %s skipped: %s", row.system_name, e)
            continue
        if row.user_name:
            conn.turnouts._set_user_name(bean, row.user_name)
        if row.inverted:
            bean.inverted = True
        if row.feedback:
            try:
                bean.feedback_mode = TurnoutFeedback[row.feedback]
            except KeyError:
                pass

    for srow in store.sensors():
        if not srow.system_name or srow.system_name[0] not in accepted_prefixes:
            continue
        try:
            sbean = await conn.sensors.provide(srow.system_name)
        except (ValueError, NotImplementedError) as e:
            _log.warning("restore sensor %s skipped: %s", srow.system_name, e)
            continue
        if srow.user_name:
            conn.sensors._set_user_name(sbean, srow.user_name)
        if srow.inverted:
            sbean.inverted = True

    for lrow in store.lights():
        if not lrow.system_name or lrow.system_name[0] not in accepted_prefixes:
            continue
        try:
            lbean = await conn.lights.provide(lrow.system_name)
        except (ValueError, NotImplementedError) as e:
            _log.warning("restore light %s skipped: %s", lrow.system_name, e)
            continue
        if lrow.user_name:
            conn.lights._set_user_name(lbean, lrow.user_name)

    # Silence unused-import warnings for state enums kept around for
    # future restore extensions (light state, sensor state, ...).
    _ = (LightState, SensorState)


@dataclass
class AppHandle:
    """Bundle exposed by :func:`build_app` -- useful for tests/dev."""

    app: FastAPI
    ctx: ServerContext
    registry: ServiceRegistry
    push: PushManager


_CONNECTION_TYPE_ALIASES: dict[str, str] = {
    # Digitrax hardware (PR3/PR4, LocoBuffer-USB, DCS-5xx throttles)
    # speaks the LocoNet protocol. The launcher exposes "digitrax" as
    # its own menu entry (Case-3 feedback: users expect to see the
    # brand name they bought, not just the protocol name); route it
    # here to the installed loconet driver so no separate jmri-digitrax
    # entry-point is needed.
    "digitrax": "loconet",
}


def select_connection(
    name: str | None, settings: ConnectionSettings | None = None
) -> SystemConnection:
    """Construct a :class:`SystemConnection` by its entry-point *name*.

    *name* must match an installed ``jmri.connection`` entry point. The
    returned connection is **not** opened; :func:`build_app` opens it via
    the FastAPI lifespan, passing the stashed *settings* through.

    A small alias map (:data:`_CONNECTION_TYPE_ALIASES`) redirects
    marketing-name types ("digitrax") to the underlying protocol driver
    ("loconet") so the GUI menu can carry brand names without needing
    a parallel entry-point package per brand.
    """
    if name is None:
        return InternalConnection()
    resolved_name = _CONNECTION_TYPE_ALIASES.get(name, name)
    connections = load_connections()
    if resolved_name not in connections:
        raise ValueError(
            f"unknown connection {name!r}; installed: {sorted(connections)}"
        )
    instance = connections[resolved_name]()
    instance.__dict__["_open_settings"] = settings or {}
    return instance


def _env_connection() -> tuple[str | None, ConnectionSettings | None]:
    """Resolve the connection from environment variables.

    Reads through :mod:`jmri_core.env` so operator-facing names
    (``JMRI_CONNECTION`` / ``JMRI_CONNECTION_SETTINGS``) stay in one
    registry with the rest of the JMRI env vars.
    """
    from jmri_core import env as jenv  # noqa: PLC0415
    name = jenv.connection_name()
    settings_dict = jenv.connection_settings()
    return name, (settings_dict or None)


def build_app(
    *,
    connection: SystemConnection | None = None,
    connection_name: str | None = None,
    connection_settings: ConnectionSettings | None = None,
    metadata: ServerMetadata | None = None,
) -> AppHandle:
    """Build a JMRI server.

    Connection resolution (first match wins):

    1. *connection* -- already-constructed instance (tests use this).
    2. *connection_name* + *connection_settings* -- entry-point name.
    3. ``JMRI_CONNECTION`` / ``JMRI_CONNECTION_SETTINGS`` env vars.
    4. ``InternalConnection`` fallback.
    """
    if connection is None:
        if connection_name is None:
            connection_name, env_settings = _env_connection()
            if connection_settings is None:
                connection_settings = env_settings
        connection = select_connection(connection_name, connection_settings)
    conn = connection
    ctx = ServerContext(connection=conn, metadata=metadata or ServerMetadata())
    # Every context gets an empty LogixNGManager so services can rely
    # on it being present. ``create_app`` (with a real profile) loads
    # YAML programs into it during profile-persistence attachment.
    try:
        from jmri_logixng import LogixNGManager  # noqa: PLC0415
        ctx.logix_ng = LogixNGManager()
    except ImportError:
        # jmri-logixng is an optional install; leave ctx.logix_ng None
        # and the service will just report "not available" on any
        # request.
        pass

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        # Configure logging FIRST -- uvicorn installs its own root
        # handlers during startup before our lifespan runs, and if we
        # configure before uvicorn does, uvicorn's ``dictConfig`` call
        # wipes our file handler. Doing it inside lifespan is the one
        # spot that survives that reset. The resolved log-file path
        # (or ``None`` when ``JMRI_LOG_FILE`` isn't set) also lands on
        # the context here so the /logs endpoint and the home widget
        # see it after the first request.
        from jmri_core.log import configure as _configure_log  # noqa: PLC0415
        try:
            resolved = _configure_log()
        except Exception:  # noqa: BLE001
            _log.exception("logging bootstrap failed")
            resolved = None
        ctx._log_file = resolved  # type: ignore[attr-defined]
        if resolved is not None:
            _log.info("JMRI logs writing to %s", resolved)
        else:
            _log.info(
                "JMRI file logging disabled (JMRI_LOG_FILE unset); "
                "console only"
            )
        if not conn.is_open:
            settings = conn.__dict__.pop("_open_settings", None)
            await conn.open(settings or {})
        # Pre-populate beans from disk if a config store has been attached.
        # The store is opt-in (set by ``create_app()`` for the production
        # entry point; tests skip it via ``build_app()`` directly).
        if isinstance(ctx.config_store, ConfigStore):
            await _restore_beans_from_config(conn, ctx.config_store)
        # Load LogixNG programs from the profile's logixng dir if
        # ``attach_profile_persistence`` recorded one.
        logixng_dir = getattr(ctx, "_logixng_dir", None)
        if (
            ctx.logix_ng is not None
            and logixng_dir is not None
            and hasattr(logixng_dir, "is_dir")
            and logixng_dir.is_dir()
        ):
            try:
                loaded = await ctx.logix_ng.load_all(logixng_dir)
            except Exception:  # noqa: BLE001
                _log.exception("failed to load LogixNG programs")
            else:
                if loaded:
                    _log.info("loaded %d LogixNG program(s)", loaded)
        # Auto-launch profile scripts (jython/*.py) -- alpha tester's
        # explicit request that profile Load also fires up the
        # railroad's startup scripts.
        await _autolaunch_profile_scripts(ctx)
        # Start the WiThrottle TCP server if it was attached.
        withrottle = getattr(ctx, "withrottle", None)
        if withrottle is not None:
            host = getattr(ctx, "_withrottle_host", "0.0.0.0")
            port = getattr(ctx, "_withrottle_port", 12090)
            try:
                await withrottle.start(host=host, port=port)
            except OSError as e:
                _log.warning("WiThrottle could not bind %s:%d (%s)", host, port, e)
        # Periodic hardware-clock sync -- see :mod:`jmri_server.clock_sync`.
        # A no-op when the active connection has no ``read_hardware_clock``.
        from jmri_server.clock_sync import start_sync_task  # noqa: PLC0415
        ctx._clock_sync_task = start_sync_task(ctx)  # type: ignore[attr-defined]
        try:
            yield
        finally:
            from jmri_server.clock_sync import stop_sync_task  # noqa: PLC0415
            await stop_sync_task(getattr(ctx, "_clock_sync_task", None))
            if withrottle is not None and withrottle.is_running:
                await withrottle.stop()
            await push.aclose()
            if conn.is_open:
                await conn.close()

    app = FastAPI(
        title="jmri-server",
        version=ctx.metadata.jmri_version,
        lifespan=lifespan,
    )
    # Auth middleware wraps every request. Its dispatch is a no-op
    # when ``ctx.auth is None`` (auth disabled) so registering it
    # unconditionally keeps the code path uniform. Add BEFORE
    # ``install_routes`` so it wraps them all; FastAPI applies
    # middleware in reverse-registration order (last-in, first-out),
    # so ``AuthMiddleware`` here means it's the OUTERMOST layer.
    from jmri_server.auth.middleware import AuthMiddleware  # noqa: PLC0415
    app.add_middleware(AuthMiddleware, ctx=ctx)

    registry = ServiceRegistry(ctx)
    register_all(registry)
    push = PushManager(ctx, registry)
    install_routes(app, ctx, registry, push)
    return AppHandle(app=app, ctx=ctx, registry=registry, push=push)


def attach_auth(handle: AppHandle, settings_root: pathlib.Path) -> None:
    """Enable auth when ``JMRI_AUTH_ENABLED=1``.

    Called by :func:`create_app` after the settings root is
    known. If the env var isn't set, this is a no-op and
    :attr:`ServerContext.auth` stays ``None`` -- every request
    then runs as admin-equivalent (see :class:`AuthMiddleware`).

    When enabled, loads or bootstraps ``users.yaml`` under
    *settings_root* and hands the manager to the context. On
    first run, the operator will find bootstrap credentials in
    ``<settings-root>/admin_password.txt`` (mode 0600).
    """
    from jmri_core import env as jenv  # noqa: PLC0415
    from jmri_core.env import _get_bool as _bool  # noqa: PLC0415

    from jmri_server.auth import AUTH_ENABLED_ENV, build_auth_manager  # noqa: PLC0415

    if not _bool(None, AUTH_ENABLED_ENV):
        _log.info("auth disabled (%s not set); every request runs as admin",
                  AUTH_ENABLED_ENV)
        return
    handle.ctx.auth = build_auth_manager(pathlib.Path(settings_root))
    _log.warning(
        "auth ENABLED -- %d user(s) loaded. First-run bootstrap credentials "
        "(if any) are at %s/admin_password.txt.",
        len(handle.ctx.auth.users),
        settings_root,
    )
    _ = jenv  # silence unused warning; kept for future expansion


def _resolve_initial_profile(
    registry: ProfileRegistry, pmri_cfg: PmriConfig | None = None,
) -> Profile:
    """Resolve the active profile.

    Resolution order (first non-empty match wins):

    1. ``pmri.xml``'s ``<active-profile name="…"/>`` **when non-empty**
       -- the operator-set explicit override.
    2. ``<settings>/.active_profile`` marker file -- JMRI convention
       and the Case-1 (existing JMRI install) path.
    3. First entry in ``profiles.xml`` -- JMRI's implicit default.
    4. A bare ``Profile(root=<settings>, name="default")`` so a
       fresh install still boots.

    Empty ``pmri_cfg.active_profile`` is the default and means "no
    override" so a fresh PMRI install picks up the JMRI user's
    existing choice without disturbing it.
    """
    if pmri_cfg is not None and pmri_cfg.active_profile:
        hit = registry.by_name(pmri_cfg.active_profile) or registry.by_id(
            pmri_cfg.active_profile,
        )
        if hit is not None:
            return hit.to_profile(registry.settings_root)
        # Name wasn't in profiles.xml but may still be a direct
        # directory name on disk -- JMRI tolerates this and so do we.
        candidate = Profile(root=registry.settings_root, name=pmri_cfg.active_profile)
        if candidate.profile_dir.is_dir():
            return candidate
    p = registry.active_profile()
    if p is not None:
        return p
    return Profile(root=registry.settings_root, name="default")


def _resolve_profile_connection(
    profile_settings: ProfileSettings | None,
) -> tuple[str | None, ConnectionSettings | None]:
    """Pick the connection from ``profile.xml``'s ``<connections>``.

    Returns the ``(type, settings)`` tuple for :func:`select_connection`.
    None/None when the profile doesn't nominate anything (empty
    connections list, multi-connection with no default -- the picker
    the launcher runs sets JMRI_CONNECTION env vars for that case,
    which take over via the usual env fallback).
    """
    if profile_settings is None:
        return None, None
    picked = profile_settings.default_connection()
    if picked is None:
        return None, None
    return picked.type, dict(picked.settings) if picked.settings else None


def attach_withrottle_server(
    handle: AppHandle, *, host: str = "0.0.0.0", port: int = 12090,
) -> None:
    """Wire up a :class:`WiThrottleServer` on the handle's lifespan.

    Engine Driver / WiThrottle iOS clients connect to ``host:port``
    after the FastAPI lifespan opens the connection. The server shares
    the active connection's :class:`ThrottleManager`, so any throttle
    acquired over WiThrottle is the same bean the JSON/web layer drives.

    Roster is fetched lazily from the roster service so profile
    switches are picked up automatically.
    """
    try:
        from jmri_withrottle import WiThrottleServer  # noqa: PLC0415
    except ImportError:
        _log.info("jmri_withrottle not installed; WiThrottle disabled")
        return

    def roster_provider() -> list[dict]:
        """Return the active profile's roster as WiThrottle-shaped dicts.

        The RosterService builds its store lazily, but a WiThrottle
        client can connect *before* any HTTP request triggers that
        construction. Build the store directly here so the very first
        Engine Driver connection sees the full roster.
        """
        store = getattr(handle.ctx, "_roster_store", None)
        if store is None:
            from jmri_core.persistence.profile import Profile  # noqa: PLC0415

            from jmri_server.roster_store import RosterStore  # noqa: PLC0415
            profile = (
                getattr(handle.ctx, "_roster_profile", None) or Profile.default()
            )
            store = RosterStore(profile=profile)
            handle.ctx._roster_store = store  # type: ignore[attr-defined]
            # Wire the roster-change rebroadcast if the WiThrottle
            # server has installed its hook.
            attach = getattr(handle.ctx, "_attach_roster_signals", None)
            if callable(attach):
                try:
                    attach()
                except Exception:  # noqa: BLE001
                    pass
        return [
            {
                "id": entry.id,
                "dccAddress": entry.dcc_address,
                "longAddress": entry.long_address,
            }
            for entry in store
        ]

    def turnout_provider() -> list[dict]:
        """Return the connection's turnout inventory for the Turnouts tab."""
        turnouts = handle.ctx.connection.turnouts
        return [
            {
                "system_name": t.system_name,
                "user_name": t.user_name or "",
                "state": int(t.known_state),
            }
            for t in turnouts
        ]

    async def turnout_action_handler(system_name: str, action: str) -> None:
        """Apply a WiThrottle client's turnout throw/close/toggle to the bean."""
        from jmri_core.state import TurnoutState  # noqa: PLC0415
        turnouts = handle.ctx.connection.turnouts
        bean = turnouts.get_by_system_name(system_name)
        if bean is None:
            _log.info("WiThrottle turnout %s not found", system_name)
            return
        if action == "T":
            target = TurnoutState.THROWN
        elif action == "C":
            target = TurnoutState.CLOSED
        elif action == "2":
            target = (
                TurnoutState.THROWN
                if bean.commanded_state == TurnoutState.CLOSED
                else TurnoutState.CLOSED
            )
        else:
            _log.info("WiThrottle unknown turnout action %r", action)
            return
        await bean.set_commanded_state(target)

    def route_provider() -> list[dict]:
        """Return the connection's route inventory for the Routes tab.

        Routes are quiescent most of the time -- there's no "steady
        state" the way turnouts have (CLOSED/THROWN). We advertise
        every route as UNKNOWN so the client renders a resting badge;
        it flips to ACTIVE briefly when the route fires (via the
        state_changed subscription wired in :meth:`WiThrottleServer.start`).
        """
        from jmri_withrottle import protocol as wp  # noqa: PLC0415
        routes = getattr(handle.ctx.connection, "routes", None)
        if routes is None:
            return []
        return [
            {
                "system_name": r.system_name,
                "user_name": r.user_name or "",
                "state": wp.ROUTE_UNKNOWN,
            }
            for r in routes
        ]

    async def route_action_handler(system_name: str, action: str) -> None:
        """Fire the named route when the client sends ``PRA2<sysName>``.

        Other actions are logged and ignored -- JMRI's RouteController
        only recognises ``2`` (fire) as a meaningful verb.
        """
        if action != "2":
            _log.info("WiThrottle route %s action %r ignored", system_name, action)
            return
        routes = getattr(handle.ctx.connection, "routes", None)
        if routes is None:
            return
        route = routes.get_by_system_name(system_name)
        if route is None:
            _log.info("WiThrottle route %s not found", system_name)
            return
        try:
            await route.fire()
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "WiThrottle route %s fire failed: %s", system_name, e,
            )

    def fast_clock_provider() -> str | None:
        """Return the current fast-clock PFT line, or None if no clock."""
        clock = getattr(handle.ctx, "fast_clock", None)
        if clock is None:
            return None
        from jmri_withrottle import protocol as wp  # noqa: PLC0415
        # Include seconds so the client's initial extrapolation starts
        # from the correct point -- without this we lose up to 59
        # fast-seconds, which at 8x rate maps to ~7 real seconds of
        # visible drift between broadcasts.
        total = int(clock.time_seconds())
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        unix_seconds = wp.fast_clock_local_unix_seconds(h, m, s)
        # JMRI signals "stopped" by broadcasting a follow-up ``PFT`` with
        # rate 0.0; for a stopped clock we send rate 0.0 directly on the
        # single sync line -- Engine Driver reads the rate to know whether
        # to advance its local display.
        rate = clock.rate if clock.running else 0.0
        return wp.fast_clock(unix_seconds, rate)

    withrottle = WiThrottleServer(
        connection=handle.ctx.connection,
        roster_provider=roster_provider,
        fast_clock_provider=fast_clock_provider,
        turnout_provider=turnout_provider,
        turnout_action_handler=turnout_action_handler,
        route_provider=route_provider,
        route_action_handler=route_action_handler,
        railroad=handle.ctx.metadata.railroad,
    )
    handle.ctx.withrottle = withrottle  # type: ignore[attr-defined]
    # Turnout ``state_changed`` subscription is wired inside
    # ``WiThrottleServer.start()`` -- must happen post-``conn.open()``
    # because ``open()`` swaps ``conn.turnouts`` for the driver-specific
    # manager. Subscribing here would leak to the old base manager.
    handle.ctx._withrottle_host = host  # type: ignore[attr-defined]
    handle.ctx._withrottle_port = port  # type: ignore[attr-defined]

    # Rebroadcast the roster to every connected WiThrottle client when
    # the RosterStore mutates. Without this, a locomotive added via
    # the web UI is invisible to an already-connected Engine Driver
    # until it reconnects. The store is built lazily by RosterService
    # / _lazy_roster_store; subscribe on the first store we can see and
    # again on any later replacement (profile switch attach clears the
    # store attr).
    async def _rebroadcast(_entry: Any) -> None:
        await withrottle.broadcast_roster()

    def _attach_roster_signals() -> None:
        store = getattr(handle.ctx, "_roster_store", None)
        if store is None:
            return
        already = getattr(store, "_withrottle_wired", False)
        if already:
            return
        store.added.connect(_rebroadcast)
        store.updated.connect(_rebroadcast)
        store.removed.connect(_rebroadcast)
        store._withrottle_wired = True

    handle.ctx._attach_roster_signals = _attach_roster_signals  # type: ignore[attr-defined]


def attach_profile_persistence(handle: AppHandle) -> None:
    """Attach the multi-profile registry + PMRI config + ConfigStore
    to *handle*.

    Reads ``<settings>/profiles.xml`` from :func:`default_settings_dir`
    (honouring ``JMRI_SETTINGS_DIR``) and ``<settings>/pmri.xml``;
    exposes the registry on ``ctx.profile_registry``, the PMRI
    install config on ``ctx.pmri_config``, the profile-local config
    on ``ctx.profile_settings`` (only when ``profile.xml`` exists on
    disk -- see :class:`ServerContext` for why), and points
    :class:`ConfigStore` + the (lazy) roster store at the active
    profile. Idempotent -- calling twice replaces what's there.

    Used by both :func:`create_app` and by ``jmri-web``'s app factory
    so every production entry point gets profile-driven persistence.
    """
    settings_root = default_settings_dir()
    pmri_cfg = load_pmri_config(settings_root)
    try:
        registry = load_profile_registry(settings_root)
    except (OSError, ValueError) as e:
        _log.warning("could not load profiles.xml at %s: %s", settings_root, e)
        registry = ProfileRegistry(settings_root=settings_root, profiles=[])
    handle.ctx.profile_registry = registry
    handle.ctx.pmri_config = pmri_cfg
    active_profile = _resolve_initial_profile(registry, pmri_cfg)
    # Only populate profile_settings when profile.xml exists on disk;
    # the autoloader + connection resolver fall back cleanly when the
    # field is None (pre-PMRI JMRI install).
    handle.ctx.profile_settings = (
        load_profile_xml(active_profile)
        if profile_xml_path(active_profile).is_file()
        else None
    )
    handle.ctx.config_store = ConfigStore(profile=active_profile)
    # The roster service constructs its store lazily off ``_roster_profile``;
    # point it at the same profile so the roster + bean tables stay in
    # sync. Drop any pre-existing lazy store so the next request rebuilds
    # it against the new path.
    handle.ctx._roster_profile = active_profile  # type: ignore[attr-defined]
    if hasattr(handle.ctx, "_roster_store"):
        delattr(handle.ctx, "_roster_store")
    # LogixNG programs live at ``<profile>/logixng/*.yaml``. Load them
    # onto the manager built in ``build_app`` so the JSON service and
    # the /logixng page see the active profile's programs immediately.
    # We schedule the load on the app's lifespan startup rather than
    # doing it inline here so the async loader doesn't need a running
    # event loop yet (this function is sync).
    handle.ctx._logixng_dir = active_profile.logixng_dir  # type: ignore[attr-defined]


def create_app() -> FastAPI:
    """Default ASGI entry point. ``uvicorn jmri_server:create_app --factory``.

    Bootstraps a multi-profile PMRI installation:

    * Reads ``<settings>/pmri.xml`` (if present) so the launcher's
      install-level config (ports, active profile, log settings)
      is available on the context.
    * Reads ``<settings>/profiles.xml`` and exposes the registry on
      ``ctx.profile_registry``. Users editing JMRI's ``profiles.xml``
      can use this server against their existing setup.
    * Resolves the active profile (pmri.xml -> ``.active_profile``
      marker -> first profile -> fallback ``default``).
    * Reads the active profile's ``profile.xml`` (if present) and
      uses its ``<connections>`` list to pick which hardware driver
      to open. Env vars (``JMRI_CONNECTION`` /
      ``JMRI_CONNECTION_SETTINGS``) override profile.xml -- the
      launcher's one-run ``--connection NAME`` picker writes those
      env vars, so operator-interactive picks beat the stored
      default without touching profile.xml.
    * Attaches a :class:`ConfigStore` so user-created beans persist
      into the chosen profile's ``PanelPro.xml``.

    Tests that build the app via :func:`build_app` directly skip this
    setup for hermeticity.

    Logging is configured inside the lifespan (see :func:`build_app`)
    rather than here -- uvicorn's own ``dictConfig`` call happens
    between ``create_app`` returning and the lifespan startup, and it
    would wipe any handlers we installed pre-lifespan. Configuring in
    lifespan is the only spot that survives that reset.
    """
    settings_root = default_settings_dir()
    # Load pmri.xml + profile.xml eagerly so build_app sees the
    # right connection before it constructs the context.
    pmri_cfg = load_pmri_config(settings_root)
    try:
        registry = load_profile_registry(settings_root)
    except (OSError, ValueError) as e:
        _log.warning("could not load profiles.xml at %s: %s", settings_root, e)
        registry = ProfileRegistry(settings_root=settings_root, profiles=[])
    active_profile = _resolve_initial_profile(registry, pmri_cfg)
    profile_settings = (
        load_profile_xml(active_profile)
        if profile_xml_path(active_profile).is_file()
        else None
    )
    # Connection resolution: env vars (launcher's one-run override) ->
    # profile.xml <connections> -> InternalConnection fallback. Env
    # beats profile.xml so the launcher's --connection flag doesn't
    # need to mutate profile.xml for a one-off run.
    env_name, env_settings = _env_connection()
    if env_name is not None:
        name: str | None = env_name
        settings: ConnectionSettings | None = env_settings
    else:
        name, settings = _resolve_profile_connection(profile_settings)
    handle = build_app(connection_name=name, connection_settings=settings)
    # Second attach loads everything again onto the handle's context.
    # Not redundant -- build_app constructs a fresh ServerContext, so
    # the pmri_cfg / profile_settings we loaded above don't carry
    # across. attach_profile_persistence is the one that populates
    # the context fields plus ConfigStore + roster bindings.
    attach_profile_persistence(handle)
    attach_withrottle_server(handle)
    # Wire auth last so it sees the fully-populated context.
    # ``attach_auth`` is a no-op unless ``JMRI_AUTH_ENABLED=1``.
    attach_auth(handle, settings_root)
    return handle.app


async def switch_active_profile(handle: AppHandle, profile_id: str) -> None:
    """Switch the active profile at runtime.

    Clears the in-memory bean managers, repoints :class:`ConfigStore`
    and the (lazy) roster store at the new profile, then re-populates
    the connection's beans from the new profile's ``PanelPro.xml``.

    The hardware connection is **not** reopened -- driver state (open
    serial port, traffic controller, monitor task) survives the switch.
    """
    registry = handle.ctx.profile_registry
    if not isinstance(registry, ProfileRegistry):
        raise RuntimeError("server has no profile registry attached")
    ref = registry.by_id(profile_id)
    if ref is None:
        raise KeyError(f"no profile with id {profile_id!r}")

    # Persist the choice so the next startup uses it. Both bookkeeping
    # paths are updated so a profile switch survives whichever reader
    # the next launch uses: PMRI reads pmri.xml, pre-PMRI JMRI reads
    # ``.active_profile``.
    registry.set_active(profile_id)
    pmri_cfg = getattr(handle.ctx, "pmri_config", None)
    if isinstance(pmri_cfg, PmriConfig):
        pmri_cfg.active_profile = ref.name
        try:
            from jmri_core.persistence import save_pmri_config  # noqa: PLC0415
            save_pmri_config(registry.settings_root, pmri_cfg)
        except OSError as e:
            _log.warning("could not persist active profile to pmri.xml: %s", e)

    new_profile = ref.to_profile(registry.settings_root)
    conn = handle.ctx.connection

    # 1. Deregister every existing bean so a profile with disjoint names
    #    doesn't accumulate stale entries.
    for mgr in (conn.turnouts, conn.sensors, conn.lights):
        for bean in list(mgr):
            await mgr.deregister(bean)

    # 2. Re-point the persistence stores + reload the new profile's
    #    profile.xml so the script autoloader + any future connection
    #    reads see the right list.
    handle.ctx.config_store = ConfigStore(profile=new_profile)
    handle.ctx._roster_profile = new_profile  # type: ignore[attr-defined]
    handle.ctx.profile_settings = (
        load_profile_xml(new_profile)
        if profile_xml_path(new_profile).is_file()
        else None
    )
    # Force the roster service to re-create its lazy store against the new path.
    if hasattr(handle.ctx, "_roster_store"):
        delattr(handle.ctx, "_roster_store")

    # 3. Re-restore beans from the new profile's PanelPro.xml.
    await _restore_beans_from_config(conn, handle.ctx.config_store)

    # 4. Auto-launch the new profile's startup scripts.
    await _autolaunch_profile_scripts(handle.ctx)
