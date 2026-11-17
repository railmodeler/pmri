"""Unit tests for the panel_xml edit operations."""
from __future__ import annotations

import pytest

from pmri_core.persistence.panel_model import parse_panel_xml
from pmri_core.persistence.panel_xml import (
    PanelEditError,
    add_icon,
    add_layout_turnout,
    add_positionable_point,
    add_track_segment,
    load_panel_bytes,
    move_icon,
    move_positionable_point,
    remove_icon,
    remove_layout_turnout,
    remove_positionable_point,
    remove_track_segment,
    save_panel_bytes,
    set_layout_block,
    set_panel_size,
    set_turnout_bean,
    update_layout_turnout,
    update_track_segment,
)


def _empty_panel(name: str = "T") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<layout-config>'
        f'<LayoutEditor name="{name}" panelwidth="100" panelheight="100"/>'
        '</layout-config>'
    ).encode()


def test_add_track_segment_round_trips_through_panel_model() -> None:
    file = load_panel_bytes(_empty_panel())
    add_positionable_point(file, "T", ident="A", x=10, y=10)
    add_positionable_point(file, "T", ident="B", x=90, y=10)
    add_track_segment(
        file, "T",
        ident="S1", connect1_name="A", connect2_name="B",
        main_line=True, layout_block="IB1",
    )
    saved = save_panel_bytes(file)
    model = parse_panel_xml(saved)
    assert len(model.track_segments) == 1
    seg = model.track_segments[0]
    assert seg.ident == "S1"
    assert seg.main_line is True
    assert seg.layout_block == "IB1"
    assert len(model.positionable_points) == 2


def test_add_track_segment_rejects_duplicate_ident() -> None:
    file = load_panel_bytes(_empty_panel())
    add_track_segment(
        file, "T",
        ident="S1", connect1_name="A", connect2_name="B",
    )
    with pytest.raises(PanelEditError, match="already exists"):
        add_track_segment(
            file, "T",
            ident="S1", connect1_name="X", connect2_name="Y",
        )


def test_update_track_segment_mutates_fields_in_place() -> None:
    file = load_panel_bytes(_empty_panel())
    add_track_segment(
        file, "T",
        ident="S1", connect1_name="A", connect2_name="B",
    )
    update_track_segment(
        file, "T", "S1",
        connect1_name="A2", main_line=True, layout_block="IB1",
    )
    model = parse_panel_xml(save_panel_bytes(file))
    seg = model.track_segments[0]
    assert seg.connect1_name == "A2"
    assert seg.main_line is True
    assert seg.layout_block == "IB1"


def test_update_track_segment_clear_block_via_none() -> None:
    file = load_panel_bytes(_empty_panel())
    add_track_segment(
        file, "T",
        ident="S1", connect1_name="A", connect2_name="B",
        layout_block="IB1",
    )
    update_track_segment(file, "T", "S1", layout_block=None)
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.track_segments[0].layout_block is None


def test_remove_track_segment_drops_it() -> None:
    file = load_panel_bytes(_empty_panel())
    add_track_segment(
        file, "T",
        ident="S1", connect1_name="A", connect2_name="B",
    )
    remove_track_segment(file, "T", "S1")
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.track_segments == []


def test_remove_track_segment_unknown_ident_raises() -> None:
    file = load_panel_bytes(_empty_panel())
    with pytest.raises(PanelEditError, match="not found"):
        remove_track_segment(file, "T", "nope")


def test_add_layout_turnout_round_trips() -> None:
    file = load_panel_bytes(_empty_panel())
    add_layout_turnout(
        file, "T",
        ident="X1", turnout_name="IT1",
        xcen=50, ycen=50,
        xa=30, ya=50, xb=70, yb=50, xc=60, yc=70,
        layout_block="IB1",
    )
    model = parse_panel_xml(save_panel_bytes(file))
    assert len(model.layout_turnouts) == 1
    t = model.layout_turnouts[0]
    assert t.turnout == "IT1"
    assert t.layout_block == "IB1"
    assert (t.xcen, t.ycen) == (50, 50)


def test_set_turnout_bean_updates_turnoutname_attribute() -> None:
    file = load_panel_bytes(_empty_panel())
    add_layout_turnout(
        file, "T",
        ident="X1", turnout_name="",
        xcen=50, ycen=50,
        xa=30, ya=50, xb=70, yb=50, xc=60, yc=70,
    )
    set_turnout_bean(file, "T", "X1", "IT1")
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.layout_turnouts[0].turnout == "IT1"


def test_move_positionable_point_updates_xy() -> None:
    file = load_panel_bytes(_empty_panel())
    add_positionable_point(file, "T", ident="A", x=10, y=20)
    move_positionable_point(file, "T", "A", x=99, y=101)
    model = parse_panel_xml(save_panel_bytes(file))
    pp = model.positionable_points[0]
    assert (pp.x, pp.y) == (99, 101)


def test_remove_positionable_point_drops_it() -> None:
    file = load_panel_bytes(_empty_panel())
    add_positionable_point(file, "T", ident="A", x=10, y=20)
    remove_positionable_point(file, "T", "A")
    assert parse_panel_xml(save_panel_bytes(file)).positionable_points == []


def test_set_layout_block_flips_blockname_on_segment() -> None:
    file = load_panel_bytes(_empty_panel())
    add_track_segment(
        file, "T",
        ident="S1", connect1_name="A", connect2_name="B",
        layout_block="IB_old",
    )
    set_layout_block(file, "T", "tracksegment", "S1", "IB_new")
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.track_segments[0].layout_block == "IB_new"


def test_op_against_unknown_panel_raises() -> None:
    file = load_panel_bytes(_empty_panel())
    with pytest.raises(PanelEditError, match="no LayoutEditor"):
        add_track_segment(
            file, "Nope",
            ident="S1", connect1_name="A", connect2_name="B",
        )


def test_add_sensor_icon_round_trips() -> None:
    file = load_panel_bytes(_empty_panel())
    add_icon(file, "T", kind="sensoricon", key="IS1", x=50, y=60)
    model = parse_panel_xml(save_panel_bytes(file))
    assert len(model.sensor_icons) == 1
    s = model.sensor_icons[0]
    assert s.sensor == "IS1"
    assert (s.x, s.y) == (50, 60)


def test_add_icon_rejects_duplicate_key_per_kind() -> None:
    file = load_panel_bytes(_empty_panel())
    add_icon(file, "T", kind="turnouticon", key="IT1", x=10, y=10)
    with pytest.raises(PanelEditError, match="already exists"):
        add_icon(file, "T", kind="turnouticon", key="IT1", x=20, y=20)


def test_add_icon_allows_same_key_different_kinds() -> None:
    """Sensor icon + turnout icon sharing a name is weird but legal --
    they're separate beans. The uniqueness guard is scoped per (kind, key)."""
    file = load_panel_bytes(_empty_panel())
    add_icon(file, "T", kind="sensoricon", key="X1", x=1, y=1)
    add_icon(file, "T", kind="turnouticon", key="X1", x=2, y=2)
    model = parse_panel_xml(save_panel_bytes(file))
    assert len(model.sensor_icons) == 1
    assert len(model.turnout_icons) == 1


def test_move_icon_updates_xy() -> None:
    file = load_panel_bytes(_empty_panel())
    add_icon(file, "T", kind="memoryicon", key="IM1", x=10, y=10)
    move_icon(file, "T", "memoryicon", "IM1", x=55, y=77)
    model = parse_panel_xml(save_panel_bytes(file))
    m = model.memory_icons[0]
    assert (m.x, m.y) == (55, 77)


def test_remove_icon_drops_it() -> None:
    file = load_panel_bytes(_empty_panel())
    add_icon(file, "T", kind="sensoricon", key="IS1", x=10, y=10)
    remove_icon(file, "T", "sensoricon", "IS1")
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.sensor_icons == []


def test_add_icon_with_unknown_kind_errors() -> None:
    file = load_panel_bytes(_empty_panel())
    with pytest.raises(PanelEditError, match="unknown icon kind"):
        add_icon(file, "T", kind="widgeticon", key="X", x=0, y=0)


def test_update_layout_turnout_translates_all_coords() -> None:
    """Moving a turnout means shifting cen + all three legs by the same
    delta -- the server accepts the final coords from the client."""
    file = load_panel_bytes(_empty_panel())
    add_layout_turnout(
        file, "T",
        ident="X1", turnout_name="IT1",
        xcen=50, ycen=50,
        xa=30, ya=50, xb=70, yb=50, xc=60, yc=70,
    )
    update_layout_turnout(
        file, "T", "X1",
        xcen=150, ycen=150,
        xa=130, ya=150, xb=170, yb=150, xc=160, yc=170,
    )
    model = parse_panel_xml(save_panel_bytes(file))
    t = model.layout_turnouts[0]
    assert (t.xcen, t.ycen) == (150, 150)
    assert (t.xa, t.ya) == (130, 150)


def test_update_layout_turnout_partial_fields() -> None:
    file = load_panel_bytes(_empty_panel())
    add_layout_turnout(
        file, "T",
        ident="X1", turnout_name="IT1",
        xcen=50, ycen=50,
        xa=30, ya=50, xb=70, yb=50, xc=60, yc=70,
    )
    update_layout_turnout(file, "T", "X1", layout_block="IB_new")
    model = parse_panel_xml(save_panel_bytes(file))
    t = model.layout_turnouts[0]
    assert t.layout_block == "IB_new"
    assert (t.xcen, t.ycen) == (50, 50)   # geometry unchanged


def test_set_panel_size_updates_viewport_attrs() -> None:
    file = load_panel_bytes(_empty_panel())
    set_panel_size(file, "T", width=1600, height=1200)
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.width == 1600
    assert model.height == 1200


def test_set_panel_size_clamps_to_sane_range() -> None:
    file = load_panel_bytes(_empty_panel())
    set_panel_size(file, "T", width=5, height=99999)
    model = parse_panel_xml(save_panel_bytes(file))
    assert model.width == 100
    assert model.height == 16384


def test_update_layout_turnout_unknown_ident_raises() -> None:
    file = load_panel_bytes(_empty_panel())
    with pytest.raises(PanelEditError, match="not found"):
        update_layout_turnout(file, "T", "nope", xcen=0, ycen=0)


def test_signalmasticon_round_trips() -> None:
    file = load_panel_bytes(_empty_panel())
    add_icon(file, "T", kind="signalmasticon", key="IF$shsm:AAR-1946:SL-1-high:red",
             x=100, y=50)
    model = parse_panel_xml(save_panel_bytes(file))
    assert len(model.signal_icons) == 1
    sig = model.signal_icons[0]
    assert sig.kind == "mast"
    assert sig.x == 100


def test_remove_layout_turnout_drops_it() -> None:
    file = load_panel_bytes(_empty_panel())
    add_layout_turnout(
        file, "T",
        ident="X1", turnout_name="IT1",
        xcen=50, ycen=50,
        xa=30, ya=50, xb=70, yb=50, xc=60, yc=70,
    )
    remove_layout_turnout(file, "T", "X1")
    assert parse_panel_xml(save_panel_bytes(file)).layout_turnouts == []
