"""Panel / Layout XML -- envelope-level round-trip + edit operations.

JMRI panel files (``layout-config`` / ``layout-5-5-5.xsd``) hold an editor's
entire visual layout: every positionable icon, signal, sensor, layout-block
graph node, train icon, memory icon, route table, Logix etc. We ship:

* envelope-level load/save (:func:`load_panel_bytes`, :func:`save_panel_bytes`)
  that preserves every interior element verbatim;
* a small set of **edit operations** (``add_track_segment`` etc.) that
  mutate the LayoutEditor section in place so the Panel Editor service
  can add/remove/update track plan primitives without the caller having
  to hand-craft lxml.

Edit operations are intentionally terse: they take only the fields JMRI
emits on the corresponding element, each as a kwarg, and produce the
JMRI shape JMRI Java knows how to round-trip. A caller that needs a
feature we haven't added yet mutates the raw ``panel.root`` tree
directly.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Literal

from lxml import etree

# ``HitPointType`` codes we accept for ``type1`` / ``type2`` on a
# ``<tracksegment>``. Match ``pmri_core.persistence.panel_model`` so
# callers can use the same constants.
_HIT_NAME: dict[int, str] = {
    1: "POS_POINT",
    2: "TURNOUT_A",
    3: "TURNOUT_B",
    4: "TURNOUT_C",
    5: "TURNOUT_D",
    6: "LEVEL_XING_A",
    7: "LEVEL_XING_B",
    8: "LEVEL_XING_C",
    9: "LEVEL_XING_D",
    21: "SLIP_A",
    22: "SLIP_B",
    23: "SLIP_C",
    24: "SLIP_D",
}


@dataclass
class PanelFile:
    """A panel / layout XML file with preserved structure.

    Attributes:
        root: top-level lxml ``<layout-config>`` element (or whatever the
            file's root happens to be -- we don't reject unknown roots).
        doctype: original DOCTYPE declaration string, preserved across save.
    """

    root: etree._Element
    doctype: str | None = None

    @property
    def root_tag(self) -> str:
        return str(self.root.tag)

    @property
    def pmri_version(self) -> str:
        """JMRI version that wrote this file, from the root attributes."""
        return self.root.get("jmriversion") or self.root.get("jmriVersion") or ""

    def panels(self) -> list[etree._Element]:
        """List of top-level ``<panel>`` elements (PanelPro-style files)."""
        return self.root.findall("panel")

    def layout_editors(self) -> list[etree._Element]:
        """List of ``<LayoutEditor>`` elements (LayoutEditor-style files)."""
        return self.root.findall("LayoutEditor")

    def named_beans(self, manager_class: str) -> list[etree._Element]:
        """All ``<*Manager>`` child elements matching *manager_class*.

        Panel files persist managed-bean state inside the panel-config so
        ``turnoutmanager``, ``sensormanager``, etc. show up at the root.
        """
        return self.root.findall(manager_class)


def load_panel_bytes(data: bytes) -> PanelFile:
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    root = etree.fromstring(data, parser=parser)
    tree = root.getroottree()
    # getattr shields us from lxml stub gaps around DocInfo.doctype.
    doctype: str | None = getattr(tree.docinfo, "doctype", None) or None
    return PanelFile(root=root, doctype=doctype)


def load_panel(path: str | pathlib.Path) -> PanelFile:
    return load_panel_bytes(pathlib.Path(path).read_bytes())


def save_panel_bytes(file: PanelFile) -> bytes:
    if file.doctype:
        return etree.tostring(
            file.root,
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=False,
            standalone=False,
            doctype=file.doctype,
        )
    return etree.tostring(
        file.root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=False,
        standalone=False,
    )


def save_panel(path: str | pathlib.Path, file: PanelFile) -> None:
    pathlib.Path(path).write_bytes(save_panel_bytes(file))


# ----- Edit operations on the LayoutEditor section --------------------------

# JMRI panel files can hold multiple ``<LayoutEditor>`` siblings (one per
# panel on the layout). Every edit operation selects one by its
# ``name="..."`` attribute. ``_find_layout_editor`` is the single lookup
# shared across operations -- it raises :class:`PanelEditError` so the
# caller always gets a typed failure rather than a silent None.


class PanelEditError(Exception):
    """Raised when a panel-edit operation can't complete (unknown panel,
    missing element, etc.)."""


def _find_layout_editor(
    file: PanelFile, panel_name: str,
) -> etree._Element:
    """Locate the ``<LayoutEditor name="panel_name">`` element."""
    # JMRI emits ``<LayoutEditor>`` as a child of ``<layout-config>``; older
    # files use lowercase ``<layouteditor>``. Accept both.
    for tag in ("LayoutEditor", "layouteditor"):
        for elem in file.root.findall(tag):
            if elem.get("name") == panel_name:
                return elem
    raise PanelEditError(f"no LayoutEditor named {panel_name!r}")


def set_panel_size(
    file: PanelFile, panel_name: str, *, width: int, height: int,
) -> etree._Element:
    """Update the panel's ``panelwidth`` / ``panelheight`` attributes.

    These drive the drawable viewport in both the JMRI LayoutEditor
    window and our Canvas editor. Clamped to a sane range -- a 0-sized
    panel is a trap, and widths past 16k pixels tend to be a typo.
    """
    panel = _find_layout_editor(file, panel_name)
    w = max(100, min(int(width), 16384))
    h = max(100, min(int(height), 16384))
    panel.set("panelwidth", str(w))
    panel.set("panelheight", str(h))
    return panel


def _append_blockname(elem: etree._Element, block_name: str | None) -> None:
    """Attach a ``<blockname>`` child to *elem* when *block_name* is set.
    JMRI stores layout-block assignments as a child element, not an
    attribute, on both ``<tracksegment>`` and ``<layoutturnout>``."""
    if block_name:
        etree.SubElement(elem, "blockname").text = block_name


def _find_by_ident(
    parent: etree._Element, tag: str, ident: str,
) -> etree._Element | None:
    """Return the first ``<tag ident="...">`` child of *parent*,
    or ``None`` if no match."""
    for child in parent.findall(tag):
        if child.get("ident") == ident:
            return child
    return None


def _hit_name(hit: int | str) -> str:
    """Normalize a HitPointType value to the string JMRI writes."""
    if isinstance(hit, int):
        return _HIT_NAME.get(hit, "POS_POINT")
    return hit


# ----- track-segment ops ----------------------------------------------------


def add_track_segment(
    file: PanelFile,
    panel_name: str,
    *,
    ident: str,
    connect1_name: str,
    connect2_name: str,
    type1: int | str = 1,
    type2: int | str = 1,
    main_line: bool = False,
    dashed: bool = False,
    layout_block: str | None = None,
) -> etree._Element:
    """Append a new ``<tracksegment>`` to the named LayoutEditor."""
    panel = _find_layout_editor(file, panel_name)
    if _find_by_ident(panel, "tracksegment", ident) is not None:
        raise PanelEditError(f"tracksegment {ident!r} already exists")
    attrs = {
        "ident": ident,
        "connect1name": connect1_name,
        "type1": _hit_name(type1),
        "connect2name": connect2_name,
        "type2": _hit_name(type2),
        "mainline": "yes" if main_line else "no",
        "dashed": "yes" if dashed else "no",
    }
    seg = etree.SubElement(panel, "tracksegment", attrib=attrs)
    _append_blockname(seg, layout_block)
    return seg


def remove_track_segment(
    file: PanelFile, panel_name: str, ident: str,
) -> None:
    """Remove the ``<tracksegment ident=...>`` from the named panel."""
    panel = _find_layout_editor(file, panel_name)
    seg = _find_by_ident(panel, "tracksegment", ident)
    if seg is None:
        raise PanelEditError(f"tracksegment {ident!r} not found")
    panel.remove(seg)


def update_track_segment(
    file: PanelFile, panel_name: str, ident: str,
    **fields: Any,
) -> etree._Element:
    """Mutate a ``<tracksegment>``'s attributes / block assignment.

    Accepts the same keyword names :func:`add_track_segment` takes.
    ``layout_block=None`` removes the ``<blockname>`` child; passing
    a string replaces it. Omitted fields are left untouched.
    """
    panel = _find_layout_editor(file, panel_name)
    seg = _find_by_ident(panel, "tracksegment", ident)
    if seg is None:
        raise PanelEditError(f"tracksegment {ident!r} not found")
    if "connect1_name" in fields:
        seg.set("connect1name", fields["connect1_name"])
    if "connect2_name" in fields:
        seg.set("connect2name", fields["connect2_name"])
    if "type1" in fields:
        seg.set("type1", _hit_name(fields["type1"]))
    if "type2" in fields:
        seg.set("type2", _hit_name(fields["type2"]))
    if "main_line" in fields:
        seg.set("mainline", "yes" if fields["main_line"] else "no")
    if "dashed" in fields:
        seg.set("dashed", "yes" if fields["dashed"] else "no")
    if "layout_block" in fields:
        existing = seg.find("blockname")
        if existing is not None:
            seg.remove(existing)
        _append_blockname(seg, fields["layout_block"])
    return seg


# ----- positionable-point ops ------------------------------------------------


def add_positionable_point(
    file: PanelFile, panel_name: str,
    *, ident: str, x: float, y: float,
    kind: Literal["anchor", "end_bumper", "edge_connector"] = "anchor",
) -> etree._Element:
    """Append a new ``<positionablepoint>`` to the named panel."""
    panel = _find_layout_editor(file, panel_name)
    if _find_by_ident(panel, "positionablepoint", ident) is not None:
        raise PanelEditError(f"positionablepoint {ident!r} already exists")
    kind_int = {"anchor": "1", "end_bumper": "2", "edge_connector": "3"}[kind]
    attrs = {"ident": ident, "type": kind_int, "x": str(x), "y": str(y)}
    return etree.SubElement(panel, "positionablepoint", attrib=attrs)


def move_positionable_point(
    file: PanelFile, panel_name: str, ident: str, *, x: float, y: float,
) -> etree._Element:
    panel = _find_layout_editor(file, panel_name)
    pp = _find_by_ident(panel, "positionablepoint", ident)
    if pp is None:
        raise PanelEditError(f"positionablepoint {ident!r} not found")
    pp.set("x", str(x))
    pp.set("y", str(y))
    return pp


def remove_positionable_point(
    file: PanelFile, panel_name: str, ident: str,
) -> None:
    panel = _find_layout_editor(file, panel_name)
    pp = _find_by_ident(panel, "positionablepoint", ident)
    if pp is None:
        raise PanelEditError(f"positionablepoint {ident!r} not found")
    panel.remove(pp)


# ----- layout-turnout ops ---------------------------------------------------


def add_layout_turnout(
    file: PanelFile, panel_name: str,
    *,
    ident: str,
    turnout_name: str,
    xcen: float, ycen: float,
    xa: float, ya: float,
    xb: float, yb: float,
    xc: float, yc: float,
    layout_block: str | None = None,
    turnout_type: int = 1,   # 1=RH, 2=LH, 3=WYE, 4=DoubleXover, etc.
) -> etree._Element:
    """Append a new ``<layoutturnout>`` to the named panel."""
    panel = _find_layout_editor(file, panel_name)
    if _find_by_ident(panel, "layoutturnout", ident) is not None:
        raise PanelEditError(f"layoutturnout {ident!r} already exists")
    attrs = {
        "ident": ident,
        "type": str(turnout_type),
        "turnoutname": turnout_name,
        "xcen": str(xcen), "ycen": str(ycen),
        "xa": str(xa), "ya": str(ya),
        "xb": str(xb), "yb": str(yb),
        "xc": str(xc), "yc": str(yc),
    }
    turnout = etree.SubElement(panel, "layoutturnout", attrib=attrs)
    _append_blockname(turnout, layout_block)
    return turnout


def remove_layout_turnout(
    file: PanelFile, panel_name: str, ident: str,
) -> None:
    panel = _find_layout_editor(file, panel_name)
    t = _find_by_ident(panel, "layoutturnout", ident)
    if t is None:
        raise PanelEditError(f"layoutturnout {ident!r} not found")
    panel.remove(t)


_TURNOUT_COORD_ATTRS = ("xcen", "ycen", "xa", "ya", "xb", "yb", "xc", "yc")


def update_layout_turnout(
    file: PanelFile, panel_name: str, ident: str,
    **fields: Any,
) -> etree._Element:
    """Mutate any subset of a layout turnout's geometry + metadata.

    Accepts ``xcen`` / ``ycen`` / ``xa`` / ``ya`` / ``xb`` / ``yb`` /
    ``xc`` / ``yc`` as floats, plus ``turnout_type`` as int and
    ``layout_block`` as ``str | None``. Clients drive translation,
    rotation, and flipping by computing the new coordinates and
    posting a single update -- the server treats the panel file as
    data and doesn't impose a transform model on the client.
    """
    panel = _find_layout_editor(file, panel_name)
    t = _find_by_ident(panel, "layoutturnout", ident)
    if t is None:
        raise PanelEditError(f"layoutturnout {ident!r} not found")
    for attr in _TURNOUT_COORD_ATTRS:
        if attr in fields:
            t.set(attr, str(fields[attr]))
    if "turnout_type" in fields:
        t.set("type", str(int(fields["turnout_type"])))
    if "layout_block" in fields:
        existing = t.find("blockname")
        if existing is not None:
            t.remove(existing)
        _append_blockname(t, fields["layout_block"])
    return t


def set_turnout_bean(
    file: PanelFile, panel_name: str, ident: str, turnout_name: str,
) -> etree._Element:
    """Assign (or clear with ``""``) the physical turnout bean for a
    ``<layoutturnout>``."""
    panel = _find_layout_editor(file, panel_name)
    t = _find_by_ident(panel, "layoutturnout", ident)
    if t is None:
        raise PanelEditError(f"layoutturnout {ident!r} not found")
    t.set("turnoutname", turnout_name)
    return t


def set_layout_block(
    file: PanelFile, panel_name: str, element_tag: str, ident: str,
    layout_block: str | None,
) -> etree._Element:
    """Reassign the ``<blockname>`` child on either a ``<tracksegment>``
    or ``<layoutturnout>``. Pass ``layout_block=None`` to clear."""
    panel = _find_layout_editor(file, panel_name)
    elem = _find_by_ident(panel, element_tag, ident)
    if elem is None:
        raise PanelEditError(f"{element_tag} {ident!r} not found")
    existing = elem.find("blockname")
    if existing is not None:
        elem.remove(existing)
    _append_blockname(elem, layout_block)
    return elem


# ----- icon ops ------------------------------------------------------------

# Icons (sensoricon, turnouticon, signal{mast,head}icon, memoryicon) don't
# carry an ``ident`` attribute -- JMRI identifies them by the bean they
# reference. Each kind has one "key attribute" that holds the bean's
# system name; we use (kind, key_value) to uniquely locate an icon on a
# panel. Callers mutate by bean system name, not by ordinal position.

_ICON_KEY_ATTR: dict[str, str] = {
    "sensoricon": "sensor",
    "turnouticon": "turnout",
    "signalmasticon": "signalmast",
    "signalheadicon": "signalhead",
    "memoryicon": "memory",
}


def _find_icon(
    panel: etree._Element, kind: str, key: str,
) -> etree._Element | None:
    """Locate an icon by its bean-system-name key."""
    key_attr = _ICON_KEY_ATTR.get(kind)
    if key_attr is None:
        return None
    for child in panel.findall(kind):
        if child.get(key_attr) == key:
            return child
    return None


def add_icon(
    file: PanelFile, panel_name: str,
    *, kind: str, key: str, x: float, y: float,
    rotate: int = 0,
) -> etree._Element:
    """Append a new icon (sensor / turnout / signal / memory) to a panel.

    *kind* names the XML element (``sensoricon`` etc.); *key* is the
    bean's system name. Refuses duplicates -- a panel shouldn't carry
    two icons for the same bean.
    """
    key_attr = _ICON_KEY_ATTR.get(kind)
    if key_attr is None:
        raise PanelEditError(f"unknown icon kind {kind!r}")
    panel = _find_layout_editor(file, panel_name)
    if _find_icon(panel, kind, key) is not None:
        raise PanelEditError(f"{kind} for {key!r} already exists on panel")
    attrs = {key_attr: key, "x": str(x), "y": str(y)}
    if rotate and kind in ("sensoricon", "turnouticon"):
        attrs["rotate"] = str(rotate)
    return etree.SubElement(panel, kind, attrib=attrs)


def remove_icon(
    file: PanelFile, panel_name: str, kind: str, key: str,
) -> None:
    panel = _find_layout_editor(file, panel_name)
    icon = _find_icon(panel, kind, key)
    if icon is None:
        raise PanelEditError(f"{kind} for {key!r} not found on panel")
    panel.remove(icon)


def move_icon(
    file: PanelFile, panel_name: str, kind: str, key: str,
    *, x: float, y: float,
) -> etree._Element:
    panel = _find_layout_editor(file, panel_name)
    icon = _find_icon(panel, kind, key)
    if icon is None:
        raise PanelEditError(f"{kind} for {key!r} not found on panel")
    icon.set("x", str(x))
    icon.set("y", str(y))
    return icon
