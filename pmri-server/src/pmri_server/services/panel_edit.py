"""``PanelEditService`` -- JSON topic ``panel-edit``.

Mutating companion to :class:`PanelService`. Each ``post`` carries an
``op`` plus the fields that op needs; the service loads the target
panel's XML, applies the edit through :mod:`pmri_core.persistence.panel_xml`,
and writes it back with a rolling backup so nothing we do can
unrecoverably corrupt a user's panel file.

Supported ops (round-up; icon ops land in a follow-on):

* ``addSegment`` / ``removeSegment`` / ``updateSegment``
* ``addTurnout`` / ``removeTurnout`` / ``setTurnoutBean``
* ``addPoint`` / ``movePoint`` / ``removePoint``
* ``setLayoutBlock``

Wire shape::

    { "type": "panel-edit",
      "method": "post",
      "data": { "op": "addSegment",
                "panel": "MyPanel",
                "ident": "T42",
                ... } }

On success the service returns the op summary so a client that
rebroadcasts the change (Task 6 of the panel-editor plan) can echo it
to peer viewers without re-reading the file.

Every mutating op is ``OPERATOR``-gated; reads are not supported (the
read side lives on :class:`PanelService`).
"""
from __future__ import annotations

import pathlib
import shutil
from typing import Any

from pmri_core.persistence.panel_xml import (
    PanelEditError,
    PanelFile,
    add_icon,
    add_layout_turnout,
    add_positionable_point,
    add_track_segment,
    load_panel,
    move_icon,
    move_positionable_point,
    remove_icon,
    remove_layout_turnout,
    remove_positionable_point,
    remove_track_segment,
    save_panel,
    set_layout_block,
    set_panel_size,
    set_turnout_bean,
    update_layout_turnout,
    update_track_segment,
)
from pmri_server.service import JsonService, JsonServiceError
from pmri_server.services.panel import _panels_dir


# JMRI's URL / name convention from PanelService: ``<Type>/<basename>``.
# The edit service keys off the basename; the type comes from the file
# contents when we load it, not from the name.
def _resolve_panel_file(ctx: Any, panel_ref: str) -> pathlib.Path:
    """Resolve ``LayoutEditor/MyPanel`` (or just ``MyPanel``) to a file
    under the active profile's panels dir."""
    stem = panel_ref.split("/", 1)[-1]
    panels = _panels_dir(ctx)
    if panels is None:
        raise JsonServiceError(500, "no panels directory configured")
    candidate = panels / f"{stem}.xml"
    if not candidate.is_file():
        raise JsonServiceError(404, f"no panel named {panel_ref!r}")
    return candidate


def _rolling_backup(path: pathlib.Path) -> None:
    """Keep up to one ``.bak`` + two ``.bakN`` siblings before we
    overwrite *path*. Matches the JMRI backup habit so operators can
    recover when a panel-edit goes wrong."""
    if not path.is_file():
        return
    bak = path.with_suffix(path.suffix + ".bak")
    bak2 = path.with_suffix(path.suffix + ".bak2")
    if bak.is_file():
        try:
            if bak2.is_file():
                bak2.unlink()
            bak.rename(bak2)
        except OSError:
            pass   # best-effort; the main save must still succeed
    try:
        shutil.copy2(path, bak)
    except OSError:
        pass


def _require(data: dict[str, Any] | None, *keys: str) -> dict[str, Any]:
    """Return *data* after validating it's a dict carrying each of
    *keys*. Raise 400 on failure."""
    if not isinstance(data, dict):
        raise JsonServiceError(400, "panel-edit requires a JSON object body")
    missing = [k for k in keys if k not in data]
    if missing:
        raise JsonServiceError(
            400, f"panel-edit missing field(s): {', '.join(missing)}",
        )
    return data


class PanelEditService(JsonService):
    """Mutate a panel XML file via one of the supported ops."""

    topic = "panel-edit"

    async def on_post(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = _require(data, "op", "panel")
        op = str(data["op"])
        panel_ref = str(data["panel"])
        path = _resolve_panel_file(self.ctx, panel_ref)
        file = load_panel(path)
        # Panel-name inside the LayoutEditor section is the trailing
        # segment of the panel ref (e.g. "LayoutEditor/Main" -> "Main").
        panel_name = panel_ref.split("/", 1)[-1]

        try:
            result = _dispatch_op(file, panel_name, op, data)
        except PanelEditError as e:
            raise JsonServiceError(409, str(e)) from None

        _rolling_backup(path)
        save_panel(path, file)
        return {
            "op": op,
            "panel": panel_ref,
            "ok": True,
            **result,
        }


def _dispatch_op(
    file: PanelFile, panel_name: str, op: str, data: dict[str, Any],
) -> dict[str, Any]:
    """Call the matching panel_xml.py helper. Return a summary dict
    with just the fields a client needs to echo the change to peers."""
    if op == "addSegment":
        _require(data, "ident", "connect1_name", "connect2_name")
        add_track_segment(
            file, panel_name,
            ident=data["ident"],
            connect1_name=data["connect1_name"],
            connect2_name=data["connect2_name"],
            type1=data.get("type1", 1),
            type2=data.get("type2", 1),
            main_line=bool(data.get("main_line", False)),
            dashed=bool(data.get("dashed", False)),
            layout_block=data.get("layout_block"),
        )
        return {"ident": data["ident"]}

    if op == "removeSegment":
        _require(data, "ident")
        remove_track_segment(file, panel_name, data["ident"])
        return {"ident": data["ident"]}

    if op == "updateSegment":
        _require(data, "ident")
        fields = {
            k: data[k] for k in data
            if k in {"connect1_name", "connect2_name", "type1", "type2",
                     "main_line", "dashed", "layout_block"}
        }
        update_track_segment(file, panel_name, data["ident"], **fields)
        return {"ident": data["ident"], "fields": fields}

    if op == "addTurnout":
        _require(data, "ident", "turnout_name", "xcen", "ycen",
                 "xa", "ya", "xb", "yb", "xc", "yc")
        add_layout_turnout(
            file, panel_name,
            ident=data["ident"], turnout_name=data["turnout_name"],
            xcen=float(data["xcen"]), ycen=float(data["ycen"]),
            xa=float(data["xa"]), ya=float(data["ya"]),
            xb=float(data["xb"]), yb=float(data["yb"]),
            xc=float(data["xc"]), yc=float(data["yc"]),
            layout_block=data.get("layout_block"),
            turnout_type=int(data.get("turnout_type", 1)),
        )
        return {"ident": data["ident"]}

    if op == "removeTurnout":
        _require(data, "ident")
        remove_layout_turnout(file, panel_name, data["ident"])
        return {"ident": data["ident"]}

    if op == "setTurnoutBean":
        _require(data, "ident", "turnout_name")
        set_turnout_bean(file, panel_name, data["ident"], data["turnout_name"])
        return {"ident": data["ident"], "turnout_name": data["turnout_name"]}

    if op == "updateTurnout":
        _require(data, "ident")
        allowed = {"xcen", "ycen", "xa", "ya", "xb", "yb", "xc", "yc",
                   "turnout_type", "layout_block"}
        fields = {
            k: (float(data[k]) if k.startswith(("x", "y")) else data[k])
            for k in data if k in allowed
        }
        update_layout_turnout(file, panel_name, data["ident"], **fields)
        return {"ident": data["ident"], "fields": fields}

    if op == "addPoint":
        _require(data, "ident", "x", "y")
        kind = data.get("kind", "anchor")
        if kind not in ("anchor", "end_bumper", "edge_connector"):
            raise JsonServiceError(400, f"invalid point kind {kind!r}")
        add_positionable_point(
            file, panel_name,
            ident=data["ident"],
            x=float(data["x"]), y=float(data["y"]),
            kind=kind,
        )
        return {"ident": data["ident"]}

    if op == "movePoint":
        _require(data, "ident", "x", "y")
        move_positionable_point(
            file, panel_name,
            data["ident"], x=float(data["x"]), y=float(data["y"]),
        )
        return {"ident": data["ident"], "x": float(data["x"]),
                "y": float(data["y"])}

    if op == "removePoint":
        _require(data, "ident")
        remove_positionable_point(file, panel_name, data["ident"])
        return {"ident": data["ident"]}

    if op == "setLayoutBlock":
        _require(data, "ident", "element_tag")
        et = data["element_tag"]
        if et not in ("tracksegment", "layoutturnout"):
            raise JsonServiceError(400, f"invalid element_tag {et!r}")
        set_layout_block(
            file, panel_name, et, data["ident"],
            data.get("layout_block"),
        )
        return {"ident": data["ident"], "element_tag": et,
                "layout_block": data.get("layout_block")}

    if op == "addIcon":
        _require(data, "kind", "key", "x", "y")
        add_icon(
            file, panel_name,
            kind=data["kind"], key=data["key"],
            x=float(data["x"]), y=float(data["y"]),
            rotate=int(data.get("rotate", 0)),
        )
        return {"kind": data["kind"], "key": data["key"]}

    if op == "removeIcon":
        _require(data, "kind", "key")
        remove_icon(file, panel_name, data["kind"], data["key"])
        return {"kind": data["kind"], "key": data["key"]}

    if op == "setPanelSize":
        _require(data, "width", "height")
        set_panel_size(
            file, panel_name,
            width=int(data["width"]), height=int(data["height"]),
        )
        return {"width": int(data["width"]), "height": int(data["height"])}

    if op == "moveIcon":
        _require(data, "kind", "key", "x", "y")
        move_icon(
            file, panel_name, data["kind"], data["key"],
            x=float(data["x"]), y=float(data["y"]),
        )
        return {
            "kind": data["kind"], "key": data["key"],
            "x": float(data["x"]), "y": float(data["y"]),
        }

    raise JsonServiceError(400, f"unknown panel-edit op {op!r}")
