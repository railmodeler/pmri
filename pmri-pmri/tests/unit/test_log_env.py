"""``_publish_log_env`` -- push pmri.xml log settings to env vars.

Mirrors the connection-env stopgap: the server reads JMRI_LOG_*
env vars directly for logging; the launcher sets them from
pmri.xml before uvicorn starts.
"""
from __future__ import annotations

import os
import pathlib

import pytest
from pmri_core.persistence import LogConfig, PmriConfig

from pmri_pmri.cli import _publish_log_env


def test_log_file_published_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """pmri.xml.log.file -> JMRI_LOG_FILE env var. Pins Case-1 item 7:
    operator entered pmri.log in the Log file field, launcher must
    carry that through. We use tmp_path to produce an OS-native
    path string so Windows ``\\`` separators don't fight the test."""
    monkeypatch.delenv("JMRI_LOG_FILE", raising=False)
    target = tmp_path / "pmri.log"
    cfg = PmriConfig(log=LogConfig(file=target))
    _publish_log_env(cfg)
    assert os.environ["JMRI_LOG_FILE"] == str(target)


def test_log_file_not_published_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare install (log.file=None) must not set JMRI_LOG_FILE --
    the server's own default (console-only) kicks in."""
    monkeypatch.delenv("JMRI_LOG_FILE", raising=False)
    _publish_log_env(PmriConfig())
    assert "JMRI_LOG_FILE" not in os.environ


def test_log_level_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JMRI_LOG_LEVEL", raising=False)
    cfg = PmriConfig(log=LogConfig(level="DEBUG"))
    _publish_log_env(cfg)
    assert os.environ["JMRI_LOG_LEVEL"] == "DEBUG"


def test_log_max_mb_only_published_when_non_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default max_mb=5 isn't worth polluting the env with -- the
    server has the same default. Only non-default values go out."""
    monkeypatch.delenv("JMRI_LOG_MAX_MB", raising=False)
    _publish_log_env(PmriConfig())  # default max_mb=5
    assert "JMRI_LOG_MAX_MB" not in os.environ
    _publish_log_env(PmriConfig(log=LogConfig(max_mb=50)))
    assert os.environ["JMRI_LOG_MAX_MB"] == "50"


def test_log_backups_only_published_when_non_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JMRI_LOG_BACKUPS", raising=False)
    _publish_log_env(PmriConfig())  # default backups=3
    assert "JMRI_LOG_BACKUPS" not in os.environ
    _publish_log_env(PmriConfig(log=LogConfig(backups=10)))
    assert os.environ["JMRI_LOG_BACKUPS"] == "10"


# ---- auth env ----------------------------------------------------------
#
# Feedback item 12: pmri.xml needs fields for the JMRI_AUTH_* env vars
# the server reads. Same stopgap pattern as the log + connection
# publishers: the server still reads env; the launcher sets env from
# pmri.xml before uvicorn starts.


from pmri_core.persistence import AuthConfig  # noqa: E402

from pmri_pmri.cli import _publish_auth_env  # noqa: E402


def test_auth_env_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh pmri.xml has auth off -- no env vars set, so a bare
    install doesn't accidentally enable auth. The server's own
    default (auth off) kicks in."""
    for v in (
        "JMRI_AUTH_ENABLED", "JMRI_AUTH_EPHEMERAL_SECRET",
        "JMRI_AUTH_SECRET", "JMRI_AUTH_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(v, raising=False)
    _publish_auth_env(PmriConfig())
    for v in (
        "JMRI_AUTH_ENABLED", "JMRI_AUTH_EPHEMERAL_SECRET",
        "JMRI_AUTH_SECRET", "JMRI_AUTH_ADMIN_TOKEN",
    ):
        assert v not in os.environ


def test_auth_enabled_flag_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JMRI_AUTH_ENABLED", raising=False)
    _publish_auth_env(PmriConfig(auth=AuthConfig(enabled=True)))
    assert os.environ["JMRI_AUTH_ENABLED"] == "1"


def test_auth_ephemeral_secret_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JMRI_AUTH_EPHEMERAL_SECRET", raising=False)
    _publish_auth_env(PmriConfig(auth=AuthConfig(ephemeral_secret=True)))
    assert os.environ["JMRI_AUTH_EPHEMERAL_SECRET"] == "1"


def test_auth_secret_published_when_non_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JMRI_AUTH_SECRET", raising=False)
    _publish_auth_env(PmriConfig(auth=AuthConfig(secret="hunter2")))
    assert os.environ["JMRI_AUTH_SECRET"] == "hunter2"


def test_auth_admin_token_published_when_non_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JMRI_AUTH_ADMIN_TOKEN", raising=False)
    _publish_auth_env(PmriConfig(auth=AuthConfig(admin_token="deadbeef")))
    assert os.environ["JMRI_AUTH_ADMIN_TOKEN"] == "deadbeef"


def test_auth_empty_secret_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-string secret must NOT set JMRI_AUTH_SECRET -- would
    stomp on an explicit shell override and let an unsigned secret
    through to the auth manager (which would reject it, but better
    to not reach there)."""
    monkeypatch.delenv("JMRI_AUTH_SECRET", raising=False)
    _publish_auth_env(PmriConfig(auth=AuthConfig(secret="")))
    assert "JMRI_AUTH_SECRET" not in os.environ


# ---- live republish on service.start (feedback item C) --------------
#
# Fixes the "pmri.log in the pmri.xml field is ignored" item: the
# launcher-start publish ran ONCE before the GUI opened; by the time
# the operator typed a filename and saved, the env was already set
# from the empty config. Publishing on every service.start reads from
# the live config, so GUI edits + Save take effect next Start.


from pmri_pmri.service import _publish_env_from_config  # noqa: E402


def test_service_republish_falls_back_to_default_pmri_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator leaves the Log file field blank (``cfg.log.file is None``):
    the service still publishes a JMRI_LOG_FILE pointing at
    ``<settings_root>/pmri.log`` so launcher boot lines + republished
    connection INFO land in a known file. This replaced the earlier
    "clear on vacate" contract -- see the Case-1 feedback cycle where
    unsigned pmri.xml left the launcher's INFO xterm-only (now fixed
    by the default fallback)."""
    from pmri_core.persistence import default_settings_dir  # noqa: PLC0415

    monkeypatch.setenv("JMRI_LOG_FILE", "/tmp/stale.log")
    _publish_env_from_config(PmriConfig())  # log.file=None by default
    expected = str(default_settings_dir() / "pmri.log")
    assert os.environ["JMRI_LOG_FILE"] == expected


def test_service_republish_sets_log_file_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    monkeypatch.delenv("JMRI_LOG_FILE", raising=False)
    target = tmp_path / "pmri.log"
    _publish_env_from_config(PmriConfig(log=LogConfig(file=target)))
    assert os.environ["JMRI_LOG_FILE"] == str(target)


def test_bare_log_filename_resolves_to_settings_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """Operator intent: ``pmri.log`` in the field means
    ``<settings>/pmri.log`` -- the same directory as ``jmri.log``.
    Pins the Case-1 feedback: ``If a full path is given, then
    assume the user knows what they are doing and actually wants
    it there rather than the normal location.``
    """
    monkeypatch.delenv("JMRI_LOG_FILE", raising=False)
    monkeypatch.setattr(
        "pmri_core.persistence.default_settings_dir",
        lambda: tmp_path,
    )
    _publish_env_from_config(PmriConfig(log=LogConfig(file=pathlib.Path("pmri.log"))))
    assert os.environ["JMRI_LOG_FILE"] == str(tmp_path / "pmri.log")


def test_absolute_log_path_passes_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """Opposite half of the resolution rule: an absolute path is
    honoured as-is, not re-rooted at the settings dir."""
    monkeypatch.delenv("JMRI_LOG_FILE", raising=False)
    monkeypatch.setattr(
        "pmri_core.persistence.default_settings_dir",
        lambda: tmp_path,
    )
    absolute = tmp_path / "elsewhere" / "pmri.log"
    _publish_env_from_config(PmriConfig(log=LogConfig(file=absolute)))
    assert os.environ["JMRI_LOG_FILE"] == str(absolute)


# ---- console echo tri-state (feedback item 30) ----------------------


def test_console_echo_none_clears_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """None (= auto) must take JMRI_LOG_CONSOLE back out of the env
    so pmri-core's ``configure`` falls through to its auto rule
    (echo iff no file)."""
    monkeypatch.setenv("JMRI_LOG_CONSOLE", "1")
    _publish_env_from_config(PmriConfig(log=LogConfig(console_echo=None)))
    assert "JMRI_LOG_CONSOLE" not in os.environ


def test_console_echo_true_sets_env_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JMRI_LOG_CONSOLE", raising=False)
    _publish_env_from_config(PmriConfig(log=LogConfig(console_echo=True)))
    assert os.environ["JMRI_LOG_CONSOLE"] == "1"


def test_console_echo_false_sets_env_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JMRI_LOG_CONSOLE", raising=False)
    _publish_env_from_config(PmriConfig(log=LogConfig(console_echo=False)))
    assert os.environ["JMRI_LOG_CONSOLE"] == "0"


# ---- xml roots (feedback item 31) ------------------------------------


def test_signal_xml_root_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """Pins Case-1 item 31: ``JMRI_SIGNAL_XML_ROOT`` and
    ``JMRI_DECODER_XML_ROOT`` are missing from pmri.xml and so the
    signal + decoder loaders start empty."""
    from pmri_core.persistence import XmlRootsConfig  # noqa: PLC0415
    monkeypatch.delenv("JMRI_SIGNAL_XML_ROOT", raising=False)
    _publish_env_from_config(PmriConfig(xml_roots=XmlRootsConfig(signal=tmp_path)))
    assert os.environ["JMRI_SIGNAL_XML_ROOT"] == str(tmp_path)


def test_decoder_xml_root_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    from pmri_core.persistence import XmlRootsConfig  # noqa: PLC0415
    monkeypatch.delenv("JMRI_DECODER_XML_ROOT", raising=False)
    _publish_env_from_config(PmriConfig(xml_roots=XmlRootsConfig(decoder=tmp_path)))
    assert os.environ["JMRI_DECODER_XML_ROOT"] == str(tmp_path)


def test_xml_roots_cleared_when_vacated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JMRI_SIGNAL_XML_ROOT", "/stale")
    monkeypatch.setenv("JMRI_DECODER_XML_ROOT", "/stale")
    _publish_env_from_config(PmriConfig())  # both default None
    assert "JMRI_SIGNAL_XML_ROOT" not in os.environ
    assert "JMRI_DECODER_XML_ROOT" not in os.environ


def test_service_republish_clears_vacated_auth_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same clear-on-revoke behaviour for auth: enabling auth, then
    saving a disabled auth config must take JMRI_AUTH_ENABLED back
    out of the env on the next start."""
    monkeypatch.setenv("JMRI_AUTH_ENABLED", "1")
    monkeypatch.setenv("JMRI_AUTH_SECRET", "stale")
    _publish_env_from_config(PmriConfig())  # auth default: all off / empty
    assert "JMRI_AUTH_ENABLED" not in os.environ
    assert "JMRI_AUTH_SECRET" not in os.environ
