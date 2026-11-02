"""CLI argument parsing + the connection-env stopgap.

The CLI itself is thin -- most behaviour is covered by the picker and
service tests. Here we pin the argparse surface so a casual rename
or default change is caught (operator-visible contract) and the
``_publish_connection_env`` stopgap writes JMRI_* env vars correctly
for Chunk C to replace.
"""
from __future__ import annotations

import json
import os

import pytest
from jmri_core.persistence import ConnectionDef, PmriConfig

from jmri_pmri.cli import (
    _apply_cli_overrides,
    _parse_args,
    _publish_connection_env,
)

# ---- argparse surface ---------------------------------------------------


def test_bare_invocation_defaults() -> None:
    """``pmri`` with no args: no overrides, GUI mode, open-browser
    inherits from pmri.xml."""
    args = _parse_args([])
    assert args.settings_dir is None
    assert args.profile is None
    assert args.connection is None
    assert args.host is None
    assert args.port is None
    assert args.headless is False
    assert args.open_browser is None  # tri-state: None => follow pmri.xml
    assert args.log_level == "INFO"


def test_headless_flag_parsed() -> None:
    args = _parse_args(["--headless"])
    assert args.headless is True


def test_no_browser_flag_sets_false() -> None:
    """``--no-browser`` sets ``open_browser`` False to override
    pmri.xml. The default is None (inherit) -- the three-state is
    important so CLI absence doesn't force True."""
    args = _parse_args(["--no-browser"])
    assert args.open_browser is False


def test_host_and_port_overrides_parsed() -> None:
    args = _parse_args(["--host", "127.0.0.1", "--port", "9999"])
    assert args.host == "127.0.0.1"
    assert args.port == 9999


def test_settings_dir_override_parsed() -> None:
    args = _parse_args(["--settings-dir", "/tmp/jmri-test"])
    assert args.settings_dir == "/tmp/jmri-test"


def test_connection_override_parsed() -> None:
    args = _parse_args(["--connection", "yard"])
    assert args.connection == "yard"


# ---- override application ------------------------------------------------


def test_overrides_mutate_pmri_config_in_place() -> None:
    """``_apply_cli_overrides`` writes CLI values onto the config
    without touching pmri.xml. Per-run only."""
    cfg = PmriConfig()
    args = _parse_args([
        "--host", "127.0.0.1",
        "--port", "9999",
        "--profile", "DRGW",
        "--no-browser",
    ])
    _apply_cli_overrides(cfg, args)
    assert cfg.http.host == "127.0.0.1"
    assert cfg.http.port == 9999
    assert cfg.active_profile == "DRGW"
    assert cfg.launcher.open_browser_on_start is False


def test_overrides_leave_untouched_fields_at_their_defaults() -> None:
    cfg = PmriConfig()
    assert cfg.http.port == 12080  # baseline
    _apply_cli_overrides(cfg, _parse_args(["--host", "127.0.0.1"]))
    assert cfg.http.host == "127.0.0.1"
    assert cfg.http.port == 12080  # untouched


# ---- connection env stopgap (removed in Chunk C) ------------------------


def test_publish_connection_env_sets_both_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stopgap writes JMRI_CONNECTION + JMRI_CONNECTION_SETTINGS
    so the current server picks up the launcher's choice. Chunk C
    replaces this with a direct call path."""
    monkeypatch.delenv("JMRI_CONNECTION", raising=False)
    monkeypatch.delenv("JMRI_CONNECTION_SETTINGS", raising=False)
    picked = ConnectionDef(
        name="layout", type="nce",
        settings={"port": "COM4", "baud": 9600},
    )
    _publish_connection_env(picked)
    assert os.environ["JMRI_CONNECTION"] == "nce"
    assert json.loads(os.environ["JMRI_CONNECTION_SETTINGS"]) == {
        "port": "COM4", "baud": 9600,
    }


def test_publish_connection_env_none_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no connection is picked, env must stay untouched -- the
    server's default (InternalConnection) kicks in."""
    monkeypatch.delenv("JMRI_CONNECTION", raising=False)
    monkeypatch.delenv("JMRI_CONNECTION_SETTINGS", raising=False)
    _publish_connection_env(None)
    assert "JMRI_CONNECTION" not in os.environ
    assert "JMRI_CONNECTION_SETTINGS" not in os.environ
