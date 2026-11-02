"""Active-profile discovery for the PMRI launcher.

Replaces the Chunk-B naive ``Profile(root=…, name=pmri_cfg.active_profile)``
with the real JMRI-shaped chain the Case-1 operator expects:

1. ``pmri.xml`` ``<active-profile name="…"/>`` **when non-empty** --
   explicit operator override.
2. ``<settings>/.active_profile`` marker -- JMRI convention. A
   Case-1 user (existing JMRI install) picks up their profile via
   this step without PMRI touching anything.
3. If profiles.xml has exactly one entry, that entry wins (and the
   choice is recorded to ``.active_profile`` so step 2 catches
   next launch).
4. If profiles.xml has multiple entries and nothing above resolved,
   prompt the operator. The pick is recorded to ``.active_profile``.
5. Final fallback: ``Profile(root=<settings>, name="default")`` so a
   fresh install still boots.

The resolver is deliberately kept out of :mod:`jmri_server.app` --
that module's :func:`_resolve_initial_profile` only runs steps 1-2
(it can't prompt). The launcher invokes this discovery flow BEFORE
constructing the server so by the time ``create_app`` runs the
``.active_profile`` marker is populated and the server's resolver
yields the same answer.
"""
from __future__ import annotations

import enum
import logging
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jmri_core.persistence import (
    Profile,
    load_profile_registry,
)
from jmri_core.persistence.profile_registry import (
    ACTIVE_MARKER,
    ProfileRegistry,
)

if TYPE_CHECKING:
    from jmri_core.persistence import PmriConfig, ProfileRef

_log = logging.getLogger(__name__)


class ProfileSource(enum.Enum):
    """Where the discovery flow found the active profile.

    Used by the launcher to decide whether to write back to
    ``.active_profile``: auto-pick / prompt both write, explicit
    sources (pmri.xml, existing marker) do not (nothing to update)."""

    PMRI_CONFIG = "pmri.xml"
    MARKER = ".active_profile"
    SINGLE_PROFILE = "profiles.xml single"
    PICKED = "picker"
    FALLBACK = "fallback"


@dataclass
class ProfileDiscovery:
    """Result of :func:`discover_active_profile`.

    ``profile`` is the picked :class:`Profile`. ``source`` tells the
    caller which rule fired; the launcher logs it on each start so
    an operator debugging a wrong-profile boot sees "found via
    .active_profile marker" vs "picked via prompt" at a glance.

    ``should_persist`` is True iff the launcher should rewrite
    ``.active_profile`` with the pick. That's the case when
    discovery AUTO-chose (single-profile or picker) and nothing
    pre-existing recorded the choice. pmri.xml override and existing
    marker both have ``should_persist=False`` -- those paths need no
    update.
    """

    profile: Profile
    source: ProfileSource
    should_persist: bool


def discover_active_profile(  # noqa: PLR0911 -- the chain IS a return ladder
    settings_root: pathlib.Path,
    pmri_cfg: PmriConfig,
    *,
    pick: Callable[[list[ProfileRef]], ProfileRef | None] | None = None,
) -> ProfileDiscovery | None:
    """Walk the five-step resolution chain described in the module docstring.

    *pick* is the ambiguity-breaker for step 4 (multiple profiles,
    no marker, no pmri.xml override). It receives the list of
    ``ProfileRef`` entries from profiles.xml and returns the picked
    one, or None to abort. The default pick strategy depends on the
    launcher mode -- the caller injects a CLI picker in headless
    mode and a Tk dialog in GUI mode (see :mod:`jmri_pmri.picker`).

    Returns ``None`` when step 4 was reached and the operator
    cancelled the prompt -- the launcher aborts rather than silently
    falling through to the "default" fallback (which would open the
    wrong profile without the operator knowing).
    """
    registry = _load_registry(settings_root)

    # Step 1: pmri.xml override (non-empty).
    if pmri_cfg.active_profile:
        hit = _resolve_name(registry, pmri_cfg.active_profile)
        if hit is not None:
            return ProfileDiscovery(hit, ProfileSource.PMRI_CONFIG, should_persist=False)
        _log.warning(
            "pmri.xml active-profile %r doesn't match any profile; "
            "falling through to the marker/registry chain",
            pmri_cfg.active_profile,
        )

    # Step 2: .active_profile marker.
    marker_hit = _resolve_from_marker(settings_root, registry)
    if marker_hit is not None:
        return ProfileDiscovery(marker_hit, ProfileSource.MARKER, should_persist=False)

    # Step 3: single-entry profiles.xml -> auto-pick.
    if len(registry.profiles) == 1:
        ref = registry.profiles[0]
        _persist_marker(settings_root, ref.id)
        return ProfileDiscovery(
            ref.to_profile(settings_root),
            ProfileSource.SINGLE_PROFILE,
            should_persist=True,
        )

    # Step 4: multiple profiles, no override, no marker -> prompt.
    if len(registry.profiles) > 1:
        if pick is None:
            _log.error(
                "profiles.xml has %d entries and none selected; a "
                "picker was not supplied -- aborting launcher",
                len(registry.profiles),
            )
            return None
        picked = pick(registry.profiles)
        if picked is None:
            _log.info("profile picker was cancelled; aborting launcher")
            return None
        _persist_marker(settings_root, picked.id)
        return ProfileDiscovery(
            picked.to_profile(settings_root),
            ProfileSource.PICKED,
            should_persist=True,
        )

    # Step 5: fallback -- no profiles.xml / empty, no marker, no override.
    # Fresh install. Create a "default" profile on first save.
    _log.info(
        "no profiles.xml entries at %s; using the \"default\" fallback",
        settings_root,
    )
    return ProfileDiscovery(
        Profile(root=settings_root, name="default"),
        ProfileSource.FALLBACK,
        should_persist=False,
    )


# ---- internals -----------------------------------------------------------


def _load_registry(settings_root: pathlib.Path) -> ProfileRegistry:
    """Load profiles.xml with the same error tolerance the server
    uses: a malformed or missing file yields an empty registry."""
    try:
        return load_profile_registry(settings_root)
    except (OSError, ValueError) as e:
        _log.warning("could not load profiles.xml at %s: %s", settings_root, e)
        return ProfileRegistry(settings_root=settings_root, profiles=[])


def _resolve_name(
    registry: ProfileRegistry, name: str,
) -> Profile | None:
    """Resolve *name* to a :class:`Profile`: match registry by name
    or id first, otherwise accept a bare on-disk directory (JMRI's
    tolerance for operators who drop a dir in without updating
    profiles.xml)."""
    hit = registry.by_name(name) or registry.by_id(name)
    if hit is not None:
        return hit.to_profile(registry.settings_root)
    candidate = Profile(root=registry.settings_root, name=name)
    if candidate.profile_dir.is_dir():
        return candidate
    return None


def _resolve_from_marker(
    settings_root: pathlib.Path, registry: ProfileRegistry,
) -> Profile | None:
    """Read ``<settings>/.active_profile`` and resolve its content.

    JMRI stores a profile id (or occasionally a name) in the marker;
    we accept either. Returns None when the marker doesn't exist
    OR points at nothing resolvable -- discovery's step 3 handles
    the "no marker, so auto-pick the single entry" case; we don't
    pre-empt it here by returning the registry's own fallback.
    """
    marker = settings_root / ACTIVE_MARKER
    if not marker.exists():
        return None
    stored = marker.read_text(encoding="utf-8").strip()
    if not stored:
        return None
    hit = _resolve_name(registry, stored)
    if hit is None:
        _log.warning(
            ".active_profile points at %r but no matching profile "
            "exists; falling through", stored,
        )
    return hit


def _persist_marker(settings_root: pathlib.Path, profile_id: str) -> None:
    """Write the resolved profile id to ``<settings>/.active_profile``.

    This is the JMRI convention; the Chunk-C server-side code reads
    the same file. Writing here means the next launch picks up the
    same profile without prompting. OSErrors (read-only filesystem,
    permissions) are logged + swallowed -- the launcher continues
    with the in-memory pick."""
    marker = settings_root / ACTIVE_MARKER
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(profile_id, encoding="utf-8")
    except OSError as e:
        _log.warning("could not persist .active_profile marker: %s", e)
