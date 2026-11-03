"""Unit tests for :mod:`pmri_core.env`.

Every JMRI_* env var this project reads goes through one typed
getter. The tests here pin two things: (a) that the getter parses
values the way the docstring promises, and (b) that operator
mistakes (unset, empty, whitespace, malformed) fall back to a
predictable default with a logged warning rather than a crash.
"""
from __future__ import annotations

import logging
import pathlib

from pmri_core import env


def test_connection_name_returns_none_when_unset() -> None:
    assert env.connection_name({}) is None


def test_connection_name_trims_whitespace() -> None:
    """A stray newline in a systemd EnvironmentFile is common enough
    that we absorb it -- a plain ``nce`` and ``nce\\n`` behave the
    same."""
    assert env.connection_name({"JMRI_CONNECTION": "  nce  \n"}) == "nce"


def test_connection_name_empty_reads_as_none() -> None:
    """An operator that unsets a var by setting it to empty should
    get the "unset" behaviour, not a bogus empty string."""
    assert env.connection_name({"JMRI_CONNECTION": ""}) is None
    assert env.connection_name({"JMRI_CONNECTION": "   "}) is None


def test_connection_settings_parses_json_object() -> None:
    s = env.connection_settings({
        "JMRI_CONNECTION_SETTINGS": '{"port": "/dev/ttyUSB0"}',
    })
    assert s == {"port": "/dev/ttyUSB0"}


def test_connection_settings_malformed_json_returns_empty(
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="pmri_core.env")
    result = env.connection_settings({
        "JMRI_CONNECTION_SETTINGS": "{not: json",
    })
    assert result == {}
    assert any(
        "JMRI_CONNECTION_SETTINGS" in r.message for r in caplog.records
    )


def test_connection_settings_non_object_returns_empty(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="pmri_core.env")
    result = env.connection_settings({
        "JMRI_CONNECTION_SETTINGS": '["a", "b"]',   # array, not object
    })
    assert result == {}


def test_settings_dir_expands_tilde() -> None:
    p = env.settings_dir({"JMRI_SETTINGS_DIR": "~/JMRI"})
    assert isinstance(p, pathlib.Path)
    # ``expanduser`` fully resolved the ``~`` (path no longer starts with
    # ``~``). Exact expanded value depends on the host, so we assert
    # the tilde is gone.
    assert not str(p).startswith("~")


def test_http_port_default() -> None:
    assert env.http_port({}) == 12080


def test_http_port_override() -> None:
    assert env.http_port({"JMRI_HTTP_PORT": "9000"}) == 9000


def test_http_port_garbage_falls_back(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="pmri_core.env")
    result = env.http_port({"JMRI_HTTP_PORT": "not-a-number"}, default=8080)
    assert result == 8080
    assert any("JMRI_HTTP_PORT" in r.message for r in caplog.records)


def test_log_level_uppercases() -> None:
    assert env.log_level({"JMRI_LOG_LEVEL": "debug"}) == "DEBUG"
    assert env.log_level({}) == "INFO"


def test_log_file_none_when_unset() -> None:
    assert env.log_file({}) is None


def test_log_max_mb_clamped_to_at_least_1() -> None:
    """A 0-byte or negative rotation size is nonsense -- falls back to 1."""
    assert env.log_max_mb({"JMRI_LOG_MAX_MB": "0"}) == 1
    assert env.log_max_mb({"JMRI_LOG_MAX_MB": "-4"}) == 1


def test_log_backups_clamped_to_at_least_0() -> None:
    """Negative backup counts get clamped to 0 (no rotation kept)."""
    assert env.log_backups({"JMRI_LOG_BACKUPS": "-2"}) == 0


def test_nce_debug_truthy_variants() -> None:
    """``JMRI_NCE_DEBUG=0`` reads as off -- the narrow truthy list is
    intentional so a script that toggles ``=0``/``=1`` behaves the
    obvious way."""
    for val in ("1", "true", "True", "yes", "on"):
        assert env.nce_debug({"JMRI_NCE_DEBUG": val}), val
    for val in ("0", "false", "no", "off", ""):
        assert not env.nce_debug({"JMRI_NCE_DEBUG": val}), val


def test_time_home_default_off() -> None:
    """The per-request timing lines are noisy -- off unless explicitly
    turned on with ``JMRI_TIME_HOME=1``."""
    assert env.time_home({}) is False
    assert env.time_home({"JMRI_TIME_HOME": "1"}) is True


def test_signal_xml_root_defaults_to_none() -> None:
    assert env.signal_xml_root({}) is None


def test_signal_xml_root_expands_tilde() -> None:
    p = env.signal_xml_root({"JMRI_SIGNAL_XML_ROOT": "~/JMRI/xml/signals"})
    assert p is not None
    assert not str(p).startswith("~")


def test_decoder_xml_root_defaults_to_none() -> None:
    """Unset => ``None``. The programmer page renders an empty
    ``value=`` in that case -- same behaviour as before this knob
    existed."""
    assert env.decoder_xml_root({}) is None


def test_decoder_xml_root_expands_tilde() -> None:
    p = env.decoder_xml_root({"JMRI_DECODER_XML_ROOT": "~/JMRI/xml/decoders"})
    assert p is not None
    assert not str(p).startswith("~")


def test_all_names_returns_every_prefixed_var() -> None:
    """The registry is what docs / diagnostics look at. Every getter
    must have a matching entry so the docs stay honest."""
    names = env.all_names()
    for name in names:
        assert name.startswith("JMRI_")
    # Spot-check a few that must be present.
    for expected in (
        "JMRI_CONNECTION",
        "JMRI_LOG_FILE",
        "JMRI_HTTP_PORT",
        "JMRI_NCE_DEBUG",
        "JMRI_SIGNAL_XML_ROOT",
        "JMRI_DECODER_XML_ROOT",
    ):
        assert expected in names
