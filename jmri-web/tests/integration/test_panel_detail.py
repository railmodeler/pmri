"""Integration tests for the /panels/{name}, /panels/{name}/svg,
and /panel/upload (with SVG preview) routes."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from jmri_core.persistence.profile import Profile

from jmri_web.app import build_web_app


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
