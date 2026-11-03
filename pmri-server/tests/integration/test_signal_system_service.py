"""Integration tests for the ``signalSystem`` JSON topic."""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from tests.conftest import post


_BASIC = """<?xml version='1.0'?>
<aspecttable>
  <name>Basic Model Signals</name>
  <reference>Reference text</reference>
  <aspects>
    <aspect>
      <name>Clear</name><indication>Proceed</indication>
      <speed>Normal</speed><speed2>Normal</speed2>
    </aspect>
    <aspect>
      <name>Stop</name><indication>Stop</indication>
      <speed>Stop</speed>
    </aspect>
  </aspects>
</aspecttable>"""


def _write_system(root: pathlib.Path, system_id: str, xml: str) -> None:
    d = root / system_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "aspects.xml").write_text(xml, encoding="utf-8")


def test_list_empty_when_no_root_configured(client: TestClient) -> None:
    """No ``JMRI_SIGNAL_XML_ROOT`` set -> empty list, not 500. Mobile
    clients need to see "we have zero systems" as a graceful state."""
    resp = post(client, "signalSystem", method="list")
    assert resp["data"] == []


def test_list_returns_configured_systems(
    tmp_path: pathlib.Path, handle,
) -> None:
    _write_system(tmp_path, "basic", _BASIC)
    _write_system(tmp_path, "extra", _BASIC)
    handle.ctx._signal_xml_root = tmp_path
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "signalSystem", "method": "list",
        })
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()["data"]]
        assert ids == ["basic", "extra"]


def test_get_returns_full_aspect_table(
    tmp_path: pathlib.Path, handle,
) -> None:
    _write_system(tmp_path, "basic", _BASIC)
    handle.ctx._signal_xml_root = tmp_path
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "signalSystem", "method": "get",
            "data": {"id": "basic"},
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["id"] == "basic"
        assert data["name"] == "Basic Model Signals"
        aspect_names = [a["name"] for a in data["aspects"]]
        assert aspect_names == ["Clear", "Stop"]


def test_get_accepts_legacy_name_key(
    tmp_path: pathlib.Path, handle,
) -> None:
    """Older JMRI clients pass ``name`` (matching the schema for
    other NamedBean topics) rather than ``id``. Accept both."""
    _write_system(tmp_path, "basic", _BASIC)
    handle.ctx._signal_xml_root = tmp_path
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "signalSystem", "method": "get",
            "data": {"name": "basic"},
        })
        assert r.status_code == 200


def test_get_unknown_returns_404(
    tmp_path: pathlib.Path, handle,
) -> None:
    handle.ctx._signal_xml_root = tmp_path
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "signalSystem", "method": "get",
            "data": {"id": "no-such-system"},
        })
        assert r.status_code == 404


def test_get_without_id_returns_400(
    tmp_path: pathlib.Path, handle,
) -> None:
    handle.ctx._signal_xml_root = tmp_path
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "signalSystem", "method": "get",
            "data": {},
        })
        assert r.status_code == 400


# ---- signalMast integration: validAspects appears when the mast's
# signal-system is a known one --------------------------------------------


def test_signal_mast_projection_includes_valid_aspects(
    tmp_path: pathlib.Path, handle,
) -> None:
    """Once a mast is tagged with a signal_system the loader knows,
    its projection carries the valid-aspect list. Mobile clients use
    this to build the picker instead of hard-coding aspect names."""
    _write_system(tmp_path, "basic", _BASIC)
    handle.ctx._signal_xml_root = tmp_path
    with TestClient(handle.app) as tc:
        # Prime the loader by hitting /signalSystem/list (lazy init).
        tc.post("/json/", json={
            "type": "signalSystem", "method": "list",
        })
        # Create a mast and set its signalSystem to "basic".
        tc.post("/json/", json={
            "type": "signalMast", "method": "put",
            "data": {"name": "IF$dsh:basic:mast",
                     "signalSystem": "basic"},
        })
        r = tc.post("/json/", json={
            "type": "signalMast", "method": "get",
            "data": {"name": "IF$dsh:basic:mast"},
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["signalSystem"] == "basic"
        assert data["validAspects"] == ["Clear", "Stop"]


def test_signal_mast_projection_omits_valid_aspects_without_system(
    handle,
) -> None:
    """A mast without a signal_system (or one whose system the
    loader doesn't know) doesn't ship ``validAspects`` -- clients see
    ``signalSystem: null`` and treat aspect entry as free-form."""
    with TestClient(handle.app) as tc:
        tc.post("/json/", json={
            "type": "signalMast", "method": "put",
            "data": {"name": "IF$dsh:no-system:mast"},
        })
        r = tc.post("/json/", json={
            "type": "signalMast", "method": "get",
            "data": {"name": "IF$dsh:no-system:mast"},
        })
        data = r.json()["data"]
        assert data["signalSystem"] is None
        assert "validAspects" not in data
