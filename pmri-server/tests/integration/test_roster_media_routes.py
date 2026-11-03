"""Integration tests for the roster-media HTTP routes and the
``imageUrl`` / ``iconUrl`` extension on the roster projection.

Uses an isolated profile so we control the resources dir + the
image files that resolve. The path-traversal safety tests live in
``test_roster_media.py`` (unit level); this file focuses on the
route + projection wire-level behaviour.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from pmri_core.persistence.profile import Profile

from pmri_server.app import build_app


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000000"
    "1f15c489000000017352474200aece1ce90000000d4944415478da62fcff"
    "ff3f0300050001d16acb1e0000000049454e44ae426082"
)


@pytest.fixture()
def env(tmp_path: pathlib.Path):
    """Bare handle wired to an isolated profile with a resources dir."""
    profile = Profile(root=tmp_path, name="test")
    profile.ensure()
    (profile.profile_dir / "resources").mkdir(parents=True, exist_ok=True)
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    return handle, profile


def _put_entry(
    client: TestClient, entry_id: str, **overrides: object,
) -> dict:
    payload: dict = {
        "id": entry_id,
        "dccAddress": "3601",
        "longAddress": True,
        "decoderFamily": "ESU",
        "decoderModel": "LokSound 5",
    }
    payload.update(overrides)
    r = client.post("/json/", json={
        "type": "roster", "method": "put", "data": payload,
    })
    r.raise_for_status()
    return r.json()["data"]


# ---- projection ---------------------------------------------------------


def test_projection_has_no_urls_when_no_path_set(env) -> None:
    """A roster entry without image / icon paths should carry
    ``imageFilePath: null`` and no ``imageUrl`` field -- presence
    absence is the client's cue to render the no-photo state."""
    handle, _ = env
    with TestClient(handle.app) as tc:
        _put_entry(tc, "UP_3601")
        r = tc.post("/json/", json={
            "type": "roster", "method": "get",
            "data": {"id": "UP_3601"},
        })
        d = r.json()["data"]
        assert d["imageFilePath"] is None
        assert "imageUrl" not in d
        assert "iconUrl" not in d


def test_projection_has_no_urls_when_file_missing(env) -> None:
    """The entry references a path that doesn't exist on disk. We
    keep the raw ``imageFilePath`` (round-trips) but skip the URL --
    a mobile client that fetched the URL would 404, better to just
    tell it no URL is available."""
    handle, _ = env
    with TestClient(handle.app) as tc:
        _put_entry(tc, "UP_3601", imageFilePath="img/nope.png")
        r = tc.post("/json/", json={
            "type": "roster", "method": "get",
            "data": {"id": "UP_3601"},
        })
        d = r.json()["data"]
        assert d["imageFilePath"] == "img/nope.png"
        assert "imageUrl" not in d


def test_projection_gains_urls_when_files_exist(env) -> None:
    handle, profile = env
    (profile.profile_dir / "resources" / "img").mkdir(parents=True)
    (profile.profile_dir / "resources" / "img" / "loco.png").write_bytes(PNG_1PX)
    (profile.profile_dir / "resources" / "img" / "loco-icon.png").write_bytes(PNG_1PX)
    with TestClient(handle.app) as tc:
        _put_entry(tc, "UP_3601",
                   imageFilePath="img/loco.png",
                   iconFilePath="img/loco-icon.png")
        r = tc.post("/json/", json={
            "type": "roster", "method": "get",
            "data": {"id": "UP_3601"},
        })
        d = r.json()["data"]
        assert d["imageUrl"] == "/roster/UP_3601/image"
        assert d["iconUrl"] == "/roster/UP_3601/icon"


# ---- HTTP routes --------------------------------------------------------


def test_get_image_route_streams_bytes(env) -> None:
    handle, profile = env
    (profile.profile_dir / "resources" / "img").mkdir(parents=True)
    (profile.profile_dir / "resources" / "img" / "loco.png").write_bytes(PNG_1PX)
    with TestClient(handle.app) as tc:
        _put_entry(tc, "UP_3601", imageFilePath="img/loco.png")
        r = tc.get("/roster/UP_3601/image")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content == PNG_1PX


def test_get_icon_route_streams_bytes(env) -> None:
    handle, profile = env
    (profile.profile_dir / "resources" / "img").mkdir(parents=True)
    (profile.profile_dir / "resources" / "img" / "loco-icon.png").write_bytes(PNG_1PX)
    with TestClient(handle.app) as tc:
        _put_entry(tc, "UP_3601", iconFilePath="img/loco-icon.png")
        r = tc.get("/roster/UP_3601/icon")
        assert r.status_code == 200
        assert r.content == PNG_1PX


def test_missing_entry_returns_404(env) -> None:
    handle, _ = env
    with TestClient(handle.app) as tc:
        r = tc.get("/roster/DOES_NOT_EXIST/image")
        assert r.status_code == 404


def test_entry_without_media_returns_404(env) -> None:
    handle, _ = env
    with TestClient(handle.app) as tc:
        _put_entry(tc, "UP_3601")   # no imageFilePath set
        r = tc.get("/roster/UP_3601/image")
        assert r.status_code == 404


def test_route_rejects_absolute_path_escape(env) -> None:
    """If the roster XML somehow carries an absolute path pointing
    outside the profile, the media route must NOT serve the file.
    The resolver already guards this -- the route test pins the
    end-to-end behaviour so a regression can't slip through."""
    handle, _ = env
    with TestClient(handle.app) as tc:
        # Point at an absolute path outside the profile -- resolver
        # returns None -> route 404s. Use a definitely-outside path.
        _put_entry(tc, "UP_3601", imageFilePath="/etc/hostname")
        r = tc.get("/roster/UP_3601/image")
        assert r.status_code == 404
