"""Configuration Description Information (CDI) -- S-9.7.4.3.

Every OpenLCB node ships an XML schema in memory space ``0xFF``
describing its configuration variables: which bytes at which offset
in memory space ``0xFE`` are what type, what they mean, and what
values they accept. A discovery / config UI reads the CDI, presents
the fields to the operator, and edits the raw bytes via the memory
protocol.

**What we support here**:

* Fetching the CDI XML from a peer (``fetch_cdi``) via the existing
  :class:`~pmri_openlcb.memory.MemoryClient`.
* Parsing it into a typed tree with automatically-computed byte
  offsets -- segments -> groups -> leaves.
* Leaf types: ``int`` (1/2/4/8 bytes), ``string`` (fixed-size UTF-8),
  ``eventid`` (8 bytes), ``float`` (4/8 bytes), ``bit`` (single bit
  inside a hosting byte).
* Group replication -- a ``<group replication="4">`` becomes 4 copies
  in the offset walk.
* Typed read/write of one field at a time
  (:func:`read_field` / :func:`write_field`).

**What we don't yet support**:

* Bitfields spanning multiple bytes.
* ``<map>`` enumeration presentation -- parsed but not enforced at
  write time (the caller can round-trip anything the raw ``int`` type
  allows). An editor UI is expected to reject out-of-map values on
  the way in.
* Rewriting XML (we only read).

XML parsing uses ``xml.etree.ElementTree`` -- safe for untrusted
input from Python 3.7.1+ (external entities disabled by default).
We additionally cap the fetch size at 256 KiB so a hostile or
broken node can't wedge us reading forever.
"""
from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pmri_openlcb.events import EventId
from pmri_openlcb.memory import SPACE_CDI, SPACE_CONFIG

if TYPE_CHECKING:
    from pmri_openlcb.memory import MemoryClient

# Absolute ceiling on CDI size. Real CDIs are ~2..10 KiB; 256 KiB
# is way beyond anything sane and stops a runaway fetch cold.
MAX_CDI_BYTES = 256 * 1024

# Chunk size when scanning for the null terminator. Larger blocks
# mean fewer round-trips on a big CDI but more wasted bytes when the
# CDI is small. 64 bytes matches the memory-config max chunk and
# hits a good tradeoff for a bus-attached fetch.
_FETCH_CHUNK = 64


# ---- model ----------------------------------------------------------------


@dataclass
class CdiIdentification:
    """The ``<identification>`` block near the top of a CDI. Same
    four strings as SNIP; some nodes populate one but not the other,
    so both are worth reading."""

    manufacturer: str = ""
    model: str = ""
    hardware_version: str = ""
    software_version: str = ""


@dataclass
class CdiValueMap:
    """An ``<int>`` with a ``<map>`` child is a value-labelled enum:
    each ``<relation>`` binds a numeric value to a display label.
    We preserve the mapping so an editor can offer a drop-down."""

    values: dict[int, str] = field(default_factory=dict)


@dataclass
class CdiField:
    """Base class for CDI leaves. ``size`` is the leaf's byte
    footprint in the config space; ``offset`` is filled in during
    tree walking as the running byte position."""

    name: str = ""
    description: str = ""
    size: int = 0
    offset: int = 0


@dataclass
class CdiInt(CdiField):
    """``<int size="N">`` -- signed integer of 1, 2, 4, or 8 bytes."""

    default: int | None = None
    min: int | None = None
    max: int | None = None
    value_map: CdiValueMap | None = None


@dataclass
class CdiString(CdiField):
    """``<string size="N">`` -- fixed-size UTF-8, null-padded."""

    default: str | None = None


@dataclass
class CdiEventId(CdiField):
    """``<eventid>`` -- always 8 bytes, holds an :class:`EventId`.

    ``size`` is fixed to 8; the field is here so it plays with the
    same base ``CdiField.offset`` bookkeeping as other leaves."""

    size: int = 8


@dataclass
class CdiFloat(CdiField):
    """``<float size="N">`` -- 4 or 8 bytes, big-endian IEEE-754."""


@dataclass
class CdiBit(CdiField):
    """``<bit>`` -- single bit inside a hosting byte at *offset*.
    ``size`` is 1 (the hosting byte); ``bit`` picks the position
    0..7. Only one CdiBit spans a byte in the offset walk."""

    bit: int = 0
    size: int = 1


@dataclass
class CdiGroup:
    """``<group>`` -- a container. ``replication`` > 1 means the
    group's ``children`` layout is repeated N times back-to-back in
    the segment. Repeated groups are already flattened in the model
    by copy -- the caller sees N distinct instances so ``read_field``
    can address any specific copy without extra indexing."""

    name: str = ""
    description: str = ""
    replication: int = 1
    replication_name: str = ""
    children: list[CdiEntry] = field(default_factory=list)
    offset: int = 0
    size: int = 0


@dataclass
class CdiSegment:
    """``<segment>`` -- a top-level container tied to a memory space.

    ``origin`` is the base address in ``space`` (usually 0 for the
    single-segment CDIs stock hardware exposes; multi-segment CDIs
    can share ``SPACE_CONFIG`` with different origins).
    """

    name: str = ""
    description: str = ""
    origin: int = 0
    space: int = SPACE_CONFIG
    children: list[CdiEntry] = field(default_factory=list)
    size: int = 0


CdiEntry = CdiInt | CdiString | CdiEventId | CdiFloat | CdiBit | CdiGroup


@dataclass
class Cdi:
    """A parsed CDI tree. Top-level identification + supported
    features + one or more segments."""

    identification: CdiIdentification = field(
        default_factory=CdiIdentification,
    )
    supports_acdi: bool = False
    segments: list[CdiSegment] = field(default_factory=list)


# ---- wire fetch ----------------------------------------------------------


async def fetch_cdi(
    client: MemoryClient,
    peer_alias: int,
    *,
    max_bytes: int = MAX_CDI_BYTES,
) -> Cdi:
    """Download the CDI XML from *peer_alias* and parse it.

    The CDI in memory space ``SPACE_CDI`` (0xFF) is null-terminated
    UTF-8 XML. We read in 64-byte chunks (matching the memory-config
    payload cap) until we hit the terminator, then hand the bytes to
    :func:`parse_cdi_bytes`.
    """
    buf = bytearray()
    cursor = 0
    while cursor < max_bytes:
        chunk = await client.read(peer_alias, SPACE_CDI, cursor, _FETCH_CHUNK)
        if not chunk:
            break
        term = chunk.find(b"\x00")
        if term >= 0:
            buf.extend(chunk[:term])
            break
        buf.extend(chunk)
        cursor += len(chunk)
        # A short chunk with no terminator signals end-of-space; a
        # peer that doesn't null-terminate is still fetched to the
        # end so we can attempt to parse whatever's there.
        if len(chunk) < _FETCH_CHUNK:
            break
    else:
        raise ValueError(
            f"CDI exceeded {max_bytes} bytes without hitting a null "
            "terminator -- refusing to keep reading",
        )
    return parse_cdi_bytes(bytes(buf))


# ---- parser --------------------------------------------------------------


def parse_cdi_bytes(data: bytes) -> Cdi:
    """Decode raw CDI bytes (UTF-8 XML) into a :class:`Cdi` tree.

    Tolerant of the ``<?xml version=...?>`` prologue being absent --
    some minimalist nodes ship a bare ``<cdi>`` root."""
    text = data.decode("utf-8", errors="replace").strip("\x00").strip()
    if not text:
        raise ValueError("CDI is empty")
    return parse_cdi(text)


def parse_cdi(xml_text: str) -> Cdi:
    """Parse a CDI XML string into a :class:`Cdi`. Walks segments
    to assign each field its byte ``offset`` (running position within
    the segment) and ``size``."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"CDI XML parse error: {e}") from e
    if root.tag != "cdi":
        raise ValueError(f"CDI root must be <cdi>, got <{root.tag}>")
    cdi = Cdi()
    for child in root:
        tag = child.tag
        if tag == "identification":
            cdi.identification = _parse_identification(child)
        elif tag == "acdi":
            cdi.supports_acdi = True
        elif tag == "segment":
            cdi.segments.append(_parse_segment(child))
        # else: ignore unknown top-level tags for forward compat.
    return cdi


def _parse_identification(el: ET.Element) -> CdiIdentification:
    return CdiIdentification(
        manufacturer=_text(el, "manufacturer"),
        model=_text(el, "model"),
        hardware_version=_text(el, "hardwareVersion"),
        software_version=_text(el, "softwareVersion"),
    )


def _parse_segment(el: ET.Element) -> CdiSegment:
    seg = CdiSegment(
        name=_text(el, "name"),
        description=_text(el, "description"),
        origin=int(el.get("origin", "0"), 0),
        space=int(el.get("space", str(SPACE_CONFIG)), 0),
        children=[],
    )
    seg.children, seg.size = _parse_children(el, start_offset=seg.origin)
    return seg


def _parse_children(
    el: ET.Element, *, start_offset: int,
) -> tuple[list[CdiEntry], int]:
    """Walk element ``el`` collecting its typed CDI children, filling
    in each field's ``offset``. Returns ``(children, next_offset)``
    so callers know how much space the block consumed for offset
    accounting in an outer scope."""
    children: list[CdiEntry] = []
    cursor = start_offset
    for c in el:
        entry = _parse_entry(c, cursor)
        if entry is None:
            continue
        children.append(entry)
        cursor += entry.size if not isinstance(entry, CdiGroup) else entry.size
    return children, cursor


def _parse_entry(el: ET.Element, offset: int) -> CdiEntry | None:
    """Recognise one CDI leaf/container element. Unknown tags
    (name/description/repname/etc. handled at the parent level) get
    ``None`` and are skipped."""
    tag = el.tag
    if tag == "int":
        return _parse_int(el, offset)
    if tag == "string":
        return _parse_string(el, offset)
    if tag == "eventid":
        return _parse_eventid(el, offset)
    if tag == "float":
        return _parse_float(el, offset)
    if tag == "bit":
        return _parse_bit(el, offset)
    if tag == "group":
        return _parse_group(el, offset)
    return None


def _parse_int(el: ET.Element, offset: int) -> CdiInt:
    size = int(el.get("size", "1"), 0)
    if size not in (1, 2, 4, 8):
        raise ValueError(f"<int size=...> must be 1/2/4/8, got {size}")
    value_map = None
    for child in el:
        if child.tag == "map":
            value_map = _parse_map(child)
    return CdiInt(
        name=_text(el, "name"),
        description=_text(el, "description"),
        size=size,
        offset=offset,
        default=_int_or_none(_text(el, "default")),
        min=_int_or_none(_text(el, "min")),
        max=_int_or_none(_text(el, "max")),
        value_map=value_map,
    )


def _parse_string(el: ET.Element, offset: int) -> CdiString:
    size = int(el.get("size", "0"), 0)
    if size <= 0:
        raise ValueError(f"<string size=...> must be positive, got {size}")
    return CdiString(
        name=_text(el, "name"),
        description=_text(el, "description"),
        size=size,
        offset=offset,
        default=_text(el, "default") or None,
    )


def _parse_eventid(el: ET.Element, offset: int) -> CdiEventId:
    return CdiEventId(
        name=_text(el, "name"),
        description=_text(el, "description"),
        offset=offset,
    )


def _parse_float(el: ET.Element, offset: int) -> CdiFloat:
    size = int(el.get("size", "4"), 0)
    if size not in (4, 8):
        raise ValueError(f"<float size=...> must be 4 or 8, got {size}")
    return CdiFloat(
        name=_text(el, "name"),
        description=_text(el, "description"),
        size=size,
        offset=offset,
    )


def _parse_bit(el: ET.Element, offset: int) -> CdiBit:
    bit = int(el.get("bit", "0"), 0)
    if not 0 <= bit < 8:
        raise ValueError(f"<bit bit=...> must be 0..7, got {bit}")
    return CdiBit(
        name=_text(el, "name"),
        description=_text(el, "description"),
        offset=offset,
        bit=bit,
    )


def _parse_group(el: ET.Element, offset: int) -> CdiGroup:
    replication = int(el.get("replication", "1"), 0)
    if replication < 1:
        raise ValueError(f"<group replication=...> must be >=1, got {replication}")
    # One layout pass gives us the group's per-copy children +
    # per-copy size. Repetition is expressed by cloning children
    # and re-offsetting.
    template_children, per_copy_size = _parse_children(el, start_offset=0)
    total_size = per_copy_size * replication
    combined: list[CdiEntry] = []
    for copy_i in range(replication):
        base = offset + copy_i * per_copy_size
        for tpl in template_children:
            combined.append(_reoffset(tpl, base + tpl.offset))
    return CdiGroup(
        name=_text(el, "name"),
        description=_text(el, "description"),
        replication=replication,
        replication_name=_text(el, "repname"),
        children=combined,
        offset=offset,
        size=total_size,
    )


def _reoffset(entry: CdiEntry, new_offset: int) -> CdiEntry:
    """Clone *entry* with an updated ``offset``. Used to lay out
    replicated groups without hand-writing a copy per type."""
    if isinstance(entry, CdiGroup):
        # Shift every child by the same delta.
        delta = new_offset - entry.offset
        return CdiGroup(
            name=entry.name,
            description=entry.description,
            replication=entry.replication,
            replication_name=entry.replication_name,
            children=[
                _reoffset(c, c.offset + delta) for c in entry.children
            ],
            offset=new_offset,
            size=entry.size,
        )
    # Leaf types: dataclasses.replace-alike, but we hand-roll to
    # keep the module free of the replace import.
    if isinstance(entry, CdiInt):
        return CdiInt(
            name=entry.name, description=entry.description,
            size=entry.size, offset=new_offset,
            default=entry.default, min=entry.min, max=entry.max,
            value_map=entry.value_map,
        )
    if isinstance(entry, CdiString):
        return CdiString(
            name=entry.name, description=entry.description,
            size=entry.size, offset=new_offset, default=entry.default,
        )
    if isinstance(entry, CdiEventId):
        return CdiEventId(
            name=entry.name, description=entry.description,
            offset=new_offset,
        )
    if isinstance(entry, CdiFloat):
        return CdiFloat(
            name=entry.name, description=entry.description,
            size=entry.size, offset=new_offset,
        )
    if isinstance(entry, CdiBit):
        return CdiBit(
            name=entry.name, description=entry.description,
            offset=new_offset, bit=entry.bit,
        )
    raise TypeError(f"unrecognised CDI entry: {type(entry).__name__}")


def _parse_map(el: ET.Element) -> CdiValueMap:
    values: dict[int, str] = {}
    for relation in el.findall("relation"):
        prop = relation.findtext("property")
        val = relation.findtext("value")
        if prop is None or val is None:
            continue
        try:
            values[int(prop, 0)] = val
        except ValueError:
            continue
    return CdiValueMap(values=values)


def _text(el: ET.Element, tag: str) -> str:
    """Return the direct-child ``<tag>`` text, empty string if absent."""
    child = el.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _int_or_none(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        return None


# ---- typed value access --------------------------------------------------


CdiValue = int | str | EventId | float | bool


async def read_field(
    client: MemoryClient,
    peer_alias: int,
    segment: CdiSegment,
    field_: CdiField,
) -> CdiValue:
    """Read one CDI field's current value from the peer's config space.

    Type dispatch:

    * :class:`CdiInt` -> signed int (big-endian, 1/2/4/8 bytes)
    * :class:`CdiString` -> UTF-8 with trailing nulls stripped
    * :class:`CdiEventId` -> :class:`EventId`
    * :class:`CdiFloat` -> float (big-endian IEEE-754)
    * :class:`CdiBit` -> bool (bit at ``field.bit`` of the hosting byte)

    Reads happen in *segment.space* -- usually ``SPACE_CONFIG`` -- at
    ``field_.offset``. The offset was computed at parse time so it
    already includes ``segment.origin``.
    """
    raw = await client.read(
        peer_alias, segment.space, field_.offset, field_.size,
    )
    if len(raw) < field_.size:
        raise ValueError(
            f"short read for CDI field {field_.name!r}: expected "
            f"{field_.size} bytes at offset {field_.offset:#x}, "
            f"got {len(raw)}",
        )
    return _decode(raw, field_)


async def write_field(
    client: MemoryClient,
    peer_alias: int,
    segment: CdiSegment,
    field_: CdiField,
    value: CdiValue,
) -> None:
    """Encode *value* for *field_*'s type and write it to the peer.

    Bit fields do a read-modify-write against the hosting byte so
    neighbouring bits aren't clobbered -- a plain "set byte to 0/1"
    would race with any other bit in the same byte."""
    if isinstance(field_, CdiBit):
        # Read the hosting byte, flip the target bit, write it back.
        hosting = await client.read(
            peer_alias, segment.space, field_.offset, 1,
        )
        base = hosting[0] if hosting else 0
        mask = 1 << field_.bit
        if bool(value):
            base |= mask
        else:
            base &= ~mask & 0xFF
        await client.write(
            peer_alias, segment.space, field_.offset, bytes([base]),
        )
        return
    encoded = _encode(value, field_)
    await client.write(
        peer_alias, segment.space, field_.offset, encoded,
    )


def _decode(raw: bytes, field_: CdiField) -> CdiValue:
    if isinstance(field_, CdiInt):
        return int.from_bytes(raw, "big", signed=True)
    if isinstance(field_, CdiString):
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    if isinstance(field_, CdiEventId):
        return EventId(int.from_bytes(raw, "big"))
    if isinstance(field_, CdiFloat):
        fmt = ">f" if field_.size == 4 else ">d"
        return struct.unpack(fmt, raw)[0]
    if isinstance(field_, CdiBit):
        return bool(raw[0] & (1 << field_.bit))
    raise TypeError(f"cannot decode {type(field_).__name__}")


def _encode(value: CdiValue, field_: CdiField) -> bytes:
    if isinstance(field_, CdiInt):
        if not isinstance(value, int):
            raise TypeError(
                f"CdiInt {field_.name!r} expects int, got {type(value).__name__}",
            )
        return int(value).to_bytes(field_.size, "big", signed=True)
    if isinstance(field_, CdiString):
        if not isinstance(value, str):
            raise TypeError(
                f"CdiString {field_.name!r} expects str, "
                f"got {type(value).__name__}",
            )
        raw = value.encode("utf-8")
        if len(raw) > field_.size:
            raise ValueError(
                f"CdiString {field_.name!r} value too long: "
                f"{len(raw)} bytes > {field_.size} slot",
            )
        return raw.ljust(field_.size, b"\x00")
    if isinstance(field_, CdiEventId):
        if not isinstance(value, EventId):
            raise TypeError(
                f"CdiEventId {field_.name!r} expects EventId, "
                f"got {type(value).__name__}",
            )
        return value.value.to_bytes(8, "big")
    if isinstance(field_, CdiFloat):
        fmt = ">f" if field_.size == 4 else ">d"
        return struct.pack(fmt, float(value))
    if isinstance(field_, CdiBit):
        # Handled by write_field's read-modify-write branch; encode()
        # only sees a plain-value type from here.
        raise TypeError("CdiBit values are handled by write_field, not _encode")
    raise TypeError(f"cannot encode {type(field_).__name__}")


# ---- raw-coordinate accessors ------------------------------------------

# Type discriminators the wire-level accessors accept. The JSON topic
# surface uses these strings as-is so a UI client doesn't have to
# import Cdi model classes just to read one field.
FIELD_TYPES = ("int", "string", "eventid", "float", "bit")


def _field_for_type(
    type_name: str, *, size: int, bit: int = 0,
) -> CdiField:
    """Build a shim :class:`CdiField` for the given *type_name*.

    Used by :func:`read_field_raw` / :func:`write_field_raw` to drive
    :func:`_decode` / :func:`_encode` without needing a parsed Cdi
    tree. Sizes are enforced per type (matches the parser rules)."""
    if type_name == "int":
        if size not in (1, 2, 4, 8):
            raise ValueError(
                f"int field size must be 1/2/4/8, got {size}",
            )
        return CdiInt(name="", size=size, offset=0)
    if type_name == "string":
        if size <= 0:
            raise ValueError(
                f"string field size must be positive, got {size}",
            )
        return CdiString(name="", size=size, offset=0)
    if type_name == "eventid":
        return CdiEventId(name="", offset=0)
    if type_name == "float":
        if size not in (4, 8):
            raise ValueError(
                f"float field size must be 4 or 8, got {size}",
            )
        return CdiFloat(name="", size=size, offset=0)
    if type_name == "bit":
        if not 0 <= bit < 8:
            raise ValueError(f"bit position must be 0..7, got {bit}")
        return CdiBit(name="", offset=0, bit=bit)
    raise ValueError(
        f"unknown CDI field type {type_name!r}; supported: {FIELD_TYPES}",
    )


async def read_field_raw(
    client: MemoryClient,
    peer_alias: int,
    *,
    space: int,
    offset: int,
    size: int,
    type_name: str,
    bit: int = 0,
) -> CdiValue:
    """Read one CDI field by explicit wire coordinates.

    Same decode dispatch as :func:`read_field`, but takes wire args
    directly so a caller that doesn't have a parsed :class:`Cdi`
    handy (typically the JSON topic layer) can still read fields.
    *space* + *offset* + *size* address the raw bytes; *type_name*
    picks the decoder; *bit* is the position (0..7) for
    ``type_name="bit"``.
    """
    shim = _field_for_type(type_name, size=size, bit=bit)
    raw = await client.read(peer_alias, space, offset, size)
    if len(raw) < size:
        raise ValueError(
            f"short read at {space:#04x}:{offset:#06x} -- "
            f"expected {size} bytes, got {len(raw)}",
        )
    return _decode(raw, shim)


async def write_field_raw(
    client: MemoryClient,
    peer_alias: int,
    *,
    space: int,
    offset: int,
    size: int,
    type_name: str,
    value: CdiValue,
    bit: int = 0,
) -> None:
    """Write one CDI field by explicit wire coordinates. Bit fields
    do the same read-modify-write dance as :func:`write_field` so
    neighbouring bits survive."""
    shim = _field_for_type(type_name, size=size, bit=bit)
    if isinstance(shim, CdiBit):
        hosting = await client.read(peer_alias, space, offset, 1)
        base = hosting[0] if hosting else 0
        mask = 1 << bit
        if bool(value):
            base |= mask
        else:
            base &= ~mask & 0xFF
        await client.write(peer_alias, space, offset, bytes([base]))
        return
    await client.write(peer_alias, space, offset, _encode(value, shim))
