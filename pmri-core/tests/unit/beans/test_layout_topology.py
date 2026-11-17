"""Unit tests for the layout-topology graph builder."""
from __future__ import annotations

import textwrap

from pmri_core.beans.layout_topology import Topology, build_topology
from pmri_core.persistence.panel_model import parse_panel_xml


def _panel(body: str) -> bytes:
    """Wrap *body* inside a minimal <layout-config><LayoutEditor> envelope."""
    return textwrap.dedent(f"""\
<?xml version="1.0" encoding="utf-8"?>
<layout-config>
<LayoutEditor name="t" panelwidth="100" panelheight="100">
{body}
</LayoutEditor>
</layout-config>
""").encode("utf-8")


def test_straight_two_point_segment_builds_one_edge() -> None:
    xml = _panel("""\
<positionablepoint ident="A" x="10" y="10"/>
<positionablepoint ident="B" x="90" y="10"/>
<tracksegment ident="S1" connect1name="A" connect2name="B" type1="POS_POINT" type2="POS_POINT"/>
""")
    topo = build_topology(parse_panel_xml(xml))
    assert set(topo.nodes) == {"p:A", "p:B"}
    assert len(topo.edges) == 1
    e = topo.edges[0]
    assert (e.a, e.b) == ("p:A", "p:B")
    assert e.layout_block is None


def test_segment_into_turnout_leg_resolves_to_turnout_leg_node() -> None:
    xml = _panel("""\
<positionablepoint ident="A" x="10" y="10"/>
<layoutturnout ident="T1" turnoutname="IT1" xcen="50" ycen="10"
               xa="40" ya="10" xb="60" yb="10" xc="55" yc="20"/>
<tracksegment ident="S1" connect1name="A" connect2name="T1"
              type1="POS_POINT" type2="TURNOUT_A"/>
""")
    topo = build_topology(parse_panel_xml(xml))
    assert "t:T1:A" in topo.nodes
    assert "t:T1:B" in topo.nodes
    assert "t:T1:C" in topo.nodes
    # Only one edge drawn (S1 → A ↔ turnout leg A).
    assert [e.id for e in topo.edges] == ["S1"]
    e = topo.edges[0]
    assert {e.a, e.b} == {"p:A", "t:T1:A"}


def test_dangling_segment_is_dropped() -> None:
    xml = _panel("""\
<positionablepoint ident="A" x="10" y="10"/>
<tracksegment ident="X1" connect1name="A" connect2name="NOPE"
              type1="POS_POINT" type2="POS_POINT"/>
""")
    topo = build_topology(parse_panel_xml(xml))
    assert topo.edges == []


def test_block_boundaries_report_adjacent_segments_in_different_blocks() -> None:
    xml = _panel("""\
<positionablepoint ident="A" x="10" y="10"/>
<positionablepoint ident="B" x="50" y="10"/>
<positionablepoint ident="C" x="90" y="10"/>
<tracksegment ident="S1" connect1name="A" connect2name="B"
              type1="POS_POINT" type2="POS_POINT">
  <blockname>IB1</blockname>
</tracksegment>
<tracksegment ident="S2" connect1name="B" connect2name="C"
              type1="POS_POINT" type2="POS_POINT">
  <blockname>IB2</blockname>
</tracksegment>
""")
    topo = build_topology(parse_panel_xml(xml))
    boundaries = topo.block_boundaries()
    assert len(boundaries) == 1
    e1, e2 = boundaries[0]
    assert {e1.id, e2.id} == {"S1", "S2"}
    # Both touch the same middle node B.
    assert ("p:B" in (e1.a, e1.b)) and ("p:B" in (e2.a, e2.b))


def test_edges_in_block_returns_only_matching_block() -> None:
    xml = _panel("""\
<positionablepoint ident="A" x="10" y="10"/>
<positionablepoint ident="B" x="50" y="10"/>
<positionablepoint ident="C" x="90" y="10"/>
<tracksegment ident="S1" connect1name="A" connect2name="B"
              type1="POS_POINT" type2="POS_POINT">
  <blockname>IB1</blockname>
</tracksegment>
<tracksegment ident="S2" connect1name="B" connect2name="C"
              type1="POS_POINT" type2="POS_POINT"/>
""")
    topo = build_topology(parse_panel_xml(xml))
    assert [e.id for e in topo.edges_in_block("IB1")] == ["S1"]
    assert topo.edges_in_block("IB2") == []


def test_connected_component_walks_through_turnout_legs_only_via_edges() -> None:
    xml = _panel("""\
<positionablepoint ident="A" x="10" y="10"/>
<positionablepoint ident="D" x="90" y="10"/>
<layoutturnout ident="T1" turnoutname="IT1" xcen="50" ycen="10"
               xa="40" ya="10" xb="60" yb="10" xc="55" yc="20"/>
<tracksegment ident="S1" connect1name="A" connect2name="T1"
              type1="POS_POINT" type2="TURNOUT_A"/>
<tracksegment ident="S2" connect1name="D" connect2name="T1"
              type1="POS_POINT" type2="TURNOUT_B"/>
""")
    topo = build_topology(parse_panel_xml(xml))
    # Walking from A we only reach T1:A (and back) because the graph
    # has no synthetic turnout-internal edge from leg A to leg B.
    assert topo.connected_component("p:A") == {"p:A", "t:T1:A"}
    # Walking from D we only reach T1:B.
    assert topo.connected_component("p:D") == {"p:D", "t:T1:B"}


def test_turnout_legs_lists_only_existing_legs() -> None:
    xml = _panel("""\
<layoutturnout ident="T1" turnoutname="IT1" xcen="50" ycen="10"
               xa="40" ya="10" xb="60" yb="10" xc="55" yc="20"/>
""")
    topo: Topology = build_topology(parse_panel_xml(xml))
    legs = topo.turnout_legs("T1")
    assert [n.kind for n in legs] == ["turnout_a", "turnout_b", "turnout_c"]
    assert all(n.turnout_sysname == "IT1" for n in legs)
