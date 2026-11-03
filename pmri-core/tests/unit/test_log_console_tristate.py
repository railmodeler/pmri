"""``pmri_core.log.configure`` -- the console tri-state.

Pins Case-1 feedback item 30: with a log file configured, every
INFO line was echoed to the launcher's xterm. The fix shifts the
default to "echo iff no file" and adds an explicit tri-state
override (``JMRI_LOG_CONSOLE`` env + ``log.console_echo`` field
in pmri.xml).
"""
from __future__ import annotations

import logging
import pathlib

import pytest

from pmri_core.log import configure, reset


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Drop any handlers we installed before each test so the stdlib
    root logger stays sane for other suites."""
    reset()
    yield
    reset()


def _handler_kinds() -> set[str]:
    """Return the set of handler kinds configure() installed --
    ``'stream'`` for a stderr console handler, ``'file'`` for the
    rotating file handler. Reads from the module's own ``_installed``
    list, not from ``logging.getLogger().handlers``, because pytest's
    log-capture adds its own handlers to the root logger that we
    must not confuse with ours."""
    import pmri_core.log as _mod  # noqa: PLC0415
    kinds: set[str] = set()
    for h in _mod._installed:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            kinds.add("file")
        elif isinstance(h, logging.StreamHandler):
            kinds.add("stream")
    return kinds


# ---- auto default --------------------------------------------------


def test_default_console_only_when_no_file() -> None:
    """Bare install (no file configured, no env override) -- the
    stream handler goes on so operators still see log output."""
    configure()  # no args, no env
    assert "stream" in _handler_kinds()
    assert "file" not in _handler_kinds()


def test_default_file_only_when_file_configured(tmp_path: pathlib.Path) -> None:
    """Operator sets a log file -- the stream handler stays OFF so
    the launcher's xterm doesn't duplicate every line."""
    configure(log_file=tmp_path / "x.log")
    assert "file" in _handler_kinds()
    assert "stream" not in _handler_kinds()


# ---- explicit opt-in --------------------------------------------------


def test_explicit_console_true_echos_with_file(tmp_path: pathlib.Path) -> None:
    """Operator sets both the file AND asks for the echo (useful
    for watching live during development)."""
    configure(log_file=tmp_path / "x.log", console=True)
    assert "file" in _handler_kinds()
    assert "stream" in _handler_kinds()


def test_explicit_console_false_silent_with_no_file() -> None:
    """Daemon-style run: no file, no console -> silent service.
    Honoured even though it's rare -- the tri-state exists for
    exactly these corner cases."""
    configure(console=False)
    assert _handler_kinds() == set()


# ---- env-var tri-state -----------------------------------------------


def test_env_var_overrides_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """``JMRI_LOG_CONSOLE=1`` turns on the stream handler even when
    a file is configured. Mirrors the operator-opt-in flow via
    pmri.xml's ``<log console-echo="true"/>``."""
    monkeypatch.setenv("JMRI_LOG_CONSOLE", "1")
    configure(log_file=tmp_path / "x.log")
    assert "stream" in _handler_kinds()


def test_env_var_turns_off_even_without_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JMRI_LOG_CONSOLE", "0")
    configure()  # no file
    assert "stream" not in _handler_kinds()


def test_explicit_kwarg_beats_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """When the caller passes an explicit True/False, the env is
    ignored. Lets a test or scripted call stay deterministic."""
    monkeypatch.setenv("JMRI_LOG_CONSOLE", "1")
    configure(log_file=tmp_path / "x.log", console=False)
    assert "stream" not in _handler_kinds()
