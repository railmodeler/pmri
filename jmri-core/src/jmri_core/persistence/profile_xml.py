"""``<profile>/profile/profile.xml`` -- JMRI's auxiliary-configuration
profile store, read + written in the real JMRI schema so PMRI and
JMRI share one file.

The file is a NetBeans auxiliary-configuration envelope that JMRI
owns::

    <?xml version="1.0" encoding="UTF-8"?>
    <auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">
      <connections xmlns="http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd">
        <connection xmlns="" class="jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml"
                    manufacturer="NCE" port="ttyUSB0" speed="9600"
                    systemPrefix="N" userName="NCE" disabled="no" ...>
          <options>
            <option><name>Eprom</name><value>2006 or later</value></option>
          </options>
        </connection>
      </connections>
      <startup xmlns="http://jmri.org/xml/schema/auxiliary-configuration/startup-4-3-5.xsd">
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformFileModelXml"
                 name="preference:PanelPro.xml" type="XmlFile"/>
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformActionModelXml"
                 name="jmri.jmrit.beantable.ListedTableAction" type="Action"/>
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformScriptModelXml"
                 name="preference:jython/TurnoutStateManagement.py" type="ScriptFile"/>
        ...
      </startup>
    </auxiliary-configuration>

Two important schema features:

* Each ``<connection>`` and ``<perform>`` child reopens the empty
  namespace via ``xmlns=""`` so the parent's JMRI namespace doesn't
  leak onto the inner attributes.
* The ``<startup>`` section is a flat list of ``<perform>`` entries
  with ``type="XmlFile" | "Action" | "ScriptFile"``. PMRI only
  interprets ``ScriptFile`` entries; the other two kinds are
  **preserved opaquely** (``other_startup``) so JMRI's own startup
  actions survive a PMRI round-trip untouched.

Mapping to PMRI's internal model:

* ``<connection class="jmri.jmrix.X.…ConnectionConfigXml">`` ->
  :class:`ConnectionDef` with ``type`` derived from the family
  segment (``X`` lowercased, with ``dccpp`` renamed to ``dccex``).
  The raw class string survives on ``jmri_class`` for round-trip.
  JMRI's ``userName`` attribute becomes ``name``; everything else
  on the element (``port``, ``speed``, ``manufacturer``, ...) plus
  each ``<option><name>K</name><value>V</value></option>`` lands in
  ``settings``. ``speed`` is renamed to ``baud`` so our serial
  drivers' ``open()`` sees the key they expect.
* ``<perform type="ScriptFile" name="preference:dir/file.py">`` ->
  :class:`ScriptDef`\\(name=\"file.py\", dir=\"dir\"\\). The
  ``preference:`` prefix means "profile root"; stripped on load,
  re-prepended on save.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from jmri_core.persistence.backup import write_with_backup
from jmri_core.persistence.profile import Profile

PROFILE_XML = "profile.xml"
"""JMRI's filename inside ``<profile>/profile/``."""

NS_NETBEANS = "http://www.netbeans.org/ns/auxiliary-configuration/1"
"""Root envelope namespace (NetBeans auxiliary-configuration)."""

NS_CONNECTIONS = (
    "http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd"
)
"""Default JMRI namespace for ``<connections>``. Preserved verbatim
from the loaded file when present; otherwise synthesised at write
time. The ``-2-9-6`` version-in-URI is JMRI's convention; we keep
whatever version the file we loaded carried."""

NS_STARTUP = (
    "http://jmri.org/xml/schema/auxiliary-configuration/startup-4-3-5.xsd"
)
"""Default JMRI namespace for ``<startup>``. Same preserve-on-load
convention as :data:`NS_CONNECTIONS`."""

# ---- JMRI connection class <-> PMRI driver type map --------------------
#
# JMRI's class attribute follows ``jmri.jmrix.<family>.<transport>.
# configurexml.ConnectionConfigXml``. The family segment maps directly
# to our driver type, with one rename (DCC-EX is JMRI's "dccpp").
# Unknown families stay as the raw class string -- round-trips cleanly,
# the server's connection factory treats unknown types as "pick
# default InternalConnection".

_FAMILY_RENAME = {
    "dccpp": "dccex",  # JMRI calls it DCC++; we call it DCC-EX.
}


def _class_to_type(class_name: str) -> str:
    """Extract the PMRI driver type key from a JMRI
    ``ConnectionConfigXml`` class name. See the dataclass docstring
    for the mapping rules; falls back to the full class string when
    the pattern doesn't match."""
    # Expect ``jmri.jmrix.<family>.…`` -- 3 or more segments where the
    # first two are the JMRI package prefix. The family lives in [2].
    parts = class_name.split(".")
    if (
        len(parts) >= 3  # noqa: PLR2004 -- 3 is the shape, not a magic number
        and parts[0] == "jmri" and parts[1] == "jmrix"
    ):
        family = parts[2].lower()
        return _FAMILY_RENAME.get(family, family)
    return class_name


_SERIAL_HINTS = ("serialdriver", ".serial.")
_NETWORK_HINTS = ("networkdriver", ".network.")


def _infer_kind(class_name: str) -> str | None:
    """Infer the ``kind`` setting (``serial``/``tcp``/``loopback``) from
    the connection class name. Our serial drivers' ``open()`` dispatches
    on ``kind``; JMRI's schema doesn't carry it as its own attribute,
    so we recover it from the class segment."""
    low = class_name.lower()
    if any(h in low for h in _SERIAL_HINTS):
        return "serial"
    if any(h in low for h in _NETWORK_HINTS):
        return "tcp"
    return None


# ---- dataclasses -------------------------------------------------------


@dataclass(slots=True)
class ConnectionDef:
    """One ``<connection>`` entry mapped to PMRI's model.

    ``name`` <- JMRI ``userName``. ``type`` <- :func:`_class_to_type`
    on the ``class`` attribute. ``settings`` holds every other
    attribute plus the ``<option>`` children, with ``speed`` renamed
    to ``baud`` for serial compatibility. ``jmri_class`` preserves the
    original class string for lossless round-trip.
    """

    name: str
    type: str
    settings: dict[str, Any] = field(default_factory=dict)
    default: bool = False
    jmri_class: str = ""


@dataclass(slots=True)
class ScriptDef:
    """One ``<perform type="ScriptFile">`` entry.

    *name* is the trailing filename. *dir* holds whatever path
    segments the entry put between ``preference:`` and the filename
    (empty = profile root). ``jython`` is the JMRI default; PMRI
    preserves what the file says.
    """

    name: str
    dir: str | None = None


@dataclass(slots=True)
class OpaqueStartupItem:
    """A ``<perform>`` entry PMRI doesn't interpret (``XmlFile``,
    ``Action``, future types). Stored as a shallow attribute copy +
    child XML so we can write it back verbatim and JMRI's own
    startup actions ("load PanelPro.xml", "open the SystemConsole")
    survive the round-trip."""

    class_name: str
    name: str
    type: str
    properties: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ProfileSettings:
    """In-memory view of ``profile.xml``.

    ``connections_ns`` and ``startup_ns`` carry the exact JMRI
    namespace URIs read from the file so we round-trip them even
    when JMRI bumps the version-in-URI (``connections-2-9-6`` ->
    ``connections-2-10-0`` etc.).
    """

    connections: list[ConnectionDef] = field(default_factory=list)
    scripts: list[ScriptDef] = field(default_factory=list)
    other_startup: list[OpaqueStartupItem] = field(default_factory=list)
    connections_ns: str = NS_CONNECTIONS
    startup_ns: str = NS_STARTUP

    # ---- accessors used by the launcher ------------------------

    def default_connection(self) -> ConnectionDef | None:
        """Return the connection flagged ``default=True``, or the
        first one if exactly one is defined, or None when the picker
        should prompt. JMRI's schema only allows one connection, so
        for a JMRI-authored profile the "first" branch is the usual
        case."""
        if not self.connections:
            return None
        for c in self.connections:
            if c.default:
                return c
        if len(self.connections) == 1:
            return self.connections[0]
        return None

    def connection_by_name(self, name: str) -> ConnectionDef | None:
        for c in self.connections:
            if c.name == name:
                return c
        return None


# ---- public I/O --------------------------------------------------------


def profile_xml_path(profile: Profile) -> pathlib.Path:
    """``<profile>/profile/profile.xml`` -- same path JMRI uses."""
    return profile.preferences_dir / PROFILE_XML


def load_profile_xml(profile: Profile) -> ProfileSettings:
    """Load ``<profile>/profile/profile.xml``. Returns default
    :class:`ProfileSettings` when the file doesn't exist (fresh
    profile or a profile that JMRI itself hasn't yet written)."""
    path = profile_xml_path(profile)
    if not path.exists():
        return ProfileSettings()
    return load_profile_xml_bytes(path.read_bytes())


def load_profile_xml_bytes(data: bytes) -> ProfileSettings:
    """Byte-level entry point. Validates the root envelope then walks
    ``<connections>`` and ``<startup>`` using local-name matching so
    the mix of JMRI namespaces + ``xmlns=""`` resets on inner elements
    doesn't trip the parser."""
    root = etree.fromstring(data, parser=etree.XMLParser(remove_blank_text=False))
    if _local_name(root) != "auxiliary-configuration":
        raise ValueError(
            f"profile.xml root must be <auxiliary-configuration>, "
            f"got <{root.tag}>",
        )
    settings = ProfileSettings()
    for section in root:
        local = _local_name(section)
        if local == "connections":
            # Preserve the exact JMRI namespace URI for round-trip.
            ns = _element_default_ns(section)
            if ns:
                settings.connections_ns = ns
            for c in section:
                if _local_name(c) == "connection":
                    settings.connections.append(_parse_connection(c))
        elif local == "startup":
            ns = _element_default_ns(section)
            if ns:
                settings.startup_ns = ns
            for p in section:
                if _local_name(p) != "perform":
                    continue
                parsed = _parse_perform(p)
                if isinstance(parsed, ScriptDef):
                    settings.scripts.append(parsed)
                else:
                    settings.other_startup.append(parsed)
        # Any other section we don't know -> ignore; JMRI may add new
        # auxiliary-config sections in the future and we must not lose
        # them. (For now; a stronger round-trip would preserve them,
        # but no current section besides connections/startup is
        # relevant to PMRI boot.)
    return settings


def save_profile_xml(profile: Profile, settings: ProfileSettings) -> None:
    """Serialise *settings* and write to ``profile.xml`` via a
    rolling backup. Parent directory is auto-created so a fresh
    profile's first save works."""
    path = profile_xml_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_with_backup(path, save_profile_xml_bytes(settings))


def save_profile_xml_bytes(settings: ProfileSettings) -> bytes:
    """Serialise *settings* to JMRI-shaped auxiliary-configuration
    XML bytes. The resulting file is a valid JMRI profile.xml that
    JMRI itself can read (connections visible in Preferences,
    startup actions preserved)."""
    # Root is in the NetBeans namespace. lxml takes a map of
    # prefix->uri; using ``None`` as the prefix sets the DEFAULT
    # namespace for the subtree.
    nsmap_root: dict[str | None, str] = {None: NS_NETBEANS}
    root = etree.Element(
        f"{{{NS_NETBEANS}}}auxiliary-configuration", nsmap=nsmap_root,
    )

    # <connections xmlns="http://jmri.org/xml/schema/.../connections-...xsd">
    conn_el = etree.SubElement(
        root,
        f"{{{settings.connections_ns}}}connections",
        nsmap={None: settings.connections_ns},
    )
    for c in settings.connections:
        _append_connection(conn_el, c)

    # <startup xmlns="http://jmri.org/xml/schema/.../startup-...xsd">
    startup_el = etree.SubElement(
        root,
        f"{{{settings.startup_ns}}}startup",
        nsmap={None: settings.startup_ns},
    )
    for item in settings.other_startup:
        _append_opaque_startup(startup_el, item)
    for script in settings.scripts:
        _append_script(startup_el, script)

    # Case-3 feedback: an empty <startup> and <connections> must
    # serialise as <startup></startup>, not <startup/>. Both forms are
    # XML-equivalent, but JMRI (and some operators' downstream tooling)
    # treat the self-closing variant as "no section at all" and skip
    # the whole profile's connection list on reload. Setting text=""
    # keeps lxml from collapsing the element -- see the lxml test in
    # the beta33 feedback cycle for the exact behaviour.
    if len(conn_el) == 0:
        conn_el.text = ""
    if len(startup_el) == 0:
        startup_el.text = ""

    return etree.tostring(
        root, xml_declaration=True, encoding="utf-8",
        pretty_print=True, standalone=False,
    )


# ---- helpers: parse ----------------------------------------------------


def _local_name(el: etree._Element) -> str:
    """Return ``el``'s tag with any ``{uri}`` prefix stripped."""
    tag = el.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return str(tag)


def _element_default_ns(el: etree._Element) -> str | None:
    """Extract the element's own default-namespace URI from its tag.

    When the element itself declares ``xmlns=".../foo.xsd"`` the tag
    is ``{...}localname``. Returns the URI, or None for the empty
    namespace."""
    tag = el.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return None


def _parse_connection(el: etree._Element) -> ConnectionDef:
    """Build a :class:`ConnectionDef` from a ``<connection>`` element."""
    class_name = el.get("class", "")
    jmri_type = _class_to_type(class_name)
    user_name = el.get("userName") or el.get("manufacturer") or jmri_type

    settings: dict[str, Any] = {}
    for key, raw in el.attrib.items():
        if key in ("class", "userName"):
            continue
        settings[_translate_attr_key(key)] = _coerce_setting_value(raw)
    kind = _infer_kind(class_name)
    if kind is not None:
        settings.setdefault("kind", kind)

    # <options><option><name>K</name><value>V</value></option>...</options>
    for options_el in el:
        if _local_name(options_el) != "options":
            continue
        for opt in options_el:
            if _local_name(opt) != "option":
                continue
            name = _child_text(opt, "name")
            value = _child_text(opt, "value")
            if name:
                settings[name] = _coerce_setting_value(value or "")

    disabled = (el.get("disabled", "no").lower() in ("yes", "true", "1"))
    return ConnectionDef(
        name=user_name,
        type=jmri_type,
        settings=settings,
        default=not disabled,  # JMRI's single-connection convention
        jmri_class=class_name,
    )


def _parse_perform(el: etree._Element) -> ScriptDef | OpaqueStartupItem:
    """Dispatch on ``type`` -- ``ScriptFile`` -> :class:`ScriptDef`,
    otherwise :class:`OpaqueStartupItem` for round-trip preservation."""
    perform_type = el.get("type", "")
    name = el.get("name", "")
    class_name = el.get("class", "")
    if perform_type == "ScriptFile":
        return _parse_script_path(name)
    # Opaque: capture all <property name="X" value="Y"/> children too.
    props: list[tuple[str, str]] = [
        (child.get("name", ""), child.get("value", ""))
        for child in el if _local_name(child) == "property"
    ]
    return OpaqueStartupItem(
        class_name=class_name, name=name, type=perform_type, properties=props,
    )


def _parse_script_path(wire_name: str) -> ScriptDef:
    """Decode a JMRI script ``name`` field.

    JMRI stores script paths like ``preference:jython/x.py``. The
    ``preference:`` prefix resolves to the active profile root.
    We split the remaining path into a leading ``dir`` + trailing
    filename; a path with no slash has empty ``dir``.
    """
    body = wire_name
    if body.startswith("preference:"):
        body = body[len("preference:"):]
    # Normalise path separators (JMRI uses forward slashes).
    body = body.replace("\\", "/")
    if "/" in body:
        head, _, tail = body.rpartition("/")
        return ScriptDef(name=tail, dir=head or None)
    return ScriptDef(name=body)


def _child_text(el: etree._Element, local_name: str) -> str | None:
    for child in el:
        if _local_name(child) == local_name:
            return child.text
    return None


# ---- helpers: write ----------------------------------------------------


def _append_connection(
    parent: etree._Element, c: ConnectionDef,
) -> None:
    """Append a ``<connection xmlns="" ...>`` under *parent*.

    The empty ``xmlns=""`` reset on the inner element is JMRI's
    convention -- the parent's connections-N-N-N.xsd namespace
    only applies to ``<connections>`` itself; the connection body
    is in the no-namespace. lxml renders that correctly when we
    create a plain-tag element inside a namespaced parent provided
    we pass ``nsmap={None: ""}`` to override."""
    class_name = c.jmri_class or _synthesise_class(c.type, c.settings)
    el = etree.SubElement(parent, "connection", nsmap={None: ""})
    el.set("class", class_name)
    # JMRI's "disabled" flag -- our ``default`` field is the inverse
    # (an enabled connection is the launcher's default pick).
    el.set("disabled", "no" if c.default else "yes")
    if c.name:
        el.set("userName", c.name)
    # Serialise settings back to attributes. ``kind`` is PMRI-only;
    # ``baud`` renames back to JMRI's ``speed`` on the wire.
    for key, value in c.settings.items():
        if key == "kind":
            continue  # inferred on read; not written back
        wire_key = _translate_attr_key_reverse(key)
        el.set(wire_key, _render_setting_value(value))


def _append_script(
    parent: etree._Element, script: ScriptDef,
) -> None:
    """Append a ``<perform type="ScriptFile" ...>`` under *parent*."""
    path = script.name if not script.dir else f"{script.dir}/{script.name}"
    el = etree.SubElement(parent, "perform", nsmap={None: ""})
    el.set("class", "jmri.util.startup.configurexml.PerformScriptModelXml")
    el.set("name", f"preference:{path}")
    el.set("type", "ScriptFile")


def _append_opaque_startup(
    parent: etree._Element, item: OpaqueStartupItem,
) -> None:
    """Append a non-script ``<perform>`` entry verbatim."""
    el = etree.SubElement(parent, "perform", nsmap={None: ""})
    el.set("class", item.class_name)
    el.set("name", item.name)
    el.set("type", item.type)
    for pname, pvalue in item.properties:
        prop = etree.SubElement(el, "property")
        prop.set("name", pname)
        prop.set("value", pvalue)


# ---- attribute key translation ----------------------------------------
#
# JMRI's attribute names on <connection> don't always match the keys
# our drivers' ``open(settings)`` expects. The translator fixes the
# common renames (``speed`` <-> ``baud``) and passes everything else
# through unchanged.


_ATTR_RENAMES = {
    "speed": "baud",  # NCE / loconet / dccex serial drivers want ``baud``.
}
_ATTR_RENAMES_REVERSE = {v: k for k, v in _ATTR_RENAMES.items()}


def _translate_attr_key(jmri_key: str) -> str:
    return _ATTR_RENAMES.get(jmri_key, jmri_key)


def _translate_attr_key_reverse(pmri_key: str) -> str:
    return _ATTR_RENAMES_REVERSE.get(pmri_key, pmri_key)


# ---- class synthesis for brand-new PMRI connections ------------------


_TYPE_TO_CLASS = {
    "nce": "jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml",
    "loconet": (
        "jmri.jmrix.loconet.locobuffer.configurexml.ConnectionConfigXml"
    ),
    # Digitrax = LocoNet protocol over a Digitrax interface; the
    # server aliases this type to the loconet driver. Round-tripping
    # through the loconet class means a saved digitrax connection
    # reloads as type "loconet" (same underlying protocol).
    "digitrax": (
        "jmri.jmrix.loconet.locobuffer.configurexml.ConnectionConfigXml"
    ),
    "dccex": "jmri.jmrix.dccpp.serial.ConnectionConfigXml",
    "lenz": "jmri.jmrix.lenz.liusb.configurexml.ConnectionConfigXml",
    "mrc": "jmri.jmrix.mrc.serialdriver.configurexml.ConnectionConfigXml",
    "openlcb": "jmri.jmrix.openlcb.can.network.ConnectionConfigXml",
    "internal": "jmri.jmrix.internal.ConnectionConfigXml",
}


def _synthesise_class(driver_type: str, settings: dict[str, Any]) -> str:
    """Build a JMRI-style class name for a PMRI-authored connection
    (no ``jmri_class`` round-trip metadata). ``settings["kind"]`` picks
    between serial / network transports when both are mapped; falls
    back to a generic serial variant otherwise.

    Unknown driver types produce a synthetic ``pmri.<type>.ConnectionConfigXml``
    sentinel -- JMRI won't recognise it but the file is still
    structurally valid, and PMRI's own loader round-trips it back
    through ``_class_to_type`` just fine."""
    base = _TYPE_TO_CLASS.get(driver_type)
    if base is not None:
        kind = settings.get("kind")
        if kind == "tcp" and "serial" in base:
            # Operator picked a tcp bind for a type we default-mapped
            # to serial -- rewrite the transport segment.
            return base.replace("serial", "network")
        return base
    return f"pmri.{driver_type}.ConnectionConfigXml"


# ---- value coercion ---------------------------------------------------


def _coerce_setting_value(raw: str) -> Any:
    """Best-effort type recovery for a setting value: int -> float ->
    bool -> raw string. Keeps GUI form fields neatly typed; drivers'
    ``open()`` tolerates string-shaped settings too."""
    if raw == "":
        return ""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    return raw


def _render_setting_value(value: Any) -> str:
    """Serialise a settings value back to XML text."""
    if isinstance(value, bool):
        return "yes" if value else "no"  # match JMRI's attribute convention
    return str(value)
