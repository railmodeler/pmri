from __future__ import annotations

from lxml import etree

from jmri_core.beans.roster_entry import RosterEntry
from jmri_core.persistence.roster_index_xml import (
    RosterGroup,
    RosterIndex,
    load_roster_index_bytes,
    save_roster_index_bytes,
)


def _sample_index() -> RosterIndex:
    return RosterIndex(
        entries=[
            RosterEntry(
                id="A", dcc_address="3", decoder_family="generic", decoder_model="x"
            ),
            RosterEntry(
                id="B",
                dcc_address="3601",
                long_address=True,
                decoder_family="ESU",
                decoder_model="LokSound 5",
            ),
        ],
        groups=[RosterGroup(name="freight"), RosterGroup(name="passenger")],
        filter="club",
    )


def test_roster_index_roundtrip() -> None:
    idx = _sample_index()
    blob = save_roster_index_bytes(idx)
    loaded = load_roster_index_bytes(blob)
    assert len(loaded.entries) == 2
    assert {e.id for e in loaded.entries} == {"A", "B"}
    assert [g.name for g in loaded.groups] == ["freight", "passenger"]
    assert loaded.filter == "club"


def test_emitted_xml_has_correct_root() -> None:
    blob = save_roster_index_bytes(_sample_index())
    root = etree.fromstring(blob)
    assert root.tag == "roster-config"
    assert root.find("roster") is not None
    assert root.find("rosterGroup") is not None


def test_loading_wrong_root_raises() -> None:
    blob = b'<?xml version="1.0"?><wrong/>'
    import pytest  # noqa: PLC0415
    with pytest.raises(ValueError, match="roster-config"):
        load_roster_index_bytes(blob)


def test_empty_index_has_no_group_element() -> None:
    idx = RosterIndex(entries=[
        RosterEntry(id="X", dcc_address="3", decoder_family="g", decoder_model="x"),
    ])
    blob = save_roster_index_bytes(idx)
    root = etree.fromstring(blob)
    assert root.find("rosterGroup") is None


def test_index_strips_cv_values_from_preserved_xml() -> None:
    """The index writer must NOT include ``<values>`` (decoder CV state)
    or ``<speedprofile>`` (per-step calibration): those belong ONLY in
    the per-loco file. Otherwise every profile-copy / roster-write
    duplicates the loco's entire CV table into ``roster.xml``, which
    bloats the file, breaks JMRI Java's expectations (its index parser
    ignores those elements), and defeats the roster.xml =
    "compact identity list" design."""
    idx = RosterIndex(entries=[
        RosterEntry(
            id="Z", dcc_address="3", decoder_family="g", decoder_model="x",
            preserved_xml=[
                "<values><CVvalue name='1' value='42'/></values>",
                "<speedprofile><overRunTime>0.0</overRunTime></speedprofile>",
                "<dateUpdated>2026-09-17 12:00:00</dateUpdated>",
            ],
        ),
    ])
    blob = save_roster_index_bytes(idx)
    text = blob.decode("utf-8")
    assert "<values>" not in text
    assert "<speedprofile>" not in text
    # Non-bulk preserved children still round-trip.
    assert "<dateUpdated>" in text


def test_per_loco_file_still_includes_cv_values() -> None:
    """Regression pin: the strip only fires on the INDEX write path.
    Per-loco file serialization (``save_locomotive`` -> writes one entry
    to its own file) still preserves ``<values>`` -- otherwise CV state
    would round-trip through an editor as empty."""
    from jmri_core.persistence.roster_xml import save_locomotive_bytes  # noqa: PLC0415

    entry = RosterEntry(
        id="Z", dcc_address="3", decoder_family="g", decoder_model="x",
        preserved_xml=[
            "<values><CVvalue name='1' value='42'/></values>",
        ],
    )
    blob = save_locomotive_bytes([entry])
    assert b"<values>" in blob
    assert b"<CVvalue" in blob
