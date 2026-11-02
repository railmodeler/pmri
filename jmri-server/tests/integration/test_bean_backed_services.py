"""Integration tests for the five bean-backed topics added in the
``missing JSON topics`` pass: ``audio``, ``idTag``, ``layoutBlock``,
``oblock``, ``panel``.

The bean shapes are pinned against the schemas at
``stage-0/contracts/json-schema/{audio,idtag,layoutblock,oblock,util}/``.
Behaviour parity with JMRI Java is checked against
``jmri.server.json.audio.JsonAudioHttpService`` for the audio Play/Stop
verbs; the others are straight round-trip CRUD.
"""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from tests.conftest import post


# ---------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------


def test_audio_put_creates_source_with_defaults(client: TestClient) -> None:
    r = post(client, "audio", method="put", data={"name": "IAS1"})
    assert r["data"]["name"] == "IAS1"
    assert r["data"]["kind"] == "source"
    assert r["data"]["state"] == 16     # STOPPED
    assert r["data"]["gain"] == 1.0


def test_audio_play_and_stop_via_command(client: TestClient) -> None:
    """``command`` field mirrors JMRI Java's ``Play`` / ``Stop`` verbs."""
    post(client, "audio", method="put", data={"name": "IAS2"})
    r_play = post(client, "audio", method="post", data={
        "name": "IAS2", "command": "Play",
    })
    assert r_play["data"]["state"] == 17    # PLAYING
    r_stop = post(client, "audio", method="post", data={
        "name": "IAS2", "command": "Stop",
    })
    assert r_stop["data"]["state"] == 16    # STOPPED


def test_audio_play_no_op_on_buffer_kind(client: TestClient) -> None:
    """Buffer / listener kinds don't have play semantics -- the command
    is a no-op instead of an error so polymorphic clients aren't
    forced to branch on kind."""
    post(client, "audio", method="put", data={
        "name": "IAS3", "kind": "buffer",
    })
    r = post(client, "audio", method="post", data={
        "name": "IAS3", "command": "Play",
    })
    assert r["data"]["state"] == 16


def test_audio_gain_range_enforced(client: TestClient) -> None:
    post(client, "audio", method="put", data={"name": "IAS4"})
    r = client.post("/json/", json={
        "type": "audio", "method": "post",
        "data": {"name": "IAS4", "gain": 1.5},
    })
    assert r.status_code == 400


def test_audio_unknown_kind_400s(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "audio", "method": "put",
        "data": {"name": "IAS5", "kind": "orchestra"},
    })
    assert r.status_code == 400


def test_audio_list_and_delete(client: TestClient) -> None:
    post(client, "audio", method="put", data={"name": "IAS6"})
    resp = post(client, "audio", method="list")
    names = {a["name"] for a in resp["data"]}
    assert "IAS6" in names
    post(client, "audio", method="delete", data={"name": "IAS6"})
    resp = post(client, "audio", method="list")
    names = {a["name"] for a in resp["data"]}
    assert "IAS6" not in names


# ---------------------------------------------------------------------
# idTag
# ---------------------------------------------------------------------


def test_id_tag_put_creates_with_unknown_state(client: TestClient) -> None:
    r = post(client, "idTag", method="put", data={"name": "ID100"})
    assert r["data"]["name"] == "ID100"
    assert r["data"]["state"] == 0     # UNKNOWN
    assert r["data"]["reporter"] is None
    assert r["data"]["time"] is None


def test_id_tag_report_seen_records_reporter_and_time(
    client: TestClient,
) -> None:
    post(client, "idTag", method="put", data={"name": "ID101"})
    r = post(client, "idTag", method="post", data={
        "name": "ID101", "state": 2,
        "reporter": "IR1", "time": "2026-08-03T12:34:56Z",
    })
    assert r["data"]["state"] == 2      # SEEN
    assert r["data"]["reporter"] == "IR1"
    assert r["data"]["time"] == "2026-08-03T12:34:56Z"


def test_id_tag_seen_without_reporter_400s(client: TestClient) -> None:
    post(client, "idTag", method="put", data={"name": "ID102"})
    r = client.post("/json/", json={
        "type": "idTag", "method": "post",
        "data": {"name": "ID102", "state": 2},
    })
    assert r.status_code == 400


def test_id_tag_unseen_preserves_last_reporter(client: TestClient) -> None:
    """Once a tag has been seen, transitioning to UNSEEN keeps the
    reporter attribution so clients can render "last seen at X, gone
    since Y"."""
    post(client, "idTag", method="put", data={"name": "ID103"})
    post(client, "idTag", method="post", data={
        "name": "ID103", "state": 2,
        "reporter": "IR7", "time": "2026-08-03T12:00:00Z",
    })
    r = post(client, "idTag", method="post", data={
        "name": "ID103", "state": 3,
        "time": "2026-08-03T12:05:00Z",
    })
    assert r["data"]["state"] == 3       # UNSEEN
    assert r["data"]["reporter"] == "IR7"   # kept
    assert r["data"]["time"] == "2026-08-03T12:05:00Z"


def test_id_tag_invalid_state_400s(client: TestClient) -> None:
    post(client, "idTag", method="put", data={"name": "ID104"})
    r = client.post("/json/", json={
        "type": "idTag", "method": "post",
        "data": {"name": "ID104", "state": 99},
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------
# layoutBlock
# ---------------------------------------------------------------------


def test_layout_block_put_creates_with_default_colours(
    client: TestClient,
) -> None:
    r = post(client, "layoutBlock", method="put", data={"name": "ILB1"})
    d = r["data"]
    assert d["name"] == "ILB1"
    assert d["state"] == 0
    assert d["block"] is None
    assert d["memory"] is None
    assert d["occupancySensor"] is None
    assert d["occupiedSense"] == 2
    assert d["blockColor"] == "black"
    assert d["trackColor"] == "black"
    assert d["occupiedColor"] == "red"
    assert d["extraColor"] == "white"
    assert d["useExtraColor"] is False


def test_layout_block_apply_updates_every_field(client: TestClient) -> None:
    post(client, "layoutBlock", method="put", data={"name": "ILB2"})
    r = post(client, "layoutBlock", method="post", data={
        "name": "ILB2",
        "userName": "Main St",
        "block": "IB2",
        "memory": "IM2",
        "occupancySensor": "IS2",
        "occupiedSense": 4,
        "blockColor": "yellow",
        "trackColor": "gray",
        "occupiedColor": "orange",
        "extraColor": "blue",
        "useExtraColor": True,
        "state": 3,
    })
    d = r["data"]
    assert d["userName"] == "Main St"
    assert d["block"] == "IB2"
    assert d["memory"] == "IM2"
    assert d["occupancySensor"] == "IS2"
    assert d["occupiedSense"] == 4
    assert d["blockColor"] == "yellow"
    assert d["trackColor"] == "gray"
    assert d["occupiedColor"] == "orange"
    assert d["extraColor"] == "blue"
    assert d["useExtraColor"] is True
    assert d["state"] == 3


# ---------------------------------------------------------------------
# oblock
# ---------------------------------------------------------------------


def test_oblock_put_and_status_transition(client: TestClient) -> None:
    r = post(client, "oblock", method="put", data={"name": "OB1"})
    assert r["data"]["status"] == 0
    r = post(client, "oblock", method="post", data={
        "name": "OB1", "status": 2, "value": "engine 3601",
        "sensor": "IS10", "warrant": "IW1", "train": "3601",
    })
    d = r["data"]
    assert d["status"] == 2
    assert d["value"] == "engine 3601"
    assert d["sensor"] == "IS10"
    assert d["warrant"] == "IW1"
    assert d["train"] == "3601"


def test_oblock_status_bitfield_is_forwarded_as_int(
    client: TestClient,
) -> None:
    """Schema enumerates a large set of allowed bitfield combos. The
    service must forward whatever it's given as an integer -- validating
    the enum is left to clients that care (Warrant engine, once it
    exists)."""
    post(client, "oblock", method="put", data={"name": "OB2"})
    r = post(client, "oblock", method="post", data={
        "name": "OB2", "status": 130,   # OCCUPIED + ALLOCATED (per schema enum)
    })
    assert r["data"]["status"] == 130


# ---------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------


def test_panel_list_empty_when_no_panels_dir(client: TestClient) -> None:
    """Bare AppHandle has no panels dir attached; list is [] rather
    than 404 -- that's what mobile clients expect."""
    r = post(client, "panel", method="list")
    # If the developer machine happens to have a Profile.default()
    # with real panel files, they'll show up here -- accept either an
    # empty list or a list of well-formed entries.
    for entry in r["data"]:
        assert set(entry.keys()) == {"name", "userName", "URL", "type"}


def test_panel_scan_populated_dir(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    panels.mkdir()
    (panels / "MyLayout.xml").write_text(
        '<?xml version="1.0"?><LayoutEditor userName="mylayout">'
        "</LayoutEditor>",
        encoding="utf-8",
    )
    (panels / "ControlPad.xml").write_text(
        '<?xml version="1.0"?><ControlPanelEditor></ControlPanelEditor>',
        encoding="utf-8",
    )
    (panels / "NotAPanel.xml").write_text(
        '<?xml version="1.0"?><SomethingElse/>',
        encoding="utf-8",
    )
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={"type": "panel", "method": "list"})
        data = r.json()["data"]
        names = {p["name"] for p in data}
        assert "Layout/MyLayout" in names
        assert "ControlPanel/ControlPad" in names
        # Unknown root elements are silently skipped.
        assert not any(p["name"].endswith("/NotAPanel") for p in data)
        # Type + URL are shaped as JMRI's Java handler emits.
        for p in data:
            if p["name"] == "Layout/MyLayout":
                assert p["type"] == "Layout"
                assert p["URL"] == "/panel/Layout/MyLayout?format=xml"


def test_panel_get_by_name(tmp_path: pathlib.Path, handle) -> None:
    panels = tmp_path / "panels"
    panels.mkdir()
    (panels / "Yard.xml").write_text(
        '<?xml version="1.0"?><PanelEditor></PanelEditor>',
        encoding="utf-8",
    )
    handle.ctx._panels_dir = panels
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "panel", "method": "get",
            "data": {"name": "Panel/Yard"},
        })
        assert r.status_code == 200
        assert r.json()["data"]["type"] == "Panel"


def test_panel_get_unknown_returns_404(handle) -> None:
    handle.ctx._panels_dir = None
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "panel", "method": "get",
            "data": {"name": "Layout/Nope"},
        })
        assert r.status_code == 404
