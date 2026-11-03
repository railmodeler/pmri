"""Operations XML tests -- load all golden ops files + verify accessors."""
from __future__ import annotations

import pathlib

import pytest

from pmri_core.persistence.operations_xml import (
    load_operations,
    load_operations_bytes,
    save_operations_bytes,
)

GOLDEN_OPS = (
    pathlib.Path(__file__).resolve().parents[4]
    / "stage-0"
    / "contracts"
    / "xml"
    / "golden"
    / "demoOperations"
)


def _is_operations_config(path: pathlib.Path) -> bool:
    """The demoOperations dir mixes operations-config and layout-config files
    (the panel file uses the same filename prefix). Filter on root tag."""
    from xml.etree import ElementTree as ET  # noqa: PLC0415
    try:
        return ET.parse(path).getroot().tag == "operations-config"
    except ET.ParseError:
        return False


_OPS_FILES = sorted(p for p in GOLDEN_OPS.glob("*.xml") if _is_operations_config(p))


@pytest.mark.parametrize("path", _OPS_FILES, ids=lambda p: p.name)
def test_each_golden_ops_file_loads(path: pathlib.Path) -> None:
    f = load_operations(path)
    assert f.root.tag == "operations-config"
    assert f.kind != "unknown", f"unrecognized top-level for {path.name}"


@pytest.mark.parametrize("path", _OPS_FILES, ids=lambda p: p.name)
def test_each_golden_ops_file_roundtrips_semantically(path: pathlib.Path) -> None:
    """Load -> save -> load yields the same accessor data.

    Byte-equality with the original is deferred to Stage 2.5 -- we don't try
    to mimic JMRI's exact attribute ordering / whitespace.
    """
    once = load_operations(path)
    emitted = save_operations_bytes(once)
    twice = load_operations_bytes(emitted)

    # Same kind, same engine/car/route/train counts.
    assert once.kind == twice.kind
    assert len(once.engines()) == len(twice.engines())
    assert len(once.cars()) == len(twice.cars())
    assert len(once.routes()) == len(twice.routes())
    assert len(once.trains()) == len(twice.trains())
    assert len(once.locations()) == len(twice.locations())


def test_engine_roster_accessors() -> None:
    f = load_operations(GOLDEN_OPS / "OperationsEngineRoster.xml")
    engines = f.engines()
    assert engines, "demo engine roster has engines"
    by_id = {e.id: e for e in engines}
    sp = by_id.get("SP1017")
    assert sp is not None
    assert sp.road_name == "SP"
    assert sp.road_number == "1017"
    assert sp.type == "Diesel"
    assert sp.model == "SW1200"
    # Attributes not in the typed schema fall into `extra`.
    assert "hp" in sp.extra
    assert sp.extra["hp"] == "1200"


def test_route_roster_includes_locations() -> None:
    f = load_operations(GOLDEN_OPS / "OperationsRouteRoster.xml")
    routes = f.routes()
    assert routes
    by_id = {r.id: r for r in routes}
    r1 = by_id.get("1")
    assert r1 is not None
    assert r1.name == "Bksfld-Frmnt"
    assert len(r1.locations) == 6
    assert r1.locations[0].name == "Bakersfield"
    assert r1.locations[0].sequence_id == "1"


def test_operations_setup_file_accessors() -> None:
    f = load_operations(GOLDEN_OPS / "Operations.xml")
    setup = f.setup()
    assert "railRoad" in setup
    assert setup["railRoad"]["name"] == "JMRI Operations Demo Railroad"
    assert "settings" in setup
    assert setup["settings"]["trainLength"] == "900"


def test_operations_rejects_wrong_root() -> None:
    with pytest.raises(ValueError, match="operations-config"):
        load_operations_bytes(b"<?xml version='1.0'?><wrong/>")
