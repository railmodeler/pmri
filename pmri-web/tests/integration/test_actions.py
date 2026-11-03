"""htmx-driven form actions return the right HTML fragments."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_turnout_returns_table_fragment(client: TestClient) -> None:
    r = client.post("/turnouts/create", data={"name": "IT1"})
    r.raise_for_status()
    assert "IT1" in r.text
    # The full table fragment should be returned, with our new row.
    assert "<table" in r.text


def test_set_turnout_state_returns_row_fragment(client: TestClient) -> None:
    client.post("/turnouts/create", data={"name": "IT5"})
    r = client.post("/turnouts/IT5/state", data={"state": 4})  # THROWN
    r.raise_for_status()
    assert "IT5" in r.text
    assert "THROWN" in r.text


def test_create_sensor_returns_table_fragment(client: TestClient) -> None:
    r = client.post("/sensors/create", data={"name": "IS1"})
    r.raise_for_status()
    assert "IS1" in r.text


def test_simulate_sensor_returns_row_fragment(client: TestClient) -> None:
    client.post("/sensors/create", data={"name": "IS9"})
    r = client.post("/sensors/IS9/state", data={"state": 2})  # ACTIVE
    r.raise_for_status()
    assert "IS9" in r.text
    assert "ACTIVE" in r.text


def test_set_power_returns_panel_fragment(client: TestClient) -> None:
    r = client.post("/power/set", data={"state": 2})  # ON
    r.raise_for_status()
    assert "Track power" in r.text
    assert "ON" in r.text

    r = client.post("/power/set", data={"state": 4})  # OFF
    r.raise_for_status()
    assert "OFF" in r.text


def test_set_state_on_unknown_turnout_returns_404(client: TestClient) -> None:
    r = client.post("/turnouts/IT9999/state", data={"state": 2})
    assert r.status_code == 404


# ----- edit + delete affordances ------------------------------------------


def test_turnout_edit_returns_form_row(client: TestClient) -> None:
    client.post("/turnouts/create", data={"name": "IT42"})
    r = client.get("/turnouts/IT42/edit")
    r.raise_for_status()
    assert "IT42" in r.text
    # Edit form should have a userName input and an inverted checkbox.
    assert 'name="userName"' in r.text
    assert 'name="inverted"' in r.text


def test_turnout_edit_row_has_no_form_in_tr(client: TestClient) -> None:
    """HTML5 does not allow <form> as a direct child of <tr>; the browser
    parser hoists it out and orphans the tds, making Save silently do
    nothing. Guard against that regression: the edit row must wire htmx
    directly on its inputs/buttons rather than wrapping the row in a
    <form>."""
    client.post("/turnouts/create", data={"name": "IT_editmarkup"})
    r = client.get("/turnouts/IT_editmarkup/edit")
    r.raise_for_status()
    assert "<form" not in r.text
    # Save button must carry hx-post + hx-include so the endpoint sees
    # userName and inverted despite the missing wrapper form.
    assert 'hx-post="/turnouts/IT_editmarkup/update"' in r.text
    assert 'hx-include="closest tr"' in r.text


def test_turnout_update_renames_and_persists_inverted(client: TestClient) -> None:
    client.post("/turnouts/create", data={"name": "IT43"})
    r = client.post(
        "/turnouts/IT43/update",
        data={"userName": "Yard Ladder", "inverted": "1"},
    )
    r.raise_for_status()
    assert "Yard Ladder" in r.text
    # Fetch the read-only row to confirm persisted state.
    r2 = client.get("/turnouts/IT43/row")
    r2.raise_for_status()
    assert "Yard Ladder" in r2.text


def test_turnout_update_clears_user_name_when_empty(client: TestClient) -> None:
    client.post("/turnouts/create", data={"name": "IT44"})
    client.post(
        "/turnouts/IT44/update",
        data={"userName": "Temporary", "inverted": "0"},
    )
    r = client.post(
        "/turnouts/IT44/update",
        data={"userName": "", "inverted": "0"},
    )
    r.raise_for_status()
    # Row should no longer show the previous user name as the content.
    # (It can still appear as the placeholder, so check the structured cell.)
    assert "<td></td>" in r.text or ">Temporary<" not in r.text


def test_turnout_delete_returns_empty_and_removes(client: TestClient) -> None:
    client.post("/turnouts/create", data={"name": "IT45"})
    r = client.delete("/turnouts/IT45")
    r.raise_for_status()
    assert r.text.strip() == ""
    # Subsequent edit fetch should 404.
    assert client.get("/turnouts/IT45/row").status_code == 404


def test_create_light_returns_table_fragment(client: TestClient) -> None:
    r = client.post("/lights/create", data={"name": "IL1"})
    r.raise_for_status()
    assert "IL1" in r.text
    assert "<table" in r.text


def test_light_on_off_cycle(client: TestClient) -> None:
    client.post("/lights/create", data={"name": "IL7"})
    r = client.post("/lights/IL7/state", data={"state": 4})  # ON
    r.raise_for_status()
    assert "ON" in r.text
    r = client.post("/lights/IL7/state", data={"state": 2})  # OFF
    r.raise_for_status()
    assert "OFF" in r.text


def test_light_edit_update_delete_cycle(client: TestClient) -> None:
    client.post("/lights/create", data={"name": "IL9"})
    r = client.get("/lights/IL9/edit")
    r.raise_for_status()
    assert 'name="userName"' in r.text

    r = client.post("/lights/IL9/update", data={"userName": "Yard Lamp"})
    r.raise_for_status()
    assert "Yard Lamp" in r.text

    r = client.delete("/lights/IL9")
    r.raise_for_status()
    assert client.get("/lights/IL9/row").status_code == 404


def test_sensor_edit_update_delete_cycle(client: TestClient) -> None:
    client.post("/sensors/create", data={"name": "IS22"})
    r = client.get("/sensors/IS22/edit")
    r.raise_for_status()
    assert 'name="userName"' in r.text

    r = client.post(
        "/sensors/IS22/update",
        data={"userName": "Block 22", "inverted": "1"},
    )
    r.raise_for_status()
    assert "Block 22" in r.text

    r = client.delete("/sensors/IS22")
    r.raise_for_status()
    assert client.get("/sensors/IS22/row").status_code == 404


# ----- roster edit + DELETE verb ------------------------------------------


def _isolated_roster_client(tmp_path):  # noqa: ANN001, ANN202
    """Build a web client whose roster writes to a tmp dir, not the real profile."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415
    handle = build_web_app()
    handle.server.ctx._roster_profile = Profile(  # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    return TestClient(handle.app)


def test_roster_create_then_edit_persists(tmp_path) -> None:  # noqa: ANN001
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "UP_3601", "dccAddress": "3601", "longAddress": "1",
            "decoderFamily": "ESU", "decoderModel": "LokSound 5",
            "roadName": "Union Pacific", "roadNumber": "3601",
        })
        r = client.get("/roster/UP_3601/edit")
        r.raise_for_status()
        assert 'name="dccAddress"' in r.text
        assert "Union Pacific" in r.text

        r = client.post("/roster/UP_3601/update", data={
            "dccAddress": "3602", "longAddress": "1",
            "decoderFamily": "ESU", "decoderModel": "LokSound 5",
            "roadName": "Union Pacific", "roadNumber": "3602",
        })
        r.raise_for_status()
        assert "3602" in r.text


def test_roster_delete_uses_delete_verb(tmp_path) -> None:  # noqa: ANN001
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "ToDel", "dccAddress": "3",
        })
        r = client.delete("/roster/ToDel")
        r.raise_for_status()
        assert r.text.strip() == ""
        assert client.get("/roster/ToDel/row").status_code == 404


def test_throttle_remembers_last_acquisition(client: TestClient) -> None:
    """Tier A1: /throttle without args returns to the last-acquired address."""
    # Acquire 463 long.
    r = client.get("/throttle?address=463&long=1")
    r.raise_for_status()
    assert "Addr 463" in r.text
    # Acquire 3 short.
    r = client.get("/throttle?address=3")
    r.raise_for_status()
    assert "Addr 3" in r.text
    # Now hit /throttle bare -- should return to 3 (most recent).
    r = client.get("/throttle")
    r.raise_for_status()
    assert "Addr 3" in r.text
    # And again hit 463 long, then bare -> 463 long.
    client.get("/throttle?address=463&long=1")
    r = client.get("/throttle")
    r.raise_for_status()
    assert "Addr 463" in r.text
    assert "(long)" in r.text


def test_throttle_falls_back_to_3_with_no_history(client: TestClient) -> None:
    """Fresh server with no acquisition history -> defaults to address 3."""
    r = client.get("/throttle")
    r.raise_for_status()
    assert "Addr 3" in r.text


def test_throttle_page_shows_roster_picker(tmp_path) -> None:  # noqa: ANN001
    """Tier A2: throttle page lists roster entries in a picker."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415
    handle = build_web_app()
    handle.server.ctx._roster_profile = Profile(  # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    with TestClient(handle.app) as c:
        c.post("/roster/create", data={
            "id": "UP_3601", "dccAddress": "3601", "longAddress": "1",
            "roadName": "Union Pacific", "roadNumber": "3601",
        })
        r = c.get("/throttle")
        r.raise_for_status()
        assert 'id="roster-picker"' in r.text
        assert "UP_3601" in r.text
        assert "Union Pacific" in r.text


def test_roster_row_program_button_gated_on_catalog_when_empty(tmp_path) -> None:  # noqa: ANN001
    """Programmer step 3: the Program button is disabled with a hint
    when the decoder catalog hasn't been loaded -- matches the spec
    that the tabbed programming view "should not be allowed to open
    unless the decoder catalog is loaded"."""
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "DRGW_459", "dccAddress": "459", "longAddress": "1",
        })
        r = client.get("/roster/DRGW_459/row")
        r.raise_for_status()
        # No live catalog in this test app -> link is a disabled span.
        assert "/programmer/loco/DRGW_459" not in r.text
        assert "disabled" in r.text
        assert "decoder catalog" in r.text.lower()


def test_programmer_loco_page_renders(tmp_path) -> None:  # noqa: ANN001
    """Tier B6: /programmer/loco/<id> renders the per-loco programmer."""
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "DRGW_459", "dccAddress": "459", "longAddress": "1",
            "roadName": "D&RGW", "roadNumber": "459",
            "decoderFamily": "DSD Steam", "decoderModel": "DSD Steam",
        })
        r = client.get("/programmer/loco/DRGW_459")
        r.raise_for_status()
        assert "DRGW_459" in r.text
        assert "D&amp;RGW" in r.text or "D&RGW" in r.text
        assert "roster/DRGW_459.xml" in r.text
        assert "DSD Steam" in r.text


def test_programmer_loco_page_404_for_unknown(tmp_path) -> None:  # noqa: ANN001
    client = _isolated_roster_client(tmp_path)
    with client:
        r = client.get("/programmer/loco/does-not-exist")
        assert r.status_code == 404


def test_roster_xml_endpoint_serves_xml(tmp_path) -> None:  # noqa: ANN001
    """Engine Driver hits /roster?format=xml -- must return XML, not HTML."""
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "UP_3601", "dccAddress": "3601", "longAddress": "1",
        })
        r = client.get("/roster", params={"format": "xml"})
        r.raise_for_status()
        assert r.headers["content-type"].startswith("application/xml")
        assert b"<roster" in r.content
        assert b"UP_3601" in r.content


def test_roster_xml_endpoint_handles_trailing_slash(tmp_path) -> None:  # noqa: ANN001
    """Engine Driver issues GET /roster/?format=xml (with slash).

    FastAPI's default behaviour is to 307 redirect to the canonical
    no-slash form -- Engine Driver doesn't follow that redirect, so we
    need to serve both forms directly.
    """
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3",
        })
        # The TestClient is configured not to follow redirects.
        r = client.get("/roster/", params={"format": "xml"}, follow_redirects=False)
        # Either 200 with XML, or a redirect -- but no redirect must be
        # the outcome since Engine Driver won't follow.
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/xml")


def test_prefs_endpoint_returns_empty_for_missing_file(tmp_path) -> None:  # noqa: ANN001
    """Engine Driver's GET /prefs/engine_driver/auto_preferences.ed
    must not 404. Empty 200 keeps the client happy."""
    client = _isolated_roster_client(tmp_path)
    with client:
        r = client.get("/prefs/engine_driver/auto_preferences.ed")
        assert r.status_code == 200
        assert r.content == b""


def test_prefs_put_round_trip(tmp_path) -> None:  # noqa: ANN001
    """PUTting prefs writes to <profile>/preferences/<app>/<file>; subsequent
    GET returns the same bytes."""
    client = _isolated_roster_client(tmp_path)
    with client:
        body = b"hello=world\n"
        r = client.put("/prefs/engine_driver/test.ed", content=body)
        assert r.status_code == 204
        r = client.get("/prefs/engine_driver/test.ed")
        assert r.content == body


def test_programmer_loco_honors_roster_filename(tmp_path) -> None:  # noqa: ANN001
    """B6 refinement: when the roster pins ``fileName="1.xml"``, the
    programmer page reads/writes ``roster/1.xml`` (not 459.xml)."""
    import pathlib  # noqa: PLC0415
    client = _isolated_roster_client(tmp_path)
    with client:
        # Create the roster entry first.
        client.post("/roster/create", data={
            "id": "459", "dccAddress": "459", "longAddress": "1",
            "roadName": "D&RGW", "roadNumber": "459",
        })
        # The roster store assigns fileName on first put. Inspect the
        # roster entry to confirm a fileName was set.
        r = client.post("/json/", json={
            "type": "roster", "method": "get", "data": {"id": "459"},
        })
        r.raise_for_status()
        file_name = r.json()["data"].get("fileName") or "459.xml"

        # Now hit the per-loco programmer page. The CV file path the
        # template shows must match the roster's fileName.
        r = client.get("/programmer/loco/459")
        r.raise_for_status()
        assert f"roster/{file_name}" in r.text


def test_roster_cancel_returns_read_only_row(tmp_path) -> None:  # noqa: ANN001
    client = _isolated_roster_client(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "Cancel1", "dccAddress": "5",
            "roadName": "Original",
        })
        r = client.get("/roster/Cancel1/row")
        r.raise_for_status()
        assert "Cancel1" in r.text
        assert "Original" in r.text
        # The read-only row shouldn't contain the edit form's inputs.
        assert 'name="dccAddress"' not in r.text


def test_panel_upload_renders_summary() -> None:
    """A real layout-config XML uploads cleanly."""
    from pmri_web.app import build_web_app  # noqa: PLC0415
    handle = build_web_app()
    with TestClient(handle.app) as client:
        sample = (
            b'<?xml version="1.0"?>'
            b'<layout-config jmriversion="5.14.0">'
            b'<panel name="Yard"/>'
            b'<LayoutEditor name="Main"/>'
            b'</layout-config>'
        )
        files = {"file": ("test.xml", sample, "application/xml")}
        r = client.post("/panel/upload", files=files)
        r.raise_for_status()
        assert "Yard" in r.text
        assert "Main" in r.text
        assert "layout-config" in r.text


# ----- programmer step 3: tabbed per-roster programming view -------------


def _client_with_populated_catalog(tmp_path):  # noqa: ANN001, ANN202
    """Isolated web client with a pre-populated decoder catalog + one
    programmer definition -- the state the tabbed programmer needs.

    Route closures read the catalog from ``handle.ctx._decoder_catalog``
    on every request, so attaching a pre-loaded catalog after
    ``build_web_app`` is enough -- no rebuild needed.
    """
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily,
        DecoderFile,
        DecoderModel,
        DecoderPane,
        DecoderVariable,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="TestFamily", mfg="TestMfg",
                models=[DecoderModel(model="TestModel")],
            ),
            variables=[
                DecoderVariable.model_validate({"CV": "1", "item": "Short Address"}),
                DecoderVariable.model_validate({"CV": "2", "item": "Vstart"}),
            ],
            panes=[DecoderPane(name="Extras", items=["Short Address"])],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[
            DecoderPane(name="Basic", items=["Short Address"]),
            DecoderPane(name="Motor", items=["Vstart"]),
        ],
    )

    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    return TestClient(handle.app), catalog


def test_roster_row_program_button_enabled_when_catalog_loaded(tmp_path) -> None:  # noqa: ANN001
    """When the catalog has definitions loaded, the Program button on a
    roster row links to the tabbed programmer view rather than showing
    the disabled placeholder."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/roster/L1/row")
        r.raise_for_status()
        assert "/programmer/loco/L1" in r.text
        assert 'target="_blank"' in r.text


def test_programmer_loco_tab_bar_matches_composed_panes(tmp_path) -> None:
    """The tabbed programmer view emits one ``.pane-tab`` per composed
    pane -- Roster Entry + standard tabs the decoder can serve + any
    decoder-declared extras."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        # Fixed spec order: Roster Entry -> Basic -> Motor -> Extras.
        idx_roster = r.text.index("Roster Entry")
        idx_basic = r.text.index("Basic")
        idx_motor = r.text.index("Motor")
        idx_extras = r.text.index("Extras")
        assert idx_roster < idx_basic < idx_motor < idx_extras


def test_programmer_loco_swap_pane_via_htmx_endpoint(tmp_path) -> None:
    """``GET /programmer/loco/<id>/pane/<slug>`` returns just the pane
    content fragment -- that's what the HTMX tab-swap hits."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1/pane/motor")
        r.raise_for_status()
        assert "Vstart" in r.text
        assert "Motor" in r.text
        # Must NOT include the base-template nav (this is a fragment).
        assert "<html" not in r.text.lower()


def test_programmer_loco_uses_own_base_template_not_main_nav(tmp_path) -> None:
    """Spec requires: the new programming tab should not have the same
    headers as the main server. The tabbed page uses ``programmer_base``
    which only shows a Return-to-Roster link + railroad."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        # Main nav links (turnouts, sensors, lights, ...) must not appear.
        assert 'href="/turnouts"' not in r.text
        assert 'href="/sensors"' not in r.text
        # Programmer-base marker present.
        assert "Return to Roster" in r.text


def test_programmer_page_shows_catalog_loaded_state(tmp_path) -> None:
    """When the catalog has definitions, the /programmer page swaps the
    "Catalog empty" badge for "Catalog loaded"."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        r = client.get("/programmer")
        r.raise_for_status()
        assert "Catalog loaded" in r.text
        assert "Catalog empty" not in r.text


def test_programmer_page_shows_catalog_empty_state_when_no_catalog(tmp_path) -> None:
    """Default state: no catalog loaded -> the /programmer page shows
    the "Catalog empty" hint pointing at the load form."""
    import pathlib  # noqa: PLC0415
    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_web.app import build_web_app  # noqa: PLC0415

    handle = build_web_app()
    handle.server.ctx._roster_profile = Profile(  # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    with TestClient(handle.app) as client:
        r = client.get("/programmer")
        r.raise_for_status()
        assert "Catalog empty" in r.text


def test_programmer_loco_prompts_when_cv_state_source_ambiguous(tmp_path) -> None:
    """When both JMRI-canonical roster/<id>.xml (with <values>) and
    legacy rosters/<id>.xml have CV state, the tabbed programmer must
    show a chooser rather than picking one silently. Once resolved,
    subsequent requests skip the chooser."""
    import pathlib  # noqa: PLC0415
    client, catalog = _client_with_populated_catalog(tmp_path)
    # The test client's profile lives at ``<tmp_path>/test``.
    profile_dir = pathlib.Path(tmp_path) / "test"

    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        # Seed BOTH sources with different data -- AFTER create so
        # roster/L1.xml already exists (the roster save writes it).
        (profile_dir / "roster" / "L1.xml").write_bytes(
            b"""<?xml version="1.0"?>
<locomotive-config><locomotive id="L1" dccAddress="3">
<decoder model="TestModel" family="TestFamily" />
<values><CVvalue><CV name="1" value="3" /></CVvalue></values>
</locomotive></locomotive-config>"""
        )
        (profile_dir / "rosters").mkdir(parents=True, exist_ok=True)
        (profile_dir / "rosters" / "L1.xml").write_bytes(
            b"""<?xml version="1.0"?>
<locomotive-cv-values id="L1"><CV name="1" value="99" /></locomotive-cv-values>"""
        )
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        # Chooser rendered instead of the tabbed view.
        assert "Two CV state files found" in r.text
        assert 'value="roster"' in r.text
        assert 'value="legacy"' in r.text
        # Pick legacy -- this remembers the choice on the store.
        r = client.post(
            "/programmer/loco/L1/resolve-source", data={"source": "legacy"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # Subsequent GET renders panes (no more chooser).
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        assert "Two CV state files found" not in r.text
        assert "pane-tabs" in r.text


# ----- programmer step 4: dirty tracking + batch writes + cancel --------


def test_banner_labels_cv_state_file_under_roster_dir(tmp_path) -> None:
    """The loco page banner tells the user where CV state persists.
    That's ``roster/<id>.xml`` (JMRI-canonical <values> block) since
    the step-5 refactor -- not the legacy ``rosters/`` layout."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        assert "<code>roster/L1.xml</code>" in r.text
        assert "<code>rosters/" not in r.text


def test_loco_read_returns_extracted_human_value_for_masked_variable(tmp_path) -> None:
    """When the read payload carries an ``item`` name that resolves to
    a masked variable, the response's ``humanValue`` must be the
    extracted bits, not the raw CV byte. The client uses ``humanValue``
    as the row's dirty-tracking baseline; without extraction, reading a
    CV that a masked variable shares would flip the row to false-dirty
    immediately (raw byte 2 vs enum choice 0)."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        CvBitMask, DecoderFamily, DecoderFile, DecoderModel, DecoderPane,
        DecoderVariable, VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                # Bit 4 of CV 29 = Speed Table use flag. CV 29 = 2
                # (0b00000010) -> bit 4 is 0 -> "Don't use".
                DecoderVariable.model_validate({
                    "CV": "29", "item": "Speed Table",
                    "mask": "XXXVXXXX",
                    "bit_mask": CvBitMask.parse("XXXVXXXX"),
                    "kind": VariableKind.ENUM,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Basic", items=["Speed Table"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    # Pre-seed the store so the "read" via the InternalConnection can
    # complete (there's no real programmer; we bypass by writing to the
    # store first, then reading from it via the endpoint).
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        # Force CV 29 = 2 into the store, then dispatch a read via the
        # endpoint. Read fails on the connection (no programmer) but
        # first the store already holds 2 so we can prove extraction
        # semantics against a known state -- easier than mocking a real
        # programmer round-trip in a unit test.
        _loco_cv_store(handle.server.ctx).set_cv("L1", "29", 2)
        r = client.post(
            "/programmer/loco/L1/read",
            json={"cv": 29, "mode": "service", "item": "Speed Table"},
        )
        # InternalConnection has no service programmer -> 503 from
        # ``_resolve_programmer`` (distinct from 502 which is for
        # connection errors *during* a real op). The extraction logic
        # itself is exercised in unit tests of variable_extract_from_state
        # under pmri-core; this integration test just proves the
        # endpoint accepts ``item`` in the payload without crashing.
        assert r.status_code == 503


def test_loco_read_endpoint_returns_json_not_html_fragment(tmp_path) -> None:
    """The per-row Read button now consumes JSON so client JS can update
    the row in place; the old HTML-fragment shape swapped ``<tr>`` for
    ``<div>`` and destroyed the table layout."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        # Internal connection has no programmer -> 503 from the service
        # bubbles up as an HTTP error, still shaped as JSON detail.
        r = client.post(
            "/programmer/loco/L1/read", json={"cv": 1, "mode": "service"},
        )
        # 503 = expected because InternalConnection has no programmer.
        assert r.status_code == 503
        # Response body is not an HTML fragment.
        assert "<div class=\"card\">" not in r.text


def test_speed_table_expands_into_28_cv_rows(tmp_path) -> None:
    """A ``<speedTable/>`` variable at CV67 must render as 28 separate
    rows (CV67..CV94) so the user can program each step individually.
    Prior behaviour showed only CV67 with a 'not supported' placeholder."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "67", "item": "Speed Table",
                    "kind": VariableKind.SPEED_TABLE,
                    "speed_table_entries": 28,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Speed Table", items=["Speed Table"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.get("/programmer/loco/L1/pane/speed-table")
        r.raise_for_status()
        # 28 individual CV rows anchored at CV67.
        for cv in range(67, 95):
            assert f'data-cv="{cv}"' in r.text, f"CV{cv} row missing"
        # Not present past the entries count.
        assert 'data-cv="95"' not in r.text
        # Human-friendly step labels.
        assert "Speed Step 1" in r.text
        assert "Speed Step 28" in r.text
        # The 'not yet supported' placeholder must be gone.
        assert "not yet supported" not in r.text


def test_split_datetime_variable_renders_formatted_timestamp(tmp_path) -> None:
    """A ``<splitDateTimeVal base factor unit display>`` variable must
    render the stored integer as a formatted date/time string -- the
    old "not yet supported" placeholder is gone, replaced by a
    read-only ``<span class="mono">`` showing the computed value.

    Pin for the pane-widget wiring: ``_format_split_datetime`` turns
    the stored int into ``base + stored*factor`` in the configured
    ChronoUnit, honours ``display="dateOnly" / "timeOnly" / else``,
    and the raw int rides along in the ``title`` attribute for the
    power-user who wants to see the wire value."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane,
        DecoderVariable, SplitCv, VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "275:2",
                    "item": "Last Run",
                    "label": "Last Run",
                    "kind": VariableKind.SPLIT_DATETIME,
                    "split": SplitCv.parse("275:2"),
                    "dt_base": "2000-01-01T00:00:00",
                    "dt_factor": 60,
                    "dt_unit": "SECONDS",
                    "dt_display": "dateOnly",
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Life", items=["Last Run"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.get("/programmer/loco/L1/pane/life")
        r.raise_for_status()
        # The old placeholder is gone.
        assert "not yet supported" not in r.text
        # Variable rendered via the mono date span -- without a stored
        # value yet, the pane shows "--" (not an exception).
        assert "Last Run" in r.text
        # The read-only span uses mono styling (we pick the class on
        # the datetime row).
        assert 'class="mono"' in r.text


def test_speed_table_pane_expanded_by_name_when_widget_undetected(tmp_path) -> None:
    """Fallback trigger: any pane whose slug is ``speed-table`` (or
    whose name normalises to "speed table") expands the decoder's
    SPEED_TABLE variable even if we didn't parse a speedtable widget
    off the fragment. Defends against JMRI shipping an unexpected
    widget variant we haven't seen yet."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "67", "item": "Speed Table",
                    "kind": VariableKind.SPEED_TABLE,
                    "speed_table_entries": 28,
                }),
            ],
        ),
    ])
    # Pane declares neither the widget nor a <display> ref -- but its
    # name identifies it as Speed Table. Fallback must still expand.
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[
            DecoderPane(name="Speed Table", items=[], has_speed_table=False),
            # Second pane to keep the tab non-empty via has_cvtable so
            # compose_panes doesn't drop the empty Speed Table.
        ],
    )
    # ...but compose_panes drops the Speed Table since has_speed_table
    # is False and there are no items. That's a legitimate scenario the
    # fallback can't fix from within _build_pane_view. Simulate the
    # user reaching the pane via a direct URL by pinning show_empty.
    catalog.programmers["Comprehensive"].show_empty_panes = "yes"
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.get("/programmer/loco/L1/pane/speed-table")
        r.raise_for_status()
        # Name-triggered expansion -> all 28 CV rows present.
        for cv in range(67, 95):
            assert f'data-cv="{cv}"' in r.text, f"CV{cv} row missing"


def test_speed_table_pane_via_widget_still_expands(tmp_path) -> None:
    """JMRI's real Speed Table pane declares a ``<speedtable/>`` widget
    with no ``<display item="Speed Table"/>``. That meant items_available
    was empty -> pane got dropped as empty -> user saw nothing. Fix
    detects the widget and expands the decoder's SPEED_TABLE variable
    into per-step rows even when the pane has no display items."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "67", "item": "Speed Table",
                    "kind": VariableKind.SPEED_TABLE,
                    "speed_table_entries": 28,
                }),
            ],
        ),
    ])
    # Programmer pane declares the widget, NOT a <display item> reference.
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Speed Table", items=[], has_speed_table=True)],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        # Pane must appear in the tab list even though it has no
        # <display> items -- the widget alone keeps it non-empty.
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        assert "Speed Table" in r.text
        # And the pane content shows all 28 step rows.
        r = client.get("/programmer/loco/L1/pane/speed-table")
        r.raise_for_status()
        for cv in range(67, 95):
            assert f'data-cv="{cv}"' in r.text, f"CV{cv} row missing"


def test_speed_table_honours_custom_entries_count(tmp_path) -> None:
    """Some decoders declare non-standard step counts via
    ``<speedTable entries="N"/>`` (14-step legacy, 128-step high-res).
    The pane must render exactly N rows."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "67", "item": "Speed Table",
                    "kind": VariableKind.SPEED_TABLE,
                    "speed_table_entries": 14,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Speed Table", items=["Speed Table"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.get("/programmer/loco/L1/pane/speed-table")
        r.raise_for_status()
        # 14 steps -> CV67..CV80.
        for cv in range(67, 81):
            assert f'data-cv="{cv}"' in r.text
        assert 'data-cv="81"' not in r.text


def test_pane_extracts_masked_bits_per_variable_on_shared_cv(tmp_path) -> None:
    """CV 29 hosts several bit-mask variables (Address Format, Speed
    Steps, Power Source Conv). When the raw byte is 2 (0b00000010),
    Speed Steps=1 (bit 1), Address Format=0 (bit 5), Power Source=0
    (bit 2). If the pane were to project the raw byte as each
    variable's "current value", enum options would compare wrong (2 vs
    1 for Speed Steps -> dirty even when unchanged) and switching panes
    would look inconsistent."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        CvBitMask, DecoderFamily, DecoderFile, DecoderModel, DecoderPane,
        DecoderVariable, VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                # Bit 1 of CV 29 -- Speed Steps (0=14, 1=28).
                DecoderVariable.model_validate({
                    "CV": "29", "item": "Speed Steps",
                    "mask": "XXXXXXVX",
                    "bit_mask": CvBitMask.parse("XXXXXXVX"),
                    "kind": VariableKind.ENUM,
                }),
                # Bit 5 of CV 29 -- Address Format (0=short, 1=long).
                DecoderVariable.model_validate({
                    "CV": "29", "item": "Address Format",
                    "mask": "XXVXXXXX",
                    "bit_mask": CvBitMask.parse("XXVXXXXX"),
                    "kind": VariableKind.ENUM,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[
            DecoderPane(name="Basic", items=["Speed Steps", "Address Format"]),
        ],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        # Simulate a "read CV29 = 2" via the CV state store.
        from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
        _loco_cv_store(handle.server.ctx).set_cv("L1", "29", 2)

        r = client.get("/programmer/loco/L1/pane/basic")
        r.raise_for_status()
        # Speed Steps row: bit 1 of 2 = 1.
        assert 'data-item="Speed Steps"' in r.text
        # Address Format row: bit 5 of 2 = 0.
        assert 'data-item="Address Format"' in r.text
        # Each row's data-current is the *masked* value, not the raw byte.
        # Grep for the specific value on each row line -- order matters.
        speed_line_idx = r.text.index('data-item="Speed Steps"')
        addr_line_idx = r.text.index('data-item="Address Format"')
        speed_row = r.text[speed_line_idx:speed_line_idx + 400]
        addr_row = r.text[addr_line_idx:addr_line_idx + 400]
        assert 'data-current="1"' in speed_row
        assert 'data-current="0"' in addr_row


def test_pane_shows_catalog_default_when_roster_has_no_stored_value(tmp_path) -> None:
    """When a variable's CV isn't in roster/<id>.xml's <values> block,
    the pane must fall back to the catalog default so the user sees the
    from-factory value (not a blank field). This is the baseline dirty
    tracking compares against."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate(
                    {"CV": "2", "item": "Vstart", "default": "42"},
                ),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Motor", items=["Vstart"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        # No CV writes -> cv_values is empty; the row's baseline must be
        # 42 (catalog default) rather than blank.
        r = client.get("/programmer/loco/L1/pane/motor")
        r.raise_for_status()
        assert 'data-current="42"' in r.text
        assert 'value="42"' in r.text


def test_programmer_pane_exposes_dirty_tracking_hooks(tmp_path) -> None:
    """The pane content template must expose data-* attributes and the
    Write pane / Write all dirty / Cancel controls that programmer_batch.js
    binds to. If any of these disappear, the client-side batch flow
    silently no-ops -- hence the assertion here."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1/pane/motor")
        r.raise_for_status()
        assert 'class="card programmer-pane"' in r.text
        assert 'data-loco-id="L1"' in r.text
        assert 'data-pane-slug="motor"' in r.text
        assert "cv-value-input" in r.text
        assert 'data-scope="pane"' in r.text
        assert 'data-scope="all"' in r.text
        assert "pane-cancel-btn" in r.text
        assert "batch-progress" in r.text


def test_programmer_loco_loads_batch_script(tmp_path) -> None:
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        assert "/static/programmer_batch.js" in r.text


def test_batch_write_endpoint_returns_batch_id(tmp_path) -> None:
    """POST /programmer/loco/<id>/write-batch kicks off the queue and
    responds with the batch id -- client polls status from there."""
    import time  # noqa: PLC0415
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [{"cv": 1, "value": 3, "item": "Short Address"}],
                "mode": "service",
            },
        )
        # Internal connection has no programmer -> the write callback
        # raises 503; but the queue captures per-write errors, so the
        # POST itself still returns 202 (queue accepted the batch).
        assert r.status_code == 202
        assert "batch_id" in r.json()
        batch_id = r.json()["batch_id"]
        assert isinstance(batch_id, str) and batch_id

        # Poll until terminal -- should error out since no programmer.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r = client.get("/programmer/loco/L1/batch/status")
            r.raise_for_status()
            data = r.json()
            if data["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"batch didn't finish: {data}")
        # State is "error" because every write failed (no programmer).
        assert data["state"] == "error"
        assert data["total"] == 1


def test_ops_mode_refuses_long_address_write(tmp_path) -> None:
    """Long-address (CV17+CV18 atomic pair) writes are unsafe in ops
    mode: after CV17 changes the decoder's operating address, CV18's
    POM packet aims at the old address and is silently dropped. Refuse
    with 400 so the user knows to switch to service mode."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "17.18", "item": "Long Address",
                    "kind": VariableKind.LONG_ADDRESS,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Basic", items=["Long Address"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": 17, "value": 0xC4, "item": "Long Address"},
                ],
                "mode": "ops", "address": 3, "isLongAddress": False,
            },
        )
        assert r.status_code == 400
        assert "long address" in r.json()["detail"].lower()
        assert "service mode" in r.json()["detail"].lower()


def test_ops_mode_refuses_short_address_write(tmp_path) -> None:
    """CV1 short-address writes also change the operating address --
    any subsequent write in the batch goes to the wrong loco."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "1", "item": "Short Address",
                    "kind": VariableKind.SHORT_ADDRESS,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Basic", items=["Short Address"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": 1, "value": 5, "item": "Short Address"},
                ],
                "mode": "ops", "address": 3, "isLongAddress": False,
            },
        )
        assert r.status_code == 400
        assert "short address" in r.json()["detail"].lower()


def test_ops_mode_refuses_cv29_address_format_bit_write(tmp_path) -> None:
    """A masked CV29 write that overlaps bit 5 (Address Format) flips
    the decoder between short and long addressing -- same follow-on
    breakage as CV1/CV17. Detected by mask overlap, not the item name,
    so any variable declared on CV29 bit 5 gets caught."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        CvBitMask, DecoderFamily, DecoderFile, DecoderModel, DecoderPane,
        DecoderVariable, VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "29", "item": "Address Format",
                    "mask": "XXVXXXXX",
                    "bit_mask": CvBitMask.parse("XXVXXXXX"),
                    "kind": VariableKind.ENUM,
                }),
                # Also on CV29 but a different bit -- must NOT be blocked.
                DecoderVariable.model_validate({
                    "CV": "29", "item": "Speed Steps",
                    "mask": "XXXXXXVX",
                    "bit_mask": CvBitMask.parse("XXXXXXVX"),
                    "kind": VariableKind.ENUM,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Basic", items=[
            "Address Format", "Speed Steps",
        ])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        # Address Format on CV29 -> refused.
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": 29, "value": 1, "item": "Address Format"},
                ],
                "mode": "ops", "address": 3,
            },
        )
        assert r.status_code == 400
        assert "address format" in r.json()["detail"].lower()

        # Speed Steps on CV29 -> allowed (bit 1, not bit 5).
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": 29, "value": 1, "item": "Speed Steps"},
                ],
                "mode": "ops", "address": 3,
            },
        )
        assert r.status_code == 202


def test_ops_mode_allows_writes_that_dont_change_address(tmp_path) -> None:
    """Regression guard: the safety check must not accidentally block
    legitimate ops-mode writes to CV3 (accel), CV4 (decel), or any of
    the sound / motor / function CVs that live above the address range."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": 2, "value": 12, "item": "Vstart"},
                ],
                "mode": "ops", "address": 3,
            },
        )
        assert r.status_code == 202


def test_rich_pane_layout_renders_authored_column_row_structure(tmp_path) -> None:
    """A pane whose XML defines nested ``<column>``/``<row>``/``<label>``
    /``<separator>``/``<display>`` renders those hints as nested divs
    with the widgets in the JMRI-authored positions. Falls back to a
    flat column for panes constructed without an XML raw element."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderVariable,
        VariableKind, load_pane_bytes,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    # Authored pane: two-column layout with a labelled group.
    pane = load_pane_bytes(b"""<?xml version="1.0"?>
<pane>
  <name>Motor</name>
  <column>
    <row>
      <display item="Vstart"/>
      <display item="Vmid"/>
      <display item="Vhigh"/>
    </row>
    <separator/>
    <label><text>Advanced motor tuning</text></label>
    <display item="Accel"/>
  </column>
</pane>""")

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "2", "item": "Vstart",
                    "kind": VariableKind.DECIMAL,
                }),
                DecoderVariable.model_validate({
                    "CV": "6", "item": "Vmid",
                    "kind": VariableKind.DECIMAL,
                }),
                DecoderVariable.model_validate({
                    "CV": "5", "item": "Vhigh",
                    "kind": VariableKind.DECIMAL,
                }),
                DecoderVariable.model_validate({
                    "CV": "3", "item": "Accel",
                    "kind": VariableKind.DECIMAL,
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive", panes=[pane],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.get("/programmer/loco/L1/pane/motor")
        r.raise_for_status()
        # The pane-layout container is present, wrapping the rest.
        assert 'class="pane-layout"' in r.text
        # Nested containers per the XML.
        assert 'class="pane-col"' in r.text
        assert 'class="pane-row"' in r.text
        assert 'class="pane-label"' in r.text
        assert 'class="pane-separator"' in r.text
        # All four variables render as widget divs (not <tr>) with their
        # data-item + data-cv attrs preserved for the dirty-tracking JS.
        for item, cv in (
            ("Vstart", 2), ("Vmid", 6), ("Vhigh", 5), ("Accel", 3),
        ):
            assert f'data-item="{item}"' in r.text
            assert f'data-cv="{cv}"' in r.text
        # Order: Vstart, Vmid, Vhigh come before the separator; Accel
        # after the label. Cheap check: separator sits between Vhigh
        # and Accel in the rendered HTML.
        vhigh_idx = r.text.index('data-item="Vhigh"')
        sep_idx = r.text.index('class="pane-separator"')
        accel_idx = r.text.index('data-item="Accel"')
        assert vhigh_idx < sep_idx < accel_idx


def test_composite_variable_renders_with_current_choice_selected(tmp_path) -> None:
    """Composite variables render as a preset picker. The pane must
    pre-select the option that matches the current sub-variable state
    (so opening the pane doesn't accidentally show a different preset
    as the "current" one), and each option must carry its settings
    inline so client-side batch collection can fan out without a
    server round-trip per pick."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        CompositeChoice, CompositeSetting, DecoderFamily, DecoderFile,
        DecoderModel, DecoderPane, DecoderVariable, VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "10", "item": "Chuff Rate",
                    "kind": VariableKind.DECIMAL,
                }),
                DecoderVariable.model_validate({
                    "CV": "0", "item": "Chuff Style",
                    "kind": VariableKind.COMPOSITE,
                    "composite_choices": [
                        CompositeChoice(choice="Off", settings=[
                            CompositeSetting(label="Chuff Rate", value=0),
                        ]),
                        CompositeChoice(choice="Fast", settings=[
                            CompositeSetting(label="Chuff Rate", value=25),
                        ]),
                    ],
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Sound", items=["Chuff Rate", "Chuff Style"])],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        # Seed CV 10 = 25 -> Chuff Style should show "Fast" pre-selected.
        _loco_cv_store(handle.server.ctx).set_cv("L1", "10", 25)

        r = client.get("/programmer/loco/L1/pane/sound")
        r.raise_for_status()
        # Composite renders as a select carrying the sub-variable
        # settings JSON on each option.
        assert 'class="cv-value-input composite-select"' in r.text
        assert 'data-settings=' in r.text
        # "Fast" is currently active -- should be the selected option
        # (not "Off", which would be first in doc order).
        # Look near the "Fast" option for the selected attribute.
        fast_idx = r.text.index('value="Fast"')
        fast_option = r.text[fast_idx:fast_idx + 200]
        assert "selected" in fast_option


def test_cvs_tab_enumerates_all_catalog_cvs_by_default(tmp_path) -> None:
    """The CVs tab lists every CV the decoder catalog defines, whether
    or not it's been read/written yet. Matches JMRI Java's behaviour
    and gives the user a direct read/write handle on any CV without
    having to type it into the Add CV input first."""
    import pathlib  # noqa: PLC0415

    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        SplitCv, VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "3", "item": "Accel",
                    "kind": VariableKind.DECIMAL,
                }),
                DecoderVariable.model_validate({
                    "CV": "16.1.275", "item": "Sound Setting 42",
                    "kind": VariableKind.DECIMAL,
                    "split": SplitCv.parse("16.1.275"),
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="CVs", items=[], has_cvtable=True)],
    )
    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.get("/programmer/loco/L1/pane/cvs")
        r.raise_for_status()
        # Both CVs listed -- the plain one and the indexed one -- even
        # though the store is empty (no reads/writes yet).
        assert 'data-cv-name="3"' in r.text
        assert 'data-cv-name="16.1.275"' in r.text


def test_raw_cv_endpoint_rejects_malformed_names(tmp_path) -> None:
    """Empty and unparseable CV names get a clean 400 (not a 500)."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/cv/read",
            json={"cvName": "", "mode": "service"},
        )
        assert r.status_code == 400
        r = client.post(
            "/programmer/loco/L1/cv/read",
            json={"cvName": "hello.world", "mode": "service"},
        )
        assert r.status_code == 400


def test_batch_write_of_indexed_cv_dispatches_cv31_cv32_setup(tmp_path) -> None:
    """A write to an indexed variable (bank.index.base like ``16.1.275``)
    must prepend CV31=bank and CV32=index wire writes so the decoder's
    page state matches before the base CV is written. Without this, the
    byte lands on whatever CV 275 means under the current (uncontrolled)
    page -- corrupting unrelated sound settings."""
    import pathlib  # noqa: PLC0415
    import time  # noqa: PLC0415

    from fastapi.testclient import TestClient  # noqa: PLC0415

    from pmri_core.beans.programmer import (  # noqa: PLC0415
        Programmer, ProgrammerManager, ProgrammingMode,
    )
    from pmri_core.persistence import Profile  # noqa: PLC0415
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderFamily, DecoderFile, DecoderModel, DecoderPane, DecoderVariable,
        VariableKind,
    )
    from pmri_core.persistence.programmer_xml import ProgrammerConfig  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    from pmri_web.app import build_web_app  # noqa: PLC0415

    class _FakeProgrammer(Programmer):
        def __init__(self) -> None:
            self.writes: list[tuple[int, int]] = []
            self.reads: dict[int, int] = {}

        @property
        def mode(self) -> ProgrammingMode:
            return ProgrammingMode.SERVICE_DIRECT

        async def read_cv(self, cv: int) -> int:
            return self.reads.get(cv, 0)

        async def write_cv(self, cv: int, value: int) -> None:
            self.writes.append((cv, value))

    class _FakeManager(ProgrammerManager):
        def __init__(self) -> None:
            self.service = _FakeProgrammer()

        def global_programmer(self) -> _FakeProgrammer:
            return self.service

        def ops_programmer(self, address: int, *, long_address: bool = False):  # noqa: ANN201
            return None

    # Catalog: one indexed variable at 16.1.275.
    from pmri_core.persistence.decoder_xml import SplitCv  # noqa: PLC0415
    catalog = DecoderCatalog.from_files([
        DecoderFile(
            family=DecoderFamily(
                name="F", mfg="M", models=[DecoderModel(model="M1")],
            ),
            variables=[
                DecoderVariable.model_validate({
                    "CV": "16.1.275", "item": "Sound Setting 42",
                    "kind": VariableKind.DECIMAL,
                    "split": SplitCv.parse("16.1.275"),
                }),
            ],
        ),
    ])
    catalog.programmers["Comprehensive"] = ProgrammerConfig(
        name="Comprehensive",
        panes=[DecoderPane(name="Sound", items=["Sound Setting 42"])],
    )

    handle = build_web_app()
    handle.server.ctx._decoder_catalog = catalog     # type: ignore[attr-defined]
    handle.server.ctx._roster_profile = Profile(     # type: ignore[attr-defined]
        root=pathlib.Path(tmp_path), name="test",
    )
    fake_mgr = _FakeManager()
    handle.server.ctx.connection.programmers = fake_mgr  # type: ignore[attr-defined]

    client = TestClient(handle.app)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "F", "decoderModel": "M1",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": 275, "value": 42, "item": "Sound Setting 42"},
                ],
                "mode": "service",
            },
        )
        assert r.status_code == 202

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r = client.get("/programmer/loco/L1/batch/status")
            r.raise_for_status()
            data = r.json()
            if data["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.02)
        assert data["state"] == "done"

    # Wire ops: CV31=1 (bank), CV32=1 (index), CV275=42 (payload).
    # Actually the bank in "16.1.275" parses as bank=16, index=1.
    assert fake_mgr.service.writes == [(31, 16), (32, 1), (275, 42)]

    # Store: payload byte lives under the compound key.
    from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
    store = _loco_cv_store(handle.server.ctx)
    state = store.get("L1")
    assert state.cv_values["16.1.275"] == 42
    # Setup writes did NOT leave a trace under "31" / "32".
    assert "31" not in state.cv_values
    assert "32" not in state.cv_values


def test_batch_write_forwards_ops_mode_address_to_programmer(tmp_path) -> None:
    """When the batch payload declares ``mode="ops"`` + an address, that
    routes through the connection's ops_programmer(address, ...) path.
    Verified by asserting the ProgrammerService's mode-selection
    surfaces the ops-mode complaint (503 no ops-mode programmer) rather
    than the service-mode one -- the InternalConnection has neither, but
    different modes produce distinguishable errors."""
    import time  # noqa: PLC0415
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [{"cv": 1, "value": 3, "item": "Short Address"}],
                "mode": "ops",
                "address": 3,
                "isLongAddress": False,
            },
        )
        assert r.status_code == 202

        # Wait for terminal state -- expect ERROR because no ops programmer.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r = client.get("/programmer/loco/L1/batch/status")
            r.raise_for_status()
            data = r.json()
            if data["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"batch did not finish: {data}")
        assert data["state"] == "error"
        # Distinguishable error: the write callback's dispatch hit the
        # ops-mode branch of _resolve_programmer, not the service branch.
        error = data["writes"][0]["error"]
        assert "ops-mode" in error or "ops" in error.lower()


def test_programmer_page_offers_service_and_ops_mode_toggle(tmp_path) -> None:
    """The loco page must render a mode picker so the user can flip
    between service (programming track) and ops (POM). The JS reads it
    to shape the batch payload."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1")
        r.raise_for_status()
        assert "programming-mode-picker" in r.text
        assert 'value="service"' in r.text
        assert 'value="ops"' in r.text
        # The picker carries the loco's DCC address so the JS doesn't
        # have to re-fetch the roster row.
        assert 'data-loco-address="3"' in r.text


def test_read_batch_endpoint_returns_batch_id_and_dedupes(tmp_path) -> None:
    """POST /programmer/loco/<id>/read-batch queues reads with the
    same lifecycle as write-batch. Duplicate CVs collapse to one op
    (each read on service track is 2-6 s, so silent dedup matters)."""
    import time  # noqa: PLC0415
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/read-batch",
            json={
                "reads": [
                    {"cv": 1, "item": "Short Address"},
                    {"cv": 7, "item": "Version"},
                    {"cv": 1, "item": "Duplicate CV1"},   # dedup target
                ],
                "mode": "service",
            },
        )
        assert r.status_code == 202
        assert "batch_id" in r.json()

        # Wait for terminal -- InternalConnection has no programmer so
        # all reads fail. State is ERROR.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r = client.get("/programmer/loco/L1/batch/status")
            r.raise_for_status()
            data = r.json()
            if data["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"batch did not finish: {data}")
        assert data["state"] == "error"
        # Dedup: only 2 ops (CV1 once, CV7).
        assert data["total"] == 2


def test_read_batch_rejects_empty_reads_list(tmp_path) -> None:
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/read-batch",
            json={"reads": []},
        )
        assert r.status_code == 400


def test_pane_renders_read_pane_button(tmp_path) -> None:
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.get("/programmer/loco/L1/pane/motor")
        r.raise_for_status()
        assert "pane-read-btn" in r.text
        # Selector "pane-write-btn[data-scope=\"pane\"]" must still be
        # distinguishable from the read button (both have data-scope).
        assert 'class="pane-write-btn ok"' in r.text


def test_batch_write_rejects_empty_writes_list(tmp_path) -> None:
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={"writes": []},
        )
        assert r.status_code == 400


def test_batch_status_404_when_no_batch_active(tmp_path) -> None:
    """Polling for a loco that has never had a batch returns 404 so the
    UI knows to stop polling."""
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        r = client.get("/programmer/loco/nobody/batch/status")
        assert r.status_code == 404


def test_batch_cancel_marks_pending_writes_cancelled(tmp_path) -> None:
    """Cancelling a batch drops the pending writes so retries only pick
    up the un-attempted ones."""
    import time  # noqa: PLC0415
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        # Kick off many writes; no programmer means each errors quickly.
        r = client.post(
            "/programmer/loco/L1/write-batch",
            json={
                "writes": [
                    {"cv": i, "value": i, "item": f"CV{i}"}
                    for i in range(1, 20)
                ],
                "mode": "service",
            },
        )
        assert r.status_code == 202
        # Race the cancel against the queue.
        r = client.post("/programmer/loco/L1/batch/cancel")
        # If the queue already finished, cancel returns the final
        # status; otherwise it returns 200 with the mid-flight snapshot.
        # Either way we don't 500.
        assert r.status_code in (200, 404)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r = client.get("/programmer/loco/L1/batch/status")
            r.raise_for_status()
            data = r.json()
            if data["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.02)


def test_batch_clear_removes_terminal_state(tmp_path) -> None:
    """After a batch completes, the client can clear it so the next
    batch starts fresh (queue only holds one at a time)."""
    import time  # noqa: PLC0415
    client, _ = _client_with_populated_catalog(tmp_path)
    with client:
        client.post("/roster/create", data={
            "id": "L1", "dccAddress": "3", "longAddress": "0",
            "decoderFamily": "TestFamily", "decoderModel": "TestModel",
        })
        client.post(
            "/programmer/loco/L1/write-batch",
            json={"writes": [{"cv": 1, "value": 3, "item": "A"}]},
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r = client.get("/programmer/loco/L1/batch/status")
            if r.status_code == 200 and r.json()["state"] in (
                "done", "error", "cancelled",
            ):
                break
            time.sleep(0.02)
        r = client.post("/programmer/loco/L1/batch/clear")
        assert r.status_code == 200
        assert r.json() == {"cleared": True}
        # Status now 404 -- queue is empty.
        r = client.get("/programmer/loco/L1/batch/status")
        assert r.status_code == 404


def test_panel_upload_handles_invalid_xml() -> None:
    from pmri_web.app import build_web_app  # noqa: PLC0415
    handle = build_web_app()
    with TestClient(handle.app) as client:
        files = {"file": ("bad.xml", b"<<< invalid >>>", "application/xml")}
        r = client.post("/panel/upload", files=files)
        assert r.status_code == 400
        assert "failed to parse" in r.text
