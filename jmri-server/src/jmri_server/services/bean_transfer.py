"""``BeanTransferService`` -- copy bean definitions between profiles.

The user story: "I set up a staging layout on my dev profile; I want
that bean set on production too." Without this service, the operator
has to shell in and copy ``PanelPro.xml`` by hand, plus edit out the
beans they DON'T want to overwrite.

Wire shape (a rewrite-specific extension -- JMRI Java doesn't have a
``beanTransfer`` topic)::

    method=list
        -> {"profiles": [
             {"id": ..., "name": ..., "path": ..., "isActive": bool,
              "counts": {"turnout": N, "sensor": N, "light": N}},
           ], "types": ["turnout", "sensor", "light"]}

    method=post, data={"action": "preview", "from": <profile_id>,
                       "to": <profile_id>, "types": ["turnout", ...]}
        -> {"counts": {"turnout": N, "sensor": N, "light": N},
           "willOverwrite": {"turnout": N, ...}}

    method=post, data={"action": "apply", "from": <profile_id>,
                       "to": <profile_id>, "types": [...]}
        -> {"copied": {"turnout": N, "sensor": N, "light": N},
           "targetIsActive": bool}

An apply against the active profile also re-populates the live bean
managers (so a Copy to production is visible immediately, not just
after a profile switch).
"""
from __future__ import annotations

import logging
from typing import Any

from jmri_core.persistence.profile import Profile
from jmri_core.persistence.profile_registry import ProfileRef, ProfileRegistry

from jmri_server.config_store import ConfigStore
from jmri_server.auth.models import Role
from jmri_server.roster_store import RosterStore
from jmri_server.service import JsonService, JsonServiceError

_log = logging.getLogger(__name__)

# Every kind the operator can pick in the transfer UI. Ordering here is
# the ordering the UI renders; keep the bean-XML kinds (backed by the
# ConfigStore / PanelPro.xml) first, then the roster kind (backed by
# a separate on-disk layout under ``<profile>/roster/``).
SUPPORTED_TYPES = ("turnout", "sensor", "light", "roster")

# Kinds backed by ConfigStore / PanelPro.xml. Roster is deliberately
# separate: RosterEntry lives in ``<profile>/roster/<id>.xml`` +
# ``<profile>/roster.xml``, not in the panel config.
_CONFIG_STORE_KINDS = ("turnout", "sensor", "light")


def _profile_from_ref(registry: ProfileRegistry, ref: ProfileRef) -> Profile:
    return ref.to_profile(registry.settings_root)


def _open_store(profile: Profile) -> ConfigStore:
    """Read the profile's ``PanelPro.xml`` into a fresh
    :class:`ConfigStore`. Missing / never-saved files parse as an
    empty config -- the caller gets empty iterators for each bean
    type without raising."""
    return ConfigStore(profile=profile)


def _count(store: ConfigStore, kind: str) -> int:
    if kind == "turnout":
        return sum(1 for _ in store.turnouts())
    if kind == "sensor":
        return sum(1 for _ in store.sensors())
    if kind == "light":
        return sum(1 for _ in store.lights())
    return 0


def _existing_names(store: ConfigStore, kind: str) -> set[str]:
    """System names already present in *store* for the given bean
    kind. Used to compute how many of a would-be transfer would
    OVERWRITE vs. add fresh, so preview can warn honestly."""
    if kind == "turnout":
        return {r.system_name for r in store.turnouts()}
    if kind == "sensor":
        return {r.system_name for r in store.sensors()}
    if kind == "light":
        return {r.system_name for r in store.lights()}
    return set()


def _copy_kind(
    src: ConfigStore, dst: ConfigStore, kind: str,
) -> int:
    """Upsert every row of ``kind`` from *src* into *dst*. Returns
    the number of rows copied. Auto-preserves *dst*'s extra_attribs
    for rows that already exist there -- the same logic ``upsert_*``
    uses on the bean-service edit path."""
    n = 0
    if kind == "turnout":
        for row in src.turnouts():
            dst.upsert_turnout(row)
            n += 1
    elif kind == "sensor":
        for row in src.sensors():
            dst.upsert_sensor(row)
            n += 1
    elif kind == "light":
        for row in src.lights():
            dst.upsert_light(row)
            n += 1
    return n


# ---- roster helpers -----------------------------------------------------
#
# Roster lives in RosterStore, not ConfigStore. Kept separate from the
# ConfigStore helpers above because the on-disk layout differs (one
# XML file per entry under ``<profile>/roster/`` plus the ``roster.xml``
# index) and the "system name" analogue is ``entry.id``.


def _open_roster(profile: Profile) -> RosterStore:
    return RosterStore(profile=profile)


def _roster_count(store: RosterStore) -> int:
    return len(store)


def _roster_ids(store: RosterStore) -> set[str]:
    return {entry.id for entry in store}


def _copy_roster(src: RosterStore, dst: RosterStore) -> int:
    """Upsert every roster entry from *src* into *dst*. Returns the
    number of entries copied. ``file_name`` is cleared on the copy so
    the destination re-derives it from its own filename policy -- a
    stale source-profile filename would otherwise fight the destination
    store's ``_safe_filename`` and produce orphan files."""
    n = 0
    for entry in src:
        # Copy the model but let the destination store name the file.
        clone = entry.model_copy(update={"file_name": ""})
        dst.put(clone)
        n += 1
    return n


class BeanTransferService(JsonService):
    """Cross-profile bean transfer. Read-only ``list`` +
    ``post`` with ``action=preview|apply``."""

    topic = "beanTransfer"
    # ``list`` is safe for any role. ``post`` writes across
    # profiles -- admin-only, since a compromised operator token
    # shouldn't be able to leak or overwrite the production
    # profile's beans from a staging session.
    REQUIRED_ROLE = {
        "list": Role.VIEWER,
        "post": Role.ADMIN,
    }

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        registry = self._registry_or_none()
        profiles_out: list[dict[str, Any]] = []
        if registry is not None:
            active = registry.active()
            active_id = active.id if active else None
            for ref in registry.profiles:
                profile = _profile_from_ref(registry, ref)
                store = _open_store(profile)
                counts: dict[str, int] = {
                    kind: _count(store, kind)
                    for kind in _CONFIG_STORE_KINDS
                }
                # Roster count uses a separate on-disk layout; open the
                # RosterStore just to enumerate. Cheap: it only reads
                # each ``<profile>/roster/*.xml`` on construction.
                try:
                    counts["roster"] = _roster_count(_open_roster(profile))
                except Exception:  # noqa: BLE001
                    counts["roster"] = 0
                profiles_out.append({
                    "id": ref.id,
                    "name": ref.name,
                    "path": str(profile.profile_dir),
                    "isActive": ref.id == active_id,
                    "counts": counts,
                })
        return {
            "profiles": profiles_out,
            "types": list(SUPPORTED_TYPES),
        }

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        action = data.get("action")
        if action == "preview":
            return await self._preview(data)
        if action == "apply":
            return await self._apply(data)
        raise JsonServiceError(
            400, f"unknown beanTransfer action: {action!r}",
        )

    # ---- helpers -----------------------------------------------------

    def _registry_or_none(self) -> ProfileRegistry | None:
        registry = getattr(self.ctx, "profile_registry", None)
        return registry if isinstance(registry, ProfileRegistry) else None

    def _require_registry(self) -> ProfileRegistry:
        registry = self._registry_or_none()
        if registry is None:
            raise JsonServiceError(
                503,
                "no profile registry loaded (no profiles.xml at settings root)",
            )
        return registry

    def _resolve_pair(
        self, data: dict[str, Any],
    ) -> tuple[Profile, Profile, ProfileRef, ProfileRef, list[str]]:
        registry = self._require_registry()
        src_id = data.get("from")
        dst_id = data.get("to")
        if not isinstance(src_id, str) or not isinstance(dst_id, str):
            raise JsonServiceError(
                400, "beanTransfer requires 'from' and 'to' profile ids",
            )
        if src_id == dst_id:
            raise JsonServiceError(
                400, "beanTransfer 'from' and 'to' must differ",
            )
        src_ref = registry.by_id(src_id)
        if src_ref is None:
            raise JsonServiceError(
                404, f"no source profile with id {src_id!r}",
            )
        dst_ref = registry.by_id(dst_id)
        if dst_ref is None:
            raise JsonServiceError(
                404, f"no target profile with id {dst_id!r}",
            )
        raw_types = data.get("types")
        if raw_types is None:
            types = list(SUPPORTED_TYPES)
        elif isinstance(raw_types, list):
            types = [t for t in raw_types if isinstance(t, str)]
        else:
            raise JsonServiceError(
                400, "'types' must be an array of bean-kind strings",
            )
        unknown = [t for t in types if t not in SUPPORTED_TYPES]
        if unknown:
            raise JsonServiceError(
                400,
                f"unsupported bean types: {unknown!r}. "
                f"Supported: {list(SUPPORTED_TYPES)}",
            )
        if not types:
            raise JsonServiceError(
                400, "'types' must not be empty",
            )
        return (
            _profile_from_ref(registry, src_ref),
            _profile_from_ref(registry, dst_ref),
            src_ref,
            dst_ref,
            types,
        )

    async def _preview(self, data: dict[str, Any]) -> dict[str, Any]:
        src, dst, _sref, _dref, types = self._resolve_pair(data)
        src_store = _open_store(src)
        dst_store = _open_store(dst)
        counts: dict[str, int] = {}
        will_overwrite: dict[str, int] = {}
        for kind in types:
            if kind == "roster":
                src_roster = _open_roster(src)
                dst_roster = _open_roster(dst)
                src_names = _roster_ids(src_roster)
                counts[kind] = len(src_names)
                will_overwrite[kind] = len(
                    src_names & _roster_ids(dst_roster)
                )
            else:
                src_names = _existing_names(src_store, kind)
                counts[kind] = len(src_names)
                will_overwrite[kind] = len(
                    src_names & _existing_names(dst_store, kind)
                )
        return {"counts": counts, "willOverwrite": will_overwrite}

    async def _apply(self, data: dict[str, Any]) -> dict[str, Any]:
        registry = self._require_registry()
        src, dst, _sref, dst_ref, types = self._resolve_pair(data)
        src_store = _open_store(src)
        active = registry.active()
        target_is_active = active is not None and dst_ref.id == active.id

        # If the target is the active profile, write through the
        # ctx.config_store the live server holds -- otherwise a fresh
        # instance we throw away, since no in-memory managers are
        # attached to the target's beans.
        if target_is_active and isinstance(self.ctx.config_store, ConfigStore):
            dst_store = self.ctx.config_store
        else:
            dst_store = _open_store(dst)

        copied: dict[str, int] = {}
        for kind in types:
            if kind == "roster":
                # For the active target, write through the live
                # RosterStore on ctx so WiThrottle's rebroadcast
                # (subscribed on the same instance) fires per entry
                # and Engine Driver sees the new roster without a
                # reconnect. For inactive targets, use a fresh store
                # tied to the destination profile directory; no live
                # subscribers, but the on-disk files land.
                src_roster = _open_roster(src)
                if target_is_active:
                    dst_roster = getattr(self.ctx, "_roster_store", None)
                    if dst_roster is None:
                        dst_roster = _open_roster(dst)
                        self.ctx._roster_store = dst_roster  # type: ignore[attr-defined]
                        attach = getattr(
                            self.ctx, "_attach_roster_signals", None,
                        )
                        if callable(attach):
                            try:
                                attach()
                            except Exception:  # noqa: BLE001
                                pass
                else:
                    dst_roster = _open_roster(dst)
                copied[kind] = _copy_roster(src_roster, dst_roster)
            else:
                copied[kind] = _copy_kind(src_store, dst_store, kind)

        # Re-populate live managers when we've just written into the
        # active profile. Otherwise the caller would have to switch
        # profile (or restart) to see the new beans in the running
        # connection.
        if target_is_active:
            from jmri_server.app import _restore_beans_from_config  # noqa: PLC0415
            await _restore_beans_from_config(
                self.ctx.connection, dst_store,
            )

        return {"copied": copied, "targetIsActive": target_is_active}
