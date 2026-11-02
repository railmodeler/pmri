"""``ProfileRegistry`` -- parse JMRI's ``profiles.xml``.

Real JMRI installs keep one or more profile directories (DRGW.jmri,
HomeYard.jmri, etc.) under the settings root, with ``profiles.xml``
at the root listing them. Example::

    <profileConfig>
      <profiles>
        <profile id="DRGW.3f28eb5e" path="settings:DRGW.jmri/" />
      </profiles>
      <searchPaths>
        <path path="settings:" default="true" />
      </searchPaths>
    </profileConfig>

``path`` values use a ``settings:`` URI prefix that means "relative to
the settings dir". Some installs also store the *active* profile
indirectly via a property file; we keep our own marker
(``<settings>/.active_profile``) so the server can flip profiles
without touching JMRI's bookkeeping.

The registry is purely a data-layer abstraction -- no I/O against the
hardware connection. Higher layers (jmri-server) handle the runtime
switch (clearing bean managers, etc.).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from lxml import etree

from jmri_core.persistence.profile import Profile, default_settings_dir

PROFILES_XML = "profiles.xml"
ACTIVE_MARKER = ".active_profile"


@dataclass(slots=True)
class ProfileRef:
    """One ``<profile>`` entry from ``profiles.xml``."""

    id: str
    path: str   # raw ``path`` attribute, possibly ``settings:Foo.jmri/``
    name: str   # derived display name -- directory name stripped of suffix

    def to_profile(self, settings_root: pathlib.Path) -> Profile:
        """Resolve to a runtime :class:`Profile` rooted at *settings_root*."""
        dir_name = _strip_settings_prefix(self.path).rstrip("/").split("/")[-1]
        if not dir_name:
            raise ValueError(f"profile {self.id!r} has empty resolved path")
        return Profile(root=settings_root, name=dir_name)


@dataclass
class ProfileRegistry:
    """In-memory view of ``<settings>/profiles.xml`` and the active profile."""

    settings_root: pathlib.Path
    profiles: list[ProfileRef]

    # ----- accessors -------------------------------------------------------

    def by_id(self, profile_id: str) -> ProfileRef | None:
        for p in self.profiles:
            if p.id == profile_id:
                return p
        return None

    def by_name(self, name: str) -> ProfileRef | None:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def active(self) -> ProfileRef | None:
        """Return the currently-active profile, or ``None`` if unresolved.

        Resolution order:
        1. ``<settings>/.active_profile`` marker file (our own bookkeeping).
        2. First profile in ``profiles.xml`` (JMRI's implicit default).
        """
        marker = self.settings_root / ACTIVE_MARKER
        if marker.exists():
            stored = marker.read_text(encoding="utf-8").strip()
            hit = self.by_id(stored) or self.by_name(stored)
            if hit:
                return hit
        return self.profiles[0] if self.profiles else None

    def set_active(self, profile_id: str) -> None:
        """Write the ``.active_profile`` marker. Raises if id is unknown."""
        if self.by_id(profile_id) is None:
            raise KeyError(f"no profile with id {profile_id!r}")
        marker = self.settings_root / ACTIVE_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(profile_id, encoding="utf-8")

    def active_profile(self) -> Profile | None:
        """Convenience: resolved :class:`Profile` for the active entry."""
        ref = self.active()
        return ref.to_profile(self.settings_root) if ref else None


# ----- parse ----------------------------------------------------------------


def _strip_settings_prefix(path: str) -> str:
    """``settings:DRGW.jmri/`` -> ``DRGW.jmri/``."""
    return path[len("settings:"):] if path.startswith("settings:") else path


def _name_from_path(path: str) -> str:
    """Display name = last path segment with trailing ``/`` stripped."""
    stripped = _strip_settings_prefix(path).rstrip("/")
    return stripped.split("/")[-1] if stripped else ""


def _parse_profile_elem(elem: etree._Element) -> ProfileRef | None:
    pid = elem.get("id") or ""
    path = elem.get("path") or ""
    if not pid or not path:
        return None
    name = _name_from_path(path)
    return ProfileRef(id=pid, path=path, name=name or pid)


def load_profile_registry(settings_root: pathlib.Path | None = None) -> ProfileRegistry:
    """Load ``profiles.xml`` from *settings_root* (default: platform default)."""
    if settings_root is None:
        settings_root = default_settings_dir()
    path = settings_root / PROFILES_XML
    if not path.exists():
        return ProfileRegistry(settings_root=settings_root, profiles=[])
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    root = etree.fromstring(path.read_bytes(), parser=parser)
    if root.tag != "profileConfig":
        raise ValueError(
            f"expected <profileConfig> root in {path}, got <{root.tag}>"
        )
    profiles_elem = root.find("profiles")
    profiles: list[ProfileRef] = []
    if profiles_elem is not None:
        for child in profiles_elem.findall("profile"):
            ref = _parse_profile_elem(child)
            if ref is not None:
                profiles.append(ref)
    return ProfileRegistry(settings_root=settings_root, profiles=profiles)


def save_profile_registry(registry: ProfileRegistry) -> None:
    """Write the registry back to ``<settings>/profiles.xml``.

    Preserves a simple shape -- we don't model ``<searchPaths>`` because
    we never need to vary it. If a user's existing ``profiles.xml`` has
    one, it gets dropped on save; this is acceptable for our alpha
    since we don't create new profiles from the UI yet.
    """
    root = etree.Element("profileConfig")
    profiles_elem = etree.SubElement(root, "profiles")
    for ref in registry.profiles:
        etree.SubElement(profiles_elem, "profile", attrib={
            "id": ref.id,
            "path": ref.path,
        })
    search = etree.SubElement(root, "searchPaths")
    etree.SubElement(search, "path", attrib={
        "path": "settings:",
        "default": "true",
    })
    out = etree.tostring(
        root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
        standalone=False,
    )
    registry.settings_root.mkdir(parents=True, exist_ok=True)
    (registry.settings_root / PROFILES_XML).write_bytes(out)
