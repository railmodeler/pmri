"""Each page renders successfully + key content is present."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_home_renders(client: TestClient) -> None:
    r = client.get("/")
    r.raise_for_status()
    body = r.text
    assert "<title>" in body
    assert "jmri-web" in body
    # Both power surfaces are visible on the home page: the summary CS
    # power badge in the Active connection card, and the full Track
    # Power ON/OFF/IDLE control lower down.
    assert "Command Station Power" in body
    assert "Track power" in body


def test_home_shows_cs_power_on_when_connection_open(client: TestClient) -> None:
    """Under an open ``InternalConnection`` (the test default), the CS
    is always considered on -- the base ``is_command_station_on``
    property tracks ``is_open``."""
    r = client.get("/")
    r.raise_for_status()
    body = r.text
    # Find the CS-power badge and confirm it reports On.
    assert 'id="home-cs-power-badge"' in body
    idx = body.find('id="home-cs-power-badge"')
    end = body.find("</span>", idx)
    badge = body[idx:end]
    assert "ok" in badge   # class="badge ok"
    # Badge text is whitespace-padded from the template; strip and compare.
    text_after_gt = badge.split(">", 2)[-1].strip()
    assert text_after_gt.startswith("On")


def test_static_css_served(client: TestClient) -> None:
    r = client.get("/static/style.css")
    r.raise_for_status()
    assert "--accent" in r.text


def test_static_js_served(client: TestClient) -> None:
    r = client.get("/static/app.js")
    r.raise_for_status()
    assert "jmriBus" in r.text


def test_throttle_page_renders(client: TestClient) -> None:
    r = client.get("/throttle?address=42")
    r.raise_for_status()
    assert "Addr 42" in r.text
    assert "Forward" in r.text


def test_turnouts_page_renders(client: TestClient) -> None:
    r = client.get("/turnouts")
    r.raise_for_status()
    assert "Turnouts" in r.text
    assert "No turnouts yet" in r.text


def test_sensors_page_renders(client: TestClient) -> None:
    r = client.get("/sensors")
    r.raise_for_status()
    assert "Sensors" in r.text
    assert "No sensors yet" in r.text


def test_programmer_page_empty_directory_input_by_default(
    client: TestClient,
) -> None:
    """No ``JMRI_DECODER_XML_ROOT`` set -> the catalog-load input
    renders with an empty ``value=``. Same behaviour as before this
    env knob was added."""
    r = client.get("/programmer")
    r.raise_for_status()
    body = r.text
    assert 'name="directory"' in body
    # Pull the whole <input> element and confirm its value is empty.
    idx = body.find('name="directory"')
    tag_start = body.rfind("<input", 0, idx)
    tag_end = body.find(">", idx) + 1
    tag = body[tag_start:tag_end]
    assert 'value=""' in tag


def test_programmer_page_prefills_from_env(
    monkeypatch, handle, tmp_path,
) -> None:
    """When ``JMRI_DECODER_XML_ROOT`` is set, the catalog-load input
    ships that path as its ``value=`` so the operator only needs to
    press Load. Using ``tmp_path`` gives us an OS-native path so the
    ``pathlib.Path`` round-trip inside the route doesn't flip
    separators on us (backslash on Windows, forward slash elsewhere)."""
    from fastapi.testclient import TestClient as _TC  # noqa: PLC0415

    decoder_root = tmp_path / "decoders"
    monkeypatch.setenv("JMRI_DECODER_XML_ROOT", str(decoder_root))
    with _TC(handle.app) as tc:
        r = tc.get("/programmer")
        r.raise_for_status()
        body = r.text
        idx = body.find('name="directory"')
        tag = body[body.rfind("<input", 0, idx): body.find(">", idx) + 1]
        # The route runs the env value through ``pathlib.Path`` which
        # normalises separators, then stringifies. Compare against the
        # same round-trip so the test is OS-agnostic.
        import pathlib as _p  # noqa: PLC0415
        expected = str(_p.Path(str(decoder_root)))
        # HTML-escape backslashes for the naive substring check.
        assert f'value="{expected}"' in tag


def test_signalsystems_page_renders_hint_when_empty(
    client: TestClient,
) -> None:
    """No signal systems loaded -> the page shows the operator hint
    pointing at ``JMRI_SIGNAL_XML_ROOT`` instead of a blank card."""
    r = client.get("/signalsystems")
    r.raise_for_status()
    body = r.text
    assert "Signal systems" in body
    assert "JMRI_SIGNAL_XML_ROOT" in body


def test_signalsystems_page_renders_loaded_systems(handle) -> None:
    """When systems are loaded, each renders as an expandable card
    with its aspect table."""
    import pathlib as _p  # noqa: PLC0415
    import tempfile       # noqa: PLC0415

    from fastapi.testclient import TestClient as _TC  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmpdir:
        root = _p.Path(tmpdir)
        (root / "basic").mkdir()
        (root / "basic" / "aspects.xml").write_text(
            "<?xml version='1.0'?>"
            "<aspecttable><name>Basic Model Signals</name>"
            "<reference>Ref</reference>"
            "<aspects>"
            "<aspect><name>Clear</name><indication>Proceed</indication>"
            "<speed>Normal</speed></aspect>"
            "<aspect><name>Stop</name><indication>Stop</indication>"
            "<speed>Stop</speed></aspect>"
            "</aspects></aspecttable>",
            encoding="utf-8",
        )
        handle.server.ctx._signal_xml_root = root
        with _TC(handle.app) as tc:
            r = tc.get("/signalsystems")
            r.raise_for_status()
            body = r.text
            assert "Basic Model Signals" in body
            assert "Clear" in body
            assert "Stop" in body


def test_home_log_widget_no_file_configured(client: TestClient) -> None:
    """When ``JMRI_LOG_FILE`` isn't set, the widget shows the hint
    directing the operator at the env var. Empty state, no crash."""
    r = client.get("/")
    r.raise_for_status()
    assert "No log file configured" in r.text
    assert "JMRI_LOG_FILE" in r.text


def test_logs_refresh_returns_fragment(client: TestClient) -> None:
    """The Refresh button hits ``/logs/refresh`` and swaps the widget
    body -- must return the same fragment shape as the initial render."""
    r = client.get("/logs/refresh")
    r.raise_for_status()
    # No file configured in the default AppHandle -> hint is present.
    assert "No log file configured" in r.text


def test_logixng_page_renders_empty(client: TestClient) -> None:
    r = client.get("/logixng")
    r.raise_for_status()
    body = r.text
    assert "LogixNG" in body
    assert "Create a program" in body
    assert "No LogixNG programs defined yet" in body


def test_logixng_create_flow(client: TestClient) -> None:
    r = client.post("/logixng/create", data={
        "name": "IQtest1", "userName": "Test program",
    })
    r.raise_for_status()
    body = r.text
    assert "IQtest1" in body
    assert "Test program" in body
    assert "enabled" in body
    assert "0 conditionals" in body


def test_logixng_disable_shows_badge_change(client: TestClient) -> None:
    client.post("/logixng/create", data={"name": "IQtest2"}).raise_for_status()
    r = client.post("/logixng/IQtest2/disable")
    r.raise_for_status()
    body = r.text
    # The badge on a disabled program is "disabled" without the ``ok`` class.
    idx = body.find("IQtest2")
    assert idx >= 0
    card_end = body.find("</table>", idx)
    if card_end == -1:   # empty program has no table
        card_end = body.find("</div>", idx + 100)
    card_slice = body[idx:card_end]
    assert "disabled" in card_slice


def test_logixng_delete_returns_empty_state(client: TestClient) -> None:
    client.post("/logixng/create", data={"name": "IQtestd"}).raise_for_status()
    r = client.post("/logixng/IQtestd/delete")
    r.raise_for_status()
    assert "No LogixNG programs defined yet" in r.text


def test_openlcb_page_renders_warning_on_non_olcb_connection(
    client: TestClient,
) -> None:
    """Default test build_app() uses InternalConnection -- the page
    should render with a "not OpenLCB" warning rather than a 500
    from the underlying service. Regression pin for the graceful
    degradation path."""
    r = client.get("/openlcb")
    r.raise_for_status()
    body = r.text
    assert "OpenLCB / LCC bus" in body
    assert "isn't OpenLCB" in body
    # Discovery / search UI hidden under the guard.
    assert "Discovered nodes" not in body


def test_openlcb_nav_link_present(client: TestClient) -> None:
    """Every page has the openlcb nav link so operators can find the
    discovery UI without knowing the URL."""
    r = client.get("/")
    r.raise_for_status()
    assert 'href="/openlcb"' in r.text


def test_openlcb_node_detail_renders_error_on_non_olcb(
    client: TestClient,
) -> None:
    """Default connection isn't OpenLCB -> the detail page should
    still render (with the openlcb.html shape) but carry an inline
    error notice rather than exploding."""
    r = client.get("/openlcb/nodes/0x222")
    # 400 (from service) is expected; page still renders the shell.
    assert r.status_code == 400
    body = r.text
    assert "OpenLCB node" in body
    assert "0x222" in body
    assert "not OpenLCB" in body


def test_openlcb_node_detail_uses_openlcb_active_nav(
    client: TestClient,
) -> None:
    """Node detail page should highlight the openlcb nav tab."""
    r = client.get("/openlcb/nodes/0x222")
    body = r.text
    # The active nav link gets class="active"; check the openlcb one.
    assert 'href="/openlcb" class="active"' in body


def test_openlcb_node_probe_returns_error_on_non_olcb(
    client: TestClient,
) -> None:
    """A probe against a non-OpenLCB connection is a service-level
    400. FastAPI's default handler propagates that up to the client
    as a 400 too -- the operator sees the error at the route level
    rather than a rendered fragment. Good enough for the "warning
    surface" contract; the page-level probe UI is gated behind the
    "isn't OpenLCB" banner so users don't reach this path normally."""
    r = client.post("/openlcb/nodes/0x222/probe")
    assert r.status_code == 400


def test_consists_page_renders_empty(client: TestClient) -> None:
    r = client.get("/consists")
    r.raise_for_status()
    body = r.text
    assert "Consists" in body
    assert "Create a consist" in body
    assert "No consists defined yet" in body


def test_consists_create_flow_renders_row(client: TestClient) -> None:
    """Creating a consist through the web form should return a fragment
    with the new consist visible in the table."""
    r = client.post("/consists/create", data={
        "address": "42", "kind": "software", "comment": "unit test",
    })
    r.raise_for_status()
    body = r.text
    # The fragment includes the new consist's address, kind badge,
    # comment, and the members-management form.
    assert "42" in body
    assert "software" in body
    assert "unit test" in body
    assert "no members yet" in body


def test_consists_add_member_appears_in_fragment(client: TestClient) -> None:
    """A follow-up POST to add-member should show the member with a
    lead badge (first member is always the lead)."""
    client.post("/consists/create", data={
        "address": "43", "kind": "software",
    }).raise_for_status()
    r = client.post("/consists/43/add-member", data={
        "member_address": "111", "long_address": "1", "direction_normal": "1",
    })
    r.raise_for_status()
    body = r.text
    assert "111" in body
    assert "lead" in body


def test_consists_delete_flow_returns_empty_state(client: TestClient) -> None:
    """Deleting the last consist should return the 'no consists defined
    yet' empty-state fragment."""
    client.post("/consists/create", data={
        "address": "44", "kind": "software",
    }).raise_for_status()
    r = client.post("/consists/44/delete")
    r.raise_for_status()
    assert "No consists defined yet" in r.text


def test_consists_page_shows_refresh_from_cs_button(client: TestClient) -> None:
    r = client.get("/consists")
    r.raise_for_status()
    body = r.text
    assert "Refresh from CS" in body
    assert "/consists/refresh-from-cs" in body


def test_consists_refresh_from_cs_unsupported_shows_notice(
    client: TestClient,
) -> None:
    """Default connection doesn't expose ``refresh_from_cs`` -- the
    route should still return 200 with the table + a friendly error
    notice instead of leaking a 400 to htmx."""
    r = client.post("/consists/refresh-from-cs")
    r.raise_for_status()
    body = r.text
    assert "Refresh from CS failed" in body


def test_consists_refresh_from_cs_success_shows_change_notice(handle) -> None:
    """When the driver's manager has ``refresh_from_cs``, the route
    surfaces a "found N updated" banner."""
    async def fake_refresh(**kwargs):
        return [10, 42]

    handle.server.ctx.connection.consists.refresh_from_cs = fake_refresh  # type: ignore[attr-defined]
    with TestClient(handle.app) as tc:
        r = tc.post("/consists/refresh-from-cs")
        r.raise_for_status()
        body = r.text
        assert "2 consists updated" in body
        assert "10, 42" in body


def test_consists_refresh_from_cs_success_no_changes(handle) -> None:
    async def fake_refresh(**kwargs):
        return []

    handle.server.ctx.connection.consists.refresh_from_cs = fake_refresh  # type: ignore[attr-defined]
    with TestClient(handle.app) as tc:
        r = tc.post("/consists/refresh-from-cs")
        r.raise_for_status()
        assert "no changes" in r.text


def test_home_page_shows_fast_clock_widget(client: TestClient) -> None:
    r = client.get("/")
    r.raise_for_status()
    assert "<h3>Fast Clock</h3>" in r.text
    assert 'id="fc-display"' in r.text
    # Default state.
    assert "00:00" in r.text
    assert "STOPPED" in r.text


def test_lights_page_renders(client: TestClient) -> None:
    r = client.get("/lights")
    r.raise_for_status()
    assert "Lights" in r.text
    assert "No lights yet" in r.text


def test_roster_page_renders(client: TestClient) -> None:
    r = client.get("/roster")
    r.raise_for_status()
    assert "Roster" in r.text


def test_panel_page_renders(client: TestClient) -> None:
    r = client.get("/panel")
    r.raise_for_status()
    assert "Panel viewer" in r.text


def test_unknown_page_returns_404(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404


def test_json_api_still_mounted(client: TestClient) -> None:
    """Confirm jmri-server's /json endpoint is still alive alongside templates."""
    r = client.post("/json/", json={"type": "ping"})
    r.raise_for_status()
    body = r.json()
    assert body["type"] == "ping"
