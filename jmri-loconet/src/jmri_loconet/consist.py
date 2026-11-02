"""``LnConsistManager`` -- hardware consist manager for LocoNet.

LocoNet consists are **slot-linked**: the command station tracks which
slots belong to a group and, on speed and direction commands to the
group's *lead* slot, broadcasts the same command to every follower slot
automatically. Function commands stay per-loco because per-loco lighting
is what the user wants.

Wire ops:

* ``OPC_LINK_SLOTS(src, dest)`` -- attach ``src`` as a follower of
  ``dest``. Repeat for each additional follower.
* ``OPC_UNLINK_SLOTS(src, dest)`` -- detach ``src`` from ``dest``.

We build on the software :class:`~jmri_core.beans.consist_manager.ConsistManager`
so a script that only uses the public API works whether the connection
is LocoNet, NCE, DCC-EX, or the internal loopback. Overrides:

* ``_after_membership_change`` -- reconciles the on-wire link set with
  the consist's current membership, sending ``LINK`` / ``UNLINK`` for
  the diff.
* ``_before_delete`` -- unlinks every follower so the consist tears
  down cleanly.
* ``set_speed`` / ``set_direction`` / ``emergency_stop`` -- send only
  to the *lead* throttle. The CS handles the fan-out via LINK, so
  sending to followers would be redundant (harmless, but wastes
  bandwidth on a slow serial bus).
* ``set_function`` -- falls through to the base's per-loco fan-out
  because LocoNet doesn't fan functions through LINK.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jmri_core.beans.consist import Consist
from jmri_core.beans.consist_manager import ConsistManager
from jmri_core.state import ThrottleDirection

from jmri_loconet.messages import OpcLinkSlots, OpcUnlinkSlots
from jmri_loconet.throttle import LnThrottle, LnThrottleManager


def _invert(direction: ThrottleDirection) -> ThrottleDirection:
    """Flip a throttle direction. Used to send physically-reversed
    followers the opposite of the lead's requested direction."""
    return (
        ThrottleDirection.REVERSE if direction is ThrottleDirection.FORWARD
        else ThrottleDirection.FORWARD
    )

if TYPE_CHECKING:
    from jmri_loconet.slots import SlotManager
    from jmri_loconet.traffic import LocoNetTrafficController

_log = logging.getLogger(__name__)


class LnConsistManager(ConsistManager):
    """LocoNet consist manager backed by ``OPC_LINK_SLOTS`` /
    ``OPC_UNLINK_SLOTS``.

    Consists are ``command_station`` by default here -- the CS is doing
    the fan-out, not us. If a caller explicitly creates a consist with
    ``kind="software"`` we still store it and drive it via base
    fan-out, so the two modes coexist.
    """

    default_kind = "command_station"

    def __init__(
        self,
        throttles: LnThrottleManager,
        system_prefix: str,
        *,
        tc: LocoNetTrafficController,
        slots: SlotManager,
    ) -> None:
        super().__init__(throttles, system_prefix)
        self._tc = tc
        self._slots = slots
        # Per-consist set of currently-linked (lead_addr, follower_addr)
        # pairs. Kept alongside the consist's own locos list so we can
        # diff on membership change and only issue LINK/UNLINK for the
        # deltas -- repeatedly sending LINK for an already-linked pair
        # confuses some CS firmware versions.
        self._linked: dict[int, set[tuple[int, int]]] = {}

    # ---- wire-level plumbing --------------------------------------------

    async def _link_pair(
        self, lead_addr: int, follower_addr: int,
        *, direction_normal: bool = True,
    ) -> None:
        """Acquire both throttles' slots and send ``OPC_LINK_SLOTS``.

        Acquiring the throttle triggers the slot lookup + claim, so we
        can rely on ``LnThrottle.slot`` being populated afterward.

        When ``direction_normal=False`` (member physically reversed),
        immediately flip the follower's direction opposite to the
        lead's current direction so the LINK's automatic speed fan-out
        moves the loco correctly. Matches JMRI Java's
        ``LocoNetConsist.setDirection`` behaviour called from
        ``notifyChangedSlot`` post-link.
        """
        lead = await self._throttles.acquire(lead_addr)
        follower = await self._throttles.acquire(follower_addr)
        if not isinstance(lead, LnThrottle) or not isinstance(follower, LnThrottle):
            # Should not happen under a real LocoNet connection, but
            # keeps the type-narrowing honest.
            return
        await self._tc.send(OpcLinkSlots(
            src=follower.slot.number, dest=lead.slot.number,
        ))
        _log.info(
            "LINK slot %d as follower of %d (addr %d follows %d) direction_normal=%s",
            follower.slot.number, lead.slot.number,
            follower_addr, lead_addr, direction_normal,
        )
        if not direction_normal:
            # Send the opposite of the lead's direction to the
            # follower so it moves in the physical direction the
            # user intended. If the lead has no direction yet (idle
            # slot), we default to REVERSE for the follower -- a
            # subsequent ``set_direction`` call will re-align both
            # via the inverted fan-out.
            lead_dir = getattr(lead, "direction", ThrottleDirection.FORWARD)
            await follower.set_direction(_invert(lead_dir))

    async def _unlink_pair(self, lead_addr: int, follower_addr: int) -> None:
        """Send ``OPC_UNLINK_SLOTS`` for the pair.

        The throttles must still hold their slots -- we look them up
        rather than acquire, because after unlink the follower is a
        standalone slot again and re-acquiring would confuse the CS.
        """
        lead = self._throttles.get(lead_addr)
        follower = self._throttles.get(follower_addr)
        if not isinstance(lead, LnThrottle) or not isinstance(follower, LnThrottle):
            return
        await self._tc.send(OpcUnlinkSlots(
            src=follower.slot.number, dest=lead.slot.number,
        ))
        _log.info(
            "UNLINK slot %d from lead %d (addr %d -> %d)",
            follower.slot.number, lead.slot.number,
            follower_addr, lead_addr,
        )

    # ---- ConsistManager overrides ---------------------------------------

    async def _after_membership_change(self, consist: Consist) -> None:
        """Diff the current wire-linked set against the consist's
        membership and issue the LINK/UNLINK deltas.

        The consist's first loco is always the lead. Every other member
        must be LINKed to the lead's slot. Order of LINK ops matches
        JMRI Java's behaviour: unlink stale followers first, then link
        new ones -- avoids the CS transiently seeing overlapping links.
        """
        if consist.kind == "software":
            # Explicit software consist: don't touch the wire.
            return
        if not consist.locos:
            return
        lead_addr = consist.locos[0].address
        desired: set[tuple[int, int]] = {
            (lead_addr, m.address) for m in consist.locos[1:]
        }
        current = self._linked.setdefault(consist.address, set())
        to_unlink = current - desired
        to_link = desired - current
        for lead, follower in to_unlink:
            await self._unlink_pair(lead, follower)
            current.discard((lead, follower))
        for lead, follower in to_link:
            # Look up the follower's direction_normal flag from the
            # consist's authoritative loco list. Absence (defensive
            # default) is treated as normal -- matches the base
            # class's default when direction_normal isn't supplied.
            follower_dir_normal = True
            for m in consist.locos:
                if m.address == follower:
                    follower_dir_normal = m.direction_normal
                    break
            await self._link_pair(
                lead, follower, direction_normal=follower_dir_normal,
            )
            current.add((lead, follower))

    async def _before_delete(self, consist: Consist) -> None:
        """Unlink every follower before dropping the consist."""
        if consist.kind == "software":
            return
        for lead_addr, follower_addr in list(
            self._linked.get(consist.address, set())
        ):
            await self._unlink_pair(lead_addr, follower_addr)
        self._linked.pop(consist.address, None)

    async def set_speed(self, consist_address: int, speed: float) -> None:
        """Send to the lead only -- the CS fans via LINK.

        For a ``kind="software"`` consist stored alongside CS-managed
        ones, we still delegate to the base's per-loco fan-out.
        """
        consist = self._require(consist_address)
        if consist.kind == "software":
            await super().set_speed(consist_address, speed)
            return
        if not consist.locos:
            return
        lead = consist.locos[0]
        throttle = await self._throttles.acquire(
            lead.address, long_address=lead.long_address,
        )
        await throttle.set_speed(speed)

    async def set_direction(
        self, consist_address: int, direction: ThrottleDirection,
    ) -> None:
        """Send direction to the lead; fan the inverted direction to
        any follower whose ``direction_normal`` is False.

        LocoNet's ``OPC_LINK_SLOTS`` fans speed to followers
        automatically but not direction -- the CS just replicates the
        lead's direction bit, so a physically-reversed follower would
        run the wrong way. Mirroring the JMRI Java behaviour in
        ``LocoNetConsist.setDirection`` (line 411..415): send the
        lead's requested direction to every normal follower and the
        opposite direction to every reversed follower.
        """
        consist = self._require(consist_address)
        if consist.kind == "software":
            await super().set_direction(consist_address, direction)
            return
        if not consist.locos:
            return

        # Lead throttle: direction as requested.
        lead = consist.locos[0]
        lead_throttle = await self._throttles.acquire(
            lead.address, long_address=lead.long_address,
        )
        await lead_throttle.set_direction(direction)

        # Only inverted followers need an explicit DIRF from us -- the
        # CS handles normal followers' direction via its LINK
        # fan-out. Touching normal followers would duplicate the CS's
        # work and count as extra wire chatter that the "only 1 DIRF"
        # test guards against.
        inverted = _invert(direction)
        for member in consist.locos[1:]:
            if member.direction_normal:
                continue
            member_throttle = await self._throttles.acquire(
                member.address, long_address=member.long_address,
            )
            await member_throttle.set_direction(inverted)

    async def emergency_stop(self, consist_address: int) -> None:
        """Send e-stop to the lead -- CS fans it out.

        For extra safety we also fan to any acquired follower throttle
        so a decoder that's momentarily out of sync with the CS still
        stops. Matches JMRI Java's belt-and-braces approach on the
        e-stop path.
        """
        consist = self._require(consist_address)
        if consist.kind == "software":
            await super().emergency_stop(consist_address)
            return
        for member in consist.locos:
            throttle = await self._throttles.acquire(
                member.address, long_address=member.long_address,
            )
            await throttle.emergency_stop()

    # ``set_function`` intentionally *not* overridden: functions are per
    # loco on LocoNet -- LINK doesn't fan them -- so the base's fan-out is
    # exactly what we want.
