"""Panel / Layout XML parser + in-memory model.

Lives in pmri-core (not pmri-web) because the model is consumed by both
the web renderer AND the ``layout_topology`` bean, which lives in core.
The companion pmri-web ``panel_svg`` module re-exports these names so
existing callers keep working through the migration.

Two panel flavours parse into the same model:

* ``<paneleditor>`` -- icon-tile layout. Just ``<sensoricon>`` /
  ``<turnouticon>`` / ``<positionablelabel>`` / etc. at absolute
  (x, y), with ``rotate`` increments of 90 degrees.
* ``<layouteditor>`` -- the track-plan layout. Carries
  ``<positionablepoint>`` anchors + ``<tracksegment>`` connectors +
  ``<layoutturnout>`` turnouts + the same icon vocabulary.

Each ``<tracksegment>`` carries ``type1`` / ``type2`` attributes that
say what kind of endpoint each end is (positionable point vs turnout
leg); without those the topology graph can't route through turnouts,
so the model exposes them. Values match JMRI's ``HitPointType`` enum:

* 1 = POS_POINT   (a ``<positionablepoint>`` with the matching ident)
* 2 = TURNOUT_A
* 3 = TURNOUT_B
* 4 = TURNOUT_C
* 5 = TURNOUT_D

Elements we don't recognise are **skipped silently** (counted into
:attr:`PanelModel.unknown_tags`). This keeps a real-world panel
rendering gracefully even when it contains Panel Editor features we
haven't ported yet.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from lxml import etree


# ---- HitPointType constants (subset we care about for topology) ---------


HIT_POS_POINT = 1
HIT_TURNOUT_A = 2
HIT_TURNOUT_B = 3
HIT_TURNOUT_C = 4
HIT_TURNOUT_D = 5
HIT_LEVEL_XING_A = 6
HIT_LEVEL_XING_B = 7
HIT_LEVEL_XING_C = 8
HIT_LEVEL_XING_D = 9
HIT_SLIP_A = 21
HIT_SLIP_B = 22
HIT_SLIP_C = 23
HIT_SLIP_D = 24


# ---- model --------------------------------------------------------------


@dataclass(frozen=True)
class SensorIcon:
    sensor: str
    x: float
    y: float
    rotate: int = 0


@dataclass(frozen=True)
class TurnoutIcon:
    turnout: str
    x: float
    y: float
    rotate: int = 0


@dataclass(frozen=True)
class SignalIcon:
    """``signalmasticon`` or ``signalheadicon`` -- rendered the same for MVP."""

    system_name: str
    x: float
    y: float
    kind: Literal["mast", "head"] = "mast"


@dataclass(frozen=True)
class MemoryIcon:
    memory: str
    x: float
    y: float


@dataclass(frozen=True)
class TextLabel:
    text: str
    x: float
    y: float


@dataclass(frozen=True)
class IconLabel:
    """``positionablelabel`` with an ``icon=`` attribute -- we don't
    fetch the icon file; just show a stub square + the href as hover."""

    icon_url: str
    x: float
    y: float


@dataclass(frozen=True)
class PositionablePoint:
    """LayoutEditor anchor at (x, y). Track segments reference it by
    ``ident`` as one endpoint."""

    ident: str
    x: float
    y: float


@dataclass(frozen=True)
class TrackSegment:
    """A line (or arc, when we gain that scope) between two endpoints.

    ``connect1_name`` / ``connect2_name`` reference either a
    :class:`PositionablePoint`'s ident (when ``type1`` / ``type2`` is
    ``HIT_POS_POINT``) or a :class:`LayoutTurnoutGeom` ident (when the
    type is one of the ``HIT_TURNOUT_*`` values -- the suffix selects
    which leg).
    """

    ident: str
    connect1_name: str
    connect2_name: str
    type1: int = HIT_POS_POINT
    type2: int = HIT_POS_POINT
    main_line: bool = False
    dashed: bool = False
    layout_block: str | None = None   # <blockname> child, if any


@dataclass(frozen=True)
class LayoutTurnoutGeom:
    """LayoutEditor turnout: a center + three endpoints (A/B/C).

    JMRI's XML carries the center at ``xcen``/``ycen`` and the three
    endpoints at ``xa``/``ya`` etc. We reproduce the two legs as
    lines from center to A/B/C with the C leg coloured by the bean's
    current state (closed vs thrown). ``layout_block`` is the per-turnout
    block assignment (``<blockname>`` child in the XML).
    """

    turnout: str                  # bean system name, or "" for unassigned
    ident: str
    xcen: float
    ycen: float
    xa: float
    ya: float
    xb: float
    yb: float
    xc: float
    yc: float
    layout_block: str | None = None


@dataclass
class PanelModel:
    """Parsed panel content ready to render."""

    name: str = ""
    width: int = 1200
    height: int = 800
    sensor_icons: list[SensorIcon] = field(default_factory=list)
    turnout_icons: list[TurnoutIcon] = field(default_factory=list)
    signal_icons: list[SignalIcon] = field(default_factory=list)
    memory_icons: list[MemoryIcon] = field(default_factory=list)
    text_labels: list[TextLabel] = field(default_factory=list)
    icon_labels: list[IconLabel] = field(default_factory=list)
    positionable_points: list[PositionablePoint] = field(default_factory=list)
    track_segments: list[TrackSegment] = field(default_factory=list)
    layout_turnouts: list[LayoutTurnoutGeom] = field(default_factory=list)
    unknown_tags: Counter[str] = field(default_factory=Counter)

    @property
    def point_by_ident(self) -> dict[str, PositionablePoint]:
        return {p.ident: p for p in self.positionable_points}

    @property
    def turnout_by_ident(self) -> dict[str, LayoutTurnoutGeom]:
        return {t.ident: t for t in self.layout_turnouts}


# ---- parser -------------------------------------------------------------


_KNOWN_ICON_TAGS = frozenset({
    "sensoricon", "turnouticon", "signalmasticon", "signalheadicon",
    "memoryicon", "positionablelabel",
})
_KNOWN_LE_TAGS = frozenset({
    "positionablepoint", "tracksegment", "layoutturnout",
})
_KNOWN_SKIP = frozenset({
    # Infrastructure / configuration elements that aren't drawable but
    # legitimately appear inside a panel section -- don't count them
    # as "unknown drawable types".
    "operations", "operation", "signalhead", "signalmast", "block",
    "blockname",     # child of <tracksegment> and <layoutturnout>
    "logix", "conditional", "variable", "action", "layoutShape",
    "turnoutbx", "turnoutcx", "turnoutwid", "xoverlong", "xoverhwid",
    "xovershort", "sidetrackwidth", "mainlinetrackwidth",
    "trackAsColourType", "defaulttrackcolor", "defaultoccupiedtrackcolor",
    "defaultalternativetrackcolor", "defaulttextcolor",
    "turnoutcirclecolor", "turnoutcirclethrowncolor",
    "turnoutCircleColourType", "turnoutcirclesize", "turnoutcircles",
    "turnoutdrawunselectedleg", "turnoutfillcontrolcircles",
    "layoutTrackDrawingOptions", "gridSize", "gridSize2nd",
    "snaponadd", "snaponmove", "antialiasing", "drawgrid",
    "showhelpbar", "tooltipsinedit", "tooltipsnotedit",
    "autoblkgenerate", "scrollable", "editable", "controlling",
    "animating", "sliders", "redBackground", "greenBackground",
    "blueBackground", "panelmenu", "highlightCursor", "fastclock",
    "openDispatcher", "useDirectTurnoutControl", "LayoutEditorType",
})


def _f(elem: etree._Element, attr: str, default: float = 0.0) -> float:
    """Parse a float attribute. Missing / malformed -> *default*."""
    raw = elem.get(attr)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _i(elem: etree._Element, attr: str, default: int = 0) -> int:
    raw = elem.get(attr)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _child_text(elem: etree._Element, tag: str) -> str | None:
    """Return the trimmed text of the first ``<tag>`` child, if any."""
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _hit_type(elem: etree._Element, attr: str) -> int:
    """Read a ``HitPointType``-valued attribute. JMRI's 5.x writer
    emits the enum NAME (``"POS_POINT"``, ``"TURNOUT_A"``); older
    files used the ordinal. Accept either, fall back to POS_POINT.
    """
    raw = elem.get(attr)
    if raw is None:
        return HIT_POS_POINT
    raw = raw.strip()
    if not raw:
        return HIT_POS_POINT
    # Numeric form.
    try:
        return int(raw)
    except ValueError:
        pass
    # Named form.
    return _HIT_NAME_TO_INT.get(raw, HIT_POS_POINT)


_HIT_NAME_TO_INT: dict[str, int] = {
    "NONE": 0,
    "POS_POINT": HIT_POS_POINT,
    "TURNOUT_A": HIT_TURNOUT_A,
    "TURNOUT_B": HIT_TURNOUT_B,
    "TURNOUT_C": HIT_TURNOUT_C,
    "TURNOUT_D": HIT_TURNOUT_D,
    "LEVEL_XING_A": HIT_LEVEL_XING_A,
    "LEVEL_XING_B": HIT_LEVEL_XING_B,
    "LEVEL_XING_C": HIT_LEVEL_XING_C,
    "LEVEL_XING_D": HIT_LEVEL_XING_D,
    "SLIP_A": HIT_SLIP_A,
    "SLIP_B": HIT_SLIP_B,
    "SLIP_C": HIT_SLIP_C,
    "SLIP_D": HIT_SLIP_D,
}


def parse_panel_xml(xml_bytes: bytes) -> PanelModel:
    """Parse *xml_bytes* (a complete JMRI panel XML file) into a model.

    Handles both ``<paneleditor>`` and ``<layouteditor>`` roots; the
    file's actual root is ``<layout-config>`` so we look for either
    kind of panel inside.
    """
    root = etree.fromstring(xml_bytes)
    model = PanelModel()
    _panel_tags = {
        "paneleditor", "layouteditor",
        "PanelEditor", "LayoutEditor", "ControlPanelEditor",
        "SwitchboardEditor",
    }
    panel_elem: etree._Element | None = None
    for cand in root.iter():
        if cand.tag in _panel_tags:
            panel_elem = cand
            break
    if panel_elem is None:
        return model
    model.name = panel_elem.get("name", "")
    model.width = _i(panel_elem, "panelwidth", _i(panel_elem, "width", 1200))
    model.height = _i(panel_elem, "panelheight", _i(panel_elem, "height", 800))

    for child in panel_elem.iter():
        if child is panel_elem:
            continue
        tag = child.tag
        if not isinstance(tag, str):
            continue   # comment / PI
        _parse_one(model, child, tag)
    return model


def _parse_one(model: PanelModel, child: etree._Element, tag: str) -> None:
    if tag == "sensoricon":
        sysname = child.get("sensor", "")
        model.sensor_icons.append(SensorIcon(
            sensor=sysname,
            x=_f(child, "x"), y=_f(child, "y"),
            rotate=_i(child, "rotate", 0),
        ))
    elif tag == "turnouticon":
        model.turnout_icons.append(TurnoutIcon(
            turnout=child.get("turnout", ""),
            x=_f(child, "x"), y=_f(child, "y"),
            rotate=_i(child, "rotate", 0),
        ))
    elif tag in ("signalmasticon", "signalheadicon"):
        attr = "signalmast" if tag == "signalmasticon" else "signalhead"
        model.signal_icons.append(SignalIcon(
            system_name=child.get(attr, ""),
            x=_f(child, "x"), y=_f(child, "y"),
            kind="mast" if tag == "signalmasticon" else "head",
        ))
    elif tag == "memoryicon":
        model.memory_icons.append(MemoryIcon(
            memory=child.get("memory", ""),
            x=_f(child, "x"), y=_f(child, "y"),
        ))
    elif tag == "positionablelabel":
        text = child.get("text") or ""
        icon = child.get("icon") or ""
        if text:
            model.text_labels.append(TextLabel(
                text=text,
                x=_f(child, "x"), y=_f(child, "y"),
            ))
        elif icon:
            model.icon_labels.append(IconLabel(
                icon_url=icon,
                x=_f(child, "x"), y=_f(child, "y"),
            ))
    elif tag == "positionablepoint":
        model.positionable_points.append(PositionablePoint(
            ident=child.get("ident", ""),
            x=_f(child, "x"), y=_f(child, "y"),
        ))
    elif tag == "tracksegment":
        model.track_segments.append(TrackSegment(
            ident=child.get("ident", ""),
            connect1_name=child.get("connect1name", ""),
            connect2_name=child.get("connect2name", ""),
            type1=_hit_type(child, "type1"),
            type2=_hit_type(child, "type2"),
            main_line=(child.get("mainline", "no").lower() == "yes"),
            dashed=(child.get("dashed", "no").lower() == "yes"),
            layout_block=(_child_text(child, "blockname")
                          or child.get("blockname") or None),
        ))
    elif tag == "layoutturnout":
        model.layout_turnouts.append(LayoutTurnoutGeom(
            turnout=child.get("turnoutname", ""),
            ident=child.get("ident", ""),
            xcen=_f(child, "xcen"), ycen=_f(child, "ycen"),
            xa=_f(child, "xa"), ya=_f(child, "ya"),
            xb=_f(child, "xb"), yb=_f(child, "yb"),
            xc=_f(child, "xc"), yc=_f(child, "yc"),
            layout_block=(_child_text(child, "blockname")
                          or child.get("blockname") or None),
        ))
    elif tag in _KNOWN_SKIP:
        return
    else:
        model.unknown_tags[tag] += 1


__all__ = [
    "HIT_LEVEL_XING_A", "HIT_LEVEL_XING_B", "HIT_LEVEL_XING_C",
    "HIT_LEVEL_XING_D",
    "HIT_POS_POINT",
    "HIT_SLIP_A", "HIT_SLIP_B", "HIT_SLIP_C", "HIT_SLIP_D",
    "HIT_TURNOUT_A", "HIT_TURNOUT_B", "HIT_TURNOUT_C", "HIT_TURNOUT_D",
    "IconLabel",
    "LayoutTurnoutGeom",
    "MemoryIcon",
    "PanelModel",
    "PositionablePoint",
    "SensorIcon",
    "SignalIcon",
    "TextLabel",
    "TrackSegment",
    "TurnoutIcon",
    "parse_panel_xml",
]
