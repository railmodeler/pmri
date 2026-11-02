"""``<settings>/pmri.xml`` -- install-level PMRI configuration.

Single source of truth for everything the PMRI launcher needs to
start the web service, replacing the ``JMRI_*`` environment variables
that previously carried the same knobs. The env vars still work as a
debug-only fallback (see :mod:`jmri_core.env`), but when ``pmri.xml``
exists its values win.

Lives at ``<settings-root>/pmri.xml`` alongside ``profiles.xml``.
JMRI Java doesn't know about this file and ignores it; PMRI reads
and writes it exclusively.

Example::

    <?xml version="1.0" encoding="UTF-8"?>
    <pmri>
      <http host="0.0.0.0" port="12080"/>
      <withrottle host="0.0.0.0" port="12090"/>
      <log level="INFO" file="" max_mb="5" backups="3"/>
      <xml-roots signal="" decoder=""/>
      <debug nce="false" time-home="false"/>
      <active-profile name="default"/>
      <launcher open-browser-on-start="true"/>
    </pmri>

Load returns defaults when the file doesn't exist, so a bare install
with no ``pmri.xml`` runs with sensible fallbacks. Save writes
through :func:`jmri_core.persistence.backup.write_with_backup` so
every edit rolls a ``.bak`` series.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from lxml import etree

from jmri_core.persistence.backup import write_with_backup

PMRI_XML = "pmri.xml"
"""Filename at the settings root. Public so the launcher can report
the path in error messages ("couldn't parse ``~/.jmri/pmri.xml``...")."""


# ----- field groups --------------------------------------------------------
#
# Each dataclass maps to a single element in pmri.xml. Defaults mirror
# what the equivalent env getters in jmri_core.env return when the var
# is unset -- so a fresh install (no pmri.xml, no env vars) behaves
# identically to the pre-pmri.xml world.


@dataclass(slots=True)
class HttpConfig:
    """uvicorn bind for the web UI + JSON API."""
    host: str = "0.0.0.0"
    port: int = 12080


@dataclass(slots=True)
class WithrottleConfig:
    """TCP bind for the Engine Driver / WiThrottle iOS protocol."""
    host: str = "0.0.0.0"
    port: int = 12090


@dataclass(slots=True)
class LogConfig:
    """File logger for the web service. ``file=None`` means console-only --
    matches the pre-config default so unattended installs don't
    silently grow files.

    ``console_echo`` is tri-state:
    * ``None`` (default) -- auto: echo to console iff no file is
      configured, i.e. logs always go SOMEWHERE but a configured
      file doesn't flood the launcher's xterm by default.
    * ``True`` -- always echo (operator opted into both the file
      and the console view).
    * ``False`` -- never echo (file-only, even if no file is
      configured, which produces a silent service -- rare but
      a valid operator choice for daemon runs).
    """
    level: str = "INFO"
    file: pathlib.Path | None = None
    max_mb: int = 5
    backups: int = 3
    console_echo: bool | None = None


@dataclass(slots=True)
class XmlRootsConfig:
    """Paths to JMRI's bundled XML corpora. ``None`` means not
    configured; the dependent features (signal systems, decoder
    programmer) treat it as "nothing available"."""
    signal: pathlib.Path | None = None
    decoder: pathlib.Path | None = None


@dataclass(slots=True)
class DebugConfig:
    """Feature-gated verbose/noisy toggles. Default off."""
    nce: bool = False
    time_home: bool = False


@dataclass(slots=True)
class LauncherConfig:
    """Preferences specific to the PMRI launcher GUI."""
    open_browser_on_start: bool = True


@dataclass(slots=True)
class AuthConfig:
    """``jmri-server`` auth settings.

    Mirrors the server's four ``JMRI_AUTH_*`` env vars so the
    launcher can carry them in pmri.xml rather than requiring the
    operator to export shell variables. The launcher publishes them
    into the environment before uvicorn starts; the server reads
    them from the environment as it does today.

    * ``enabled`` -> ``JMRI_AUTH_ENABLED=1`` when True.
    * ``ephemeral_secret`` -> ``JMRI_AUTH_EPHEMERAL_SECRET=1`` when
      True -- the server mints a fresh random session-signing
      secret on every start. Convenient for development; forces
      every active session to re-login on restart.
    * ``secret`` -> ``JMRI_AUTH_SECRET=<value>`` when non-empty --
      explicit operator-provided secret, pins session cookies
      across restarts. Beats ``ephemeral_secret``.
    * ``admin_token`` -> ``JMRI_AUTH_ADMIN_TOKEN=<value>`` when
      non-empty -- bypass token for scripted admin-only requests.
    """
    enabled: bool = False
    ephemeral_secret: bool = False
    secret: str = ""
    admin_token: str = ""


# ----- top-level ----------------------------------------------------------


@dataclass(slots=True)
class PmriConfig:
    """In-memory view of ``pmri.xml``.

    ``active_profile`` is an **optional** override. When set to a
    non-empty string, PMRI uses it as the active profile regardless of
    what the JMRI-convention ``.active_profile`` marker file says.
    When empty (the default), the launcher falls through to the
    marker, then to profiles.xml, then to a prompt -- so a Case-1
    user (existing JMRI install, first PMRI launch) picks up their
    JMRI-chosen profile without touching pmri.xml.
    """
    http: HttpConfig = field(default_factory=HttpConfig)
    withrottle: WithrottleConfig = field(default_factory=WithrottleConfig)
    log: LogConfig = field(default_factory=LogConfig)
    xml_roots: XmlRootsConfig = field(default_factory=XmlRootsConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    launcher: LauncherConfig = field(default_factory=LauncherConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    active_profile: str = ""


# ----- I/O -----------------------------------------------------------------


def pmri_config_path(settings_root: pathlib.Path) -> pathlib.Path:
    """``<settings>/pmri.xml`` -- where the config lives."""
    return pathlib.Path(settings_root) / PMRI_XML


def load_pmri_config(settings_root: pathlib.Path) -> PmriConfig:
    """Load ``<settings>/pmri.xml`` into a :class:`PmriConfig`.

    Returns a default-populated config when the file doesn't exist,
    so a fresh install boots. Parse errors propagate -- a corrupted
    pmri.xml is operator-visible and the launcher should refuse to
    silently discard it.
    """
    path = pmri_config_path(settings_root)
    if not path.exists():
        return PmriConfig()
    return load_pmri_config_bytes(path.read_bytes())


def load_pmri_config_bytes(data: bytes) -> PmriConfig:
    """Byte-level entry point for tests and in-memory round-trips."""
    root = etree.fromstring(data, parser=etree.XMLParser(remove_blank_text=False))
    if root.tag != "pmri":
        raise ValueError(
            f"pmri.xml root element must be <pmri>, got <{root.tag}>",
        )
    cfg = PmriConfig()
    for child in root:
        if child.tag == "http":
            cfg.http = HttpConfig(
                host=child.get("host", cfg.http.host),
                port=_int(child.get("port"), cfg.http.port),
            )
        elif child.tag == "withrottle":
            cfg.withrottle = WithrottleConfig(
                host=child.get("host", cfg.withrottle.host),
                port=_int(child.get("port"), cfg.withrottle.port),
            )
        elif child.tag == "log":
            cfg.log = LogConfig(
                level=child.get("level", cfg.log.level).upper(),
                file=_path(child.get("file")),
                max_mb=_int(child.get("max_mb"), cfg.log.max_mb),
                backups=_int(child.get("backups"), cfg.log.backups),
                console_echo=_tristate(child.get("console-echo")),
            )
        elif child.tag == "xml-roots":
            cfg.xml_roots = XmlRootsConfig(
                signal=_path(child.get("signal")),
                decoder=_path(child.get("decoder")),
            )
        elif child.tag == "debug":
            cfg.debug = DebugConfig(
                nce=_bool(child.get("nce"), cfg.debug.nce),
                time_home=_bool(child.get("time-home"), cfg.debug.time_home),
            )
        elif child.tag == "active-profile":
            # Empty string == "no override" (fall through to the
            # ``.active_profile`` marker and the profiles.xml list).
            cfg.active_profile = child.get("name", cfg.active_profile) or ""
        elif child.tag == "launcher":
            cfg.launcher = LauncherConfig(
                open_browser_on_start=_bool(
                    child.get("open-browser-on-start"),
                    cfg.launcher.open_browser_on_start,
                ),
            )
        elif child.tag == "auth":
            cfg.auth = AuthConfig(
                enabled=_bool(child.get("enabled"), cfg.auth.enabled),
                ephemeral_secret=_bool(
                    child.get("ephemeral-secret"), cfg.auth.ephemeral_secret,
                ),
                secret=child.get("secret", cfg.auth.secret) or "",
                admin_token=(
                    child.get("admin-token", cfg.auth.admin_token) or ""
                ),
            )
        # Unknown elements are ignored -- forward-compatibility for
        # future PMRI schema extensions.
    return cfg


def save_pmri_config(settings_root: pathlib.Path, cfg: PmriConfig) -> None:
    """Serialise *cfg* and write to ``<settings>/pmri.xml`` with a
    rolling backup. Idempotent for identical content."""
    write_with_backup(pmri_config_path(settings_root), save_pmri_config_bytes(cfg))


def save_pmri_config_bytes(cfg: PmriConfig) -> bytes:
    """Serialise *cfg* to XML bytes. Pure function for tests."""
    root = etree.Element("pmri")
    etree.SubElement(root, "http", host=cfg.http.host, port=str(cfg.http.port))
    etree.SubElement(
        root, "withrottle",
        host=cfg.withrottle.host, port=str(cfg.withrottle.port),
    )
    log_el = etree.SubElement(
        root, "log",
        level=cfg.log.level,
        file=_render_path(cfg.log.file),
        max_mb=str(cfg.log.max_mb),
        backups=str(cfg.log.backups),
    )
    # console-echo is tri-state; omit the attribute entirely when
    # None so the file stays "auto" rather than silently committing
    # to a value.
    if cfg.log.console_echo is not None:
        log_el.set("console-echo", _render_bool(cfg.log.console_echo))
    etree.SubElement(
        root, "xml-roots",
        signal=_render_path(cfg.xml_roots.signal),
        decoder=_render_path(cfg.xml_roots.decoder),
    )
    etree.SubElement(
        root, "debug",
        nce=_render_bool(cfg.debug.nce),
        **{"time-home": _render_bool(cfg.debug.time_home)},
    )
    etree.SubElement(root, "active-profile", name=cfg.active_profile)
    etree.SubElement(
        root, "launcher",
        **{"open-browser-on-start": _render_bool(cfg.launcher.open_browser_on_start)},
    )
    etree.SubElement(
        root, "auth",
        enabled=_render_bool(cfg.auth.enabled),
        **{"ephemeral-secret": _render_bool(cfg.auth.ephemeral_secret)},
        secret=cfg.auth.secret,
        **{"admin-token": cfg.auth.admin_token},
    )
    return etree.tostring(
        root, xml_declaration=True, encoding="utf-8",
        pretty_print=True, standalone=False,
    )


# ----- field coercion helpers --------------------------------------------


def _int(raw: str | None, default: int) -> int:
    """Parse an XML attribute as int, falling back to *default* on
    missing or malformed input. Matches the tolerance of the env-var
    getters in :mod:`jmri_core.env` -- a bad config file shouldn't
    stop the service from launching."""
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _tristate(raw: str | None) -> bool | None:
    """Parse an XML attribute as a tri-state bool. Missing attribute
    OR empty string returns None (= "no explicit value, pick a
    sensible default at use time"); recognised truthy/falsy values
    return the corresponding bool; anything unrecognised returns
    None too so a typo doesn't silently commit the operator to
    an arbitrary value."""
    if raw is None or raw.strip() == "":
        return None
    s = raw.strip().lower()
    if s in ("true", "yes", "1", "on"):
        return True
    if s in ("false", "no", "0", "off"):
        return False
    return None


def _bool(raw: str | None, default: bool) -> bool:
    """Parse an XML attribute as bool. ``true``/``yes``/``1``/``on``
    (case-insensitive) are true; ``false``/``no``/``0``/``off`` are
    false; anything else falls back to *default*."""
    if raw is None:
        return default
    s = raw.strip().lower()
    if s in ("true", "yes", "1", "on"):
        return True
    if s in ("false", "no", "0", "off"):
        return False
    return default


def _path(raw: str | None) -> pathlib.Path | None:
    """Parse an XML attribute as a path, with ``~`` expansion. Empty /
    missing -> None, matching the env-var convention."""
    if raw is None or raw.strip() == "":
        return None
    return pathlib.Path(raw).expanduser()


def _render_path(p: pathlib.Path | None) -> str:
    """Serialise a Path for an XML attribute. ``None`` -> empty string
    so the attribute is still present (keeps the XML structure
    stable for diffing)."""
    return "" if p is None else str(p)


def _render_bool(v: bool) -> str:
    """Boolean -> ``true`` / ``false`` strings on the wire. We use
    ``true``/``false`` rather than JMRI's roster-xml ``yes``/``no``
    convention because the field names in pmri.xml are OS-level
    knobs, not domain beans."""
    return "true" if v else "false"
