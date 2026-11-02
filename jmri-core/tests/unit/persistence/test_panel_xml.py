"""Panel XML envelope tests."""
from __future__ import annotations

import pathlib

import pytest
from lxml import etree

from jmri_core.persistence.panel_xml import (
    PanelFile,
    load_panel,
    load_panel_bytes,
    save_panel_bytes,
)

GOLDEN_PANELS = (
    pathlib.Path(__file__).resolve().parents[4]
    / "stage-0"
    / "contracts"
    / "xml"
    / "golden"
    / "samples"
)


@pytest.mark.parametrize(
    "path",
    sorted(GOLDEN_PANELS.rglob("*.xml")),
    ids=lambda p: str(p.relative_to(GOLDEN_PANELS)),
)
def test_each_golden_panel_loads_and_roundtrips(path: pathlib.Path) -> None:
    f = load_panel(path)
    assert isinstance(f, PanelFile)
    assert f.root_tag, "must have a root tag"

    # Semantic round-trip: same root tag, same number of top-level children.
    blob = save_panel_bytes(f)
    again = load_panel_bytes(blob)
    assert again.root_tag == f.root_tag
    assert len(list(again.root)) == len(list(f.root))


def test_panel_envelope_accessors() -> None:
    blob = b"""<?xml version="1.0"?>
<layout-config jmriversion="5.14.0">
  <panel name="MyPanel" />
  <panel name="Other" />
  <LayoutEditor name="Yard" />
  <turnoutmanager />
  <sensormanager />
</layout-config>"""
    f = load_panel_bytes(blob)
    assert f.root_tag == "layout-config"
    assert f.jmri_version == "5.14.0"
    assert len(f.panels()) == 2
    assert [p.get("name") for p in f.panels()] == ["MyPanel", "Other"]
    assert len(f.layout_editors()) == 1
    assert len(f.named_beans("turnoutmanager")) == 1


def test_panel_roundtrip_preserves_unknown_elements() -> None:
    """An unknown <weirdElement> nested inside must survive a round-trip."""
    blob = b"""<?xml version="1.0"?>
<layout-config>
  <weirdElement attribute="value">
    <child>text</child>
  </weirdElement>
</layout-config>"""
    f = load_panel_bytes(blob)
    out = save_panel_bytes(f)
    parsed = etree.fromstring(out)
    weird = parsed.find("weirdElement")
    assert weird is not None
    assert weird.get("attribute") == "value"
    assert weird.find("child") is not None
    assert weird.find("child").text == "text"
