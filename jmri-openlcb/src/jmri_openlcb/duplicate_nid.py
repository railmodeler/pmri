"""Detect and react to another node claiming our own 48-bit NodeID.

The CAN alias claim (:mod:`jmri_openlcb.alias`) handles the case
where two nodes want the same 12-bit alias -- one loses the CID/RID
race and picks a different alias. That's a different problem from
this one: two nodes with the *same* 48-bit NodeID. That happens
when a developer copies a config across bench decoders, or when
two units ship with the same reserved-range NID by mistake.

Detection: any inbound frame that reports a peer alias mapped to
our own NodeID is a duplicate. Two frame types carry (alias, NID)
today -- AMD (Alias Map Definition, control frame 0x701) and
Verified NID (MTI 0x0170) -- and the watcher listens on both.

On detection: (1) emit a PCER for the well-known Duplicate Node
ID Detected event (:data:`~jmri_openlcb.events.EVENT_DUPLICATE_NODE_ID`),
so any observer on the bus sees the collision; (2) fire the
registered callbacks with the offending peer alias, so the caller
can shut down, re-claim with a fresh NID, or surface an alert.

The watcher does *not* re-claim automatically -- that's a policy
decision (a bench user might want to log and keep running for the
duration of a debug session). Callers who want the automatic path
subscribe a shutdown-then-reopen callback.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from jmri_openlcb.codec import (
    CTRL_AMD,
    FT_MTI,
    OlcbFrame,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
    is_control,
    make_pcer_header,
)
from jmri_openlcb.events import EVENT_DUPLICATE_NODE_ID

if TYPE_CHECKING:
    from jmri_openlcb.alias import NodeId, OlcbTrafficLike

_log = logging.getLogger(__name__)


# Verified NID reply -- cross-imported from verify.py would create a
# cycle; the value is a standard MTI so we redeclare it here with a
# comment pointing to the canonical source.
MTI_VERIFIED_NID = 0x0170   # see jmri_openlcb.verify.MTI_VERIFIED_NID


class DuplicateNodeIdWatcher:
    """Fire callbacks when a peer alias reports our own NodeID.

    ``our_node_id`` -- the 48-bit NodeID we claim. Every inbound
    AMD or Verified NID whose payload matches this value AND whose
    source alias isn't our own triggers the collision handling.
    """

    def __init__(
        self,
        tc: OlcbTrafficLike,
        our_node_id: NodeId,
    ) -> None:
        self._tc = tc
        self._our_nid_bytes = our_node_id.bytes
        self._callbacks: list[Callable[[int], None]] = []
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        self._callbacks.clear()

    def on_duplicate(
        self, callback: Callable[[int], None],
    ) -> Callable[[], None]:
        """Register *callback* to fire with the offending peer's
        alias when a duplicate NID is detected. Returns an
        unsubscribe function."""
        self._callbacks.append(callback)

        def _unsub() -> None:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

        return _unsub

    async def _on_frame(self, frame: OlcbFrame) -> None:
        source = extract_source_alias(frame.header)
        if source == self._tc.source_alias:
            # Our own outbound AMD (looped through :attr:`sent_frames`)
            # would match too; skip it explicitly rather than relying
            # on the traffic controller's separation.
            return
        # AMD control frame carries the peer's NodeID as the 6-byte
        # data payload.
        if is_control(frame.header, CTRL_AMD):
            if len(frame.data) >= 6 and frame.data[:6] == self._our_nid_bytes:
                await self._on_duplicate(source, "AMD")
            return
        # Verified NID reply is an MTI frame with the same 6-byte
        # NID payload.
        if extract_frame_type(frame.header) != FT_MTI:
            return
        if extract_mti(frame.header) != MTI_VERIFIED_NID:
            return
        if len(frame.data) >= 6 and frame.data[:6] == self._our_nid_bytes:
            await self._on_duplicate(source, "Verified NID")

    async def _on_duplicate(self, peer_alias: int, via: str) -> None:
        _log.warning(
            "duplicate NodeID detected: peer alias %#05x claims our "
            "own NodeID (seen via %s)", peer_alias, via,
        )
        # Broadcast the well-known event so any observer (a monitor,
        # a peer's roster) sees the collision. Failures here don't
        # block callback delivery -- the wire announce is a courtesy,
        # the callbacks are the actionable path.
        try:
            await self._tc.send_frame(OlcbFrame(
                header=make_pcer_header(self._tc.source_alias),
                data=EVENT_DUPLICATE_NODE_ID.bytes,
            ))
        except Exception:
            _log.exception("failed to broadcast Duplicate NID PCER")
        for cb in list(self._callbacks):
            try:
                cb(peer_alias)
            except Exception:
                _log.exception(
                    "on_duplicate callback failed for alias %#05x",
                    peer_alias,
                )
