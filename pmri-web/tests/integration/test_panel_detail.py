"""Integration tests for the /panels/{name}, /panels/{name}/svg,
and /panel/upload (with SVG preview) routes."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from pmri_core.persistence.profile import Profile

from pmri_web.app import build_web_app


_SAMPLE_PANEL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<PanelEditor name="Yard" width="800" height="600">'
    '<sensoricon sensor="LS1" x="100" y="200" />'
    '<turnouticon turnout="LT5" x="150" y="220" />'
    '</PanelEditor>'
).encode()


@pytest.fixture()
def panel_profile(tmp_path: pathlib.Path) -> Profile:
    """Create a profile with a sample panel on disk."""
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    panels_dir = profile.profile_dir / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    (panels_dir / "MainPanel.xml").write_bytes(_SAMPLE_PANEL_XML)
    return profile


@pytest.fixture()
def handle_with_panel(panel_profile: Profile):
    handle = build_web_app()
    handle.server.ctx._roster_profile = panel_profile  # type: ignore[attr-defined]
    # PanelService's listing resolver prefers ctx._panels_dir when set;
    # point it at the profile's panels directory so both the list
    # endpoint and the SVG renderer see the same files.
    handle.server.ctx._panels_dir = (  # type: ignore[attr-defined]
        panel_profile.profile_dir / "panels"
    )
    return handle


# ---- /panels/{name} ----------------------------------------------------


def test_panels_detail_renders_inline_svg(handle_with_panel) -> None:
    """The detail page carries the SVG inline so a browser sees the
    panel + the surrounding chrome in one request."""
    with TestClient(handle_with_panel.app) as tc:
        r = tc.get("/panels/MainPanel")
        r.raise_for_status()
        body = r.text
        assert "<svg" in body
        assert 'data-sysname="LS1"' in body
        assert 'data-sysname="LT5"' in body
        assert "1 sensor" in body   # element count summary


def test_panels_detail_404_on_missing(handle_with_panel) -> None:
    with TestClient(handle_with_panel.app) as tc:
        r = tc.get("/panels/no-such-panel")
        assert r.status_code == 404


def test_panels_detail_rejects_path_traversal(handle_with_panel) -> None:
    """``name`` is a filesystem basename -- path separators are rejected
    before touching the FS so a crafted name can't walk out of
    ``panels/``. The httpx client normalises path segments for us
    (``..`` collapses) so this guards the one segment the operator
    could post directly, URL-encoded."""
    with TestClient(handle_with_panel.app) as tc:
        # Backslash-encoded (the Windows separator) must still 404.
        r = tc.get("/panels/%5Cetc%5Cpasswd")
        assert r.status_code == 404
        # A dot-only name must 404 rather than reading the panels dir
        # itself as a file.
        r = tc.get("/panels/.")
        assert r.status_code == 404


# ---- /panels/{name}/svg ------------------------------------------------


def test_panels_svg_endpoint_returns_image_media_type(handle_with_panel) -> None:
    """The ``/svg`` endpoint serves ``image/svg+xml`` so a browser can
    open it standalone or an ``<img>`` can embed it. Older users of
    the panel-upload path still see inline SVG; this one is for
    direct links / exports."""
    with TestClient(handle_with_panel.app) as tc:
        r = tc.get("/panels/MainPanel/svg")
        r.raise_for_status()
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert r.text.startswith("<svg")


# ---- /panels/{name}/model and /panels/{name}/edit ----------------------


_LAYOUT_PANEL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<layout-config>'
    '<LayoutEditor name="Main" panelwidth="800" panelheight="600">'
    '<positionablepoint ident="A" x="10" y="10"/>'
    '<positionablepoint ident="B" x="90" y="10"/>'
    '<tracksegment ident="S1" connect1name="A" connect2name="B"'
    ' type1="POS_POINT" type2="POS_POINT" mainline="yes">'
    '<blockname>IB1</blockname></tracksegment>'
    '</LayoutEditor>'
    '</layout-config>'
).encode()


@pytest.fixture()
def handle_with_layout_panel(tmp_path: pathlib.Path):
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    panels_dir = profile.profile_dir / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    (panels_dir / "Main.xml").write_bytes(_LAYOUT_PANEL_XML)
    handle = build_web_app()
    handle.server.ctx._roster_profile = profile      # type: ignore[attr-defined]
    handle.server.ctx._panels_dir = panels_dir       # type: ignore[attr-defined]
    return handle


def test_panels_model_returns_json(handle_with_layout_panel) -> None:
    with TestClient(handle_with_layout_panel.app) as tc:
        r = tc.get("/panels/Main/model")
        r.raise_for_status()
        data = r.json()
        assert data["name"] == "Main"
        assert data["width"] == 800
        assert data["height"] == 600
        assert len(data["points"]) == 2
        assert data["points"][0]["ident"] == "A"
        assert len(data["segments"]) == 1
        seg = data["segments"][0]
        assert seg["ident"] == "S1"
        assert seg["c1"]["ref"] == "A"
        assert seg["mainline"] is True
        assert seg["block"] == "IB1"


def test_panels_edit_page_renders_canvas_chrome(
    handle_with_layout_panel,
) -> None:
    with TestClient(handle_with_layout_panel.app) as tc:
        r = tc.get("/panels/Main/edit")
        r.raise_for_status()
        body = r.text
        assert 'class="panel-editor-shell"' in body
        assert 'data-panel="Main"' in body
        assert 'data-role="canvas"' in body
        assert '/static/panel_canvas.js' in body


def test_panels_model_404_on_missing(handle_with_layout_panel) -> None:
    with TestClient(handle_with_layout_panel.app) as tc:
        r = tc.get("/panels/nope/model")
        assert r.status_code == 404


def test_layout_config_wrapped_panel_lists_in_profile(
    handle_with_layout_panel,
) -> None:
    """A ``<layout-config>``-wrapped ``<LayoutEditor>`` (the shape JMRI
    writes and the shape our ``/panel/create`` route writes) must show
    up in the panel listing. Regression for the earlier filter that
    only matched bare-rooted editor elements."""
    with TestClient(handle_with_layout_panel.app) as tc:
        r = tc.get("/panel")
        r.raise_for_status()
        assert 'href="/panels/Main/edit"' in r.text


def test_panel_create_then_delete_round_trip(tmp_path: pathlib.Path) -> None:
    """End-to-end: create a blank panel via /panel/create, confirm it
    appears in the list, then delete it via /panel/delete. The delete
    also sweeps rolling-backup siblings."""
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    panels_dir = profile.profile_dir / "panels"
    handle = build_web_app()
    handle.server.ctx._roster_profile = profile      # type: ignore[attr-defined]
    with TestClient(handle.app, follow_redirects=False) as tc:
        # Create.
        r = tc.post("/panel/create", data={
            "name": "Yard", "width": "800", "height": "600",
        })
        assert r.status_code == 303
        assert r.headers["location"] == "/panels/Yard/edit"
        # Listing shows it.
        r = tc.get("/panel")
        r.raise_for_status()
        assert "/panels/Yard/edit" in r.text
        # Simulate a rolling backup sibling landing from a prior edit.
        (panels_dir / "Yard.xml.bak").write_text("<x/>", encoding="utf-8")
        # Delete.
        r = tc.post("/panel/delete", data={"name": "Yard"})
        assert r.status_code == 303
        assert "deleted=Yard" in r.headers["location"]
    assert not (panels_dir / "Yard.xml").exists()
    assert not (panels_dir / "Yard.xml.bak").exists()


def test_panel_delete_missing_redirects_with_marker(
    tmp_path: pathlib.Path,
) -> None:
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    handle = build_web_app()
    handle.server.ctx._roster_profile = profile      # type: ignore[attr-defined]
    with TestClient(handle.app, follow_redirects=False) as tc:
        r = tc.post("/panel/delete", data={"name": "ghost"})
        assert r.status_code == 303
        assert "err=missing" in r.headers["location"]


def test_panel_create_name_collision_redirects_with_marker(
    tmp_path: pathlib.Path,
) -> None:
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    handle = build_web_app()
    handle.server.ctx._roster_profile = profile      # type: ignore[attr-defined]
    with TestClient(handle.app, follow_redirects=False) as tc:
        tc.post("/panel/create", data={"name": "A"})
        r = tc.post("/panel/create", data={"name": "A"})
        assert r.status_code == 303
        assert "err=exists" in r.headers["location"]


# ---- /panel home ---------------------------------------------------------


def test_panel_home_lists_profile_panels(handle_with_panel) -> None:
    """The /panel page lists panels from the active profile with
    links to each one's detail page."""
    with TestClient(handle_with_panel.app) as tc:
        r = tc.get("/panel")
        r.raise_for_status()
        body = r.text
        assert "MainPanel" in body
        assert 'href="/panels/MainPanel"' in body


# ---- /panel/upload (preview) ---------------------------------------------


def test_upload_includes_svg_preview(handle_with_panel) -> None:
    """Uploading a panel XML renders a preview SVG alongside the
    existing summary. Previously the response showed only the
    envelope-level counts."""
    with TestClient(handle_with_panel.app) as tc:
        r = tc.post(
            "/panel/upload",
            files={"file": ("x.xml", _SAMPLE_PANEL_XML, "application/xml")},
        )
        r.raise_for_status()
        body = r.text
        assert "<svg" in body
        assert "Rendered preview" in body
        assert 'data-sysname="LS1"' in body
