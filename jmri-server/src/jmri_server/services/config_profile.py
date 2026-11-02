"""``ConfigProfileService`` -- CRUD over ``<settings>/profiles.xml``.

Without this, adding a new profile means hand-editing
``profiles.xml`` and hoping the syntax matches JMRI's exact
expectations -- an alpha tester ran into exactly that failure mode.
This service performs the register / unregister mechanically, and
mints an empty ``PanelPro.xml`` for freshly-created profiles so the
Copy-beans-between-profiles flow sees them as valid targets on the
next reload.

Wire shape (rewrite extension -- JMRI Java has a ``configProfile``
topic but with a different mutation contract; we lean on our own
:class:`ProfileRegistry` for simplicity)::

    method=list
        -> array of {id, name, path, isActive}

    method=put, data={"name": "prod"}          # id auto-generated
    method=put, data={"name": "prod", "id": "..."}
        -> the new profile projection

    method=delete, data={"id": "..."}
        -> {"id": ..., "deleted": true}

Delete only unregisters the profile -- the on-disk directory is left
alone. Reason: a user delete of the wrong profile through the UI
would otherwise be non-recoverable. Removing the dir is a manual
follow-up.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from jmri_core.persistence.profile import Profile
from jmri_core.persistence.profile_registry import (
    ProfileRef,
    ProfileRegistry,
    save_profile_registry,
)

from jmri_server.auth.models import Role
from jmri_server.service import JsonService, JsonServiceError

_log = logging.getLogger(__name__)

# Same character class as roster / logixng system names -- filesystem
# safe on Windows (no colon), no path separators.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _project(ref: ProfileRef, registry: ProfileRegistry) -> dict[str, Any]:
    active = registry.active()
    profile = ref.to_profile(registry.settings_root)
    return {
        "id": ref.id,
        "name": ref.name,
        "path": str(profile.profile_dir),
        "isActive": active is not None and active.id == ref.id,
    }


class ConfigProfileService(JsonService):
    topic = "configProfile"
    # Profile CRUD is admin-only -- a compromised operator token
    # shouldn't be able to create/delete profiles or their
    # backing data.
    REQUIRED_ROLE = {
        "list": Role.VIEWER,
        "get": Role.VIEWER,
        "put": Role.ADMIN,
        "delete": Role.ADMIN,
        "post": Role.ADMIN,
    }

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        registry = self._registry_or_none()
        if registry is None:
            return []
        return [_project(ref, registry) for ref in registry.profiles]

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise JsonServiceError(400, "configProfile.put requires 'name'")
        if not _SAFE_NAME_RE.match(name):
            raise JsonServiceError(
                400,
                "profile name must match [A-Za-z0-9._-]+ "
                "(filesystem-safe, no colon or path separators)",
            )
        registry = self._require_registry()
        # Reject duplicates by name -- same-name entries would collide
        # on disk since we store per-name subdirectories.
        for existing in registry.profiles:
            if existing.name == name:
                raise JsonServiceError(
                    409, f"profile with name {name!r} already exists",
                )
        # Auto-generate an id if the caller didn't specify one. JMRI's
        # own convention is ``<name>.<8-hex>``; matching that keeps
        # profiles.xml recognizable by a JMRI Java install if the user
        # ever imports our settings back into it.
        raw_id = data.get("id")
        if isinstance(raw_id, str) and raw_id:
            if registry.by_id(raw_id) is not None:
                raise JsonServiceError(
                    409, f"profile id {raw_id!r} already exists",
                )
            profile_id = raw_id
        else:
            profile_id = f"{name}.{uuid.uuid4().hex[:8]}"
        ref = ProfileRef(
            id=profile_id,
            path=f"settings:{name}/",
            name=name,
        )
        registry.profiles.append(ref)
        # Persist profiles.xml before touching the filesystem so a
        # save-failure doesn't leave orphan directories. Create the
        # profile's dir + an empty PanelPro.xml afterwards.
        save_profile_registry(registry)
        profile = ref.to_profile(registry.settings_root)
        profile.ensure()
        _write_empty_config(profile)
        return _project(ref, registry)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        profile_id = data.get("id")
        if not isinstance(profile_id, str) or not profile_id:
            raise JsonServiceError(400, "configProfile.delete requires 'id'")
        registry = self._require_registry()
        ref = registry.by_id(profile_id)
        if ref is None:
            raise JsonServiceError(
                404, f"no profile with id {profile_id!r}",
            )
        active = registry.active()
        if active is not None and active.id == profile_id:
            raise JsonServiceError(
                409,
                "can't delete the active profile; switch to another profile first",
            )
        registry.profiles = [p for p in registry.profiles if p.id != profile_id]
        save_profile_registry(registry)
        return {"id": profile_id, "deleted": True}

    # ---- helpers ----------------------------------------------------

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


def _write_empty_config(profile: Profile) -> None:
    """Mint a minimal ``PanelPro.xml`` so the new profile is a valid
    target for the beanTransfer service on its first day of life.
    Uses the same shape :func:`~jmri_core.persistence.empty_config`
    produces so JMRI Java would accept it if imported back."""
    from jmri_core.persistence import empty_config, save_config  # noqa: PLC0415

    cfg = empty_config(doctype='layout-config SYSTEM "layout-config.dtd"')
    save_config(profile.config_file, cfg)
