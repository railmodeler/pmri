"""``pmri_core.env`` -- one place to parse ``JMRI_*`` environment variables.

Every operator-facing knob is a ``JMRI_``-prefixed env var. Historically
these were read directly from ``os.environ`` at each call site -- which
made it easy to typo names, forget to strip whitespace, or add a knob
that never got mentioned in the docs. This module funnels every read
through one typed getter, so:

* the full name registry lives in one file (search for ``JMRI_`` here
  to see every knob);
* callers can't confuse ``JMRI_HTTP_PORT`` with ``JMRI_HTTP_PORT_``;
* defaults are documented alongside the getter;
* tests can monkeypatch a single ``os.environ`` dict.

Naming convention: getters are named after the concept, not the env
var. So :func:`http_port` reads ``JMRI_HTTP_PORT`` -- the operator-facing
name is stable, the Python API stays readable.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

_log = logging.getLogger(__name__)

# ---- registry ------------------------------------------------------------
#
# Every env var this project reads. Documented here so operators have a
# single place to look. Order matches importance / freqency of use.

ENV_CONNECTION = "JMRI_CONNECTION"
ENV_CONNECTION_SETTINGS = "JMRI_CONNECTION_SETTINGS"
ENV_SETTINGS_DIR = "JMRI_SETTINGS_DIR"
ENV_HTTP_PORT = "JMRI_HTTP_PORT"
ENV_WITHROTTLE_PORT = "JMRI_WITHROTTLE_PORT"
ENV_WITHROTTLE_HOST = "JMRI_WITHROTTLE_HOST"

ENV_LOG_LEVEL = "JMRI_LOG_LEVEL"
ENV_LOG_FILE = "JMRI_LOG_FILE"
ENV_LOG_MAX_MB = "JMRI_LOG_MAX_MB"
ENV_LOG_BACKUPS = "JMRI_LOG_BACKUPS"
# NB: ENV_LOG_CONSOLE is declared later alongside log_console() so
# the tri-state parser lives next to the constant it reads.

ENV_NCE_DEBUG = "JMRI_NCE_DEBUG"
ENV_TIME_HOME = "JMRI_TIME_HOME"

ENV_SIGNAL_XML_ROOT = "JMRI_SIGNAL_XML_ROOT"
ENV_DECODER_XML_ROOT = "JMRI_DECODER_XML_ROOT"


def all_names() -> list[str]:
    """List of every env var name this project reads. Useful for docs
    generation and for the ``/logs`` endpoint's "configured knobs"
    diagnostic block."""
    return [
        ENV_CONNECTION,
        ENV_CONNECTION_SETTINGS,
        ENV_SETTINGS_DIR,
        ENV_HTTP_PORT,
        ENV_WITHROTTLE_PORT,
        ENV_WITHROTTLE_HOST,
        ENV_LOG_LEVEL,
        ENV_LOG_FILE,
        ENV_LOG_MAX_MB,
        ENV_LOG_BACKUPS,
        ENV_LOG_CONSOLE,
        ENV_NCE_DEBUG,
        ENV_TIME_HOME,
        ENV_SIGNAL_XML_ROOT,
        ENV_DECODER_XML_ROOT,
    ]


# ---- primitive parsers ---------------------------------------------------


def _get(env: dict[str, str] | None, name: str) -> str | None:
    """Read *name* from *env* (default ``os.environ``). Trims whitespace
    on both sides -- a stray newline in a systemd EnvironmentFile is a
    frequent-enough footgun that we absorb it here rather than at
    every call site."""
    source = os.environ if env is None else env
    val = source.get(name)
    if val is None:
        return None
    val = val.strip()
    if not val:
        return None
    return val


def _get_int(
    env: dict[str, str] | None, name: str, default: int,
) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning(
            "%s=%r isn't an int; falling back to %d", name, raw, default,
        )
        return default


def _get_bool(env: dict[str, str] | None, name: str) -> bool:
    """Truthy strings: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else -- including unset -- is false. Deliberately narrow so
    ``JMRI_NCE_DEBUG=0`` reads as off."""
    raw = _get(env, name)
    if raw is None:
        return False
    return raw.lower() in ("1", "true", "yes", "on")


# ---- typed getters -------------------------------------------------------


def connection_name(env: dict[str, str] | None = None) -> str | None:
    """Which hardware driver to start (``nce``, ``loconet``, ...). ``None``
    means the server picks its default (Internal)."""
    return _get(env, ENV_CONNECTION)


def connection_settings(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Driver-specific settings blob, JSON-encoded in the env var. Missing
    or malformed -> empty dict, and the parse error is logged so the
    operator sees why their config didn't take."""
    raw = _get(env, ENV_CONNECTION_SETTINGS)
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        _log.warning(
            "%s isn't valid JSON (%s); ignoring", ENV_CONNECTION_SETTINGS, e,
        )
        return {}
    if not isinstance(parsed, dict):
        _log.warning(
            "%s must be a JSON object; got %s",
            ENV_CONNECTION_SETTINGS, type(parsed).__name__,
        )
        return {}
    return parsed


def settings_dir(env: dict[str, str] | None = None) -> pathlib.Path | None:
    """Override for the JMRI profile-root directory. ``None`` means the
    OS default (handled by :func:`pmri_core.persistence.profile.default_settings_dir`)."""
    raw = _get(env, ENV_SETTINGS_DIR)
    return pathlib.Path(raw).expanduser() if raw else None


def http_port(env: dict[str, str] | None = None, default: int = 12080) -> int:
    return _get_int(env, ENV_HTTP_PORT, default)


def withrottle_port(
    env: dict[str, str] | None = None, default: int = 12090,
) -> int:
    return _get_int(env, ENV_WITHROTTLE_PORT, default)


def withrottle_host(
    env: dict[str, str] | None = None, default: str = "0.0.0.0",
) -> str:
    return _get(env, ENV_WITHROTTLE_HOST) or default


def log_level(
    env: dict[str, str] | None = None, default: str = "INFO",
) -> str:
    """Log level for the root logger (``DEBUG`` / ``INFO`` / ``WARNING`` /
    ``ERROR``). Case-insensitive; unknown values fall back to the
    default with a warning logged once the logging is up."""
    raw = _get(env, ENV_LOG_LEVEL)
    if raw is None:
        return default.upper()
    return raw.upper()


def log_file(env: dict[str, str] | None = None) -> pathlib.Path | None:
    """Path to write logs to. ``None`` means "console only" -- matches
    the pre-Tier-2 behaviour so unattended installs don't suddenly
    grow files."""
    raw = _get(env, ENV_LOG_FILE)
    return pathlib.Path(raw).expanduser() if raw else None


def log_max_mb(env: dict[str, str] | None = None, default: int = 5) -> int:
    """Max size of a single log file before rotation, in MiB."""
    return max(_get_int(env, ENV_LOG_MAX_MB, default), 1)


def log_backups(env: dict[str, str] | None = None, default: int = 3) -> int:
    """Number of rotated log files to keep."""
    return max(_get_int(env, ENV_LOG_BACKUPS, default), 0)


ENV_LOG_CONSOLE = "JMRI_LOG_CONSOLE"


def log_console(env: dict[str, str] | None = None) -> bool | None:
    """Tri-state override for console log echo.

    * Unset -> ``None``: callers pick a sensible default (echo when
      no file is configured, otherwise file-only).
    * ``1`` / ``true`` / ``yes`` / ``on`` -> ``True``: echo to
      console AND the configured file.
    * ``0`` / ``false`` / ``no`` / ``off`` -> ``False``: suppress
      console even when no file is set (silent service for daemons).

    Added for the Case-1 feedback: with a log file configured, the
    launcher's xterm was flooding with every INFO line. Default
    is now "file-only when a file is set" (:func:`pmri_core.log.configure`
    auto-picks when this returns None)."""
    raw = _get(env, ENV_LOG_CONSOLE)
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def nce_debug(env: dict[str, str] | None = None) -> bool:
    """Verbose NCE frame logging. Off in production; NCE drivers flip
    their own logger to DEBUG when this is true."""
    return _get_bool(env, ENV_NCE_DEBUG)


def time_home(env: dict[str, str] | None = None) -> bool:
    """Emit per-phase timing warnings on the home-page render path.
    Used to diagnose the "profile-load takes 9 s" class of bug. Noisy
    if left on -- hence env-var gated."""
    return _get_bool(env, ENV_TIME_HOME)


def signal_xml_root(env: dict[str, str] | None = None) -> pathlib.Path | None:
    """Root directory of JMRI's signal-system XML corpus (typically
    ``<jmri>/xml/signals``). ``None`` means no root is configured --
    the loader treats that as "no systems available" and the JSON
    service returns an empty list."""
    raw = _get(env, ENV_SIGNAL_XML_ROOT)
    return pathlib.Path(raw).expanduser() if raw else None


def decoder_xml_root(env: dict[str, str] | None = None) -> pathlib.Path | None:
    """Root directory of JMRI's decoder-definition XML corpus
    (typically ``<jmri>/xml/decoders``). When set, the programmer
    page pre-populates its "Load catalog from directory" input so
    the operator only needs to click Load. Unset -> same
    empty-input behaviour as before this knob existed."""
    raw = _get(env, ENV_DECODER_XML_ROOT)
    return pathlib.Path(raw).expanduser() if raw else None
