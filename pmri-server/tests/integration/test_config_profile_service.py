"""Integration tests for the ``configProfile`` JSON topic + XML
formatting regression for ``PanelPro.xml``."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from pmri_core.persistence.profile_registry import (
    PROFILES_XML,
    ProfileRef,
    ProfileRegistry,
    load_profile_registry,
)

from pmri_server.app import build_app


@pytest.fixture()
def handle_with_registry(tmp_path: pathlib.Path):
    """Bare AppHandle wired to a fresh registry rooted at tmp_path."""
    (tmp_path / "dev").mkdir()
    registry = ProfileRegistry(
        settings_root=tmp_path,
        profiles=[ProfileRef(id="dev-1", path="settings:dev/", name="dev")],
    )
    handle = build_app()
    handle.ctx.profile_registry = registry
    return handle, registry, tmp_path


# ---- list ----------------------------------------------------------------


def test_list_returns_registered_profiles(handle_with_registry) -> None:
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "list",
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert [p["name"] for p in data] == ["dev"]
        assert data[0]["isActive"] is True


def test_list_empty_when_no_registry(handle_with_registry) -> None:
    handle, _, _ = handle_with_registry
    handle.ctx.profile_registry = None
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "list",
        })
        assert r.status_code == 200
        assert r.json()["data"] == []


# ---- create --------------------------------------------------------------


def test_create_appends_and_writes_profiles_xml(
    handle_with_registry, tmp_path,
) -> None:
    """After PUT, the new profile shows in list AND profiles.xml on
    disk carries the new <profile> entry. That was the whole point
    of the service -- hand-editing profiles.xml wasn't reliable."""
    handle, _, root = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "put",
            "data": {"name": "prod"},
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["name"] == "prod"
        assert data["isActive"] is False
        # profiles.xml on disk carries the new entry -- reload it to
        # confirm persistence, not just in-memory.
        reloaded = load_profile_registry(root)
        names = [p.name for p in reloaded.profiles]
        assert "prod" in names
        # Empty PanelPro.xml minted under the new dir.
        assert (root / "prod" / "PanelPro.xml").is_file()


def test_create_auto_generates_id_matching_pmri_convention(
    handle_with_registry,
) -> None:
    """JMRI Java uses ``<name>.<8-hex>`` for profile ids so an install
    imported back into Java stays recognizable. Match that."""
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "put",
            "data": {"name": "staging"},
        })
        pid = r.json()["data"]["id"]
        # ``staging.abcd1234`` shape.
        assert pid.startswith("staging.")
        suffix = pid.split(".", 1)[1]
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)


def test_create_rejects_duplicate_name(handle_with_registry) -> None:
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "put",
            "data": {"name": "dev"},   # already exists
        })
        assert r.status_code == 409


def test_create_rejects_unsafe_name(handle_with_registry) -> None:
    """Filesystem-safe regex -- no colon (Windows-hostile), no
    slashes (path traversal). The service must refuse."""
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        for bad in ("../evil", "with space", "with/slash", "with:colon"):
            r = tc.post("/json/", json={
                "type": "configProfile", "method": "put",
                "data": {"name": bad},
            })
            assert r.status_code == 400, bad


def test_create_missing_name_400s(handle_with_registry) -> None:
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "put",
            "data": {},
        })
        assert r.status_code == 400


# ---- delete --------------------------------------------------------------


def test_delete_removes_from_registry(
    handle_with_registry, tmp_path,
) -> None:
    """The on-disk directory is intentionally left alone -- an
    accidental UI click shouldn't destroy data. Only the registry
    entry goes."""
    handle, _, root = handle_with_registry
    with TestClient(handle.app) as tc:
        # Create a fresh one so we can delete it without touching
        # the active dev profile.
        tc.post("/json/", json={
            "type": "configProfile", "method": "put",
            "data": {"name": "throwaway"},
        })
        assert (root / "throwaway").is_dir()

        # Find its id from the list.
        listing = tc.post("/json/", json={
            "type": "configProfile", "method": "list",
        }).json()["data"]
        target = next(p for p in listing if p["name"] == "throwaway")

        r = tc.post("/json/", json={
            "type": "configProfile", "method": "delete",
            "data": {"id": target["id"]},
        })
        assert r.status_code == 200
        assert r.json()["data"]["deleted"] is True

        # Registry no longer contains it.
        reloaded = load_profile_registry(root)
        assert "throwaway" not in {p.name for p in reloaded.profiles}
        # But the directory is still there.
        assert (root / "throwaway").is_dir()


def test_delete_refuses_active_profile(handle_with_registry) -> None:
    """Deleting the active profile would leave the running server
    pointing at nothing. Force the user to switch first."""
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "delete",
            "data": {"id": "dev-1"},
        })
        assert r.status_code == 409


def test_delete_unknown_id_404s(handle_with_registry) -> None:
    handle, _, _ = handle_with_registry
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "configProfile", "method": "delete",
            "data": {"id": "no-such-id"},
        })
        assert r.status_code == 404


# ---- PanelPro.xml formatting -------------------------------------------


def test_panel_pro_xml_is_indented_after_upsert(
    handle_with_registry, tmp_path,
) -> None:
    """Regression for the "one giant line" bug -- after any bean
    write, the ``PanelPro.xml`` must be human-readable so operators
    can hand-inspect it. Checks a specific known-good chunk is on
    its own line, not embedded in a long unbroken run."""
    from pmri_core.persistence import TurnoutRow, SensorRow  # noqa: PLC0415
    from pmri_core.persistence.profile import Profile  # noqa: PLC0415

    from pmri_server.config_store import ConfigStore  # noqa: PLC0415

    profile = Profile(root=tmp_path, name="dev")
    store = ConfigStore(profile=profile)
    store.upsert_turnout(TurnoutRow(system_name="IT1", user_name="Main"))
    store.upsert_sensor(SensorRow(system_name="IS10"))

    text = profile.config_file.read_text(encoding="utf-8")
    # Each of these should be on a line of its own with leading
    # whitespace, not glued to the surrounding element.
    lines = text.splitlines()
    def line_starting_with(prefix):
        return any(line.lstrip().startswith(prefix) for line in lines)
    assert line_starting_with("<turnouts")
    assert line_starting_with("<turnout ")
    assert line_starting_with("<systemName>")
    assert line_starting_with("<sensors")
    # A one-line file would have every element on the same line as
    # ``<layout-config>``. Reject that.
    assert len(lines) > 5
