"""Inline-SVG renderer for a parsed :class:`PanelModel`.

The data model and parser (``PanelModel``, ``parse_panel_xml``, and the
per-element dataclasses) moved to :mod:`pmri_core.persistence.panel_model`
so that both the web layer and the ``layout_topology`` bean in
``pmri-core`` can consume them. This module re-exports those names for
backward compatibility and keeps only the SVG rendering.

The SVG renderer is slated for replacement by a Canvas-based renderer
(the ongoing Layout Panel Editor work), but it stays live for the
read-only panel viewer until the Canvas path ships.

The renderer:

* draws every parsed element at its XML (x, y) in a viewport sized to
  the panel's declared ``width`` / ``height``;
* colours bean-backed icons (sensor / turnout / signal) by current bean
  state when the caller passes a ``bean_state`` function;
* tags every bean-backed element with ``data-sysname`` + ``data-kind``
  so client-side JS can wire click-to-toggle and live-refresh without
  re-parsing the XML.
"""
from __future__ import annotations

from typing import Callable

from pmri_core.persistence.panel_model import (
    HIT_POS_POINT,
    HIT_TURNOUT_A,
    HIT_TURNOUT_B,
    HIT_TURNOUT_C,
    HIT_TURNOUT_D,
    IconLabel,
    LayoutTurnoutGeom,
    MemoryIcon,
    PanelModel,
    PositionablePoint,
    SensorIcon,
    SignalIcon,
    TextLabel,
    TrackSegment,
    TurnoutIcon,
    parse_panel_xml,
)


# Bean state codes. The parser doesn't know about these; the caller
# passes a lookup function to colour icons by state. States follow
# JMRI's enum ints (pmri_core.state).
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


def _segment_endpoint(
    name: str, hit_type: int,
    points: dict[str, PositionablePoint],
    turnouts: dict[str, LayoutTurnoutGeom],
) -> tuple[float, float] | None:
    """Resolve a ``tracksegment`` endpoint to an (x, y) pair.

    When the endpoint type is ``HIT_POS_POINT`` we look up the
    :class:`PositionablePoint` by ident. For turnout-leg endpoints we
    pick the matching leg's (xa, ya) / (xb, yb) / (xc, yc) /
    (xd? not modelled) on the referenced :class:`LayoutTurnoutGeom`.
    """
    if hit_type == HIT_POS_POINT:
        pt = points.get(name)
        if pt is None:
            return None
        return pt.x, pt.y
    tt = turnouts.get(name)
    if tt is None:
        return None
    if hit_type == HIT_TURNOUT_A:
        return tt.xa, tt.ya
    if hit_type == HIT_TURNOUT_B:
        return tt.xb, tt.yb
    if hit_type == HIT_TURNOUT_C:
        return tt.xc, tt.yc
    if hit_type == HIT_TURNOUT_D:
        # Not modelled on LayoutTurnoutGeom today; fall through to centre
        # so the segment still draws rather than disappearing.
        return tt.xcen, tt.ycen
    return None


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
        f'class="pmri-panel-svg" '
        f'style="background:#fff;border:1px solid var(--muted,#aaa)">'
    )
    # Track segments draw first so icons sit on top.
    pts = model.point_by_ident
    tos = model.turnout_by_ident
    for seg in model.track_segments:
        a = _segment_endpoint(seg.connect1_name, seg.type1, pts, tos)
        b = _segment_endpoint(seg.connect2_name, seg.type2, pts, tos)
        if a is None or b is None:
            continue
        width = 4 if seg.main_line else 2
        dash = ' stroke-dasharray="6,4"' if seg.dashed else ""
        parts.append(
            f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" '
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
