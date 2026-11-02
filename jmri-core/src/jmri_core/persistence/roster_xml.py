"""``locomotive-config`` XML load/save -- see ``contracts/xml/schema/locomotive-config.xsd``.

A locomotive-config file contains one or more ``<locomotive>`` elements. Each
maps to a :class:`~jmri_core.beans.roster_entry.RosterEntry`. The mapping is
attribute-by-attribute and the round-trip is lossless for every field the
schema declares.

XML normalization rules (matching JMRI's Java writer):

* attribute order matches the order declared in the XSD;
* boolean attributes are written ``"yes"``/``"no"`` (not ``true``/``false``);
* missing optional values are *omitted* (not written as empty);
* element order: ``decoder, locoaddress, functionlabels, soundlabels,
  attributepairs``.
"""
from __future__ import annotations

import pathlib

from lxml import etree

from jmri_core.beans.roster_entry import FunctionLabel, RosterEntry, SoundLabel

# JMRI stores maxSpeed as a fraction 0..1; our model uses 0..100.
_DEFAULT_MAX_SPEED_PCT = 100

# Attribute order on <locomotive> -- matches the XSD declaration order.
_LOCO_ATTR_ORDER: tuple[str, ...] = (
    "id",
    "fileName",
    "roadNumber",
    "roadName",
    "mfg",
    "owner",
    "model",
    "dccAddress",
    "comment",
    "maxSpeed",
    "imageFilePath",
    "iconFilePath",
    "URL",
)


# ----- public API ----------------------------------------------------------


def load_locomotive_bytes(data: bytes) -> list[RosterEntry]:
    """Parse a locomotive-config XML byte string into RosterEntry objects."""
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(data, parser=parser)
    return _parse_root(root)


def load_locomotive(path: str | pathlib.Path) -> list[RosterEntry]:
    """Parse a locomotive-config XML file path."""
    p = pathlib.Path(path)
    return load_locomotive_bytes(p.read_bytes())


def save_locomotive_bytes(entries: list[RosterEntry]) -> bytes:
    """Serialize *entries* to locomotive-config XML bytes."""
    root = etree.Element("locomotive-config")
    for e in entries:
        root.append(_entry_to_element(e))
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
        standalone=False,
    )


def save_locomotive(path: str | pathlib.Path, entries: list[RosterEntry]) -> None:
    pathlib.Path(path).write_bytes(save_locomotive_bytes(entries))


# ----- parsing -------------------------------------------------------------


def _parse_root(root: etree._Element) -> list[RosterEntry]:
    if root.tag != "locomotive-config":
        raise ValueError(f"expected <locomotive-config>, got <{root.tag}>")
    return [_element_to_entry(loco) for loco in root.findall("locomotive")]


def _element_to_entry(loco: etree._Element) -> RosterEntry:
    attrs = loco.attrib

    # Sub-elements first; their values can override attribute fallbacks.
    decoder = loco.find("decoder")
    decoder_family = decoder.get("family", "") if decoder is not None else ""
    decoder_model  = decoder.get("model",  "") if decoder is not None else ""
    decoder_comment = decoder.get("comment", "") if decoder is not None else ""

    addr_block = loco.find("locoaddress")
    if addr_block is not None:
        ll = addr_block.find("dcclocoaddress")
        if ll is not None:
            dcc_address = ll.get("number", "3")
            long_address = ll.get("longaddress", "no") == "yes"
        else:
            dcc_address = attrs.get("dccAddress", "3")
            long_address = False
        protocol_elem = addr_block.find("protocol")
        protocol = protocol_elem.text or "dcc_short" if protocol_elem is not None else "dcc_short"
    else:
        dcc_address = attrs.get("dccAddress", "3")
        long_address = False
        protocol = "dcc_short"

    fls = loco.find("functionlabels")
    function_labels: list[FunctionLabel] = [
        FunctionLabel(
            number=int(fl.get("num", "0")),
            label=fl.text or "",
            lockable=_to_bool(fl.get("lockable", "true")),
            image=fl.get("functionImage"),
            selected_image=fl.get("functionImageSelected"),
        )
        for fl in (fls.findall("functionlabel") if fls is not None else [])
    ]

    sls = loco.find("soundlabels")
    sound_labels: list[SoundLabel] = [
        SoundLabel(number=int(sl.get("num", "0")), label=sl.text or "")
        for sl in (sls.findall("soundlabel") if sls is not None else [])
    ]

    attributes: dict[str, str] = {}
    for ap in loco.findall("attributepairs"):
        for kv in ap.findall("keyvaluepair"):
            k = kv.find("key")
            v = kv.find("value")
            if k is not None and v is not None:
                attributes[k.text or ""] = v.text or ""

    # Preserve unmodeled child elements (most importantly ``<values>`` --
    # the decoder CV state for in-roster-file storage). Without this,
    # editing a loco's road name in our UI would silently destroy its
    # programmed CVs on the next save.
    _modeled = {
        "decoder", "locoaddress", "functionlabels",
        "soundlabels", "attributepairs",
    }
    preserved_xml: list[str] = []
    for child in loco:
        if not isinstance(child.tag, str):
            # Comments / processing instructions -- preserve.
            preserved_xml.append(
                etree.tostring(child, encoding="unicode")
            )
            continue
        if child.tag in _modeled:
            continue
        preserved_xml.append(etree.tostring(child, encoding="unicode"))

    return RosterEntry(
        id=attrs.get("id", ""),
        file_name=attrs.get("fileName", ""),
        dcc_address=dcc_address,
        long_address=long_address,
        protocol=protocol,
        decoder_family=decoder_family,
        decoder_model=decoder_model,
        decoder_comment=decoder_comment,
        road_name=attrs.get("roadName", ""),
        road_number=attrs.get("roadNumber", ""),
        mfg=attrs.get("mfg", ""),
        owner=attrs.get("owner", ""),
        model=attrs.get("model", ""),
        comment=attrs.get("comment", ""),
        max_speed_pct=_max_speed_pct_from_attr(attrs.get("maxSpeed")),
        image_file_path=attrs.get("imageFilePath"),
        icon_file_path=attrs.get("iconFilePath"),
        url=attrs.get("URL"),
        function_labels=function_labels,
        sound_labels=sound_labels,
        attributes=attributes,
        preserved_xml=preserved_xml,
    )


# ----- serialization -------------------------------------------------------


def _entry_to_element(
    entry: RosterEntry,
    *,
    skip_preserved_tags: frozenset[str] = frozenset(),
) -> etree._Element:
    """Serialize *entry* to a ``<locomotive>`` element.

    *skip_preserved_tags* filters :attr:`RosterEntry.preserved_xml` by
    the root tag of each preserved fragment. Used by the roster-index
    writer to omit heavyweight per-loco content (``<values>`` CV state,
    ``<speedprofile>``) from ``roster.xml`` -- that data belongs only
    in the per-loco file. Empty set (the default) preserves everything.
    """
    loco = etree.Element("locomotive")

    raw_attrs: dict[str, str | None] = {
        "id": entry.id,
        "fileName": entry.file_name or None,
        "roadNumber": entry.road_number or None,
        "roadName": entry.road_name or None,
        "mfg": entry.mfg or None,
        "owner": entry.owner or None,
        "model": entry.model or None,
        "dccAddress": entry.dcc_address or None,
        "comment": entry.comment or None,
        "maxSpeed": (
            f"{entry.max_speed_pct / 100:.2f}"
            if entry.max_speed_pct != _DEFAULT_MAX_SPEED_PCT
            else None
        ),
        "imageFilePath": entry.image_file_path,
        "iconFilePath": entry.icon_file_path,
        "URL": entry.url,
    }
    for key in _LOCO_ATTR_ORDER:
        v = raw_attrs.get(key)
        if v is not None and v != "":
            loco.set(key, v)

    # <decoder>
    decoder = etree.SubElement(loco, "decoder")
    decoder.set("family", entry.decoder_family)
    decoder.set("model", entry.decoder_model)
    if entry.decoder_comment:
        decoder.set("comment", entry.decoder_comment)

    # <locoaddress>
    addr = etree.SubElement(loco, "locoaddress")
    dcc = etree.SubElement(addr, "dcclocoaddress")
    dcc.set("number", entry.dcc_address)
    dcc.set("longaddress", "yes" if entry.long_address else "no")
    proto = etree.SubElement(addr, "protocol")
    proto.text = entry.protocol

    # <functionlabels>
    if entry.function_labels:
        fls = etree.SubElement(loco, "functionlabels")
        for fl in entry.function_labels:
            el = etree.SubElement(fls, "functionlabel")
            el.set("num", str(fl.number))
            el.set("lockable", "true" if fl.lockable else "false")
            if fl.image:
                el.set("functionImage", fl.image)
            if fl.selected_image:
                el.set("functionImageSelected", fl.selected_image)
            el.text = fl.label

    # <soundlabels>
    if entry.sound_labels:
        sls = etree.SubElement(loco, "soundlabels")
        for sl in entry.sound_labels:
            el = etree.SubElement(sls, "soundlabel")
            el.set("num", str(sl.number))
            el.text = sl.label

    # <attributepairs>
    if entry.attributes:
        ap = etree.SubElement(loco, "attributepairs")
        for k, v in entry.attributes.items():
            kv = etree.SubElement(ap, "keyvaluepair")
            etree.SubElement(kv, "key").text = k
            etree.SubElement(kv, "value").text = v

    # Append preserved children verbatim (CV state, dateUpdated, etc.).
    for raw in entry.preserved_xml:
        try:
            elem = etree.fromstring(raw)
        except etree.XMLSyntaxError:
            continue
        # Comments / processing instructions have a callable tag; keep
        # them regardless of the skip filter.
        if isinstance(elem.tag, str) and elem.tag in skip_preserved_tags:
            continue
        loco.append(elem)

    return loco


# ----- helpers -------------------------------------------------------------


def _to_bool(s: str) -> bool:
    return s.lower() in ("yes", "true", "1")


def _max_speed_pct_from_attr(raw: str | None) -> int:
    """JMRI stores maxSpeed as a fraction 0..1; RosterEntry uses 0..100."""
    if raw is None or raw == "":
        return 100
    try:
        f = float(raw)
    except ValueError:
        return 100
    return max(0, min(100, round(f * 100)))
