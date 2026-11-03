"""CDI XML parser tests -- layout, offsets, replication, type decode."""
from __future__ import annotations

import pytest

from pmri_openlcb.cdi import (
    Cdi,
    CdiBit,
    CdiEventId,
    CdiFloat,
    CdiGroup,
    CdiInt,
    CdiSegment,
    CdiString,
    parse_cdi,
    parse_cdi_bytes,
)


CANONICAL = """\
<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>ACME</manufacturer>
    <model>Widget-3000</model>
    <hardwareVersion>rev A</hardwareVersion>
    <softwareVersion>1.2.3</softwareVersion>
  </identification>
  <acdi/>
  <segment origin="0" space="0xFE">
    <name>User Config</name>
    <int size="1">
      <name>Number of turnouts</name>
      <default>4</default>
      <min>1</min>
      <max>16</max>
    </int>
    <int size="2">
      <name>Startup delay</name>
    </int>
    <string size="16">
      <name>Node label</name>
    </string>
    <eventid>
      <name>Trigger event</name>
    </eventid>
  </segment>
</cdi>
"""


# ---- identification + top-level ------------------------------------------


def test_parse_identification_and_flags() -> None:
    cdi = parse_cdi(CANONICAL)
    assert cdi.identification.manufacturer == "ACME"
    assert cdi.identification.model == "Widget-3000"
    assert cdi.identification.hardware_version == "rev A"
    assert cdi.identification.software_version == "1.2.3"
    assert cdi.supports_acdi is True


def test_parse_rejects_non_cdi_root() -> None:
    with pytest.raises(ValueError, match="root must be <cdi>"):
        parse_cdi("<other><segment/></other>")


def test_parse_rejects_malformed_xml() -> None:
    with pytest.raises(ValueError, match="parse error"):
        parse_cdi("<cdi><segment>")


def test_parse_from_bytes_strips_trailing_nulls() -> None:
    """The wire CDI is null-terminated. ``parse_cdi_bytes`` strips
    that before parsing."""
    payload = CANONICAL.encode("utf-8") + b"\x00\x00\x00"
    cdi = parse_cdi_bytes(payload)
    assert cdi.identification.manufacturer == "ACME"


# ---- offset walking ------------------------------------------------------


def test_flat_segment_offsets() -> None:
    """Fields land at running byte positions: int1 at 0, int2 at 1,
    string16 at 3, eventid at 19."""
    cdi = parse_cdi(CANONICAL)
    seg = cdi.segments[0]
    assert seg.space == 0xFE
    assert seg.origin == 0
    fields = seg.children
    assert isinstance(fields[0], CdiInt) and fields[0].offset == 0 and fields[0].size == 1
    assert isinstance(fields[1], CdiInt) and fields[1].offset == 1 and fields[1].size == 2
    assert isinstance(fields[2], CdiString) and fields[2].offset == 3 and fields[2].size == 16
    assert isinstance(fields[3], CdiEventId) and fields[3].offset == 19 and fields[3].size == 8
    # Total consumed = 19 + 8 = 27.
    assert seg.size == 27


def test_segment_origin_shifts_all_offsets() -> None:
    """A non-zero origin adds to every child's offset -- the CDI's
    offsets are absolute addresses in the space so ``read_field``
    passes them straight to the memory client."""
    xml = """\
<cdi><segment origin="0x100" space="0xFE">
    <int size="1"><name>A</name></int>
    <int size="4"><name>B</name></int>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    assert seg.origin == 0x100
    assert seg.children[0].offset == 0x100
    assert seg.children[1].offset == 0x101
    assert seg.size == 0x100 + 5


# ---- groups + replication ------------------------------------------------


def test_group_lays_out_children_contiguously() -> None:
    xml = """\
<cdi><segment origin="0" space="0xFE">
    <group>
        <name>Options</name>
        <int size="1"><name>x</name></int>
        <int size="2"><name>y</name></int>
    </group>
    <int size="1"><name>after</name></int>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    grp = seg.children[0]
    assert isinstance(grp, CdiGroup)
    assert grp.offset == 0
    assert grp.size == 3
    assert grp.children[0].offset == 0
    assert grp.children[1].offset == 1
    # The int following the group starts at 3.
    after = seg.children[1]
    assert after.offset == 3


def test_group_replication_flattens_into_n_copies() -> None:
    """``replication="4"`` x [1-byte, 2-byte] -> 12 bytes total, 8
    leaves, each at its own offset."""
    xml = """\
<cdi><segment origin="0" space="0xFE">
    <group replication="4">
        <name>Slot</name>
        <int size="1"><name>a</name></int>
        <int size="2"><name>b</name></int>
    </group>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    grp = seg.children[0]
    assert isinstance(grp, CdiGroup)
    assert grp.replication == 4
    assert grp.size == 4 * 3
    # 4 copies x 2 leaves = 8 flattened children.
    assert len(grp.children) == 8
    # Offsets: [0,1, 3,4, 6,7, 9,10]
    offsets = [c.offset for c in grp.children]
    assert offsets == [0, 1, 3, 4, 6, 7, 9, 10]
    # Segment size = 12.
    assert seg.size == 12


def test_replicated_group_preserves_field_names_across_copies() -> None:
    xml = """\
<cdi><segment origin="0" space="0xFE">
    <group replication="2">
        <name>Copy</name>
        <int size="1"><name>colour</name></int>
    </group>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    grp = seg.children[0]
    # Both copies retain the "colour" name -- a UI can label them
    # "Copy 1: colour" / "Copy 2: colour" using the replication index.
    assert all(c.name == "colour" for c in grp.children)


def test_nested_group_offsets_line_up() -> None:
    xml = """\
<cdi><segment origin="0" space="0xFE">
    <group replication="2">
        <group>
            <int size="1"><name>inner</name></int>
        </group>
        <int size="1"><name>outer</name></int>
    </group>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    outer = seg.children[0]
    assert isinstance(outer, CdiGroup)
    assert outer.size == 4   # 2 copies x (1+1)
    # Flattened outer.children = [inner_group_0, outer_0, inner_group_1, outer_1]
    assert len(outer.children) == 4
    offsets = [c.offset for c in outer.children]
    assert offsets == [0, 1, 2, 3]


# ---- leaf type parsing ---------------------------------------------------


def test_int_parses_size_default_min_max() -> None:
    xml = """\
<cdi><segment origin="0" space="0xFE">
    <int size="4">
        <name>N</name>
        <default>42</default>
        <min>-100</min>
        <max>1000</max>
    </int>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    it = seg.children[0]
    assert isinstance(it, CdiInt)
    assert it.size == 4
    assert it.default == 42
    assert it.min == -100
    assert it.max == 1000


def test_int_parses_value_map() -> None:
    xml = """\
<cdi><segment origin="0" space="0xFE">
    <int size="1">
        <name>Mode</name>
        <map>
            <relation><property>0</property><value>Off</value></relation>
            <relation><property>1</property><value>On</value></relation>
            <relation><property>2</property><value>Blink</value></relation>
        </map>
    </int>
</segment></cdi>
"""
    seg = parse_cdi(xml).segments[0]
    it = seg.children[0]
    assert isinstance(it, CdiInt)
    assert it.value_map is not None
    assert it.value_map.values == {0: "Off", 1: "On", 2: "Blink"}


def test_int_rejects_bad_size() -> None:
    with pytest.raises(ValueError, match="1/2/4/8"):
        parse_cdi("""<cdi><segment origin="0" space="0xFE">
            <int size="3"><name>x</name></int>
        </segment></cdi>""")


def test_string_parses_size_and_default() -> None:
    xml = """<cdi><segment origin="0" space="0xFE">
        <string size="16">
            <name>Label</name>
            <default>hello</default>
        </string>
    </segment></cdi>"""
    seg = parse_cdi(xml).segments[0]
    s = seg.children[0]
    assert isinstance(s, CdiString)
    assert s.size == 16
    assert s.default == "hello"


def test_string_rejects_zero_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_cdi("""<cdi><segment origin="0" space="0xFE">
            <string size="0"><name>x</name></string>
        </segment></cdi>""")


def test_eventid_is_always_8_bytes() -> None:
    xml = """<cdi><segment origin="0" space="0xFE">
        <eventid><name>Ev</name></eventid>
        <eventid><name>Ev2</name></eventid>
    </segment></cdi>"""
    seg = parse_cdi(xml).segments[0]
    assert all(isinstance(c, CdiEventId) for c in seg.children)
    assert seg.children[0].size == 8 and seg.children[0].offset == 0
    assert seg.children[1].size == 8 and seg.children[1].offset == 8


def test_float_parses_size() -> None:
    xml = """<cdi><segment origin="0" space="0xFE">
        <float size="4"><name>f32</name></float>
        <float size="8"><name>f64</name></float>
    </segment></cdi>"""
    seg = parse_cdi(xml).segments[0]
    assert isinstance(seg.children[0], CdiFloat)
    assert seg.children[0].size == 4 and seg.children[0].offset == 0
    assert seg.children[1].size == 8 and seg.children[1].offset == 4


def test_float_rejects_bad_size() -> None:
    with pytest.raises(ValueError, match="4 or 8"):
        parse_cdi("""<cdi><segment origin="0" space="0xFE">
            <float size="2"><name>x</name></float>
        </segment></cdi>""")


def test_bit_parses_position() -> None:
    xml = """<cdi><segment origin="0" space="0xFE">
        <bit bit="3"><name>enable</name></bit>
    </segment></cdi>"""
    seg = parse_cdi(xml).segments[0]
    b = seg.children[0]
    assert isinstance(b, CdiBit)
    assert b.bit == 3
    assert b.size == 1


def test_bit_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="0..7"):
        parse_cdi("""<cdi><segment origin="0" space="0xFE">
            <bit bit="9"><name>x</name></bit>
        </segment></cdi>""")


# ---- forward compat ------------------------------------------------------


def test_unknown_top_level_tags_ignored() -> None:
    """A future spec that adds new top-level elements shouldn't cause
    old CDI parsers to explode. Only the tags we recognise are
    consumed; the rest are dropped."""
    xml = """<cdi>
        <future-tag>whatever</future-tag>
        <identification><manufacturer>M</manufacturer></identification>
    </cdi>"""
    cdi = parse_cdi(xml)
    assert cdi.identification.manufacturer == "M"


def test_unknown_leaf_tag_is_skipped() -> None:
    """Same rule for unknown types inside a segment. Offset walking
    should just skip them -- we can't compute their size, so we treat
    the field as absent."""
    xml = """<cdi><segment origin="0" space="0xFE">
        <int size="1"><name>a</name></int>
        <experimental><name>x</name></experimental>
        <int size="1"><name>b</name></int>
    </segment></cdi>"""
    seg = parse_cdi(xml).segments[0]
    # The unknown tag is skipped, so 'b' lands at offset 1 (right
    # after the first int) -- same as if the middle tag weren't there.
    assert len(seg.children) == 2
    assert seg.children[0].offset == 0
    assert seg.children[1].offset == 1


def test_parse_cdi_bytes_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_cdi_bytes(b"\x00\x00\x00")
