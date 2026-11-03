"""Tests for the ``ProfileRegistry`` parser + active-profile bookkeeping."""
from __future__ import annotations

import pathlib

import pytest

from pmri_core.persistence import (
    Profile,
    ProfileRef,
    ProfileRegistry,
    load_profile_registry,
    save_profile_registry,
)
from pmri_core.persistence.profile_registry import ACTIVE_MARKER

_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<profileConfig>
  <profiles>
    <profile id="DRGW.3f28eb5e" path="settings:DRGW.jmri/" />
    <profile id="N-Scale.abc1234" path="settings:N-Scale.jmri/" />
  </profiles>
  <searchPaths>
    <path path="settings:" default="true" />
  </searchPaths>
</profileConfig>"""


def _seed(tmp_path: pathlib.Path, body: bytes = _SAMPLE) -> pathlib.Path:
    (tmp_path / "profiles.xml").write_bytes(body)
    return tmp_path


# ----- parse ---------------------------------------------------------------


def test_loads_profiles_from_real_pmri_shape(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    reg = load_profile_registry(settings)
    assert len(reg.profiles) == 2
    by_id = {p.id: p for p in reg.profiles}
    assert "DRGW.3f28eb5e" in by_id
    assert by_id["DRGW.3f28eb5e"].name == "DRGW.jmri"
    assert by_id["DRGW.3f28eb5e"].path == "settings:DRGW.jmri/"


def test_missing_profiles_xml_returns_empty_registry(tmp_path: pathlib.Path) -> None:
    reg = load_profile_registry(tmp_path)
    assert reg.profiles == []
    assert reg.active() is None
    assert reg.active_profile() is None


def test_rejects_wrong_root_tag(tmp_path: pathlib.Path) -> None:
    (tmp_path / "profiles.xml").write_bytes(b"<?xml version='1.0'?><wrong/>")
    with pytest.raises(ValueError, match="profileConfig"):
        load_profile_registry(tmp_path)


def test_ref_to_profile_resolves_directory_name(tmp_path: pathlib.Path) -> None:
    ref = ProfileRef(id="x", path="settings:DRGW.jmri/", name="DRGW.jmri")
    p = ref.to_profile(tmp_path)
    assert isinstance(p, Profile)
    assert p.profile_dir == tmp_path / "DRGW.jmri"


# ----- active-profile bookkeeping ------------------------------------------


def test_active_defaults_to_first_profile(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    reg = load_profile_registry(settings)
    assert reg.active() is not None
    assert reg.active().id == "DRGW.3f28eb5e"


def test_set_active_writes_marker_file(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    reg = load_profile_registry(settings)
    reg.set_active("N-Scale.abc1234")
    marker = settings / ACTIVE_MARKER
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == "N-Scale.abc1234"
    # And reloading the registry honours the marker.
    reg2 = load_profile_registry(settings)
    assert reg2.active().id == "N-Scale.abc1234"


def test_set_active_rejects_unknown_id(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    reg = load_profile_registry(settings)
    with pytest.raises(KeyError):
        reg.set_active("does-not-exist")


def test_marker_falls_back_to_first_if_stale(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    (settings / ACTIVE_MARKER).write_text("stale-id", encoding="utf-8")
    reg = load_profile_registry(settings)
    # No matching id -> fall through to first profile.
    assert reg.active().id == "DRGW.3f28eb5e"


def test_active_profile_returns_resolved_profile(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    reg = load_profile_registry(settings)
    p = reg.active_profile()
    assert isinstance(p, Profile)
    assert p.profile_dir == settings / "DRGW.jmri"


# ----- save round trip -----------------------------------------------------


def test_save_round_trip(tmp_path: pathlib.Path) -> None:
    settings = _seed(tmp_path)
    reg = load_profile_registry(settings)
    save_profile_registry(reg)
    reg2 = load_profile_registry(settings)
    ids_before = {p.id for p in reg.profiles}
    ids_after = {p.id for p in reg2.profiles}
    assert ids_before == ids_after


# ----- integration with real DRGW.jmri install (when present) -------------


def test_loads_users_real_profiles_xml() -> None:
    """If the developer has a real JMRI install at ~/JMRI/profiles.xml,
    confirm we can parse it without exploding. Skips otherwise."""
    real = pathlib.Path.home() / "JMRI" / "profiles.xml"
    if not real.exists():
        pytest.skip("no real JMRI install at ~/JMRI")
    reg = load_profile_registry(real.parent)
    # We don't make claims about contents -- just that it parses.
    assert isinstance(reg.profiles, list)
