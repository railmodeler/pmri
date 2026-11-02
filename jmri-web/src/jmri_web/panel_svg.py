"""Parse JMRI panel XML + render as inline SVG.

JMRI panels come in two flavours:

* ``<paneleditor>`` -- the icon-tile layout used by control panels.
  Just ``<sensoricon>`` / ``<turnouticon>`` / ``<positionablelabel>``
  / etc. at absolute (x, y) positions, with ``rotate`` increments
  of 90 degrees.
* ``<layouteditor>`` -- the track-plan layout. Carries
  ``<positionablepoint>`` anchors + ``<tracksegment>`` connectors +
  ``<layoutturnout>`` turnouts + the same icon vocabulary.

This module parses either into a :class:`PanelModel` of position-tagged
primitives and renders SVG that:

* Shows every parsed element at its XML (x, y) in a viewport sized
  to the panel's declared ``width`` / ``height``.
* Draws bean-backed icons (sensor / turnout / signal) coloured by
  current bean state when the caller passes a *bean_state* function.
* Tags every bean-backed element with ``data-sysname`` + ``data-kind``
  so client-side JS can wire click-to-toggle and live-refresh
  without re-parsing the XML.

Elements we don't recognise are **skipped silently** (counted into
:attr:`PanelModel.unknown_tags` so the page can show a "N element
types we didn't draw" footnote). This keeps a real-world panel
rendering gracefully even when it contains Panel Editor features
we haven't ported yet.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Literal

from lxml import etree


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
    """A line (or arc, when we gain that scope) between two anchor
    points identified by their ``ident``."""

    ident: str
    connect1_name: str
    connect2_name: str
    main_line: bool = False
    dashed: bool = False


@dataclass(frozen=True)
class LayoutTurnoutGeom:
    """LayoutEditor turnout: a center + three endpoints (A/B/C).

    JMRI's XML carries the center at ``xcen``/``ycen`` and the three
    endpoints at ``xa``/``ya`` etc. We reproduce the two legs as
    lines from center to A/B/C with the C leg coloured by the bean's
    current state (closed vs thrown).
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


def parse_panel_xml(xml_bytes: bytes) -> PanelModel:
    """Parse *xml_bytes* (a complete JMRI panel XML file) into a model.

    Handles both ``<paneleditor>`` and ``<layouteditor>`` roots; the
    file's actual root is ``<layout-config>`` so we look for either
    kind of panel inside.
    """
    root = etree.fromstring(xml_bytes)
    model = PanelModel()
    # Find the first paneleditor / layouteditor child. Real JMRI
    # files have exactly one of these (sometimes zero if the file is
    # a pure configuration dump), so iterate and take the first.
    panel_elem: etree._Element | None = None
    # JMRI uses lowercase element names inside ``<layout-config>``
    # (``paneleditor``, ``layouteditor``) but the schema's standalone
    # root uses PascalCase names (``PanelEditor``, ``LayoutEditor``,
    # ``ControlPanelEditor``, ``SwitchboardEditor``). Accept either so
    # the renderer works against real panel files AND the simpler
    # standalone-root form used in test fixtures + the JSON service's
    # existing panel listings.
    _panel_tags = {
        "paneleditor", "layouteditor",
        "PanelEditor", "LayoutEditor", "ControlPanelEditor",
        "SwitchboardEditor",
    }
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
            main_line=(child.get("mainline", "no").lower() == "yes"),
            dashed=(child.get("dashed", "no").lower() == "yes"),
        ))
    elif tag == "layoutturnout":
        model.layout_turnouts.append(LayoutTurnoutGeom(
            turnout=child.get("turnoutname", ""),
            ident=child.get("ident", ""),
            xcen=_f(child, "xcen"), ycen=_f(child, "ycen"),
            xa=_f(child, "xa"), ya=_f(child, "ya"),
            xb=_f(child, "xb"), yb=_f(child, "yb"),
            xc=_f(child, "xc"), yc=_f(child, "yc"),
        ))
    elif tag in _KNOWN_SKIP:
        return
    else:
        model.unknown_tags[tag] += 1


# ---- renderer -----------------------------------------------------------


# Bean state codes. The parser doesn't know about these; the caller
# passes a lookup function to colour icons by state. States follow
# JMRI's enum ints (jmri_core.state).
_SENSOR_ACTIVE = 2
_SENSOR_INACTIVE = 4
_TURNOUT_CLOSED = 2
_TURNOUT_THROWN = 4


BeanStateFn = Callable[[str, str], int | None]
"""(kind, system_name) -> integer state, or None if the bean is unknown.

``kind`` is one of ``"sensor"``, ``"turnout"``, ``"memory"``,
``"signal"``. The caller resolves these against the active
``SystemConnection``'s managers.
"""


def _sensor_fill(state: int | None) -> str:
    if state == _SENSOR_ACTIVE:
        return "#c33"
    if state == _SENSOR_INACTIVE:
        return "#3a3"
    return "#aaa"


def _turnout_fill(state: int | None) -> tuple[str, str]:
    """(closed_leg_colour, thrown_leg_colour) for the half-icon pair."""
    if state == _TURNOUT_CLOSED:
        return "#3a3", "#333"
    if state == _TURNOUT_THROWN:
        return "#333", "#c33"
    return "#888", "#888"


def render_svg(
    model: PanelModel,
    bean_state: BeanStateFn | None = None,
) -> str:
    """Serialise *model* as an inline SVG string.

    Width/height come from the parsed panel; the viewBox matches so a
    CSS ``max-width:100%`` on the embedding element scales the whole
    panel proportionally.

    Elements with a bean reference carry ``data-sysname`` + ``data-kind``
    so a client-side script can wire interactivity (click to toggle,
    live state refresh) without reparsing.
    """
    def state_for(kind: str, name: str) -> int | None:
        if bean_state is None or not name:
            return None
        return bean_state(kind, name)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {model.width} {model.height}" '
        f'width="{model.width}" height="{model.height}" '
        f'class="jmri-panel-svg" '
        f'style="background:#fff;border:1px solid var(--muted,#aaa)">'
    )
    # Track segments draw first so icons sit on top.
    pts = model.point_by_ident
    for seg in model.track_segments:
        a = pts.get(seg.connect1_name)
        b = pts.get(seg.connect2_name)
        if a is None or b is None:
            continue
        width = 4 if seg.main_line else 2
        dash = ' stroke-dasharray="6,4"' if seg.dashed else ""
        parts.append(
            f'<line x1="{a.x}" y1="{a.y}" x2="{b.x}" y2="{b.y}" '
            f'stroke="#444" stroke-width="{width}"{dash} />'
        )
    for pp in model.positionable_points:
        parts.append(
            f'<circle cx="{pp.x}" cy="{pp.y}" r="2" fill="#888" />'
        )
    for lt in model.layout_turnouts:
        state = state_for("turnout", lt.turnout)
        closed_c, thrown_c = _turnout_fill(state)
        active_x, active_y = (
            (lt.xb, lt.yb) if state == _TURNOUT_CLOSED
            else (lt.xc, lt.yc) if state == _TURNOUT_THROWN
            else (lt.xb, lt.yb)
        )
        # A: stock rail (fixed), B: closed leg, C: thrown leg.
        parts.append(
            f'<g data-kind="turnout" data-sysname="{lt.turnout}">'
            f'<line x1="{lt.xcen}" y1="{lt.ycen}" x2="{lt.xa}" y2="{lt.ya}" '
            f'stroke="#444" stroke-width="3" />'
            f'<line x1="{lt.xcen}" y1="{lt.ycen}" x2="{lt.xb}" y2="{lt.yb}" '
            f'stroke="{closed_c}" stroke-width="3" />'
            f'<line x1="{lt.xcen}" y1="{lt.ycen}" x2="{lt.xc}" y2="{lt.yc}" '
            f'stroke="{thrown_c}" stroke-width="3" />'
            # Hit-target circle at the pivot.
            f'<circle cx="{lt.xcen}" cy="{lt.ycen}" r="6" fill="transparent" '
            f'stroke="#888" stroke-width="1" style="cursor:pointer" />'
            f'<title>{lt.turnout}</title>'
            f'</g>'
        )
        _ = active_x, active_y   # placeholder for future "indicator" dot
    # Icons.
    for si in model.sensor_icons:
        state = state_for("sensor", si.sensor)
        parts.append(
            f'<g data-kind="sensor" data-sysname="{si.sensor}">'
            f'<circle cx="{si.x + 8}" cy="{si.y + 8}" r="7" '
            f'fill="{_sensor_fill(state)}" stroke="#222" stroke-width="1" />'
            f'<title>{si.sensor}</title>'
            f'</g>'
        )
    for ti in model.turnout_icons:
        state = state_for("turnout", ti.turnout)
        closed_c, thrown_c = _turnout_fill(state)
        parts.append(
            f'<g data-kind="turnout" data-sysname="{ti.turnout}" '
            f'style="cursor:pointer">'
            f'<rect x="{ti.x}" y="{ti.y}" width="16" height="16" '
            f'fill="transparent" stroke="#222" stroke-width="1" />'
            f'<rect x="{ti.x}" y="{ti.y}" width="8" height="16" '
            f'fill="{closed_c}" />'
            f'<rect x="{ti.x + 8}" y="{ti.y}" width="8" height="16" '
            f'fill="{thrown_c}" />'
            f'<title>{ti.turnout}</title>'
            f'</g>'
        )
    for sg in model.signal_icons:
        state = state_for("signal", sg.system_name)
        colour = "#aaa"   # signals have many states; MVP shows a dot
        _ = state
        parts.append(
            f'<g data-kind="signal" data-sysname="{sg.system_name}">'
            f'<circle cx="{sg.x + 6}" cy="{sg.y + 6}" r="6" '
            f'fill="{colour}" stroke="#222" />'
            f'<title>{sg.system_name}</title>'
            f'</g>'
        )
    for mi in model.memory_icons:
        parts.append(
            f'<g data-kind="memory" data-sysname="{mi.memory}">'
            f'<rect x="{mi.x}" y="{mi.y}" width="48" height="16" '
            f'fill="#ffd" stroke="#776" stroke-width="1" />'
            f'<text x="{mi.x + 2}" y="{mi.y + 12}" font-size="10" '
            f'font-family="monospace" fill="#333">{mi.memory}</text>'
            f'</g>'
        )
    for tl in model.text_labels:
        parts.append(
            f'<text x="{tl.x}" y="{tl.y + 12}" font-size="12" '
            f'fill="#222">{_escape(tl.text)}</text>'
        )
    for il in model.icon_labels:
        parts.append(
            f'<g><rect x="{il.x}" y="{il.y}" width="24" height="24" '
            f'fill="#eef" stroke="#99a" stroke-dasharray="2,2" />'
            f'<title>{_escape(il.icon_url)}</title></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _escape(s: str) -> str:
    """Minimal XML-text escaping for user-supplied strings."""
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


__all__ = [
    "BeanStateFn",
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
    "render_svg",
]
