"""``rosters/<id>.xml`` -- per-locomotive CV state file.

The locomotive-config file at ``roster/<id>.xml`` carries identity and
function labels; the *programmed CV state* of a specific decoder is
either embedded there as a ``<values>`` block (JMRI 5.x default) or
kept in a separate ``rosters/<id>.xml``. This module models the
separate-file form because that's the layout the alpha tester
explicitly asked for in their feedback:

> there is a rosters subdirectory that should *not* be read by default.
> It contains all the decoder cv settings for the locomotives listed in
> the roster. These should be loaded as needed by the programmer panel

The format we read/write:

    <?xml version="1.0" encoding="UTF-8"?>
    <locomotive-cv-values id="459">
      <CV name="1" value="3" />
      <CV name="7" value="151" />
      <CV name="29" value="38" />
    </locomotive-cv-values>

When the file doesn't exist yet, an empty :class:`LocoCvState` is
returned. The programmer page accumulates reads + writes into it and
flushes back to disk on every mutation.
"""
from __future__ import annotations

import pathlib

from lxml import etree

from pmri_core.beans.loco_cv_state import LocoCvState


def load_loco_cv_bytes(data: bytes) -> LocoCvState:
    """Parse a ``rosters/<id>.xml`` from bytes."""
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    root = etree.fromstring(data, parser=parser)
    if root.tag != "locomotive-cv-values":
        raise ValueError(
            f"expected <locomotive-cv-values> root, got <{root.tag}>"
        )
    loco_id = root.get("id") or ""
    cv_values: dict[str, int] = {}
    for cv in root.findall("CV"):
        name = cv.get("name")
        val = cv.get("value")
        if not name or val is None:
            continue
        try:
            cv_values[name] = int(val)
        except ValueError:
            # Skip malformed entries; don't blow up the rest of the file.
            continue
    return LocoCvState(loco_id=loco_id, cv_values=cv_values)


def load_loco_cv(path: str | pathlib.Path) -> LocoCvState:
    """Load a per-loco CV file. Returns an empty state if missing."""
    p = pathlib.Path(path)
    if not p.exists():
        # The id is the file stem (e.g. ``459.xml`` -> ``"459"``).
        return LocoCvState(loco_id=p.stem)
    return load_loco_cv_bytes(p.read_bytes())


def save_loco_cv_bytes(state: LocoCvState) -> bytes:
    """Serialize a :class:`LocoCvState` to XML."""
    root = etree.Element("locomotive-cv-values", attrib={"id": state.loco_id})
    # Sort by numeric CV when possible, fall back to lexicographic.
    def _key(name: str) -> tuple[int, str]:
        try:
            return (0, f"{int(name):05d}")
        except ValueError:
            return (1, name)
    for name in sorted(state.cv_values, key=_key):
        etree.SubElement(root, "CV", attrib={
            "name": name,
            "value": str(state.cv_values[name]),
        })
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
        standalone=False,
    )


def save_loco_cv(path: str | pathlib.Path, state: LocoCvState) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp + rename so a crash mid-write can't corrupt the file.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(save_loco_cv_bytes(state))
    tmp.replace(target)


# ----- reading CV state from a JMRI locomotive-config -------------------
#
# JMRI's real-world layout embeds CV state inside the roster entry file
# itself (``roster/<id>.xml``) under a ``<values>`` block. Our proprietary
# ``rosters/<id>.xml`` was a design choice that diverges from JMRI. When
# opening a real JMRI profile we must fall back to reading CV values
# from the roster entry, or the programmer view can't display CVs the
# user has already programmed.


def load_cv_values_from_locomotive_config(
    path: str | pathlib.Path, *, loco_id: str | None = None,
) -> LocoCvState | None:
    """Extract CV state from a ``locomotive-config`` file's ``<values>`` block.

    The JMRI-canonical location for per-loco CV state. Returns ``None`` if
    the file doesn't exist or has no ``<values>`` block; returns an
    (empty) :class:`LocoCvState` when the block is present but has no
    ``<CV>`` rows yet.

    *loco_id* narrows the search when the file lists multiple
    ``<locomotive>`` entries (rare -- JMRI writes one per file). When
    omitted, the first ``<locomotive>`` wins.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return None
    return load_cv_values_from_locomotive_config_bytes(
        p.read_bytes(), loco_id=loco_id,
    )


def save_cv_values_into_locomotive_config(
    path: str | pathlib.Path,
    cv_values: dict[str, int],
    *,
    loco_id: str | None = None,
) -> None:
    """Update or insert a ``<values>`` block inside a ``locomotive-config``
    file, preserving every other element.

    * If *path* doesn't exist, does nothing (caller is expected to have
      created the roster entry first via the roster service).
    * If *cv_values* is empty, an existing ``<values>`` block is removed
      entirely -- the file still round-trips through the roster loader.
    * When the file lists multiple locomotives (rare), *loco_id* picks
      which one gets the update; otherwise the first is used.

    Writes are atomic (tmp + rename) so a crash mid-write can't corrupt
    the roster entry.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    tree = etree.parse(str(p), parser=parser)
    root = tree.getroot()
    if root.tag != "locomotive-config":
        return
    target_loco = None
    for loco in root.iter("locomotive"):
        if loco_id is None or loco.get("id", "") == loco_id:
            target_loco = loco
            break
    if target_loco is None:
        return

    # Update in place: preserve any existing ``<values>`` sub-elements
    # that AREN'T our per-CV entries. In particular JMRI Java writes a
    # sibling ``<decoderDef>`` block inside ``<values>`` that tracks
    # per-variable human values; wiping the whole ``<values>`` block
    # (as an earlier revision did) destroys that data. Same story for
    # any other unrecognised child element we don't own.
    values_el = target_loco.find("values")
    if values_el is None:
        if not cv_values:
            return
        values_el = etree.SubElement(target_loco, "values")
    else:
        # Remove existing per-CV entries so we don't stack duplicates.
        # Accept both element shapes we've written across versions.
        for entry in list(values_el):
            if isinstance(entry.tag, str) and entry.tag in ("CVvalue", "CV"):
                values_el.remove(entry)

    def _key(name: str) -> tuple[int, str]:
        try:
            return (0, f"{int(name):05d}")
        except ValueError:
            return (1, name)

    if cv_values:
        # Emit ``<CVvalue>`` -- that's the tag name JMRI Java writes and
        # reads. Interop-safe: a file we write will round-trip through
        # JMRI Java, and a file JMRI Java wrote (with ``<CVvalue>``)
        # gets picked up by our reader too.
        #
        # lxml's ``pretty_print`` only formats truly-new subtrees; when
        # we append into an already-parsed element with existing
        # whitespace nodes it silently packs siblings onto one line.
        # Explicitly set ``.tail`` on each new entry to a newline +
        # indent so the file stays hand-editable and diff-friendly.
        # The last entry gets the closing-tag indent so ``</values>``
        # lines up under its parent.
        parent_depth = 0
        node = values_el
        while node is not None and node.tag != "locomotive-config":
            parent_depth += 1
            node = node.getparent()
        indent_unit = "  "
        entry_indent = "\n" + indent_unit * (parent_depth + 1)
        close_indent = "\n" + indent_unit * parent_depth
        sorted_names = sorted(cv_values, key=_key)
        # Make sure the last-existing child's tail (if any) leads into
        # the first new entry cleanly.
        if len(values_el) > 0:
            values_el[-1].tail = entry_indent
        for i, name in enumerate(sorted_names):
            el = etree.SubElement(values_el, "CVvalue", attrib={
                "name": name,
                "value": str(cv_values[name]),
            })
            el.tail = (
                close_indent if i == len(sorted_names) - 1 else entry_indent
            )
    elif len(values_el) == 0:
        # No per-CV entries left and no other children (decoderDef etc.)
        # -> drop the empty ``<values>`` element rather than leaving
        # dead XML behind.
        target_loco.remove(values_el)

    tmp = p.with_suffix(p.suffix + ".tmp")
    tree.write(
        str(tmp),
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
        standalone=False,
    )
    tmp.replace(p)


def load_cv_values_from_locomotive_config_bytes(
    data: bytes, *, loco_id: str | None = None,
) -> LocoCvState | None:
    """Bytes-in variant of :func:`load_cv_values_from_locomotive_config`."""
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError:
        return None
    if root.tag != "locomotive-config":
        return None
    for loco in root.iter("locomotive"):
        this_id = loco.get("id", "")
        if loco_id is not None and this_id != loco_id:
            continue
        values = loco.find("values")
        if values is None:
            return None
        cv_values: dict[str, int] = {}
        # Accept both element shapes:
        # * ``<CVvalue name=... value=.../>`` -- what real JMRI Java
        #   writes inside ``<values>``. This is the JMRI-canonical form.
        # * ``<CV name=... value=.../>`` -- what earlier versions of our
        #   own code wrote. Kept for backwards compat on files we've
        #   already touched.
        for tag in ("CVvalue", "CV"):
            for cv in values.iter(tag):
                name = cv.get("name")
                val = cv.get("value")
                if not name or val is None:
                    continue
                try:
                    cv_values[name] = int(val)
                except ValueError:
                    continue
        return LocoCvState(loco_id=this_id or (loco_id or ""), cv_values=cv_values)
    return None
