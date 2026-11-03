"""``pmri`` entry point -- argparse + mode dispatch (GUI vs headless).

Flow:

1. Parse args. ``--settings-dir`` and ``--profile`` override the
   defaults; ``--headless`` skips the GUI; ``--no-browser`` suppresses
   the "open browser on start" action; ``--host`` / ``--port`` edit
   the uvicorn bind.
2. Load ``<settings>/pmri.xml`` (defaults when absent).
3. Run profile discovery (see :mod:`pmri_pmri.discovery`): pmri.xml
   override -> ``.active_profile`` marker -> profiles.xml single ->
   prompt -> fallback. The pick is recorded to ``.active_profile``
   when discovery had to choose (auto or prompt), so subsequent
   launches skip the prompt. Case 1 (existing JMRI install) picks
   up the JMRI-chosen profile without touching anything.
4. Load the active profile's ``profile.xml`` (defaults when absent).
5. Resolve the picked connection (CLI prompt in headless, dialog in
   GUI). Store the resolved connection in the environment as the
   server's one-run override channel.
6. GUI mode: hand off to :func:`pmri_pmri.gui.run`. Headless mode:
   boot :class:`ServiceController` directly and park on the main
   thread until SIGINT.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import signal
import sys
import threading
from typing import Any

from pmri_core.log import configure as _configure_log
from pmri_core.persistence import (
    PmriConfig,
    default_settings_dir,
    load_pmri_config,
    load_profile_xml,
)

from pmri_pmri.browser import open_browser_when_ready
from pmri_pmri.discovery import discover_active_profile
from pmri_pmri.picker import (
    pick_profile_cli,
    pick_profile_dialog,
    resolve_connection,
)
from pmri_pmri.service import ServiceController

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a shell exit code; 0 on normal shutdown,
    non-zero on error / aborted-picker."""
    args = _parse_args(argv)
    # Use pmri_core.log.configure instead of logging.basicConfig so the
    # handlers we install are tracked in _installed and can be swapped
    # out when ServiceController.start() re-configures from pmri.xml.
    # basicConfig attaches a stream handler directly to the root logger
    # that _clear() can't see -- which is how an operator-opted-OFF
    # console echo in pmri.xml was still echoing every INFO line to
    # the launcher's xterm (feedback item 30).
    _configure_log(level=args.log_level, console=True)

    settings_root = args.settings_dir or default_settings_dir()
    pmri_cfg = load_pmri_config(settings_root)
    _apply_cli_overrides(pmri_cfg, args)
    # Re-configure logging now that pmri.xml is loaded, so the
    # launcher's own startup lines ("active profile: ...",
    # "starting uvicorn: ...") land in the configured log file in
    # addition to the xterm. Without this, cli's own INFO logs stay
    # xterm-only until ServiceController.start() reconfigures.
    # Bare log file names resolve against settings_root -- matches
    # the same bare-name-> settings-dir contract the service uses
    # for pmri.log (Case-1 feedback item 29).
    _reconfigure_launcher_log(pmri_cfg, settings_root)

    # Profile discovery -- walk the chain, prompt via the appropriate
    # front-end if ambiguity hits step 4. ``--profile NAME`` CLI
    # override has already been folded into pmri_cfg.active_profile
    # by _apply_cli_overrides, so step 1 of discovery catches it.
    picker = pick_profile_cli if args.headless else pick_profile_dialog
    disc = discover_active_profile(settings_root, pmri_cfg, pick=picker)
    if disc is None:
        _log.error("No profile picked; aborting launcher.")
        return 3
    _log.info(
        "active profile: %s  (via %s%s)",
        disc.profile.name, disc.source.value,
        "; recorded to .active_profile" if disc.should_persist else "",
    )

    profile = disc.profile
    profile_settings = load_profile_xml(profile)
    picked = resolve_connection(
        profile_settings,
        override_name=args.connection,
        headless=args.headless,
    )
    if picked is None and (profile_settings.connections or args.connection):
        # The operator was prompted and declined (or named a bogus
        # connection via --connection). Abort rather than silently
        # falling back to InternalConnection -- an unexpected driver
        # choice is worse than a clear failure.
        _log.error("No connection picked; aborting launcher.")
        return 2

    _publish_connection_env(picked)
    # Log + auth env vars are published by ServiceController.start()
    # from the live config, so GUI edits take effect on the next
    # Start click without requiring a launcher restart.

    if args.headless:
        return _run_headless(pmri_cfg, open_browser=args.open_browser)
    # Lazy import so a Pi install without customtkinter can still
    # ``pmri --headless`` without the dep installed.
    from pmri_pmri.gui import run as run_gui  # noqa: PLC0415

    return run_gui(
        pmri_cfg,
        settings_root=settings_root,
        profile=profile,
        profile_settings=profile_settings,
        open_browser=args.open_browser,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pmri",
        description=(
            "PMRI launcher -- the Python Model Railroad Interface. "
            "Starts the web service and (by default) opens it in a "
            "browser tab."
        ),
    )
    p.add_argument(
        "--settings-dir",
        type=str,
        default=None,
        help=(
            "JMRI/PMRI settings root (default: ~/JMRI on Windows, "
            "~/.jmri on Linux, ~/Library/Preferences/JMRI on macOS)."
        ),
    )
    p.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Override pmri.xml's active-profile name for this run.",
    )
    p.add_argument(
        "--connection",
        type=str,
        default=None,
        help=(
            "Pick a specific connection from profile.xml by name, "
            "skipping the picker when the profile has multiple."
        ),
    )
    p.add_argument(
        "--host", type=str, default=None,
        help="Override pmri.xml's HTTP bind host for this run.",
    )
    p.add_argument(
        "--port", type=int, default=None,
        help="Override pmri.xml's HTTP bind port for this run.",
    )
    p.add_argument(
        "--headless", action="store_true",
        help="Skip the GUI; run the service only (Pi / systemd use case).",
    )
    p.add_argument(
        "--no-browser", dest="open_browser", action="store_false",
        default=None,  # tri-state: None => use pmri.xml, True/False => override
        help="Don't open the browser on start (overrides pmri.xml).",
    )
    p.add_argument(
        "--log-level", type=str, default="INFO",
        help="Launcher log level (DEBUG/INFO/WARNING/ERROR).",
    )
    return p.parse_args(argv)


def _apply_cli_overrides(cfg: PmriConfig, args: argparse.Namespace) -> None:
    """Mutate *cfg* in-place to apply ``--host`` / ``--port`` /
    ``--profile`` overrides. The CLI never writes these back to
    pmri.xml -- they're per-run only, matching JMRI's own
    command-line-flag semantics."""
    if args.profile is not None:
        cfg.active_profile = args.profile
    if args.host is not None:
        cfg.http.host = args.host
    if args.port is not None:
        cfg.http.port = args.port
    if args.open_browser is not None:
        cfg.launcher.open_browser_on_start = args.open_browser


def _publish_connection_env(
    picked: ConnectionDef | None,  # type: ignore[name-defined]  # noqa: F821
) -> None:
    """Stopgap for Chunk C: the server still reads ``JMRI_CONNECTION``
    + ``JMRI_CONNECTION_SETTINGS`` env vars. The launcher writes the
    picked connection into the environment just before launching
    uvicorn so the server picks it up.

    Chunk C replaces this with a direct path (server reads the
    ``ConnectionDef`` straight from the loaded ``profile.xml``). At
    that point this function and its callers go away.
    """
    if picked is None:
        return
    os.environ["JMRI_CONNECTION"] = picked.type
    os.environ["JMRI_CONNECTION_SETTINGS"] = json.dumps(picked.settings)


def _reconfigure_launcher_log(cfg: PmriConfig, settings_root: pathlib.Path) -> None:
    """Reconfigure logging to add the file handler from pmri.xml.

    Called in :func:`main` immediately after ``load_pmri_config`` so
    the launcher's own INFO logs ("active profile: ...",
    "starting uvicorn: ...") land in the configured log file. The
    ``console`` tri-state is forwarded to :func:`pmri_core.log.configure`
    unchanged: ``None`` auto-picks (echo iff no file is configured),
    ``True`` always echoes, ``False`` never echoes. With a file in
    play the launcher's xterm stays quiet by default -- matches
    :meth:`ServiceController.start`'s own reconfigure semantics.

    When ``cfg.log.file`` is ``None`` (a pmri.xml that predates
    the PMRI launcher, or an operator who cleared the Log file
    field), we still log to ``settings_root / "pmri.log"`` so a
    Case-1 user's launcher INFO lines are preserved. This matches
    the GUI's "Log file" field prefill default and lets support
    recover from an operator who hits a startup bug.

    Rotates any prior ``pmri.log`` to ``pmri.log.1`` (shifting
    existing backups up) BEFORE opening the fresh file, so each
    launcher run writes to a clean file instead of appending to
    the previous run's log.
    """
    log_path = cfg.log.file if cfg.log.file is not None else pathlib.Path("pmri.log")
    if not log_path.is_absolute():
        log_path = settings_root / log_path
    _rotate_launcher_log(log_path, cfg.log.backups)
    _configure_log(
        level=cfg.log.level or "INFO",
        log_file=log_path,
        max_mb=cfg.log.max_mb,
        backups=cfg.log.backups,
        console=cfg.log.console_echo,
    )


def _rotate_launcher_log(log_path: pathlib.Path, backups: int) -> None:
    """Shift ``pmri.log`` -> ``pmri.log.1`` -> ``pmri.log.2`` -> ... so
    each launcher boot writes to a fresh file.

    Mirrors :class:`logging.handlers.RotatingFileHandler.doRollover` but
    runs once at startup rather than at a size threshold. Oldest
    backup beyond *backups* is deleted. Non-existent source files
    are skipped silently so a fresh install (no prior log) does
    nothing.

    OS errors (missing parent, permissions) are swallowed and
    logged -- rotation failing shouldn't prevent the launcher from
    starting; worst case we append to the existing file one more
    time, which is still better than aborting boot.
    """
    try:
        if not log_path.is_file():
            return
        parent = log_path.parent
        base = log_path.name
        # Shift pmri.log.N-1 -> pmri.log.N, deleting the top-most
        # that would overflow the backup count.
        for i in range(backups, 0, -1):
            src = parent / f"{base}.{i}"
            if not src.exists():
                continue
            if i >= backups:
                src.unlink()
            else:
                src.rename(parent / f"{base}.{i + 1}")
        # Move the current file to .1 (or delete it when backups=0,
        # i.e. the operator asked for no history).
        if backups >= 1:
            log_path.rename(parent / f"{base}.1")
        else:
            log_path.unlink()
    except OSError as e:
        # Can't just log via _log here -- rotation runs BEFORE the
        # file handler is installed, so a warning would only hit
        # the stream handler. Still worth telling the operator:
        _log.warning("could not rotate launcher log %s: %s", log_path, e)


def _publish_log_env(cfg: PmriConfig) -> None:
    """Push pmri.xml's log settings into env vars so pmri-server's
    existing env-reader (``pmri_core.env.log_file`` etc.) honours
    them. Same stopgap pattern as :func:`_publish_connection_env`:
    the server still reads ``JMRI_*`` env vars directly for the
    log pipeline, so the launcher sets them from pmri.xml before
    uvicorn starts.

    Only sets each variable when pmri.xml carries a non-default
    value, so a bare install still gets the server's own defaults
    (console-only logging, INFO level)."""
    if cfg.log.file is not None:
        os.environ["JMRI_LOG_FILE"] = str(cfg.log.file)
    if cfg.log.level:
        os.environ["JMRI_LOG_LEVEL"] = cfg.log.level
    # max_mb / backups always have an int value in the dataclass; only
    # publish when they differ from the server's own defaults so a bare
    # pmri.xml doesn't flood the env.
    if cfg.log.max_mb != 5:  # noqa: PLR2004 -- matches env.log_max_mb default
        os.environ["JMRI_LOG_MAX_MB"] = str(cfg.log.max_mb)
    if cfg.log.backups != 3:  # noqa: PLR2004 -- matches env.log_backups default
        os.environ["JMRI_LOG_BACKUPS"] = str(cfg.log.backups)


def _publish_auth_env(cfg: PmriConfig) -> None:
    """Push pmri.xml's auth settings into env vars so pmri-server's
    auth subsystem (which reads ``JMRI_AUTH_*`` directly) honours
    them. Same stopgap pattern as :func:`_publish_connection_env`.

    Each var is only set when its pmri.xml field is "on" / non-empty,
    so a bare install (auth off) doesn't accidentally enable auth
    via a stale env var the server would otherwise pick up.
    """
    if cfg.auth.enabled:
        os.environ["JMRI_AUTH_ENABLED"] = "1"
    if cfg.auth.ephemeral_secret:
        os.environ["JMRI_AUTH_EPHEMERAL_SECRET"] = "1"
    if cfg.auth.secret:
        os.environ["JMRI_AUTH_SECRET"] = cfg.auth.secret
    if cfg.auth.admin_token:
        os.environ["JMRI_AUTH_ADMIN_TOKEN"] = cfg.auth.admin_token


def _run_headless(cfg: PmriConfig, *, open_browser: bool | None) -> int:
    """Boot the service without the GUI and park until SIGINT.

    *open_browser*: None means "follow pmri.xml's
    ``open-browser-on-start``"; True/False override that. Headless
    mode on a Pi with no display will usually have this False, but
    nothing stops a desktop user from running --headless and still
    opening a tab."""
    controller = ServiceController(config=cfg)
    want_browser = (
        cfg.launcher.open_browser_on_start if open_browser is None else open_browser
    )
    stop_event = threading.Event()

    def _handle_sigint(_signum: int, _frame: Any) -> None:
        _log.info("received signal; shutting down.")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sigint)
    # SIGTERM lets systemd stop the service cleanly.
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)

    controller.start()
    if want_browser:
        # Even headless-mode users on a desktop may want the tab.
        threading.Thread(
            target=open_browser_when_ready, args=(controller,),
            daemon=True, name="pmri-browser-opener",
        ).start()
    # Still block until ready so we can report bind failures
    # before parking on SIGINT.
    elif not controller.wait_until_ready():
        _log.error("service failed to start: %s", controller.last_error)
        controller.stop()
        return 1
    _log.info("PMRI running at %s -- Ctrl+C to stop", controller.url)
    stop_event.wait()
    controller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
