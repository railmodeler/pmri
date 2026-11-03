"""``ConsistService`` -- JSON wire interface for the consist manager.

Wire shape (a pmri-server-specific extension; stock JMRI Java's JSON
API exposes ``consist`` too, but with a different envelope -- this
service uses the same envelope style as ``throttle`` and ``scripts``
so all rewrite-specific services are consistent).

Methods:

* ``list`` -> array of projected consist dicts.
* ``get``, ``data={"address": N}`` -> one projected dict.
* ``put``, ``data={"address": N, "long_address": bool, "kind":
  "software"|"command_station"|"advanced", "comment": "..."}`` ->
  create-if-absent and return the projected dict. Idempotent.
* ``post``, ``data={"address": N, "action": ..., ...}`` -- mutate the
  consist. Verbs:
    - ``add_member`` -- ``member`` = ``{address, long_address,
      direction_normal}``.
    - ``remove_member`` -- ``member_address`` = int.
    - ``set_speed`` -- ``speed`` = float in [-1.0, 1.0].
    - ``set_direction`` -- ``direction`` = ``"forward"``/``"reverse"``.
    - ``set_function`` -- ``function`` = int 0..28, ``on`` = bool.
    - ``emergency_stop`` -- no extra fields.
* ``delete``, ``data={"address": N}`` -> drop the consist.

Projected consist shape::

    {
      "address": 3601,
      "long_address": true,
      "kind": "command_station",
      "comment": "Baldwin twins",
      "locos": [
        {"address": 3601, "long_address": true,
         "direction_normal": true, "position": 0},
        {"address": 3602, "long_address": true,
         "direction_normal": false, "position": 1}
      ]
    }
"""
from __future__ import annotations

from typing import Any

from pmri_core.beans.consist import CONSIST_ADDRESS_MAX, Consist
from pmri_core.beans.consist_manager import ConsistManager
from pmri_core.state import ThrottleDirection

from pmri_server.service import JsonService, JsonServiceError


def _project(c: Consist, mgr: ConsistManager | None = None) -> dict[str, Any]:
    """Project a consist to its wire form.

    ``mgr`` is optional so callers that only have a :class:`Consist` in
    hand (mostly tests and older code paths) can still project -- the
    speed/direction fields fall back to defaults in that case. The
    push wiring and the JSON service pass the manager so the projection
    carries the runtime drive state the UI needs to render the slider
    at the right position and highlight the active direction button.
    """
    projection: dict[str, Any] = {
        "address": c.address,
        "kind": c.kind,
        "comment": c.comment,
        "locos": [
            {
                "address": m.address,
                "long_address": m.long_address,
                "direction_normal": m.direction_normal,
                "position": m.position,
            }
            for m in c.locos
        ],
    }
    if mgr is not None:
        projection["speed"] = mgr.current_speed(c.address)
        projection["direction"] = (
            "forward"
            if mgr.current_direction(c.address) is ThrottleDirection.FORWARD
            else "reverse"
        )
    else:
        projection["speed"] = 0.0
        projection["direction"] = "forward"
    return projection


def _address_from(data: dict[str, Any]) -> int:
    """Extract the consist address from a request. Raises 400 on missing."""
    addr = data.get("address")
    if not isinstance(addr, int):
        raise JsonServiceError(400, "consist request requires int 'address'")
    return addr


class ConsistService(JsonService):
    topic = "consist"

    @property
    def _consists(self) -> ConsistManager:
        return self.ctx.connection.consists

    def _require(self, address: int) -> Consist:
        c = self._consists.get(address)
        if c is None:
            raise JsonServiceError(404, f"no consist at address {address}")
        return c

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        return [_project(c, self._consists) for c in self._consists]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        addr = _address_from(data or {})
        return _project(self._require(addr), self._consists)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        """Create-if-absent. Idempotent: returns the existing consist if
        one already lives at ``address`` without overwriting its
        members. Change comment/kind on an existing consist via a
        follow-up ``post`` (not currently wired -- the two fields are
        immutable after create; a delete+recreate is the escape hatch).

        Enforces two rules that were left implicit before:

        * Consist address must be 1..127 (short only). CV19 is 7-bit,
          so anything else is either meaningless (advanced) or
          gratuitously inconsistent across kinds.
        * Address must not collide with any existing roster loco. If
          it did, the roster loco would silently participate in every
          fan-out addressed to that consist -- a footgun.
        """
        data = data or {}
        addr = _address_from(data)
        if not 1 <= addr <= CONSIST_ADDRESS_MAX:
            raise JsonServiceError(
                400,
                f"consist address must be 1..{CONSIST_ADDRESS_MAX} short, "
                f"got {addr}",
            )
        kind = data.get("kind")
        comment = str(data.get("comment", ""))
        # Validate kind against the model's Literal -- raise 400 on typo.
        if kind is not None and kind not in ("software", "command_station", "advanced"):
            raise JsonServiceError(400, f"invalid consist kind: {kind!r}")
        # Roster-collision check. Skip if the consist already exists
        # at this address (idempotent create still wins).
        if self._consists.get(addr) is None:
            conflict = self._roster_conflict(addr)
            if conflict is not None:
                raise JsonServiceError(
                    409,
                    f"consist address {addr} collides with roster loco "
                    f"{conflict!r} (short {addr}); pick a different "
                    f"consist address or remove/renumber the loco.",
                )
        consist = await self._consists.create(addr, kind=kind, comment=comment)
        return _project(consist, self._consists)

    def _roster_conflict(self, addr: int) -> str | None:
        """Return the id of any roster entry using *addr* as a short
        DCC address, or ``None`` if there's no collision.

        Only short addresses count -- a long-address loco at the same
        numeric value shares no wire space with a 1..127 consist. The
        roster store is lazy-loaded here on first use (same pattern
        as :class:`RosterService`) so the check works even when the
        user's first action is Create-consist and no roster/throttle
        page has been hit yet -- that was a real bug where the check
        silently short-circuited when navigation started on
        ``/consists``.
        """
        store = self._roster_store()
        if store is None:
            return None
        target = str(addr)
        for entry in store:
            if entry.long_address:
                continue
            if entry.dcc_address == target:
                return entry.id
        return None

    def _roster_store(self) -> Any:
        """Return the shared :class:`RosterStore`, constructing it on
        first access if nothing else has touched it yet.

        Uses the same context attribute (``_roster_store``) as
        :class:`RosterService` so both see the same instance -- no
        double-load, and edits made through /roster show up in this
        check immediately.
        """
        store = getattr(self.ctx, "_roster_store", None)
        if store is not None:
            return store
        try:
            from pmri_core.persistence.profile import Profile  # noqa: PLC0415
            from pmri_server.roster_store import RosterStore  # noqa: PLC0415
        except ImportError:
            return None
        profile = getattr(self.ctx, "_roster_profile", None) or Profile.default()
        try:
            store = RosterStore(profile=profile)
        except Exception:  # noqa: BLE001
            # No profile on disk, permission error, corrupt index -- the
            # conflict check is a courtesy, not a hard invariant. Bail
            # rather than fail create.
            return None
        self.ctx._roster_store = store  # type: ignore[attr-defined]
        return store

    async def on_post(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any] | list[int]:
        data = data or {}
        action = data.get("action")
        # ``refresh_from_cs`` is manager-scoped -- it walks the CS's
        # consist table and reconciles every in-memory consist with
        # what the hardware reports. No consist address needed. Only
        # supported by drivers that implement it (NCE today).
        if action == "refresh_from_cs":
            return await self._refresh_from_cs(data)
        addr = _address_from(data)
        # Every remaining action targets an existing consist.
        self._require(addr)
        if action == "add_member":
            m = data.get("member") or {}
            member_addr = m.get("address")
            if not isinstance(member_addr, int):
                raise JsonServiceError(
                    400, "consist.add_member requires 'member.address'",
                )
            await self._consists.add_member(
                addr, member_addr,
                long_address=bool(m.get("long_address", False)),
                direction_normal=bool(m.get("direction_normal", True)),
            )
        elif action == "remove_member":
            m_addr = data.get("member_address")
            if not isinstance(m_addr, int):
                raise JsonServiceError(
                    400, "consist.remove_member requires int 'member_address'",
                )
            if not await self._consists.remove_member(addr, m_addr):
                raise JsonServiceError(
                    404, f"consist {addr} has no member at {m_addr}",
                )
        elif action == "set_speed":
            speed = data.get("speed")
            if not isinstance(speed, (int, float)):
                raise JsonServiceError(400, "consist.set_speed requires 'speed'")
            await self._consists.set_speed(addr, float(speed))
        elif action == "set_direction":
            direction = data.get("direction")
            if direction not in ("forward", "reverse"):
                raise JsonServiceError(
                    400, "consist.set_direction requires 'forward'|'reverse'",
                )
            await self._consists.set_direction(
                addr,
                ThrottleDirection.FORWARD if direction == "forward"
                else ThrottleDirection.REVERSE,
            )
        elif action == "set_function":
            fn = data.get("function")
            on = data.get("on")
            if not isinstance(fn, int) or not isinstance(on, bool):
                raise JsonServiceError(
                    400, "consist.set_function requires int 'function' + bool 'on'",
                )
            await self._consists.set_function(addr, fn, on)
        elif action == "emergency_stop":
            await self._consists.emergency_stop(addr)
        else:
            raise JsonServiceError(400, f"unknown consist action: {action!r}")
        return _project(self._require(addr), self._consists)

    async def _refresh_from_cs(
        self, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Trigger a memory-mapped consist read-back on the driver.

        Optional fields ``min_consist`` / ``max_consist`` bound the
        scan range and are forwarded verbatim when present, so
        operators can spot-check one slot instead of the full 127.

        Returns::

            {"changed": [<consist_addr>, ...],
             "consists": [<projected consist>, ...]}

        so a caller can log which consist numbers were touched **and**
        get the fresh full snapshot without a follow-up ``list`` call.
        """
        refresh = getattr(self._consists, "refresh_from_cs", None)
        if refresh is None:
            raise JsonServiceError(
                400,
                "connection does not support consist refresh_from_cs "
                "(requires NCE with memory-mapped consist table)",
            )
        kwargs: dict[str, int] = {}
        for field in ("min_consist", "max_consist"):
            if field in data:
                value = data[field]
                if not isinstance(value, int):
                    raise JsonServiceError(
                        400, f"consist.refresh_from_cs {field!r} must be int",
                    )
                kwargs[field] = value
        changed = await refresh(**kwargs)
        return {
            "changed": list(changed),
            "consists": [_project(c, self._consists) for c in self._consists],
        }

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        addr = _address_from(data)
        if not await self._consists.delete(addr):
            raise JsonServiceError(404, f"no consist at address {addr}")
        return {"address": addr, "deleted": True}
