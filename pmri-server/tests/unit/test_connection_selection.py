"""Connection selection via build_app + env vars."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pmri_core.hal.internal import InternalConnection

from pmri_server.app import _env_connection, build_app, select_connection


def test_select_connection_default_returns_internal() -> None:
    conn = select_connection(None)
    assert isinstance(conn, InternalConnection)


def test_select_connection_by_name_picks_entry_point() -> None:
    conn = select_connection("internal")
    assert isinstance(conn, InternalConnection)


def test_select_connection_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown connection"):
        select_connection("nope")


def test_select_connection_stashes_settings() -> None:
    conn = select_connection("internal", {"some": "settings"})
    # build_app's lifespan reads this private slot.
    assert conn.__dict__["_open_settings"] == {"some": "settings"}


def test_env_connection_reads_env_vars() -> None:
    with patch.dict(os.environ, {
        "JMRI_CONNECTION": "internal",
        "JMRI_CONNECTION_SETTINGS": '{"hello": "world"}',
    }):
        name, settings = _env_connection()
        assert name == "internal"
        assert settings == {"hello": "world"}


def test_env_connection_handles_missing_vars() -> None:
    with patch.dict(os.environ, {}, clear=True):
        name, settings = _env_connection()
        assert name is None
        assert settings is None


def test_env_connection_warns_on_bad_json() -> None:
    with patch.dict(os.environ, {"JMRI_CONNECTION_SETTINGS": "{not json"}, clear=True):
        _, settings = _env_connection()
        assert settings is None  # falls through to None on parse failure


def test_build_app_with_explicit_name() -> None:
    handle = build_app(connection_name="internal")
    assert isinstance(handle.ctx.connection, InternalConnection)


def test_build_app_env_var_picks_driver() -> None:
    with patch.dict(os.environ, {"JMRI_CONNECTION": "internal"}, clear=True):
        handle = build_app()
        assert isinstance(handle.ctx.connection, InternalConnection)
