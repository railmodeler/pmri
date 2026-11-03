"""Panel / Layout XML -- envelope-level round-trip.

JMRI panel files (``layout-config`` / ``layout-5-5-5.xsd``) hold an editor's
entire visual layout: every positionable icon, signal, sensor, layout-block
graph node, train icon, memory icon, route table, Logix etc. Modeling every
element type is **Stage 11** (the full layout editor port); for Stage 2 we
ship envelope-level structural round-trip:

* parse the file into an :class:`PanelFile` exposing the top-level metadata
  (pmri-version, encoding, panel/layout list) and the raw lxml tree;
* save writes the lxml tree back, preserving every interior element as-is.

Callers that need to inspect or mutate individual elements use the lxml
tree directly via :attr:`PanelFile.root`. When Stage 11 lands, those raw
elements will be replaced by typed Pydantic models.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from lxml import etree


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
