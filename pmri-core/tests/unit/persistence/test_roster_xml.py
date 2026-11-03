"""Round-trip tests for ``locomotive-config`` XML.

Stage 1 acceptance: serializing a :class:`RosterEntry` and parsing it back
yields an equal model. Stage 1.5 will broaden this to byte-equal canonical
XML output against a corpus of real user files.
"""
from __future__ import annotations

from lxml import etree

from pmri_core.beans.roster_entry import FunctionLabel, RosterEntry, SoundLabel
from pmri_core.persistence import (
    load_locomotive_bytes,
    save_locomotive_bytes,
)


def _sample_entry() -> RosterEntry:
    return RosterEntry(
        id="UP_3601",
        file_name="UP_3601.xml",
        dcc_address="3601",
        long_address=True,
        protocol="dcc_long",
        decoder_family="ESU LokSound v5",
        decoder_model="LokSound 5",
        decoder_comment="programmed 2024-08",
        road_name="Union Pacific",
        road_number="3601",
        mfg="Athearn",
        owner="club",
        model="SD45",
        comment="lead unit",
        max_speed_pct=85,
        image_file_path="resources/img/UP_3601.png",
        icon_file_path="resources/img/UP_3601_icon.png",
        url="https://example.org/locos/UP_3601",
        function_labels=[
            FunctionLabel(number=0, label="Headlight", lockable=True),
            FunctionLabel(number=1, label="Bell", lockable=True),
            FunctionLabel(number=2, label="Horn", lockable=False),
        ],
        sound_labels=[
            SoundLabel(number=1, label="Prime mover"),
            SoundLabel(number=2, label="Air compressor"),
        ],
        attributes={"DispatcherZone": "main", "DCCInstalledBy": "Alice"},
    )


def test_save_load_roundtrip_preserves_model() -> None:
    entry = _sample_entry()
    blob = save_locomotive_bytes([entry])
    loaded = load_locomotive_bytes(blob)
    assert len(loaded) == 1
    assert loaded[0] == entry


def test_emitted_xml_is_schema_shaped() -> None:
    """Spot-check emitted XML structure matches locomotive-config.xsd."""
    blob = save_locomotive_bytes([_sample_entry()])
    root = etree.fromstring(blob)
    assert root.tag == "locomotive-config"
    loco = root.find("locomotive")
    assert loco is not None
    assert loco.get("id") == "UP_3601"
    assert loco.get("roadName") == "Union Pacific"

    # Boolean is "yes"/"no", not "true"/"false".
    addr = loco.find("locoaddress/dcclocoaddress")
    assert addr is not None
    assert addr.get("longaddress") == "yes"

    decoder = loco.find("decoder")
    assert decoder is not None
    assert decoder.get("family") == "ESU LokSound v5"

    # Functions present with correct numbers.
    fls = loco.findall("functionlabels/functionlabel")
    assert [fl.get("num") for fl in fls] == ["0", "1", "2"]
    assert fls[2].get("lockable") == "false"

    # Sounds.
    sls = loco.findall("soundlabels/soundlabel")
    assert [sl.get("num") for sl in sls] == ["1", "2"]

    # Attribute pairs.
    pairs = loco.findall("attributepairs/keyvaluepair")
    assert len(pairs) == 2


def test_byte_equal_after_two_passes() -> None:
    """Canonical round-trip: load -> save -> load -> save must produce identical bytes."""
    entry = _sample_entry()
    a = save_locomotive_bytes([entry])
    once = load_locomotive_bytes(a)
    b = save_locomotive_bytes(once)
    assert a == b


def test_values_block_with_cv_state_round_trips() -> None:
    """Decoder CV state lives inside ``<values>`` on real JMRI roster files.
    Loading and saving must not lose it -- otherwise editing a loco's
    road name in our UI destroys the user's programmed CVs."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<locomotive-config>
  <locomotive id="459" fileName="1.xml" dccAddress="459" roadName="D&amp;RGW"
              model="K27" mfg="Blackstone">
    <dateUpdated>2025-09-06T18:10:19.011+00:00</dateUpdated>
    <decoder model="DSD Steam" family="DSD Steam" comment="" maxFnNum="28" />
    <locoaddress>
      <dcclocoaddress number="459" longaddress="yes" />
      <number>459</number>
      <protocol>dcc_long</protocol>
    </locoaddress>
    <functionlabels />
    <soundlabels />
    <values>
      <decoderDef>
        <varValue item="Vstart" value="0" />
        <varValue item="Total PWM Period" value="180" />
        <varValue item="Packet Time-out Value" value="0" />
      </decoderDef>
      <CVvalue>
        <CV name="1" value="3" />
        <CV name="29" value="38" />
      </CVvalue>
    </values>
  </locomotive>
</locomotive-config>"""
    entries = load_locomotive_bytes(body)
    assert len(entries) == 1
    entry = entries[0]
    # The CV state is captured in preserved_xml.
    joined = "\n".join(entry.preserved_xml)
    assert "<values>" in joined
    assert 'item="Vstart"' in joined
    assert 'name="29"' in joined
    # Round-trip -- the values block survives a save.
    out = save_locomotive_bytes([entry])
    assert b"<values>" in out
    assert b'item="Vstart"' in out
    assert b'name="29"' in out
    # And re-loading preserves it again.
    entries2 = load_locomotive_bytes(out)
    joined2 = "\n".join(entries2[0].preserved_xml)
    assert "Vstart" in joined2
    assert "name=\"29\"" in joined2


def test_dateUpdated_element_preserved() -> None:
    """JMRI's <dateUpdated> child element must round-trip."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<locomotive-config>
  <locomotive id="X" fileName="x.xml" dccAddress="3">
    <dateUpdated>2025-09-06T18:12:13.124+00:00</dateUpdated>
    <decoder family="f" model="m" />
    <locoaddress>
      <dcclocoaddress number="3" longaddress="no" />
      <number>3</number>
      <protocol>dcc_short</protocol>
    </locoaddress>
    <functionlabels />
    <soundlabels />
  </locomotive>
</locomotive-config>"""
    [entry] = load_locomotive_bytes(body)
    out = save_locomotive_bytes([entry])
    assert b"<dateUpdated>2025-09-06T18:12:13.124+00:00</dateUpdated>" in out


def test_minimal_entry_omits_optional_fields() -> None:
    minimal = RosterEntry(id="L1", dcc_address="3", decoder_family="x", decoder_model="y")
    blob = save_locomotive_bytes([minimal])
    root = etree.fromstring(blob)
    loco = root.find("locomotive")
    assert loco is not None
    # Optional attributes must not appear as empty strings.
    assert "roadName" not in loco.attrib
    assert "fileName" not in loco.attrib
    assert "maxSpeed" not in loco.attrib  # 100% is the default
    # functionlabels/soundlabels/attributepairs must be omitted when empty.
    assert loco.find("functionlabels") is None
    assert loco.find("soundlabels") is None
    assert loco.find("attributepairs") is None
