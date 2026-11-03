"""``NceConsistManager`` -- hardware consist manager for NCE PowerPro.

NCE tracks command-station consists internally (1..127, six-member max:
one LEAD + one REAR + up to four MID). The CS handles speed and
direction fan-out on its own once a consist is built; we send add /
remove commands using the existing ``0xA2`` loco command with
consist-specific sub-command bytes.

Wire ops (all sub-commands of ``OP_NCE_LOCO_CMD = 0xA2`` -- see
:mod:`pmri_nce.opcodes` for the numeric constants):

* ``LOCO_SUB_CS_ADD_LEAD_FWD / _REV`` -- install the first (lead) loco.
* ``LOCO_SUB_CS_ADD_REAR_FWD / _REV`` -- install the second (rear) loco.
* ``LOCO_SUB_CS_ADD_MID_FWD / _REV`` -- install a mid loco (positions 3..6).
* ``LOCO_SUB_CS_REMOVE`` -- remove a *mid* loco. LEAD/REAR are rejected
  by the CS; the manager guards this at the bean level.
* ``LOCO_SUB_CS_KILL`` -- dispose the whole consist; sent using the
  LEAD's DCC address so the CS knows which consist to tear down.

All values were cross-checked against JMRI Java ``jmri.jmrix.nce.NceConsist``
(``add()``, ``removeLocoFromConsist``, ``killConsist``) before being
committed here -- see the notes in :mod:`pmri_nce.opcodes`.

Semantic constraints:

* **Address range 1..127** -- matches ``CONSIST_ADDRESS_MAX`` (already
  enforced by the ``Consist`` model).
* **Size limit 6 members** -- enforced here so add-past-six raises a
  ``ValueError`` that surfaces to the UI as a 400 rather than a silent
  no-op.
* **LEAD and REAR can't be removed individually** -- the CS design
  requires either killing the whole consist or leaving them alone.
  ``remove_member`` rejects those positions with a clear error.
* **Direction is baked in at ADD time**. Flipping a member's
  ``direction_normal`` after add doesn't reprogram the CS in this
  first pass; the workaround is delete + recreate. Consistent with
  JMRI Java's behaviour.

Base :class:`~pmri_core.beans.consist_manager.ConsistManager` remains
usable for ``kind="software"`` consists on an NCE connection -- those
bypass the CS and fan out via throttle commands, useful when a caller
wants inverted-direction followers (not supported by NCE CS consists in
this pass) or more than six members.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pmri_core.beans.consist import Consist
from pmri_core.beans.consist_manager import ConsistManager
from pmri_core.beans.throttle_manager import ThrottleManager
from pmri_core.state import ThrottleDirection

from pmri_nce.messages import nce_loco_cmd, read_16_memory
from pmri_nce.opcodes import (
    LOCO_SUB_CS_ADD_LEAD_FWD,
    LOCO_SUB_CS_ADD_LEAD_REV,
    LOCO_SUB_CS_ADD_MID_FWD,
    LOCO_SUB_CS_ADD_MID_REV,
    LOCO_SUB_CS_ADD_REAR_FWD,
    LOCO_SUB_CS_ADD_REAR_REV,
    LOCO_SUB_CS_KILL,
    LOCO_SUB_CS_REMOVE,
)

if TYPE_CHECKING:
    from pmri_core.beans.programmer import ProgrammerManager

    from pmri_nce.traffic import NceTrafficController

_log = logging.getLogger(__name__)

# CS-managed consists on NCE PowerPro top out at six members (one LEAD,
# one REAR, up to four MIDs). Enforced in ``add_member`` -- the CS would
# silently drop the seventh anyway but a clean error surfaces to the UI.
NCE_CS_CONSIST_SIZE_LIMIT = 6

# NCE consist table addresses. The table is not a contiguous
# per-consist record -- it's three parallel arrays, one per slot role,
# packed by consist number:
#
# * HEAD (0xF500): 2 bytes per consist x 128 consists = 256 bytes total,
#   holds the LEAD loco.
# * TAIL (0xF600): 2 bytes per consist x 128 = 256 bytes, holds REAR.
# * MID  (0xF700): ``mid_entries`` x 2 bytes per consist x 128 = 1024 B
#   on stock firmware (mid_entries=4), holds the 4 mid locos.
#
# Consist N's members are therefore stitched from three lookups:
# ``HEAD[N*2 : N*2+2]``, ``TAIL[N*2 : N*2+2]``, and
# ``MID[N*8 : N*8+8]``. Cross-checked against the JMRI Java sources
# ``NceCmdStationMemory`` (addresses), ``NceConsist.readConsistMemory``
# (per-region indexing at line 356..373), and ``NceConsistBackup``
# (contiguous 1536-byte iteration).
#
# The naive "16-byte record per consist starting at 0xF500" layout
# that shipped in beta 4 was wrong: it read random memory outside
# the consist table for higher consist numbers (e.g. consist 127
# -> 0xFCF0, well past the table's 0xFB00 end) and produced
# phantom locos on read-back.
NCE_CONSIST_ADDR_MIN = 1
NCE_CONSIST_ADDR_MAX = 127
NCE_CONSIST_HEAD_BYTES = 2
NCE_CONSIST_TAIL_BYTES = 2
NCE_CONSIST_HEAD_REGION_BYTES = 256   # 2 B x 128 consists
NCE_CONSIST_TAIL_REGION_BYTES = 256
NCE_CONSIST_READ_CHUNK = 16   # NCE READ16 opcode payload size


def _decode_loco(hi: int, lo: int) -> tuple[int, bool]:
    """Decode one 2-byte loco slot into ``(address, is_long)``.

    Per JMRI Java's ``NceConsist.getLocoAddrText`` (line 493..500):
    address bits 8..13 live in the low 6 bits of the high byte;
    address bits 0..7 are the low byte; ``(hi & 0xC0) == 0xC0``
    signals a long address. ``(0, False)`` = empty slot."""
    addr = ((hi << 8) & 0x3F00) | (lo & 0xFF)
    is_long = (hi & 0xC0) == 0xC0
    return addr, is_long


def _members_for_consist(
    consist_num: int,
    head_region: bytes,
    tail_region: bytes,
    mid_region: bytes,
    mid_entries: int,
) -> list[tuple[int, bool]]:
    """Assemble consist ``consist_num``'s members from the three
    parallel region buffers. Empty slots (address 0) are skipped so
    callers see live membership only.

    ``head_region`` / ``tail_region`` are 2 bytes per consist.
    ``mid_region`` is ``mid_entries * 2`` bytes per consist. All three
    buffers are indexed by the same consist number.

    Order in the returned list matches the wire layout: LEAD, REAR,
    then any mids that are non-empty."""
    head_off = consist_num * NCE_CONSIST_HEAD_BYTES
    tail_off = consist_num * NCE_CONSIST_TAIL_BYTES
    mid_off = consist_num * mid_entries * 2
    if (
        head_off + 2 > len(head_region)
        or tail_off + 2 > len(tail_region)
        or mid_off + mid_entries * 2 > len(mid_region)
    ):
        raise ValueError(
            f"consist {consist_num} indexes past the region buffers "
            f"(head={len(head_region)}, tail={len(tail_region)}, "
            f"mid={len(mid_region)})"
        )
    out: list[tuple[int, bool]] = []
    for hi, lo in (
        (head_region[head_off], head_region[head_off + 1]),
        (tail_region[tail_off], tail_region[tail_off + 1]),
    ):
        addr, is_long = _decode_loco(hi, lo)
        if addr != 0:
            out.append((addr, is_long))
    for slot in range(mid_entries):
        hi = mid_region[mid_off + slot * 2]
        lo = mid_region[mid_off + slot * 2 + 1]
        addr, is_long = _decode_loco(hi, lo)
        if addr != 0:
            out.append((addr, is_long))
    return out


class NceConsistManager(ConsistManager):
    """NCE consist manager. Default kind is ``command_station``.

    Software consists still work if the caller asks for
    ``kind="software"`` at create time -- the base fan-out then drives
    each member's throttle directly. Advanced consists (``kind="advanced"``)
    also still work, going through the base's CV19 ops-mode write.
    """

    default_kind = "command_station"

    def __init__(
        self,
        throttles: ThrottleManager,
        system_prefix: str,
        programmers: ProgrammerManager | None = None,
        *,
        tc: NceTrafficController,
        consist_head_addr: int = 0xF500,
        consist_tail_addr: int = 0xF600,
        consist_mid_addr: int = 0xF700,
        consist_mid_entries: int = 4,
    ) -> None:
        super().__init__(throttles, system_prefix, programmers)
        self._tc = tc
        self._consist_head_addr = consist_head_addr
        self._consist_tail_addr = consist_tail_addr
        self._consist_mid_addr = consist_mid_addr
        self._consist_mid_entries = consist_mid_entries
        # Members already installed on the CS side for each consist,
        # keyed by consist address. Compared against ``consist.locos``
        # in :meth:`_after_membership_change` to compute add / remove
        # deltas. A member's tuple carries ``(address, long_address,
        # direction_normal)`` so we can rebuild the exact ADD command
        # if we ever need to re-send after a kill.
        self._on_cs: dict[int, list[tuple[int, bool, bool]]] = {}
        # Set during :meth:`refresh_from_cs` so the membership-change
        # hook skips its wire ops -- the CS already has these members
        # (that's why we're reading them back), and re-sending ADD
        # for something the CS already knows about is at best chatter
        # and at worst confuses firmware.
        self._refreshing = False

    # ---- overrides ------------------------------------------------------

    async def add_member(
        self,
        consist_address: int,
        member_address: int,
        *,
        long_address: bool = False,
        direction_normal: bool = True,
    ) -> object:
        """Enforce the CS-consist size cap before delegating to the
        base add. Software consists on an NCE connection bypass the
        cap since they don't touch the CS.
        """
        consist = self._consists.get(consist_address)
        if (
            consist is not None
            and consist.kind == "command_station"
            and not self._is_existing_member(consist, member_address)
            and len(consist.locos) >= NCE_CS_CONSIST_SIZE_LIMIT
            and not self._refreshing
        ):
            raise ValueError(
                f"NCE CS consist {consist_address} is full "
                f"({NCE_CS_CONSIST_SIZE_LIMIT} members max); "
                f"remove a member or delete the consist first"
            )
        return await super().add_member(
            consist_address, member_address,
            long_address=long_address, direction_normal=direction_normal,
        )

    async def remove_member(
        self, consist_address: int, member_address: int,
    ) -> bool:
        """Reject LEAD (position 0) and REAR (position 1) removals for
        CS-managed consists -- the CS silently ignores those anyway,
        and the JMRI Java implementation rejects them at the bean
        level for the same reason. The escape hatch is to delete the
        whole consist, which sends KILL.

        During a memory-mapped refresh (``self._refreshing``) the
        LEAD/REAR guard is bypassed: reconciliation may need to
        drop a stale LEAD our bookkeeping thought was on the CS
        but that the CS's actual table doesn't include. Skipping
        the wire ops is handled by
        :meth:`_after_membership_change`.
        """
        consist = self._consists.get(consist_address)
        if (
            consist is not None
            and consist.kind == "command_station"
            and not self._refreshing
        ):
            for loco in consist.locos:
                if loco.address == member_address and loco.position < 2:
                    role = "LEAD" if loco.position == 0 else "REAR"
                    raise ValueError(
                        f"cannot remove {role} loco {member_address} from "
                        f"NCE CS consist {consist_address}; "
                        f"delete the consist to disband it"
                    )
        return await super().remove_member(consist_address, member_address)

    async def _after_membership_change(self, consist: Consist) -> None:
        """Diff the CS-side member list against the consist's current
        locos and send the ADD / REMOVE deltas.

        Direction is baked in at ADD time -- flipping ``direction_normal``
        on an already-added member does *not* re-send the add. To change
        orientation, remove the member (only MID positions are removable)
        and re-add, or dispose the whole consist and rebuild.

        During a memory-mapped refresh (see :meth:`refresh_from_cs`)
        the wire ops are skipped: we're reflecting the CS's own
        state back into our beans, so sending ADD/REMOVE for it
        would duplicate what the CS already knows and count as
        wasted chatter (worst case, a firmware confusion).
        """
        if self._refreshing:
            return
        if consist.kind != "command_station":
            return
        cs_list = self._on_cs.setdefault(consist.address, [])
        cs_addrs = {t[0] for t in cs_list}
        want_addrs = {m.address for m in consist.locos}

        # Adds first, then removes. On adds, position is inferred from
        # the member's index in ``consist.locos`` -- the base's
        # ``add_member`` sets ``position`` to the pre-append length, so
        # it aligns with our LEAD/REAR/MID sub-command mapping.
        for member in consist.locos:
            if member.address in cs_addrs:
                continue
            await self._send_add(
                consist.address, member.address,
                long_address=member.long_address,
                direction_normal=member.direction_normal,
                position=member.position,
            )
            cs_list.append((
                member.address, member.long_address, member.direction_normal,
            ))

        # Removes: anything on the CS side but not in the desired
        # membership. In practice only MID positions ever land here
        # because the ``remove_member`` override blocks LEAD/REAR.
        stale = [t for t in cs_list if t[0] not in want_addrs]
        for addr, long_addr, _dir in stale:
            await self._send_remove(addr, long_addr)
            cs_list.remove((addr, long_addr, _dir))

    async def _before_delete(self, consist: Consist) -> None:
        """Send KILL to the LEAD address so the CS wipes its
        consist-table entry for this consist. If there's no LEAD (the
        consist was empty), there's nothing to kill -- just drop our
        bookkeeping."""
        if consist.kind != "command_station":
            return
        cs_list = self._on_cs.pop(consist.address, None)
        if not cs_list:
            return
        lead_addr, lead_long, _dir = cs_list[0]
        await self._send_kill(lead_addr, lead_long)

    # ---- CS memory-mapped read-back --------------------------------------

    async def refresh_from_cs(
        self,
        *,
        min_consist: int = NCE_CONSIST_ADDR_MIN,
        max_consist: int = NCE_CONSIST_ADDR_MAX,
    ) -> list[int]:
        """Scan the CS consist table and reconcile the in-memory state.

        Reads the three parallel regions (HEAD, TAIL, MID) in 16-byte
        chunks, then for each consist number in the range stitches
        the LEAD + REAR + MID slots back together and diffs against
        the in-memory bean. Then:

        * If the CS has members for a consist we don't track:
          create the consist (kind=``command_station``) and populate
          it via the base's ``add_member`` (which fires the normal
          added/updated signals so the UI + WebSocket push both see
          the new state).
        * If we track a consist the CS still knows about but with
          different membership: bring our state in line -- add
          missing members, remove ones the CS no longer has.
        * If the CS shows an empty slot for a consist we track and
          the consist was already ``kind=command_station``: leave
          it alone. Deletions are a manual operator action, not
          something an unrelated refresh should perform silently.

        **Direction not recovered**: the memory-table read gives
        us addresses only, not the per-loco direction flag (which
        was baked in at the CS's ADD command's FWD/REV variant and
        isn't reflected in the consist table on read-back). Every
        reconstructed member gets ``direction_normal=True``. Post
        a corrective ``add_member`` if the operator needs to flip
        orientation.

        Returns the list of consist addresses that were changed
        (created or membership-updated) so callers can log or
        surface a "found N consists" notice.
        """
        mid_region_bytes = (
            self._consist_mid_entries * 2 * (NCE_CONSIST_ADDR_MAX + 1)
        )
        head = await self._read_region(
            self._consist_head_addr, NCE_CONSIST_HEAD_REGION_BYTES,
        )
        tail = await self._read_region(
            self._consist_tail_addr, NCE_CONSIST_TAIL_REGION_BYTES,
        )
        mid = await self._read_region(
            self._consist_mid_addr, mid_region_bytes,
        )

        changed: list[int] = []
        self._refreshing = True
        try:
            for consist_num in range(min_consist, max_consist + 1):
                members = _members_for_consist(
                    consist_num, head, tail, mid,
                    self._consist_mid_entries,
                )
                if await self._reconcile_one(consist_num, members):
                    changed.append(consist_num)
        finally:
            self._refreshing = False
        return changed

    async def _read_region(self, base: int, length: int) -> bytes:
        """Read ``length`` bytes starting at ``base`` in 16-byte chunks
        via the NCE READ16 opcode. Length must be a multiple of 16 --
        the whole consist table is aligned that way on stock NCE
        firmware."""
        if length % NCE_CONSIST_READ_CHUNK != 0:
            raise ValueError(
                f"region length {length} not aligned to "
                f"{NCE_CONSIST_READ_CHUNK}-byte READ16 chunks"
            )
        buf = bytearray(length)
        for offset in range(0, length, NCE_CONSIST_READ_CHUNK):
            reply = await self._tc.send_command(read_16_memory(base + offset))
            chunk = reply.data[:NCE_CONSIST_READ_CHUNK]
            if len(chunk) < NCE_CONSIST_READ_CHUNK:
                raise ValueError(
                    f"short READ16 reply at {base + offset:#06x}: "
                    f"{len(chunk)} bytes"
                )
            buf[offset : offset + NCE_CONSIST_READ_CHUNK] = chunk
        return bytes(buf)

    async def _reconcile_one(
        self, consist_num: int, cs_members: list[tuple[int, bool]],
    ) -> bool:
        """Bring one consist's in-memory state into line with what
        the CS reports. Returns True when anything changed."""
        existing = self._consists.get(consist_num)

        # CS has no members here.
        if not cs_members:
            # Empty in-memory too -> no work.
            if existing is None:
                return False
            # We track a consist the CS doesn't. Don't delete --
            # operator hasn't asked for it. Log and move on.
            _log.debug(
                "refresh_from_cs: consist %d empty on CS but still "
                "tracked in-memory (skipping)", consist_num,
            )
            return False

        # CS has members. Create the consist if missing.
        if existing is None:
            existing = await self.create(consist_num, kind="command_station")

        current = {m.address for m in existing.locos}
        want = {addr for addr, _ in cs_members}
        changed = False

        # Add missing members.
        for addr, is_long in cs_members:
            if addr not in current:
                await super().add_member(
                    consist_num, addr, long_address=is_long,
                    direction_normal=True,
                )
                # Update our CS-side bookkeeping too so subsequent
                # ADD-command sends compute the right delta.
                cs_list = self._on_cs.setdefault(consist_num, [])
                cs_list.append((addr, is_long, True))
                changed = True

        # Remove members the CS no longer has.
        for addr in current - want:
            await super().remove_member(consist_num, addr)
            cs_list = self._on_cs.get(consist_num, [])
            self._on_cs[consist_num] = [
                t for t in cs_list if t[0] != addr
            ]
            changed = True

        return changed

    # ---- fan-out overrides -- CS handles speed / direction internally ----

    async def set_speed(self, consist_address: int, speed: float) -> None:
        """Send to the LEAD only -- the CS fans out. Falls through to
        the base per-loco fan-out for ``kind="software"``.
        """
        consist = self._require(consist_address)
        if consist.kind != "command_station":
            await super().set_speed(consist_address, speed)
            return
        if not consist.locos:
            return
        lead = consist.locos[0]
        throttle = await self._throttles.acquire(
            lead.address, long_address=lead.long_address,
        )
        await throttle.set_speed(speed)
        # Preserve the UI-side "last commanded" state that the base
        # tracks -- the /consists slider reads this back on re-render.
        self._speeds[consist_address] = speed
        await self.updated.emit(consist)

    async def set_direction(
        self, consist_address: int, direction: ThrottleDirection,
    ) -> None:
        """Send to the LEAD only. Per-loco orientation is already baked
        in on each member's slot (from the ADD command's FWD/REV
        variant) so flipping the whole consist's direction here is a
        single-address operation."""
        consist = self._require(consist_address)
        if consist.kind != "command_station":
            await super().set_direction(consist_address, direction)
            return
        if not consist.locos:
            return
        lead = consist.locos[0]
        throttle = await self._throttles.acquire(
            lead.address, long_address=lead.long_address,
        )
        await throttle.set_direction(direction)
        self._directions[consist_address] = direction
        await self.updated.emit(consist)

    async def emergency_stop(self, consist_address: int) -> None:
        """E-stop the LEAD; CS fans out. Belt-and-braces: we also
        e-stop every member throttle individually so a decoder that's
        momentarily out of sync with the CS still stops (matches JMRI
        Java's ``NceConsistManager`` e-stop behaviour)."""
        consist = self._require(consist_address)
        if consist.kind != "command_station":
            await super().emergency_stop(consist_address)
            return
        for member in consist.locos:
            throttle = await self._throttles.acquire(
                member.address, long_address=member.long_address,
            )
            await throttle.emergency_stop()
        self._speeds[consist_address] = 0.0
        await self.updated.emit(consist)

    # ``set_function`` intentionally *not* overridden. NCE CS consists,
    # like LocoNet's, don't fan functions -- the base's per-loco loop is
    # exactly what a user wants (a headlight command should hit every
    # loco separately).

    # ---- wire helpers ---------------------------------------------------

    async def _send_add(
        self,
        consist_address: int,
        member_address: int,
        *,
        long_address: bool,
        direction_normal: bool,
        position: int,
    ) -> None:
        """Send the appropriate ``0xA2`` sub-command based on position
        and direction. Position 0 -> LEAD, 1 -> REAR, else MID."""
        if position == 0:
            sub = (
                LOCO_SUB_CS_ADD_LEAD_FWD if direction_normal
                else LOCO_SUB_CS_ADD_LEAD_REV
            )
            role = "LEAD"
        elif position == 1:
            sub = (
                LOCO_SUB_CS_ADD_REAR_FWD if direction_normal
                else LOCO_SUB_CS_ADD_REAR_REV
            )
            role = "REAR"
        else:
            sub = (
                LOCO_SUB_CS_ADD_MID_FWD if direction_normal
                else LOCO_SUB_CS_ADD_MID_REV
            )
            role = f"MID#{position - 1}"
        cmd = nce_loco_cmd(
            member_address,
            long_address=long_address,
            sub_cmd=sub,
            data=consist_address,
        )
        _log.info(
            "NCE consist %d: ADD %s addr=%d%s dir=%s (sub=0x%02X)",
            consist_address, role, member_address,
            "L" if long_address else "S",
            "FWD" if direction_normal else "REV",
            sub,
        )
        await self._tc.send_command(cmd)

    async def _send_remove(
        self, member_address: int, long_address: bool,
    ) -> None:
        cmd = nce_loco_cmd(
            member_address,
            long_address=long_address,
            sub_cmd=LOCO_SUB_CS_REMOVE,
            data=0,
        )
        _log.info(
            "NCE consist: REMOVE addr=%d%s",
            member_address, "L" if long_address else "S",
        )
        await self._tc.send_command(cmd)

    async def _send_kill(
        self, lead_address: int, lead_long_address: bool,
    ) -> None:
        cmd = nce_loco_cmd(
            lead_address,
            long_address=lead_long_address,
            sub_cmd=LOCO_SUB_CS_KILL,
            data=0,
        )
        _log.info(
            "NCE consist: KILL via LEAD addr=%d%s",
            lead_address, "L" if lead_long_address else "S",
        )
        await self._tc.send_command(cmd)

    def _is_existing_member(self, consist: Consist, address: int) -> bool:
        return any(loco.address == address for loco in consist.locos)
