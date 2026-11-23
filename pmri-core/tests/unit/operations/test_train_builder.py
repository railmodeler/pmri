"""Tests for the Operations Pro train builder."""
from __future__ import annotations

from pmri_core.operations import build_train
from pmri_core.persistence.operations_xml import (
    Car, Engine, Location, Route, RouteLocation, Station, Train,
)


def _route(stop_ids: list[str], **kwargs) -> Route:
    """Build a Route whose stops reference the given station ids.
    Defaults: pickups/drops yes, maxCarMoves 5, miles 0."""
    locations = []
    for i, sid in enumerate(stop_ids):
        locations.append(RouteLocation(
            id=f"rl{i + 1}",
            stationId=sid,
            pickups="yes",
            drops="yes",
            maxCarMoves="5",
            miles="0" if i == 0 else "10",
        ))
    return Route(id="r1", name="Line", locations=locations, **kwargs)


def _train(**kwargs) -> Train:
    base = dict(
        id="t1", name="Local", route="r1",
        trainType="Freight", trainClass="2",
    )
    base.update(kwargs)
    return Train(**base)


def test_route_with_single_stop_warns_and_skips_pick_drop() -> None:
    route = _route(["a"])
    manifest = build_train(
        train=_train(), route=route, stations=[Station(id="a")],
        locations=[], cars=[], engines=[],
    )
    # The sole stop renders, but no picks/drops happen and a warning
    # explains why.
    assert len(manifest.stops) == 1
    assert manifest.stops[0].picks == ()
    assert manifest.stops[0].drops == ()
    assert any("at least two stops" in w for w in manifest.warnings)


def test_basic_pickup_to_terminal() -> None:
    """One car at origin whose type is served by the terminal's
    Location gets picked and routed there."""
    stations = [Station(id="a", name="A"), Station(id="b", name="B")]
    locations = [
        Location(id="a-yard", name="A Yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", name="B Spur", stationId="b", carTypes=["Boxcar"]),
    ]
    cars = [Car(id="c1", type="Boxcar", locationId="a-yard")]
    manifest = build_train(
        train=_train(allowThroughCars="true"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    assert len(manifest.stops) == 2
    picks = manifest.stops[0].picks
    drops = manifest.stops[1].drops
    assert len(picks) == 1
    assert picks[0].car_id == "c1"
    assert picks[0].to_station_id == "b"
    assert picks[0].to_location_id == "b-spur"
    assert len(drops) == 1
    assert drops[0].car_id == "c1"
    assert manifest.warnings == ()


def test_through_cars_blocked_unless_opted_in() -> None:
    """A car going straight from origin to terminal with no
    intermediate stop is a 'through' move; blocked when the train
    has allow_through_cars != 'true'."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", stationId="b", carTypes=["Boxcar"]),
    ]
    cars = [Car(id="c1", type="Boxcar", locationId="a-yard")]
    # No allow_through_cars -> the car can't be placed.
    manifest = build_train(
        train=_train(),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    assert manifest.stops[0].picks == ()
    assert manifest.stops[1].drops == ()


def test_car_type_filter_at_destination() -> None:
    """A car whose type isn't in any destination Location's car_types
    doesn't get routed; the builder warns about it."""
    stations = [Station(id=s) for s in ("a", "b", "c")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar", "Tank"]),
        # B serves only Tank; C serves only Boxcar.
        Location(id="b-spur", stationId="b", carTypes=["Tank"]),
        Location(id="c-spur", stationId="c", carTypes=["Boxcar"]),
    ]
    # Tank car at A: only B will accept it.
    # Hopper car at A: neither B nor C accepts -> warning.
    cars = [
        Car(id="tank-1", type="Tank", locationId="a-yard"),
        Car(id="hop-1", type="Hopper", locationId="a-yard"),
    ]
    manifest = build_train(
        train=_train(allowThroughCars="true"),
        route=_route(["a", "b", "c"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    all_picks = [p for stop in manifest.stops for p in stop.picks]
    picked_ids = {p.car_id for p in all_picks}
    assert "tank-1" in picked_ids
    assert "hop-1" not in picked_ids
    assert any("hop-1" in w for w in manifest.warnings)
    # Tank goes to B (first match), not C.
    tank_pick = next(p for p in all_picks if p.car_id == "tank-1")
    assert tank_pick.to_station_id == "b"


def test_max_car_moves_caps_stop_activity() -> None:
    """When max_car_moves at the origin is 2, only 2 cars get picked
    even if more are available."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", stationId="b", carTypes=["Boxcar"]),
    ]
    cars = [
        Car(id=f"c{n}", type="Boxcar", locationId="a-yard")
        for n in range(5)
    ]
    route = Route(
        id="r1", name="Line",
        locations=[
            RouteLocation(
                id="rl1", stationId="a",
                pickups="yes", drops="yes", maxCarMoves="2",
            ),
            RouteLocation(
                id="rl2", stationId="b",
                pickups="yes", drops="yes", maxCarMoves="5",
            ),
        ],
    )
    manifest = build_train(
        train=_train(allowThroughCars="true"),
        route=route, stations=stations, locations=locations,
        cars=cars, engines=[],
    )
    assert len(manifest.stops[0].picks) == 2
    assert manifest.stops[0].moves_used == 2
    assert manifest.stops[0].moves_cap == 2


def test_engines_picked_from_origin_honoring_count_and_model() -> None:
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", stationId="b", carTypes=["Boxcar"]),
    ]
    engines = [
        Engine(id="e1", roadName="UP", model="SD40", locationId="a-yard"),
        Engine(id="e2", roadName="UP", model="SD70", locationId="a-yard"),
        Engine(id="e3", roadName="BN", model="SD40", locationId="a-yard"),
        # Engine not at origin shouldn't be considered.
        Engine(id="e-wrong", roadName="UP", model="SD40", locationId="b-spur"),
    ]
    manifest = build_train(
        train=_train(numberEngines="1", engineModel="SD40"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=[], engines=engines,
    )
    assert len(manifest.engines) == 1
    assert manifest.engines[0].model == "SD40"
    assert manifest.engines[0].engine_id in {"e1", "e3"}
    assert manifest.warnings == ()


def test_engines_warning_when_fewer_available_than_requested() -> None:
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a"),
        Location(id="b-spur", stationId="b"),
    ]
    manifest = build_train(
        train=_train(numberEngines="2"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=[], engines=[],
    )
    assert manifest.engines == ()
    assert any("engine" in w.lower() for w in manifest.warnings)


def test_caboose_required_and_dropped_at_terminal() -> None:
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Caboose"]),
        Location(id="b-cab", stationId="b", carTypes=["Caboose"]),
    ]
    cars = [Car(id="cab-1", type="Caboose", roadName="UP", locationId="a-yard")]
    manifest = build_train(
        train=_train(requires="1"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    assert manifest.caboose is not None
    assert manifest.caboose.car_id == "cab-1"
    # Caboose shouldn't double-count as a pickup; it shows up as a drop
    # at the terminal.
    terminal_drops = manifest.stops[-1].drops
    assert any(d.car_id == "cab-1" for d in terminal_drops)
    # No pick at origin for the caboose (it's assigned implicitly).
    origin_picks = manifest.stops[0].picks
    assert not any(p.car_id == "cab-1" for p in origin_picks)


def test_passenger_train_skips_caboose_requirement() -> None:
    """A Passenger train never carries a caboose even if the requires
    bitmask is set. No warning, no caboose on the manifest."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-plat", stationId="a"),
        Location(id="b-plat", stationId="b"),
    ]
    manifest = build_train(
        train=_train(trainType="Passenger", requires="1"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=[], engines=[],
    )
    assert manifest.caboose is None
    assert not any("caboose" in w.lower() for w in manifest.warnings)


def test_mixed_train_also_skips_caboose() -> None:
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-plat", stationId="a"),
        Location(id="b-plat", stationId="b"),
    ]
    manifest = build_train(
        train=_train(trainType="Mixed", requires="1"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=[], engines=[],
    )
    assert manifest.caboose is None
    assert not any("caboose" in w.lower() for w in manifest.warnings)


def test_pinned_engine_id_overrides_road_and_model_filter() -> None:
    """When train.engine_id is set, that specific engine is picked
    verbatim, regardless of engine_road / engine_model filters."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a"),
        Location(id="b-spur", stationId="b"),
    ]
    engines = [
        Engine(id="e-pinned", roadName="BN", model="SD70", locationId="a-yard"),
        Engine(id="e-other", roadName="UP", model="SD40", locationId="a-yard"),
    ]
    manifest = build_train(
        train=_train(
            engineId="e-pinned",
            # These filters would otherwise exclude the pinned engine.
            engineRoad="UP", engineModel="SD40",
        ),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=[], engines=engines,
    )
    assert len(manifest.engines) == 1
    assert manifest.engines[0].engine_id == "e-pinned"


def test_pinned_engine_missing_from_roster_warns() -> None:
    manifest = build_train(
        train=_train(engineId="ghost"),
        route=_route(["a", "b"]),
        stations=[Station(id=s) for s in ("a", "b")],
        locations=[
            Location(id="a-yard", stationId="a"),
            Location(id="b-spur", stationId="b"),
        ],
        cars=[], engines=[],
    )
    assert manifest.engines == ()
    assert any("ghost" in w for w in manifest.warnings)


def test_pinned_caboose_id_bypasses_road_filter() -> None:
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Caboose"]),
        Location(id="b-cab", stationId="b", carTypes=["Caboose"]),
    ]
    cars = [
        Car(id="cab-pinned", type="Caboose", roadName="ATSF", locationId="a-yard"),
        Car(id="cab-other", type="Caboose", roadName="UP", locationId="a-yard"),
    ]
    manifest = build_train(
        train=_train(requires="1", cabooseId="cab-pinned", cabooseRoad="UP"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    assert manifest.caboose is not None
    assert manifest.caboose.car_id == "cab-pinned"


def test_assigned_car_picked_first_and_routed() -> None:
    """A manually-assigned car gets a pick/drop on the manifest
    before auto-selection runs."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", stationId="b", carTypes=["Boxcar"]),
    ]
    cars = [
        Car(id="assigned", type="Boxcar", roadName="SP", locationId="a-yard"),
        Car(id="auto-1", type="Boxcar", roadName="UP", locationId="a-yard"),
    ]
    manifest = build_train(
        train=_train(
            allowThroughCars="true",
            assignedCarIds=["assigned"],
        ),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    all_picks = [p for stop in manifest.stops for p in stop.picks]
    picked_ids = [p.car_id for p in all_picks]
    assert "assigned" in picked_ids


def test_assigned_car_bypasses_through_cars_restriction() -> None:
    """A passenger-style train without allowThroughCars can still
    carry an assigned car end-to-end (the user explicitly wants it)."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-plat", stationId="a", carTypes=["Passenger"]),
        Location(id="b-plat", stationId="b", carTypes=["Passenger"]),
    ]
    cars = [
        Car(id="coach-1", type="Passenger", roadName="SP", locationId="a-plat"),
    ]
    manifest = build_train(
        train=_train(trainType="Passenger", assignedCarIds=["coach-1"]),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=cars, engines=[],
    )
    assert any(
        p.car_id == "coach-1" for stop in manifest.stops for p in stop.picks
    )


def test_assigned_car_not_in_roster_warns() -> None:
    manifest = build_train(
        train=_train(assignedCarIds=["ghost"]),
        route=_route(["a", "b"]),
        stations=[Station(id=s) for s in ("a", "b")],
        locations=[
            Location(id="a-yard", stationId="a"),
            Location(id="b-spur", stationId="b"),
        ],
        cars=[], engines=[],
    )
    assert any("ghost" in w for w in manifest.warnings)


def test_caboose_required_but_none_available_warns() -> None:
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a"),
        Location(id="b-spur", stationId="b"),
    ]
    manifest = build_train(
        train=_train(requires="1"),
        route=_route(["a", "b"]),
        stations=stations, locations=locations, cars=[], engines=[],
    )
    assert manifest.caboose is None
    assert any("caboose" in w.lower() for w in manifest.warnings)


def test_build_is_reproducible_across_runs() -> None:
    """Same inputs + same train.id -> same manifest. Stability so
    the operator sees consistent output and so the Dispatcher can
    bank on a given train's plan."""
    stations = [Station(id=s) for s in ("a", "b")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", stationId="b", carTypes=["Boxcar"]),
    ]
    cars = [
        Car(id="c1", type="Boxcar", locationId="a-yard"),
        Car(id="c2", type="Boxcar", locationId="a-yard"),
        Car(id="c3", type="Boxcar", locationId="a-yard"),
    ]
    route = _route(["a", "b"])
    args = dict(
        train=_train(allowThroughCars="true"),
        route=route, stations=stations, locations=locations,
        cars=cars, engines=[],
    )
    first = build_train(**args)
    second = build_train(**args)
    assert [p.car_id for p in first.stops[0].picks] == [
        p.car_id for p in second.stops[0].picks
    ]


def test_drops_at_stop_not_accepting_them_are_skipped() -> None:
    """A middle stop with drops='no' doesn't receive any cars even
    if its Locations serve the car type."""
    stations = [Station(id=s) for s in ("a", "b", "c")]
    locations = [
        Location(id="a-yard", stationId="a", carTypes=["Boxcar"]),
        Location(id="b-spur", stationId="b", carTypes=["Boxcar"]),
        Location(id="c-spur", stationId="c", carTypes=["Boxcar"]),
    ]
    cars = [Car(id="c1", type="Boxcar", locationId="a-yard")]
    route = Route(
        id="r1", name="Line",
        locations=[
            RouteLocation(
                id="rl1", stationId="a",
                pickups="yes", drops="yes", maxCarMoves="5",
            ),
            RouteLocation(
                id="rl2", stationId="b",
                pickups="yes", drops="no", maxCarMoves="5",
            ),
            RouteLocation(
                id="rl3", stationId="c",
                pickups="yes", drops="yes", maxCarMoves="5",
            ),
        ],
    )
    manifest = build_train(
        train=_train(allowThroughCars="true"),
        route=route, stations=stations, locations=locations,
        cars=cars, engines=[],
    )
    # c1 should have landed at c (skipping b), via through.
    drop_ids = {d.car_id for stop in manifest.stops for d in stop.drops}
    assert "c1" in drop_ids
    # Specifically dropped at c, not b.
    assert manifest.stops[1].drops == ()
    assert any(d.car_id == "c1" for d in manifest.stops[-1].drops)
