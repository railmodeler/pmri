"""Integration tests for the ``panel-edit`` JSON service."""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient


def _bootstrap_panel(panels_dir: pathlib.Path, name: str = "MyLayout") -> pathlib.Path:
    panels_dir.mkdir(parents=True, exist_ok=True)
    path = panels_dir / f"{name}.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<layout-config>'
        f'<LayoutEditor name="{name}" panelwidth="100" panelheight="100"/>'
        '</layout-config>',
        encoding="utf-8",
    )
    return path


def _post(client: TestClient, payload: dict) -> dict:
    r = client.post("/json/", json={
        "type": "panel-edit", "method": "post", "data": payload,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_add_segment_then_read_back(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addPoint", "panel": "Layout/MyLayout",
            "ident": "A", "x": 10, "y": 10,
        })
        _post(tc, {
            "op": "addPoint", "panel": "Layout/MyLayout",
            "ident": "B", "x": 90, "y": 10,
        })
        resp = _post(tc, {
            "op": "addSegment", "panel": "Layout/MyLayout",
            "ident": "S1", "connect1_name": "A", "connect2_name": "B",
            "main_line": True, "layout_block": "IB1",
        })
    assert resp["data"]["ok"] is True
    assert resp["data"]["ident"] == "S1"

    # Confirm the file now holds the segment.
    text = (panels / "MyLayout.xml").read_text(encoding="utf-8")
    assert '<tracksegment' in text
    assert 'ident="S1"' in text
    assert 'mainline="yes"' in text
    assert "<blockname>IB1</blockname>" in text


def test_update_and_remove_segment(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addSegment", "panel": "Layout/MyLayout",
            "ident": "S1", "connect1_name": "A", "connect2_name": "B",
        })
        _post(tc, {
            "op": "updateSegment", "panel": "Layout/MyLayout",
            "ident": "S1", "main_line": True, "layout_block": "IB2",
        })
        resp = _post(tc, {
            "op": "removeSegment", "panel": "Layout/MyLayout",
            "ident": "S1",
        })
    assert resp["data"]["ok"] is True

    text = (panels / "MyLayout.xml").read_text(encoding="utf-8")
    assert "<tracksegment" not in text


def test_add_turnout_and_set_bean(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addTurnout", "panel": "Layout/MyLayout",
            "ident": "X1", "turnout_name": "",
            "xcen": 50, "ycen": 50,
            "xa": 30, "ya": 50, "xb": 70, "yb": 50,
            "xc": 60, "yc": 70,
        })
        _post(tc, {
            "op": "setTurnoutBean", "panel": "Layout/MyLayout",
            "ident": "X1", "turnout_name": "IT1",
        })
    text = (panels / "MyLayout.xml").read_text(encoding="utf-8")
    assert 'turnoutname="IT1"' in text


def test_move_point(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addPoint", "panel": "Layout/MyLayout",
            "ident": "A", "x": 10, "y": 10,
        })
        resp = _post(tc, {
            "op": "movePoint", "panel": "Layout/MyLayout",
            "ident": "A", "x": 55, "y": 77,
        })
    assert resp["data"]["x"] == 55
    assert resp["data"]["y"] == 77
    text = (panels / "MyLayout.xml").read_text(encoding="utf-8")
    # Coords serialise as floats ("55.0") from the service layer; the
    # parser accepts either so the exact punctuation doesn't matter.
    assert 'x="55' in text and 'y="77' in text


def test_unknown_panel_returns_404(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    panels.mkdir()
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "panel-edit", "method": "post",
            "data": {
                "op": "addSegment", "panel": "Layout/Nope",
                "ident": "S1", "connect1_name": "A", "connect2_name": "B",
            },
        })
    assert r.status_code == 404


def test_unknown_op_returns_400(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "panel-edit", "method": "post",
            "data": {"op": "frobnicate", "panel": "Layout/MyLayout"},
        })
    assert r.status_code == 400


def test_duplicate_ident_returns_409(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addSegment", "panel": "Layout/MyLayout",
            "ident": "S1", "connect1_name": "A", "connect2_name": "B",
        })
        r = tc.post("/json/", json={
            "type": "panel-edit", "method": "post",
            "data": {
                "op": "addSegment", "panel": "Layout/MyLayout",
                "ident": "S1", "connect1_name": "A", "connect2_name": "B",
            },
        })
    assert r.status_code == 409


def test_icon_add_move_remove_round_trip(
    tmp_path: pathlib.Path, handle,
) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addIcon", "panel": "Layout/MyLayout",
            "kind": "sensoricon", "key": "IS1", "x": 50, "y": 60,
        })
        _post(tc, {
            "op": "moveIcon", "panel": "Layout/MyLayout",
            "kind": "sensoricon", "key": "IS1", "x": 99, "y": 77,
        })
        resp = _post(tc, {
            "op": "removeIcon", "panel": "Layout/MyLayout",
            "kind": "sensoricon", "key": "IS1",
        })
    assert resp["data"]["ok"] is True
    text = (panels / "MyLayout.xml").read_text(encoding="utf-8")
    assert "<sensoricon" not in text


def test_duplicate_icon_returns_409(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addIcon", "panel": "Layout/MyLayout",
            "kind": "turnouticon", "key": "IT1", "x": 10, "y": 10,
        })
        r = tc.post("/json/", json={
            "type": "panel-edit", "method": "post",
            "data": {
                "op": "addIcon", "panel": "Layout/MyLayout",
                "kind": "turnouticon", "key": "IT1", "x": 20, "y": 20,
            },
        })
    assert r.status_code == 409


def test_edit_creates_rolling_backup(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    _bootstrap_panel(panels)
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        _post(tc, {
            "op": "addSegment", "panel": "Layout/MyLayout",
            "ident": "S1", "connect1_name": "A", "connect2_name": "B",
        })
    bak = panels / "MyLayout.xml.bak"
    assert bak.is_file(), "first edit should leave a .bak sibling behind"
