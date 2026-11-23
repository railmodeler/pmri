"""Unit tests for the operations_xml edit operations."""
from __future__ import annotations

import pytest

from pmri_core.persistence.operations_xml import (
    Car,
    Engine,
    Location,
    OperationsEditError,
    Route,
    RouteLocation,
    Train,
    empty_operations,
    load_operations_bytes,
    remove_car,
    remove_engine,
    remove_location,
    remove_route,
    remove_train,
    save_operations_bytes,
    upsert_car,
    upsert_engine,
    upsert_location,
    upsert_route,
    upsert_train,
)


# ----- cars ---------------------------------------------------------------


def test_upsert_car_creates_and_round_trips() -> None:
    file = empty_operations()
    upsert_car(file, Car(
        id="car-1", roadName="SP", roadNumber="4449",
        type="boxcar", length="40", locationId="yard-a", load="E",
    ))
    model = load_operations_bytes(save_operations_bytes(file))
    cars = model.cars()
    assert len(cars) == 1
    c = cars[0]
    assert c.id == "car-1"
    assert c.road_name == "SP"
    assert c.road_number == "4449"
    assert c.type == "boxcar"
    assert c.load == "E"


def test_upsert_car_overwrites_existing() -> None:
    file = empty_operations()
    upsert_car(file, Car(id="c1", roadName="SP", roadNumber="100"))
    upsert_car(file, Car(id="c1", roadName="UP", roadNumber="200"))
    cars = load_operations_bytes(save_operations_bytes(file)).cars()
    assert len(cars) == 1
    assert cars[0].road_name == "UP"
    assert cars[0].road_number == "200"


def test_upsert_car_preserves_extra_attributes() -> None:
    """Unknown JMRI attributes round-trip via the ``extra`` overflow."""
    file = empty_operations()
    upsert_car(file, Car(
        id="c1", roadName="SP",
        extra={"weightTons": "35.5", "color": "boxcar red"},
    ))
    car = load_operations_bytes(save_operations_bytes(file)).cars()[0]
    assert car.extra["weightTons"] == "35.5"
    assert car.extra["color"] == "boxcar red"


def test_upsert_car_rejects_blank_id() -> None:
    file = empty_operations()
    with pytest.raises(OperationsEditError, match="id is required"):
        upsert_car(file, Car(id=""))


def test_remove_car_drops_it() -> None:
    file = empty_operations()
    upsert_car(file, Car(id="c1"))
    upsert_car(file, Car(id="c2"))
    assert remove_car(file, "c1") is True
    cars = load_operations_bytes(save_operations_bytes(file)).cars()
    assert [c.id for c in cars] == ["c2"]


def test_remove_car_missing_is_false() -> None:
    file = empty_operations()
    assert remove_car(file, "nope") is False


# ----- engines ------------------------------------------------------------


def test_upsert_engine_round_trips() -> None:
    file = empty_operations()
    upsert_engine(file, Engine(
        id="eng-1", roadName="SP", roadNumber="4449",
        model="FEF-3", type="Steam", length="100",
    ))
    engines = load_operations_bytes(save_operations_bytes(file)).engines()
    assert len(engines) == 1
    e = engines[0]
    assert e.id == "eng-1"
    assert e.model == "FEF-3"
    assert e.length == "100"


def test_remove_engine_drops_it() -> None:
    file = empty_operations()
    upsert_engine(file, Engine(id="e1"))
    assert remove_engine(file, "e1") is True
    assert load_operations_bytes(save_operations_bytes(file)).engines() == []


# ----- locations ----------------------------------------------------------


def test_upsert_location_round_trips() -> None:
    file = empty_operations()
    upsert_location(file, Location(id="loc-1", name="Oakland Yard"))
    locs = load_operations_bytes(save_operations_bytes(file)).locations()
    assert len(locs) == 1
    assert locs[0].id == "loc-1"
    assert locs[0].name == "Oakland Yard"


def test_remove_location_drops_it() -> None:
    file = empty_operations()
    upsert_location(file, Location(id="l1"))
    assert remove_location(file, "l1") is True
    assert load_operations_bytes(save_operations_bytes(file)).locations() == []


def test_location_kind_and_length_round_trip() -> None:
    file = empty_operations()
    upsert_location(file, Location(
        id="yard-a", name="Oakland", ops="1",
        kind="yard", length="2400",
    ))
    locs = load_operations_bytes(save_operations_bytes(file)).locations()
    l = locs[0]
    assert l.kind == "yard"
    assert l.length == "2400"


def test_location_car_types_round_trip() -> None:
    file = empty_operations()
    upsert_location(file, Location(
        id="yard-a", name="Oakland",
        car_types=["Boxcar", "Flatcar", "Reefer"],
    ))
    locs = load_operations_bytes(save_operations_bytes(file)).locations()
    assert locs[0].car_types == ["Boxcar", "Flatcar", "Reefer"]


def test_location_car_types_update_preserves_loco_types() -> None:
    """``<locoType>`` siblings inside ``<types>`` must survive a
    carType-only edit -- we only rewrite the carType entries."""
    from lxml import etree

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<operations-config>'
        '<locations>'
        '<location id="yard-a" name="Oakland" ops="1">'
        '<types>'
        '<carType name="Boxcar"/>'
        '<locoType name="Diesel"/>'
        '</types>'
        '</location>'
        '</locations>'
        '</operations-config>'
    ).encode()
    file = load_operations_bytes(xml)
    upsert_location(file, Location(
        id="yard-a", name="Oakland", ops="1",
        car_types=["Flatcar", "Reefer"],
    ))
    saved = save_operations_bytes(file)
    # Check at the lxml level: locoType is preserved, carTypes rewritten.
    reloaded = load_operations_bytes(saved)
    loc_elem = reloaded.root.find("locations").find("location")
    types_elem = loc_elem.find("types")
    car_names = [c.get("name") for c in types_elem.findall("carType")]
    loco_names = [c.get("name") for c in types_elem.findall("locoType")]
    assert car_names == ["Flatcar", "Reefer"]
    assert loco_names == ["Diesel"]


def test_location_with_no_car_types_omits_empty_types_wrapper() -> None:
    """A location with no car types and no loco types shouldn't carry
    a bare ``<types/>`` element in the serialised output."""
    file = empty_operations()
    upsert_location(file, Location(id="yard-a", name="Oakland"))
    saved = save_operations_bytes(file).decode()
    assert "<types" not in saved


def test_route_upsert_writes_ordered_locations() -> None:
    file = empty_operations()
    upsert_route(file, Route(
        id="r-1", name="Local Line",
        locations=[
            RouteLocation(
                id="", name="Oakland", stationId="yard-a",
                pickups="yes", drops="yes",
            ),
            RouteLocation(
                id="", name="Fremont", stationId="yard-b",
                pickups="yes", drops="no",
            ),
        ],
    ))
    [route] = load_operations_bytes(save_operations_bytes(file)).routes()
    assert route.name == "Local Line"
    assert [rl.station_id for rl in route.locations] == ["yard-a", "yard-b"]
    assert [rl.sequence_id for rl in route.locations] == ["1", "2"]
    assert route.locations[0].pickups == "yes"
    assert route.locations[1].drops == "no"


def test_route_update_replaces_locations() -> None:
    """Reposting a route with a different stops list replaces the
    old stops, not merging them."""
    file = empty_operations()
    upsert_route(file, Route(
        id="r-1", name="v1",
        locations=[RouteLocation(id="", stationId="A")],
    ))
    upsert_route(file, Route(
        id="r-1", name="v2",
        locations=[
            RouteLocation(id="", stationId="X"),
            RouteLocation(id="", stationId="Y"),
        ],
    ))
    [route] = load_operations_bytes(save_operations_bytes(file)).routes()
    assert route.name == "v2"
    assert [rl.station_id for rl in route.locations] == ["X", "Y"]


def test_remove_route_drops_it() -> None:
    file = empty_operations()
    upsert_route(file, Route(id="r-1"))
    assert remove_route(file, "r-1") is True
    assert load_operations_bytes(save_operations_bytes(file)).routes() == []


def test_train_upsert_embeds_route_reference() -> None:
    file = empty_operations()
    upsert_train(file, Train(
        id="t-1", name="Local 101", description="Daily local",
        route="r-1", departHour="09", departMinute="30",
    ))
    trains = load_operations_bytes(save_operations_bytes(file)).trains()
    assert len(trains) == 1
    t = trains[0]
    assert t.name == "Local 101"
    assert t.route_id == "r-1"
    assert t.depart_hour == "09"
    assert t.depart_minute == "30"
    # Verify the nested <route id> child JMRI expects is present.
    saved = save_operations_bytes(file).decode()
    assert '<route id="r-1"' in saved


def test_train_update_replaces_route_reference() -> None:
    file = empty_operations()
    upsert_train(file, Train(id="t-1", route="r-1"))
    upsert_train(file, Train(id="t-1", route="r-2"))
    saved = save_operations_bytes(file).decode()
    assert '<route id="r-2"' in saved
    # Original r-1 reference gone.
    assert '<route id="r-1"' not in saved


def test_remove_train_drops_it() -> None:
    file = empty_operations()
    upsert_train(file, Train(id="t-1"))
    assert remove_train(file, "t-1") is True
    assert load_operations_bytes(save_operations_bytes(file)).trains() == []


def test_mutating_a_loaded_file_preserves_unknown_siblings() -> None:
    """A file carrying ``<operations>``, ``<cars>``, and some unmodeled
    element should still round-trip the unmodeled section verbatim after
    an upsert into one of the modeled ones."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<operations-config>'
        '<operations><setup realName="Test &amp; Co"/></operations>'
        '<cars><car id="c0" roadName="SP"/></cars>'
        '<schedules><schedule id="sched-1" name="daily"/></schedules>'
        '</operations-config>'
    ).encode()
    file = load_operations_bytes(xml)
    upsert_car(file, Car(id="c1", roadName="UP"))
    out = save_operations_bytes(file)
    reloaded = load_operations_bytes(out)
    # New car landed next to the existing one.
    car_ids = {c.id for c in reloaded.cars()}
    assert car_ids == {"c0", "c1"}
    # Unmodeled <schedules> survived.
    assert reloaded.root.find("schedules") is not None
    # Setup block intact.
    setup = reloaded.setup()
    assert setup.get("setup", {}).get("realName") == "Test & Co"
