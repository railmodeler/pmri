"""``<layout-config>`` Configuration.xml -- typed sections + opaque pass-through."""
from __future__ import annotations

import pathlib

import pytest
from lxml import etree

from pmri_core.persistence import (
    ConfigFile,
    LightRow,
    LightSection,
    SensorRow,
    SensorSection,
    TurnoutRow,
    TurnoutSection,
    empty_config,
    load_config_bytes,
    save_config_bytes,
)

GOLDEN_DIR = (
    pathlib.Path(__file__).parents[3]
    / "../stage-0/contracts/xml/golden/samples"
).resolve()


# ----- load against the JMRI-installed sample ------------------------------


def test_load_golden_configuration_xml() -> None:
    path = GOLDEN_DIR / "Configuration.xml"
    config = load_config_bytes(path.read_bytes())

    # The sample has both a LocoNet section and an Internal section per topic.
    assert any(s.manager_class.endswith("LnTurnoutManagerXml")
               for s in config.turnout_sections)
    assert any(s.manager_class.endswith("InternalTurnoutManagerXml")
               for s in config.turnout_sections)

    # Spot-check a turnout from the LocoNet section.
    turnouts_by_sys = {row.system_name: row for row in config.all_turnouts()}
    assert "LT200" in turnouts_by_sys
    assert turnouts_by_sys["LT200"].feedback == "MONITORING"
    assert turnouts_by_sys["LT200"].automate == "Default"

    # Spot-check sensors -- at least one has a userName.
    sensors_by_sys = {row.system_name: row for row in config.all_sensors()}
    assert "LS103" in sensors_by_sys
    assert sensors_by_sys["LS103"].user_name == "Start/Stop"

    # Sections we don't parse (signalheads, memories, blocks, panels...)
    # must end up in `preserved` so we don't lose them on save.
    preserved_tags = {p.tag for p in config.preserved}
    assert "signalheads" in preserved_tags
    assert "memories" in preserved_tags
    assert "blocks" in preserved_tags


def test_unknown_root_raises() -> None:
    with pytest.raises(ValueError, match="layout-config"):
        load_config_bytes(b"<?xml version='1.0'?><something-else/>")


# ----- round-trip preserves unknown sections -------------------------------


def test_round_trip_preserves_unknown_sections() -> None:
    """Load -> save should not lose signalheads/blocks/memories etc."""
    path = GOLDEN_DIR / "Configuration.xml"
    config = load_config_bytes(path.read_bytes())
    out = save_config_bytes(config)
    reparsed = load_config_bytes(out)

    # Every preserved tag from before survives (filter comments -- their
    # ``tag`` is a callable, not a string).
    before_tags = sorted(p.tag for p in config.preserved if isinstance(p.tag, str))
    after_tags = sorted(p.tag for p in reparsed.preserved if isinstance(p.tag, str))
    assert after_tags == before_tags

    # All bean rows round-trip semantically.
    assert sorted(r.system_name for r in config.all_turnouts()) == sorted(
        r.system_name for r in reparsed.all_turnouts()
    )
    assert sorted(r.system_name for r in config.all_sensors()) == sorted(
        r.system_name for r in reparsed.all_sensors()
    )


def test_round_trip_preserves_doctype() -> None:
    path = GOLDEN_DIR / "Configuration.xml"
    config = load_config_bytes(path.read_bytes())
    out = save_config_bytes(config)
    # JMRI's Configuration.xml uses <!DOCTYPE layout-config ...>.
    assert b"layout-config" in out
    assert b"<!DOCTYPE" in out


# ----- typed bean-row encoding ---------------------------------------------


def test_turnout_row_round_trip() -> None:
    """User name, feedback, inverted, automate all encode + decode."""
    config = empty_config()
    config.turnout_sections.append(
        TurnoutSection(
            manager_class="jmri.jmrix.nce.configurexml.NceTurnoutManagerXml",
            rows=[
                TurnoutRow("NT80", user_name="Yard Ladder",
                           feedback="DIRECT", inverted=True, automate="Default"),
                TurnoutRow("NT91"),  # bare row
            ],
        )
    )
    reparsed = load_config_bytes(save_config_bytes(config))
    rows = {r.system_name: r for r in reparsed.all_turnouts()}
    yard = rows["NT80"]
    assert yard.user_name == "Yard Ladder"
    assert yard.feedback == "DIRECT"
    assert yard.inverted is True
    assert yard.automate == "Default"
    assert rows["NT91"].user_name is None
    assert rows["NT91"].inverted is False


def test_sensor_row_round_trip() -> None:
    config = empty_config()
    config.sensor_sections.append(
        SensorSection(rows=[
            SensorRow("NS3", user_name="Block 3", inverted=True),
            SensorRow("NS4"),
        ])
    )
    reparsed = load_config_bytes(save_config_bytes(config))
    rows = {r.system_name: r for r in reparsed.all_sensors()}
    assert rows["NS3"].user_name == "Block 3"
    assert rows["NS3"].inverted is True
    assert rows["NS4"].inverted is False


def test_light_row_round_trip() -> None:
    config = empty_config()
    config.light_sections.append(
        LightSection(rows=[LightRow("NL1", user_name="Yard light")])
    )
    reparsed = load_config_bytes(save_config_bytes(config))
    rows = {r.system_name: r for r in reparsed.all_lights()}
    assert rows["NL1"].user_name == "Yard light"


# ----- empty file path -----------------------------------------------------


def test_empty_config_is_writable_and_reloadable() -> None:
    config = empty_config()
    out = save_config_bytes(config)
    reparsed = load_config_bytes(out)
    assert reparsed.turnout_sections == []
    assert reparsed.sensor_sections == []
    assert reparsed.light_sections == []


def test_inverted_attribute_accepts_pmri_forms() -> None:
    """JMRI booleans appear as 'true'/'false' (preferred) or 'yes'/'no'."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<layout-config>
  <turnouts class="jmri.foo">
    <turnout systemName="LT1" inverted="true" />
    <turnout systemName="LT2" inverted="yes" />
    <turnout systemName="LT3" inverted="false" />
    <turnout systemName="LT4" />
  </turnouts>
</layout-config>"""
    config = load_config_bytes(body)
    rows = {r.system_name: r for r in config.all_turnouts()}
    assert rows["LT1"].inverted is True
    assert rows["LT2"].inverted is True
    assert rows["LT3"].inverted is False
    assert rows["LT4"].inverted is False


def test_preserved_comment_survives_round_trip() -> None:
    """Comments at the root level must survive load -> save."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<layout-config>
  <!-- yard layout v3 -->
  <turnouts class="jmri.foo"/>
</layout-config>"""
    config = load_config_bytes(body)
    out = save_config_bytes(config)
    assert b"yard layout v3" in out


# ----- profile integration -------------------------------------------------


def test_profile_config_file_path(tmp_path: pathlib.Path) -> None:
    """Profile.config_file points at <profile>/PanelPro.xml when neither
    file exists (5.x default)."""
    from pmri_core.persistence import Profile
    p = Profile(root=tmp_path, name="alpha")
    p.ensure()
    assert p.config_file == p.profile_dir / "PanelPro.xml"


def test_round_trip_through_disk(tmp_path: pathlib.Path) -> None:
    """save_config + load_config via a real file path."""
    from pmri_core.persistence import load_config, save_config
    config = empty_config()
    config.turnout_sections.append(
        TurnoutSection(rows=[TurnoutRow("NT80", user_name="Test")])
    )
    target = tmp_path / "Configuration.xml"
    save_config(target, config)
    reparsed = load_config(target)
    rows = list(reparsed.all_turnouts())
    assert len(rows) == 1
    assert rows[0].system_name == "NT80"
    assert rows[0].user_name == "Test"


# ----- lxml-level guards ---------------------------------------------------


def test_preserved_elements_are_lxml_elements() -> None:
    """preserved is a list of raw lxml elements -- confirm shape."""
    path = GOLDEN_DIR / "Configuration.xml"
    config = load_config_bytes(path.read_bytes())
    for elem in config.preserved:
        # Skip comment/PI nodes; tag for elements is a str.
        if isinstance(elem.tag, str):
            assert isinstance(elem, etree._Element)


# ----- JMRI 5.x shape (systemName as child element) ------------------------


_JMRI_5X_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<layout-config>
  <turnouts class="jmri.jmrix.nce.configurexml.NceTurnoutManagerXml">
    <operations automate="false"/>
    <defaultclosedspeed>Normal</defaultclosedspeed>
    <turnout feedback="MONITORING" inverted="false" locked="false" lockMode="cab" decoder="None" automate="Off">
      <systemName>NT40</systemName>
      <userName>Durango Staging turnout 1</userName>
    </turnout>
    <turnout feedback="DIRECT" inverted="true" decoder="None" automate="Default">
      <systemName>NT42</systemName>
      <userName>Chama Staging Turnout 1</userName>
      <comment>Line With 45</comment>
    </turnout>
  </turnouts>
  <sensors class="jmri.jmrix.internal.configurexml.InternalSensorManagerXml">
    <defaultInitialState>unknown</defaultInitialState>
    <sensor inverted="false">
      <systemName>ISCLOCKRUNNING</systemName>
    </sensor>
  </sensors>
</layout-config>"""


def test_loads_pmri_5x_systemName_child_element() -> None:
    """The 5.x shape stores systemName as a child element."""
    config = load_config_bytes(_JMRI_5X_SAMPLE)
    rows = {r.system_name: r for r in config.all_turnouts()}
    assert "NT40" in rows
    assert rows["NT40"].user_name == "Durango Staging turnout 1"
    assert rows["NT40"].feedback == "MONITORING"
    assert rows["NT40"].inverted is False
    assert rows["NT42"].inverted is True
    assert rows["NT42"].comment == "Line With 45"
    sensors = {r.system_name: r for r in config.all_sensors()}
    assert "ISCLOCKRUNNING" in sensors
    assert sensors["ISCLOCKRUNNING"].inverted is False


def test_pmri_5x_extra_attribs_round_trip() -> None:
    """Unmodeled parent attributes (locked, lockMode, decoder) must
    survive a load -> save round trip so JMRI can still open the file."""
    config = load_config_bytes(_JMRI_5X_SAMPLE)
    rows = {r.system_name: r for r in config.all_turnouts()}
    assert rows["NT40"].extra_attribs["locked"] == "false"
    assert rows["NT40"].extra_attribs["lockMode"] == "cab"
    assert rows["NT40"].extra_attribs["decoder"] == "None"

    out = save_config_bytes(config)
    reparsed = load_config_bytes(out)
    reparsed_rows = {r.system_name: r for r in reparsed.all_turnouts()}
    assert reparsed_rows["NT40"].extra_attribs["locked"] == "false"
    assert reparsed_rows["NT40"].extra_attribs["lockMode"] == "cab"


def test_write_uses_5x_shape_systemName_as_child() -> None:
    """The serializer must emit JMRI 5.x form (systemName as child)."""
    config = empty_config()
    config.turnout_sections.append(
        TurnoutSection(rows=[
            TurnoutRow("NT80", user_name="Yard Ladder", feedback="DIRECT",
                       comment="Optional"),
        ])
    )
    out = save_config_bytes(config).decode("utf-8")
    # The 5.x shape uses child elements for systemName/userName/comment.
    assert "<systemName>NT80</systemName>" in out
    assert "<userName>Yard Ladder</userName>" in out
    assert "<comment>Optional</comment>" in out
    # And the bean attributes go on the parent.
    assert 'feedback="DIRECT"' in out
    # Make sure we DON'T duplicate to the legacy attribute form.
    assert 'systemName="NT80"' not in out


def test_section_preserved_children_round_trip() -> None:
    """``<operations>`` and per-direction defaults inside a section
    must round-trip even though they aren't bean rows."""
    config = load_config_bytes(_JMRI_5X_SAMPLE)
    section = config.turnout_sections[0]
    preserved_tags = {c.tag for c in section.preserved_children}
    assert "operations" in preserved_tags
    assert "defaultclosedspeed" in preserved_tags
    out = save_config_bytes(config)
    assert b"defaultclosedspeed" in out
    assert b"<operations" in out


def test_comment_element_read_and_written() -> None:
    """Bean comments are a child element, not an attribute."""
    config = empty_config()
    config.sensor_sections.append(
        SensorSection(rows=[
            SensorRow("NS3", user_name="Block 3", comment="Track 3 lower"),
        ])
    )
    out = save_config_bytes(config).decode("utf-8")
    assert "<comment>Track 3 lower</comment>" in out
    reparsed = load_config_bytes(save_config_bytes(config))
    rows = {r.system_name: r for r in reparsed.all_sensors()}
    assert rows["NS3"].comment == "Track 3 lower"


def test_legacy_systemname_attribute_still_reads() -> None:
    """Old (pre-5.x) attribute form must still load -- JMRI itself
    accepts both, so we have to as well for any historical files
    users might bring."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<layout-config>
  <turnouts class="jmri.foo">
    <turnout systemName="LT200" feedback="MONITORING" userName="Old Style" />
  </turnouts>
</layout-config>"""
    config = load_config_bytes(body)
    rows = list(config.all_turnouts())
    assert rows[0].system_name == "LT200"
    assert rows[0].user_name == "Old Style"
    assert rows[0].feedback == "MONITORING"
