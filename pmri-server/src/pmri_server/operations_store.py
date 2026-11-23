"""In-memory + on-disk JMRI Operations Pro state.

Owns the six operations files that JMRI persists (cars, engines,
locations, routes, trains, setup) and exposes CRUD operations at the
level the JSON services need. Follows the same pattern as
:class:`pmri_server.config_store.ConfigStore`: load on construction,
every mutation re-serialises the owning file to disk so a
crash-restart sees the committed state.

Each roster lives in its own XML file under ``<profile>/operations/``
so a locations edit doesn't rewrite the whole cars file. That matches
JMRI's own layout.
"""
from __future__ import annotations

import logging
import pathlib
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field

from pmri_core.persistence.operations_xml import (
    Car,
    Engine,
    Location,
    OperationsFile,
    Route,
    Station,
    Train,
    empty_operations,
    load_operations,
    remove_car,
    remove_engine,
    remove_location,
    remove_route,
    remove_station,
    remove_train,
    save_operations,
    upsert_car,
    upsert_engine,
    upsert_location,
    upsert_route,
    upsert_station,
    upsert_train,
)
from pmri_core.persistence.profile import Profile

_log = logging.getLogger(__name__)


# File family -> on-disk filename under ``<profile>/operations/``.
# These names match JMRI Java's OperationsXml defaults so a profile
# authored by PMRI stays readable by JMRI and vice versa.
_CAR_ROSTER = "OperationsCarRoster.xml"
_ENGINE_ROSTER = "OperationsEngineRoster.xml"
_LOCATION_ROSTER = "OperationsLocationRoster.xml"
_ROUTE_ROSTER = "OperationsRouteRoster.xml"
_TRAIN_ROSTER = "OperationsTrainRoster.xml"


def _rolling_backup(path: pathlib.Path) -> None:
    """Keep one ``.bak`` + one ``.bak2`` sibling before overwriting *path*.

    Matches the panel-edit service's safety net so an Operations
    mutation gone wrong can always be recovered by hand.
    """
    if not path.is_file():
        return
    bak = path.with_suffix(path.suffix + ".bak")
    bak2 = path.with_suffix(path.suffix + ".bak2")
    if bak.is_file():
        try:
            if bak2.is_file():
                bak2.unlink()
            bak.rename(bak2)
        except OSError:
            pass
    try:
        shutil.copy2(path, bak)
    except OSError:
        pass


def _load_or_empty(path: pathlib.Path) -> OperationsFile:
    if not path.is_file():
        return empty_operations()
    try:
        return load_operations(path)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "OperationsStore: failed to parse %s (%s); starting empty",
            path, e,
        )
        return empty_operations()


@dataclass
class OperationsStore:
    """In-memory + on-disk operations rosters, per profile."""

    profile: Profile
    _cars: OperationsFile = field(init=False)
    _engines: OperationsFile = field(init=False)
    _locations: OperationsFile = field(init=False)
    _routes: OperationsFile = field(init=False)
    _trains: OperationsFile = field(init=False)

    def __post_init__(self) -> None:
        self.profile.ensure()
        self.profile.operations_dir.mkdir(parents=True, exist_ok=True)
        self._cars = _load_or_empty(self.profile.operations_dir / _CAR_ROSTER)
        self._engines = _load_or_empty(
            self.profile.operations_dir / _ENGINE_ROSTER,
        )
        self._locations = _load_or_empty(
            self.profile.operations_dir / _LOCATION_ROSTER,
        )
        self._routes = _load_or_empty(
            self.profile.operations_dir / _ROUTE_ROSTER,
        )
        self._trains = _load_or_empty(
            self.profile.operations_dir / _TRAIN_ROSTER,
        )

    # ----- read ------------------------------------------------------------

    def cars(self) -> Iterable[Car]:
        return self._cars.cars()

    def car(self, car_id: str) -> Car | None:
        return next((c for c in self._cars.cars() if c.id == car_id), None)

    def engines(self) -> Iterable[Engine]:
        return self._engines.engines()

    def engine(self, engine_id: str) -> Engine | None:
        return next(
            (e for e in self._engines.engines() if e.id == engine_id), None,
        )

    def locations(self) -> Iterable[Location]:
        return self._locations.locations()

    def location(self, location_id: str) -> Location | None:
        return next(
            (l for l in self._locations.locations() if l.id == location_id),
            None,
        )

    def stations(self) -> Iterable[Station]:
        return self._locations.stations()

    def station(self, station_id: str) -> Station | None:
        return next(
            (s for s in self._locations.stations() if s.id == station_id),
            None,
        )

    # ----- write -----------------------------------------------------------

    def upsert_car(self, car: Car) -> None:
        upsert_car(self._cars, car)
        self._flush_cars()

    def remove_car(self, car_id: str) -> bool:
        existed = remove_car(self._cars, car_id)
        if existed:
            self._flush_cars()
        return existed

    def upsert_engine(self, engine: Engine) -> None:
        upsert_engine(self._engines, engine)
        self._flush_engines()

    def remove_engine(self, engine_id: str) -> bool:
        existed = remove_engine(self._engines, engine_id)
        if existed:
            self._flush_engines()
        return existed

    def upsert_location(self, location: Location) -> None:
        upsert_location(self._locations, location)
        self._flush_locations()

    def remove_location(self, location_id: str) -> bool:
        existed = remove_location(self._locations, location_id)
        if existed:
            self._flush_locations()
        return existed

    def upsert_station(self, station: Station) -> None:
        upsert_station(self._locations, station)
        self._flush_locations()

    def remove_station(self, station_id: str) -> bool:
        existed = remove_station(self._locations, station_id)
        if existed:
            self._flush_locations()
        return existed

    def routes(self) -> Iterable[Route]:
        return self._routes.routes()

    def route(self, route_id: str) -> Route | None:
        return next((r for r in self._routes.routes() if r.id == route_id), None)

    def upsert_route(self, route: Route) -> None:
        upsert_route(self._routes, route)
        self._flush_routes()

    def remove_route(self, route_id: str) -> bool:
        existed = remove_route(self._routes, route_id)
        if existed:
            self._flush_routes()
        return existed

    def trains(self) -> Iterable[Train]:
        return self._trains.trains()

    def train(self, train_id: str) -> Train | None:
        return next((t for t in self._trains.trains() if t.id == train_id), None)

    def upsert_train(self, train: Train) -> None:
        upsert_train(self._trains, train)
        self._flush_trains()

    def remove_train(self, train_id: str) -> bool:
        existed = remove_train(self._trains, train_id)
        if existed:
            self._flush_trains()
        return existed

    # ----- internals -------------------------------------------------------

    def _flush_cars(self) -> None:
        path = self.profile.operations_dir / _CAR_ROSTER
        _rolling_backup(path)
        save_operations(path, self._cars)

    def _flush_engines(self) -> None:
        path = self.profile.operations_dir / _ENGINE_ROSTER
        _rolling_backup(path)
        save_operations(path, self._engines)

    def _flush_locations(self) -> None:
        path = self.profile.operations_dir / _LOCATION_ROSTER
        _rolling_backup(path)
        save_operations(path, self._locations)

    def _flush_routes(self) -> None:
        path = self.profile.operations_dir / _ROUTE_ROSTER
        _rolling_backup(path)
        save_operations(path, self._routes)

    def _flush_trains(self) -> None:
        path = self.profile.operations_dir / _TRAIN_ROSTER
        _rolling_backup(path)
        save_operations(path, self._trains)
