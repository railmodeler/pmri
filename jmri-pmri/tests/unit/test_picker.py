"""Startup connection picker.

Covers the resolver's four cases:

* ``--connection NAME`` wins unconditionally.
* 0 connections -> None, launcher falls back to InternalConnection.
* 1 connection (or 1-of-N marked default) -> silent pick.
* >1 connections no default -> prompt (CLI form in headless mode).
"""
from __future__ import annotations

import io

import pytest
from jmri_core.persistence import ConnectionDef, ProfileSettings

from jmri_pmri.picker import (
    pick_connection_cli,
    resolve_connection,
)

# ---- resolver -------------------------------------------------------------


def test_resolver_returns_none_on_empty_settings() -> None:
    """No connections at all -> None. The launcher reads this as 'use
    the InternalConnection' and skips the picker."""
    picked = resolve_connection(
        ProfileSettings(), override_name=None, headless=True,
    )
    assert picked is None


def test_resolver_picks_single_without_prompting() -> None:
    """One connection, no default flag -- the picker short-circuits
    because there's only one choice."""
    only = ConnectionDef(name="layout", type="nce")
    s = ProfileSettings(connections=[only])
    assert resolve_connection(s, override_name=None, headless=True) is only


def test_resolver_honours_default_flag() -> None:
    """Multiple connections but one flagged default -- that one wins
    without a prompt."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="layout", type="nce"),
        ConnectionDef(name="prog", type="dccex", default=True),
    ])
    picked = resolve_connection(s, override_name=None, headless=True)
    assert picked is not None
    assert picked.name == "prog"


def test_resolver_override_name_wins() -> None:
    """``--connection NAME`` beats the default flag -- useful for
    scripting one-shot runs against a non-default connection."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="layout", type="nce", default=True),
        ConnectionDef(name="prog", type="dccex"),
    ])
    picked = resolve_connection(s, override_name="prog", headless=True)
    assert picked is not None
    assert picked.name == "prog"


def test_resolver_override_name_missing_returns_none() -> None:
    """A ``--connection`` naming a nonexistent connection returns
    None so the launcher aborts rather than silently falling back."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="layout", type="nce"),
    ])
    assert resolve_connection(s, override_name="bogus", headless=True) is None


def test_resolver_multiple_no_default_headless_delegates_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple connections, no default, headless mode -> CLI
    picker. We monkeypatch stdin to simulate the operator typing
    ``2``."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="layout", type="nce"),
        ConnectionDef(name="prog", type="dccex"),
    ])
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    picked = resolve_connection(s, override_name=None, headless=True)
    assert picked is not None
    assert picked.name == "prog"


# ---- CLI picker directly -------------------------------------------------


def test_cli_picker_accepts_valid_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    choices = [
        ConnectionDef(name="a", type="nce"),
        ConnectionDef(name="b", type="dccex"),
        ConnectionDef(name="c", type="loconet"),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("3\n"))
    picked = pick_connection_cli(choices)
    assert picked is not None
    assert picked.name == "c"


def test_cli_picker_retries_on_non_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer is a bad input, not a crash. The picker should
    try again rather than falling through."""
    choices = [ConnectionDef(name="a", type="nce")]
    monkeypatch.setattr("sys.stdin", io.StringIO("notanumber\n1\n"))
    picked = pick_connection_cli(choices)
    assert picked is not None
    assert picked.name == "a"


def test_cli_picker_retries_on_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = [ConnectionDef(name="a", type="nce")]
    monkeypatch.setattr("sys.stdin", io.StringIO("5\n1\n"))
    picked = pick_connection_cli(choices)
    assert picked is not None
    assert picked.name == "a"


def test_cli_picker_gives_up_after_three_bad_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = [ConnectionDef(name="a", type="nce")]
    monkeypatch.setattr("sys.stdin", io.StringIO("bad\nalso-bad\nstill-bad\n"))
    assert pick_connection_cli(choices) is None


def test_cli_picker_eof_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF on stdin -> operator declined (Ctrl+D). Not a crash."""
    choices = [ConnectionDef(name="a", type="nce")]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert pick_connection_cli(choices) is None
