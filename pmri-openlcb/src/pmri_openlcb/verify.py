"""Verify Node ID -- S-9.7.4.1 sec.5.

Every OpenLCB node's alias is transient: it's picked at boot via
:mod:`pmri_openlcb.alias`, and if the node loses power or the CAN
bus resets, the same node comes back on the next boot with a
different alias. What stays the same is the 48-bit **Node ID**.

Verify Node ID is how the alias<->NID map gets rebuilt without waiting
for :func:`AME` probes to trickle in. Two flavours:

* **Verify NID Global** (MTI ``0x0490``) -- global broadcast. Empty
  data means "everybody answer with your NID"; 6-byte NID data
  means "the node with this NID, answer with your alias." A
  discovery UI uses the empty form on startup and the targeted
  form to locate a specific node by its published NID.
* **Verify NID Addressed** (MTI ``0x0488``) -- targets one alias
  via the standard addressed 2-byte prefix. Used when you have an
  alias and want to confirm the NID behind it.

Both provoke a **Verified NID** (MTI ``0x0170``) reply carrying our
6-byte NID as the frame data.

This module ships:

* :class:`VerifyNodeIdService` -- our side. Auto-answers verify
  probes that target us. Constructed with the Node ID we
  advertise (usually the one we claimed our alias for).
* :class:`NodeIdLookup` -- the client side. Maintains an alias<->NID
  map by watching inbound Verified NID replies. ``probe(alias)``
  awaits a specific reply with a timeout; ``verify_all`` fires a
  global sweep to (re-)populate the map from scratch.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pmri_openlcb.alias import NodeId
from pmri_openlcb.codec import (
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_frame_type,
    extract_mti,
    extract_source_alias,
)

if TYPE_CHECKING:
    from pmri_core.events import Signal

_log = logging.getLogger(__name__)

# ---- MTIs (S-9.7.4.1 Table 1) --------------------------------------------

MTI_VERIFY_NID_GLOBAL = 0x0490
MTI_VERIFY_NID_ADDRESSED = 0x0488
MTI_VERIFIED_NID = 0x0170

# Default timeout for a targeted probe. Real hardware answers in
# milliseconds; 2 seconds accommodates a WAN-tunneled hub.
VERIFY_TIMEOUT = 2.0


# ---- typing shim ---------------------------------------------------------


class _TcLike:
    """See :class:`~pmri_openlcb.snip.SnipTrafficLike` -- same shape."""


# ---- our side: auto-answer verify probes ---------------------------------


class VerifyNodeIdService:
    """Respond to Verify Node ID probes with our Verified NID.

    Global probes with empty data provoke a reply from every node;
    global probes carrying a 6-byte NID only draw a reply from the
    node whose NID matches. Addressed probes carry a 2-byte dest
    alias -- we answer when that alias is ours.
    """

    def __init__(self, tc: _TcLike, node_id: NodeId) -> None:
        self._tc = tc
        self._node_id = node_id
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) != FT_MTI:
            return
        mti = extract_mti(frame.header)
        if mti == MTI_VERIFY_NID_GLOBAL:
            if not frame.data:
                # Global sweep -- every node answers.
                await self._send_verified()
                return
            if (
                len(frame.data) >= 6
                and frame.data[:6] == self._node_id.bytes
            ):
                # Targeted by NID -- only the matching node answers.
                await self._send_verified()
            return
        if mti == MTI_VERIFY_NID_ADDRESSED:
            # Addressed frames use a 2-byte flag+dest-alias prefix
            # (same layout as SNIP + all other addressed MTIs).
            if len(frame.data) >= 2:
                dest = int.from_bytes(frame.data[:2], "big") & 0x0FFF
                if dest == self._tc.source_alias:
                    await self._send_verified()

    async def _send_verified(self) -> None:
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_VERIFIED_NID, self._tc.source_alias,
            ),
            data=self._node_id.bytes,
        ))


# ---- client side: NodeId lookup ------------------------------------------


class NodeIdLookup:
    """Track the alias<->NodeId map by watching Verified NID replies.

    ``verify_all()`` fires a global sweep so every reachable node
    identifies. ``verify_by_nid(nid)`` finds a specific node by its
    published NID. ``probe(alias, timeout)`` awaits a Verified reply
    from a targeted addressed probe -- useful when a discovery UI has
    seen a new alias appear (e.g. via P/C Identified) and wants to
    resolve it to a NID without doing a global sweep.
    """

    def __init__(self, tc: _TcLike) -> None:
        self._tc = tc
        self._by_alias: dict[int, NodeId] = {}
        # Per-alias probes (from :meth:`probe`) get their reply
        # routed by source alias; per-NID resolves (from
        # :meth:`resolve`) key their future by the target NID
        # instead, since the caller doesn't know the alias yet.
        self._pending: dict[int, asyncio.Future[NodeId]] = {}
        self._pending_by_nid: dict[NodeId, asyncio.Future[int]] = {}
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("verify lookup closed"))
        for fut in self._pending_by_nid.values():
            if not fut.done():
                fut.set_exception(RuntimeError("verify lookup closed"))
        self._pending.clear()
        self._pending_by_nid.clear()

    # ---- outbound probes ---------------------------------------------

    async def verify_all(self) -> None:
        """Broadcast a global Verify NID -- every node answers."""
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_VERIFY_NID_GLOBAL, self._tc.source_alias,
            ),
            data=b"",
        ))

    async def verify_by_nid(self, node_id: NodeId) -> None:
        """Ask the node with *node_id* to identify itself (reveal
        its current alias). Global broadcast, but only the matching
        node replies."""
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(
                FT_MTI, MTI_VERIFY_NID_GLOBAL, self._tc.source_alias,
            ),
            data=node_id.bytes,
        ))

    async def resolve(
        self, node_id: NodeId, *, timeout: float = VERIFY_TIMEOUT,
    ) -> int:
        """Find the alias currently claimed by *node_id*. Returns
        immediately if the map already knows about it; otherwise
        broadcasts a targeted Verify NID Global and awaits the reply.

        Raises :class:`asyncio.TimeoutError` if no node answers within
        *timeout* -- typically means that NodeID isn't present on the
        bus."""
        alias = self.by_nid(node_id)
        if alias is not None:
            return alias
        if node_id in self._pending_by_nid:
            # Piggy-back on the outstanding resolve so a caller that
            # requests the same NID twice doesn't fire duplicate
            # verify broadcasts.
            return await asyncio.wait_for(
                asyncio.shield(self._pending_by_nid[node_id]),
                timeout=timeout,
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[int] = loop.create_future()
        self._pending_by_nid[node_id] = fut
        try:
            await self.verify_by_nid(node_id)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_by_nid.pop(node_id, None)

    async def probe(
        self, dest_alias: int, *, timeout: float = VERIFY_TIMEOUT,
    ) -> NodeId:
        """Send an addressed Verify NID to *dest_alias* and await the
        Verified reply. Raises :class:`asyncio.TimeoutError` if no
        reply arrives within *timeout*.

        Nested probes to the same alias are rejected -- the reply
        dispatch keys by alias, so two waiting futures would race."""
        if dest_alias in self._pending:
            raise RuntimeError(
                f"verify probe to alias {dest_alias:#05x} already in flight",
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[NodeId] = loop.create_future()
        self._pending[dest_alias] = fut
        try:
            prefix = dest_alias.to_bytes(2, "big")
            await self._tc.send_frame(OlcbFrame(
                header=_pack_header(
                    FT_MTI, MTI_VERIFY_NID_ADDRESSED, self._tc.source_alias,
                ),
                data=prefix,
            ))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(dest_alias, None)

    # ---- queries -----------------------------------------------------

    def get(self, alias: int) -> NodeId | None:
        """Return the NodeId for *alias* if we've seen a Verified NID
        reply from it; ``None`` otherwise. Query does *not* trigger a
        probe -- call :meth:`probe` for that."""
        return self._by_alias.get(alias)

    def by_nid(self, node_id: NodeId) -> int | None:
        """Return the alias currently claimed by *node_id*, or
        ``None`` if we haven't seen a Verified reply from it."""
        for alias, nid in self._by_alias.items():
            if nid == node_id:
                return alias
        return None

    def known(self) -> dict[int, NodeId]:
        """Snapshot of the alias->NodeId map."""
        return dict(self._by_alias)

    def forget(self, alias: int) -> None:
        """Drop *alias* from the map. Call this on Alias Map Reset
        (AMR) so a node that vanishes doesn't leak stale entries.
        Cancels any in-flight probe to the same alias."""
        self._by_alias.pop(alias, None)
        fut = self._pending.pop(alias, None)
        if fut is not None and not fut.done():
            fut.set_exception(RuntimeError(
                f"alias {alias:#05x} released before verify reply",
            ))

    # ---- inbound reply handler ---------------------------------------

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if extract_frame_type(frame.header) != FT_MTI:
            return
        if extract_mti(frame.header) != MTI_VERIFIED_NID:
            return
        if len(frame.data) < 6:
            return
        source = extract_source_alias(frame.header)
        nid = NodeId(int.from_bytes(frame.data[:6], "big"))
        self._by_alias[source] = nid
        fut = self._pending.get(source)
        if fut is not None and not fut.done():
            fut.set_result(nid)
        fut_by_nid = self._pending_by_nid.get(nid)
        if fut_by_nid is not None and not fut_by_nid.done():
            fut_by_nid.set_result(source)
