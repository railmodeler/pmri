"""panel_svg -- parser + SVG renderer for JMRI panels."""
from __future__ import annotations

from pmri_web.panel_svg import (
    PanelModel,
    SensorIcon,
    TurnoutIcon,
    parse_panel_xml,
    render_svg,
)


# ---- parser ------------------------------------------------------------


def _wrap(body: str) -> bytes:
    """Minimal layout-config shell so lxml accepts the fragments."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<layout-config>' + body + '</layout-config>'
    ).encode()


def test_parse_panel_editor_extracts_icons() -> None:
    model = parse_panel_xml(_wrap(
        '<paneleditor name="Yard" width="800" height="600">'
        '<sensoricon sensor="LS1" x="100" y="200" rotate="0" />'
        '<turnouticon turnout="LT5" x="150" y="220" rotate="2" />'
        '<positionablelabel x="10" y="10" text="North Yard" />'
        '</paneleditor>'
    ))
    assert model.name == "Yard"
    assert model.width == 800
    assert model.height == 600
    assert len(model.sensor_icons) == 1
    assert model.sensor_icons[0].sensor == "LS1"
    assert model.sensor_icons[0].x == 100.0
    assert len(model.turnout_icons) == 1
    assert model.turnout_icons[0].turnout == "LT5"
    assert len(model.text_labels) == 1
    assert model.text_labels[0].text == "North Yard"


def test_parse_layout_editor_extracts_geometry() -> None:
    model = parse_panel_xml(_wrap(
        '<layouteditor name="Mainline" panelwidth="1000" panelheight="500">'
        '<positionablepoint ident="pp1" x="100.0" y="200.0" />'
        '<positionablepoint ident="pp2" x="400.0" y="200.0" />'
        '<tracksegment ident="t1" connect1name="pp1" connect2name="pp2" mainline="yes" />'
        '<layoutturnout ident="to1" turnoutname="LT5"'
        '    xcen="300" ycen="200" xa="250" ya="200" xb="350" yb="200" xc="340" yc="150" />'
        '</layouteditor>'
    ))
    assert model.name == "Mainline"
    assert model.width == 1000
    assert model.height == 500
    assert len(model.positionable_points) == 2
    assert len(model.track_segments) == 1
    assert model.track_segments[0].main_line is True
    assert len(model.layout_turnouts) == 1
    lt = model.layout_turnouts[0]
    assert lt.turnout == "LT5"
    assert (lt.xcen, lt.ycen) == (300.0, 200.0)


def test_parse_counts_unknown_element_tags() -> None:
    """Unknown element types are counted into ``unknown_tags`` instead
    of crashing the parse -- a panel that uses a feature we haven't
    ported yet still renders what we recognise, with a note."""
    model = parse_panel_xml(_wrap(
        '<paneleditor name="x" width="100" height="100">'
        '<trainicon x="10" y="10" />'
        '<trainicon x="20" y="20" />'
        '<locoicon x="30" y="30" />'
        '</paneleditor>'
    ))
    assert model.unknown_tags["trainicon"] == 2
    assert model.unknown_tags["locoicon"] == 1
    # No drawable primitives came out of it.
    assert not model.sensor_icons and not model.turnout_icons


def test_parse_ignores_known_non_drawable_configuration_elements() -> None:
    """Non-drawable children of the panel section (``defaulttrackcolor``,
    ``gridSize``, etc.) must NOT inflate ``unknown_tags`` -- otherwise
    every rendered panel shows a long "we didn't draw these" list of
    config knobs that aren't drawables anyway."""
    model = parse_panel_xml(_wrap(
        '<layouteditor name="x" panelwidth="100" panelheight="100">'
        '<defaulttrackcolor>#000</defaulttrackcolor>'
        '<gridSize>10</gridSize>'
        '<snaponadd>yes</snaponadd>'
        '</layouteditor>'
    ))
    assert model.unknown_tags == {}


def test_parse_missing_panel_section_returns_empty_model() -> None:
    """A layout-config with no paneleditor / layouteditor child yields
    an empty model rather than crashing (operations-only XML files
    look like this)."""
    model = parse_panel_xml(_wrap('<turnouts/>'))
    assert isinstance(model, PanelModel)
    assert not model.sensor_icons
    assert not model.turnout_icons


# ---- renderer ----------------------------------------------------------


def test_render_svg_contains_viewport_and_bean_hooks() -> None:
    """Every bean-backed shape carries ``data-kind`` + ``data-sysname``
    so client JS can wire click handlers + live state refresh."""
    model = PanelModel(
        name="x", width=400, height=300,
        sensor_icons=[SensorIcon(sensor="LS1", x=10, y=20)],
        turnout_icons=[TurnoutIcon(turnout="LT5", x=50, y=60)],
    )
    svg = render_svg(model)
    assert 'viewBox="0 0 400 300"' in svg
    assert 'data-kind="sensor"' in svg
    assert 'data-sysname="LS1"' in svg
    assert 'data-kind="turnout"' in svg
    assert 'data-sysname="LT5"' in svg


def test_render_svg_uses_bean_state_for_colour() -> None:
    """When *bean_state* resolves a sensor to ACTIVE (2) the circle
    renders red; INACTIVE (4) renders green; unknown renders grey."""
    model = PanelModel(
        width=200, height=100,
        sensor_icons=[
            SensorIcon(sensor="LS_ACT", x=10, y=20),
            SensorIcon(sensor="LS_INACT", x=30, y=20),
            SensorIcon(sensor="LS_UNKNOWN", x=50, y=20),
        ],
    )
    def state(kind: str, name: str) -> int | None:
        assert kind == "sensor"
        if name == "LS_ACT":
            return 2
        if name == "LS_INACT":
            return 4
        return None
    svg = render_svg(model, state)
    # Three fills -- red for active, green for inactive, grey otherwise.
    assert svg.count('fill="#c33"') >= 1
    assert svg.count('fill="#3a3"') >= 1
    assert svg.count('fill="#aaa"') >= 1


def test_render_svg_draws_track_segments_between_anchor_points() -> None:
    """A track segment references two positionablepoint idents. The
    renderer must look them up by ident and draw a line between
    their coordinates. Missing endpoints are skipped (not crashed on)."""
    model = parse_panel_xml(_wrap(
        '<layouteditor name="x" panelwidth="400" panelheight="200">'
        '<positionablepoint ident="a" x="10" y="20" />'
        '<positionablepoint ident="b" x="200" y="20" />'
        '<tracksegment ident="t1" connect1name="a" connect2name="b" mainline="yes" />'
        '<tracksegment ident="dangling" connect1name="a" connect2name="no-such" />'
        '</layouteditor>'
    ))
    svg = render_svg(model)
    # Mainline segment rendered as a 4px stroke line. Coordinates
    # round-trip as floats from the XML attributes.
    assert 'x1="10.0"' in svg and 'x2="200.0"' in svg
    assert 'stroke-width="4"' in svg
    # The dangling segment with a missing endpoint doesn't crash.


def test_render_svg_escapes_user_strings() -> None:
    """Text labels and tooltips come from XML attribute values -- user
    content that could contain ``<`` must be escaped so the SVG stays
    well-formed and the panel can't inject arbitrary SVG elements."""
    from pmri_web.panel_svg import TextLabel  # noqa: PLC0415
    model = PanelModel(
        width=200, height=100,
        text_labels=[TextLabel(text="<bad> & \"wild\"", x=10, y=10)],
    )
    svg = render_svg(model)
    assert "&lt;bad&gt;" in svg
    assert "&amp;" in svg
    assert "<bad>" not in svg
