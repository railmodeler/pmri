"""Operations Pro train builder.

Ports the essence of ``jmri.jmrit.operations.trains.TrainBuilder``:
given a Train plus its Route, the active Station / Location / Car /
Engine rosters, produce a :class:`Manifest` describing

* engine assignments at the origin,
* a caboose assignment at the origin (when the train's ``requires``
  bitmask demands one),
* per-stop pickups and drops, honoring the route's pickups/drops
  flags, max-car-moves caps, each destination Location's
  ``car_types`` catalog, and the train's ``allow_through_cars``
  option, and
* a warnings list for conditions the operator should know about
  (missing engine, no caboose available, cars with no viable
  destination, etc).

The builder is a pure function: nothing writes to disk. The UI calls
it on demand ("Build" button) and renders the result. Re-building
re-runs the algorithm against the current roster so the manifest is
always coherent with live data.

Design notes:

* Tie-breaking among equally valid candidates is pseudo-random with
  a stable seed derived from the Train id, so successive builds of
  the same train produce the same manifest while two trains built
  side-by-side don't deterministically compete for the same cars.
* The algorithm is intentionally single-pass per stop. It doesn't
  attempt the multi-pass "improve the plan" phase JMRI's full
  TrainBuilder does; those refinements will land as the Dispatcher
  Pro integration raises concrete gaps.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from pmri_core.persistence.operations_xml import (
    Car, Engine, Location, Route, Station, Train,
)


# ---- manifest shape -------------------------------------------------------


@dataclass(frozen=True)
class ManifestEngineAssignment:
    engine_id: str
    road_name: str = ""
    road_number: str = ""
    model: str = ""


@dataclass(frozen=True)
class ManifestCaboose:
    car_id: str
    road_name: str = ""
    road_number: str = ""


@dataclass(frozen=True)
class ManifestPick:
    car_id: str
    car_type: str
    road_name: str
    road_number: str
    from_location_id: str   # the Location the car is sitting at
    from_location_name: str
    to_station_id: str      # destination Station
    to_station_name: str
    to_location_id: str     # Location at destination Station
    to_location_name: str


@dataclass(frozen=True)
class ManifestDrop:
    car_id: str
    car_type: str
    road_name: str
    road_number: str
    to_location_id: str
    to_location_name: str
    from_station_id: str    # Station the car was picked up at
    from_station_name: str


@dataclass(frozen=True)
class ManifestStop:
    sequence_id: int
    station_id: str
    station_name: str
    picks: tuple[ManifestPick, ...] = ()
    drops: tuple[ManifestDrop, ...] = ()
    moves_used: int = 0           # pickups + drops counted against max_car_moves
    moves_cap: int = 0            # the route's max_car_moves for this stop


@dataclass(frozen=True)
class Manifest:
    train_id: str
    train_name: str
    route_id: str
    route_name: str
    engines: tuple[ManifestEngineAssignment, ...] = ()
    caboose: ManifestCaboose | None = None
    stops: tuple[ManifestStop, ...] = ()
    warnings: tuple[str, ...] = ()


# ---- helpers --------------------------------------------------------------


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def _stable_seed(ident: str) -> int:
    """Derive a reproducible 64-bit seed from a train id so repeated
    builds of the same train pick the same tie-break order."""
    digest = hashlib.sha1(ident.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _car_label(car: Car) -> str:
    """Human-friendly car label for warnings: ``RoadName RoadNumber``
    with id as fallback when both road fields are blank."""
    base = f"{car.road_name or ''} {car.road_number or ''}".strip()
    return base or car.id


def _location_label(loc_id: str, location_by_id: dict[str, Location]) -> str:
    loc = location_by_id.get(loc_id)
    if loc is None:
        return loc_id
    return loc.name or loc.id


def _station_label(sid: str, station_by_id: dict[str, Station]) -> str:
    stn = station_by_id.get(sid)
    if stn is None:
        return sid
    return stn.name or stn.id


def _first_match(
    station_id: str,
    locations_by_station: dict[str, list[Location]],
    want_type: str,
) -> Location | None:
    """Return the first Location at *station_id* whose ``car_types``
    serves *want_type*. Falls back to any Location at the station
    if the car has no explicit type, so untyped cars can still move.
    """
    candidates = locations_by_station.get(station_id, [])
    if not candidates:
        return None
    if not want_type:
        return candidates[0]
    for loc in candidates:
        if want_type in loc.car_types:
            return loc
    return None


# ---- public entry point ---------------------------------------------------


@dataclass
class _BuildState:
    """Mutable accumulator while walking the route; converted into a
    frozen Manifest at the end. Kept private so the public API stays
    the immutable Manifest dataclass."""

    picks: dict[str, list[ManifestPick]] = field(default_factory=dict)
    drops: dict[str, list[ManifestDrop]] = field(default_factory=dict)
    remaining_moves: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    consumed_car_ids: set[str] = field(default_factory=set)


def build_train(
    *,
    train: Train,
    route: Route,
    stations: list[Station],
    locations: list[Location],
    cars: list[Car],
    engines: list[Engine],
    seed: int | None = None,
) -> Manifest:
    """Compute a Manifest for *train* against the current roster.

    Deterministic given the same inputs: tie-breaking uses a stable
    pseudo-random order seeded by the train id (overridable via
    *seed* for tests).
    """
    rng = random.Random(seed if seed is not None else _stable_seed(train.id))

    station_by_id = {s.id: s for s in stations}
    location_by_id = {l.id: l for l in locations}
    locations_by_station: dict[str, list[Location]] = {}
    for loc in locations:
        if loc.station_id:
            locations_by_station.setdefault(loc.station_id, []).append(loc)

    route_stops = [rl for rl in route.locations if rl.station_id]
    state = _BuildState()

    if len(route_stops) < 2:
        state.warnings.append(
            f"Route {route.name or route.id!r} needs at least two stops "
            f"to build a train; this one has {len(route_stops)}.",
        )
        return _finalize(train, route, route_stops, station_by_id, state, (), None)

    # Initialise per-stop counters + empty pick/drop lists.
    for rl in route_stops:
        sid = rl.station_id
        state.picks.setdefault(sid, [])
        state.drops.setdefault(sid, [])
        state.remaining_moves[sid] = _int(rl.max_car_moves, 5)

    origin_sid = route_stops[0].station_id
    origin_locations = locations_by_station.get(origin_sid, [])
    origin_loc_ids = {l.id for l in origin_locations}
    if not origin_locations:
        state.warnings.append(
            f"Origin station "
            f"{_station_label(origin_sid, station_by_id)!r} has no "
            "Locations; can't source engines, caboose, or cars.",
        )

    engines_out = _pick_engines(
        train, engines, origin_loc_ids, origin_sid, state, rng,
    )
    caboose = _pick_caboose(
        train, cars, origin_loc_ids, state, rng,
    )

    # Walk the route picking up cars and routing them to later stops.
    _plan_pickups(
        train=train,
        route_stops=route_stops,
        cars=cars,
        caboose=caboose,
        locations_by_station=locations_by_station,
        location_by_id=location_by_id,
        station_by_id=station_by_id,
        state=state,
        rng=rng,
    )

    # Caboose drops at the terminal.
    if caboose is not None:
        _drop_caboose_at_terminal(
            caboose=caboose,
            terminal_rl=route_stops[-1],
            origin_sid=origin_sid,
            station_by_id=station_by_id,
            locations_by_station=locations_by_station,
            state=state,
        )

    return _finalize(
        train, route, route_stops, station_by_id, state, engines_out, caboose,
    )


# ---- engine / caboose picking ---------------------------------------------


def _pick_engines(
    train: Train,
    engines: list[Engine],
    origin_loc_ids: set[str],
    origin_sid: str,
    state: _BuildState,
    rng: random.Random,
) -> tuple[ManifestEngineAssignment, ...]:
    # A pinned engine id wins outright: we use that engine and skip
    # all road/model filtering. This is the "always pull this specific
    # locomotive" workflow -- for named power or a particular consist.
    if train.engine_id:
        pinned = next(
            (e for e in engines if e.id == train.engine_id), None,
        )
        if pinned is None:
            state.warnings.append(
                f"Pinned engine {train.engine_id!r} not in the roster.",
            )
            return ()
        return (ManifestEngineAssignment(
            engine_id=pinned.id, road_name=pinned.road_name,
            road_number=pinned.road_number, model=pinned.model,
        ),)

    needed = _int(train.number_engines, 0)
    if needed <= 0:
        return ()
    # PMRI stores Engine.location_id as a Station id (the UI's engine
    # editor picks a Station). Fall back to matching Location ids too
    # for legacy data imported before the Stations layer landed.
    candidates = [
        e for e in engines
        if e.location_id == origin_sid or e.location_id in origin_loc_ids
    ]
    if train.engine_road:
        candidates = [
            e for e in candidates if e.road_name == train.engine_road
        ]
    if train.engine_model:
        candidates = [
            e for e in candidates if e.model == train.engine_model
        ]
    rng.shuffle(candidates)
    chosen = candidates[:needed]
    if len(chosen) < needed:
        state.warnings.append(
            f"Train needs {needed} engine(s) at origin; "
            f"{len(chosen)} available",
        )
    return tuple(
        ManifestEngineAssignment(
            engine_id=e.id, road_name=e.road_name,
            road_number=e.road_number, model=e.model,
        )
        for e in chosen
    )


def _pick_caboose(
    train: Train,
    cars: list[Car],
    origin_loc_ids: set[str],
    state: _BuildState,
    rng: random.Random,
) -> Car | None:
    """JMRI's requires bitmask: bit 0 = caboose, bit 1 = FRED.
    We treat "needs caboose" as "needs a Caboose car at the origin";
    FRED is handled identically (both just claim a car for the tail).

    Passenger and Mixed trains never carry a caboose, so we short
    circuit them regardless of the ``requires`` bitmask.
    """
    ttype = (train.train_type or "").strip().lower()
    if ttype in ("passenger", "mixed"):
        return None

    # A pinned caboose id wins outright.
    if train.caboose_id:
        pinned = next(
            (c for c in cars if c.id == train.caboose_id), None,
        )
        if pinned is None:
            state.warnings.append(
                f"Pinned caboose {train.caboose_id!r} not in the car roster.",
            )
            return None
        return pinned

    requires = _int(train.requires, 0)
    needs = (requires & 0b11) != 0
    if not needs:
        return None
    pool = [
        c for c in cars
        if c.location_id in origin_loc_ids
        and c.type.lower() == "caboose"
    ]
    if train.caboose_road:
        pool = [c for c in pool if c.road_name == train.caboose_road]
    rng.shuffle(pool)
    if not pool:
        state.warnings.append(
            "Train requires a caboose / FRED; none available at origin.",
        )
        return None
    return pool[0]


# ---- pickup planning ------------------------------------------------------


def _plan_assigned_cars(
    *,
    train: Train,
    route_stops: list[Any],
    cars: list[Car],
    caboose: Car | None,
    locations_by_station: dict[str, list[Location]],
    location_by_id: dict[str, Location],
    station_by_id: dict[str, Station],
    state: _BuildState,
    allow_through: bool,
    terminal_idx: int,
) -> None:
    """Place manually-assigned cars onto the manifest.

    Each car id in ``train.assigned_car_ids`` is looked up in the car
    roster and placed on the earliest route stop that serves its
    Station, routed to the first later stop whose Location serves
    the car's type. Assigned cars still count against each stop's
    max-car-moves. A car that can't be placed (not in roster, not at
    a route station, no valid destination) triggers a warning.
    """
    if not train.assigned_car_ids:
        return
    cars_by_id = {c.id: c for c in cars}
    # Passenger trains run a fixed consist end-to-end; the cars
    # shuttle origin->terminal->origin so from the switcher's point
    # of view they're always "in the wrong place" at build time. The
    # block below reroutes each assigned car to the origin regardless
    # of its persisted Location and lands it at the terminal, skipping
    # the pickup-station-must-be-on-the-route check entirely.
    is_passenger = (train.train_type or "").strip().lower() == "passenger"
    # Map station_id -> position in the route so we can find each
    # assigned car's pickup stop and verify ordering.
    station_to_idx: dict[str, int] = {}
    for i, rl in enumerate(route_stops):
        if rl.station_id and rl.station_id not in station_to_idx:
            station_to_idx[rl.station_id] = i
    # Look up Location -> Station so a car sitting on a specific track
    # (Location) still resolves to the right route stop.
    loc_to_station: dict[str, str] = {}
    for sid, locs in locations_by_station.items():
        for loc in locs:
            loc_to_station[loc.id] = sid
    origin_sid = route_stops[0].station_id if route_stops else ""

    for cid in train.assigned_car_ids:
        car = cars_by_id.get(cid)
        if car is None:
            state.warnings.append(
                f"Assigned car {cid!r} is not in the car roster.",
            )
            continue
        if caboose is not None and car.id == caboose.id:
            # The caboose is already claimed; don't double-count it.
            continue
        if car.id in state.consumed_car_ids:
            continue
        if is_passenger:
            # Place at origin, run to terminal; no location sanity
            # checks -- a passenger consist can "always be in the
            # wrong place" between runs.
            pickup_sid = origin_sid
            from_idx = 0
            terminal_rl = route_stops[terminal_idx]
            terminal_sid = terminal_rl.station_id
            terminal_locs = locations_by_station.get(terminal_sid, [])
            dest_loc = (
                _first_match(terminal_sid, locations_by_station, car.type)
                or (terminal_locs[0] if terminal_locs else None)
            )
            if dest_loc is None:
                state.warnings.append(
                    f"Passenger car {_car_label(car)!r} has no Location "
                    f"at the terminal "
                    f"{_station_label(terminal_sid, station_by_id)!r}.",
                )
                continue
            dest_sid = terminal_sid
            dest_route_idx = terminal_idx
        else:
            # Resolve the car's current Location to a station on the route.
            pickup_sid = loc_to_station.get(car.location_id, "")
            if pickup_sid not in station_to_idx:
                state.warnings.append(
                    f"Assigned car {_car_label(car)!r} is at "
                    f"{_location_label(car.location_id, location_by_id)!r}, "
                    "which isn't a station on this train's route.",
                )
                continue
            from_idx = station_to_idx[pickup_sid]
            if from_idx == terminal_idx:
                state.warnings.append(
                    f"Assigned car {_car_label(car)!r} is already at "
                    "the terminal; nothing to carry.",
                )
                continue
            pickup_rl = route_stops[from_idx]
            if pickup_rl.pickups != "yes":
                state.warnings.append(
                    f"Assigned car {_car_label(car)!r} is at "
                    f"{_station_label(pickup_sid, station_by_id)!r}, "
                    "which disallows pickups; can't carry it.",
                )
                continue
            if state.remaining_moves.get(pickup_sid, 0) <= 0:
                state.warnings.append(
                    f"Assigned car {_car_label(car)!r} can't be picked up "
                    f"at {_station_label(pickup_sid, station_by_id)!r}: "
                    "move cap reached.",
                )
                continue
            dest_sid, dest_loc, dest_route_idx = _route_car(
                car=car, from_idx=from_idx, route_stops=route_stops,
                locations_by_station=locations_by_station,
                remaining=state.remaining_moves,
            )
            if dest_sid is None or dest_loc is None:
                state.warnings.append(
                    f"Assigned car {_car_label(car)!r} ({car.type}) has "
                    "no later destination on this route that serves "
                    "its type.",
                )
                continue
        if (
            not allow_through
            and from_idx == 0
            and dest_route_idx == terminal_idx
        ):
            # Passenger-style through consist: the user WANT this. An
            # assigned car explicitly overrides the through-car policy
            # so passenger trains route end-to-end without needing to
            # toggle allow_through_cars.
            pass

        from_loc = location_by_id.get(car.location_id)
        from_loc_name = from_loc.name if from_loc else car.location_id
        dest_station = station_by_id.get(dest_sid)
        dest_station_name = (
            dest_station.name if dest_station else dest_sid
        )
        pickup_station = station_by_id.get(pickup_sid)
        pickup_station_name = (
            pickup_station.name if pickup_station else pickup_sid
        )
        pick = ManifestPick(
            car_id=car.id, car_type=car.type,
            road_name=car.road_name, road_number=car.road_number,
            from_location_id=car.location_id,
            from_location_name=from_loc_name,
            to_station_id=dest_sid,
            to_station_name=dest_station_name,
            to_location_id=dest_loc.id,
            to_location_name=dest_loc.name or dest_loc.id,
        )
        drop = ManifestDrop(
            car_id=car.id, car_type=car.type,
            road_name=car.road_name, road_number=car.road_number,
            to_location_id=dest_loc.id,
            to_location_name=dest_loc.name or dest_loc.id,
            from_station_id=pickup_sid,
            from_station_name=pickup_station_name,
        )
        state.picks[pickup_sid].append(pick)
        state.drops[dest_sid].append(drop)
        state.remaining_moves[pickup_sid] -= 1
        state.remaining_moves[dest_sid] -= 1
        state.consumed_car_ids.add(car.id)


def _plan_pickups(
    *,
    train: Train,
    route_stops: list[Any],
    cars: list[Car],
    caboose: Car | None,
    locations_by_station: dict[str, list[Location]],
    location_by_id: dict[str, Location],
    station_by_id: dict[str, Station],
    state: _BuildState,
    rng: random.Random,
) -> None:
    allow_through = (train.allow_through_cars or "").lower() == "true"
    terminal_idx = len(route_stops) - 1

    # Manually-assigned cars get routed first. They short-circuit the
    # auto-pick logic for those cars, but still obey route pickups/
    # drops/max-car-moves/destination-car-type checks. If an assigned
    # car can't be routed (e.g. its current Location isn't on the
    # route, or no destination serves its type) we warn and move on.
    _plan_assigned_cars(
        train=train, route_stops=route_stops, cars=cars,
        caboose=caboose, locations_by_station=locations_by_station,
        location_by_id=location_by_id, station_by_id=station_by_id,
        state=state, allow_through=allow_through,
        terminal_idx=terminal_idx,
    )

    for i, rl in enumerate(route_stops):
        if rl.pickups != "yes":
            continue
        if i == terminal_idx:
            # Terminal doesn't pick up; everything there is at destination.
            continue
        pickup_sid = rl.station_id
        if state.remaining_moves[pickup_sid] <= 0:
            continue
        pickup_loc_ids = {
            l.id for l in locations_by_station.get(pickup_sid, [])
        }
        if not pickup_loc_ids:
            continue
        # Cars currently at any Location of this Station, minus already-
        # picked ones and minus the caboose (which rides the whole route).
        cars_here = [
            c for c in cars
            if c.id not in state.consumed_car_ids
            and c.location_id in pickup_loc_ids
            and (caboose is None or c.id != caboose.id)
        ]
        rng.shuffle(cars_here)

        for car in cars_here:
            if state.remaining_moves[pickup_sid] <= 0:
                break
            dest_sid, dest_loc, dest_route_idx = _route_car(
                car=car, from_idx=i, route_stops=route_stops,
                locations_by_station=locations_by_station,
                remaining=state.remaining_moves,
            )
            if dest_sid is None or dest_loc is None:
                state.warnings.append(
                    f"Car {_car_label(car)!r} ({car.type}) at "
                    f"{_station_label(pickup_sid, station_by_id)!r} has no "
                    "viable destination later on this route.",
                )
                continue
            # Through-car restriction: car travels from origin all the
            # way to terminal, with no intermediate stop. Allow only if
            # the train opts in.
            if (
                not allow_through
                and i == 0
                and dest_route_idx == terminal_idx
            ):
                continue

            from_loc = location_by_id.get(car.location_id)
            from_loc_name = from_loc.name if from_loc else car.location_id
            dest_station = station_by_id.get(dest_sid)
            dest_station_name = (
                dest_station.name if dest_station else dest_sid
            )
            pick = ManifestPick(
                car_id=car.id, car_type=car.type,
                road_name=car.road_name, road_number=car.road_number,
                from_location_id=car.location_id,
                from_location_name=from_loc_name,
                to_station_id=dest_sid,
                to_station_name=dest_station_name,
                to_location_id=dest_loc.id,
                to_location_name=dest_loc.name or dest_loc.id,
            )
            pickup_station = station_by_id.get(pickup_sid)
            pickup_station_name = (
                pickup_station.name if pickup_station else pickup_sid
            )
            drop = ManifestDrop(
                car_id=car.id, car_type=car.type,
                road_name=car.road_name, road_number=car.road_number,
                to_location_id=dest_loc.id,
                to_location_name=dest_loc.name or dest_loc.id,
                from_station_id=pickup_sid,
                from_station_name=pickup_station_name,
            )
            state.picks[pickup_sid].append(pick)
            state.drops[dest_sid].append(drop)
            state.remaining_moves[pickup_sid] -= 1
            state.remaining_moves[dest_sid] -= 1
            state.consumed_car_ids.add(car.id)


def _route_car(
    *,
    car: Car,
    from_idx: int,
    route_stops: list[Any],
    locations_by_station: dict[str, list[Location]],
    remaining: dict[str, int],
) -> tuple[str | None, Location | None, int]:
    """Find the first later stop whose Station has a Location that
    serves this car's type and that still has moves available. Returns
    (station_id, location, route_index) or (None, None, -1)."""
    for j in range(from_idx + 1, len(route_stops)):
        drl = route_stops[j]
        if drl.drops != "yes":
            continue
        if remaining.get(drl.station_id, 0) <= 0:
            continue
        dest_loc = _first_match(
            drl.station_id, locations_by_station, car.type,
        )
        if dest_loc is not None:
            return drl.station_id, dest_loc, j
    return None, None, -1


def _drop_caboose_at_terminal(
    *,
    caboose: Car,
    terminal_rl: Any,
    origin_sid: str,
    station_by_id: dict[str, Station],
    locations_by_station: dict[str, list[Location]],
    state: _BuildState,
) -> None:
    terminal_sid = terminal_rl.station_id
    terminal_locs = locations_by_station.get(terminal_sid, [])
    dest_loc = next(
        (l for l in terminal_locs if "Caboose" in l.car_types),
        terminal_locs[0] if terminal_locs else None,
    )
    if dest_loc is None:
        state.warnings.append(
            "Caboose has no Location at the terminal Station to drop into.",
        )
        return
    origin_station = station_by_id.get(origin_sid)
    state.drops[terminal_sid].append(
        ManifestDrop(
            car_id=caboose.id, car_type=caboose.type,
            road_name=caboose.road_name, road_number=caboose.road_number,
            to_location_id=dest_loc.id,
            to_location_name=dest_loc.name or dest_loc.id,
            from_station_id=origin_sid,
            from_station_name=origin_station.name if origin_station else origin_sid,
        ),
    )


# ---- finalize -------------------------------------------------------------


def _finalize(
    train: Train, route: Route, route_stops: list[Any],
    station_by_id: dict[str, Station], state: _BuildState,
    engines_out: tuple[ManifestEngineAssignment, ...],
    caboose: Car | None,
) -> Manifest:
    stops: list[ManifestStop] = []
    for i, rl in enumerate(route_stops):
        sid = rl.station_id
        station = station_by_id.get(sid)
        picks = tuple(state.picks.get(sid, []))
        drops = tuple(state.drops.get(sid, []))
        cap = _int(rl.max_car_moves, 5)
        used = cap - state.remaining_moves.get(sid, cap)
        stops.append(
            ManifestStop(
                sequence_id=i + 1,
                station_id=sid,
                station_name=station.name if station else sid,
                picks=picks,
                drops=drops,
                moves_used=used,
                moves_cap=cap,
            ),
        )
    return Manifest(
        train_id=train.id,
        train_name=train.name or train.id,
        route_id=route.id,
        route_name=route.name or route.id,
        engines=engines_out,
        caboose=(
            ManifestCaboose(
                car_id=caboose.id,
                road_name=caboose.road_name,
                road_number=caboose.road_number,
            )
            if caboose is not None else None
        ),
        stops=tuple(stops),
        warnings=tuple(state.warnings),
    )
