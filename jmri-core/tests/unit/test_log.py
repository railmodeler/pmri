"""Unit tests for :mod:`jmri_core.log`.

Each test resets the module's tracked handler list in teardown so we
don't leak handlers into other tests running in the same process.
"""
from __future__ import annotations

import logging
import pathlib

import pytest

from jmri_core import log as jmri_log


@pytest.fixture(autouse=True)
def _clear_handlers() -> None:
    """Reset before AND after every test -- a previous test's failure
    would otherwise leave handlers around and confuse the next run."""
    jmri_log.reset()
    yield
    jmri_log.reset()


def test_configure_installs_console_handler() -> None:
    jmri_log.configure(console=True)
    root = logging.getLogger()
    installed = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert installed, "expected a StreamHandler on the root logger"


def test_configure_without_file_returns_none() -> None:
    """A missing ``log_file`` (env or explicit) leaves no file handler
    and returns ``None`` -- matches the pre-Tier-2 "console only"
    behaviour."""
    result = jmri_log.configure(log_file=None)
    assert result is None


def test_configure_with_file_creates_parent_dir(
    tmp_path: pathlib.Path,
) -> None:
    """Operators point ``JMRI_LOG_FILE`` at a path whose parent may not
    exist yet (systemd services often use ``/var/log/jmri/<name>``).
    :func:`configure` must ``mkdir`` for them."""
    target = tmp_path / "nested" / "sub" / "jmri.log"
    resolved = jmri_log.configure(log_file=target)
    assert resolved == target
    assert target.parent.is_dir()


def test_configure_actually_writes_to_file(
    tmp_path: pathlib.Path,
) -> None:
    log_file = tmp_path / "out.log"
    jmri_log.configure(log_file=log_file, level="DEBUG", console=False)
    logging.getLogger("jmri_core.test").info("hello world")
    # Force any buffered output out -- RotatingFileHandler doesn't
    # buffer by default, but be explicit.
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.exists()
    body = log_file.read_text(encoding="utf-8")
    assert "hello world" in body
    assert "[jmri_core.test]" in body


def test_configure_is_idempotent(tmp_path: pathlib.Path) -> None:
    """A repeat call must replace handlers, not stack. Otherwise every
    log line would double after the second call -- trivial to hit in
    tests, painful in production. We track the specific file we
    installed (pytest attaches its own handler pointing at ``nul`` so
    we can't just count all FileHandlers)."""
    log_file = tmp_path / "d.log"
    jmri_log.configure(log_file=log_file, console=False)
    jmri_log.configure(log_file=log_file, console=False)
    ours = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "") == str(log_file)
    ]
    assert len(ours) == 1


def test_unknown_level_falls_back_to_info(tmp_path: pathlib.Path) -> None:
    """Typos in JMRI_LOG_LEVEL must not crash the boot -- fall back to
    INFO with a warning logged."""
    jmri_log.configure(level="LOUD", console=False)
    assert logging.getLogger().level == logging.INFO


def test_format_columns_align(tmp_path: pathlib.Path) -> None:
    """The 5-wide level column keeps ``INFO`` / ``WARN`` / ``DEBUG``
    aligned when someone greps the file. A regression here would
    make the log ugly enough to fix twice -- pin it now."""
    log_file = tmp_path / "f.log"
    jmri_log.configure(log_file=log_file, level="DEBUG", console=False)
    logging.getLogger("x").info("a")
    logging.getLogger("x").warning("b")
    for h in logging.getLogger().handlers:
        h.flush()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert any(" INFO  [x] " in line for line in lines)
    assert any(" WARNING [x] " in line for line in lines)


# ---- tail() -------------------------------------------------------------


def test_tail_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    assert jmri_log.tail(tmp_path / "nope.log", 100) == []


def test_tail_returns_last_n_lines(tmp_path: pathlib.Path) -> None:
    log_file = tmp_path / "big.log"
    log_file.write_text("\n".join(f"line-{i}" for i in range(200)), encoding="utf-8")
    result = jmri_log.tail(log_file, 5)
    assert result == [f"line-{i}" for i in range(195, 200)]


def test_tail_skips_blank_lines(tmp_path: pathlib.Path) -> None:
    """Blank lines in the middle of a log file (someone's ``print()``
    with no message, or an editor mishap) shouldn't crowd out real
    entries when the operator asks for the last N."""
    log_file = tmp_path / "sparse.log"
    log_file.write_text(
        "one\n\n\ntwo\n\nthree\n\n", encoding="utf-8",
    )
    assert jmri_log.tail(log_file, 10) == ["one", "two", "three"]


def test_tail_n_zero_returns_empty(tmp_path: pathlib.Path) -> None:
    log_file = tmp_path / "z.log"
    log_file.write_text("only\n", encoding="utf-8")
    assert jmri_log.tail(log_file, 0) == []
