"""Layout topology graph -- derived from a parsed :class:`PanelModel`.

Walks the panel's :class:`PositionablePoint` + :class:`TrackSegment` +
:class:`LayoutTurnoutGeom` primitives to produce a graph the Dispatcher
Section builder, the Canvas editor hit-tester, and the block-boundary
auditor all need. The result is derived state: cheap to rebuild from
scratch after any panel edit, so we never persist it.

Each **node** is an endpoint where segments meet:

* a :class:`PositionablePoint` id (shared by multiple segments), or
* a turnout leg (``<ident>:A|B|C|D``) -- a turnout has one node per
  leg, no node for the center since the center isn't a segment
  endpoint in JMRI's model.

Each **edge** is either:

* a :class:`TrackSegment` between two endpoints (the normal case), or
* a synthetic "turnout-internal" edge connecting two legs of the same
  turnout -- so a walker can cross a turnout by taking the right leg
  pair for the turnout's current state (A↔B for closed, A↔C for
  thrown, B↔D and A↔D on double crossovers).

For now we emit ONLY the external edges (TrackSegments) and expose the
turnout legs as nodes; callers that care about through-paths across a
turnout walk its legs via :meth:`Topology.turnout_legs`. Internal-edge
synthesis is Dispatcher work.

JMRI reference: ``jmri.jmrit.display.layoutEditor.ConnectivityUtil``
drives the same relationships; this is the derived-state subset we
need here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pmri_core.persistence.panel_model import (
    HIT_POS_POINT,
    HIT_TURNOUT_A,
    HIT_TURNOUT_B,
    HIT_TURNOUT_C,
    HIT_TURNOUT_D,
    LayoutTurnoutGeom,
    PanelModel,
    PositionablePoint,
    TrackSegment,
)


NodeKind = Literal["point", "turnout_a", "turnout_b", "turnout_c", "turnout_d"]


@dataclass(frozen=True)
class TopoNode:
    """One endpoint in the topology graph."""

    id: str                  # "p:<ident>" or "t:<ident>:A|B|C|D"
    kind: NodeKind
    x: float
    y: float
    # The ident of the underlying panel element: a PositionablePoint's
    # ident for ``kind="point"``, a LayoutTurnoutGeom's ident otherwise.
    panel_ident: str
    turnout_sysname: str = ""   # populated only for turnout-leg nodes


@dataclass(frozen=True)
class TopoEdge:
    """One edge in the topology graph.

    An edge is a :class:`TrackSegment` whose two endpoints have been
    resolved to :class:`TopoNode` ids. Edges carry the layout-block
    name so the Dispatcher can tell where block boundaries fall just
    by comparing two edges' ``layout_block``.
    """

    id: str                  # segment ident
    a: str                   # TopoNode.id
    b: str                   # TopoNode.id
    layout_block: str | None
    main_line: bool


@dataclass
class Topology:
    """The derived-state graph for one panel."""

    nodes: dict[str, TopoNode] = field(default_factory=dict)
    edges: list[TopoEdge] = field(default_factory=list)
    # Index: node id -> list of edge ids touching it. Rebuilt eagerly
    # by :func:`build_topology`; callers treat it as read-only.
    _by_node: dict[str, list[str]] = field(default_factory=dict)
    # Index: block name -> list of edge ids whose ``layout_block``
    # matches. Edges whose block is ``None`` do NOT appear here.
    _by_block: dict[str, list[str]] = field(default_factory=dict)

    # ----- lookups --------------------------------------------------------

    def edges_touching(self, node_id: str) -> list[TopoEdge]:
        """All edges incident to *node_id*."""
        return [e for e in self.edges if e.id in self._by_node.get(node_id, ())]

    def edges_in_block(self, layout_block: str) -> list[TopoEdge]:
        """All edges whose ``layout_block`` equals *layout_block*."""
        return [
            e for e in self.edges
            if e.id in self._by_block.get(layout_block, ())
        ]

    def turnout_legs(self, turnout_ident: str) -> list[TopoNode]:
        """The four leg nodes (A/B/C/D) of a turnout, in that order,
        skipping any that don't actually exist in the graph (e.g.
        a right-hand turnout has A/B/C but no D).
        """
        legs = []
        for suffix in ("A", "B", "C", "D"):
            nid = f"t:{turnout_ident}:{suffix}"
            if nid in self.nodes:
                legs.append(self.nodes[nid])
        return legs

    def block_boundaries(self) -> list[tuple[TopoEdge, TopoEdge]]:
        """Pairs of edges that share a node AND have different
        ``layout_block`` assignments -- i.e. where the layout crosses a
        block boundary. Order within each pair is insertion order; a
        boundary is reported once per shared node, not once per edge.
        """
        out: list[tuple[TopoEdge, TopoEdge]] = []
        by_id = {e.id: e for e in self.edges}
        seen: set[tuple[str, str]] = set()
        for node_id, edge_ids in self._by_node.items():
            edges_here = [by_id[eid] for eid in edge_ids if eid in by_id]
            for i, e1 in enumerate(edges_here):
                for e2 in edges_here[i + 1:]:
                    if e1.layout_block != e2.layout_block:
                        key = (min(e1.id, e2.id), max(e1.id, e2.id))
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append((e1, e2))
        return out

    def connected_component(self, start_node_id: str) -> set[str]:
        """All node ids reachable from *start_node_id* via edges.

        Does NOT walk turnout-internal connections (A↔B for a closed
        turnout etc.) -- callers that care about those compose the
        walk with :meth:`turnout_legs`.
        """
        if start_node_id not in self.nodes:
            return set()
        seen = {start_node_id}
        frontier = [start_node_id]
        while frontier:
            n = frontier.pop()
            for e in self.edges_touching(n):
                other = e.b if e.a == n else e.a
                if other not in seen:
                    seen.add(other)
                    frontier.append(other)
        return seen


# ---- builder -----------------------------------------------------------


_TURNOUT_LEG_SUFFIX: dict[int, str] = {
    HIT_TURNOUT_A: "A",
    HIT_TURNOUT_B: "B",
    HIT_TURNOUT_C: "C",
    HIT_TURNOUT_D: "D",
}


def _point_node(p: PositionablePoint) -> TopoNode:
    return TopoNode(
        id=f"p:{p.ident}", kind="point",
        x=p.x, y=p.y, panel_ident=p.ident,
    )


def _turnout_leg_nodes(t: LayoutTurnoutGeom) -> list[TopoNode]:
    """Emit one node per existing leg of *t*. A right-hand or wye
    turnout has three legs (A/B/C); a double-crossover adds D.
    """
    nodes = [
        TopoNode(id=f"t:{t.ident}:A", kind="turnout_a",
                 x=t.xa, y=t.ya, panel_ident=t.ident,
                 turnout_sysname=t.turnout),
        TopoNode(id=f"t:{t.ident}:B", kind="turnout_b",
                 x=t.xb, y=t.yb, panel_ident=t.ident,
                 turnout_sysname=t.turnout),
        TopoNode(id=f"t:{t.ident}:C", kind="turnout_c",
                 x=t.xc, y=t.yc, panel_ident=t.ident,
                 turnout_sysname=t.turnout),
    ]
    # D leg isn't modelled on LayoutTurnoutGeom today. When we extend
    # the parser to pick up xd/yd for double-crossovers / slips, add a
    # fourth node here.
    return nodes


def _endpoint_node_id(
    seg: TrackSegment, which: Literal["1", "2"],
    points: dict[str, PositionablePoint],
    turnouts: dict[str, LayoutTurnoutGeom],
) -> str | None:
    """Resolve one end of a :class:`TrackSegment` to a node id, or
    ``None`` if the reference dangles (unknown ident).
    """
    name = seg.connect1_name if which == "1" else seg.connect2_name
    hit_type = seg.type1 if which == "1" else seg.type2
    if hit_type == HIT_POS_POINT:
        if name not in points:
            return None
        return f"p:{name}"
    suffix = _TURNOUT_LEG_SUFFIX.get(hit_type)
    if suffix is None:
        # Level-xings, slips, etc. aren't walked yet. Future work.
        return None
    if name not in turnouts:
        return None
    return f"t:{name}:{suffix}"


def build_topology(model: PanelModel) -> Topology:
    """Walk *model* and return its topology graph.

    Cheap to rebuild from scratch after each panel edit; callers do
    not cache the returned :class:`Topology` across edits.
    """
    topo = Topology()
    point_by_ident = {p.ident: p for p in model.positionable_points}
    turnout_by_ident = {t.ident: t for t in model.layout_turnouts}

    # Nodes first so edge resolution can refer to them.
    for p in model.positionable_points:
        node = _point_node(p)
        topo.nodes[node.id] = node
    for t in model.layout_turnouts:
        for node in _turnout_leg_nodes(t):
            topo.nodes[node.id] = node

    # Edges: one per TrackSegment whose both endpoints resolved.
    by_node: dict[str, list[str]] = defaultdict(list)
    by_block: dict[str, list[str]] = defaultdict(list)
    for seg in model.track_segments:
        a_id = _endpoint_node_id(seg, "1", point_by_ident, turnout_by_ident)
        b_id = _endpoint_node_id(seg, "2", point_by_ident, turnout_by_ident)
        if a_id is None or b_id is None:
            continue
        edge = TopoEdge(
            id=seg.ident, a=a_id, b=b_id,
            layout_block=seg.layout_block,
            main_line=seg.main_line,
        )
        topo.edges.append(edge)
        by_node[a_id].append(edge.id)
        by_node[b_id].append(edge.id)
        if edge.layout_block:
            by_block[edge.layout_block].append(edge.id)

    topo._by_node = dict(by_node)
    topo._by_block = dict(by_block)
    return topo


__all__ = [
    "NodeKind",
    "TopoEdge",
    "TopoNode",
    "Topology",
    "build_topology",
]
