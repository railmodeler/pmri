"""JMRI Operations module XML files -- load/save with structural preservation.

The Operations module persists six file families, all sharing the
``<operations-config>`` envelope:

* **Operations.xml**             -- global setup (railroad, fonts, manifest format, ...)
* **OperationsEngineRoster.xml** -- locomotive instances (models, consists, engines)
* **OperationsCarRoster.xml**    -- car instances (roads, types, loads, kernels, cars)
* **OperationsLocationRoster.xml** -- locations and their tracks/schedules
* **OperationsRouteRoster.xml**  -- routes (location sequences)
* **OperationsTrainRoster.xml**  -- trains (route assignments, schedules)

These files have a *lot* of attributes per element and JMRI evolves the
schema regularly. Rather than enumerate every attribute, this module:

1. Parses the file into an :class:`OperationsFile` that preserves the
   complete lxml element tree.
2. Exposes typed Pydantic accessors over the major top-level elements
   (engines, cars, routes, trains, locations) -- these surface the *common*
   attributes callers actually need (id, name, type, length, etc.) while
   carrying any other attributes in an ``extra`` dict.
3. Serialization writes the underlying lxml tree, so unknown attributes
   and elements survive round-trip unchanged.

This is "semantic round-trip" -- the same XML structure goes in and out.
True byte-equality with the JMRI Java writer is deferred to Stage 2.5
(attribute-order canonicalization, DTD declaration preservation, comment
spacing).
"""
from __future__ import annotations

import pathlib
from collections.abc import Iterator
from dataclasses import dataclass

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

# ---- typed accessors (Pydantic, used for tests + scripting) -----------------


class _AttrModel(BaseModel):
    """Base for the typed accessors. Stores the *common* attributes and
    overflows everything else into ``extra``. ``__source`` holds the
    originating element if loaded from XML."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    extra: dict[str, str] = Field(default_factory=dict)


class Engine(_AttrModel):
    id: str
    road_name: str = Field(alias="roadName", default="")
    road_number: str = Field(alias="roadNumber", default="")
    type: str = ""
    length: str = ""
    location_id: str = Field(alias="locationId", default="")
    model: str = ""
    consist: str = ""


class Car(_AttrModel):
    id: str
    road_name: str = Field(alias="roadName", default="")
    road_number: str = Field(alias="roadNumber", default="")
    type: str = ""
    length: str = ""
    location_id: str = Field(alias="locationId", default="")
    load: str = ""


class Station(_AttrModel):
    """A railroad station: the operations point a train stops at.

    PMRI-specific: JMRI has no equivalent. Stations group the
    Locations (industries, yards, tracks) physically at the same
    stop. Trains stop at Stations; the train-builder resolves which
    specific Location at a Station each car picks up or drops at.

    Attributes:
    * ``facilities`` -- free-text letter code for station amenities
      (e.g. ``TGCWT`` = telegraph / coal / water / wye / turntable).
      Fixed to the station, not the route that reaches it.
    * ``siding_capacity`` -- number of cars the station's passing
      siding can hold; blank means no siding.
    """

    id: str
    name: str = ""
    comment: str = ""
    facilities: str = ""
    siding_capacity: str = Field(alias="sidingCapacity", default="")
    physical_location: str = Field(alias="physicalLocation", default="")
    division_id: str = Field(alias="divisionId", default="")


class Location(_AttrModel):
    """A JMRI Operations location (yard / spur / interchange / staging).

    Field names mirror the attributes JMRI writes on the ``<location>``
    element (see ``Location.java`` ``store()``). Unmodeled scalars still
    round-trip through the inherited ``extra`` overflow; child elements
    (``<types>`` + ``<track>``) get special handling:

    * ``car_types`` loads from the ``<types>/<carType name="...">`` child
      block and writes back to the same block, so it round-trips.
    * ``<track>`` children aren't modeled here (Phase 4); the mutator
      layer leaves them intact when upserting attributes.

    PMRI additions beyond the bare JMRI attribute set:

    * ``kind`` -- Yard / Spur / Interchange / Staging. JMRI at the
      location level only tracks Normal (``ops=1``) vs Staging
      (``ops=2``); the fine-grained Yard/Spur/Interchange split lives
      on tracks in JMRI. PMRI surfaces it at the location level as a
      usability shortcut and derives ``ops`` on save.
    * ``length`` -- the declared total capacity in rolling-stock length
      units. JMRI computes this from the sum of track lengths at
      runtime; PMRI persists it directly until tracks are modeled.
    * ``station_id`` -- the Station this Location belongs to.
      Required in PMRI mode; legacy JMRI files may have it blank, in
      which case the upgrade wizard assigns a station on read.
    """

    id: str
    name: str = ""
    ops: str = ""
    dir: str = ""
    comment: str = ""
    switch_list: str = Field(alias="switchList", default="")
    switch_list_comment: str = Field(alias="switchListComment", default="")
    physical_location: str = Field(alias="physicalLocation", default="")
    division_id: str = Field(alias="divisionId", default="")
    kind: str = ""
    length: str = ""
    station_id: str = Field(alias="stationId", default="")
    car_types: list[str] = Field(alias="carTypes", default_factory=list)


class RouteLocation(_AttrModel):
    """One stop in a Route. References a :class:`Station` by id.

    ``id`` is optional on construction so clients can add a stop
    without having to synthesise a unique identifier. The write path
    (:func:`_write_route_locations`) assigns one from the owning
    route's id + list position when the field is blank, giving JMRI
    readers a stable key without client-side bookkeeping.

    The legacy ``train_direction`` / ``wait`` / ``travel_time`` /
    ``depart_time`` fields stay here so vanilla JMRI files still
    round-trip, but the PMRI UI drives those from the train.

    PMRI: ``station_id`` replaces JMRI's ``location_id`` so Route
    stops reference the operations-level Station (not a specific
    industry / yard track). Facilities and siding capacity live on
    the Station itself and are fixed — they don't vary by which
    route happens to reach the station. The ``miles`` field is
    distance from the previous stop (not cumulative; first stop 0),
    and ``stop_type`` is the railroad-local letter code for the
    stop's operational class ("s" = scheduled, "f" = flag, ...).
    """

    id: str = ""
    name: str = ""
    station_id: str = Field(alias="stationId", default="")
    sequence_id: str = Field(alias="sequenceId", default="")
    train_direction: str = Field(alias="trainDirection", default="")
    max_train_length: str = Field(alias="maxTrainLength", default="")
    max_car_moves: str = Field(alias="maxCarMoves", default="")
    grade: str = ""
    pickups: str = ""     # "yes" / "no"
    drops: str = ""       # "yes" / "no"
    local_moves: str = Field(alias="localMoves", default="")
    wait: str = ""
    travel_time: str = Field(alias="travelTime", default="")
    depart_time: str = Field(alias="departTime", default="")
    random_control: str = Field(alias="randomControl", default="")
    comment: str = ""
    miles: str = ""
    stop_type: str = Field(alias="stopType", default="")


class Route(_AttrModel):
    """A JMRI Operations route plus PMRI direction / branch metadata.

    JMRI's native route only carries ``id`` / ``name`` / ``comment``;
    PMRI adds:

    * ``direction`` -- the pair of cardinal directions the route
      spans: ``EW`` (East/West) or ``NS`` (North/South). Blank until
      the user picks one.
    * ``superior_direction`` -- the preferred direction trains travel
      the route, one of ``E``/``W``/``N``/``S`` constrained to match
      ``direction``. Stop order on the route should run in this
      direction (first stop = origin for a train travelling the
      superior way).
    * ``is_branch`` -- ``true`` / ``false``. A branch line attaches to
      a mainline route at one of that line's stops.
    * ``mainline_route_id`` -- for branches, the id of the mainline
      route this route departs from. Direction + superior_direction
      must match the mainline.
    * ``branch_start_stop_id`` -- for branches, the location-id of
      the stop on the mainline where the branch begins. Must match
      an existing stop on the mainline route.
    """

    id: str
    name: str = ""
    comment: str = ""
    direction: str = ""
    superior_direction: str = Field(alias="superiorDirection", default="")
    is_branch: str = Field(alias="isBranch", default="")
    mainline_route_id: str = Field(alias="mainlineRouteId", default="")
    branch_start_stop_id: str = Field(alias="branchStartStopId", default="")
    # Ordered list of stops. On write the per-location sequenceId is
    # synthesised from list position when blank so callers don't have
    # to hand-number them.
    locations: list[RouteLocation] = Field(default_factory=list)


class TrainStop(_AttrModel):
    """One stop in a train's per-route schedule.

    Trains carry their own per-stop timing against the route they
    run. A route defines the shape (which stations, how far apart,
    with what stop-type codes); the train defines its departure at
    each, its wait time there, and whether a branch-train picks up
    the mainline leading into the branch's start.

    * ``station_id`` -- the Station this stop is at.
    * ``depart_hour`` / ``depart_minute`` -- computed by default from
      the previous stop's departure + travel time (miles / speed);
      user can override.
    * ``wait`` -- minutes of dwell at this stop.
    * ``included`` -- ``true`` (default) / ``false``. For trains on a
      branch route, stops on the mainline leading into the branch can
      be left out ("false") so the train effectively originates at
      the branch's first station.
    """

    station_id: str = Field(alias="stationId", default="")
    depart_hour: str = Field(alias="departHour", default="")
    depart_minute: str = Field(alias="departMinute", default="")
    wait: str = ""
    included: str = ""


class Train(_AttrModel):
    """A JMRI Operations train. Mirrors ``Train.java`` ``store()``.

    The route reference goes out as a nested ``<route id name>`` child
    element on the wire; we also surface it as the scalar ``route_id``
    (wire alias ``route``) for the common "assign a train to a route"
    flow.

    PMRI-specific additions:

    * ``train_type`` -- one of ``Passenger`` / ``Freight`` / ``Mixed``
      / ``Through`` (empty while unset).
    * ``train_class`` -- operational class: ``1`` / ``2`` / ``3`` /
      ``X`` (Extra). Lower numeric class is superior; Extra is lowest.
    * ``direction`` -- cardinal direction travelled, one of
      ``E``/``W``/``N``/``S``. Must match the route it runs on.
    * ``average_speed`` -- speed used to compute per-stop depart
      times from route miles when the user hasn't overridden.
    * ``stops`` -- per-stop schedule entries (see :class:`TrainStop`).
      Empty on freshly created trains; the timetable falls back to
      the legacy single ``depart_hour`` / ``depart_minute``.
    """

    id: str
    name: str = ""
    description: str = ""
    route_id: str = Field(alias="route", default="")
    train_type: str = Field(alias="trainType", default="")
    train_class: str = Field(alias="trainClass", default="")
    direction: str = ""
    average_speed: str = Field(alias="averageSpeed", default="")
    depart_hour: str = Field(alias="departHour", default="")
    depart_minute: str = Field(alias="departMinute", default="")
    built_start_year: str = Field(alias="builtStartYear", default="")
    built_end_year: str = Field(alias="builtEndYear", default="")
    number_engines: str = Field(alias="numberEngines", default="")
    engine_road: str = Field(alias="engineRoad", default="")
    engine_model: str = Field(alias="engineModel", default="")
    # PMRI-specific: pin a specific engine by id rather than relying
    # on road/model filtering. When set, the builder uses this engine
    # verbatim (and ignores road/model filters). Empty = auto-pick.
    engine_id: str = Field(alias="engineId", default="")
    caboose_road: str = Field(alias="cabooseRoad", default="")
    # PMRI-specific: pin a specific caboose by car id. Empty = auto.
    caboose_id: str = Field(alias="cabooseId", default="")
    requires: str = ""
    build_normal: str = Field(alias="buildNormal", default="")
    to_terminal: str = Field(alias="toTerminal", default="")
    allow_local_moves: str = Field(alias="allowLocalMoves", default="")
    allow_return: str = Field(alias="allowReturn", default="")
    allow_through_cars: str = Field(alias="allowThroughCars", default="")
    stops: list[TrainStop] = Field(default_factory=list)
    # PMRI-specific: cars manually assigned to this train (passenger
    # consist, named freight cars). The builder picks these first,
    # then auto-selects more up to the per-stop move caps. Stored as
    # ``<assignedCar carId="..."/>`` children on the train XML.
    assigned_car_ids: list[str] = Field(
        alias="assignedCarIds", default_factory=list,
    )


# ---- the OperationsFile wrapper --------------------------------------------


@dataclass
class OperationsFile:
    """An operations-config XML file as a structured object.

    Keeps the full lxml tree (``root``) so unknown attributes/elements
    survive round-trip. Typed accessors (:meth:`engines`, :meth:`cars`, ...)
    project the major top-level elements into Pydantic models for callers
    who want type-checked access.
    """

    root: etree._Element
    doctype: str | None = None

    # ----- read accessors ---------------------------------------------------

    @property
    def kind(self) -> str:
        """Best-guess label for this file based on the top-level children."""
        for guess in (
            "operations", "engines", "cars", "routes",
            "stations", "locations", "trains", "schedules",
        ):
            if self.root.find(guess) is not None:
                return guess
        return "unknown"

    def engines(self) -> list[Engine]:
        elem = self.root.find("engines")
        if elem is None:
            return []
        return [_elem_to_model(e, Engine) for e in elem.findall("engine")]

    def cars(self) -> list[Car]:
        elem = self.root.find("cars")
        if elem is None:
            return []
        return [_elem_to_model(c, Car) for c in elem.findall("car")]

    def locations(self) -> list[Location]:
        elem = self.root.find("locations")
        if elem is None:
            return []
        out: list[Location] = []
        for loc_el in elem.findall("location"):
            location = _elem_to_model(loc_el, Location)
            location.car_types = _read_car_types(loc_el)
            out.append(location)
        return out

    def stations(self) -> list[Station]:
        elem = self.root.find("stations")
        if elem is None:
            return []
        return [_elem_to_model(s, Station) for s in elem.findall("station")]

    def routes(self) -> list[Route]:
        elem = self.root.find("routes")
        if elem is None:
            return []
        out: list[Route] = []
        for r in elem.findall("route"):
            route = _elem_to_model(r, Route)
            route.locations = [
                _elem_to_model(loc_el, RouteLocation) for loc_el in r.findall("location")
            ]
            out.append(route)
        return out

    def trains(self) -> list[Train]:
        elem = self.root.find("trains")
        if elem is None:
            return []
        out: list[Train] = []
        for t in elem.findall("train"):
            train = _elem_to_model(t, Train)
            train.stops = [
                _elem_to_model(s, TrainStop)
                for s in t.findall("trainStop")
            ]
            train.assigned_car_ids = [
                c.get("carId", "")
                for c in t.findall("assignedCar")
                if c.get("carId")
            ]
            out.append(train)
        return out

    def setup(self) -> dict[str, dict[str, str]]:
        """Return the ``<operations>`` setup block as nested attribute dicts.

        Each child element of ``<operations>`` becomes one entry keyed by
        tag, value is the attribute dict. Order of children is preserved
        only in the underlying lxml tree, not in the returned dict.
        """
        op = self.root.find("operations")
        if op is None:
            return {}
        return {
            str(child.tag): {str(k): str(v) for k, v in child.attrib.items()}
            for child in op
        }

    # ----- iteration --------------------------------------------------------

    def iter_top_level(self) -> Iterator[etree._Element]:
        return iter(self.root)


# ---- public load/save -----------------------------------------------------


def load_operations_bytes(data: bytes) -> OperationsFile:
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    tree = etree.fromstring(data, parser=parser)
    if tree.tag != "operations-config":
        raise ValueError(f"expected <operations-config>, got <{tree.tag}>")
    return OperationsFile(root=tree, doctype=_extract_doctype(tree))


def load_operations(path: str | pathlib.Path) -> OperationsFile:
    return load_operations_bytes(pathlib.Path(path).read_bytes())


def save_operations_bytes(file: OperationsFile) -> bytes:
    if file.doctype:
        return etree.tostring(
            file.root,
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=False,
            standalone=False,
            doctype=file.doctype,
        )
    return etree.tostring(
        file.root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=False,
        standalone=False,
    )


def save_operations(path: str | pathlib.Path, file: OperationsFile) -> None:
    pathlib.Path(path).write_bytes(save_operations_bytes(file))


def empty_operations() -> OperationsFile:
    """Build a fresh, empty ``<operations-config>`` file -- used when a
    profile has no Operations*.xml on disk yet. Callers upsert into it and
    then save it to the appropriate path."""
    return OperationsFile(root=etree.Element("operations-config"))


# ---- edit operations -------------------------------------------------------
#
# Mutators keep the lxml tree as the source of truth and patch it in place
# so round-trip preserves unknown attributes. Pydantic models are the wire
# shape for CRUD callers; each mutator maps model fields onto the matching
# JMRI element attributes.


class OperationsEditError(Exception):
    """Raised when a mutation can't locate its target element."""


def _ensure_section(root: etree._Element, tag: str) -> etree._Element:
    """Return (or create) the top-level container element the caller is
    about to modify. JMRI omits the whole container when it's empty, so a
    freshly-created roster needs the ``<cars>`` / ``<engines>`` etc. wrapper
    synthesised before we can upsert into it."""
    elem = root.find(tag)
    if elem is None:
        elem = etree.SubElement(root, tag)
    return elem


def _model_to_attribs[M: _AttrModel](
    model: M,
) -> dict[str, str]:
    """Flatten a Pydantic model back to the ``attrib`` dict JMRI expects.

    Known fields use their schema alias (``roadName`` / ``roadNumber`` /
    ``locationId`` etc.); the ``extra`` overflow is appended verbatim.
    List-typed fields (e.g. ``Location.car_types``) correspond to child
    elements, not attributes, and are skipped here -- the per-type
    mutator writes them separately.
    """
    cls = type(model)
    attribs: dict[str, str] = {}
    for name, info in cls.model_fields.items():
        if name == "extra":
            continue
        value = getattr(model, name)
        if isinstance(value, list):
            # Lists map to child elements, not attributes.
            continue
        if value == "" or value is None:
            # Skip blank fields -- JMRI omits them, we omit them.
            continue
        key = info.alias or name
        attribs[key] = str(value)
    for k, v in model.extra.items():
        attribs.setdefault(k, str(v))
    return attribs


def _read_car_types(location_elem: etree._Element) -> list[str]:
    """Extract the ``<types>/<carType name="...">`` list from a
    ``<location>`` element. Returns ``[]`` when the block is missing
    or empty. ``locoType`` siblings are ignored here; callers that
    need engine-type routing use a separate accessor (not yet modeled).
    """
    types_elem = location_elem.find("types")
    if types_elem is None:
        return []
    return [
        child.get("name", "")
        for child in types_elem.findall("carType")
        if child.get("name")
    ]


def _write_car_types(
    location_elem: etree._Element, car_types: list[str],
) -> None:
    """Replace the ``<carType>`` children inside ``<types>`` with the
    supplied list, preserving any ``<locoType>`` siblings. Creates the
    ``<types>`` wrapper on demand; removes it when both lists end up
    empty, matching JMRI's "omit empty container" habit.
    """
    types_elem = location_elem.find("types")
    if types_elem is None:
        if not car_types:
            return
        types_elem = etree.SubElement(location_elem, "types")
    else:
        for old in list(types_elem.findall("carType")):
            types_elem.remove(old)
    for name in car_types:
        if not name:
            continue
        etree.SubElement(types_elem, "carType", attrib={"name": name})
    # Drop an empty wrapper rather than leaving ``<types/>``.
    if (
        not list(types_elem.findall("carType"))
        and not list(types_elem.findall("locoType"))
    ):
        location_elem.remove(types_elem)


def _upsert_by_id(
    section: etree._Element, tag: str, ident: str,
    attribs: dict[str, str],
) -> etree._Element:
    """Find the ``<tag id=ident>`` child in *section* or create one, then
    overwrite its attribute set with *attribs*. Unrelated siblings are
    left in place; existing element identity is preserved on update."""
    for child in section.findall(tag):
        if child.get("id") == ident:
            # Clear + rewrite so extras removed from the model drop out.
            for k in list(child.attrib):
                del child.attrib[k]
            for k, v in attribs.items():
                child.set(k, v)
            return child
    created = etree.SubElement(section, tag)
    for k, v in attribs.items():
        created.set(k, v)
    return created


def _remove_by_id(
    section: etree._Element, tag: str, ident: str,
) -> bool:
    for child in section.findall(tag):
        if child.get("id") == ident:
            section.remove(child)
            return True
    return False


# ----- cars ----------------------------------------------------------------


def upsert_car(file: OperationsFile, car: Car) -> etree._Element:
    if not car.id:
        raise OperationsEditError("Car.id is required")
    section = _ensure_section(file.root, "cars")
    return _upsert_by_id(section, "car", car.id, _model_to_attribs(car))


def remove_car(file: OperationsFile, car_id: str) -> bool:
    section = file.root.find("cars")
    if section is None:
        return False
    return _remove_by_id(section, "car", car_id)


# ----- engines -------------------------------------------------------------


def upsert_engine(file: OperationsFile, engine: Engine) -> etree._Element:
    if not engine.id:
        raise OperationsEditError("Engine.id is required")
    section = _ensure_section(file.root, "engines")
    return _upsert_by_id(section, "engine", engine.id, _model_to_attribs(engine))


def remove_engine(file: OperationsFile, engine_id: str) -> bool:
    section = file.root.find("engines")
    if section is None:
        return False
    return _remove_by_id(section, "engine", engine_id)


# ----- locations -----------------------------------------------------------


def upsert_location(
    file: OperationsFile, location: Location,
) -> etree._Element:
    if not location.id:
        raise OperationsEditError("Location.id is required")
    section = _ensure_section(file.root, "locations")
    elem = _upsert_by_id(
        section, "location", location.id, _model_to_attribs(location),
    )
    # The ``<types>/<carType>`` block is the one child element we
    # fully round-trip through the Pydantic model. All other children
    # (``<track>``, ``locoType`` inside ``<types>``) survive because
    # ``_upsert_by_id`` only rewrites attributes, not children.
    _write_car_types(elem, list(location.car_types))
    return elem


def remove_location(file: OperationsFile, location_id: str) -> bool:
    section = file.root.find("locations")
    if section is None:
        return False
    return _remove_by_id(section, "location", location_id)


# ----- stations ------------------------------------------------------------


def upsert_station(file: OperationsFile, station: Station) -> etree._Element:
    if not station.id:
        raise OperationsEditError("Station.id is required")
    section = _ensure_section(file.root, "stations")
    return _upsert_by_id(section, "station", station.id, _model_to_attribs(station))


def remove_station(file: OperationsFile, station_id: str) -> bool:
    section = file.root.find("stations")
    if section is None:
        return False
    return _remove_by_id(section, "station", station_id)


# ----- routes --------------------------------------------------------------


def _write_route_locations(
    route_elem: etree._Element, locations: list[RouteLocation],
) -> None:
    """Replace the ``<location>`` children of *route_elem* with the
    provided list. Preserves list order; synthesises ``sequenceId``
    from list position when blank so route-building code can rely on
    a 1..N numbering without the caller micromanaging it.
    """
    for old in list(route_elem.findall("location")):
        route_elem.remove(old)
    for index, rl in enumerate(locations, start=1):
        attribs = _model_to_attribs(rl)
        attribs.setdefault("sequenceId", str(index))
        if not attribs.get("id"):
            # Route locations with no explicit id get a synthesised one
            # so later edits can target them. Shape matches JMRI's
            # ``rl<n>`` convention loosely.
            attribs["id"] = f"{route_elem.get('id') or 'r'}-rl{index}"
        etree.SubElement(route_elem, "location", attrib=attribs)


def upsert_route(file: OperationsFile, route: Route) -> etree._Element:
    if not route.id:
        raise OperationsEditError("Route.id is required")
    section = _ensure_section(file.root, "routes")
    elem = _upsert_by_id(
        section, "route", route.id, _model_to_attribs(route),
    )
    _write_route_locations(elem, list(route.locations))
    return elem


def remove_route(file: OperationsFile, route_id: str) -> bool:
    section = file.root.find("routes")
    if section is None:
        return False
    return _remove_by_id(section, "route", route_id)


# ----- trains --------------------------------------------------------------


def _write_train_route_ref(
    train_elem: etree._Element, train: Train,
) -> None:
    """Attach (or replace) the ``<route id name>`` child reference for
    *train_elem* based on *train.route_id*. Matches the shape JMRI
    Java writes so round-trip with the vanilla Operations GUI works.
    Name is left blank when we can't resolve it from the file;
    callers that want the pretty name set it on the Train directly
    via ``extra`` or we upgrade the model later."""
    for old in list(train_elem.findall("route")):
        train_elem.remove(old)
    if train.route_id:
        etree.SubElement(train_elem, "route", attrib={"id": train.route_id})


def _write_train_stops(
    train_elem: etree._Element, stops: list[TrainStop],
) -> None:
    """Replace the ``<trainStop>`` children of *train_elem* with the
    provided list. PMRI-specific; vanilla JMRI will ignore the
    elements on read-back."""
    for old in list(train_elem.findall("trainStop")):
        train_elem.remove(old)
    for stop in stops:
        attribs = _model_to_attribs(stop)
        if not attribs:
            continue
        etree.SubElement(train_elem, "trainStop", attrib=attribs)


def _write_train_assigned_cars(
    train_elem: etree._Element, car_ids: list[str],
) -> None:
    """Replace the ``<assignedCar>`` children of *train_elem* with the
    provided list. PMRI-specific; vanilla JMRI will ignore the
    elements on read-back."""
    for old in list(train_elem.findall("assignedCar")):
        train_elem.remove(old)
    for cid in car_ids:
        if not cid:
            continue
        etree.SubElement(train_elem, "assignedCar", attrib={"carId": cid})


def upsert_train(file: OperationsFile, train: Train) -> etree._Element:
    if not train.id:
        raise OperationsEditError("Train.id is required")
    section = _ensure_section(file.root, "trains")
    attribs = _model_to_attribs(train)
    # The ``route`` alias also lives as a child reference; keep the
    # scalar on the parent for simpler clients but let the child
    # element be authoritative for JMRI readers.
    elem = _upsert_by_id(section, "train", train.id, attribs)
    _write_train_route_ref(elem, train)
    _write_train_stops(elem, list(train.stops))
    _write_train_assigned_cars(elem, list(train.assigned_car_ids))
    return elem


def remove_train(file: OperationsFile, train_id: str) -> bool:
    section = file.root.find("trains")
    if section is None:
        return False
    return _remove_by_id(section, "train", train_id)


# ---- internals ------------------------------------------------------------


def _elem_to_model[M: _AttrModel](elem: etree._Element, cls: type[M]) -> M:
    """Project *elem* onto Pydantic *cls*. Known fields use the schema mapping;
    everything else falls into ``extra``."""
    known_names: set[str] = set()
    for name, info in cls.model_fields.items():
        if info.alias:
            known_names.add(info.alias)
        known_names.add(name)

    init: dict[str, object] = {}
    extra: dict[str, str] = {}
    for raw_k, raw_v in elem.attrib.items():
        k = str(raw_k)
        v = str(raw_v)
        if k in known_names:
            init[k] = v
        else:
            extra[k] = v
    init["extra"] = extra
    return cls.model_validate(init)


def _extract_doctype(root: etree._Element) -> str | None:
    """Read DOCTYPE off the document if present. Returns ``None`` for fragments
    or documents without a DOCTYPE. ``getattr`` shields us from lxml stub gaps."""
    tree = root.getroottree()
    docinfo = tree.docinfo
    return getattr(docinfo, "doctype", None) or None
