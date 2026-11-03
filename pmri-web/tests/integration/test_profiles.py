"""Tests for the multi-profile model + home-page picker."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from pmri_core.persistence import Profile, load_profile_registry

from pmri_web.app import build_web_app

_PROFILES_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<profileConfig>
  <profiles>
    <profile id="DRGW.3f28eb5e" path="settings:DRGW.jmri/" />
    <profile id="N-Scale.abc1234" path="settings:N-Scale.jmri/" />
  </profiles>
</profileConfig>"""


@pytest.fixture()
def isolated_settings(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp settings root with two profiles + minimal PanelPro per profile.

    Uses ``I`` prefixes so beans materialize against the
    :class:`InternalConnection` (prefix ``I``).
    """
    (tmp_path / "profiles.xml").write_bytes(_PROFILES_XML)
    for name, turnouts in (("DRGW.jmri", ["IT80"]), ("N-Scale.jmri", ["IT5"])):
        pdir = tmp_path / name
        pdir.mkdir()
        rows = "\n".join(
            f'    <turnout feedback="DIRECT" inverted="false">'
            f'<systemName>{n}</systemName><userName>{n} from {name}</userName>'
            f'</turnout>'
            for n in turnouts
        )
        body = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<layout-config>\n'
            b'  <turnouts class="jmri.foo">\n'
            + rows.encode()
            + b'\n  </turnouts>\n'
            b'</layout-config>\n'
        )
        (pdir / "PanelPro.xml").write_bytes(body)
    return tmp_path


def _client_with_profiles(settings_root: pathlib.Path) -> TestClient:
    """Build a web client with its ConfigStore + registry rooted at *settings_root*."""
    from pmri_server.config_store import ConfigStore  # noqa: PLC0415

    handle = build_web_app()
    registry = load_profile_registry(settings_root)
    active = registry.active_profile()
    assert active is not None  # fixture guarantees at least one profile
    handle.server.ctx.profile_registry = registry
    handle.server.ctx.config_store = ConfigStore(profile=active)
    handle.server.ctx._roster_profile = active  # type: ignore[attr-defined]
    return TestClient(handle.app)


# ----- home page picker ----------------------------------------------------


def test_home_page_lists_profiles(isolated_settings: pathlib.Path) -> None:
    client = _client_with_profiles(isolated_settings)
    with client:
        r = client.get("/")
        r.raise_for_status()
        assert "DRGW.jmri" in r.text
        assert "N-Scale.jmri" in r.text
        # First profile is active by default; should be marked.
        assert "(active)" in r.text


def test_home_page_shows_settings_root_when_no_profiles(tmp_path: pathlib.Path) -> None:
    """No profiles.xml -> show the searched path so the user can fix it."""
    handle = build_web_app()
    from pmri_core.persistence import ProfileRegistry  # noqa: PLC0415
    handle.server.ctx.profile_registry = ProfileRegistry(
        settings_root=tmp_path, profiles=[],
    )
    with TestClient(handle.app) as c:
        r = c.get("/")
        r.raise_for_status()
        # The Profiles card is rendered, but with guidance instead of a picker.
        assert "<h3>Profiles</h3>" in r.text
        assert str(tmp_path) in r.text
        assert "JMRI_SETTINGS_DIR" in r.text
        # No <select> when there are no profiles.
        assert 'name="profile_id"' not in r.text


# ----- load action ---------------------------------------------------------


def test_load_switches_active_profile(isolated_settings: pathlib.Path) -> None:
    client = _client_with_profiles(isolated_settings)
    with client:
        # Lifespan auto-restores beans from the active (first) profile.
        r = client.post("/json/", json={
            "type": "turnout", "method": "list",
        })
        r.raise_for_status()
        names_before = {t["name"] for t in r.json()["data"]}
        # IT80 belongs to DRGW; IT5 to N-Scale.
        assert "IT80" in names_before
        assert "IT5" not in names_before

        # Switch to N-Scale.
        r = client.post("/profiles/load", data={"profile_id": "N-Scale.abc1234"})
        r.raise_for_status()
        assert "Loaded profile: N-Scale.jmri" in r.text

        # Bean managers were cleared + repopulated.
        r = client.post("/json/", json={
            "type": "turnout", "method": "list",
        })
        names_after = {t["name"] for t in r.json()["data"]}
        assert "IT5" in names_after
        assert "IT80" not in names_after


def test_load_rejects_unknown_profile_id(isolated_settings: pathlib.Path) -> None:
    client = _client_with_profiles(isolated_settings)
    with client:
        r = client.post("/profiles/load", data={"profile_id": "does-not-exist"})
        assert r.status_code == 400


def test_load_persists_active_marker(isolated_settings: pathlib.Path) -> None:
    """After switching, the .active_profile marker file should record the choice
    so the next server startup honours it."""
    client = _client_with_profiles(isolated_settings)
    with client:
        client.post("/profiles/load", data={"profile_id": "N-Scale.abc1234"})
    marker = isolated_settings / ".active_profile"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == "N-Scale.abc1234"


def test_unused_profile_import() -> None:
    """Smoke: ``Profile`` is reachable from the imports we use."""
    assert Profile is not None


def test_create_app_attaches_profile_registry(
    isolated_settings: pathlib.Path, monkeypatch: object,
) -> None:
    """The production ``create_app`` entry point must attach a profile
    registry -- the alpha tester on the Pi reported missing because
    ``pmri_web.create_app`` was bypassing the server's setup."""
    monkeypatch.setenv("JMRI_SETTINGS_DIR", str(isolated_settings))  # type: ignore[attr-defined]
    # Importing inside the test so the monkeypatched env var is honoured.
    from pmri_web.app import create_app  # noqa: PLC0415

    app = create_app()
    # Drill into FastAPI's openapi schema to locate our handle -- but
    # simpler: rebuild and check the handle directly via build_web_app
    # under the same env var.
    from pmri_web.app import build_web_app  # noqa: PLC0415
    from pmri_server.app import attach_profile_persistence  # noqa: PLC0415
    handle = build_web_app()
    attach_profile_persistence(handle.server)
    reg = handle.server.ctx.profile_registry
    assert reg is not None
    ids = {p.id for p in reg.profiles}
    assert "DRGW.3f28eb5e" in ids
    # Render the home page through the actual ``create_app`` build.
    with TestClient(app) as client:
        r = client.get("/")
        r.raise_for_status()
        assert "DRGW.jmri" in r.text
        assert str(isolated_settings) in r.text
