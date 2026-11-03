"""Function Description Information (FDI) -- S-9.7.4.9.

Loco nodes optionally expose an XML description of their functions
in memory space ``0xFA``. Each ``<function>`` entry maps a DCC
function number to a human-readable label + behaviour hints so a
throttle UI can render "Bell" instead of "F1" and know that the
horn button is momentary.

Wire fetch is identical to CDI: null-terminated UTF-8 XML read from
``SPACE_FDI`` in 64-byte chunks. Parsing yields a small tree --
segments -> groups -> :class:`FdiFunction` leaves.

XML shape (typical)::

    <?xml version="1.0"?>
    <fdi>
      <segment>
        <group>
          <name>Functions</name>
          <function size="1" kind="binary">
            <name>Headlight</name>
            <number>0</number>
          </function>
          <function size="1" kind="binary">
            <name>Bell</name>
            <number>1</number>
            <momentary/>
          </function>
          <function size="1" kind="analog">
            <name>Volume</name>
            <number>7</number>
            <min>0</min>
            <max>127</max>
          </function>
        </group>
      </segment>
    </fdi>

The MemoryClient side is shared with CDI -- same read semantics, same
size cap -- so a client wanting both fetches them independently via
``fetch_cdi(...)`` and ``fetch_fdi(...)``.

Cross-referenced against openlcb-python's ``FDIParser`` module and
the S-9.7.4.9 rev 1.0 draft. Real-world decoder FDI varies: some
omit ``<segment>``/``<group>`` wrappers, some skip the ``kind``
attribute. The parser is lenient -- missing wrappers get synthesized;
missing attributes default to ``"binary"`` + size 1.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmri_openlcb.memory import MemoryClient

_log = logging.getLogger(__name__)

# Memory space FDI lives in -- 0xFA per S-9.7.4.9 sec.3.
SPACE_FDI = 0xFA

# Hard cap on the fetch size. FDIs are tiny (a few hundred bytes
# for a full 28-function decoder); 64 KiB is way beyond anything
# sane and stops a runaway fetch on a broken node.
MAX_FDI_BYTES = 64 * 1024

_FETCH_CHUNK = 64


# ---- model ---------------------------------------------------------------


@dataclass
class FdiFunction:
    """One function on a loco.

    ``number`` is the DCC F# (0..28+). ``size`` is bytes on the wire
    (1 for binary, 1..2 for analog). ``kind`` is ``"binary"`` or
    ``"analog"``. ``momentary`` hints that the function is "on
    while pressed" rather than a latching toggle. ``min`` / ``max``
    are only populated for analog functions.
    """

    number: int = 0
    name: str = ""
    kind: str = "binary"
    size: int = 1
    momentary: bool = False
    min: int | None = None
    max: int | None = None


@dataclass
class FdiGroup:
    """Container for a set of functions. Groups may nest, matching
    the CDI convention. ``name`` and ``description`` are hints for
    a UI (e.g. "Sound functions" vs "Motion functions")."""

    name: str = ""
    description: str = ""
    functions: list[FdiFunction] = field(default_factory=list)
    groups: list["FdiGroup"] = field(default_factory=list)


@dataclass
class FdiSegment:
    """Top-level container. Most FDIs have exactly one segment; the
    field exists because the spec allows multi-segment layouts (rare
    in practice)."""

    name: str = ""
    description: str = ""
    groups: list[FdiGroup] = field(default_factory=list)


@dataclass
class Fdi:
    """Parsed function description for one loco."""

    segments: list[FdiSegment] = field(default_factory=list)

    def flatten(self) -> list[FdiFunction]:
        """Return every function across every segment/group in
        declaration order. Convenient for a throttle UI that wants
        a flat list keyed by function number rather than walking
        the nested tree."""
        out: list[FdiFunction] = []
        for segment in self.segments:
            _walk_group_list(segment.groups, out)
        return out

    def by_number(self, number: int) -> FdiFunction | None:
        """Return the (first) FdiFunction with the given F# number,
        or ``None`` if the loco doesn't describe that function.
        Multiple defs at the same number is a spec violation; we
        return the first one and let the caller sort out the mess."""
        for fn in self.flatten():
            if fn.number == number:
                return fn
        return None


def _walk_group_list(groups: list[FdiGroup], out: list[FdiFunction]) -> None:
    for g in groups:
        out.extend(g.functions)
        _walk_group_list(g.groups, out)


# ---- wire fetch ----------------------------------------------------------


async def fetch_fdi(
    client: MemoryClient,
    peer_alias: int,
    *,
    max_bytes: int = MAX_FDI_BYTES,
) -> Fdi:
    """Download the FDI XML from *peer_alias* and parse it.

    Same read strategy as :func:`~pmri_openlcb.cdi.fetch_cdi`: 64-byte
    memory-config reads until the null terminator. ``max_bytes``
    caps the fetch so a peer that never terminates can't wedge us."""
    buf = bytearray()
    cursor = 0
    while cursor < max_bytes:
        chunk = await client.read(peer_alias, SPACE_FDI, cursor, _FETCH_CHUNK)
        if not chunk:
            break
        term = chunk.find(b"\x00")
        if term >= 0:
            buf.extend(chunk[:term])
            break
        buf.extend(chunk)
        cursor += len(chunk)
        if len(chunk) < _FETCH_CHUNK:
            break
    else:
        raise ValueError(
            f"FDI exceeded {max_bytes} bytes without hitting a null "
            "terminator -- refusing to keep reading",
        )
    return parse_fdi_bytes(bytes(buf))


# ---- parser --------------------------------------------------------------


def parse_fdi_bytes(data: bytes) -> Fdi:
    """Decode raw FDI bytes (UTF-8 XML) into an :class:`Fdi` tree."""
    text = data.decode("utf-8", errors="replace").strip("\x00").strip()
    if not text:
        raise ValueError("FDI is empty")
    return parse_fdi(text)


def parse_fdi(xml_text: str) -> Fdi:
    """Parse a FDI XML string into an :class:`Fdi`.

    Lenient about structure:

    * Missing ``<segment>`` wrapper -- top-level ``<group>`` /
      ``<function>`` elements get wrapped in a synthetic segment.
    * Missing ``<group>`` wrapper -- top-level ``<function>`` gets
      wrapped in a synthetic group.
    * Missing ``kind`` attribute -- defaults to ``"binary"``.
    * Missing ``size`` attribute -- defaults to 1.

    Malformed XML raises :class:`ValueError`; anything past that
    the parser tries to render into whatever tree it can."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"FDI XML parse error: {e}") from e
    if root.tag != "fdi":
        raise ValueError(f"FDI root must be <fdi>, got <{root.tag}>")
    fdi = Fdi()
    for child in root:
        if child.tag == "segment":
            fdi.segments.append(_parse_segment(child))
        elif child.tag == "group":
            # No <segment> wrapper -- synthesize one.
            _ensure_first_segment(fdi).groups.append(_parse_group(child))
        elif child.tag == "function":
            # No wrappers at all -- group + segment implicit.
            seg = _ensure_first_segment(fdi)
            if not seg.groups:
                seg.groups.append(FdiGroup())
            seg.groups[0].functions.append(_parse_function(child))
        # else: ignore unknown top-level tags for forward compat.
    return fdi


def _ensure_first_segment(fdi: Fdi) -> FdiSegment:
    if not fdi.segments:
        fdi.segments.append(FdiSegment())
    return fdi.segments[0]


def _parse_segment(el: ET.Element) -> FdiSegment:
    seg = FdiSegment(
        name=_text(el, "name"),
        description=_text(el, "description"),
    )
    for child in el:
        if child.tag == "group":
            seg.groups.append(_parse_group(child))
        elif child.tag == "function":
            # Direct function under segment -- wrap in a synthetic
            # group so the flat/tree APIs behave consistently.
            if not seg.groups:
                seg.groups.append(FdiGroup())
            seg.groups[0].functions.append(_parse_function(child))
    return seg


def _parse_group(el: ET.Element) -> FdiGroup:
    grp = FdiGroup(
        name=_text(el, "name"),
        description=_text(el, "description"),
    )
    for child in el:
        if child.tag == "function":
            grp.functions.append(_parse_function(child))
        elif child.tag == "group":
            grp.groups.append(_parse_group(child))
    return grp


def _parse_function(el: ET.Element) -> FdiFunction:
    size = int(el.get("size", "1"), 0)
    kind = el.get("kind", "binary")
    if kind not in ("binary", "analog"):
        # Silently coerce unknown kinds to binary -- real-world FDI
        # sometimes uses vendor-specific kinds that behave like
        # binary from the throttle's perspective.
        kind = "binary"
    return FdiFunction(
        number=_int_or_zero(_text(el, "number")),
        name=_text(el, "name"),
        kind=kind,
        size=size,
        momentary=el.find("momentary") is not None,
        min=_int_or_none(_text(el, "min")),
        max=_int_or_none(_text(el, "max")),
    )


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _int_or_zero(s: str) -> int:
    if not s:
        return 0
    try:
        return int(s, 0)
    except ValueError:
        return 0


def _int_or_none(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        return None
