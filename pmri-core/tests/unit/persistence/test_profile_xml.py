"""``<profile>/profile/profile.xml`` -- JMRI auxiliary-configuration
read + write.

Covers the real JMRI schema (NetBeans envelope + connections +
startup <perform> entries) so PMRI and JMRI can share one file.
Includes a golden-fixture round-trip against the user's actual
Pi profile.xml shape so a schema evolution breaks a test, not a boot.
"""
from __future__ import annotations

import pathlib

import pytest
from pmri_core.persistence.profile import Profile
from pmri_core.persistence.profile_xml import (
    NS_CONNECTIONS,
    NS_NETBEANS,
    NS_STARTUP,
    ConnectionDef,
    OpaqueStartupItem,
    ProfileSettings,
    ScriptDef,
    load_profile_xml,
    load_profile_xml_bytes,
    profile_xml_path,
    save_profile_xml,
    save_profile_xml_bytes,
)


# ---- default construction + accessors ---------------------------------


def test_defaults_are_empty_lists() -> None:
    s = ProfileSettings()
    assert s.connections == []
    assert s.scripts == []
    assert s.other_startup == []
    assert s.connections_ns == NS_CONNECTIONS
    assert s.startup_ns == NS_STARTUP


def test_default_connection_returns_none_when_empty() -> None:
    assert ProfileSettings().default_connection() is None


def test_default_connection_single_wins() -> None:
    c = ConnectionDef(name="layout", type="nce")
    s = ProfileSettings(connections=[c])
    assert s.default_connection() is c


def test_default_connection_honours_default_flag() -> None:
    c1 = ConnectionDef(name="a", type="nce")
    c2 = ConnectionDef(name="b", type="dccex", default=True)
    assert ProfileSettings(connections=[c1, c2]).default_connection() is c2


def test_connection_by_name_lookup() -> None:
    c = ConnectionDef(name="layout", type="nce")
    s = ProfileSettings(connections=[c])
    assert s.connection_by_name("layout") is c
    assert s.connection_by_name("missing") is None


# ---- real JMRI fixture: the user's actual Pi file -------------------
#
# Pins the Case-1 regression: that same file crashed the launcher at
# beta21 because I'd invented my own schema. If this test passes, a
# JMRI-authored profile.xml loads without surprises.


_JMRI_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">
    <connections xmlns="http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd">
        <connection xmlns="" class="jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml"
                    disabled="no" manufacturer="NCE" port="ttyUSB0"
                    reconnectMaxAttempts="100" reconnectMaxInterval="120"
                    speed="9600" systemPrefix="N" turnoutInterval="250" userName="NCE">
            <options>
                <option>
                    <name>Eprom</name>
                    <value>2006 or later</value>
                </option>
            </options>
        </connection>
    </connections>
    <startup xmlns="http://jmri.org/xml/schema/auxiliary-configuration/startup-4-3-5.xsd">
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformFileModelXml" name="preference:PanelPro.xml" type="XmlFile"/>
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformActionModelXml" name="apps.SystemConsoleAction" type="Action">
            <property name="systemPrefix" value=""/>
        </perform>
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformScriptModelXml" name="preference:jython/TurnoutStateManagement.py" type="ScriptFile"/>
        <perform xmlns="" class="jmri.util.startup.configurexml.PerformScriptModelXml" name="preference:jython/NceClockState.py" type="ScriptFile"/>
    </startup>
</auxiliary-configuration>
"""


def test_pmri_fixture_loads_nce_connection() -> None:
    """The exact shape JMRI wrote on the user's Pi -> ConnectionDef."""
    s = load_profile_xml_bytes(_JMRI_FIXTURE)
    assert len(s.connections) == 1
    c = s.connections[0]
    assert c.name == "NCE"
    assert c.type == "nce"  # extracted from the class name's family segment
    assert c.pmri_class == (
        "jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml"
    )
    assert c.default is True  # disabled="no" -> enabled -> launcher default
    # Settings dict: translated ``speed`` -> ``baud``, inferred ``kind``,
    # and the <option name="Eprom"> ended up as a plain settings entry.
    assert c.settings["port"] == "ttyUSB0"
    assert c.settings["baud"] == 9600
    assert c.settings["kind"] == "serial"
    assert c.settings["systemPrefix"] == "N"
    assert c.settings["Eprom"] == "2006 or later"


def test_pmri_fixture_separates_scripts_from_other_perform(
) -> None:
    """``<perform type="ScriptFile">`` entries -> scripts list; the
    non-script entries (``XmlFile`` + ``Action``) land in
    other_startup so a round-trip preserves them."""
    s = load_profile_xml_bytes(_JMRI_FIXTURE)
    assert len(s.scripts) == 2
    assert s.scripts[0].name == "TurnoutStateManagement.py"
    assert s.scripts[0].dir == "jython"
    assert s.scripts[1].name == "NceClockState.py"
    assert s.scripts[1].dir == "jython"

    assert len(s.other_startup) == 2
    assert {o.type for o in s.other_startup} == {"XmlFile", "Action"}
    panelpro = next(o for o in s.other_startup if o.type == "XmlFile")
    assert panelpro.name == "preference:PanelPro.xml"
    sysconsole = next(o for o in s.other_startup if o.type == "Action")
    assert sysconsole.name == "apps.SystemConsoleAction"
    # The Action's <property/> children survive.
    assert sysconsole.properties == [("systemPrefix", "")]


def test_pmri_fixture_round_trips_with_pmri_class_preserved() -> None:
    """load + save re-emits the SAME class string the file carried,
    even if PMRI's own class synthesis would have picked a different
    variant. Round-trip fidelity is a hard requirement -- a user
    saving a profile after an edit must not have JMRI lose track
    of its own connection."""
    s = load_profile_xml_bytes(_JMRI_FIXTURE)
    out = save_profile_xml_bytes(s)
    reloaded = load_profile_xml_bytes(out)
    assert reloaded.connections[0].pmri_class == (
        "jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml"
    )
    assert reloaded.connections[0].settings["baud"] == 9600
    assert len(reloaded.scripts) == 2
    assert len(reloaded.other_startup) == 2


def test_pmri_fixture_round_trips_namespaces_verbatim() -> None:
    """The exact JMRI-versioned namespace URIs survive save+reload.
    JMRI may bump the ``-2-9-6`` / ``-4-3-5`` suffixes in future
    versions; we must not pin a specific version into every save."""
    s = load_profile_xml_bytes(_JMRI_FIXTURE)
    out = save_profile_xml_bytes(s)
    assert b"connections-2-9-6.xsd" in out
    assert b"startup-4-3-5.xsd" in out
    assert b'xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1"' in out


# ---- round-trip + path helpers --------------------------------------


def test_load_missing_file_returns_defaults(tmp_path: pathlib.Path) -> None:
    prof = Profile(root=tmp_path, name="x")
    assert load_profile_xml(prof) == ProfileSettings()


def test_save_creates_parent_directory(tmp_path: pathlib.Path) -> None:
    prof = Profile(root=tmp_path, name="layout.jmri")
    assert not prof.preferences_dir.exists()
    save_profile_xml(prof, ProfileSettings())
    assert profile_xml_path(prof).exists()


def test_profile_xml_path_matches_pmri_convention(
    tmp_path: pathlib.Path,
) -> None:
    """Must land at ``<profile>/profile/profile.xml`` -- the same
    path JMRI reads/writes."""
    prof = Profile(root=tmp_path, name="DRGW.jmri")
    assert profile_xml_path(prof) == tmp_path / "DRGW.jmri" / "profile" / "profile.xml"


# ---- class <-> type translation -------------------------------------


@pytest.mark.parametrize("class_name,expected_type", [
    (
        "jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml",
        "nce",
    ),
    (
        "jmri.jmrix.loconet.locobuffer.configurexml.ConnectionConfigXml",
        "loconet",
    ),
    (
        "jmri.jmrix.dccpp.serial.ConnectionConfigXml",
        "dccex",  # the DCC++ -> DCC-EX rename
    ),
    (
        "jmri.jmrix.openlcb.can.network.ConnectionConfigXml",
        "openlcb",
    ),
    (
        "jmri.jmrix.internal.ConnectionConfigXml",
        "internal",
    ),
])
def test_known_connection_classes_map_to_pmri_types(
    class_name: str, expected_type: str,
) -> None:
    """Pins the class-family -> PMRI type map for every driver we
    ship. If JMRI reorganises a package name upstream, this test
    flags the mismatch before a user's boot discovers it."""
    xml = (
        b'<?xml version="1.0"?>'
        b'<auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">'
        b'<connections xmlns="http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd">'
        b'<connection xmlns="" class="' + class_name.encode() + b'" userName="x"/>'
        b'</connections></auxiliary-configuration>'
    )
    s = load_profile_xml_bytes(xml)
    assert s.connections[0].type == expected_type


def test_unknown_connection_class_round_trips_as_raw_string() -> None:
    """A JMRI connection class we don't recognise (future driver,
    typo, operator-dropped-in binding) survives load + save with the
    raw class string intact. ``type`` is the raw string too, which
    the server's connection factory can't open -- that's fine, the
    launcher falls through to InternalConnection / env override."""
    xml = (
        b'<?xml version="1.0"?>'
        b'<auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">'
        b'<connections xmlns="http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd">'
        b'<connection xmlns="" class="org.contrib.weird.ConnectionConfigXml" userName="x"/>'
        b'</connections></auxiliary-configuration>'
    )
    s = load_profile_xml_bytes(xml)
    assert s.connections[0].pmri_class == "org.contrib.weird.ConnectionConfigXml"
    assert s.connections[0].type == "org.contrib.weird.ConnectionConfigXml"
    out = save_profile_xml_bytes(s)
    reloaded = load_profile_xml_bytes(out)
    assert reloaded.connections[0].pmri_class == "org.contrib.weird.ConnectionConfigXml"


# ---- script path encoding --------------------------------------------


def test_script_path_strips_preference_prefix() -> None:
    """``preference:jython/x.py`` -> ScriptDef(name="x.py",
    dir="jython"). The ``preference:`` prefix means "profile root"
    in JMRI's convention."""
    xml = _wrap_startup(
        b'<perform xmlns="" class="jmri.util.startup.configurexml.PerformScriptModelXml" '
        b'name="preference:jython/Foo.py" type="ScriptFile"/>'
    )
    s = load_profile_xml_bytes(xml)
    assert s.scripts[0].name == "Foo.py"
    assert s.scripts[0].dir == "jython"


def test_script_path_without_slash_has_empty_dir() -> None:
    xml = _wrap_startup(
        b'<perform xmlns="" class="jmri.util.startup.configurexml.PerformScriptModelXml" '
        b'name="preference:Standalone.py" type="ScriptFile"/>'
    )
    s = load_profile_xml_bytes(xml)
    assert s.scripts[0].name == "Standalone.py"
    assert s.scripts[0].dir is None


def test_script_round_trip_re_adds_preference_prefix() -> None:
    """Save re-emits the ``preference:`` prefix so JMRI keeps
    resolving the path against the profile root."""
    s = ProfileSettings(scripts=[ScriptDef(name="x.py", dir="jython")])
    out = save_profile_xml_bytes(s)
    assert b'name="preference:jython/x.py"' in out


def test_script_round_trip_bare_filename() -> None:
    """ScriptDef with no dir serialises as ``preference:x.py`` --
    no leading slash, no ``jython/``."""
    s = ProfileSettings(scripts=[ScriptDef(name="x.py")])
    out = save_profile_xml_bytes(s)
    assert b'name="preference:x.py"' in out


# ---- attribute translation (JMRI 'speed' <-> PMRI 'baud') -----------


def test_speed_attribute_renames_to_baud_on_load() -> None:
    """JMRI's NCE/loconet/dccex drivers all use ``speed`` on the
    wire. PMRI's serial drivers' ``open()`` expects ``baud``. The
    translator renames on load (and reverse on save) so each side
    sees the key it expects."""
    xml = (
        b'<?xml version="1.0"?>'
        b'<auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">'
        b'<connections xmlns="http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd">'
        b'<connection xmlns="" class="jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml" '
        b'speed="19200" port="COM4" userName="layout"/>'
        b'</connections></auxiliary-configuration>'
    )
    s = load_profile_xml_bytes(xml)
    assert s.connections[0].settings["baud"] == 19200
    assert "speed" not in s.connections[0].settings


def test_baud_in_memory_renames_back_to_speed_on_save() -> None:
    """Round-trip: PMRI-authored ``baud`` -> wire ``speed``. Keeps
    the file JMRI-compatible even for PMRI-created connections."""
    s = ProfileSettings(connections=[
        ConnectionDef(
            name="layout", type="nce", default=True,
            settings={"port": "COM4", "baud": 9600, "kind": "serial"},
        ),
    ])
    out = save_profile_xml_bytes(s)
    assert b'speed="9600"' in out
    assert b'baud=' not in out  # not an on-wire attribute


def test_kind_setting_is_runtime_only_not_serialised() -> None:
    """``kind`` is PMRI's transport selector, inferred from the
    class name on load. It's a runtime concern, not something JMRI
    carries on the wire, so save() must omit it."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="x", type="nce", settings={"kind": "serial", "port": "COM1"}),
    ])
    out = save_profile_xml_bytes(s)
    assert b"kind=" not in out


# ---- opaque startup round-trip --------------------------------------


def test_opaque_startup_items_preserved_through_round_trip() -> None:
    """A ``<perform type="Action">`` with properties survives
    save + reload byte-for-byte. JMRI relies on these for its
    boot-time actions (Beantable, SystemConsole, PanelPro load,
    ...), and PMRI must not drop them."""
    s = ProfileSettings(other_startup=[OpaqueStartupItem(
        class_name="jmri.util.startup.configurexml.PerformActionModelXml",
        name="jmri.jmrit.beantable.ListedTableAction",
        type="Action",
        properties=[("systemPrefix", "")],
    )])
    out = save_profile_xml_bytes(s)
    reloaded = load_profile_xml_bytes(out)
    assert len(reloaded.other_startup) == 1
    o = reloaded.other_startup[0]
    assert o.type == "Action"
    assert o.name == "jmri.jmrit.beantable.ListedTableAction"
    assert o.properties == [("systemPrefix", "")]


# ---- synthesised class for PMRI-authored connections ----------------


def test_pmri_authored_connection_synthesises_pmri_class() -> None:
    """A connection built in-memory (no ``pmri_class`` set) picks
    a known-good JMRI class string on save so JMRI reads it back."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="x", type="nce", default=True,
                      settings={"port": "COM4", "baud": 9600, "kind": "serial"}),
    ])
    out = save_profile_xml_bytes(s)
    assert b"jmri.jmrix.nce.serialdriver" in out


def test_pmri_authored_tcp_connection_picks_network_class() -> None:
    """kind=tcp swaps the ``serial`` segment for ``network`` on
    the synthesised class string."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="x", type="nce", default=True,
                      settings={"host": "pi.local", "kind": "tcp"}),
    ])
    out = save_profile_xml_bytes(s)
    assert b"jmri.jmrix.nce.network" in out


# ---- error cases ---------------------------------------------------


def test_load_rejects_wrong_root() -> None:
    with pytest.raises(ValueError, match="auxiliary-configuration"):
        load_profile_xml_bytes(b'<?xml version="1.0"?><nope/>')


def test_load_tolerates_unknown_sections() -> None:
    """A future JMRI auxiliary-config section we don't understand
    must not stop the load; we just skip it."""
    xml = (
        b'<?xml version="1.0"?>'
        b'<auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">'
        b'<future-feature xmlns="http://example.com"><thing/></future-feature>'
        b'<connections xmlns="http://jmri.org/xml/schema/auxiliary-configuration/connections-2-9-6.xsd">'
        b'<connection xmlns="" class="jmri.jmrix.nce.serialdriver.configurexml.ConnectionConfigXml" userName="x"/>'
        b'</connections></auxiliary-configuration>'
    )
    s = load_profile_xml_bytes(xml)
    assert len(s.connections) == 1


# ---- fixtures ------------------------------------------------------


def _wrap_startup(perform_xml: bytes) -> bytes:
    """Wrap *perform_xml* in a minimal auxiliary-configuration envelope."""
    return (
        b'<?xml version="1.0"?>'
        b'<auxiliary-configuration xmlns="http://www.netbeans.org/ns/auxiliary-configuration/1">'
        b'<startup xmlns="http://jmri.org/xml/schema/auxiliary-configuration/startup-4-3-5.xsd">'
        + perform_xml +
        b'</startup></auxiliary-configuration>'
    )
