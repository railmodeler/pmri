"""``jmri_core.log`` -- logging bootstrap for headless installs.

Every entry point (jmri-server's uvicorn app, WiThrottle server,
scripts) calls :func:`configure` once at startup. Configuration is
driven by the env vars in :mod:`jmri_core.env`:

* ``JMRI_LOG_LEVEL`` -- root threshold (default ``INFO``).
* ``JMRI_LOG_FILE`` -- path to a rotating log file. If unset, logs go
  to the console only, matching pre-Tier-2 behaviour so unattended
  installs don't grow files without an operator opting in.
* ``JMRI_LOG_MAX_MB`` -- rotation size in MiB (default 5).
* ``JMRI_LOG_BACKUPS`` -- number of rotated files to keep (default 3).

The unified format is:

    2026-08-04 15:03:07,412 INFO  [jmri_nce.connection] NCE ready

Timestamps use the local timezone (matches JMRI Java). Level column is
5-wide so ``INFO`` / ``WARN`` / ``ERROR`` align. Logger name goes in
brackets so grep on ``[jmri_nce`` picks up every NCE line regardless
of the sub-module.

:func:`configure` is idempotent -- a repeat call replaces the handlers
so tests can reconfigure without leaking handlers across the process.
"""
from __future__ import annotations

import logging
import logging.handlers
import pathlib
import sys

from jmri_core import env

# Column widths chosen so INFO / WARN / DEBUG all align and the logger
# name doesn't wander. Longer logger names wrap inside the brackets.
_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Tracked handlers we've installed. Kept so a re-configure removes
# them cleanly instead of adding on top (which would double every log
# line and eventually thrash file descriptors in tests).
_installed: list[logging.Handler] = []


def configure(
    *,
    level: str | None = None,
    log_file: pathlib.Path | str | None = None,
    max_mb: int | None = None,
    backups: int | None = None,
    console: bool | None = None,
) -> pathlib.Path | None:
    """Install root-logger handlers.

    Parameters mirror the :mod:`jmri_core.env` getters. Passing an
    explicit value overrides the env; passing ``None`` reads the env.

    ``console`` is tri-state:

    * ``True`` -- echo to the console (stderr) in addition to any
      configured file.
    * ``False`` -- never echo to the console (file-only, or silent
      if no file).
    * ``None`` (default) -- read ``JMRI_LOG_CONSOLE``; if still unset
      there, auto-pick: echo iff no file is configured. This gives
      a bare install visible logs AND a file-configured install a
      quiet launcher terminal.

    Returns the resolved log-file path (or ``None`` if no file handler
    was installed). Callers who want to advertise the log location on a
    status page can use the return value directly.
    """
    _clear()

    resolved_level = (level or env.log_level()).upper()
    numeric = getattr(logging, resolved_level, None)
    if not isinstance(numeric, int):
        logging.getLogger(__name__).warning(
            "unknown log level %r; using INFO", resolved_level,
        )
        numeric = logging.INFO
    root = logging.getLogger()
    root.setLevel(numeric)

    formatter = logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)

    # Resolve the file first so the auto-console branch can see
    # whether a file is in play.
    resolved_file: pathlib.Path | None = None
    if log_file is None:
        resolved_file = env.log_file()
    else:
        resolved_file = pathlib.Path(log_file).expanduser()

    # Resolve console (tri-state). None -> env lookup -> auto.
    if console is None:
        console = env.log_console()
    if console is None:
        # Auto: echo iff no file. A file-configured install stays
        # quiet; a bare install still gets stderr output so the
        # operator sees something.
        console = resolved_file is None

    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)
        _installed.append(console_handler)

    if resolved_file is not None:
        resolved_file.parent.mkdir(parents=True, exist_ok=True)
        rotate_bytes = (max_mb or env.log_max_mb()) * 1024 * 1024
        backup_count = backups if backups is not None else env.log_backups()
        file_handler = logging.handlers.RotatingFileHandler(
            resolved_file,
            maxBytes=rotate_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        _installed.append(file_handler)

    return resolved_file


def _clear() -> None:
    """Remove any handlers we've previously added. Used by
    :func:`configure` for idempotency, and by tests via
    :func:`reset`."""
    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    _installed.clear()


def reset() -> None:
    """Public alias for the private cleanup -- useful in test teardown
    so each test gets a fresh handler set."""
    _clear()


def tail(path: pathlib.Path, n: int) -> list[str]:
    """Return the last *n* non-blank log lines from *path*.

    Reads the whole file -- cheap for logs bounded by
    ``JMRI_LOG_MAX_MB`` (default 5 MB). If the file doesn't exist,
    returns an empty list rather than raising, so the /logs endpoint
    can render a graceful empty state.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-n:] if n > 0 else []
