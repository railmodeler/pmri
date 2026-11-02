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


class Location(_AttrModel):
    id: str
    name: str = ""
    location_type: str = Field(alias="locationOps", default="")


class RouteLocation(_AttrModel):
    id: str
    name: str = ""
    location_id: str = Field(alias="locationId", default="")
    sequence_id: str = Field(alias="sequenceId", default="")


class Route(_AttrModel):
    id: str
    name: str = ""
    comment: str = ""
    locations: list[RouteLocation] = Field(default_factory=list)


class Train(_AttrModel):
    id: str
    name: str = ""
    description: str = ""
    route_id: str = Field(alias="route", default="")


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
            "locations", "trains", "schedules",
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
        return [_elem_to_model(loc, Location) for loc in elem.findall("location")]

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
        return [_elem_to_model(t, Train) for t in elem.findall("train")]

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
