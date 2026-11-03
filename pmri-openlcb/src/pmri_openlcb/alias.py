"""OpenLCB alias claim + AME response (S-9.7.0.1 sec.6).

An OpenLCB CAN-bus node has a fixed 48-bit **Node ID** but on the wire
is addressed by a 12-bit **alias**. Aliases are self-assigned via a
handshake so any node can join a running bus without central
coordination:

1. Pick a candidate 12-bit alias -- deterministically from the Node ID
   plus a retry counter so a collision retry reliably lands on a
   different number.
2. Send CID 1..4 in order, each carrying a 12-bit slice of the Node
   ID in its variable field. Any listening node whose alias == our
   candidate will reply with a **RID** (Reserve ID) frame -- that's
   how we detect collisions.
3. Wait ``CLAIM_WAIT`` (200 ms per S-9.7.0.1) after the last CID.
   No collision reply -> alias is ours.
4. Send **RID** to declare the reservation, then **AMD** (Alias Map
   Definition, data = 6-byte Node ID) so the rest of the bus can
   resolve alias<->NID lookups without probing.

If step 3 sees a collision, bump the retry counter and start over
from step 1. Bail after ``MAX_ATTEMPTS`` -- that many collisions in
a row means the bus is misconfigured (too many nodes sharing the
same PRNG seed, or a jammed alias space).

Post-claim, we also need to answer **AME** (Alias Map Enquiry) probes
from other nodes so they can learn our NID without brute-forcing the
alias space. The AME data field is empty (global probe -- every node
answers) or 6 bytes (targets a specific NID; only that node answers).

Cross-referenced against openlcb-python's ``AliasEnumeration`` and
the S-9.7.0.1 rev 1.2 standard.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pmri_openlcb.codec import (
    CTRL_AME,
    CTRL_AMD,
    CTRL_AMR,
    CTRL_RID,
    OlcbFrame,
    extract_source_alias,
    is_control,
    make_amd_header,
    make_ame_header,
    make_cid_header,
    make_rid_header,
)

_log = logging.getLogger(__name__)

# Wire timing (S-9.7.0.1 sec.6.2.2). The spec says at least 200 ms
# between the last CID and RID for a candidate alias. On loopback
# transports collisions arrive instantly, so a short wait is safe;
# we keep the spec value for real serial buses.
CLAIM_WAIT = 0.2

# How many collisions to tolerate before giving up. Six covers the
# vanishingly rare case where a real bus already has many nodes
# competing for aliases; anything higher probably signals a
# configuration bug worth failing loudly.
MAX_ATTEMPTS = 6

# Node ID range 0x02_XX_XX_XX_XX_XX is set aside in S-9.7.0.10 for
# developers / private use. When the operator hasn't supplied an
# explicit Node ID we mint one in that range so we can't collide
# with a registered manufacturer's IDs.
DEV_RANGE_TOP_BYTE = 0x02


@dataclass(frozen=True, slots=True)
class NodeId:
    """A 48-bit OpenLCB Node ID.

    Rendered on the wire as six MSB-first bytes; rendered to humans
    as dotted hex like ``02.01.12.FE.DC.BA``.
    """

    value: int

    def __post_init__(self) -> None:
        if not 0 <= self.value < (1 << 48):
            raise ValueError(f"node id must be 48-bit, got {self.value:#x}")

    @classmethod
    def parse(cls, text: str) -> NodeId:
        """Accept dotted hex (``02.01.12.FE.DC.BA``) or a bare integer
        literal (``0x02011200FEDCBA``)."""
        text = text.strip()
        if "." in text:
            parts = text.split(".")
            if len(parts) != 6:
                raise ValueError(
                    f"dotted Node ID needs 6 parts, got {len(parts)} in {text!r}",
                )
            n = 0
            for p in parts:
                n = (n << 8) | int(p, 16)
            return cls(n)
        return cls(int(text, 0))

    @property
    def bytes(self) -> bytes:
        return self.value.to_bytes(6, "big")

    def dotted(self) -> str:
        return ".".join(f"{b:02X}" for b in self.bytes)

    def slice_12(self, seq: int) -> int:
        """Return the 12-bit CID slice for *seq*  in  1..4.

        CID 1 = bits 47..36, CID 2 = 35..24, CID 3 = 23..12, CID 4 = 11..0.
        """
        shifts = {1: 36, 2: 24, 3: 12, 4: 0}
        if seq not in shifts:
            raise ValueError(f"CID seq must be 1..4, got {seq}")
        return (self.value >> shifts[seq]) & 0xFFF

    def __str__(self) -> str:
        return self.dotted()


def candidate_alias(node_id: NodeId, attempt: int) -> int:
    """Deterministic 12-bit alias from a Node ID + retry counter.

    The mapping just needs to (a) always produce a 12-bit number and
    (b) yield a different alias on each retry so a colliding node
    doesn't produce the same value on retry #1 and #2. XOR-folding
    the six NID bytes with the attempt counter satisfies both; two
    nodes with different NIDs almost never collide beyond one
    retry, and even nodes with pathologically similar NIDs advance
    off the collision within ``MAX_ATTEMPTS``. The 0 alias is
    reserved (broadcast-ish), so we bump it to 1 when we roll it.
    """
    b = node_id.bytes
    seed = (
        (b[0] << 4) ^ (b[1] << 8) ^ (b[2] << 4)
        ^ (b[3] << 8) ^ (b[4] << 4) ^ (b[5] << 8)
        ^ (attempt * 0x1D3)
    )
    alias = seed & 0xFFF
    if alias == 0:
        alias = 1
    return alias


class AliasCollision(Exception):
    """Signaled by :func:`claim_alias` when the bus keeps colliding."""


async def claim_alias(
    tc: OlcbTrafficLike,
    node_id: NodeId,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    wait: float = CLAIM_WAIT,
) -> int:
    """Run the CID/RID handshake and return the reserved alias.

    *tc* must expose ``frames`` (a signal that emits every incoming
    :class:`~pmri_openlcb.codec.OlcbFrame`) and ``send_frame``
    (writes a raw frame to the bus).

    On collision, retries with a bumped ``attempt`` counter so
    :func:`candidate_alias` produces a fresh candidate. Raises
    :class:`AliasCollision` after ``max_attempts`` failures.
    """
    for attempt in range(max_attempts):
        candidate = candidate_alias(node_id, attempt)
        collision = asyncio.Event()

        def _on_frame(frame: OlcbFrame) -> None:
            # A collision is any frame carrying our candidate alias
            # from a *different* node (which by definition is
            # everybody in the alias-claim window -- we haven't sent
            # RID yet, so no one else could think this alias is
            # ours). RID + AMD (and even the rare in-flight CID
            # from an existing owner) all reveal the conflict.
            if extract_source_alias(frame.header) == candidate:
                collision.set()

        unsubscribe = tc.frames.connect(_on_frame)
        try:
            for seq in (1, 2, 3, 4):
                await tc.send_frame(OlcbFrame(
                    header=make_cid_header(
                        seq, node_id.slice_12(seq), candidate,
                    ),
                    data=b"",
                ))
            try:
                await asyncio.wait_for(collision.wait(), timeout=wait)
                _log.info(
                    "OpenLCB alias %#05x collided on attempt %d; retrying",
                    candidate, attempt,
                )
                continue
            except asyncio.TimeoutError:
                pass
            # Claim + announce.
            await tc.send_frame(OlcbFrame(
                header=make_rid_header(candidate), data=b"",
            ))
            await tc.send_frame(OlcbFrame(
                header=make_amd_header(candidate), data=node_id.bytes,
            ))
            _log.info(
                "OpenLCB alias %#05x claimed for node %s (attempt %d)",
                candidate, node_id.dotted(), attempt,
            )
            return candidate
        finally:
            unsubscribe()
    raise AliasCollision(
        f"could not claim alias for node {node_id} after "
        f"{max_attempts} attempts",
    )


class AmeResponder:
    """Answer AME (Alias Map Enquiry) probes with our AMD.

    Global AME frames (empty data) get an answer from every node on
    the bus; targeted AME frames (6-byte data = target NID) only
    provoke an answer from the matching node. Instantiated by
    :class:`~pmri_openlcb.connection.OlcbConnection` after the alias
    is claimed."""

    def __init__(
        self, tc: OlcbTrafficLike, node_id: NodeId, alias: int,
    ) -> None:
        self._tc = tc
        self._node_id = node_id
        self._alias = alias
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if not is_control(frame.header, CTRL_AME):
            return
        if frame.data and frame.data != self._node_id.bytes:
            # Targeted probe for someone else; ignore.
            return
        # Global AME (empty data) or targeted at us -> publish AMD.
        await self._tc.send_frame(OlcbFrame(
            header=make_amd_header(self._alias),
            data=self._node_id.bytes,
        ))


class AliasReleaseWatcher:
    """Watch peer AMR frames and fan out to registered callbacks.

    OpenLCB nodes send Alias Map Reset when they cleanly release
    their alias (typically at shutdown). Anything we cache keyed by
    peer alias -- NID map, event roster, SNIP/PIP snapshots -- should
    drop the entry when we see one, or a UI listing "known nodes"
    will show ghosts of nodes that have long since powered off.

    A power cut on the peer skips AMR, so this isn't a complete
    solution to stale entries -- a timeout-based sweep would catch
    the rest -- but it makes the clean case (bench cycling a decoder)
    behave correctly.
    """

    def __init__(self, tc: OlcbTrafficLike) -> None:
        self._tc = tc
        self._callbacks: list[Callable[[int], None]] = []
        self._unsubscribe = tc.frames.connect(self._on_frame)

    def close(self) -> None:
        self._unsubscribe()
        self._callbacks.clear()

    def on_release(
        self, callback: Callable[[int], None],
    ) -> Callable[[], None]:
        """Register *callback* to fire with the released alias
        whenever an inbound AMR is seen. Returns an unsubscribe
        function."""
        self._callbacks.append(callback)

        def _unsub() -> None:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

        return _unsub

    def notify_release(self, alias: int) -> None:
        """Fire the release fan-out for *alias* without an actual AMR
        frame. Used by the stale-peer sweeper so a peer that goes
        silent looks identical to a peer that cleanly shut down."""
        for cb in list(self._callbacks):
            try:
                cb(alias)
            except Exception:
                _log.exception(
                    "release callback failed for alias %#05x", alias,
                )

    async def _on_frame(self, frame: OlcbFrame) -> None:
        if not is_control(frame.header, CTRL_AMR):
            return
        self.notify_release(extract_source_alias(frame.header))


# ---- typing shim ----------------------------------------------------------


class OlcbTrafficLike:
    """Structural type for the bits of the traffic controller alias
    code needs. Kept as a docstring rather than a Protocol so the
    module has zero runtime dependencies on :mod:`pmri_openlcb.traffic`
    (which itself depends on this module indirectly).

    Required attributes:

    * ``frames`` -- :class:`~pmri_core.events.Signal` of
      :class:`OlcbFrame`. Every incoming frame is emitted.
    * ``send_frame(frame)`` -- coroutine that writes an
      :class:`OlcbFrame` to the wire.
    """
