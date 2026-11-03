"""``ConsistManager`` -- CRUD for consists + fan-out throttle control.

A consist is an ordered set of locomotives that operate as one -- an MU
(multi-unit) group. Three flavours exist in the DCC world:

* **Advanced** (aka *NMRA*, aka *CV19*) -- each loco's CV19 holds a
  shared short address; the decoder itself responds to commands on
  that address. Set-up is a decoder programming task; running the
  group is transparent afterwards.
* **Command station** -- the CS maintains the group in its own memory
  and forwards commands sent to the group's "handle" to every
  member. LocoNet's ``OPC_LINK_SLOTS``, NCE's consist memory table,
  and DCC-EX's ``<C>`` command are all this shape.
* **Software** -- JMRI holds the group; every command sent to the
  group fans out into individual throttle commands, one per member.
  Works on any driver because it only uses the ``ThrottleManager``
  API -- the fallback for drivers that don't (or can't) do hardware
  consisting.

This module ships:

* the base :class:`ConsistManager` -- CRUD + fan-out helpers that work
  for every driver via the connection's :class:`ThrottleManager`;
* subclass hooks so drivers can override the wire ops without
  changing the public surface.

Driver overrides live in each driver package (``pmri_loconet.consist``,
``pmri_nce.consist``, etc.) and are wired via
:meth:`SystemConnection._make_consist_manager`.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from pmri_core.beans.consist import Consist, ConsistKind, ConsistLoco
from pmri_core.events import Signal
from pmri_core.state import ThrottleDirection

if TYPE_CHECKING:
    from pmri_core.beans.programmer import ProgrammerManager
    from pmri_core.beans.throttle_manager import ThrottleManager

_log = __import__("logging").getLogger(__name__)


class ConsistManager:
    """Track a set of consists and fan throttle commands to their members.

    The base implementation is the **software consist** manager: it
    stores consists in a dict keyed by the group's DCC address and
    executes group speed/direction/function commands by looping over
    every member's :class:`Throttle`. Drivers that support hardware
    consisting subclass this and override :meth:`_after_create` and
    :meth:`_before_delete` to install/remove the wire-level group;
    they typically leave the fan-out helpers alone since the base's
    per-loco commands are still valid even when the CS is grouping.

    ``created`` and ``deleted`` are :class:`Signal` fan-outs so the
    push side of the JSON server, the WiThrottle broadcast, and the
    web UI's live table can react without polling.
    """

    default_kind: ConsistKind = "software"

    def __init__(
        self, throttles: ThrottleManager, system_prefix: str,
        programmers: ProgrammerManager | None = None,
    ) -> None:
        if not system_prefix:
            raise ValueError("system_prefix must be a non-empty string")
        self._throttles = throttles
        # Ops-mode programmer manager, used by the ``advanced`` kind to
        # write CV19 on each member decoder as they join or leave. May
        # be ``None`` on non-hardware setups (tests, InternalConnection);
        # advanced consists degrade silently to a no-op in that case
        # rather than raising.
        self._programmers: ProgrammerManager | None = programmers
        self.system_prefix = system_prefix
        self._consists: dict[int, Consist] = {}
        # Runtime drive state per consist address. Not persisted -- the
        # consist itself is pure config (membership + wire kind), and
        # loco throttles carry the authoritative state. But the UI
        # needs a per-consist "last commanded" value to render the
        # slider position and highlight the active direction button;
        # without it every drive command re-renders the card at the
        # hardcoded defaults and the user sees the slider snap back to
        # 0 on every drag-release. Reset on delete.
        self._speeds: dict[int, float] = {}
        self._directions: dict[int, ThrottleDirection] = {}
        self.created: Signal[Consist] = Signal()
        self.deleted: Signal[Consist] = Signal()
        self.updated: Signal[Consist] = Signal()

    def rebind_throttles(self, throttles: ThrottleManager) -> None:
        """Point the fan-out at a new :class:`ThrottleManager`.

        Base :meth:`SystemConnection.__init__` builds the consist manager
        before drivers get a chance to install their hardware-aware
        throttle manager in :meth:`open`. Without this hook the consist
        manager keeps a stale reference to the pre-open placeholder and
        every fan-out silently drives plain-Python :class:`Throttle`
        beans that never touch the wire -- the consist would look like
        it's working (bean state advances, signals fire) while the
        actual locomotive sits still. Drivers call this from ``open()``
        immediately after replacing ``self.throttles``.
        """
        self._throttles = throttles

    def rebind_programmers(
        self, programmers: ProgrammerManager | None
    ) -> None:
        """Point the CV19 write path at a new :class:`ProgrammerManager`.

        Same rationale as :meth:`rebind_throttles` -- the driver's
        hardware-aware programmer manager isn't in place until
        :meth:`open` runs. Advanced consists need ops-mode access to
        program CV19 on each member; without a real programmer the
        writes silently become no-ops.
        """
        self._programmers = programmers

    def current_speed(self, address: int) -> float:
        """Last commanded fan-out speed for this consist, or 0.0."""
        return self._speeds.get(address, 0.0)

    def current_direction(self, address: int) -> ThrottleDirection:
        """Last commanded direction for this consist, or FORWARD."""
        return self._directions.get(address, ThrottleDirection.FORWARD)

    # ---- CRUD ------------------------------------------------------------

    async def create(
        self,
        address: int,
        *,
        kind: ConsistKind | None = None,
        comment: str = "",
    ) -> Consist:
        """Create a new consist and register it. Idempotent by address.

        Returns the existing consist if one already lives at *address*
        rather than raising -- matches JMRI's tolerance for repeated
        create calls from restore paths.

        Consist addresses are constrained to 1..127 short by the
        ``Consist`` model; the caller doesn't need a ``long_address``
        flag because there is no long-address form of a consist. See
        :data:`pmri_core.beans.consist.CONSIST_ADDRESS_MAX`.
        """
        existing = self._consists.get(address)
        if existing is not None:
            return existing
        consist = Consist(
            address=address,
            kind=kind or self.default_kind, comment=comment,
        )
        self._consists[address] = consist
        await self._after_create(consist)
        await self.created.emit(consist)
        return consist

    async def delete(self, address: int) -> bool:
        """Remove the consist. Returns ``True`` if one was found.

        For ``advanced`` (CV19) consists, every member's CV19 is
        cleared to 0 on the way out so the decoder stops answering
        to the consist address. Skipping this step leaves the loco
        stuck: it no longer belongs to a consist in our tables, but
        its decoder still ignores its own address until CV19 is
        zeroed out -- a bug the operator hits as "delete the consist,
        can't drive the loco" that they can only fix by manually
        writing CV19=0.
        """
        consist = self._consists.get(address)
        if consist is None:
            return False
        # Clear CV19 BEFORE removing the consist so the write path
        # (which reads no consist state) is unaffected by whether we
        # get to run it -- if _clear_cv19 raises, the consist still
        # exists in our tables and the operator can retry delete.
        if consist.kind == "advanced":
            for loco in list(consist.locos):
                await self._clear_cv19(
                    loco.address, long_address=loco.long_address,
                    consist_address=address,
                )
        self._consists.pop(address, None)
        self._speeds.pop(address, None)
        self._directions.pop(address, None)
        await self._before_delete(consist)
        await self.deleted.emit(consist)
        return True

    def get(self, address: int) -> Consist | None:
        return self._consists.get(address)

    def __iter__(self) -> Iterator[Consist]:
        return iter(self._consists.values())

    def __len__(self) -> int:
        return len(self._consists)

    def __contains__(self, address: int) -> bool:
        return address in self._consists

    # ---- membership -----------------------------------------------------

    async def add_member(
        self,
        consist_address: int,
        member_address: int,
        *,
        long_address: bool = False,
        direction_normal: bool = True,
    ) -> ConsistLoco:
        """Add *member_address* to the consist at *consist_address*.

        The member is appended at the end. If a loco with the same
        address already exists in the consist, its direction flag is
        updated in place and the existing :class:`ConsistLoco` is
        returned. Fires :attr:`updated` on the consist so subscribers
        can react to membership changes.
        """
        consist = self._consists.get(consist_address)
        if consist is None:
            raise KeyError(f"no consist at address {consist_address}")
        for loco in consist.locos:
            if loco.address == member_address:
                loco.direction_normal = direction_normal
                await self._after_membership_change(consist)
                if consist.kind == "advanced":
                    await self._program_cv19(
                        member_address, long_address=loco.long_address,
                        consist_address=consist_address,
                        direction_normal=direction_normal,
                    )
                await self.updated.emit(consist)
                return loco
        loco = ConsistLoco(
            address=member_address,
            long_address=long_address,
            direction_normal=direction_normal,
            position=len(consist.locos),
        )
        consist.locos.append(loco)
        await self._after_membership_change(consist)
        if consist.kind == "advanced":
            await self._program_cv19(
                member_address, long_address=long_address,
                consist_address=consist_address,
                direction_normal=direction_normal,
            )
        await self.updated.emit(consist)
        return loco

    async def _program_cv19(
        self,
        member_address: int,
        *,
        long_address: bool,
        consist_address: int,
        direction_normal: bool,
    ) -> None:
        """Write CV19 on *member_address*'s decoder for an advanced
        consist.

        Per NMRA S-9.2.2, CV19 holds a 7-bit consist address in bits
        0..6 and the direction flag in bit 7. Bit 7 set means the loco
        runs in the reverse direction relative to the consist -- i.e.
        ``direction_normal=False``. A value of 0 in the low 7 bits
        disables the consist for that decoder.
        """
        prog_mgr = self._programmers
        if prog_mgr is None:
            _log.debug(
                "advanced consist %d: no programmer manager, skipping "
                "CV19 write for member %d",
                consist_address, member_address,
            )
            return
        prog = prog_mgr.ops_programmer(
            member_address, long_address=long_address,
        )
        if prog is None:
            _log.debug(
                "advanced consist %d: driver returned no ops "
                "programmer for member %d; CV19 not written",
                consist_address, member_address,
            )
            return
        value = consist_address & 0x7F
        if not direction_normal:
            value |= 0x80
        try:
            await prog.write_cv(19, value)
        except Exception as exc:  # noqa: BLE001
            # POM writes can fail (no ack, decoder not on the track,
            # RailCom garble). Log and keep going -- the consist itself
            # remains configured; the user can re-add the member after
            # putting the loco on the rails.
            _log.warning(
                "CV19 write failed for consist %d, member %d "
                "(value=%d): %s",
                consist_address, member_address, value, exc,
            )

    async def _clear_cv19(
        self, member_address: int, *, long_address: bool,
        consist_address: int,
    ) -> None:
        """Reset CV19 to 0 on the ex-member's decoder so it stops
        answering to the consist address."""
        prog_mgr = self._programmers
        if prog_mgr is None:
            return
        prog = prog_mgr.ops_programmer(
            member_address, long_address=long_address,
        )
        if prog is None:
            return
        try:
            await prog.write_cv(19, 0)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "CV19 clear failed for consist %d, ex-member %d: %s",
                consist_address, member_address, exc,
            )

    async def remove_member(
        self, consist_address: int, member_address: int,
    ) -> bool:
        """Drop *member_address* from the consist. Returns True if found.

        Re-numbers surviving members' ``position`` fields so they stay
        contiguous -- the ordering is used for lead-loco / rear-loco
        semantics in DCC-EX and NCE displays.
        """
        consist = self._consists.get(consist_address)
        if consist is None:
            return False
        removed = False
        removed_long_address = False
        surviving: list[ConsistLoco] = []
        for loco in consist.locos:
            if loco.address == member_address and not removed:
                removed = True
                # Capture before the loco leaves -- the ops programmer
                # for the CV19 clear is keyed by the decoder's real
                # address form (short vs long).
                removed_long_address = loco.long_address
                continue
            surviving.append(loco)
        if not removed:
            return False
        # Renumber positions.
        for i, loco in enumerate(surviving):
            loco.position = i
        consist.locos = surviving
        await self._after_membership_change(consist)
        if consist.kind == "advanced":
            await self._clear_cv19(
                member_address, long_address=removed_long_address,
                consist_address=consist_address,
            )
        await self.updated.emit(consist)
        return True

    # ---- fan-out throttle control ---------------------------------------

    async def set_speed(self, consist_address: int, speed: float) -> None:
        """Drive the consist's speed.

        * ``advanced`` (CV19): one throttle command to the consist
          address itself. After CV19 is written, every member decoder
          responds to packets on the consist address (per NMRA S-9.2.2),
          so fanning out to individual member addresses would be
          ignored -- the decoder is now consisted and its own address
          is dormant until CV19 is cleared.
        * ``software`` / ``command_station``: fan the command out to
          every member throttle. Missing throttles are acquired first
          so a fresh consist's first speed command works.
          ``command_station`` consists on hardware-aware drivers
          (LocoNet, future NCE) override this to send only to the
          lead slot and let the CS fan the group.

        Records the commanded speed on the manager and emits ``updated``
        so any subscriber (the /consists page's WebSocket bus in
        particular) re-renders the card with the slider at the new
        position instead of snapping back to zero.
        """
        consist = self._require(consist_address)
        if consist.kind == "advanced":
            throttle = await self._throttles.acquire(
                consist_address, long_address=False,
            )
            await throttle.set_speed(speed)
        else:
            for member in consist.locos:
                throttle = await self._throttles.acquire(
                    member.address, long_address=member.long_address,
                )
                await throttle.set_speed(speed)
        self._speeds[consist_address] = speed
        await self.updated.emit(consist)

    async def set_direction(
        self, consist_address: int, direction: ThrottleDirection,
    ) -> None:
        """Drive the consist's direction.

        * ``advanced`` (CV19): one throttle command to the consist
          address. Per-loco orientation is already encoded in CV19
          bit 7 on each decoder, so the decoder itself inverts as
          needed.
        * ``software`` / ``command_station``: fan direction out to
          every member. A member with ``direction_normal=False`` is
          oriented backwards in the consist so it receives the
          *opposite* direction -- the core reason software consists
          exist. Hardware-aware ``command_station`` managers (LocoNet)
          override to send to the lead and let the CS fan.
        """
        consist = self._require(consist_address)
        if consist.kind == "advanced":
            throttle = await self._throttles.acquire(
                consist_address, long_address=False,
            )
            await throttle.set_direction(direction)
        else:
            opposite = (
                ThrottleDirection.REVERSE
                if direction is ThrottleDirection.FORWARD
                else ThrottleDirection.FORWARD
            )
            for member in consist.locos:
                throttle = await self._throttles.acquire(
                    member.address, long_address=member.long_address,
                )
                wire = direction if member.direction_normal else opposite
                await throttle.set_direction(wire)
        self._directions[consist_address] = direction
        await self.updated.emit(consist)

    async def set_function(
        self, consist_address: int, fn: int, on: bool,
    ) -> None:
        """Fan a function command to every member.

        Functions are addressed to individual locos even in an
        advanced consist: per NMRA S-9.2.2 the decoder responds to
        function packets on its own address by default, and only
        follows the consist address when CV21/CV22 are explicitly
        programmed. We don't set those, so functions stay per-loco
        for every consist kind.
        """
        consist = self._require(consist_address)
        for member in consist.locos:
            throttle = await self._throttles.acquire(
                member.address, long_address=member.long_address,
            )
            await throttle.set_function(fn, on)

    async def emergency_stop(self, consist_address: int) -> None:
        """Emergency-stop the consist.

        Routing follows :meth:`set_speed`: advanced consists e-stop
        via one command to the consist address, others fan out to
        each member.
        """
        consist = self._require(consist_address)
        if consist.kind == "advanced":
            throttle = await self._throttles.acquire(
                consist_address, long_address=False,
            )
            await throttle.emergency_stop()
        else:
            for member in consist.locos:
                throttle = await self._throttles.acquire(
                    member.address, long_address=member.long_address,
                )
                await throttle.emergency_stop()
        # Reset UI speed so the slider snaps back to 0 after an e-stop
        # rather than staying at whatever the user last commanded.
        self._speeds[consist_address] = 0.0
        await self.updated.emit(consist)

    # ---- driver hooks ---------------------------------------------------

    async def _after_create(self, consist: Consist) -> None:
        """Called after a new consist is registered. Driver hook.

        Subclasses install the wire-level group here -- LocoNet's
        ``LINK_SLOTS``, NCE's consist-memory write, DCC-EX's ``<C>``.
        The base impl is a no-op (software consist).
        """
        return

    async def _before_delete(self, consist: Consist) -> None:
        """Called before a consist is dropped from the registry.

        Subclasses tear the wire-level group down here.
        """
        return

    async def _after_membership_change(self, consist: Consist) -> None:
        """Called after ``add_member`` / ``remove_member`` mutates the
        consist's locos list. Subclasses re-sync the wire-level group.
        """
        return

    # ---- internals ------------------------------------------------------

    def _require(self, address: int) -> Consist:
        consist = self._consists.get(address)
        if consist is None:
            raise KeyError(f"no consist at address {address}")
        return consist
