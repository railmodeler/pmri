"""OpenLCB throttle bean + manager.

Bridges the core :class:`~jmri_core.beans.throttle.Throttle` API to
the OpenLCB Traction protocol (:mod:`jmri_openlcb.traction`). Each
throttle handle wraps one loco alias and forwards speed / direction /
function / e-stop calls to the shared :class:`TractionClient`.

Acquiring a throttle involves a NID->alias resolve:

1. Turn ``(address, long_address)`` into the well-known OpenLCB
   train NodeID per S-9.7.5.1 sec.3: prefix ``06.01.00.00`` + 16-bit
   address representation. Short addresses (0..127) go straight
   into the low byte; long addresses (0..16383) get bits 15..14
   set to ``11`` per NMRA DCC baseline packet convention.
2. Resolve that NodeID to whatever 12-bit alias the loco is
   currently using via :meth:`NodeIdLookup.resolve`. That fires a
   Verify NID Global with the target NID; the loco replies with a
   Verified NID which populates the lookup's alias<->NID map.
3. Send Assign Controller to the loco with our node's NodeID so
   any previously-attached controller sees a Controller Change
   Notify and can release cleanly.

:meth:`release` sends Release Controller back so the loco is free
for the next throttle.

Throttle -> mph mapping: the core Throttle speed is a normalized
``[-1.0, 1.0]``; OpenLCB Traction speed is signed float mph. We
multiply the magnitude by 100 so a full-throttle command becomes
"100 mph." That's arbitrary but consistent -- most decoders treat
the mph value as an input to their own CDI-configurable speed
table, so the absolute scale only matters for the display; the
loco's cutout mph rescales it internally.
"""
from __future__ import annotations

import logging

from jmri_core.beans.throttle import Throttle
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.state import ThrottleDirection

from jmri_openlcb.alias import NodeId
from jmri_openlcb.traction import TractionClient
from jmri_openlcb.verify import NodeIdLookup

_log = logging.getLogger(__name__)

# S-9.7.5.1 sec.3 train-node prefix. Every DCC-addressed loco lives at
# ``0x06.01.00.00.<16-bit address form>``. Reserved for OpenLCB-DCC
# interoperability by the OpenLCB Node ID registrar.
TRAIN_NODE_PREFIX = 0x06_01_00_00_00_00

# Throttle magnitude -> mph scale. See module docstring.
MPH_PER_UNIT = 100.0


def dcc_to_train_node_id(address: int, *, long_address: bool) -> NodeId:
    """Map a DCC address to the well-known OpenLCB train NodeID.

    Short addresses (0..127) -> ``0x06.01.00.00.00.NN``.
    Long addresses (0..16383) -> ``0x06.01.00.00.<0xC0|Nhi>.<Nlo>``
    matching NMRA baseline DCC packet convention (bits 15..14 = 11
    signal long-address format).
    """
    if long_address:
        if not 0 <= address <= 0x3FFF:
            raise ValueError(
                f"long DCC address must be 0..16383, got {address}",
            )
        low16 = 0xC000 | address
    else:
        if not 0 <= address <= 127:
            raise ValueError(
                f"short DCC address must be 0..127, got {address}",
            )
        low16 = address
    return NodeId(TRAIN_NODE_PREFIX | low16)


def train_node_id_to_dcc(node_id: NodeId) -> tuple[int, bool] | None:
    """Decode a well-known OpenLCB train NodeID back to
    ``(address, long_address)``.

    Returns ``None`` if *node_id* isn't in the train range -- e.g.
    an OpenLCB throttle attached a native-OpenLCB loco (no DCC
    address) to a consist. The CS then knows this member can't be
    driven through the DCC bridge.

    Format is the inverse of :func:`dcc_to_train_node_id`:
    top 4 bytes must equal the well-known train prefix
    (``0x06.01.00.00``); the low 16 bits carry the DCC address in
    baseline NMRA form.
    """
    if (node_id.value & ~0xFFFF) != TRAIN_NODE_PREFIX:
        return None
    low16 = node_id.value & 0xFFFF
    if (low16 & 0xC000) == 0xC000:
        # Long address: bits 13..0.
        return (low16 & 0x3FFF, True)
    if low16 <= 0x7F:
        return (low16, False)
    # Anything with the top 2 bits neither 00 nor 11 is malformed --
    # the reserved 0x40xx..0x7Fxx range or the illegal 0x8000..
    # 0xBFFF short-address overflow. Treat as unknown.
    return None


class OlcbThrottle(Throttle):
    """Throttle bean routed through the OpenLCB Traction protocol.

    Holds the loco's current 12-bit alias plus a reference to a
    shared :class:`TractionClient`. Speed/direction changes convert
    to a Set Speed/Direction request against the alias; function
    changes convert to Set Function; e-stop to Emergency Stop.

    Direction is encoded in the speed frame's sign bit per
    S-9.7.4.7 sec.5.1. Because of that, a direction flip requires
    re-sending the last speed with the opposite sign -- the loco has
    no separate "just flip direction" opcode.
    """

    __slots__ = ("_alias", "_client", "_our_node_id")

    def __init__(
        self,
        address: int,
        long_address: bool,
        alias: int,
        client: TractionClient,
        our_node_id: NodeId,
    ) -> None:
        super().__init__(address=address, long_address=long_address)
        self._alias = alias
        self._client = client
        self._our_node_id = our_node_id

    @property
    def alias(self) -> int:
        """The loco's current 12-bit OpenLCB alias."""
        return self._alias

    async def set_speed(self, speed: float) -> None:
        await super().set_speed(speed)
        if speed == -1.0:
            # E-stop is a distinct opcode -- decoder halts without
            # any ramp, which set_speed(0.0) does not guarantee.
            await self._client.emergency_stop(self._alias)
            return
        forward = self.direction == ThrottleDirection.FORWARD
        await self._client.set_speed(
            self._alias, abs(speed) * MPH_PER_UNIT, forward=forward,
        )

    async def set_direction(self, direction: ThrottleDirection) -> None:
        prev_speed = self.speed
        await super().set_direction(direction)
        # OpenLCB carries direction in the sign bit of the speed
        # frame, so a direction change without re-sending speed
        # would leave the loco pointing the wrong way. E-stop
        # doesn't re-arm on direction change -- the operator has to
        # explicitly command speed again.
        if prev_speed == -1.0:
            return
        forward = direction == ThrottleDirection.FORWARD
        await self._client.set_speed(
            self._alias, abs(prev_speed) * MPH_PER_UNIT, forward=forward,
        )

    async def set_function(self, index: int, on: bool) -> None:
        await super().set_function(index, on)
        await self._client.set_function(
            self._alias, function=index, value=1 if on else 0,
        )

    async def emergency_stop(self) -> None:
        await super().emergency_stop()
        await self._client.emergency_stop(self._alias)


class OlcbThrottleManager(ThrottleManager):
    """Route throttle acquisition through the OpenLCB Traction
    protocol.

    On acquire:

    1. Convert DCC address -> train NodeID.
    2. Resolve NodeID -> alias via :meth:`NodeIdLookup.resolve`.
    3. Send Assign Controller so the loco recognises us.
    4. Return an :class:`OlcbThrottle` wrapping the alias.

    On release: send Release Controller and drop the handle.

    ``resolve_timeout`` bounds how long we wait for the loco to
    answer the Verify probe -- a value that isn't on the bus (still
    powered off, on a different segment) will surface as
    :class:`asyncio.TimeoutError` from ``acquire``.
    """

    def __init__(
        self,
        system_prefix: str,
        *,
        client: TractionClient,
        lookup: NodeIdLookup,
        our_node_id: NodeId,
        resolve_timeout: float = 2.0,
    ) -> None:
        super().__init__(system_prefix)
        self._client = client
        self._lookup = lookup
        self._our_node_id = our_node_id
        self._resolve_timeout = resolve_timeout

    async def _construct(
        self, address: int, *, long_address: bool,
    ) -> OlcbThrottle:
        nid = dcc_to_train_node_id(address, long_address=long_address)
        alias = await self._lookup.resolve(nid, timeout=self._resolve_timeout)
        await self._client.assign_controller(alias, self._our_node_id)
        return OlcbThrottle(
            address=address, long_address=long_address,
            alias=alias, client=self._client,
            our_node_id=self._our_node_id,
        )

    async def release(self, throttle: Throttle) -> None:
        if isinstance(throttle, OlcbThrottle):
            await self._client.release_controller(
                throttle.alias, self._our_node_id,
            )
        await super().release(throttle)
