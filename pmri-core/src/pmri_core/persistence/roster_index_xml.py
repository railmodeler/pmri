"""``roster.xml`` -- the index file listing every roster entry.

Per ``contracts/xml/schema/roster.xsd``, the top-level element is
``<roster-config>`` containing one ``<roster>`` (with ``<locomotive>``
elements referencing the per-loco files by ``id``) and optional
``<rosterGroup>`` definitions.

A locomotive entry in the index repeats the same attributes as the per-loco
file's root, *plus* a ``groupName`` attribute when assigned to a group.
This module models just the index -- Stage 1's ``roster_xml`` handles the
per-loco files.
"""
from __future__ import annotations

import pathlib

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from pmri_core.beans.roster_entry import RosterEntry
from pmri_core.persistence.roster_xml import _element_to_entry, _entry_to_element

# Preserved-XML tags that belong ONLY in the per-loco file, not in the
# roster.xml index. ``<values>`` holds decoder CV state (hundreds of
# elements per loco), ``<speedprofile>`` holds a per-step calibration
# table -- both bulky and per-loco by nature. JMRI Java keeps its
# roster.xml index compact by producing these via separate save paths
# (CvTableModel / SpeedProfile); we track them under
# ``RosterEntry.preserved_xml`` for lossless round-trip, so we have to
# strip them here on the index-write path.
_INDEX_STRIP_TAGS = frozenset(("values", "speedprofile"))


class RosterGroup(BaseModel):
    """A named group of roster entries (e.g. "freight power")."""
    model_config = ConfigDict(populate_by_name=True)
    name: str
    members: list[str] = Field(default_factory=list)


class RosterIndex(BaseModel):
    """Top-level ``<roster-config>`` content."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    entries: list[RosterEntry] = Field(default_factory=list)
    groups: list[RosterGroup] = Field(default_factory=list)
    filter: str = ""


def load_roster_index_bytes(data: bytes) -> RosterIndex:
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(data, parser=parser)
    if root.tag != "roster-config":
        raise ValueError(f"expected <roster-config>, got <{root.tag}>")

    roster_elem = root.find("roster")
    entries = (
        [_element_to_entry(loco) for loco in roster_elem.findall("locomotive")]
        if roster_elem is not None
        else []
    )
    filter_attr = roster_elem.get("filter", "") if roster_elem is not None else ""

    groups: list[RosterGroup] = [
        RosterGroup(name=(g.text or "").strip())
        for rg in root.findall("rosterGroup")
        for g in rg.findall("group")
    ]

    return RosterIndex(entries=entries, groups=groups, filter=filter_attr)


def load_roster_index(path: str | pathlib.Path) -> RosterIndex:
    return load_roster_index_bytes(pathlib.Path(path).read_bytes())


def save_roster_index_bytes(index: RosterIndex) -> bytes:
    root = etree.Element("roster-config")
    roster = etree.SubElement(root, "roster")
    if index.filter:
        roster.set("filter", index.filter)
    for entry in index.entries:
        roster.append(
            _entry_to_element(entry, skip_preserved_tags=_INDEX_STRIP_TAGS)
        )

    if index.groups:
        rg = etree.SubElement(root, "rosterGroup")
        for group in index.groups:
            g = etree.SubElement(rg, "group")
            g.text = group.name

    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
        standalone=False,
    )


def save_roster_index(path: str | pathlib.Path, index: RosterIndex) -> None:
    pathlib.Path(path).write_bytes(save_roster_index_bytes(index))


__all__ = [
    "RosterGroup",
    "RosterIndex",
    "load_roster_index",
    "load_roster_index_bytes",
    "save_roster_index",
    "save_roster_index_bytes",
]
