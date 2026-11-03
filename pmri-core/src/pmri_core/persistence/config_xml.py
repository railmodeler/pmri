"""``<layout-config>`` XML -- typed sections + opaque pass-through.

The JMRI 5.x bean-table file (``PanelPro.xml`` on real installs;
``Configuration.xml`` on the older shape) holds every persisted named
bean plus panels, blocks, LogixNG, etc. Modelling every section is
Stage 11 work. For alpha we ship typed load/save for the sections
operators most commonly edit through the web UI:

* ``<turnouts>`` -- system name, user name, feedback, inverted, automate, comment
* ``<sensors>`` -- system name, user name, inverted, comment
* ``<lights>`` -- system name, user name, comment

Every other top-level section (``<signalheads>``, ``<memories>``,
``<routes>``, ``<blocks>``, ``<paneleditor>``, LogixNG, etc.) is
preserved verbatim as raw lxml.

## XML shapes

JMRI 5.x writes bean rows with ``<systemName>`` (and optional
``<userName>`` / ``<comment>``) as **child elements**, with the
remaining bean attributes on the parent::

    <turnout feedback="MONITORING" inverted="false" automate="Off">
      <systemName>NT40</systemName>
      <userName>Durango Staging turnout 1</userName>
      <comment>Optional notes</comment>
    </turnout>

Older JMRI installs (pre-5.x) wrote everything as parent attributes::

    <turnout systemName="NT40" userName="..." feedback="MONITORING"/>

This module **reads both forms** but **always writes the 5.x form** for
parity with current JMRI installs. Unknown attributes on each bean row
are preserved verbatim through ``TurnoutRow.extra_attribs`` and friends.
"""
from __future__ import annotations

import pathlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from lxml import etree

# JMRI's manager-class string format -- used as the ``class`` attribute on
# each `<turnouts>` / `<sensors>` etc. element. JMRI dispatches load/save
# handlers based on this string, and its ``layout-*.xsd`` schema requires
# it to match ``[a-zA-Z0-9.]*[a-z]`` (last char lowercase). The historical
# abstract names like ``AbstractTurnoutManagerConfigXML`` end in uppercase
# ``L`` and are schema-invalid -- files with them fail to open in JMRI Java.

# Default fallback classes for prefixes we don't know how to map. These
# are the Internal (concrete) config-XML classes, both real and
# schema-valid, so a file we emit stays loadable in JMRI Java even if we
# don't recognise the connection driver.
_DEFAULT_TURNOUT_MGR_CLASS = "jmri.jmrix.internal.configurexml.InternalTurnoutManagerXml"
_DEFAULT_SENSOR_MGR_CLASS  = "jmri.jmrix.internal.configurexml.InternalSensorManagerXml"
_DEFAULT_LIGHT_MGR_CLASS   = "jmri.jmrix.internal.configurexml.InternalLightManagerXml"

# Map JMRI system-prefix single-letter code -> concrete config-XML class
# for each bean type. Values come from grepping JMRI Java's
# ``jmri/jmrix/*/configurexml/*ManagerXml.java`` files. Extend as new
# drivers get support.
_TURNOUT_MGR_CLASS_BY_PREFIX = {
    "I": "jmri.jmrix.internal.configurexml.InternalTurnoutManagerXml",
    "N": "jmri.jmrix.nce.configurexml.NceTurnoutManagerXml",
    "L": "jmri.jmrix.loconet.configurexml.LnTurnoutManagerXml",
    "M": "jmri.jmrix.openlcb.configurexml.OlcbTurnoutManagerXml",
    "D": "jmri.jmrix.dccpp.configurexml.DCCppTurnoutManagerXml",
}
_SENSOR_MGR_CLASS_BY_PREFIX = {
    "I": "jmri.jmrix.internal.configurexml.InternalSensorManagerXml",
    "N": "jmri.jmrix.nce.configurexml.NceSensorManagerXml",
    "L": "jmri.jmrix.loconet.configurexml.LnSensorManagerXml",
    "M": "jmri.jmrix.openlcb.configurexml.OlcbSensorManagerXml",
    "D": "jmri.jmrix.dccpp.configurexml.DCCppSensorManagerXml",
}
_LIGHT_MGR_CLASS_BY_PREFIX = {
    "I": "jmri.jmrix.internal.configurexml.InternalLightManagerXml",
    "N": "jmri.jmrix.nce.configurexml.NceLightManagerXml",
    "L": "jmri.jmrix.loconet.configurexml.LnLightManagerXml",
    "M": "jmri.jmrix.openlcb.configurexml.OlcbLightManagerXml",
    "D": "jmri.jmrix.dccpp.configurexml.DCCppLightManagerXml",
}


def turnout_mgr_class_for_prefix(prefix: str) -> str:
    """Concrete ``AbstractTurnoutManagerConfigXML`` subclass name for *prefix*.

    Falls back to the Internal class if we don't recognise the prefix --
    that's still a real, schema-valid class name JMRI Java can load.
    """
    return _TURNOUT_MGR_CLASS_BY_PREFIX.get(prefix, _DEFAULT_TURNOUT_MGR_CLASS)


def sensor_mgr_class_for_prefix(prefix: str) -> str:
    return _SENSOR_MGR_CLASS_BY_PREFIX.get(prefix, _DEFAULT_SENSOR_MGR_CLASS)


def light_mgr_class_for_prefix(prefix: str) -> str:
    return _LIGHT_MGR_CLASS_BY_PREFIX.get(prefix, _DEFAULT_LIGHT_MGR_CLASS)

# Attributes we model on each bean row -- read from XML, written explicitly.
# Anything else (e.g. JMRI's ``locked`` / ``lockMode`` / ``decoder`` on
# turnouts) is preserved verbatim in ``extra_attribs``.
_TURNOUT_KNOWN_ATTRS = {"systemName", "userName", "feedback", "inverted", "automate"}
_SENSOR_KNOWN_ATTRS  = {"systemName", "userName", "inverted"}
_LIGHT_KNOWN_ATTRS   = {"systemName", "userName"}


# ----- typed bean rows ------------------------------------------------------


@dataclass(slots=True)
class TurnoutRow:
    """One ``<turnout>`` row inside a ``<turnouts>`` section."""

    system_name: str
    user_name: str | None = None
    feedback: str | None = None   # JMRI feedback mode name, e.g. "MONITORING"
    inverted: bool = False
    automate: str | None = None   # JMRI turnout-operation name, e.g. "Default"
    comment: str | None = None
    # Unmodeled parent attributes (locked, lockMode, decoder, etc.) -- kept
    # verbatim so JMRI-authored details round-trip without us understanding
    # them.
    extra_attribs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SensorRow:
    """One ``<sensor>`` row inside a ``<sensors>`` section."""

    system_name: str
    user_name: str | None = None
    inverted: bool = False
    comment: str | None = None
    extra_attribs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class LightRow:
    """One ``<light>`` row inside a ``<lights>`` section."""

    system_name: str
    user_name: str | None = None
    comment: str | None = None
    extra_attribs: dict[str, str] = field(default_factory=dict)


# ----- per-section containers ----------------------------------------------


@dataclass(slots=True)
class TurnoutSection:
    """A ``<turnouts class="...">`` block. JMRI groups by connection."""

    manager_class: str = _DEFAULT_TURNOUT_MGR_CLASS
    rows: list[TurnoutRow] = field(default_factory=list)
    # JMRI emits a ``<operations>`` child + per-direction defaults that we
    # don't model -- preserve them verbatim per-section so they round-trip.
    preserved_children: list[etree._Element] = field(default_factory=list)


@dataclass(slots=True)
class SensorSection:
    manager_class: str = _DEFAULT_SENSOR_MGR_CLASS
    rows: list[SensorRow] = field(default_factory=list)
    preserved_children: list[etree._Element] = field(default_factory=list)


@dataclass(slots=True)
class LightSection:
    manager_class: str = _DEFAULT_LIGHT_MGR_CLASS
    rows: list[LightRow] = field(default_factory=list)
    preserved_children: list[etree._Element] = field(default_factory=list)


# ----- full config file ----------------------------------------------------


@dataclass
class ConfigFile:
    """A parsed ``<layout-config>`` PanelPro.xml / Configuration.xml.

    Typed sections (``turnout_sections``, ``sensor_sections``,
    ``light_sections``) are lists because JMRI emits one element per
    connection prefix. Other top-level elements (signal heads, memories,
    routes, blocks, panels, LogixNG, etc.) live in ``preserved`` and are
    written back verbatim.
    """

    root_attribs: dict[str, str] = field(default_factory=dict)
    doctype: str | None = None
    turnout_sections: list[TurnoutSection] = field(default_factory=list)
    sensor_sections: list[SensorSection] = field(default_factory=list)
    light_sections: list[LightSection] = field(default_factory=list)
    # Other top-level elements we don't model, kept verbatim:
    preserved: list[etree._Element] = field(default_factory=list)

    # ----- accessors -------------------------------------------------------

    def all_turnouts(self) -> Iterable[TurnoutRow]:
        for section in self.turnout_sections:
            yield from section.rows

    def all_sensors(self) -> Iterable[SensorRow]:
        for section in self.sensor_sections:
            yield from section.rows

    def all_lights(self) -> Iterable[LightRow]:
        for section in self.light_sections:
            yield from section.rows


# ----- parse helpers --------------------------------------------------------


def _parse_bool(value: str | None, default: bool = False) -> bool:
    """JMRI booleans are ``"true"``/``"false"`` or sometimes ``"yes"``/``"no"``."""
    if value is None:
        return default
    return value.strip().lower() in ("true", "yes", "1")


def _child_text(elem: etree._Element, tag: str) -> str | None:
    """Return the trimmed text of the first ``<tag>`` child, if any."""
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _system_name_of(elem: etree._Element) -> str:
    """Read the bean's system name from either the 5.x child element or
    the legacy attribute. Returns the empty string when neither is set."""
    return _child_text(elem, "systemName") or elem.get("systemName") or ""


def _user_name_of(elem: etree._Element) -> str | None:
    return _child_text(elem, "userName") or elem.get("userName") or None


def _comment_of(elem: etree._Element) -> str | None:
    return _child_text(elem, "comment") or elem.get("comment") or None


def _extra_attribs(elem: etree._Element, known: set[str]) -> dict[str, str]:
    """Capture parent attributes we don't model so they can round-trip."""
    return {k: v for k, v in elem.attrib.items() if k not in known}


# ----- parse: bean rows -----------------------------------------------------


def _parse_turnout(elem: etree._Element) -> TurnoutRow:
    return TurnoutRow(
        system_name=_system_name_of(elem),
        user_name=_user_name_of(elem),
        feedback=elem.get("feedback") or None,
        inverted=_parse_bool(elem.get("inverted"), False),
        automate=elem.get("automate") or None,
        comment=_comment_of(elem),
        extra_attribs=_extra_attribs(elem, _TURNOUT_KNOWN_ATTRS),
    )


def _parse_sensor(elem: etree._Element) -> SensorRow:
    return SensorRow(
        system_name=_system_name_of(elem),
        user_name=_user_name_of(elem),
        inverted=_parse_bool(elem.get("inverted"), False),
        comment=_comment_of(elem),
        extra_attribs=_extra_attribs(elem, _SENSOR_KNOWN_ATTRS),
    )


def _parse_light(elem: etree._Element) -> LightRow:
    return LightRow(
        system_name=_system_name_of(elem),
        user_name=_user_name_of(elem),
        comment=_comment_of(elem),
        extra_attribs=_extra_attribs(elem, _LIGHT_KNOWN_ATTRS),
    )


# ----- parse: sections ------------------------------------------------------


def _bean_row_children(elem: etree._Element, bean_tag: str) -> list[etree._Element]:
    """Return the bean rows inside a section, in document order."""
    return [child for child in elem.findall(bean_tag)
            if _system_name_of(child)]


def _section_preserved(elem: etree._Element, bean_tag: str) -> list[etree._Element]:
    """Section children that AREN'T bean rows (operations, defaults, ...)."""
    return [child for child in elem
            if isinstance(child.tag, str) and child.tag != bean_tag]


def _parse_turnout_section(elem: etree._Element) -> TurnoutSection:
    return TurnoutSection(
        manager_class=elem.get("class") or _DEFAULT_TURNOUT_MGR_CLASS,
        rows=[_parse_turnout(t) for t in _bean_row_children(elem, "turnout")],
        preserved_children=_section_preserved(elem, "turnout"),
    )


def _parse_sensor_section(elem: etree._Element) -> SensorSection:
    return SensorSection(
        manager_class=elem.get("class") or _DEFAULT_SENSOR_MGR_CLASS,
        rows=[_parse_sensor(s) for s in _bean_row_children(elem, "sensor")],
        preserved_children=_section_preserved(elem, "sensor"),
    )


def _parse_light_section(elem: etree._Element) -> LightSection:
    return LightSection(
        manager_class=elem.get("class") or _DEFAULT_LIGHT_MGR_CLASS,
        rows=[_parse_light(l) for l in _bean_row_children(elem, "light")],
        preserved_children=_section_preserved(elem, "light"),
    )


# ----- parse: top-level -----------------------------------------------------


def load_config_bytes(data: bytes) -> ConfigFile:
    """Parse a ``<layout-config>`` PanelPro/Configuration file from bytes.

    ``remove_blank_text=True`` strips the whitespace-only text nodes
    JMRI writes between siblings so lxml's ``pretty_print`` on save
    produces cleanly indented output. Without this, freshly-added
    ``<turnouts>`` / ``<sensors>`` sections come out on one gigantic
    line -- unreadable when a user tries to hand-edit the file. Safe
    for us because the file is element-only (no mixed content that
    would care about internal whitespace)."""
    parser = etree.XMLParser(
        remove_blank_text=True,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    root = etree.fromstring(data, parser=parser)
    if root.tag != "layout-config":
        raise ValueError(
            f"expected <layout-config> root, got <{root.tag}>"
        )
    tree = root.getroottree()
    doctype: str | None = getattr(tree.docinfo, "doctype", None) or None

    config = ConfigFile(
        root_attribs={k: v for k, v in root.attrib.items()},
        doctype=doctype,
    )
    for child in root:
        if not isinstance(child.tag, str):
            config.preserved.append(child)
            continue
        if child.tag == "turnouts":
            config.turnout_sections.append(_parse_turnout_section(child))
        elif child.tag == "sensors":
            config.sensor_sections.append(_parse_sensor_section(child))
        elif child.tag == "lights":
            config.light_sections.append(_parse_light_section(child))
        else:
            config.preserved.append(child)
    return config


def load_config(path: str | pathlib.Path) -> ConfigFile:
    return load_config_bytes(pathlib.Path(path).read_bytes())


# ----- serialize: bean rows (JMRI 5.x shape) -------------------------------


def _set_child_text(parent: etree._Element, tag: str, text: str | None) -> None:
    """Append ``<tag>text</tag>`` to *parent* iff *text* is set."""
    if text:
        etree.SubElement(parent, tag).text = text


def _write_turnout(parent: etree._Element, row: TurnoutRow) -> None:
    attrs: dict[str, str] = dict(row.extra_attribs)
    if row.feedback:
        attrs["feedback"] = row.feedback
    attrs["inverted"] = "true" if row.inverted else "false"
    if row.automate:
        attrs["automate"] = row.automate
    elem = etree.SubElement(parent, "turnout", attrib=attrs)
    _set_child_text(elem, "systemName", row.system_name)
    _set_child_text(elem, "userName", row.user_name)
    _set_child_text(elem, "comment", row.comment)


def _write_sensor(parent: etree._Element, row: SensorRow) -> None:
    attrs: dict[str, str] = dict(row.extra_attribs)
    attrs["inverted"] = "true" if row.inverted else "false"
    elem = etree.SubElement(parent, "sensor", attrib=attrs)
    _set_child_text(elem, "systemName", row.system_name)
    _set_child_text(elem, "userName", row.user_name)
    _set_child_text(elem, "comment", row.comment)


def _write_light(parent: etree._Element, row: LightRow) -> None:
    attrs: dict[str, str] = dict(row.extra_attribs)
    elem = etree.SubElement(parent, "light", attrib=attrs)
    _set_child_text(elem, "systemName", row.system_name)
    _set_child_text(elem, "userName", row.user_name)
    _set_child_text(elem, "comment", row.comment)


# ----- serialize: sections --------------------------------------------------


def _write_turnout_section(parent: etree._Element, section: TurnoutSection) -> None:
    elem = etree.SubElement(parent, "turnouts", attrib={"class": section.manager_class})
    for child in section.preserved_children:
        elem.append(child)
    for row in section.rows:
        _write_turnout(elem, row)


def _write_sensor_section(parent: etree._Element, section: SensorSection) -> None:
    elem = etree.SubElement(parent, "sensors", attrib={"class": section.manager_class})
    for child in section.preserved_children:
        elem.append(child)
    for row in section.rows:
        _write_sensor(elem, row)


def _write_light_section(parent: etree._Element, section: LightSection) -> None:
    elem = etree.SubElement(parent, "lights", attrib={"class": section.manager_class})
    for child in section.preserved_children:
        elem.append(child)
    for row in section.rows:
        _write_light(elem, row)


def save_config_bytes(config: ConfigFile) -> bytes:
    """Serialize a :class:`ConfigFile` back to XML bytes (JMRI 5.x shape)."""
    root = etree.Element("layout-config", attrib=config.root_attribs)
    for section in config.turnout_sections:
        _write_turnout_section(root, section)
    for section in config.sensor_sections:
        _write_sensor_section(root, section)
    for section in config.light_sections:
        _write_light_section(root, section)
    for elem in config.preserved:
        root.append(elem)
    common_kwargs: dict[str, object] = {
        "xml_declaration": True,
        "encoding": "utf-8",
        "pretty_print": True,
        "standalone": False,
    }
    if config.doctype:
        common_kwargs["doctype"] = config.doctype
    return etree.tostring(root, **common_kwargs)


def save_config(path: str | pathlib.Path, config: ConfigFile) -> None:
    pathlib.Path(path).write_bytes(save_config_bytes(config))


# ----- helpers for the typical "build a fresh config" path -----------------


def empty_config(*, doctype: str | None = None) -> ConfigFile:
    """A blank :class:`ConfigFile` -- useful when no file exists yet."""
    return ConfigFile(
        root_attribs={},
        doctype=doctype or '<!DOCTYPE layout-config SYSTEM "layout-config.dtd">',
    )
