"""Decoder XML loader tests -- verify against real golden decoder files."""
from __future__ import annotations

import pathlib

import pytest

from jmri_core.persistence.decoder_xml import (
    DecoderFile,
    DecoderPane,
    iter_decoder_files,
    load_decoder,
    load_decoder_bytes,
    load_pane,
    load_pane_bytes,
)

GOLDEN_DECODERS = (
    pathlib.Path(__file__).resolve().parents[4]
    / "stage-0"
    / "contracts"
    / "xml"
    / "golden"
    / "decoders"
)


def test_golden_decoders_directory_present() -> None:
    assert GOLDEN_DECODERS.is_dir(), f"Stage 0 contract dir missing: {GOLDEN_DECODERS}"
    files = list(GOLDEN_DECODERS.glob("*.xml"))
    assert len(files) >= 10


@pytest.mark.parametrize("file_path", sorted(GOLDEN_DECODERS.glob("*.xml")))
def test_each_golden_decoder_parses(file_path: pathlib.Path) -> None:
    df = load_decoder(file_path)
    assert isinstance(df, DecoderFile)
    if df.family is not None:
        assert df.family.name


def test_xinclude_in_variables_is_resolved(tmp_path: pathlib.Path) -> None:
    """Real JMRI decoders (TCS, Digitrax, ESU, ...) pull their
    ``<variable>`` list from shared fragments via ``xi:include``. The
    loader must resolve those; otherwise ``DecoderFile.variables`` is
    empty and every pane renders blank."""
    # Fake the JMRI directory layout: <xml_root>/decoders/<file>.xml
    # with a fragment at <xml_root>/decoders/vendor/vars.xml.
    xml_root = tmp_path
    (xml_root / "decoders" / "vendor").mkdir(parents=True)
    (xml_root / "decoders" / "vendor" / "vars.xml").write_bytes(b"""
        <variables>
          <variable CV="1" item="Short Address" default="3">
            <shortAddressVal/>
          </variable>
          <variable CV="2" item="Vstart" default="0">
            <decVal min="0" max="255"/>
          </variable>
        </variables>
    """)
    decoder_path = xml_root / "decoders" / "vendor_dec.xml"
    decoder_path.write_bytes(b"""<?xml version="1.0"?>
<decoder-config xmlns:xi="http://www.w3.org/2001/XInclude">
  <decoder>
    <family name="F" mfg="M"><model model="M1"/></family>
    <variables>
      <xi:include href="http://jmri.org/xml/decoders/vendor/vars.xml"/>
    </variables>
  </decoder>
</decoder-config>""")
    df = load_decoder(decoder_path)
    assert df.family is not None
    # Both included variables surface as real DecoderVariable entries.
    items = {v.item for v in df.variables}
    assert items == {"Short Address", "Vstart"}
    # Default values come through -- this is what powers the pane's
    # default-overlay fallback in the web UI.
    by_item = {v.item: v for v in df.variables}
    assert by_item["Short Address"].default == 3
    assert by_item["Vstart"].default == 0


def test_xinclude_missing_target_leaves_variables_empty(
    tmp_path: pathlib.Path,
) -> None:
    """A dangling include href doesn't crash the loader -- the loco just
    shows no CVs (same visible outcome as before this change)."""
    (tmp_path / "decoders").mkdir()
    decoder_path = tmp_path / "decoders" / "d.xml"
    decoder_path.write_bytes(b"""<?xml version="1.0"?>
<decoder-config xmlns:xi="http://www.w3.org/2001/XInclude">
  <decoder>
    <family name="F" mfg="M"><model model="M1"/></family>
    <variables>
      <xi:include href="http://jmri.org/xml/decoders/nowhere/nope.xml"/>
    </variables>
  </decoder>
</decoder-config>""")
    df = load_decoder(decoder_path)
    assert df.variables == []


def test_xinclude_for_pane_is_resolved(tmp_path: pathlib.Path) -> None:
    """Decoder ``<pane>`` fragments (SUSI, TCS Momentum, ...) live in
    included files. The loader must resolve them so tab lists surface."""
    xml_root = tmp_path
    (xml_root / "decoders" / "vendor").mkdir(parents=True)
    (xml_root / "decoders" / "vendor" / "pane.xml").write_bytes(b"""
        <pane>
          <name>Custom</name>
          <column><display item="X"/></column>
        </pane>
    """)
    decoder_path = xml_root / "decoders" / "d.xml"
    decoder_path.write_bytes(b"""<?xml version="1.0"?>
<decoder-config xmlns:xi="http://www.w3.org/2001/XInclude">
  <decoder>
    <family name="F" mfg="M"><model model="M1"/></family>
    <variables/>
  </decoder>
  <xi:include href="http://jmri.org/xml/decoders/vendor/pane.xml"/>
</decoder-config>""")
    df = load_decoder(decoder_path)
    assert len(df.panes) == 1
    assert df.panes[0].name == "Custom"


def test_speed_table_variable_captures_entries_count() -> None:
    """A ``<speedTable entries="N"/>`` element must set
    ``speed_table_entries`` on the DecoderVariable so the UI can expand
    it into N step rows. Default is 28 (NMRA standard)."""
    from jmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        VariableKind, load_decoder_bytes,
    )
    df = load_decoder_bytes(b"""<?xml version="1.0"?>
<decoder-config>
  <decoder>
    <family name="F" mfg="M"><model model="M1"/></family>
    <variables>
      <variable CV="67" item="Speed Table" default="0">
        <speedTable entries="14"/>
      </variable>
    </variables>
  </decoder>
</decoder-config>""")
    assert len(df.variables) == 1
    var = df.variables[0]
    assert var.kind == VariableKind.SPEED_TABLE
    assert var.speed_table_entries == 14


def test_pane_layout_tree_produces_nested_containers_and_leaves() -> None:
    """The rich layout tree preserves the JMRI-authored structure:
    column/row/group nest, and each ``<display>``/``<label>``/
    ``<separator>``/``<cvtable>``/``<speedtable>``/``<dccaddress>``
    becomes a leaf node the renderer knows how to draw."""
    from jmri_core.persistence import (  # noqa: PLC0415
        load_pane_bytes, pane_layout_tree,
    )
    p = load_pane_bytes(b"""<?xml version="1.0"?>
<pane>
  <name>Basic</name>
  <column>
    <row>
      <display item="Short Address"/>
      <display item="Long Address"/>
    </row>
    <separator/>
    <label><text>Speed control</text></label>
    <column>
      <display item="Vstart"/>
      <display item="Vhigh"/>
    </column>
  </column>
</pane>""")
    tree = pane_layout_tree(p)
    # Top-level auto-wrapped: only one child that's already a
    # container -> returned as-is.
    assert tree["type"] == "column"
    kids = tree["children"]
    assert kids[0]["type"] == "row"
    assert [c["item"] for c in kids[0]["children"]] == [
        "Short Address", "Long Address",
    ]
    assert kids[1]["type"] == "separator"
    assert kids[2]["type"] == "label"
    assert kids[2]["text"] == "Speed control"
    assert kids[3]["type"] == "column"
    assert [c["item"] for c in kids[3]["children"]] == ["Vstart", "Vhigh"]


def test_pane_layout_tree_dedupes_repeated_display_references() -> None:
    """Real JMRI pane fragments sometimes reference the same variable
    twice -- a numeric input + slider widget for Vstart, or two
    ``<column>`` blocks each pulling in the same variable for layout
    reasons. Our renderer only knows how to draw one widget per
    variable, so a naive walk produces duplicate rows (either in the
    same column or in two adjacent columns). The tree must dedupe
    ``<display item>`` leaves globally, keeping only the first
    occurrence in document order."""
    from jmri_core.persistence import (  # noqa: PLC0415
        load_pane_bytes, pane_layout_tree,
    )
    p = load_pane_bytes(b"""<?xml version="1.0"?>
<pane>
  <name>Sound Levels</name>
  <column>
    <display item="Master Volume"/>
    <display item="Master Volume"/>  <!-- literal repeat -->
    <display item="Whistle Volume"/>
  </column>
  <column>
    <display item="Whistle Volume"/>  <!-- repeat in the next column -->
    <display item="Bell Volume"/>
  </column>
</pane>""")
    tree = pane_layout_tree(p)
    # Flatten all display items in document order and check for dups.
    items: list[str] = []
    def walk(node: dict) -> None:
        if node.get("type") == "display":
            items.append(node["item"])
        for c in node.get("children", []):
            walk(c)
    walk(tree)
    assert items == ["Master Volume", "Whistle Volume", "Bell Volume"]


def test_pane_layout_tree_carries_cvtable_and_speedtable_widgets() -> None:
    """The CVs and Speed Table panes lean on their widget markers
    rather than ``<display>`` refs -- the tree must still emit them so
    the renderer can drop in the appropriate widget."""
    from jmri_core.persistence import (  # noqa: PLC0415
        load_pane_bytes, pane_layout_tree,
    )
    p = load_pane_bytes(b"""<?xml version="1.0"?>
<pane>
  <name>CVs</name>
  <column>
    <cvtable/>
    <separator/>
    <speedtable/>
  </column>
</pane>""")
    tree = pane_layout_tree(p)
    types = [c["type"] for c in tree["children"]]
    assert types == ["cvtable", "separator", "speedtable"]


def test_pane_layout_tree_falls_back_to_flat_column_without_raw_element() -> None:
    """Tests that construct a bare ``DecoderPane(items=[...])`` without
    a raw element still get a renderable tree -- a flat column of the
    ``items`` list."""
    from jmri_core.persistence import DecoderPane  # noqa: PLC0415
    from jmri_core.persistence import pane_layout_tree  # noqa: PLC0415
    p = DecoderPane(name="Motor", items=["Vstart", "Vmid", "Vhigh"])
    tree = pane_layout_tree(p)
    assert tree["type"] == "column"
    assert [c["item"] for c in tree["children"]] == [
        "Vstart", "Vmid", "Vhigh",
    ]


def test_pane_speedtable_widget_is_detected() -> None:
    """JMRI's Speed Table pane uses ``<speedtable/>`` as a widget rather
    than per-step ``<display>`` items. The parser must flag panes that
    have it so the render layer expands the SPEED_TABLE variable into
    per-step CV rows."""
    from jmri_core.persistence.decoder_xml import load_pane_bytes  # noqa: PLC0415
    p = load_pane_bytes(b"""<?xml version="1.0"?>
<pane>
  <name>Speed Table</name>
  <column><speedtable/></column>
</pane>""")
    assert p.has_speed_table is True
    assert p.items == []


def test_pane_speedtable_widget_case_insensitive() -> None:
    """JMRI has shipped both ``<speedtable/>`` and ``<speedTable/>`` in
    different pane fragments. The detection must catch both -- a
    case-sensitive miss meant the pane rendered as "no rows"."""
    from jmri_core.persistence.decoder_xml import load_pane_bytes  # noqa: PLC0415
    p1 = load_pane_bytes(b"""<?xml version="1.0"?>
<pane><name>Speed Table</name><column><speedTable/></column></pane>""")
    assert p1.has_speed_table is True

    p2 = load_pane_bytes(b"""<?xml version="1.0"?>
<pane><name>Speed Table</name><column><SPEEDTABLE/></column></pane>""")
    assert p2.has_speed_table is True

    # Adjacent widget (``<speedTableVarSlider/>``) also flips the flag --
    # its presence is a reliable signal that the pane is speed-table-ish.
    p3 = load_pane_bytes(b"""<?xml version="1.0"?>
<pane><name>Speed Table</name><column><speedTableVarSlider/></column></pane>""")
    assert p3.has_speed_table is True


def test_pane_without_speedtable_widget_flags_false() -> None:
    from jmri_core.persistence.decoder_xml import load_pane_bytes  # noqa: PLC0415
    p = load_pane_bytes(b"""<?xml version="1.0"?>
<pane>
  <name>Motor</name>
  <column><display item="Vstart"/></column>
</pane>""")
    assert p.has_speed_table is False


def test_speed_table_defaults_to_28_entries_when_attribute_absent() -> None:
    from jmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        VariableKind, load_decoder_bytes,
    )
    df = load_decoder_bytes(b"""<?xml version="1.0"?>
<decoder-config>
  <decoder>
    <family name="F" mfg="M"><model model="M1"/></family>
    <variables>
      <variable CV="67" item="Speed Table">
        <speedTable/>
      </variable>
    </variables>
  </decoder>
</decoder-config>""")
    assert df.variables[0].kind == VariableKind.SPEED_TABLE
    assert df.variables[0].speed_table_entries == 28


def test_xinclude_cycles_do_not_hang(tmp_path: pathlib.Path) -> None:
    """A fragment that includes itself would recurse forever; the
    resolver must break the cycle."""
    xml_root = tmp_path
    (xml_root / "decoders" / "vendor").mkdir(parents=True)
    (xml_root / "decoders" / "vendor" / "vars.xml").write_bytes(b"""
        <variables xmlns:xi="http://www.w3.org/2001/XInclude">
          <variable CV="1" item="X" default="1"><decVal/></variable>
          <xi:include href="http://jmri.org/xml/decoders/vendor/vars.xml"/>
        </variables>
    """)
    decoder_path = xml_root / "decoders" / "d.xml"
    decoder_path.write_bytes(b"""<?xml version="1.0"?>
<decoder-config xmlns:xi="http://www.w3.org/2001/XInclude">
  <decoder>
    <family name="F" mfg="M"><model model="M1"/></family>
    <variables>
      <xi:include href="http://jmri.org/xml/decoders/vendor/vars.xml"/>
    </variables>
  </decoder>
</decoder-config>""")
    df = load_decoder(decoder_path)
    # Terminates + surfaces the one variable.
    assert len(df.variables) == 1
    assert df.variables[0].item == "X"


def test_decoder_iteration_skips_bad_files(tmp_path: pathlib.Path) -> None:
    """One bad file in a dir shouldn't break the iterator for the rest."""
    good = tmp_path / "good.xml"
    good.write_text(
        '<?xml version="1.0"?><decoder-config>'
        '<decoder><family name="F" mfg="M">'
        '<model model="M1"/>'
        '</family></decoder></decoder-config>'
    )
    bad = tmp_path / "bad.xml"
    bad.write_text("<<< this is not XML >>>")
    seen = list(iter_decoder_files(tmp_path))
    # Only the good file should appear; the iterator silently skips parse failures.
    assert len(seen) == 1
    assert seen[0].family is not None
    assert seen[0].family.name == "F"


def test_decoder_iteration_skips_non_decoder_xml_roots(tmp_path: pathlib.Path) -> None:
    """JMRI's ``xml/decoders/`` tree contains many well-formed XML files
    whose root is *not* ``<decoder-config>`` (family index files, NMRA
    CV lists, function-label fragments included via xi:include, etc.).
    The iterator must skip those instead of aborting the whole scan on
    the first one -- that was the alpha-3 catalog-load bug.
    """
    (tmp_path / "decoder.xml").write_text(
        '<?xml version="1.0"?><decoder-config>'
        '<decoder><family name="F" mfg="M">'
        '<model model="M1"/></family></decoder>'
        '</decoder-config>'
    )
    # Well-formed XML but wrong root -- the actual failure path.
    (tmp_path / "NmraCvValues.xml").write_text(
        '<?xml version="1.0"?><nmra-cv-values/>'
    )
    (tmp_path / "families.xml").write_text(
        '<?xml version="1.0"?><families/>'
    )
    seen = list(iter_decoder_files(tmp_path))
    assert len(seen) == 1
    assert seen[0].family is not None
    assert seen[0].family.name == "F"


def test_decoder_iteration_over_empty_dir_yields_nothing(tmp_path: pathlib.Path) -> None:
    """An empty directory is a valid input -- just no files come out."""
    assert list(iter_decoder_files(tmp_path)) == []


def test_minimal_decoder_no_decoder_element() -> None:
    """A decoder-config that has no <decoder> child still parses."""
    df = load_decoder_bytes(b'<?xml version="1.0"?><decoder-config/>')
    assert df.family is None
    assert df.variables == []


def test_decoder_extracts_xinclude_hrefs() -> None:
    blob = b"""<?xml version="1.0"?>
<decoder-config xmlns:xi="http://www.w3.org/2001/XInclude">
  <decoder>
    <family name="X" mfg="Y"><model model="m1"/></family>
    <variables>
      <variable CV="1" item="addr"/>
      <xi:include href="http://example.org/shared.xml"/>
    </variables>
  </decoder>
  <xi:include href="http://example.org/pane.xml"/>
</decoder-config>"""
    df = load_decoder_bytes(blob)
    assert "http://example.org/shared.xml" in df.includes
    assert "http://example.org/pane.xml" in df.includes


def test_decoder_extracts_variables() -> None:
    blob = b"""<?xml version="1.0"?>
<decoder-config>
  <decoder>
    <family name="F" mfg="M"><model model="m1"/></family>
    <variables>
      <variable CV="1" item="Short Address" default="3"><label>Short Addr</label></variable>
      <variable CV="29" item="Decoder Config" mask="XXXXXXXV"/>
      <variable CV="17" item="Long Address" opsOnly="yes">
        <splitVal highCV="17" upperMask="XXXXXVVV" min="1" max="2043"/>
      </variable>
    </variables>
  </decoder>
</decoder-config>"""
    df = load_decoder_bytes(blob)
    assert len(df.variables) == 3
    assert df.variables[0].cv == "1"
    assert df.variables[0].default == 3
    assert df.variables[0].label == "Short Addr"
    assert df.variables[1].mask == "XXXXXXXV"
    assert df.variables[2].ops_only is True
    assert df.variables[2].mask == "XXXXXVVV"
    assert df.variables[2].max == "2043"


def test_decoder_extracts_model_outputs_and_protocols() -> None:
    blob = b"""<?xml version="1.0"?>
<decoder-config>
  <decoder>
    <family name="Light-It" mfg="NCE">
      <model model="Signal Mode" connector="Wires">
        <output name="1" label="White"/>
        <output name="2" label="Yellow"/>
        <protocols><protocol>dcc</protocol></protocols>
        <size length="18" width="15" height="5" units="mm"/>
      </model>
    </family>
  </decoder>
</decoder-config>"""
    df = load_decoder_bytes(blob)
    assert df.family is not None
    assert df.family.mfg == "NCE"
    assert len(df.family.models) == 1
    m = df.family.models[0]
    assert m.connector == "Wires"
    assert [o.name for o in m.outputs] == ["1", "2"]
    assert [o.label for o in m.outputs] == ["White", "Yellow"]
    assert m.protocols == ["dcc"]
    assert m.size_length == "18"
    assert m.size_units == "mm"


def test_decoder_rejects_wrong_root() -> None:
    with pytest.raises(ValueError, match="decoder-config"):
        load_decoder_bytes(b"<?xml version='1.0'?><wrong/>")


# ----- panes ------------------------------------------------------------


def test_decoder_extracts_panes_and_display_items() -> None:
    """A ``<pane>`` sibling of ``<decoder>`` inside ``<decoder-config>``
    (as in ``0NMRA_SUSI_Output_Mapping_Range1.xml``) is captured with
    its name and its ordered list of ``<display item="...">`` refs.
    """
    blob = b"""<?xml version="1.0"?>
<decoder-config>
  <decoder>
    <family name="SUSI Output Mapping" mfg="NMRA">
      <model model="Range 1"/>
    </family>
  </decoder>
  <pane>
    <name>Selected SUSI info</name>
    <column>
      <display item="SUSI-range"/>
      <display item="Manufacturer"/>
      <display item="Decoder Version"/>
    </column>
  </pane>
  <pane>
    <name>CVs</name>
    <column>
      <cvtable/>
    </column>
  </pane>
</decoder-config>"""
    df = load_decoder_bytes(blob)
    assert len(df.panes) == 2
    p1, p2 = df.panes
    assert p1.name == "Selected SUSI info"
    assert p1.items == ["SUSI-range", "Manufacturer", "Decoder Version"]
    assert p1.has_cvtable is False
    assert p1.has_dcc_address is False
    assert p2.name == "CVs"
    assert p2.items == []
    assert p2.has_cvtable is True


def test_pane_records_i18n_names() -> None:
    """The default ``<name>`` sits in ``.name``; ``xml:lang`` variants land
    in ``name_by_lang`` so a future UI can pick the user's language."""
    pane = load_pane_bytes(
        b'<?xml version="1.0"?>'
        b'<pane>'
        b'<name>Basic</name>'
        b'<name xml:lang="fr">Basique</name>'
        b'<name xml:lang="it">Basico</name>'
        b'<column><display item="Short Address"/></column>'
        b'</pane>'
    )
    assert pane.name == "Basic"
    assert pane.name_by_lang == {"fr": "Basique", "it": "Basico"}
    assert pane.items == ["Short Address"]


def test_pane_detects_dccaddress_widget() -> None:
    """The Basic pane uses ``<dccaddress/>`` for the composite short/long
    address widget. Rendering needs to know about it separately from
    normal ``<display>`` refs."""
    pane = load_pane_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Basic</name>'
        b'<column><dccaddress/><display item="Direction"/></column>'
        b'</pane>'
    )
    assert pane.has_dcc_address is True
    assert pane.items == ["Direction"]


def test_pane_collects_display_items_across_columns_and_nested() -> None:
    """Items appear in document order across every ``<column>`` / ``<row>``
    including nested containers -- that's the order the DecoderPro UI
    reads/writes them in, and it's what pane-scoped change tracking
    iterates."""
    pane = load_pane_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Wired</name>'
        b'<column>'
        b'  <display item="A"/>'
        b'  <row><display item="B"/><display item="C"/></row>'
        b'</column>'
        b'<column><display item="D"/></column>'
        b'</pane>'
    )
    assert pane.items == ["A", "B", "C", "D"]


def test_load_pane_rejects_wrong_root() -> None:
    with pytest.raises(ValueError, match="pane"):
        load_pane_bytes(b'<?xml version="1.0"?><notapane/>')


def test_pane_display_without_item_attribute_is_skipped() -> None:
    """``<display>`` without an ``item`` attribute isn't a variable ref
    (JMRI uses it as a layout hint on some legacy panes). Skip cleanly."""
    pane = load_pane_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Legacy</name>'
        b'<column><display/><display item="Real"/></column>'
        b'</pane>'
    )
    assert pane.items == ["Real"]


def test_decoder_without_panes_yields_empty_list() -> None:
    """Most decoder files declare no ``<pane>`` -- they rely entirely on
    the programmer definition's standard tabs. The parser mustn't
    invent panes in that case."""
    df = load_decoder_bytes(
        b'<?xml version="1.0"?>'
        b'<decoder-config><decoder>'
        b'<family name="F" mfg="M"><model model="M1"/></family>'
        b'</decoder></decoder-config>'
    )
    assert df.panes == []


def test_load_pane_from_path(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "PaneCustom.xml"
    p.write_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Custom</name>'
        b'<column><display item="X"/></column>'
        b'</pane>'
    )
    pane = load_pane(p)
    assert isinstance(pane, DecoderPane)
    assert pane.name == "Custom"


def test_pane_raw_element_preserved_for_later_rendering() -> None:
    """``raw_element`` gives the step-3 rendering code the full lxml
    subtree to reproduce column/row/label/separator layout exactly."""
    pane = load_pane_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Basic</name>'
        b'<column><label><text>Hello</text></label></column>'
        b'</pane>'
    )
    assert pane.raw_element is not None
    assert pane.raw_element.tag == "pane"
    # Confirmed the subtree is preserved end-to-end.
    label = pane.raw_element.find(".//text")
    assert label is not None and label.text == "Hello"
