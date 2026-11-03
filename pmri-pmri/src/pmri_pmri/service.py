"""``ServiceController`` -- own the uvicorn lifecycle in a background thread.

The launcher UI needs a Start / Stop pair that doesn't block the GUI
event loop. uvicorn's programmatic API (``uvicorn.Config`` +
``uvicorn.Server``) runs its own asyncio loop; we park that loop on a
dedicated thread so the GUI can keep polling status while the service
is up.

Lifecycle:

1. :meth:`start` constructs a ``uvicorn.Server`` from the current
   :class:`~pmri_core.persistence.PmriConfig`, launches it in a
   daemon thread, and transitions ``state`` STOPPED -> STARTING.
2. :meth:`wait_until_ready` TCP-probes the bind port with 5 ms
   polling until ``server.started`` flips or a timeout elapses.
   The GUI uses this to defer "Open browser" until the server
   actually accepts.
3. :meth:`stop` sets ``server.should_exit`` and ``force_exit`` and
   joins the thread. STARTED -> STOPPING -> STOPPED.

A failed bind (port in use, permission denied) surfaces as
:attr:`state` STOPPED + :attr:`last_error` populated, which the GUI
reads and renders inline.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import uvicorn
from pmri_core.persistence import (
    Profile,
    default_settings_dir,
    load_profile_xml,
    profile_xml_path,
)

if TYPE_CHECKING:
    from pmri_core.persistence import PmriConfig

_log = logging.getLogger(__name__)


# uvicorn log_config override: no handlers on its own loggers, propagate
# to the root logger that pmri_core.log.configure() set up. Keeps every
# log line funneled through our file + tri-state console handlers
# instead of uvicorn's own ColourizedFormatter stream. "disable_existing_
# loggers": False preserves the pmri_* loggers already present when
# uvicorn calls dictConfig; the three uvicorn loggers get an explicit
# empty-handlers entry so uvicorn's defaults don't win.
_UVICORN_PROPAGATE_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
    },
}


class ServiceState(enum.Enum):
    """Four states the launcher presents to the user.

    STARTING / STOPPING are transitional -- the thread is active but
    the socket isn't yet accepting (or still is). The GUI greys the
    Start/Stop buttons during these so the user can't double-click
    into a half-torn-down state."""

    STOPPED = "stopped"
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"


@dataclass(slots=True)
class _ServerSlot:
    """Mutable per-run slot. Nulled out between runs so a fresh start
    doesn't pick up a prior crashed server's state."""

    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    error: BaseException | None = None


@dataclass
class ServiceController:
    """Threaded uvicorn controller bound to one :class:`PmriConfig`.

    *app_import* defaults to the ``pmri_web:create_app`` factory
    (string form so tests can swap in a stub ASGI app) -- the full
    stack with the HTML UI mounted on ``/`` plus the JSON API +
    WiThrottle. Pointing at ``pmri_server:create_app`` is a
    bare-API build; the operator-visible browser would then see
    ``{"detail":"Not Found"}`` at the root. The web module brings
    in the server internally, so this one factory is enough.

    Callers that already have an app instance can pass
    ``factory=False`` with a prebuilt FastAPI; the launcher
    production path uses the string form so uvicorn constructs the
    app inside its own thread's loop.
    """

    config: PmriConfig
    app_import: str = "pmri_web:create_app"
    factory: bool = True
    state: ServiceState = ServiceState.STOPPED
    last_error: str | None = None
    _slot: _ServerSlot = field(default_factory=_ServerSlot)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Host that actually accepted the ready probe. ``None`` until
    # ``wait_until_ready`` succeeds; ``url`` reads from here so the
    # LAN-IP fallback flows through to the browser-open + GUI label.
    _probed_host: str | None = None

    def start(self) -> None:
        """Launch uvicorn on a daemon thread. Idempotent while
        running: a second ``start()`` while STARTED or STARTING is
        a no-op. Returns immediately; use :meth:`wait_until_ready`
        to block for the socket to accept.

        Env-var publishing (log + auth, see :func:`_publish_env_from_config`)
        runs here, not just once at launcher startup, so GUI edits
        to the Log file / Auth fields take effect on the NEXT Start
        click without requiring a launcher restart.
        """
        with self._lock:
            if self.state in (ServiceState.STARTED, ServiceState.STARTING):
                return
            self.state = ServiceState.STARTING
            self.last_error = None
            self._slot = _ServerSlot()
            # Clear the previous probe result; wait_until_ready will
            # re-populate it on next ready-probe success.
            self._probed_host = None
            # Publish latest log + auth settings before uvicorn imports
            # the app factory -- pmri-server reads JMRI_* at import time
            # for the lifespan log handler.
            _publish_env_from_config(self.config)
            # Re-run pmri_core.log.configure() so the launcher's startup
            # stream handler (set up by cli.main) is swapped for the
            # pmri.xml-driven one. Without this, the launcher's always-on
            # stream handler keeps echoing every INFO line to the xterm
            # even when pmri.xml set console-echo="false" (feedback
            # item 30). _publish_env_from_config just emitted JMRI_LOG_*
            # so configure() with no args reads the live settings.
            # Done BEFORE _publish_connection_env_from_disk so that
            # function's "republished connection" INFO lands in the
            # configured file + obeys the console-echo tri-state --
            # not the launcher's always-on stream handler.
            from pmri_core.log import configure as _reconfigure_log  # noqa: PLC0415
            _reconfigure_log()
            # Republish JMRI_CONNECTION / JMRI_CONNECTION_SETTINGS from
            # the just-saved profile.xml. Fixes a Case-3 cold-start
            # bug: the CLI's _publish_connection_env ran before the user
            # had a profile.xml, so nothing landed in env. The server
            # then fell through to its InternalConnection fallback even
            # though profile.xml now carried the right driver. On
            # restart, cli.main's second run DID populate the env, so
            # the second launch worked -- which was the symptom.
            _publish_connection_env_from_disk(self.config)
            _log.info(
                "starting uvicorn: app=%s host=%s port=%d",
                self.app_import, self.config.http.host, self.config.http.port,
            )
            uv_config = uvicorn.Config(
                app=self.app_import,
                factory=self.factory,
                host=self.config.http.host,
                port=self.config.http.port,
                log_level=self.config.log.level.lower(),
                # Suppress uvicorn's own stream handlers so its startup
                # chatter ("Started server process", "Uvicorn running
                # on ...", GET / 200 OK) doesn't bypass our file +
                # tri-state console setup. Letting uvicorn.* loggers
                # propagate to root means the pmri.log file picks them
                # up and the launcher's xterm stays quiet when the
                # operator asked for a quiet xterm.
                log_config=_UVICORN_PROPAGATE_LOG_CONFIG,
                # The GUI drives shutdown via ``should_exit``; uvicorn's
                # own signal handlers would collide with the host app.
                lifespan="on",
            )
            self._slot.server = uvicorn.Server(uv_config)
            self._slot.thread = threading.Thread(
                target=self._run, daemon=True, name="pmri-uvicorn",
            )
            self._slot.thread.start()

    def _run(self) -> None:
        """Thread entry: run uvicorn's own asyncio loop until shutdown.

        Captures startup failures (bind collision, bad app import,
        permission denied, ...) and surfaces them via ``last_error``
        + state=STOPPED. Uses :class:`BaseException` because uvicorn
        calls ``sys.exit(1)`` on an app-load failure, which raises
        :class:`SystemExit` (NOT :class:`Exception`); a bare
        ``except Exception`` would let that fall through our handler
        and leave ``last_error`` empty for the GUI.
        """
        assert self._slot.server is not None
        try:
            asyncio.run(self._slot.server.serve())
        except BaseException as e:
            self._slot.error = e
            _log.exception("uvicorn exited with error: %s", e)
        finally:
            with self._lock:
                self.state = ServiceState.STOPPED
                if self._slot.error is not None:
                    self.last_error = f"{type(self._slot.error).__name__}: {self._slot.error}"

    def wait_until_ready(self, *, timeout: float = 5.0) -> bool:
        """Block until the uvicorn server is accepting connections or
        *timeout* elapses. Returns True on success, False on timeout
        / bind failure.

        Primary probe is ``_probe_host(host):port`` (``127.0.0.1`` for
        a listen-everywhere bind). If the primary probe times out AND
        the bind was ``0.0.0.0`` / ``::``, retries once against the
        machine's LAN IP so the service is reachable from "somewhere"
        on a dual-stack / weird-loopback host. The working host is
        cached on :attr:`_probed_host`; :attr:`url` reads from that
        cache so the browser-open and GUI URL label see the host that
        actually accepted.

        On bind failure the thread sets state=STOPPED + last_error
        and we return False promptly.
        """
        primary = _probe_host(self.config.http.host)
        if self._wait_for_host(primary, timeout):
            with self._lock:
                self._probed_host = primary
            return True
        # Fallback: only when the primary was a loopback coercion (bind
        # was 0.0.0.0 / ::), try the LAN IP so an operator connecting
        # from another host on the LAN can still reach the service.
        if primary == "127.0.0.1":
            lan = _lan_ip()
            if lan and lan != primary and self._wait_for_host(lan, timeout):
                _log.warning(
                    "primary probe 127.0.0.1 timed out; LAN IP %s "
                    "accepted -- browser URL will use that address.",
                    lan,
                )
                with self._lock:
                    self._probed_host = lan
                return True
        return False

    def _wait_for_host(self, host: str, timeout: float) -> bool:
        """One-host probe loop. Shared between the primary + LAN-IP
        fallback paths in :meth:`wait_until_ready`.

        A tick succeeds only when BOTH ``server.started`` is True
        AND a TCP probe to ``host:port`` connects. The combined
        check matters for the LAN-IP fallback: uvicorn's
        ``server.started`` flipping on a 0.0.0.0 bind doesn't
        prove any specific interface routes, so the TCP probe
        gates on actual connectivity. Without the AND, a broken
        127.0.0.1 but working LAN IP would wrongly short-circuit
        at the primary.
        """
        deadline = time.monotonic() + timeout
        port = self.config.http.port
        while time.monotonic() < deadline:
            if self.state == ServiceState.STOPPED:
                return False
            server = self._slot.server
            if (
                server is not None
                and server.started
                and _tcp_probe(host, port)
            ):
                with self._lock:
                    self.state = ServiceState.STARTED
                return True
            time.sleep(0.01)
        return False

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Signal uvicorn to shut down and join the thread.

        Uses the graceful shutdown path (``should_exit = True`` only,
        no ``force_exit``). ``force_exit`` cancels uvicorn's lifespan
        task mid-``receive()``, which propagates a
        :class:`asyncio.CancelledError` traceback through
        starlette's lifespan handler to stderr -- noisy and
        operator-confusing on a clean quit. Graceful shutdown lets
        uvicorn drain the lifespan queue, send ``lifespan.shutdown``,
        and close normally.

        If the join times out (``join_timeout`` default 5 s), the
        thread stays alive and the operator can either close the
        window again (idempotent) or kill the process. In practice
        the join completes in well under a second for the typical
        no-active-WebSocket case.

        Idempotent: calling stop() when already STOPPED is a no-op.
        """
        with self._lock:
            if self.state == ServiceState.STOPPED:
                return
            self.state = ServiceState.STOPPING
            server = self._slot.server
            thread = self._slot.thread
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=join_timeout)
        with self._lock:
            self.state = ServiceState.STOPPED

    @property
    def url(self) -> str:
        """Browser-ready URL for the running service.

        Prefers the host that actually accepted the ready probe
        (``_probed_host`` -- set by :meth:`wait_until_ready` which
        tries 127.0.0.1 first and falls back to the LAN IP). Before
        the probe runs, uses the coerced bind host so a pre-start
        URL display shows something sensible.
        """
        host = self._probed_host or _probe_host(self.config.http.host)
        return f"http://{host}:{self.config.http.port}/"


def _probe_host(bind_host: str) -> str:
    """Map a bind host to a dialable one. ``0.0.0.0`` (listen on all
    interfaces) isn't a destination address; use ``127.0.0.1`` for
    the probe + browser URL. IPv6 ``::`` gets the same treatment."""
    if bind_host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return bind_host


def _lan_ip() -> str | None:
    """Return the host's primary LAN IP, or None if we can't guess one.

    Uses the UDP "connect-to-a-public-address, read back the source"
    trick: no packet is actually sent, but the kernel's route
    lookup for 8.8.8.8:80 picks the interface that would carry it,
    and :func:`socket.getsockname` reports that interface's IP.
    Returns None on an unroutable host (no network, loopback-only)
    or when the lookup picks 127.0.0.1 (the UDP-connect trick
    degenerates to loopback on an isolated machine; useless as a
    fallback since 127.0.0.1 is exactly what we're trying to
    bypass)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Doesn't actually open a connection -- UDP sockets
            # don't dial. Just hands the kernel enough info to pick
            # a route + source address.
            s.connect(("8.8.8.8", 80))
            ip: str = s.getsockname()[0]
    except OSError:
        return None
    if ip.startswith("127."):
        return None
    return ip


def _tcp_probe(host: str, port: int, *, timeout: float = 0.1) -> bool:
    """One-shot TCP connect probe. True iff a connection succeeds
    within *timeout* seconds. Suppresses every network error (host
    down, refused, timeout) because the caller's whole job is to
    retry until the server is up."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _publish_env_from_config(  # noqa: PLR0912 -- flat env-publishing chain
    cfg: PmriConfig,
) -> None:
    """Push pmri.xml's log + auth settings into env vars so
    pmri-server reads them from the environment.

    Imported in :meth:`ServiceController.start` so GUI edits to the
    PMRI Options tab + Save take effect on the next Start click
    without a launcher restart. The connection-env publish stays in
    the CLI (it's a one-run override tied to the picker, not a
    persistent config knob).
    """
    import os  # noqa: PLC0415

    # Log settings -- mirrors pmri_pmri.cli._publish_log_env.
    # Default to "pmri.log" (resolved against the settings dir)
    # when the operator hasn't set an explicit value. Matches the
    # launcher's own :func:`pmri_pmri.cli._reconfigure_launcher_log`
    # default so the server's logs always land SOMEWHERE a Case-1
    # user can tail -- without this fallback a pmri.xml that
    # predates PMRI had an empty log.file and the server fell back
    # to console-only, which then re-echoed every INFO line to the
    # launcher's xterm (feedback cycle after beta37).
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import default_settings_dir  # noqa: PLC0415
    log_path = cfg.log.file if cfg.log.file is not None else pathlib.Path("pmri.log")
    # A bare filename (``pmri.log``) is resolved against the JMRI
    # settings directory so it lands next to ``jmri.log``, not in
    # whatever CWD the launcher was run from. Fully qualified paths
    # pass through unchanged.
    if not log_path.is_absolute():
        log_path = default_settings_dir() / log_path
    os.environ["JMRI_LOG_FILE"] = str(log_path)
    if cfg.log.level:
        os.environ["JMRI_LOG_LEVEL"] = cfg.log.level
    if cfg.log.max_mb != 5:  # noqa: PLR2004 -- matches env.log_max_mb default
        os.environ["JMRI_LOG_MAX_MB"] = str(cfg.log.max_mb)
    else:
        os.environ.pop("JMRI_LOG_MAX_MB", None)
    if cfg.log.backups != 3:  # noqa: PLR2004 -- matches env.log_backups default
        os.environ["JMRI_LOG_BACKUPS"] = str(cfg.log.backups)
    else:
        os.environ.pop("JMRI_LOG_BACKUPS", None)
    # console_echo is tri-state: None -> let pmri-server auto-pick
    # (echo iff no file), True / False -> explicit operator opt-in.
    if cfg.log.console_echo is True:
        os.environ["JMRI_LOG_CONSOLE"] = "1"
    elif cfg.log.console_echo is False:
        os.environ["JMRI_LOG_CONSOLE"] = "0"
    else:
        os.environ.pop("JMRI_LOG_CONSOLE", None)

    # XML roots -- JMRI's signal + decoder XML corpora. Previously
    # only accessible via env vars; now carried in pmri.xml too.
    if cfg.xml_roots.signal is not None:
        os.environ["JMRI_SIGNAL_XML_ROOT"] = str(cfg.xml_roots.signal)
    else:
        os.environ.pop("JMRI_SIGNAL_XML_ROOT", None)
    if cfg.xml_roots.decoder is not None:
        os.environ["JMRI_DECODER_XML_ROOT"] = str(cfg.xml_roots.decoder)
    else:
        os.environ.pop("JMRI_DECODER_XML_ROOT", None)

    # Auth settings -- mirrors pmri_pmri.cli._publish_auth_env.
    if cfg.auth.enabled:
        os.environ["JMRI_AUTH_ENABLED"] = "1"
    else:
        os.environ.pop("JMRI_AUTH_ENABLED", None)
    if cfg.auth.ephemeral_secret:
        os.environ["JMRI_AUTH_EPHEMERAL_SECRET"] = "1"
    else:
        os.environ.pop("JMRI_AUTH_EPHEMERAL_SECRET", None)
    if cfg.auth.secret:
        os.environ["JMRI_AUTH_SECRET"] = cfg.auth.secret
    else:
        os.environ.pop("JMRI_AUTH_SECRET", None)
    if cfg.auth.admin_token:
        os.environ["JMRI_AUTH_ADMIN_TOKEN"] = cfg.auth.admin_token
    else:
        os.environ.pop("JMRI_AUTH_ADMIN_TOKEN", None)


def _publish_connection_env_from_disk(cfg: PmriConfig) -> None:
    """Re-read the active profile's ``profile.xml`` and push its
    default connection into ``JMRI_CONNECTION`` + ``JMRI_CONNECTION_SETTINGS``.

    Called by :meth:`ServiceController.start` so the server picks up
    the connection the operator just saved via the GUI, without
    waiting for a launcher restart.

    Resolution matches the server's own :func:`pmri_server.app._resolve_initial_profile`:
    honour ``cfg.active_profile`` when non-empty, else fall back to
    the platform-default profile name. Silently clears both env
    vars when no connection resolves -- an empty profile.xml should
    land on the server's :class:`InternalConnection` the same way
    a cold boot does.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    name_setting = cfg.active_profile or "default"
    settings_root = default_settings_dir()
    active = Profile(root=settings_root, name=name_setting)
    pxp = profile_xml_path(active)
    if not pxp.is_file():
        os.environ.pop("JMRI_CONNECTION", None)
        os.environ.pop("JMRI_CONNECTION_SETTINGS", None)
        return
    try:
        profile_settings = load_profile_xml(active)
    except (OSError, ValueError) as e:
        _log.warning("could not re-read profile.xml at %s: %s", pxp, e)
        return
    picked = profile_settings.default_connection()
    if picked is None:
        os.environ.pop("JMRI_CONNECTION", None)
        os.environ.pop("JMRI_CONNECTION_SETTINGS", None)
        return
    os.environ["JMRI_CONNECTION"] = picked.type
    os.environ["JMRI_CONNECTION_SETTINGS"] = json.dumps(picked.settings or {})
    _log.info(
        "republished connection from profile.xml: type=%s settings keys=%s",
        picked.type, sorted((picked.settings or {}).keys()),
    )
