"""Simple Node Ident Info (SNIP) -- S-9.7.4.5.

Every OpenLCB node exposes a short human-readable identity block:
manufacturer, model, hardware/software versions, and two operator-
editable strings (user name and description). SNIP is how a
discovery tool learns "what am I looking at" without probing the
CDI or SNIP config space.

Wire protocol:

* **Request** (MTI ``0x0DE8``): addressed message with empty payload.
  Only the ``[flag | dest_alias]`` 2-byte prefix goes on the wire.
* **Reply** (MTI ``0x0A08``): addressed message whose payload is::

      byte     0       : mfg version = 0x04
      bytes    1..*    : 4 null-terminated UTF-8 strings --
                           manufacturer, model, hardware ver, software ver
      byte     next    : user version = 0x02
      bytes    +1..    : 2 null-terminated UTF-8 strings --
                           user name, user description

Both directions can span multiple CAN frames; assembly is handled by
:class:`~pmri_openlcb.addressed.AddressedReassembler`.

Cross-checked against S-9.7.4.5 rev 1.1 and openlcb-python's
``SimpleNodeIdentInfo`` module.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pmri_openlcb.addressed import (
    AddressedMessage,
    AddressedReassembler,
    encode_addressed_frames,
)
from pmri_openlcb.codec import OlcbFrame

if TYPE_CHECKING:
    from pmri_core.events import Signal

_log = logging.getLogger(__name__)

# MTIs (S-9.7.4.5 Table 1). Both addressed (bit 3 set).
MTI_SNIP_REQUEST = 0x0DE8
MTI_SNIP_REPLY = 0x0A08

# Version prefix bytes.
SNIP_MFG_VERSION = 0x04    # 4 manufacturer strings follow
SNIP_USER_VERSION = 0x02   # 2 user strings follow

# Reply timeout for a probe. Real hardware answers in milliseconds
# for a local bus; 3 seconds accommodates a WAN-tunneled bridge.
SNIP_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """The 6 SNIP strings for one node."""

    manufacturer: str = ""
    model: str = ""
    hardware_version: str = ""
    software_version: str = ""
    user_name: str = ""
    user_description: str = ""

    def encode(self) -> bytes:
        """Emit the SNIP reply body: version + null-terminated
        strings + version + null-terminated strings.

        Strings are UTF-8; an empty string encodes as a single ``\\x00``
        terminator, matching how real hardware transmits an unset
        user name."""
        return b"".join([
            bytes([SNIP_MFG_VERSION]),
            self.manufacturer.encode("utf-8") + b"\x00",
            self.model.encode("utf-8") + b"\x00",
            self.hardware_version.encode("utf-8") + b"\x00",
            self.software_version.encode("utf-8") + b"\x00",
            bytes([SNIP_USER_VERSION]),
            self.user_name.encode("utf-8") + b"\x00",
            self.user_description.encode("utf-8") + b"\x00",
        ])

    @classmethod
    def decode(cls, data: bytes) -> NodeInfo:
        """Reverse of :meth:`encode`. Tolerant of the two known
        real-world quirks:

        * A short reply that omits the user block (some older
          hardware) is treated as empty user name + description.
        * Trailing garbage past the two required blocks is ignored.

        Malformed replies (missing version byte, missing null
        terminator) raise :class:`ValueError` -- the caller decides
        whether to log and continue or surface it as a probe error.
        """
        if not data:
            raise ValueError("SNIP reply is empty")
        if data[0] != SNIP_MFG_VERSION:
            raise ValueError(
                f"SNIP reply missing manufacturer version byte "
                f"(got {data[0]:#04x})",
            )
        pos = 1
        mfg_strings, pos = _read_null_strings(data, pos, 4)
        # User block is technically required, but some historic
        # firmware omits it; downgrade to empty strings rather than
        # rejecting the whole probe.
        user_strings: list[str] = ["", ""]
        if pos < len(data) and data[pos] == SNIP_USER_VERSION:
            pos += 1
            user_strings, pos = _read_null_strings(data, pos, 2)
        return cls(
            manufacturer=mfg_strings[0],
            model=mfg_strings[1],
            hardware_version=mfg_strings[2],
            software_version=mfg_strings[3],
            user_name=user_strings[0],
            user_description=user_strings[1],
        )


def _read_null_strings(
    data: bytes, start: int, count: int,
) -> tuple[list[str], int]:
    """Consume *count* consecutive null-terminated UTF-8 strings from
    *data*, starting at *start*. Return ``(strings, new_position)``.

    Missing terminators for the last string are tolerated (some
    hardware truncates the trailing null) -- the remaining bytes are
    used verbatim as the final string.
    """
    out: list[str] = []
    pos = start
    for i in range(count):
        end = data.find(b"\x00", pos)
        if end < 0:
            if i == count - 1:
                # Last string missing terminator -- accept whatever's
                # left and stop.
                out.append(data[pos:].decode("utf-8", errors="replace"))
                pos = len(data)
                break
            raise ValueError(
                f"SNIP reply missing null terminator for string {i}",
            )
        out.append(data[pos:end].decode("utf-8", errors="replace"))
        pos = end + 1
    # If earlier strings terminated but the loop ended before count
    # was reached (e.g. count > available), pad the tail.
    while len(out) < count:
        out.append("")
    return out, pos


# ---- protocol client / responder -----------------------------------------


class SnipClient:
    """Probe SNIP from a peer.

    Instantiate once per connection; the client subscribes to
    inbound frames and reassembles multi-frame replies. ``probe``
    sends a SNIP request and awaits the matching reply from the
    target alias.

    A single outstanding probe per target is the assumption -- nested
    probes to the same alias raise. Probes to different aliases can
    run concurrently.
    """

    def __init__(self, tc: SnipTrafficLike) -> None:
        self._tc = tc
        self._reassembler = AddressedReassembler()
        self._pending: dict[int, asyncio.Future[NodeInfo]] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("SNIP client closed"))
        self._pending.clear()

    async def probe(
        self, dest_alias: int, *, timeout: float = SNIP_TIMEOUT,
    ) -> NodeInfo:
        """Ask node *dest_alias* for its SNIP; await the reply."""
        if dest_alias in self._pending:
            raise RuntimeError(
                f"SNIP probe to alias {dest_alias:#05x} already in flight",
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[NodeInfo] = loop.create_future()
        self._pending[dest_alias] = fut
        try:
            for frame in encode_addressed_frames(
                MTI_SNIP_REQUEST, self._tc.source_alias, dest_alias, b"",
            ):
                await self._tc.send_frame(frame)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(dest_alias, None)

    async def _on_frame(self, frame: OlcbFrame) -> None:
        msg = self._reassembler.receive(frame)
        if msg is None or msg.mti != MTI_SNIP_REPLY:
            return
        # Reply's source_alias is the peer we probed; that's the
        # key we stored the future under.
        fut = self._pending.get(msg.source_alias)
        if fut is None or fut.done():
            return
        try:
            info = NodeInfo.decode(msg.body)
        except ValueError as e:
            fut.set_exception(e)
            return
        fut.set_result(info)


class SnipResponder:
    """Answer SNIP requests targeting our node.

    Subscribes to inbound frames, reassembles the (empty-payload)
    request, and sends a SNIP reply built from a caller-provided
    :class:`NodeInfo`. If the caller wants dynamic content (a user
    name that reflects recent config edits), pass a ``get_info``
    callable and we'll invoke it fresh on every probe.
    """

    def __init__(
        self,
        tc: SnipTrafficLike,
        get_info: Callable[[], NodeInfo] | NodeInfo,
    ) -> None:
        self._tc = tc
        if callable(get_info):
            self._get_info = get_info
        else:
            info = get_info
            self._get_info = lambda: info
        self._reassembler = AddressedReassembler()
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    async def _on_frame(self, frame: OlcbFrame) -> None:
        msg = self._reassembler.receive(frame)
        if msg is None or msg.mti != MTI_SNIP_REQUEST:
            return
        # Requests are addressed; check the target is us.
        if msg.dest_alias != self._tc.source_alias:
            return
        info = self._get_info()
        body = info.encode()
        for reply in encode_addressed_frames(
            MTI_SNIP_REPLY, self._tc.source_alias, msg.source_alias, body,
        ):
            await self._tc.send_frame(reply)


# ---- typing shim ---------------------------------------------------------


class SnipTrafficLike:
    """Structural type for the bits of the traffic controller the
    SNIP layer needs. Written as a docstring rather than a Protocol
    so this module has no runtime dependency on
    :mod:`pmri_openlcb.traffic`.

    Required attributes:

    * ``source_alias`` (int) -- our claimed 12-bit alias.
    * ``frames`` -- :class:`~pmri_core.events.Signal` of
      :class:`OlcbFrame`. Every incoming frame is emitted.
    * ``send_frame(frame)`` -- coroutine that writes a frame.
    """
