"""``resolve_serial_port_name`` -- bare device names to full paths.

JMRI stores serial ports by their short display name (``ttyUSB0`` on
Linux, ``COM4`` on Windows). pyserial-asyncio's ``open()`` expects a
full path on POSIX systems. This helper is the one place our serial
drivers call to normalise the input before handing it to pyserial.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jmri_core.hal.serial_resolve import resolve_serial_port_name


# ---- POSIX: bare name -> /dev/<name> --------------------------------


@pytest.mark.parametrize("bare_name", [
    "ttyUSB0",       # Linux FTDI / Prolific USB-serial
    "ttyACM0",       # Linux CDC-ACM (Arduinos)
    "ttyS0",         # Linux built-in serial
    "cu.usbserial-A1", # macOS
])
def test_posix_bare_name_prepends_dev(bare_name: str) -> None:
    """JMRI's bare device names are the biggest Case-1 pain point --
    operator writes ``ttyUSB0`` in profile.xml, pyserial can't open
    it. We prepend ``/dev/`` on POSIX."""
    with patch("platform.system", return_value="Linux"):
        assert resolve_serial_port_name(bare_name) == f"/dev/{bare_name}"


def test_posix_absolute_path_passes_through() -> None:
    """A path with a slash is already resolved; don't double-prefix."""
    with patch("platform.system", return_value="Linux"):
        assert resolve_serial_port_name("/dev/ttyUSB0") == "/dev/ttyUSB0"


def test_posix_relative_path_passes_through() -> None:
    """``./ttyUSB0`` contains a slash and so passes through -- even
    if it's probably not what the operator meant. Don't double-handle."""
    with patch("platform.system", return_value="Linux"):
        assert resolve_serial_port_name("./ttyUSB0") == "./ttyUSB0"


# ---- Windows: pass through unchanged -------------------------------


@pytest.mark.parametrize("win_name", [
    "COM4",
    "COM10",
    "\\\\.\\COM10",  # NT-namespace form for high-numbered ports
])
def test_windows_pass_through(win_name: str) -> None:
    """COM ports are already dialable; no /dev/ prefix on Windows."""
    with patch("platform.system", return_value="Windows"):
        assert resolve_serial_port_name(win_name) == win_name


# ---- pyserial URL schemes untouched -------------------------------


@pytest.mark.parametrize("url", [
    "loop://",
    "socket://pi.local:5000",
    "spy://COM3",
    "rfc2217://host:4000",
    "hwgrep://FTDI",
    "alt://class?something",
])
def test_pyserial_url_schemes_untouched(url: str) -> None:
    """A user who typed ``loop://`` wants in-memory loopback, not
    ``/dev/loop://``. Every documented pyserial URL scheme bypasses
    the prefix logic."""
    with patch("platform.system", return_value="Linux"):
        assert resolve_serial_port_name(url) == url


# ---- edge cases ---------------------------------------------------


def test_empty_string_passes_through() -> None:
    """An empty port string -- probably a config error -- surfaces
    as-is so pyserial can produce its own clearer error. Don't
    silently turn it into ``/dev/``."""
    with patch("platform.system", return_value="Linux"):
        assert resolve_serial_port_name("") == ""


def test_macos_treated_as_posix() -> None:
    """macOS (``platform.system() == "Darwin"``) uses the same
    ``/dev/<name>`` convention as Linux."""
    with patch("platform.system", return_value="Darwin"):
        assert resolve_serial_port_name("cu.usbmodem1234") == "/dev/cu.usbmodem1234"
