"""``pmri.xml`` -- round-trip + default handling + backup-on-save."""
from __future__ import annotations

import pathlib

import pytest

from pmri_core.persistence.pmri_config import (
    DebugConfig,
    HttpConfig,
    LauncherConfig,
    LogConfig,
    PmriConfig,
    WithrottleConfig,
    XmlRootsConfig,
    load_pmri_config,
    load_pmri_config_bytes,
    pmri_config_path,
    save_pmri_config,
    save_pmri_config_bytes,
)

# ---- default construction -------------------------------------------------


def test_defaults_match_the_env_var_fallback_defaults() -> None:
    """Fresh ``PmriConfig`` must match the defaults pmri_core.env
    returns when every env var is unset -- so a bare install (no
    pmri.xml, no JMRI_* env) runs with the same ports and log level
    as the pre-config world."""
    cfg = PmriConfig()
    # Mirrors env.http_port default (12080), withrottle_port (12090),
    # log_level ("INFO"), log_max_mb (5), log_backups (3).
    assert cfg.http.host == "0.0.0.0"
    assert cfg.http.port == 12080
    assert cfg.withrottle.host == "0.0.0.0"
    assert cfg.withrottle.port == 12090
    assert cfg.log.level == "INFO"
    assert cfg.log.file is None
    assert cfg.log.max_mb == 5
    assert cfg.log.backups == 3
    assert cfg.xml_roots.signal is None
    assert cfg.xml_roots.decoder is None
    assert cfg.debug.nce is False
    assert cfg.debug.time_home is False
    # Empty default means "no override" -- the launcher falls through
    # to the ``.active_profile`` marker and the profiles.xml list.
    # Case 1 (existing JMRI install) depends on this being empty.
    assert cfg.active_profile == ""
    assert cfg.launcher.open_browser_on_start is True


# ---- load behaviour -------------------------------------------------------


def test_load_missing_file_returns_defaults(tmp_path: pathlib.Path) -> None:
    """A fresh ``~/.jmri`` with no ``pmri.xml`` must boot with
    defaults rather than raising. The launcher relies on this to
    present a working first-run state."""
    cfg = load_pmri_config(tmp_path)
    assert cfg == PmriConfig()


def test_load_full_round_trip_preserves_every_field(tmp_path: pathlib.Path) -> None:
    """Save then load must return an equivalent config. The exact
    field set + nested shape must survive the XML pipe."""
    cfg = PmriConfig(
        http=HttpConfig(host="127.0.0.1", port=8080),
        withrottle=WithrottleConfig(host="10.0.0.5", port=12091),
        log=LogConfig(
            level="DEBUG",
            file=pathlib.Path("/var/log/pmri.log"),
            max_mb=20,
            backups=10,
        ),
        xml_roots=XmlRootsConfig(
            signal=pathlib.Path("/opt/jmri/xml/signals"),
            decoder=pathlib.Path("/opt/jmri/xml/decoders"),
        ),
        debug=DebugConfig(nce=True, time_home=True),
        launcher=LauncherConfig(open_browser_on_start=False),
        active_profile="DRGW.jmri",
    )
    save_pmri_config(tmp_path, cfg)
    loaded = load_pmri_config(tmp_path)
    assert loaded == cfg


def test_load_tolerates_unknown_elements() -> None:
    """A pmri.xml from a future PMRI version with extra elements
    must still load the fields we DO know about. Forward-compat is
    cheap and prevents a schema bump from stopping the service."""
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<pmri>'
        b'<http host="192.168.1.1" port="9000"/>'
        b'<future-feature value="unknown"/>'
        b'<active-profile name="Layout1"/>'
        b'</pmri>'
    )
    cfg = load_pmri_config_bytes(xml)
    assert cfg.http.host == "192.168.1.1"
    assert cfg.http.port == 9000
    assert cfg.active_profile == "Layout1"


def test_load_tolerates_malformed_int_in_attribute() -> None:
    """A ``port="notanumber"`` falls back to the default rather than
    stopping the service. Matches the tolerance pmri_core.env
    applies to env-var ints -- a bad config shouldn't take the
    service down."""
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<pmri><http host="x" port="bogus"/></pmri>'
    )
    cfg = load_pmri_config_bytes(xml)
    assert cfg.http.host == "x"
    assert cfg.http.port == 12080  # fallback


def test_load_rejects_wrong_root_element() -> None:
    """A file whose root isn't ``<pmri>`` is a schema-level mistake
    worth surfacing to the operator rather than silently merging."""
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<something-else/>'
    )
    with pytest.raises(ValueError, match="must be <pmri>"):
        load_pmri_config_bytes(xml)


# ---- bool parsing --------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("TRUE", True),
    ("yes", True), ("1", True), ("on", True),
    ("false", False), ("False", False), ("FALSE", False),
    ("no", False), ("0", False), ("off", False),
])
def test_debug_flag_parses_common_truthy_falsy_values(
    raw: str, expected: bool,
) -> None:
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<pmri><debug nce="{raw}"/></pmri>'
    ).encode()
    cfg = load_pmri_config_bytes(xml)
    assert cfg.debug.nce is expected


def test_debug_flag_falls_back_on_garbage_input() -> None:
    xml = b'<?xml version="1.0"?><pmri><debug nce="maybe"/></pmri>'
    cfg = load_pmri_config_bytes(xml)
    # ``maybe`` isn't recognisable -> falls back to the DebugConfig
    # default (False).
    assert cfg.debug.nce is False


# ---- path expansion ------------------------------------------------------


def test_log_file_expands_tilde() -> None:
    xml = (
        b'<?xml version="1.0"?>'
        b'<pmri><log level="INFO" file="~/pmri.log"/></pmri>'
    )
    cfg = load_pmri_config_bytes(xml)
    assert cfg.log.file is not None
    # Expanded to an absolute path -- the ~ is gone.
    assert "~" not in str(cfg.log.file)


def test_empty_log_file_attribute_means_none() -> None:
    """A pmri.xml with ``file=""`` means "no file, console only" --
    matches the None default. We keep the empty attribute in the
    serialised output for diff stability, so round-trip is None."""
    xml = b'<?xml version="1.0"?><pmri><log level="INFO" file=""/></pmri>'
    cfg = load_pmri_config_bytes(xml)
    assert cfg.log.file is None


# ---- backup on save ------------------------------------------------------


def test_save_creates_backup_on_second_write(tmp_path: pathlib.Path) -> None:
    """pmri.xml edits go through ``write_with_backup``, so a change
    puts the prior content in ``pmri.xml.bak``. Covered by
    test_backup.py for the mechanism; this test pins the wiring."""
    cfg1 = PmriConfig()
    cfg1.http.port = 1111
    save_pmri_config(tmp_path, cfg1)

    cfg2 = PmriConfig()
    cfg2.http.port = 2222
    save_pmri_config(tmp_path, cfg2)

    primary = pmri_config_path(tmp_path)
    backup = primary.with_name(primary.name + ".bak")
    assert primary.exists()
    assert backup.exists()
    # Primary carries port 2222; .bak carries 1111.
    assert b'port="2222"' in primary.read_bytes()
    assert b'port="1111"' in backup.read_bytes()


# ---- bytes serialisation -------------------------------------------------


def test_save_bytes_is_pretty_printed() -> None:
    """A human reading pmri.xml in a text editor shouldn't see a
    one-line blob. Pretty-print must stay on."""
    cfg = PmriConfig()
    out = save_pmri_config_bytes(cfg)
    assert out.count(b"\n") > 5


def test_save_bytes_declares_utf8() -> None:
    """lxml serialises the encoding attribute lowercase (``'utf-8'``);
    both cases are valid per the XML spec, so match what lxml emits
    rather than normalising."""
    out = save_pmri_config_bytes(PmriConfig())
    assert out.startswith(b"<?xml")
    assert b"utf-8" in out.lower()


# ---- auth config (feedback item 12) -----------------------------------


def test_auth_defaults_are_all_off() -> None:
    """A fresh pmri.xml has auth off by default -- matches the server's
    own default (``JMRI_AUTH_ENABLED`` unset -> auth off, every
    request is admin-equivalent)."""
    cfg = PmriConfig()
    assert cfg.auth.enabled is False
    assert cfg.auth.ephemeral_secret is False
    assert cfg.auth.secret == ""
    assert cfg.auth.admin_token == ""


def test_auth_round_trips_through_xml() -> None:
    """Setting every auth field then saving + reloading must bring
    the values back exactly. The hyphenated attributes
    (``ephemeral-secret``, ``admin-token``) need the right XML
    kwargs on the write side."""
    from pmri_core.persistence import AuthConfig  # noqa: PLC0415

    cfg = PmriConfig(auth=AuthConfig(
        enabled=True,
        ephemeral_secret=True,
        secret="hunter2",
        admin_token="deadbeef",
    ))
    reloaded = load_pmri_config_bytes(save_pmri_config_bytes(cfg))
    assert reloaded.auth.enabled is True
    assert reloaded.auth.ephemeral_secret is True
    assert reloaded.auth.secret == "hunter2"
    assert reloaded.auth.admin_token == "deadbeef"


def test_auth_element_emits_hyphenated_attributes() -> None:
    """Pin the on-wire attribute names since the server reads the
    corresponding JMRI_AUTH_* env vars; a kwarg rename would silently
    break the launcher's publish path."""
    from pmri_core.persistence import AuthConfig  # noqa: PLC0415

    cfg = PmriConfig(auth=AuthConfig(
        enabled=True, ephemeral_secret=True,
        secret="x", admin_token="y",
    ))
    out = save_pmri_config_bytes(cfg)
    assert b"<auth " in out
    assert b'enabled="true"' in out
    assert b'ephemeral-secret="true"' in out
    assert b'secret="x"' in out
    assert b'admin-token="y"' in out
