"""Roster CRUD service tests (full lifecycle against an isolated Profile)."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from jmri_core.persistence.profile import Profile

from jmri_server.app import build_app


@pytest.fixture()
def client_with_isolated_roster(tmp_path: pathlib.Path):
    profile = Profile(root=tmp_path, name="test")
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    with TestClient(handle.app) as client:
        # Yield the per-profile roster dir so tests don't have to know
        # the layout details -- JMRI's per-profile structure now puts
        # ``roster.xml`` and ``roster/<id>.xml`` under ``<profile>/``.
        yield client, handle, profile.profile_dir


def test_list_empty_initially(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    r = client.post("/json/", json={"type": "roster", "method": "list"})
    r.raise_for_status()
    assert r.json()["data"] == []


def test_put_then_get(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    r = client.post("/json/", json={
        "type": "roster", "method": "put",
        "data": {"id": "UP_3601", "dccAddress": "3601", "longAddress": True,
                 "decoderFamily": "ESU", "decoderModel": "LokSound 5",
                 "roadName": "Union Pacific", "roadNumber": "3601"},
    })
    r.raise_for_status()
    assert r.json()["data"]["id"] == "UP_3601"

    r = client.post("/json/", json={
        "type": "roster", "method": "get", "data": {"id": "UP_3601"},
    })
    r.raise_for_status()
    assert r.json()["data"]["roadName"] == "Union Pacific"
    assert r.json()["data"]["longAddress"] is True


def test_put_persists_xml_file(client_with_isolated_roster):  # noqa: ANN001
    client, _, profile_dir = client_with_isolated_roster
    client.post("/json/", json={
        "type": "roster", "method": "put",
        "data": {"id": "TestLoco", "dccAddress": "3",
                 "decoderFamily": "Generic", "decoderModel": "X"},
    })
    # Per-loco file lives in roster/<id>.xml; roster.xml index lives at
    # the profile root (JMRI's layout).
    per_loco = list((profile_dir / "roster").glob("*.xml"))
    assert {f.name for f in per_loco} == {"TestLoco.xml"}
    assert (profile_dir / "roster.xml").exists()


def test_post_updates_existing(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    client.post("/json/", json={
        "type": "roster", "method": "put",
        "data": {"id": "X", "dccAddress": "3",
                 "decoderFamily": "f", "decoderModel": "m"},
    })
    r = client.post("/json/", json={
        "type": "roster", "method": "post",
        "data": {"id": "X", "roadName": "Updated"},
    })
    r.raise_for_status()
    assert r.json()["data"]["roadName"] == "Updated"
    # Other fields preserved.
    assert r.json()["data"]["decoderFamily"] == "f"


def test_delete_removes_entry_and_file(client_with_isolated_roster):  # noqa: ANN001
    client, _, profile_dir = client_with_isolated_roster
    client.post("/json/", json={
        "type": "roster", "method": "put",
        "data": {"id": "Del", "dccAddress": "3",
                 "decoderFamily": "f", "decoderModel": "m"},
    })
    assert (profile_dir / "roster" / "Del.xml").exists()

    r = client.post("/json/", json={
        "type": "roster", "method": "delete", "data": {"id": "Del"},
    })
    r.raise_for_status()
    assert not (profile_dir / "roster" / "Del.xml").exists()

    r = client.post("/json/", json={
        "type": "roster", "method": "get", "data": {"id": "Del"},
    })
    assert r.status_code == 404


def test_delete_unknown_returns_404(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    r = client.post("/json/", json={
        "type": "roster", "method": "delete", "data": {"id": "no-such"},
    })
    assert r.status_code == 404


def test_put_validates_id(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    r = client.post("/json/", json={
        "type": "roster", "method": "put", "data": {},
    })
    assert r.status_code == 400


def test_post_unknown_returns_404(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    r = client.post("/json/", json={
        "type": "roster", "method": "post",
        "data": {"id": "nonexistent", "roadName": "x"},
    })
    assert r.status_code == 404


def test_list_includes_created_entries(client_with_isolated_roster):  # noqa: ANN001
    client, _, _ = client_with_isolated_roster
    for i in range(3):
        client.post("/json/", json={
            "type": "roster", "method": "put",
            "data": {"id": f"L{i}", "dccAddress": str(i + 1),
                     "decoderFamily": "f", "decoderModel": "m"},
        })
    r = client.post("/json/", json={"type": "roster", "method": "list"})
    assert {e["id"] for e in r.json()["data"]} == {"L0", "L1", "L2"}


def test_persistence_survives_store_rebuild(tmp_path: pathlib.Path) -> None:
    """Create entries, rebuild the server with the same profile, verify entries reload."""
    profile = Profile(root=tmp_path, name="test")

    h1 = build_app()
    h1.ctx._roster_profile = profile  # type: ignore[attr-defined]
    with TestClient(h1.app) as c1:
        c1.post("/json/", json={
            "type": "roster", "method": "put",
            "data": {"id": "Persist", "dccAddress": "42",
                     "decoderFamily": "f", "decoderModel": "m"},
        })

    h2 = build_app()
    h2.ctx._roster_profile = profile  # type: ignore[attr-defined]
    with TestClient(h2.app) as c2:
        r = c2.post("/json/", json={"type": "roster", "method": "list"})
        ids = {e["id"] for e in r.json()["data"]}
        assert "Persist" in ids
