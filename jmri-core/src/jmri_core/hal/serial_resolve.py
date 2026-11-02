"""``resolve_serial_port_name`` -- bare device names to full paths.

JMRI stores serial ports by their short display name
(``ttyUSB0`` on Linux, ``cu.usbserial-X`` on macOS, ``COM4`` on
Windows). pyserial / pyserial-asyncio's ``open()`` expects a full
path on POSIX systems (``/dev/ttyUSB0``). Each driver's SerialPort
wrapper calls through this helper so bare JMRI-style names resolve
transparently.

Semantics:

* Windows: pass through unchanged -- ``COM4`` IS the full path pyserial
  wants, and ``\\\\.\\COM10`` (NT-namespace form for high-numbered
  ports) is already absolute.
* POSIX (Linux, macOS, BSD): if the string looks like a bare device
  name (no slash, no scheme, no path separator), prepend ``/dev/``.
  Already-absolute paths (``/dev/ttyUSB0``) pass through.
* pyserial URL schemes (``loop://``, ``socket://host:port``,
  ``spy://``, ``alt://``, ``hwgrep://``, ``rfc2217://``) pass through
  unchanged -- those aren't device names.
"""
from __future__ import annotations

import platform

# pyserial URL schemes we must not munge. See
# https://pyserial.readthedocs.io/en/latest/url_handlers.html -- a user
# who typed ``loop://`` in the config file wants it treated as the
# in-memory loopback, not ``/dev/loop://``.
_URL_SCHEMES = (
    "loop://", "socket://", "spy://", "alt://", "hwgrep://", "rfc2217://",
)


def resolve_serial_port_name(port: str) -> str:
    """Return a path pyserial can open.

    *port* is whatever appeared in the config (``profile.xml``, the
    GUI, env vars). The helper is a no-op on Windows and for
    already-resolved POSIX paths; it only touches the bare-name case
    JMRI's XML uses on Linux/macOS.
    """
    if not port:
        return port
    # pyserial URL schemes aren't device names -- leave alone.
    low = port.lower()
    if any(low.startswith(s) for s in _URL_SCHEMES):
        return port
    if platform.system() == "Windows":
        # COM4, \\.\COM10 -- both already openable; nothing to prepend.
        return port
    # POSIX. If the name already has a path separator (absolute path
    # ``/dev/ttyUSB0`` or a relative ``./ttyUSB0``), leave it alone.
    if "/" in port:
        return port
    return f"/dev/{port}"
