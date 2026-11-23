"""Integration tests for the Operations Pro web pages."""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from pmri_core.persistence.profile import Profile

from pmri_server.operations_store import OperationsStore
from pmri_web.app import build_web_app


def _web_with_ops(tmp_path: pathlib.Path):
    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    handle = build_web_app()
    handle.server.ctx._operations_store = OperationsStore(profile=profile)
    return handle


def test_operations_home_renders(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations")
        r.raise_for_status()
    body = r.text
    assert 'href="/operations/cars"' in body
    assert 'href="/operations/engines"' in body
    assert 'href="/operations/locations"' in body


def test_cars_page_lists_empty_then_create_adds_row(
    tmp_path: pathlib.Path,
) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/cars")
        r.raise_for_status()
        assert "No cars yet" in r.text
        r = tc.post("/operations/cars/create", data={
            "id": "c1", "roadName": "SP", "roadNumber": "4449",
            "type": "boxcar",
        })
        r.raise_for_status()
    body = r.text
    assert 'data-car="c1"' in body
    assert "SP" in body
    assert "4449" in body


def test_cars_delete_drops_row(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/cars/create", data={"id": "c1"})
        r = tc.delete("/operations/cars/c1")
        r.raise_for_status()
    assert 'data-car="c1"' not in r.text
    assert "No cars yet" in r.text


def test_inferior_direction_train_walks_route_in_reverse(
    tmp_path: pathlib.Path,
) -> None:
    """A train whose direction is the route's inferior direction
    sees stops in reverse travel order on the edit page; the
    timetable schedules it starting from the route's LAST stop and
    advancing backward through the superior order."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid, name in [("a", "Alpha"), ("b", "Beta"), ("c", "Charlie")]:
            tc.post("/operations/stations/create", data={
                "id": sid, "name": name,
            })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Line",
        })
        # Stored in superior (W) order: A→B→C with 10 miles each leg.
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["a", "b", "c"],
            "stop_pickups":     ["yes", "yes", "yes"],
            "stop_drops":       ["yes", "yes", "yes"],
            "stop_maxCarMoves": ["5", "5", "5"],
            "stop_miles":       ["0", "10", "10"],
            "stop_stopType":    ["", "", ""],
        })
        # Eastbound train: origin = C, 60 mph, depart 08:00.
        tc.post("/operations/trains/create", data={
            "id": "eb-1", "name": "Eastbound", "route": "r1",
            "trainType": "Freight", "trainClass": "2",
            "direction": "E", "averageSpeed": "60",
            "departHour": "08", "departMinute": "00",
        })
        # Edit page: first row should be Charlie (the train's origin
        # under east travel), then Beta, then Alpha.
        r = tc.get("/operations/trains/eb-1/edit")
        body = r.text
        c_pos = body.find("Charlie")
        b_pos = body.find("Beta")
        a_pos = body.find("Alpha")
        # All three appear and in reversed order on the page.
        assert 0 < c_pos < b_pos < a_pos

        # Timetable: 08:00 at Charlie (origin), then 08:10 at Beta,
        # 08:20 at Alpha.
        r = tc.get("/operations/timetable?view=train")
    body = r.text
    assert "08:00" in body
    assert "08:10" in body
    assert "08:20" in body


def test_mainline_by_route_includes_branch_train_at_mainline_stops(
    tmp_path: pathlib.Path,
) -> None:
    """A train on a branch that opted in to mainline stops shows up
    in the mainline's By Route view at those stops."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        # Mainline A-B-C-D-E-F, westward superior.
        for sid in "abcdefg":
            tc.post("/operations/stations/create", data={
                "id": sid, "name": sid.upper(),
            })
        tc.post("/operations/routes/create", data={
            "id": "main", "name": "Main",
        })
        tc.post("/operations/routes/main/save", data={
            "id": "main", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["a", "b", "c", "d", "e", "f"],
            "stop_pickups":     ["yes"] * 6,
            "stop_drops":       ["yes"] * 6,
            "stop_maxCarMoves": ["5"] * 6,
            "stop_miles":       ["0", "5", "5", "5", "5", "5"],
            "stop_stopType":    [""] * 6,
        })
        # Branch G-E (west-superior), anchored at E (the last stop on
        # the branch). A train on this branch may continue west onto
        # the main to F.
        tc.post("/operations/routes/create", data={
            "id": "br", "name": "Branch",
        })
        tc.post("/operations/routes/br/save", data={
            "id": "br", "name": "Branch",
            "isBranch": "true",
            "mainlineRouteId": "main",
            "branchStartStopId": "e",
            "stop_stationId":   ["g", "e"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "4"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "br-train", "name": "Branch Local", "route": "br",
            "trainType": "Freight", "trainClass": "3", "direction": "W",
            "averageSpeed": "60",
            "departHour": "09", "departMinute": "00",
        })
        # Include station F (mainline-after E) in the train's schedule.
        tc.post("/operations/trains/br-train/save", data={
            "id": "br-train", "name": "Branch Local", "route": "br",
            "trainType": "Freight", "trainClass": "3", "direction": "W",
            "averageSpeed": "60",
            "departHour": "09", "departMinute": "00",
            "stop_stationId":   ["g", "e", "f"],
            "stop_include":     ["true", "true", "true"],
            "stop_departHour":  ["09", "", ""],
            "stop_departMinute": ["00", "", ""],
            "stop_wait":        ["0", "0", "0"],
        })
        # Mainline By Route view should now show the Branch Local
        # with a time at BOTH the junction (E) AND the mainline-after
        # stop (F). The time at E anchors the mainline segment; F is
        # the extension.
        r = tc.get("/operations/timetable?view=route&route=main")
    body = r.text
    assert "Branch Local" in body
    # The "(via Branch)" annotation clarifies this is a branch train.
    assert "via Branch" in body
    # Both the junction and the mainline-after stop should render a
    # time cell for this train. We assert by counting ``time-cell``
    # spans in the body; exactly two for the one branch train.
    import re
    cells = re.findall(r'<span class="time-cell"[^>]*>', body)
    assert len(cells) == 2


def test_train_schedule_rows_refresh_reorders_on_direction_change(
    tmp_path: pathlib.Path,
) -> None:
    """GETting ``/trains/{id}/schedule_rows`` with a direction query
    param returns the schedule fragment reordered for that direction
    -- without requiring a save+re-edit. The htmx hook on the
    direction select targets this endpoint so the Per-stop table
    reorders immediately on change."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid, name in [("a", "Alpha"), ("b", "Beta"), ("c", "Charlie")]:
            tc.post("/operations/stations/create", data={
                "id": sid, "name": name,
            })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["a", "b", "c"],
            "stop_pickups":     ["yes"] * 3,
            "stop_drops":       ["yes"] * 3,
            "stop_maxCarMoves": ["5"] * 3,
            "stop_miles":       ["0", "10", "10"],
            "stop_stopType":    [""] * 3,
        })
        # Train persisted as W (superior); flip direction via refresh
        # endpoint and expect reverse travel order in the fragment.
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "L", "route": "r1",
            "direction": "W", "averageSpeed": "60",
        })
        r_sup = tc.get(
            "/operations/trains/t1/schedule_rows?route=r1&direction=W",
        )
        r_inf = tc.get(
            "/operations/trains/t1/schedule_rows?route=r1&direction=E",
        )
    sup_body = r_sup.text
    inf_body = r_inf.text
    # Superior direction: A, B, C in that order.
    a_sup = sup_body.find("Alpha")
    b_sup = sup_body.find("Beta")
    c_sup = sup_body.find("Charlie")
    assert 0 <= a_sup < b_sup < c_sup
    # Inferior direction: C, B, A in that order.
    a_inf = inf_body.find("Alpha")
    b_inf = inf_body.find("Beta")
    c_inf = inf_body.find("Charlie")
    assert 0 <= c_inf < b_inf < a_inf


def test_inferior_branch_train_miles_rebuild_with_mainline_extension(
    tmp_path: pathlib.Path,
) -> None:
    """A branch train in INFERIOR direction, with mainline-after
    extensions, sees the per-stop ``Miles`` column rebuilt as
    "distance from previous TRAVEL stop" -- including across the
    branch/mainline boundary. For a route whose superior-order miles
    read D(0)->C(20)->B(30)->A(5), the inferior (eastbound) walk
    A->B->C->D shows 0, 5, 30, 20.
    """
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid in ("d", "c", "b", "a"):
            tc.post("/operations/stations/create", data={
                "id": sid, "name": sid.upper(),
            })
        tc.post("/operations/routes/create", data={"id": "main"})
        tc.post("/operations/routes/main/save", data={
            "id": "main", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["c", "b", "a"],
            "stop_pickups":     ["yes"] * 3,
            "stop_drops":       ["yes"] * 3,
            "stop_maxCarMoves": ["5"] * 3,
            "stop_miles":       ["0", "30", "5"],
            "stop_stopType":    [""] * 3,
        })
        tc.post("/operations/routes/create", data={"id": "br"})
        tc.post("/operations/routes/br/save", data={
            "id": "br", "name": "Branch",
            "direction": "EW", "superiorDirection": "W",
            "isBranch": "true",
            "mainlineRouteId": "main",
            "branchStartStopId": "c",
            "stop_stationId":   ["d", "c"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "20"],
            "stop_stopType":    ["", ""],
        })
        # Eastbound train with mainline-after rows already opted in.
        tc.post("/operations/trains/create", data={
            "id": "br-eb", "name": "Branch EB", "route": "br",
        })
        tc.post("/operations/trains/br-eb/save", data={
            "id": "br-eb", "name": "Branch EB", "route": "br",
            "direction": "E", "averageSpeed": "60",
            "departHour": "09", "departMinute": "00",
            "stop_stationId":   ["d", "c", "b", "a"],
            "stop_include":     ["true", "true", "true", "true"],
            "stop_departHour":  ["", "", "", ""],
            "stop_departMinute": ["", "", "", ""],
            "stop_wait":        ["0", "0", "0", "0"],
        })
        r = tc.get("/operations/trains/br-eb/edit")
    body = r.text
    # The edit page's row order top-down (east travel) is A, B, C, D.
    import re
    station_positions = {
        name: body.find(f">{name}")
        for name in ("Alpha", "Bravo", "Charlie", "Delta")
    }
    # All appear and in reversed order.
    a_pos = body.find(">A\n")
    b_pos = body.find(">B\n")
    c_pos = body.find(">C\n")
    d_pos = body.find(">D\n")
    # Allow flexibility in exact text matching -- rely on data-miles
    # on the ``<tr data-schedule-row>`` elements for the real assert.
    rows = re.findall(r'<tr data-schedule-row[^>]*data-miles="([^"]+)"', body)
    # Expected inferior travel-order miles: 0, 5, 30, 20
    assert rows == ["0", "5", "30", "20"]


def test_branch_train_save_preserves_endpoint_depart_time(
    tmp_path: pathlib.Path,
) -> None:
    """Regression: a branch train in the SUPERIOR direction, with
    mainline-after extensions opted in, saves and re-reads with the
    branch endpoint's (anchor) departure time intact. Previously the
    include-flag parsing mis-aligned when route-stop rows (no
    sentinel) were interleaved with extension rows (sentinel), and
    the endpoint's TrainStop was dropped or blanked.

    Scenario mirrors the feedback: D -> C -> B -> A superior order,
    where D-C is the branch (anchor at C, end of branch) and B-A are
    mainline-after extensions. The train is westbound (superior).
    """
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid, name in [
            ("d", "Delta"), ("c", "Charlie"),
            ("b", "Bravo"), ("a", "Alpha"),
        ]:
            tc.post("/operations/stations/create", data={
                "id": sid, "name": name,
            })
        tc.post("/operations/routes/create", data={"id": "main"})
        tc.post("/operations/routes/main/save", data={
            "id": "main", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["c", "b", "a"],
            "stop_pickups":     ["yes"] * 3,
            "stop_drops":       ["yes"] * 3,
            "stop_maxCarMoves": ["5"] * 3,
            "stop_miles":       ["0", "30", "5"],
            "stop_stopType":    [""] * 3,
        })
        tc.post("/operations/routes/create", data={"id": "br"})
        tc.post("/operations/routes/br/save", data={
            "id": "br", "name": "Branch",
            "direction": "EW", "superiorDirection": "W",
            "isBranch": "true",
            "mainlineRouteId": "main",
            "branchStartStopId": "c",
            "stop_stationId":   ["d", "c"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "20"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "br-wb", "name": "Branch WB", "route": "br",
        })
        # Save with all four stops included and depart times set.
        tc.post("/operations/trains/br-wb/save", data={
            "id": "br-wb", "name": "Branch WB", "route": "br",
            "trainType": "Freight", "trainClass": "2",
            "direction": "W", "averageSpeed": "60",
            "departHour": "08", "departMinute": "00",
            "stop_stationId":   ["d", "c", "b", "a"],
            "stop_include":     ["true", "true", "true", "true"],
            "stop_departHour":  ["08", "08", "09", "09"],
            "stop_departMinute": ["00", "20", "00", "30"],
            "stop_wait":        ["0", "0", "0", "0"],
        })
        rr = tc.post("/json/", json={
            "type": "operations-trains", "method": "get",
            "data": {"id": "br-wb"},
        })
    data = rr.json()["data"]
    stops = data.get("stops") or []
    # All four stops saved with departure times intact.
    assert len(stops) == 4
    by_stn = {s["stationId"]: s for s in stops}
    assert set(by_stn.keys()) == {"d", "c", "b", "a"}
    # The branch endpoint (C) must still have a departure time.
    assert by_stn["c"]["departHour"] == "08"
    assert by_stn["c"]["departMinute"] == "20"
    assert by_stn["d"]["departHour"] == "08"
    assert by_stn["b"]["departHour"] == "09"
    assert by_stn["a"]["departHour"] == "09"


def test_branch_train_save_excludes_unchecked_extension(
    tmp_path: pathlib.Path,
) -> None:
    """A mainline-extension row with ``stop_include=""`` is dropped
    from the saved TrainStops, while route-stop rows and other
    checked extensions persist in order."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid in ("d", "c", "b", "a"):
            tc.post("/operations/stations/create", data={
                "id": sid, "name": sid.upper(),
            })
        tc.post("/operations/routes/create", data={"id": "main"})
        tc.post("/operations/routes/main/save", data={
            "id": "main", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["c", "b", "a"],
            "stop_pickups":     ["yes"] * 3,
            "stop_drops":       ["yes"] * 3,
            "stop_maxCarMoves": ["5"] * 3,
            "stop_miles":       ["0", "30", "5"],
            "stop_stopType":    [""] * 3,
        })
        tc.post("/operations/routes/create", data={"id": "br"})
        tc.post("/operations/routes/br/save", data={
            "id": "br", "name": "Branch",
            "direction": "EW", "superiorDirection": "W",
            "isBranch": "true",
            "mainlineRouteId": "main",
            "branchStartStopId": "c",
            "stop_stationId":   ["d", "c"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "20"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "br-wb", "name": "Branch WB", "route": "br",
        })
        # Save with B unchecked (extension excluded) and A checked.
        tc.post("/operations/trains/br-wb/save", data={
            "id": "br-wb", "name": "Branch WB", "route": "br",
            "direction": "W", "averageSpeed": "60",
            "departHour": "08", "departMinute": "00",
            "stop_stationId":   ["d", "c", "b", "a"],
            "stop_include":     ["true", "true", "", "true"],
            "stop_departHour":  ["08", "08", "", "09"],
            "stop_departMinute": ["00", "20", "", "30"],
            "stop_wait":        ["0", "0", "0", "0"],
        })
        rr = tc.post("/json/", json={
            "type": "operations-trains", "method": "get",
            "data": {"id": "br-wb"},
        })
    data = rr.json()["data"]
    stops = data.get("stops") or []
    stn_ids = [s["stationId"] for s in stops]
    assert stn_ids == ["d", "c", "a"]


def test_train_edit_build_section_omits_built_years(
    tmp_path: pathlib.Path,
) -> None:
    """Regression: the two built-year inputs were removed from the
    Build section."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/trains/create", data={"id": "t1"})
        r = tc.get("/operations/trains/t1/edit")
    body = r.text
    assert 'name="builtStartYear"' not in body
    assert 'name="builtEndYear"' not in body


def test_train_edit_has_specific_engine_and_caboose_selectors(
    tmp_path: pathlib.Path,
) -> None:
    """Build section exposes engineId + cabooseId selectors populated
    from the respective rosters."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/engines/create", data={
            "id": "e-7890", "roadName": "SP", "roadNumber": "7890",
            "model": "FEF-3", "locationId": "oak",
        })
        tc.post("/operations/cars/create", data={
            "id": "cab-99", "roadName": "ATSF", "roadNumber": "99",
            "type": "Caboose",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "L", "route": "r1",
        })
        r = tc.get("/operations/trains/t1/edit")
    body = r.text
    assert '<select name="engineId"' in body
    assert 'value="e-7890"' in body
    assert "SP 7890" in body
    assert '<select name="cabooseId"' in body
    assert 'value="cab-99"' in body
    assert "ATSF 99" in body


def test_train_edit_assigned_cars_renders_picker_not_matrix(
    tmp_path: pathlib.Path,
) -> None:
    """Assigned cars section renders as Type + Car pickers plus Add
    button -- not the old full-matrix of checkboxes. Already-assigned
    cars show up in a list with a Remove button per row."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/cars/create", data={
            "id": "c1", "roadName": "ATSF", "roadNumber": "100",
            "type": "Passenger",
        })
        tc.post("/operations/cars/create", data={
            "id": "c2", "roadName": "ATSF", "roadNumber": "200",
            "type": "Passenger",
        })
        tc.post("/operations/cars/create", data={
            "id": "c3", "roadName": "UP", "roadNumber": "7",
            "type": "Boxcar",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Express", "route": "r1",
        })
        # Pre-assign c1 so the list shows one row.
        tc.post("/operations/trains/t1/save", data={
            "id": "t1", "name": "Express", "route": "r1",
            "assignedCarIds": ["c1"],
        })
        r = tc.get("/operations/trains/t1/edit")
    body = r.text
    # The pickers must be present.
    assert 'id="assigned-car-type-picker"' in body
    assert 'id="assigned-car-id-picker"' in body
    assert 'id="assigned-car-add-btn"' in body
    # The server-rendered list row for c1 is present with hidden input.
    assert 'data-assigned-car="c1"' in body
    assert 'name="assignedCarIds" value="c1"' in body
    # The old matrix of per-car checkboxes is gone.
    assert "assigned-cars-matrix" not in body
    # Catalog JSON for the JS picker is embedded.
    assert 'id="cars-catalog-json"' in body
    # Type dropdown offers the roster's distinct types.
    assert "Passenger" in body
    assert "Boxcar" in body


def test_train_edit_car_catalog_json_preserves_ampersand(
    tmp_path: pathlib.Path,
) -> None:
    """Road names with ``&`` (e.g. ``D&RGW``) must land in the JS
    catalog JSON as a literal ``&`` -- not as the HTML entity
    ``&amp;``. The picker builds dropdown options from the JSON via
    ``textContent``, so an entity-escaped value would display as the
    literal string ``D&amp;RGW`` in the browser."""
    import json as _json
    import re as _re
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/cars/create", data={
            "id": "c1", "roadName": "D&RGW", "roadNumber": "3001",
            "type": "Boxcar",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Express", "route": "r1",
        })
        r = tc.get("/operations/trains/t1/edit")
    body = r.text
    m = _re.search(
        r'<script type="application/json" id="cars-catalog-json">(.*?)</script>',
        body, _re.DOTALL,
    )
    assert m is not None
    payload = _json.loads(m.group(1))
    labels = [c["label"] for c in payload]
    # Literal ``&`` must survive; the entity must not.
    assert any("D&RGW" in lbl for lbl in labels), payload
    assert not any("D&amp;RGW" in lbl for lbl in labels), payload


def test_train_save_persists_assigned_cars_and_specific_engine(
    tmp_path: pathlib.Path,
) -> None:
    """Posting assignedCarIds (repeated field) + engineId + cabooseId
    round-trips through the JSON topic."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/engines/create", data={
            "id": "e1", "roadName": "SP", "locationId": "oak",
        })
        tc.post("/operations/cars/create", data={
            "id": "c1", "type": "Passenger",
        })
        tc.post("/operations/cars/create", data={
            "id": "c2", "type": "Passenger",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Express", "route": "r1",
        })
        tc.post("/operations/trains/t1/save", data={
            "id": "t1", "name": "Express", "route": "r1",
            "trainType": "Passenger",
            "engineId": "e1",
            "assignedCarIds": ["c1", "c2"],
        })
        rr = tc.post("/json/", json={
            "type": "operations-trains", "method": "get",
            "data": {"id": "t1"},
        })
    data = rr.json()["data"]
    assert data["engineId"] == "e1"
    assert sorted(data.get("assignedCarIds") or []) == ["c1", "c2"]


def test_passenger_train_build_does_not_warn_about_caboose(
    tmp_path: pathlib.Path,
) -> None:
    """Rendering the manifest for a Passenger train with requires=1
    (caboose bit) doesn't emit the "needs caboose" warning."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "a", "name": "A",
        })
        tc.post("/operations/stations/create", data={
            "id": "b", "name": "B",
        })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Line",
        })
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Line",
            "stop_stationId": ["a", "b"],
            "stop_pickups": ["yes", "yes"],
            "stop_drops": ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles": ["0", "10"],
            "stop_stopType": ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "pax", "name": "Express", "route": "r1",
            "trainType": "Passenger", "requires": "1",
        })
        r = tc.get("/operations/trains/pax/build")
    body = r.text.lower()
    # No caboose warning on the manifest.
    assert "requires a caboose" not in body


def test_engines_page_uses_station_dropdown(tmp_path: pathlib.Path) -> None:
    """Engine create form offers a Station dropdown (not free text);
    the list column shows the station NAME not its id."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        r = tc.get("/operations/engines")
        r.raise_for_status()
        body = r.text
        assert '<select name="locationId"' in body
        assert 'value="oak"' in body
        assert ">Oakland<" in body
        # Create an engine at the station.
        tc.post("/operations/engines/create", data={
            "id": "e1", "roadName": "UP", "model": "SD40",
            "locationId": "oak",
        })
        r = tc.get("/operations/engines")
    # Engine row shows "Oakland", not "oak".
    assert "Oakland" in r.text


def test_train_build_fields_are_dropdowns(tmp_path: pathlib.Path) -> None:
    """Engine Road, Engine Model, and Caboose Road fields on the
    train edit page are <select>s populated from the roster, with
    an "(any)" default."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/engines/create", data={
            "id": "e1", "roadName": "UP", "model": "SD40",
            "locationId": "oak",
        })
        tc.post("/operations/engines/create", data={
            "id": "e2", "roadName": "BN", "model": "SD70",
            "locationId": "oak",
        })
        tc.post("/operations/cars/create", data={
            "id": "cab-1", "roadName": "ATSF", "type": "Caboose",
        })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Line",
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "L", "route": "r1",
        })
        r = tc.get("/operations/trains/t1/edit")
    body = r.text
    # Engine roads come from the engine roster.
    assert '<select name="engineRoad"' in body
    assert "UP" in body
    assert "BN" in body
    # Engine models too.
    assert '<select name="engineModel"' in body
    assert "SD40" in body
    assert "SD70" in body
    # Caboose roads come from cars of type Caboose.
    assert '<select name="cabooseRoad"' in body
    assert "ATSF" in body


def test_timetable_reflects_per_stop_depart_override(
    tmp_path: pathlib.Path,
) -> None:
    """When the train has an explicit TrainStop.depart on a stop, the
    timetable's By Train view renders that as the DEPART (not the
    arrive) time."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid, name in [("a", "Alpha"), ("b", "Beta")]:
            tc.post("/operations/stations/create", data={
                "id": sid, "name": name,
            })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Line",
        })
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["a", "b"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "30"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Local", "route": "r1",
            "departHour": "08", "departMinute": "00",
            "averageSpeed": "60",
        })
        # Save a TrainStop override: depart Beta at 10:30 (user wants
        # a 30-minute padding over the computed 08:30 arrival).
        tc.post("/operations/trains/t1/save", data={
            "id": "t1", "name": "Local", "route": "r1",
            "departHour": "08", "departMinute": "00",
            "averageSpeed": "60",
            "stop_stationId":   ["a", "b"],
            "stop_include":     ["true", "true"],
            "stop_departHour":  ["08", "10"],
            "stop_departMinute": ["00", "30"],
            "stop_wait":        ["0", "0"],
        })
        r = tc.get("/operations/timetable?view=train")
    body = r.text
    # Arrive at Beta is still 08:30 (0:30 from 30 miles at 60 mph).
    assert "08:30" in body
    # Depart from Beta reflects the user override.
    assert "10:30" in body


def test_train_edit_per_stop_depart_inputs_are_wider_and_use_canonical(
    tmp_path: pathlib.Path,
) -> None:
    """Regression: the per-stop depart inputs were 3.5em wide (second
    digit clipped by the spinner) and had no 12-hour face. Now the
    canonical 24h value lives in hidden inputs and both faces render
    with wider number inputs."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "a", "name": "A",
        })
        tc.post("/operations/stations/create", data={
            "id": "b", "name": "B",
        })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Line",
        })
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Line",
            "stop_stationId":   ["a", "b"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "10"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "L", "route": "r1",
        })
        r = tc.get("/operations/trains/t1/edit")
    body = r.text
    # Canonical hidden inputs present.
    assert 'data-canonical="h"' in body
    assert 'data-canonical="m"' in body
    # Both faces rendered.
    assert 'data-face="24h-h"' in body
    assert 'data-face="12h-h"' in body
    assert 'data-face="12h-ap"' in body
    # Wider than the earlier 3.5em.
    assert "width:4.5em" in body


def test_build_train_renders_manifest(tmp_path: pathlib.Path) -> None:
    """End-to-end: seed stations/locations/cars/train, hit the Build
    endpoint, assert the manifest page shows picked cars + the
    destination they were routed to."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "fre", "name": "Fremont",
        })
        # Seed locations with carTypes via the JSON topic so the
        # Pydantic list validator is happy (the HTML form takes
        # carTypes as repeated ``carType`` fields on the full-page
        # edit; create shortcuts that path).
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "oak-yard", "name": "Oakland Yard",
                "kind": "yard", "stationId": "oak",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "fre-spur", "name": "Fremont Spur",
                "kind": "spur", "stationId": "fre",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/operations/cars/create", data={
            "id": "c-100", "roadName": "SP", "roadNumber": "7890",
            "type": "Boxcar", "locationId": "oak-yard",
        })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Local Line",
        })
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Local Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["oak", "fre"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "25"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Local 101", "route": "r1",
            "trainType": "Freight", "trainClass": "2",
            "allowThroughCars": "true",
        })
        r = tc.get("/operations/trains/t1/build")
        r.raise_for_status()
    body = r.text
    assert "Manifest" in body
    assert "Local 101" in body
    # The sole car got picked and routed.
    assert "SP 7890" in body
    assert "Fremont Spur" in body
    # Both stops present with Pick up / Set out sections.
    assert "Pick up" in body
    assert "Set out" in body


def test_build_train_manifest_shows_ordered_car_roster(
    tmp_path: pathlib.Path,
) -> None:
    """The manifest page renders an ordered Cars card after Power
    with each car the train carries, so the user can assemble the
    consist in the correct physical order."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "fre", "name": "Fremont",
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "oak-yard", "name": "Oakland Yard",
                "kind": "yard", "stationId": "oak",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "fre-spur", "name": "Fremont Spur",
                "kind": "spur", "stationId": "fre",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/operations/cars/create", data={
            "id": "c-100", "roadName": "SP", "roadNumber": "7890",
            "type": "Boxcar", "locationId": "oak-yard",
        })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Local Line",
        })
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Local Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["oak", "fre"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "25"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Local 101", "route": "r1",
            "trainType": "Freight", "allowThroughCars": "true",
        })
        r = tc.get("/operations/trains/t1/build")
        r.raise_for_status()
    body = r.text
    # The Cars card and ordered list are present, with the car's
    # road + number and its join/leave stations.
    assert "<h3>Cars</h3>" in body
    assert 'class="manifest-cars-list"' in body
    assert "SP 7890" in body
    assert "Boards at Oakland" in body
    assert "sets out at Fremont Spur" in body


def test_passenger_train_build_omits_switch_list(
    tmp_path: pathlib.Path,
) -> None:
    """A passenger train renders Power + Cars but no per-stop
    switch list (no ``Pick up``/``Set out`` sections)."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "fre", "name": "Fremont",
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "oak-yard", "name": "Oakland Yard",
                "kind": "yard", "stationId": "oak",
                "carTypes": ["Passenger"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "fre-sta", "name": "Fremont Station",
                "kind": "station", "stationId": "fre",
                "carTypes": ["Passenger"],
            },
        })
        tc.post("/operations/cars/create", data={
            "id": "c-coach1", "roadName": "ATSF", "roadNumber": "900",
            "type": "Passenger", "locationId": "oak-yard",
        })
        tc.post("/operations/routes/create", data={
            "id": "r1", "name": "Varnish",
        })
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Varnish",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["oak", "fre"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "25"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "The Chief", "route": "r1",
            "trainType": "Passenger",
        })
        r = tc.get("/operations/trains/t1/build")
        r.raise_for_status()
    body = r.text
    assert "<h3>Power</h3>" in body
    assert "<h3>Cars</h3>" in body
    # Switch list sections are suppressed for passenger trains.
    assert "Pick up" not in body
    assert "Set out" not in body


def test_train_edit_build_button_saves_then_redirects(
    tmp_path: pathlib.Path,
) -> None:
    """The Build button on the train edit form must be a submit
    button that posts the form to /save with ``redirectTo=build``;
    the save endpoint then persists the current form state (including
    manually added/removed assigned cars) and returns an HX-Redirect
    header pointing at the manifest page. A regression test here
    guards against the prior behaviour where Build was a plain
    ``<a>`` that bypassed the form and dropped unsaved changes."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/cars/create", data={
            "id": "c1", "roadName": "ATSF", "roadNumber": "100",
            "type": "Passenger",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Express", "route": "r1",
        })
        # The edit page renders the Build button inside the form.
        edit_body = tc.get("/operations/trains/t1/edit").text
        assert (
            'type="submit" name="redirectTo" value="build"'
            in edit_body
        ), "Build is no longer a form submit with redirectTo=build"
        # Posting the form with redirectTo=build + assigned car
        # should save the car and emit an HX-Redirect header.
        r = tc.post("/operations/trains/t1/save", data={
            "id": "t1", "name": "Express", "route": "r1",
            "redirectTo": "build",
            "assignedCarIds": ["c1"],
        })
        assert r.status_code == 204, r.text
        assert r.headers.get("HX-Redirect") == (
            "/operations/trains/t1/build"
        )
    # And the car is persisted (not thrown away as it was pre-fix).
    with TestClient(handle.app) as tc:
        again = tc.get("/operations/trains/t1/edit").text
    assert 'data-assigned-car="c1"' in again


def test_manifest_warnings_use_names_not_ids(
    tmp_path: pathlib.Path,
) -> None:
    """Warnings must identify cars by road name + number, locations
    by name, and stations by name -- not raw ids. Regression for the
    build output that previously read like
    ``Assigned car 'car-1' is at 'dur-2', which isn't a station...``.
    """
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        # A route through Oakland only. The car sits at an off-route
        # location whose Station is NOT on the route, which trips the
        # "not a station on this train's route" warning branch.
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "fre", "name": "Fremont",
        })
        tc.post("/operations/stations/create", data={
            "id": "dur", "name": "Durango",
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "oak-yard", "name": "Oakland Yard",
                "kind": "yard", "stationId": "oak",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "fre-spur", "name": "Fremont Spur",
                "kind": "spur", "stationId": "fre",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "dur-2", "name": "Durango Ice House",
                "kind": "spur", "stationId": "dur",
                "carTypes": ["Boxcar"],
            },
        })
        tc.post("/operations/cars/create", data={
            "id": "car-1", "roadName": "RGS", "roadNumber": "2101",
            "type": "Boxcar", "locationId": "dur-2",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Mainline",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["oak", "fre"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "25"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "Local 101", "route": "r1",
            "trainType": "Freight", "allowThroughCars": "true",
        })
        tc.post("/operations/trains/t1/save", data={
            "id": "t1", "name": "Local 101", "route": "r1",
            "trainType": "Freight", "allowThroughCars": "true",
            "assignedCarIds": ["car-1"],
        })
        r = tc.get("/operations/trains/t1/build")
        r.raise_for_status()
    body = r.text
    # The warning text contains the names, not the raw ids.
    assert "RGS 2101" in body
    assert "Durango Ice House" in body
    # And the raw-id spellings are gone from the warnings block.
    import re as _re
    m = _re.search(
        r'<h3[^>]*>Warnings</h3>(.*?)</div>', body, _re.DOTALL,
    )
    assert m is not None, "No Warnings section found in manifest"
    warnings_block = m.group(1)
    assert "'car-1'" not in warnings_block
    assert "'dur-2'" not in warnings_block


def test_passenger_train_assigned_car_skips_location_check(
    tmp_path: pathlib.Path,
) -> None:
    """A passenger train's assigned car is placed on the consist
    end-to-end without checking whether its current Location is on
    the route -- these consists shuttle origin->terminal->origin and
    the switcher's "where's the car" test doesn't apply."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "fre", "name": "Fremont",
        })
        tc.post("/operations/stations/create", data={
            "id": "dur", "name": "Durango",
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "oak-yard", "name": "Oakland Yard",
                "kind": "yard", "stationId": "oak",
                "carTypes": ["Passenger"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "fre-sta", "name": "Fremont Station",
                "kind": "station", "stationId": "fre",
                "carTypes": ["Passenger"],
            },
        })
        tc.post("/json/", json={
            "type": "operations-locations", "method": "put",
            "data": {
                "id": "dur-sta", "name": "Durango Station",
                "kind": "station", "stationId": "dur",
                "carTypes": ["Passenger"],
            },
        })
        # Coach is parked at Durango -- a station NOT on the route.
        tc.post("/operations/cars/create", data={
            "id": "coach-1", "roadName": "ATSF", "roadNumber": "900",
            "type": "Passenger", "locationId": "dur-sta",
        })
        tc.post("/operations/routes/create", data={"id": "r1"})
        tc.post("/operations/routes/r1/save", data={
            "id": "r1", "name": "Varnish",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["oak", "fre"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "25"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t1", "name": "The Chief", "route": "r1",
            "trainType": "Passenger",
        })
        tc.post("/operations/trains/t1/save", data={
            "id": "t1", "name": "The Chief", "route": "r1",
            "trainType": "Passenger",
            "assignedCarIds": ["coach-1"],
        })
        r = tc.get("/operations/trains/t1/build")
        r.raise_for_status()
    body = r.text
    # The coach rides the consist even though it's "in the wrong place".
    assert "ATSF 900" in body
    # No "isn't a station on this train's route" warning.
    assert "isn't a station on this train" not in body


def test_branch_train_manifest_includes_mainline_extensions(
    tmp_path: pathlib.Path,
) -> None:
    """A branch train's build must walk the mainline stops the train
    has marked ``included``, not just the branch route locations.
    Regression for the bug where an assigned car sitting at a station
    on the mainline-leading-into-the-branch would warn "not on route"
    because the builder only saw the branch locations."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        # Mainline A-B-C (westbound: A=East, C=West). Branch starts
        # at C and runs to D. Train is on the branch but the
        # schedule marks B included (one mainline stop leading in).
        for sid, name in (
            ("sa", "Alpha"), ("sb", "Bravo"), ("sc", "Charlie"),
            ("sd", "Delta"),
        ):
            tc.post("/operations/stations/create", data={
                "id": sid, "name": name,
            })
        for lid, name, sid in (
            ("la", "Alpha Yard",  "sa"),
            ("lb", "Bravo Yard",  "sb"),
            ("lc", "Charlie Jct", "sc"),
            ("ld", "Delta Spur",  "sd"),
        ):
            tc.post("/json/", json={
                "type": "operations-locations", "method": "put",
                "data": {
                    "id": lid, "name": name, "kind": "yard",
                    "stationId": sid, "carTypes": ["Boxcar"],
                },
            })
        # Car sits on the mainline (Bravo Yard) -- not on the branch.
        tc.post("/operations/cars/create", data={
            "id": "car-mb", "roadName": "RGS", "roadNumber": "7",
            "type": "Boxcar", "locationId": "lb",
        })
        # Mainline route A->B->C.
        tc.post("/operations/routes/create", data={"id": "rmain"})
        tc.post("/operations/routes/rmain/save", data={
            "id": "rmain", "name": "Mainline",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["sa", "sb", "sc"],
            "stop_pickups":     ["yes", "yes", "yes"],
            "stop_drops":       ["yes", "yes", "yes"],
            "stop_maxCarMoves": ["5", "5", "5"],
            "stop_miles":       ["0", "10", "15"],
            "stop_stopType":    ["", "", ""],
        })
        # Branch route C->D (westbound), anchored at C on mainline.
        tc.post("/operations/routes/create", data={"id": "rbr"})
        tc.post("/operations/routes/rbr/save", data={
            "id": "rbr", "name": "Branch", "isBranch": "true",
            "mainlineRouteId": "rmain", "branchStartStopId": "sc",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["sc", "sd"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "20"],
            "stop_stopType":    ["", ""],
        })
        # Train on branch; the schedule includes Bravo as a mainline
        # extension leading into the branch.
        tc.post("/operations/trains/create", data={
            "id": "tb", "name": "Branch Local", "route": "rbr",
            "trainType": "Freight", "direction": "W",
            "allowThroughCars": "true",
        })
        # Save includes the mainline extension stop ``sb`` plus the
        # two branch stops ``sc`` and ``sd``.
        tc.post("/operations/trains/tb/save", data={
            "id": "tb", "name": "Branch Local", "route": "rbr",
            "trainType": "Freight", "direction": "W",
            "allowThroughCars": "true",
            "assignedCarIds":   ["car-mb"],
            "stop_stationId":   ["sb",   "sc",   "sd"],
            "stop_include":     ["true", "true", "true"],
            "stop_departHour":  ["8",    "9",    "10"],
            "stop_departMinute":["0",    "0",    "0"],
            "stop_wait":        ["0",    "0",    "0"],
        })
        r = tc.get("/operations/trains/tb/build")
        r.raise_for_status()
    body = r.text
    # The car at Bravo Yard is now routed through the branch train
    # -- no "not on route" warning.
    assert "isn't a station on this train" not in body
    # And its road name appears on the manifest.
    assert "RGS 7" in body


def test_build_train_without_route_warns(tmp_path: pathlib.Path) -> None:
    """A train with no route assigned still renders a manifest page
    but with a warning instead of exploding."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/trains/create", data={"id": "orphan"})
        r = tc.get("/operations/trains/orphan/build")
        r.raise_for_status()
    body = r.text
    assert "no route" in body.lower()


def test_trains_list_has_build_button(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/trains/create", data={"id": "t1", "name": "L1"})
        r = tc.get("/operations/trains")
    assert "/operations/trains/t1/build" in r.text


def test_cars_delete_keeps_location_names_on_remaining_rows(
    tmp_path: pathlib.Path,
) -> None:
    """After deleting one car, the remaining rows still render the
    parent location's NAME, not its raw id. Regression for a bug
    where the delete handler didn't pass location_name_by_id into
    the refreshed-table template."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        tc.post("/operations/locations/create", data={
            "id": "yard-a", "name": "Oakland Yard",
            "kind": "yard", "stationId": "oak",
        })
        tc.post("/operations/cars/create", data={
            "id": "c1", "type": "Boxcar", "locationId": "yard-a",
        })
        tc.post("/operations/cars/create", data={
            "id": "c2", "type": "Boxcar", "locationId": "yard-a",
        })
        r = tc.delete("/operations/cars/c1")
        r.raise_for_status()
    body = r.text
    # c2 still there, rendered with the location NAME, not the id.
    assert 'data-car="c2"' in body
    assert "Oakland Yard" in body


def test_engines_crud_round_trip(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.post("/operations/engines/create", data={
            "id": "e1", "roadName": "UP", "model": "SD40",
        })
        r.raise_for_status()
    assert 'data-engine="e1"' in r.text
    assert "SD40" in r.text


def test_locations_crud_round_trip(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.post("/operations/locations/create", data={
            "id": "yard-a", "name": "Oakland Yard",
        })
        r.raise_for_status()
    assert 'data-location="yard-a"' in r.text
    assert "Oakland Yard" in r.text


def test_cars_table_triggers_on_nudge_not_polling(
    tmp_path: pathlib.Path,
) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/cars/refresh")
        r.raise_for_status()
    body = r.text
    assert "pmri:operations-cars from:body" in body
    assert "every" not in body


def test_car_edit_round_trip(tmp_path: pathlib.Path) -> None:
    """Click Edit → form row; Save → updated display row."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/cars/create", data={
            "id": "c1", "roadName": "SP", "roadNumber": "100",
        })
        r = tc.get("/operations/cars/c1/edit")
        r.raise_for_status()
        assert "operations-edit-row" in r.text
        assert 'value="SP"' in r.text
        # Update: change road + number.
        r = tc.post("/operations/cars/c1/update", data={
            "id": "c1", "roadName": "UP", "roadNumber": "844",
        })
        r.raise_for_status()
        # Response is the fresh display row with the new values.
        body = r.text
        assert 'data-car="c1"' in body
        assert "UP" in body
        assert "844" in body
        # And the edit affordances are back, not the form fields.
        assert "operations-edit-row" not in body


def test_location_edit_direction_bitmask(tmp_path: pathlib.Path) -> None:
    """Direction checkboxes fold into the ``dir`` attribute bitmask."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/locations/create", data={
            "id": "yard-a", "name": "Oakland",
            "ops": "1",
            "dir_east": "1", "dir_west": "1",
        })
        # Verify via refresh that east+west got bitmask 3.
        r = tc.get("/operations/locations/refresh")
    body = r.text
    assert 'data-location="yard-a"' in body
    # Display row prints "E W" for bitmask 3.
    assert "E " in body and "W " in body
    assert "N " not in body
    assert "S " not in body


def test_location_edit_preserves_track_children(
    tmp_path: pathlib.Path,
) -> None:
    """A location pre-seeded with ``<types>`` + ``<track>`` children
    keeps them after an edit through the web UI (attributes are
    rewritten, children are not touched)."""
    from lxml import etree

    from pmri_core.persistence.operations_xml import (
        load_operations_bytes, save_operations_bytes, upsert_location,
        Location as _Location,
    )

    handle = _web_with_ops(tmp_path)
    store = handle.server.ctx._operations_store
    # Seed: add a location with a child track block via the raw file API.
    upsert_location(store._locations, _Location(id="yard-a", name="Oakland"))
    loc_elem = store._locations.root.find("locations").find("location")
    etree.SubElement(loc_elem, "types").append(
        etree.Element("carType", attrib={"name": "boxcar"}),
    )
    etree.SubElement(loc_elem, "track", attrib={
        "id": "yard-a-t1", "name": "Track 1",
    })
    # Flush seed to disk so a reload would see it.
    store._flush_locations()

    with TestClient(handle.app) as tc:
        r = tc.post("/operations/locations/yard-a/save", data={
            "id": "yard-a", "name": "Oakland Yard",
            "kind": "yard", "dir_east": "1",
        })
        r.raise_for_status()

    reloaded = load_operations_bytes((tmp_path / "dev" / "operations"
                                      / "OperationsLocationRoster.xml"
                                      ).read_bytes())
    loc_elem = reloaded.root.find("locations").find("location")
    assert loc_elem.get("name") == "Oakland Yard"
    # ``<track>`` children live alongside ``<types>`` and survive the
    # attribute-only rewrite in ``_upsert_by_id``.
    assert loc_elem.find("track") is not None
    assert loc_elem.find("track").get("id") == "yard-a-t1"


def test_import_from_roster_populates_engines(
    tmp_path: pathlib.Path,
) -> None:
    """Hitting Import pulls roster entries and adds them as engines.

    Pre-existing engines are left alone; re-running the import is a
    no-op for ids already present.
    """
    from pmri_core.beans.roster_entry import RosterEntry

    from pmri_server.roster_store import RosterStore

    profile = Profile(root=tmp_path, name="dev")
    profile.ensure()
    roster = RosterStore(profile=profile)
    roster.put(RosterEntry(
        id="loco-1", road_name="SP", road_number="4449", model="FEF-3",
    ))
    roster.put(RosterEntry(
        id="loco-2", road_name="UP", road_number="844", model="FEF-3",
    ))

    handle = build_web_app()
    handle.server.ctx._roster_profile = profile
    handle.server.ctx._operations_store = OperationsStore(profile=profile)
    with TestClient(handle.app) as tc:
        r = tc.post("/operations/engines/import_roster")
        r.raise_for_status()
    body = r.text
    assert 'data-engine="loco-1"' in body
    assert 'data-engine="loco-2"' in body
    assert "SP" in body
    assert "UP" in body


def test_location_kind_dropdown_lists_four_options(
    tmp_path: pathlib.Path,
) -> None:
    """The create form offers Yard/Spur/Interchange/Staging and nothing
    else -- a regression guard against reintroducing the old
    Normal/Staging binary."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/locations")
        r.raise_for_status()
    body = r.text
    assert 'value="yard"' in body
    assert 'value="spur"' in body
    assert 'value="interchange"' in body
    assert 'value="staging"' in body
    # Old binary shouldn't ship in the create form anymore.
    assert 'Normal (yard /' not in body


def test_location_full_page_editor_renders_car_type_matrix(
    tmp_path: pathlib.Path,
) -> None:
    """The /edit page shows a checkbox for each known car type, with
    the location's existing choices pre-checked."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        # Seed a couple of cars so the catalog surfaces their types,
        # then a location with one of those checked.
        tc.post("/operations/cars/create", data={"id": "c1", "type": "Boxcar"})
        tc.post("/operations/cars/create", data={"id": "c2", "type": "Reefer"})
        tc.post("/operations/locations/create", data={
            "id": "yard-a", "name": "Oakland", "kind": "yard",
        })
        # Save the matrix: check Boxcar only.
        tc.post("/operations/locations/yard-a/save", data={
            "id": "yard-a", "name": "Oakland", "kind": "yard",
            "carType": ["Boxcar"],
        })
        r = tc.get("/operations/locations/yard-a/edit")
        r.raise_for_status()
    body = r.text
    # Catalog has Boxcar (from car) and Reefer (from car) + baseline
    # defaults like Flatcar / Hopper.
    assert 'value="Boxcar"' in body
    assert 'value="Reefer"' in body
    assert 'value="Flatcar"' in body
    # Boxcar is pre-checked (saved earlier); Reefer isn't.
    # Look for the checked state near the Boxcar option.
    boxcar_pos = body.find('value="Boxcar"')
    # The checked attribute lives on the SAME input tag -- scan 100
    # chars before to catch the checked flag ordering htmx/jinja
    # emits.
    boxcar_window = body[max(0, boxcar_pos - 150): boxcar_pos + 50]
    assert "checked" in boxcar_window
    reefer_pos = body.find('value="Reefer"')
    reefer_window = body[max(0, reefer_pos - 150): reefer_pos + 50]
    assert "checked" not in reefer_window


def test_location_length_and_cars_count_surface_on_list(
    tmp_path: pathlib.Path,
) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/locations/create", data={
            "id": "yard-a", "name": "Oakland", "kind": "yard",
            "length": "2400",
        })
        tc.post("/operations/cars/create", data={
            "id": "c1", "locationId": "yard-a",
        })
        tc.post("/operations/cars/create", data={
            "id": "c2", "locationId": "yard-a",
        })
        tc.post("/operations/cars/create", data={
            "id": "c3", "locationId": "other",
        })
        r = tc.get("/operations/locations")
    body = r.text
    assert "2400" in body
    # Row cell for current cars at yard-a shows 2.
    row_start = body.find('data-location="yard-a"')
    row_end = body.find("</tr>", row_start)
    row = body[row_start:row_end]
    assert ">2<" in row    # the car-count cell


def test_routes_list_then_create_adds_row(
    tmp_path: pathlib.Path,
) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/routes")
        r.raise_for_status()
        assert "No routes yet" in r.text
        r = tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line", "comment": "weekday",
        })
        r.raise_for_status()
    body = r.text
    assert 'data-route="r-1"' in body
    assert "Local Line" in body


def test_route_edit_round_trip_with_stops(
    tmp_path: pathlib.Path,
) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        # Seed two stations + an empty route.
        tc.post("/operations/stations/create", data={
            "id": "stn-a", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "stn-b", "name": "Fremont",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        r = tc.get("/operations/routes/r-1/edit")
        r.raise_for_status()
        body = r.text
        assert 'value="stn-a"' in body
        assert 'value="stn-b"' in body
        r = tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Local Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":     ["stn-a", "stn-b"],
            "stop_pickups":       ["yes", "yes"],
            "stop_drops":         ["yes", "no"],
            "stop_maxCarMoves":   ["5", "3"],
            "stop_miles":         ["0", "25"],
            "stop_stopType":      ["s", ""],
        })
        r.raise_for_status()
        rr = tc.post("/json/", json={
            "type": "operations-routes", "method": "get",
            "data": {"id": "r-1"},
        })
        data = rr.json()["data"]
        assert [s["stationId"] for s in data["stops"]] == ["stn-a", "stn-b"]
        assert [s["sequenceId"] for s in data["stops"]] == ["1", "2"]
        assert data["stops"][1]["drops"] == "no"
        assert data["stops"][1]["miles"] == "25"
        assert data["stops"][0]["stopType"] == "s"
        assert data["direction"] == "EW"
        assert data["superiorDirection"] == "W"


def test_trains_list_and_create_round_trip(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        r = tc.post("/operations/trains/create", data={
            "id": "t-1", "name": "Local 101",
            "description": "Daily local", "route": "r-1",
            "departHour": "09", "departMinute": "30",
            "trainType": "Freight", "trainClass": "2",
        })
        r.raise_for_status()
    body = r.text
    assert 'data-train="t-1"' in body
    assert "Local 101" in body
    # The trains table now renders the route's human name, not its id.
    assert "Local Line" in body
    assert "Freight" in body
    assert "Class 2" in body


def test_train_edit_round_trip(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        tc.post("/operations/trains/create", data={
            "id": "t-1", "name": "Local 101", "route": "r-1",
        })
        r = tc.get("/operations/trains/t-1/edit")
        r.raise_for_status()
        # Dropdown pre-selects the current route.
        assert 'value="r-1" selected' in r.text or "selected>" in r.text
        r = tc.post("/operations/trains/t-1/save", data={
            "id": "t-1", "name": "Local 101", "description": "Daily local",
            "route": "r-1", "departHour": "10", "departMinute": "15",
            "buildNormal": "true",
        })
        r.raise_for_status()
        # Confirm persisted.
        rr = tc.post("/json/", json={
            "type": "operations-trains", "method": "get",
            "data": {"id": "t-1"},
        })
        data = rr.json()["data"]
        assert data["departHour"] == "10"
        assert data["departMinute"] == "15"
        assert data["buildNormal"] == "true"


def test_timetable_uses_miles_and_speed_between_stops(
    tmp_path: pathlib.Path,
) -> None:
    """Route stores distance (miles); train stores speed. Timetable
    computes per-stop arrivals as miles / speed. A train leaving
    Oakland at 09:42 running at 60 mph reaches Fremont (25 mi away)
    at 10:07."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "stn-a", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "stn-b", "name": "Fremont",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Local Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":     ["stn-a", "stn-b"],
            "stop_pickups":       ["yes", "yes"],
            "stop_drops":         ["yes", "no"],
            "stop_maxCarMoves":   ["5", "3"],
            "stop_miles":         ["0", "25"],
            "stop_stopType":      ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t-1", "name": "Local 101", "route": "r-1",
            "departHour": "09", "departMinute": "42",
            "trainType": "Freight", "trainClass": "2",
            "direction": "W", "averageSpeed": "60",
        })
        r = tc.get("/operations/timetable")
        r.raise_for_status()
    body = r.text
    assert "Local Line" in body
    assert "Oakland" in body
    assert "Fremont" in body
    assert "09:42" in body
    assert "10:07" in body


def test_timetable_station_view_lists_visits(
    tmp_path: pathlib.Path,
) -> None:
    """The station view shows every train that visits a station,
    sorted by arrival time."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "stn-a", "name": "Oakland",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Local Line",
            "stop_stationId":     ["stn-a"],
            "stop_pickups":       ["yes"],
            "stop_drops":         ["yes"],
            "stop_maxCarMoves":   ["5"],
            "stop_miles":         ["0"],
            "stop_stopType":      [""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t-early", "name": "Dawn", "route": "r-1",
            "departHour": "06", "departMinute": "00",
        })
        tc.post("/operations/trains/create", data={
            "id": "t-late", "name": "Dusk", "route": "r-1",
            "departHour": "18", "departMinute": "00",
        })
        r = tc.get("/operations/timetable?view=station")
        r.raise_for_status()
    body = r.text
    # Station picker selected; both trains visible in the visit list.
    assert "Oakland" in body
    assert "Dawn" in body
    assert "Dusk" in body
    # Earlier arrival comes before the later one in the HTML.
    assert body.index("Dawn") < body.index("Dusk")


def test_timetable_type_filter_hides_other_types(
    tmp_path: pathlib.Path,
) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/locations/create", data={
            "id": "yard-a", "name": "Oakland", "kind": "yard",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Local Line",
            "stop_stationId":     ["stn-a"],
            "stop_pickups":       ["yes"],
            "stop_drops":         ["yes"],
            "stop_maxCarMoves":   ["5"],
            "stop_miles":         ["0"],
            "stop_stopType":      [""],
        })
        tc.post("/operations/trains/create", data={
            "id": "pax", "name": "Zephyr", "route": "r-1",
            "departHour": "08", "trainType": "Passenger",
        })
        tc.post("/operations/trains/create", data={
            "id": "frt", "name": "Switcher", "route": "r-1",
            "departHour": "09", "trainType": "Freight",
        })
        r = tc.get("/operations/timetable?type=Passenger")
        r.raise_for_status()
    body = r.text
    assert "Zephyr" in body
    assert "Switcher" not in body


def test_timetable_separates_unscheduled_trains(
    tmp_path: pathlib.Path,
) -> None:
    """A train missing a route lands in the Not-scheduled list, not
    the main schedule."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/trains/create", data={
            "id": "t-x", "name": "Orphan",
        })
        r = tc.get("/operations/timetable")
    body = r.text
    assert "Not scheduled" in body
    assert "Orphan" in body


def test_route_list_counts_non_blank_stops(tmp_path: pathlib.Path) -> None:
    """The routes list counts stops with a non-blank station_id; the
    old bug showed 0 because the wire-shape ``stops`` key was dropped
    when projecting the Route model."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "a", "name": "A",
        })
        tc.post("/operations/stations/create", data={
            "id": "b", "name": "B",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Line",
            "stop_stationId":   ["a", "b", ""],   # one blank
            "stop_pickups":     ["yes", "yes", "yes"],
            "stop_drops":       ["yes", "yes", "yes"],
            "stop_maxCarMoves": ["5", "5", "5"],
            "stop_miles":       ["0", "10", "5"],
            "stop_stopType":    ["", "", ""],
        })
        r = tc.get("/operations/routes")
    body = r.text
    # Count non-blank station entries = 2, not 3, not 0.
    # Row should render a "2" in the stops column.
    assert "<td class=\"mono\">2</td>" in body


def test_create_route_detects_id_collision(tmp_path: pathlib.Path) -> None:
    """Creating a route with an existing id returns a 409 + banner."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Original",
        })
        r = tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Dupe",
        })
    assert r.status_code == 200
    assert "already exists" in r.text
    # Original wasn't overwritten.
    assert "Original" in r.text


def test_create_station_detects_id_collision(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        r = tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Different",
        })
    assert r.status_code == 200
    assert "already exists" in r.text


def test_create_car_detects_id_collision(tmp_path: pathlib.Path) -> None:
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/cars/create", data={
            "id": "c1", "type": "Boxcar",
        })
        r = tc.post("/operations/cars/create", data={
            "id": "c1", "type": "Reefer",
        })
    assert r.status_code == 200
    assert "already exists" in r.text


def test_cars_page_renders_type_dropdown(tmp_path: pathlib.Path) -> None:
    """The car create form's Type input is a select carrying the
    shared car-types catalog, not a free-text input."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/cars")
    body = r.text
    assert '<select name="type"' in body
    # Default catalog values show up.
    assert "Boxcar" in body
    assert "Reefer" in body


def test_branch_route_save_inherits_mainline_direction(
    tmp_path: pathlib.Path,
) -> None:
    """A branch route's Direction + Superior direction get inherited
    from its mainline on save, even if the form left them blank
    (the UI disables those selects when isBranch is checked)."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "junc", "name": "Junction",
        })
        tc.post("/operations/routes/create", data={
            "id": "main-1", "name": "Main",
        })
        tc.post("/operations/routes/main-1/save", data={
            "id": "main-1", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId": ["junc"], "stop_pickups": ["yes"],
            "stop_drops": ["yes"], "stop_maxCarMoves": ["5"],
            "stop_miles": ["0"], "stop_stopType": [""],
        })
        tc.post("/operations/routes/create", data={
            "id": "br-1", "name": "Branch",
        })
        # Save without passing direction/superiorDirection (those
        # fields are disabled on the UI when isBranch=true).
        tc.post("/operations/routes/br-1/save", data={
            "id": "br-1", "name": "Branch",
            "isBranch": "true",
            "mainlineRouteId": "main-1",
            "branchStartStopId": "junc",
        })
        rr = tc.post("/json/", json={
            "type": "operations-routes", "method": "get",
            "data": {"id": "br-1"},
        })
    data = rr.json()["data"]
    # Inherited from the mainline despite the form not sending them.
    assert data["direction"] == "EW"
    assert data["superiorDirection"] == "W"


def test_branch_train_offers_mainline_after_when_anchor_is_last(
    tmp_path: pathlib.Path,
) -> None:
    """Branch G-E (W-superior) ends at E on an A-B-C-D-E-F mainline.
    A train on the branch exits onto the main at E and could
    continue west; the editor should offer F (west of E on main)
    as a 'mainline-after' extension, not A-D."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid in "abcdefg":
            tc.post("/operations/stations/create", data={
                "id": sid, "name": sid.upper(),
            })
        # Mainline A-B-C-D-E-F west-superior.
        tc.post("/operations/routes/create", data={
            "id": "main", "name": "Main",
        })
        tc.post("/operations/routes/main/save", data={
            "id": "main", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["a", "b", "c", "d", "e", "f"],
            "stop_pickups":     ["yes"] * 6,
            "stop_drops":       ["yes"] * 6,
            "stop_maxCarMoves": ["5"] * 6,
            "stop_miles":       ["0", "5", "5", "5", "5", "5"],
            "stop_stopType":    [""] * 6,
        })
        # Branch G-E with superior W (so G is first, E is last).
        tc.post("/operations/routes/create", data={
            "id": "br", "name": "Branch",
        })
        tc.post("/operations/routes/br/save", data={
            "id": "br", "name": "Branch",
            "isBranch": "true",
            "mainlineRouteId": "main",
            "branchStartStopId": "e",
            "stop_stationId":   ["g", "e"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "4"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "br-train", "name": "Branch Local", "route": "br",
            "trainType": "Freight", "trainClass": "3", "direction": "W",
        })
        r = tc.get("/operations/trains/br-train/edit")
    body = r.text
    # G and E (branch stops) are present.
    assert "G" in body
    assert "E" in body
    # F should be offered as mainline-after extension.
    assert "F" in body
    assert "main, after branch" in body
    # A-D should NOT be offered (they're east of E; train exits
    # the branch at E heading west, not east).
    assert "main, before branch" not in body


def test_branch_train_offers_mainline_before_when_anchor_is_first(
    tmp_path: pathlib.Path,
) -> None:
    """Branch E-G (W-superior) starts at E on an A-B-C-D-E-F mainline.
    A train could have entered the branch at E, having come from
    the east on the main. Offer A-D (east of E) as mainline-before."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        for sid in "abcdefg":
            tc.post("/operations/stations/create", data={
                "id": sid, "name": sid.upper(),
            })
        tc.post("/operations/routes/create", data={
            "id": "main", "name": "Main",
        })
        tc.post("/operations/routes/main/save", data={
            "id": "main", "name": "Main",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId":   ["a", "b", "c", "d", "e", "f"],
            "stop_pickups":     ["yes"] * 6,
            "stop_drops":       ["yes"] * 6,
            "stop_maxCarMoves": ["5"] * 6,
            "stop_miles":       ["0", "5", "5", "5", "5", "5"],
            "stop_stopType":    [""] * 6,
        })
        # Branch E-G with superior W (E is first on the branch).
        tc.post("/operations/routes/create", data={
            "id": "br", "name": "Branch",
        })
        tc.post("/operations/routes/br/save", data={
            "id": "br", "name": "Branch",
            "isBranch": "true",
            "mainlineRouteId": "main",
            "branchStartStopId": "e",
            "stop_stationId":   ["e", "g"],
            "stop_pickups":     ["yes", "yes"],
            "stop_drops":       ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles":       ["0", "4"],
            "stop_stopType":    ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "br-train", "name": "Branch Local", "route": "br",
            "trainType": "Freight", "trainClass": "3", "direction": "W",
        })
        r = tc.get("/operations/trains/br-train/edit")
    body = r.text
    assert "main, before branch" in body
    assert "main, after branch" not in body


def test_stations_crud_round_trip(tmp_path: pathlib.Path) -> None:
    """The Stations page creates, edits, and deletes a Station; the
    station carries facilities + siding capacity."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/stations")
        r.raise_for_status()
        assert "No stations yet" in r.text
        r = tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
            "facilities": "TGCW", "sidingCapacity": "15",
        })
        r.raise_for_status()
        assert "Oakland" in r.text
        assert "TGCW" in r.text
        rr = tc.post("/json/", json={
            "type": "operations-stations", "method": "get",
            "data": {"id": "oak"},
        })
        data = rr.json()["data"]
        assert data["facilities"] == "TGCW"
        assert data["sidingCapacity"] == "15"


def test_location_requires_station_parent(tmp_path: pathlib.Path) -> None:
    """The Location create form exposes the parent-Station dropdown."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
        })
        r = tc.get("/operations/locations")
        r.raise_for_status()
        body = r.text
        assert 'name="stationId"' in body
        assert 'value="oak"' in body
        tc.post("/operations/locations/create", data={
            "id": "oak-yard", "name": "Oakland Yard",
            "kind": "yard", "stationId": "oak",
        })
        rr = tc.post("/json/", json={
            "type": "operations-locations", "method": "get",
            "data": {"id": "oak-yard"},
        })
    assert rr.json()["data"]["stationId"] == "oak"


def test_route_edit_page_has_direction_and_branch_fields(
    tmp_path: pathlib.Path,
) -> None:
    """The route editor exposes direction, superior-direction, branch
    toggle, and the mainline / branch-start pickers so a user can set
    up a branch line."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/routes/create", data={
            "id": "main-1", "name": "Main Line",
        })
        tc.post("/operations/routes/create", data={
            "id": "br-1", "name": "Branch Line",
        })
        r = tc.get("/operations/routes/br-1/edit")
        r.raise_for_status()
    body = r.text
    assert 'name="direction"' in body
    assert 'name="superiorDirection"' in body
    assert 'name="isBranch"' in body
    assert 'name="mainlineRouteId"' in body
    assert 'name="branchStartStopId"' in body
    # The other route shows up as a possible mainline.
    assert 'value="main-1"' in body
    assert "Main Line" in body


def test_route_edit_saves_branch_metadata(
    tmp_path: pathlib.Path,
) -> None:
    """Saving a branch route round-trips isBranch + mainline + branch
    start + direction inheritance."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "junction", "name": "Junction",
        })
        tc.post("/operations/routes/create", data={
            "id": "main-1", "name": "Main Line",
        })
        tc.post("/operations/routes/main-1/save", data={
            "id": "main-1", "name": "Main Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId": ["junction"],
            "stop_pickups": ["yes"], "stop_drops": ["yes"],
            "stop_maxCarMoves": ["5"], "stop_miles": ["0"],
            "stop_stopType": [""],
        })
        tc.post("/operations/routes/create", data={
            "id": "br-1", "name": "Branch",
        })
        tc.post("/operations/routes/br-1/save", data={
            "id": "br-1", "name": "Branch",
            "direction": "EW", "superiorDirection": "W",
            "isBranch": "true",
            "mainlineRouteId": "main-1",
            "branchStartStopId": "junction",
        })
        rr = tc.post("/json/", json={
            "type": "operations-routes", "method": "get",
            "data": {"id": "br-1"},
        })
    data = rr.json()["data"]
    assert data["isBranch"] == "true"
    assert data["mainlineRouteId"] == "main-1"
    assert data["branchStartStopId"] == "junction"


def test_train_type_is_selector_with_four_choices(
    tmp_path: pathlib.Path,
) -> None:
    """The ``Add a train`` form renders Type as a selector carrying
    the four canonical values rather than free text."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/trains")
        r.raise_for_status()
    body = r.text
    assert 'name="trainType"' in body
    for v in ("Passenger", "Freight", "Mixed", "Through"):
        assert f'value="{v}"' in body
    # Class selector too.
    assert 'name="trainClass"' in body
    for v in ("1", "2", "3", "X"):
        assert f'value="{v}"' in body


def test_train_edit_renders_per_stop_schedule_table(
    tmp_path: pathlib.Path,
) -> None:
    """Assigning a train to a route makes the edit page render one
    row per route stop, with Include/Depart/Wait inputs."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "stn-a", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "stn-b", "name": "Fremont",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Local Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId": ["stn-a", "stn-b"],
            "stop_pickups": ["yes", "yes"],
            "stop_drops": ["yes", "no"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles": ["0", "25"],
            "stop_stopType": ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t-1", "name": "Local 101", "route": "r-1",
            "trainType": "Freight", "trainClass": "2",
        })
        r = tc.get("/operations/trains/t-1/edit")
        r.raise_for_status()
    body = r.text
    assert "train-schedule-table" in body
    assert "Oakland" in body
    assert "Fremont" in body
    assert 'name="averageSpeed"' in body


def test_train_save_persists_per_stop_schedule(
    tmp_path: pathlib.Path,
) -> None:
    """Posting per-stop depart/wait arrays writes TrainStop entries
    that round-trip through the JSON topic."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "stn-a", "name": "Oakland",
        })
        tc.post("/operations/stations/create", data={
            "id": "stn-b", "name": "Fremont",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "Local Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "Local Line",
            "stop_stationId": ["stn-a", "stn-b"],
            "stop_pickups": ["yes", "yes"],
            "stop_drops": ["yes", "no"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles": ["0", "25"],
            "stop_stopType": ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "t-1", "name": "Local 101", "route": "r-1",
        })
        tc.post("/operations/trains/t-1/save", data={
            "id": "t-1", "name": "Local 101", "route": "r-1",
            "trainType": "Freight", "trainClass": "2",
            "direction": "W", "averageSpeed": "60",
            "departHour": "09", "departMinute": "42",
            "stop_stationId": ["stn-a", "stn-b"],
            "stop_include": ["true", "true"],
            "stop_departHour": ["09", "10"],
            "stop_departMinute": ["42", "07"],
            "stop_wait": ["0", "0"],
        })
        rr = tc.post("/json/", json={
            "type": "operations-trains", "method": "get",
            "data": {"id": "t-1"},
        })
    data = rr.json()["data"]
    stops = data.get("stops") or []
    assert len(stops) == 2
    assert stops[0]["stationId"] == "stn-a"
    assert stops[0]["departHour"] == "09"
    assert stops[1]["stationId"] == "stn-b"
    assert stops[1]["departHour"] == "10"
    assert data["trainClass"] == "2"
    assert data["direction"] == "W"
    assert data["averageSpeed"] == "60"


def test_timetable_by_route_view_renders_classic_table(
    tmp_path: pathlib.Path,
) -> None:
    """The ``By Route`` view renders superior/opposite columns with
    the direction banner and the station column in the middle."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/stations/create", data={
            "id": "ala", "name": "Alameda", "facilities": "TG",
        })
        tc.post("/operations/stations/create", data={
            "id": "oak", "name": "Oakland",
            "facilities": "CW", "sidingCapacity": "20",
        })
        tc.post("/operations/routes/create", data={
            "id": "r-1", "name": "East Bay Line",
        })
        tc.post("/operations/routes/r-1/save", data={
            "id": "r-1", "name": "East Bay Line",
            "direction": "EW", "superiorDirection": "W",
            "stop_stationId": ["ala", "oak"],
            "stop_pickups": ["yes", "yes"],
            "stop_drops": ["yes", "yes"],
            "stop_maxCarMoves": ["5", "5"],
            "stop_miles": ["0", "10"],
            "stop_stopType": ["", ""],
        })
        tc.post("/operations/trains/create", data={
            "id": "wb-1", "name": "Westbound Local", "route": "r-1",
            "trainType": "Freight", "trainClass": "1",
            "direction": "W", "departHour": "08", "departMinute": "00",
            "averageSpeed": "60",
        })
        tc.post("/operations/trains/create", data={
            "id": "eb-1", "name": "Eastbound Local", "route": "r-1",
            "trainType": "Freight", "trainClass": "2",
            "direction": "E", "departHour": "14", "departMinute": "00",
            "averageSpeed": "60",
        })
        r = tc.get("/operations/timetable?view=route&route=r-1")
        r.raise_for_status()
    body = r.text
    assert "Westward" in body
    assert "Eastward" in body
    assert "East Bay Line" in body
    assert "Westbound Local" in body
    assert "Eastbound Local" in body
    # Class labels show up next to the train columns.
    assert "Class 1" in body
    assert "Class 2" in body
    # Facilities + siding-capacity column header + value surface.
    assert "TG" in body
    assert "Siding" in body
    assert ">20<" in body   # siding capacity for oak
    # Per Stop sub-columns are present on both sides.
    assert body.count("Per Stop") >= 2
    assert "From Start" in body
    assert "From End" in body


def test_train_edit_page_renders_both_time_faces(
    tmp_path: pathlib.Path,
) -> None:
    """Depart-time editor exposes the canonical 24h inputs plus the
    12h overlay + toggle so the UI JS can swap between them."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        tc.post("/operations/trains/create", data={
            "id": "t-1", "name": "Local 101",
            "departHour": "14", "departMinute": "30",
        })
        r = tc.get("/operations/trains/t-1/edit")
        r.raise_for_status()
    body = r.text
    # 24h canonical inputs carry the raw stored values.
    assert 'name="departHour"' in body and 'value="14"' in body
    assert 'name="departMinute"' in body and 'value="30"' in body
    # 12h overlay + toggle present via the shared widget.
    assert 'data-role="hour12"' in body
    assert 'data-role="ampm"' in body
    assert 'data-role="depart-12h"' in body
    # Widget carries a scoping id so the setup script finds its own
    # wrapper rather than grabbing the first form on the page (the
    # logout form in base.html).
    assert 'data-depart-widget="edit"' in body


def test_train_create_form_includes_depart_widget(
    tmp_path: pathlib.Path,
) -> None:
    """The ``Add a train`` form on /operations/trains renders the
    shared 12h/24h widget so a user can enter a departure time in
    either format at creation time."""
    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        r = tc.get("/operations/trains")
        r.raise_for_status()
    body = r.text
    assert 'data-depart-widget="create"' in body
    assert 'data-role="depart-12h"' in body
    assert 'name="departHour"' in body
    assert 'name="departMinute"' in body


def test_create_car_fires_websocket_nudge(tmp_path: pathlib.Path) -> None:
    """Regression that the mutation route broadcasts the right topic
    on the ``/ws/nudge`` channel so other viewers refresh in sync."""
    import json

    handle = _web_with_ops(tmp_path)
    with TestClient(handle.app) as tc:
        with tc.websocket_connect("/ws/nudge") as ws:
            tc.post("/operations/cars/create", data={"id": "c1"})
            frame = json.loads(ws.receive_text())
    assert frame["topic"] == "operations-cars"
