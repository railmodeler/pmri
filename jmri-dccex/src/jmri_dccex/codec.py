"""DCC-EX text protocol codec.

DCC-EX (formerly DCC++) is a bracket-delimited ASCII protocol:

    <command arg1 arg2 ...>

Commands and responses both use the same bracket convention. Multiple
commands can be sent back-to-back; the codec reads one complete bracketed
message at a time and strips the brackets.

Common commands JMRI uses:

* ``<1>`` / ``<0>`` -- power on / off
* ``<T id 1|0>`` -- set turnout *id* to closed (1) or thrown (0)
* ``<S id pin pullup>`` -- define sensor (server-side state only)
* ``<t cab loco speed dir>`` -- throttle command (legacy form)
* ``<R cv>`` -- read CV; ``<W cv val>`` -- write CV
* ``<Q id>`` -- sensor active report (server -> client)
* ``<q id>`` -- sensor inactive report

Responses follow the same shape; unknown messages decode to
:class:`DccexUnknown` so listeners don't lose visibility.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DccexMessage:
    """A parsed message -- command character + space-separated args."""

    command: str          # first token (e.g. "1", "T", "Q")
    args: tuple[str, ...] # remaining tokens

    @property
    def raw(self) -> str:
        if not self.args:
            return self.command
        return f"{self.command} {' '.join(self.args)}"


@dataclass(frozen=True, slots=True)
class DccexUnknown(DccexMessage):
    """Marker subclass: a well-formed bracket message we don't recognize."""


def encode(msg: DccexMessage) -> bytes:
    """Wrap a message in brackets and return as UTF-8 bytes."""
    return f"<{msg.raw}>".encode()


def decode(text: str) -> DccexMessage:
    """Parse the content *between* the brackets into a :class:`DccexMessage`."""
    text = text.strip()
    if not text:
        raise ValueError("empty DCC-EX message body")
    tokens = text.split()
    return DccexMessage(command=tokens[0], args=tuple(tokens[1:]))


async def read_message(
    read_byte: Callable[[], Awaitable[int]],
) -> str:
    """Read one bracketed message from the byte stream.

    Bytes outside a ``<...>`` pair are silently discarded (matches DCC-EX
    convention: newlines and other status text between messages are noise).
    Returns the *unwrapped* contents (no brackets).
    """
    # Wait for opening bracket.
    while True:
        b = await read_byte()
        if b == ord('<'):
            break

    out = bytearray()
    while True:
        b = await read_byte()
        if b == ord('>'):
            return out.decode("utf-8", errors="replace")
        out.append(b)
