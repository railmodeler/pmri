from __future__ import annotations

import pathlib

from jmri_core.persistence.profile import Profile, default_settings_dir


def test_default_settings_dir_is_absolute() -> None:
    d = default_settings_dir()
    assert d.is_absolute()


def test_jmri_settings_dir_env_override(
    tmp_path: pathlib.Path, monkeypatch: object  # noqa: ANN001
) -> None:
    """``JMRI_SETTINGS_DIR`` lets you point at a non-default tree --
    important for Linux hosts running against an SMB-mounted Windows
    JMRI install."""
    monkeypatch.setenv("JMRI_SETTINGS_DIR", str(tmp_path))  # type: ignore[attr-defined]
    assert default_settings_dir() == tmp_path


def test_profile_paths_compose_correctly(tmp_path: pathlib.Path) -> None:
    """JMRI-compatible layout: profile dir is ``<root>/<name>`` directly,
    with per-profile roster, operations, panels, throttle, etc. inside it."""
    p = Profile(root=tmp_path, name="DRGW.jmri")
    assert p.profile_dir == tmp_path / "DRGW.jmri"
    assert p.roster_dir == p.profile_dir / "roster"
    assert p.roster_index == p.profile_dir / "roster.xml"
    assert p.rosters_cv_dir == p.profile_dir / "rosters"
    assert p.operations_dir == p.profile_dir / "operations"
    assert p.panel_dir == p.profile_dir / "panels"
    assert p.throttle_dir == p.profile_dir / "throttle"
    assert p.signal_dir == p.profile_dir / "signal"
    assert p.programmers_dir == p.profile_dir / "programmers"
    assert p.jython_dir == p.profile_dir / "jython"           # legacy
    assert p.python_dir == p.profile_dir / "python"           # active
    assert p.shared_python_dir == tmp_path / "python"         # shared
    assert p.log_dir == tmp_path / "log"                      # shared
    assert p.signal_systems_dir == tmp_path / "resources" / "signals"  # shared


def test_profile_config_file_prefers_panelpro_then_configuration(tmp_path: pathlib.Path) -> None:
    p = Profile(root=tmp_path, name="club")
    p.ensure()
    # Empty profile -> defaults to PanelPro.xml (5.x form).
    assert p.config_file.name == "PanelPro.xml"
    # If only Configuration.xml exists, use that.
    legacy = p.profile_dir / "Configuration.xml"
    legacy.write_text("<layout-config/>", encoding="utf-8")
    assert p.config_file == legacy
    # If both exist, prefer PanelPro.xml.
    (p.profile_dir / "PanelPro.xml").write_text("<layout-config/>", encoding="utf-8")
    assert p.config_file.name == "PanelPro.xml"


def test_ensure_creates_all_dirs(tmp_path: pathlib.Path) -> None:
    p = Profile(root=tmp_path / "JMRI", name="home")
    p.ensure()
    assert p.root.is_dir()
    assert p.roster_dir.is_dir()
    assert p.operations_dir.is_dir()
    assert p.panel_dir.is_dir()
    assert p.profile_dir.is_dir()
    assert p.throttle_dir.is_dir()


def test_ensure_is_idempotent(tmp_path: pathlib.Path) -> None:
    p = Profile(root=tmp_path, name="test")
    p.ensure()
    p.ensure()  # second call must not raise


def test_profile_repr_includes_name_and_root(tmp_path: pathlib.Path) -> None:
    p = Profile(root=tmp_path, name="alpha")
    r = repr(p)
    assert "alpha" in r
    assert "Profile" in r


def test_profile_default_uses_platform_dir() -> None:
    p = Profile.default()
    assert p.name == "default"
    assert p.root.is_absolute()
