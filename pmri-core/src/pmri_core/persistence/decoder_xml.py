"""Decoder definition XML loader -- read-only.

Each file under ``xml/decoders/*.xml`` describes one decoder family (one or
more models) plus the CVs each model exposes. JMRI ships 881 such files.

Stage 2 ships a **read-only** parser: enough to enumerate manufacturers,
families, models, and CV variables, so Stage 3+ can serve roster data and
Stage 8 (DecoderPro) can build a UI on top. Editing/writing decoder XML is
not part of this stage -- JMRI itself is the source of truth for these files.

The model intentionally captures only the major attributes. The full schema
``decoder-4-15-2.xsd`` declares dozens of element types; rather than model
every one, we expose:

* family + model identity (vendor, family name, model name, connector type);
* per-model attributes JMRI cares about for roster matching;
* a flat list of :class:`DecoderVariable` entries (CV definitions);
* the raw lxml ``Element`` via :attr:`DecoderFile.tree` for callers who need
  to reach into uncaptured corners (programmer pane layouts, etc.).
"""
from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)


class DecoderOutput(BaseModel):
    """Function-output mapping declared by a model."""
    name: str
    label: str = ""


# ----- variable value-type modelling ------------------------------------


class VariableKind(StrEnum):
    """The value-type child element declared inside a ``<variable>``.

    ``UNKNOWN`` is the parser's fallback when a variable declares something
    we don't model yet; downstream code treats it as an opaque byte.
    """
    DECIMAL         = "decimal"          # <decVal>
    HEXADECIMAL     = "hexadecimal"      # <hexVal>
    ENUM            = "enum"             # <enumVal>/<enumChoice>
    SPEED_TABLE     = "speed_table"      # <speedTable>
    SHORT_ADDRESS   = "short_address"    # <shortAddressVal> (CV1)
    LONG_ADDRESS    = "long_address"     # <longAddressVal>  (CV17+CV18)
    SPLIT           = "split"            # <splitVal>
    SPLIT_HEX       = "split_hex"        # <splitHexVal>
    SPLIT_DATETIME  = "split_datetime"   # <splitDateTimeVal>
    COMPOSITE       = "composite"        # <compositeVal>
    CONSTANT        = "constant"         # <constant>
    UNKNOWN         = "unknown"


class EnumChoice(BaseModel):
    """One ``<enumChoice value="V">text</enumChoice>`` option.

    JMRI allows the ``value`` attribute to be omitted, in which case the
    positional index (0, 1, 2, ...) is the wire value. The parser fills
    that in so downstream code can treat all choices uniformly.
    """
    value: int
    choice: str = ""
    choice_by_lang: dict[str, str] = Field(default_factory=dict)


class CompositeSetting(BaseModel):
    """One ``<compositeSetting label="X" value="Y"/>`` inside a compositeChoice.

    ``label`` references another variable via its ``item`` name; picking
    this composite choice writes ``value`` into that referenced variable.
    """
    label: str
    value: int


class CompositeChoice(BaseModel):
    """One ``<compositeChoice choice="X"><compositeSetting/>...</compositeChoice>``.

    Composite variables are meta-variables -- a user pick fans out into
    multiple sub-variable writes. The write path needs the session-level
    queue from step 4 to batch those atomically; step 2 just models the
    structure.
    """
    choice: str
    choice_by_lang: dict[str, str] = Field(default_factory=dict)
    settings: list[CompositeSetting] = Field(default_factory=list)


class CvBitMask(BaseModel):
    """Parsed ``<variable mask="XXVXXXXX">`` notation.

    JMRI masks are written MSB -> LSB with ``V`` marking bits this
    variable owns and ``X`` marking don't-cares. ``"XXVXXXXX"`` means
    bit 5 (``0b00100000``).

    * ``shift`` -- position of the LSB of the ``V``-region (from bit 0)
    * ``width`` -- number of ``V`` bits
    * ``mask_value`` -- the raw byte mask for the ``V``-region
    """
    model_config = ConfigDict(frozen=True)

    raw: str = ""
    shift: int = 0
    width: int = 8

    @classmethod
    def parse(cls, s: str | None) -> CvBitMask:
        """Turn a JMRI mask string into a structured mask.

        Empty / all-``X`` / missing masks give a full-byte mask (shift=0,
        width=8) -- that's what JMRI treats as "no mask" and it means the
        variable owns the whole byte.
        """
        if not s:
            return cls(raw="", shift=0, width=8)
        s = s.strip()
        if not s or "V" not in s:
            return cls(raw=s, shift=0, width=len(s) or 8)
        total = len(s)
        first_v = s.find("V")
        last_v = s.rfind("V")
        shift = total - 1 - last_v
        width = last_v - first_v + 1
        return cls(raw=s, shift=shift, width=width)

    @property
    def mask_value(self) -> int:
        """The byte mask that isolates this variable's V-region."""
        return ((1 << self.width) - 1) << self.shift

    def extract(self, byte: int) -> int:
        """Read this variable's value out of a raw byte."""
        return (byte & self.mask_value) >> self.shift

    def apply(self, byte: int, value: int) -> int:
        """Stuff *value* into the V-region of *byte*, preserving other bits.

        The read-modify-write step the caller performs before writing
        back: fetch the current byte, call ``apply(byte, new_value)``,
        write the resulting byte. Values wider than ``width`` bits are
        truncated to the region size.
        """
        room = (1 << self.width) - 1
        return (byte & ~self.mask_value) | ((value & room) << self.shift)


class SplitCv(BaseModel):
    """Split-CV addressing parsed from ``<variable CV="...">`` attribute strings.

    Three JMRI-supported shapes:

    * ``"1"`` -- one CV, single byte
    * ``"17.18"`` -- long-address pattern: CV17 is the high byte
      (with ``0xC0`` bits set), CV18 is the low byte
    * ``"0.255.261"`` -- bank/index/base indexed access (ESU-style)
    * ``"0.255.261:4"`` -- same as above, 4 consecutive bytes

    ``cv_list`` gives the ordered CVs the variable touches, in
    high-byte-first order (the read/write path iterates them).
    """
    model_config = ConfigDict(frozen=True)

    cv_attr: str
    primary_cv: int = 0
    byte_count: int = 1
    bank: int | None = None
    index: int | None = None

    @classmethod
    def parse(cls, s: str) -> SplitCv:
        if not s:
            return cls(cv_attr="", primary_cv=0, byte_count=1)
        s = s.strip()
        if ":" in s:
            head, count = s.split(":", 1)
            try:
                byte_count = max(1, int(count))
            except ValueError:
                byte_count = 1
        else:
            head = s
            byte_count = 1
        parts = head.split(".")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return cls(cv_attr=s, primary_cv=0, byte_count=byte_count)
        if len(nums) == 1:
            return cls(cv_attr=s, primary_cv=nums[0], byte_count=byte_count)
        if len(nums) == 2:
            # Two adjacent CVs -- the long-address pattern (CV17.CV18)
            # or any two-byte pair.
            return cls(
                cv_attr=s, primary_cv=nums[0],
                byte_count=max(byte_count, 2),
            )
        # 3+ parts: bank.index.base
        return cls(
            cv_attr=s,
            bank=nums[0], index=nums[1],
            primary_cv=nums[2],
            byte_count=byte_count,
        )

    @property
    def cv_list(self) -> list[int]:
        """The CVs this variable touches, in high-byte-first order."""
        return [self.primary_cv + i for i in range(self.byte_count)]


class DecoderVariable(BaseModel):
    """One CV (or CV-group) variable as declared in ``<variables>``.

    Backwards-compatible fields (``cv`` through ``ops_only``) stay
    exactly as parsed in earlier stages. Step 2 adds:

    * ``kind`` -- which value-type child element the variable declared
    * ``bit_mask`` -- parsed :class:`CvBitMask` (never None; a variable
      without a mask still has a full-byte mask)
    * ``enum_choices`` -- ordered choices for ENUM variables
    * ``composite_choices`` -- ordered choices for COMPOSITE variables
    * ``split`` -- parsed :class:`SplitCv` for SPLIT/SPLIT_HEX/etc
    * ``constant_value`` -- for the CONSTANT kind
    * ``read_only`` / ``write_only`` -- attribute flags
    * ``tooltip`` -- hover text
    """
    model_config = ConfigDict(populate_by_name=True)

    cv: str = Field(alias="CV", default="")
    item: str = ""
    # ``<variable default="N">`` -- the *human* default value the
    # decoder ships with (for a masked-bit variable, the masked
    # value; for a decimal variable, the whole byte). Parsed to an
    # ``int`` at load time via :func:`_parse_default_int` so downstream
    # consumers (default-overlay in the pane render, diff-filter in
    # LocoCvStore, CV7 identity match) don't have to re-parse on
    # every access. ``None`` when the attribute is absent or the value
    # isn't a plain integer.
    default: int | None = None
    mask: str | None = None
    min: str | None = None
    max: str | None = None
    label: str = ""
    ops_only: bool = False

    # step-2 additions
    kind: VariableKind = VariableKind.UNKNOWN
    bit_mask: CvBitMask = Field(default_factory=CvBitMask)
    enum_choices: list[EnumChoice] = Field(default_factory=list)
    composite_choices: list[CompositeChoice] = Field(default_factory=list)
    split: SplitCv | None = None
    constant_value: int | None = None
    read_only: bool = False
    write_only: bool = False
    tooltip: str = ""
    # SPEED_TABLE only: number of speed-step entries the table covers.
    # NMRA standard is 28 (CV67..CV94); some decoders declare a 14-step
    # or 128-step variant via ``<speedTable entries="N">``.
    speed_table_entries: int = 28
    # SPLIT_DATETIME only. Mirrors JMRI Java's
    # ``SplitDateTimeVariableValue`` extra1..4 attributes (see
    # ``~/Documents/jmri.jar.src/jmri/jmrit/symbolicprog/SplitDateTimeVariableValue.java``):
    #
    # * ``dt_base`` -- ISO-8601 base datetime (``LocalDateTime.parse``
    #   format), e.g. ``"2000-01-01T00:00:00"``. The decoded value is
    #   this base plus ``storedValue * dt_factor`` in units of
    #   ``dt_unit``.
    # * ``dt_factor`` -- multiplier on the stored integer before adding
    #   to the base.
    # * ``dt_unit`` -- ChronoUnit name (``SECONDS``, ``MINUTES``,
    #   ``HOURS``, ``DAYS``, ...).
    # * ``dt_display`` -- ``"dateOnly"`` / ``"timeOnly"`` /
    #   anything-else (= date + time). Controls rendering.
    #
    # Default empty strings signal "attribute not declared"; the pane
    # widget falls back to showing the raw stored integer.
    dt_base: str = ""
    dt_factor: int = 1
    dt_unit: str = ""
    dt_display: str = ""
    # ``<variable include="pid1,pid2,...">`` / ``exclude="..."``: show this
    # variable only for models whose ``productID`` is in the include
    # list (or NOT in the exclude list). Empty means "no filter".
    # SoundTraxx Tsunami2 uses this heavily so a single decoder file
    # can serve dozens of model revisions: e.g. HEP Mode / Turbine
    # Start-Stop / Stop Request Bell share a CV and take turns being
    # applicable per productID.
    include_product_ids: list[str] = Field(default_factory=list)
    exclude_product_ids: list[str] = Field(default_factory=list)
    # ``<qualifier><variableref>X</variableref><relation>Y</relation>
    # <value>N</value></qualifier>`` -- runtime visibility gate. All
    # qualifiers must pass (AND) for the variable to be shown. Common
    # relations: ``eq``, ``ne``, ``gt``, ``ge``, ``lt``, ``le``,
    # ``exists``. SoundTraxx uses this to split a shared CV across
    # multiple named variables depending on another CV's current
    # value (HEP Mode / HEP Mode2 / Turbine Start-Stop pattern).
    qualifiers: list[dict[str, str]] = Field(default_factory=list)


class DecoderModel(BaseModel):
    """One model within a family."""
    model: str
    connector: str = ""
    show_empty_panes: str | None = None
    max_input_volts: str | None = None
    max_motor_current: str | None = None
    outputs: list[DecoderOutput] = Field(default_factory=list)
    protocols: list[str] = Field(default_factory=list)
    size_length: str | None = None
    size_width: str | None = None
    size_height: str | None = None
    size_units: str | None = None
    # ``<model productID="..."/>`` -- SoundTraxx, Digitrax, etc. use this
    # to filter variables per specific decoder revision. When a
    # ``<variable>`` carries ``include="pid1,pid2,..."`` or ``exclude="..."``,
    # the UI shows/hides it based on whether this model's productID is
    # in the list.
    product_id: str = ""


class DecoderFamily(BaseModel):
    """One family containing one or more :class:`DecoderModel` entries."""
    name: str
    mfg: str = ""
    models: list[DecoderModel] = Field(default_factory=list)
    # Family-level ``<output>`` declarations. Some decoders (SoundTraxx
    # Tsunami2, Digitrax, ...) put ``<output name="N" label="..."/>`` after
    # all the ``<model>`` elements so the labels apply to every model
    # in the family. Callers that render function-map column headers
    # should fall back here when a model has no outputs of its own.
    outputs: list[DecoderOutput] = Field(default_factory=list)


class DecoderPane(BaseModel):
    """One JMRI programmer pane declared inside a decoder file.

    Panes are the tabs in DecoderPro's programmer UI (Basic, Motor,
    Sound, ...). A decoder file adds *extra* panes on top of the
    standard set provided by the active programmer definition (which
    lives in ``xml/programmers/*.xml`` -- a separate loader).

    ``items`` is the ordered list of variable ``item`` names referenced
    by ``<display item="...">`` anywhere in the pane. Change-tracking
    and pane-scoped read/write queues iterate this list.

    ``raw_element`` preserves the underlying lxml element so a later
    rendering step can reproduce the ``<column>``/``<row>``/``<label>``/
    ``<separator>`` structure faithfully.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = ""
    # ``xml:lang`` -> localised pane name. The default name (no lang
    # attribute) stays in ``name``; other locales go here so a future
    # UI can pick the user's language.
    name_by_lang: dict[str, str] = Field(default_factory=dict)
    # Ordered list of variable ``item`` names referenced by <display>
    # elements anywhere inside the pane. Duplicates are preserved
    # because JMRI's UI shows one row per <display>, not one row per
    # unique item.
    items: list[str] = Field(default_factory=list)
    # ``<cvtable/>`` marker anywhere in the pane -- the CVs tab that
    # DecoderPro's "CVs" pane uses to expose raw CV read/write.
    has_cvtable: bool = False
    # ``<dccaddress/>`` marker -- a first-class widget shown by the
    # Basic pane at the top.
    has_dcc_address: bool = False
    # ``<speedtable/>`` widget marker. JMRI's Speed Table pane doesn't
    # list per-step ``<display>`` items; instead the whole 28-step
    # curve is drawn by this widget. When set, the render layer expands
    # the decoder's SPEED_TABLE variable into per-step CV rows.
    has_speed_table: bool = False
    # ``<fnmapping/>`` marker -- the Function Map pane's checkbox
    # matrix. Like ``<cvtable/>``, it doesn't reference variables
    # via ``<display>``; the renderer collects ``Fx controls output N``
    # variables from the decoder's variable list.
    has_fnmapping: bool = False
    # Raw pane element for step-3 rich rendering.
    raw_element: etree._Element | None = None


class DecoderFile(BaseModel):
    """One ``xml/decoders/*.xml`` file parsed into a model.

    The ``raw_xml`` field is the original bytes (for byte-level operations);
    individual element access is via the model attributes. ``includes``
    captures any ``xi:include`` references for later resolution.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: pathlib.Path | None = None
    family: DecoderFamily | None = None
    variables: list[DecoderVariable] = Field(default_factory=list)
    # Decoder-declared programmer panes, in source order. Empty for
    # decoder files that don't declare any -- the programmer definition
    # provides the standard tab set separately.
    panes: list[DecoderPane] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)


XINCLUDE_NS = "{http://www.w3.org/2001/XInclude}"
XML_LANG_ATTR = "{http://www.w3.org/XML/1998/namespace}lang"


def load_decoder_bytes(
    data: bytes,
    path: pathlib.Path | None = None,
    *,
    xml_root: pathlib.Path | None = None,
) -> DecoderFile:
    """Parse decoder XML bytes, optionally resolving ``xi:include`` targets.

    *xml_root* is the directory that ``http://jmri.org/xml/`` hrefs map
    to on disk -- typically ``<jmri install>/xml``. When supplied,
    ``xi:include`` elements are resolved in-place before parsing, so
    variables + panes that live in shared fragments (JMRI's TCS,
    Digitrax, ESU decoders all do this) come through as real
    :class:`DecoderVariable` / :class:`DecoderPane` entries instead of
    silently vanishing. When *xml_root* is ``None``, xi:includes stay
    untouched -- matches the older no-op behaviour tests rely on.
    """
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(data, parser=parser)
    if xml_root is not None:
        _resolve_xincludes(root, xml_root)
    return _parse_decoder_root(root, path)


def load_decoder(
    path: str | pathlib.Path,
    *,
    xml_root: pathlib.Path | None = None,
) -> DecoderFile:
    """Load and parse a decoder definition file.

    When *xml_root* is omitted, it's inferred from *path* by walking
    upward looking for a ``decoders/`` ancestor -- the standard JMRI
    layout is ``<xml_root>/decoders/<file>.xml``. If no ``decoders/``
    ancestor is found, xi:include resolution is skipped (fragment paths
    would be uncomputable).
    """
    p = pathlib.Path(path)
    if xml_root is None:
        xml_root = _infer_xml_root(p)
    return load_decoder_bytes(p.read_bytes(), p, xml_root=xml_root)


def _infer_xml_root(path: pathlib.Path) -> pathlib.Path | None:
    """Return the directory ``http://jmri.org/xml/`` should map to.

    Follows JMRI's convention: decoder files live at
    ``<xml_root>/decoders/[<mfg>/]<file>.xml``, so the xml root is the
    parent of the first ancestor named ``decoders``. Returns ``None``
    when no such ancestor exists (e.g. when tests write decoder XML to
    a bare tmp dir).
    """
    for ancestor in path.parents:
        if ancestor.name == "decoders":
            return ancestor.parent
    return None


_JMRI_URL_PREFIX = "http://jmri.org/xml/"


def _resolve_xincludes(
    root: etree._Element,
    xml_root: pathlib.Path,
    _seen: set[pathlib.Path] | None = None,
) -> None:
    """Splice ``xi:include`` targets into *root*, in place.

    JMRI's fragment convention: an included file's root element carries
    the same tag as the include's parent (e.g. a ``<variables>``
    fragment is included inside a ``<variables>`` element). In that
    case we splice the fragment's children in place of the
    ``<xi:include>``. When the tags differ (a whole ``<pane>`` fragment
    included at the ``<decoder-config>`` level), we substitute the
    fragment root itself.

    Recurses into resolved fragments so nested includes (fragment A
    including fragment B) also come through. Cycle-guarded via *_seen*.

    Unresolvable hrefs (missing file, non-JMRI URL prefix) are left as
    dangling ``<xi:include>`` placeholders -- subsequent parsing sees
    no children from them, matches the "not-loaded" fallback path.
    """
    if _seen is None:
        _seen = set()
    # ``list(...)`` because we mutate the tree while iterating.
    for inc in list(root.iter(f"{XINCLUDE_NS}include")):
        href = inc.get("href", "")
        if not href.startswith(_JMRI_URL_PREFIX):
            continue
        rel = href[len(_JMRI_URL_PREFIX):]
        fragment_path = (xml_root / rel).resolve()
        if not fragment_path.is_file():
            _log.debug(
                "xi:include target not found: %s (from href=%s)",
                fragment_path, href,
            )
            continue
        if fragment_path in _seen:
            # Cycle -- the fragment we're about to load is already
            # somewhere in the current recursion path. Bail rather
            # than recurse forever.
            _log.warning("xi:include cycle detected at %s", fragment_path)
            continue
        try:
            parser = etree.XMLParser(
                remove_blank_text=False, resolve_entities=False,
            )
            fragment_root = etree.parse(str(fragment_path), parser=parser).getroot()
        except (etree.XMLSyntaxError, OSError) as e:
            _log.debug("xi:include failed to load %s: %s", fragment_path, e)
            continue
        # Track path membership only during the descent into this
        # fragment. Once we return, drop it so subsequent SIBLING
        # xi:includes of the same fragment aren't misidentified as
        # cycles. Real JMRI decoder files fan out to the same enum
        # fragment dozens of times (each Extended Function Map row
        # xi:includes ECOenumFnMap.xml, etc.) and treating those as
        # cycles silently drops enum choices from every occurrence
        # past the first.
        _seen.add(fragment_path)
        try:
            _resolve_xincludes(fragment_root, xml_root, _seen)
        finally:
            _seen.discard(fragment_path)
        parent = inc.getparent()
        if parent is None:
            continue
        idx = list(parent).index(inc)
        parent.remove(inc)
        if fragment_root.tag == parent.tag:
            # Same-tag fragment -> splice its children into the parent.
            for i, child in enumerate(list(fragment_root)):
                parent.insert(idx + i, child)
        else:
            # Fragment root gets substituted whole.
            parent.insert(idx, fragment_root)


def load_pane_bytes(data: bytes) -> DecoderPane:
    """Parse a standalone ``<pane>...</pane>`` fragment file.

    JMRI ships reusable pane definitions under paths like
    ``xml/decoders/digitrax/PaneDigitrax.xml`` with ``<pane>`` as the
    root element. Decoder files pull them in via ``xi:include``.
    """
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(data, parser=parser)
    if root.tag != "pane":
        raise ValueError(f"expected <pane> root, got <{root.tag}>")
    return _parse_pane(root)


def load_pane(path: str | pathlib.Path) -> DecoderPane:
    return load_pane_bytes(pathlib.Path(path).read_bytes())


def iter_decoder_files(dir_path: str | pathlib.Path) -> Iterator[DecoderFile]:
    """Walk *dir_path*, yielding one :class:`DecoderFile` per ``.xml``.

    JMRI's ``xml/decoders/`` tree contains many non-``<decoder-config>``
    XML files alongside the decoder definitions -- NMRA CV lists, family
    index files, function-label fragments included via xi:include, etc.
    Anything whose root isn't ``<decoder-config>`` (raising ValueError
    from :func:`_parse_decoder_root`) or whose XML is malformed is
    skipped so the caller's enumeration completes across the whole tree
    instead of aborting on the first non-decoder file.
    """
    root_path = pathlib.Path(dir_path)
    for xml in sorted(root_path.rglob("*.xml")):
        try:
            yield load_decoder(xml)
        except etree.XMLSyntaxError as e:
            _log.debug("iter_decoder_files: XML parse failure %s: %s", xml, e)
            continue
        except ValueError as e:
            # Non-decoder XML file (index/fragment/NMRA-CVs list). These
            # live in the same tree by JMRI convention; just skip.
            _log.debug("iter_decoder_files: skipping %s (%s)", xml, e)
            continue
        except OSError as e:
            _log.warning("iter_decoder_files: cannot read %s: %s", xml, e)
            continue


def load_all_decoders(dir_path: str | pathlib.Path) -> list[DecoderFile]:
    return list(iter_decoder_files(dir_path))


# ----- lightweight index scan ------------------------------------------
#
# JMRI ships 2000+ decoder XMLs; parsing every variable + pane on server
# start is too slow for interactive use (~30-60 s on a Pi). The scanner
# below extracts only the identity fields we need to look a file up by
# (family, model) -- no variables, no panes, no pydantic. Full parse is
# deferred until the user opens the programmer for a specific loco.


class DecoderIndexEntry(BaseModel):
    """One (family, model) tuple + the file it lives in.

    ``mfg`` comes from the ``<family mfg=...>`` attribute. Both ``family``
    and ``mfg`` are shared across every entry from the same file; only
    ``model`` varies. ``source_file`` is the path to the decoder XML --
    caller reads it via :func:`load_decoder` when a full definition is
    actually needed.

    ``low_version_id`` / ``high_version_id`` bracket which CV7 values
    this model accepts. Model-level ``lowVersionID``/``highVersionID``
    override family-level defaults; both are optional so JMRI files that
    don't declare them stay wildcard (0..255).

    ``product_id`` is the ``<model productID="..."/>`` attribute -- a
    manufacturer-specific identifier that lets identify narrow past
    the family. For SoundTraxx, ``productID`` is derived from CVs 253,
    255, and 256 (see the identify route).
    """
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    family: str
    mfg: str
    model: str
    source_file: pathlib.Path
    low_version_id: int = 0
    high_version_id: int = 255
    product_id: str = ""


def iter_decoder_index(dir_path: str | pathlib.Path) -> Iterator[DecoderIndexEntry]:
    """Yield one :class:`DecoderIndexEntry` per (family, model) pair.

    A single decoder XML file typically declares one family with many
    model rows, so one file emits multiple entries. Non-decoder XML
    files under the same tree (NMRA CV lists, xi:include fragments,
    programmer definitions) are skipped silently.

    This function does *not* validate the file -- it only touches the
    ``<family>`` and ``<model>`` elements. Anything else in the file
    (variables, panes, xi:includes) is ignored. Callers that need the
    full definition should call :func:`load_decoder` on the returned
    ``source_file`` path.
    """
    root_path = pathlib.Path(dir_path)
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    for xml in sorted(root_path.rglob("*.xml")):
        try:
            tree = etree.parse(str(xml), parser=parser)
        except etree.XMLSyntaxError as e:
            _log.debug("iter_decoder_index: XML parse failure %s: %s", xml, e)
            continue
        except OSError as e:
            _log.warning("iter_decoder_index: cannot read %s: %s", xml, e)
            continue
        root = tree.getroot()
        if root.tag != "decoder-config":
            # Not a decoder file (index / programmer / fragment). Skip.
            continue
        # JMRI decoder files: one <family> element under <decoder>. Use
        # descendant search because a few files nest differently and
        # xi:include can add layers.
        family_el = root.find(".//family")
        if family_el is None:
            continue
        family_name = family_el.get("name", "").strip()
        mfg = family_el.get("mfg", "").strip()
        # Family-level version range -- used as the default when a
        # model doesn't declare its own. Missing attrs stay wildcard.
        def _int_or(elem: "etree._Element", attr: str, fallback: int) -> int:
            raw = elem.get(attr, "")
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback
        fam_low = _int_or(family_el, "lowVersionID", 0)
        fam_high = _int_or(family_el, "highVersionID", 255)
        for m in family_el.findall("model"):
            model_name = m.get("model", "").strip()
            if not model_name or not family_name:
                # An empty family or model name would poison the index
                # key. Skip; log at debug so a malformed file surfaces
                # without spamming warnings on every load.
                _log.debug(
                    "iter_decoder_index: skipping model in %s: family=%r model=%r",
                    xml, family_name, model_name,
                )
                continue
            yield DecoderIndexEntry(
                family=family_name, mfg=mfg,
                model=model_name, source_file=xml,
                low_version_id=_int_or(m, "lowVersionID", fam_low),
                high_version_id=_int_or(m, "highVersionID", fam_high),
                product_id=m.get("productID", "").strip(),
            )


# ----- internal --------------------------------------------------------------


def _parse_decoder_root(root: etree._Element, path: pathlib.Path | None) -> DecoderFile:
    if root.tag != "decoder-config":
        raise ValueError(f"expected <decoder-config>, got <{root.tag}>")

    decoder = root.find("decoder")
    if decoder is None:
        return DecoderFile(path=path)

    family_elem = decoder.find("family")
    family = _parse_family(family_elem) if family_elem is not None else None

    variables_elem = decoder.find("variables")
    variables = list(_iter_variables(variables_elem)) if variables_elem is not None else []

    # Panes live at the ``<decoder-config>`` level, alongside ``<decoder>``.
    # A decoder may declare zero or more of them; standard tabs (Basic,
    # Motor, ...) come from the programmer definition and are loaded
    # separately.
    panes = [_parse_pane(p) for p in root.findall("pane")]

    includes = [
        elem.get("href", "")
        for elem in root.iter(f"{XINCLUDE_NS}include")
        if elem.get("href")
    ]

    return DecoderFile(
        path=path, family=family, variables=variables,
        panes=panes, includes=includes,
    )


def _parse_pane(elem: etree._Element) -> DecoderPane:
    """Turn a ``<pane>`` element into a :class:`DecoderPane`.

    JMRI's pane structure is nested (``<column>`` / ``<row>`` containing
    ``<display>`` / ``<label>`` / ``<separator>`` / ``<cvtable>`` /
    ``<dccaddress>`` / further ``<column>``/``<row>``), but for our
    change-tracking + queue-scoping purposes we care about the flat
    list of ``<display item="...">`` references. ``elem.iter()`` walks
    the whole subtree, giving us that flat list in document order.
    Rich layout rendering (step 3) works off the preserved
    ``raw_element``.
    """
    default_name = ""
    name_by_lang: dict[str, str] = {}
    for name_elem in elem.findall("name"):
        text = (name_elem.text or "").strip()
        lang = name_elem.get(XML_LANG_ATTR, "")
        if lang:
            name_by_lang[lang] = text
        elif not default_name:
            default_name = text

    items = [
        d.get("item", "")
        for d in elem.iter("display")
        if d.get("item")
    ]
    has_cvtable = elem.find(".//cvtable") is not None
    has_dcc_address = elem.find(".//dccaddress") is not None
    has_fnmapping = elem.find(".//fnmapping") is not None
    # JMRI's Speed Table pane widget: catch both the standard
    # ``<speedtable/>`` element and any case variant / adjacent widget
    # whose tag name starts with "speedtable" (JMRI has shipped both
    # ``<speedtable/>`` and ``<speedTable/>`` across versions, plus
    # helper widgets like ``<speedTableVarSlider/>``). Whenever any of
    # those is present, the render layer expands the decoder's
    # SPEED_TABLE variable into per-step CV rows.
    has_speed_table = any(
        isinstance(d.tag, str) and d.tag.lower().startswith("speedtable")
        for d in elem.iter()
    )

    return DecoderPane(
        name=default_name,
        name_by_lang=name_by_lang,
        items=items,
        has_cvtable=has_cvtable,
        has_dcc_address=has_dcc_address,
        has_speed_table=has_speed_table,
        has_fnmapping=has_fnmapping,
        raw_element=elem,
    )


# ----- rich pane layout tree --------------------------------------------


# Container elements -- nest child nodes.
_LAYOUT_CONTAINER_TAGS = {"column", "row", "group"}
# Widget leaf elements the renderer knows how to draw.
_LAYOUT_LEAF_TAGS = {
    "display", "label", "separator",
    "cvtable", "speedtable", "dccaddress", "fnmapping",
}
# Grid layout -- ``<grid>`` wraps ``<griditem gridx="..." gridy="...">``
# children, each of which is itself a container of layout nodes.
_LAYOUT_GRID_TAG = "grid"
_LAYOUT_GRIDITEM_TAG = "griditem"


def pane_layout_tree(pane: "DecoderPane") -> dict:
    """Build a nested tree describing the pane's authored layout.

    Walks :attr:`DecoderPane.raw_element` and emits a tree of dicts
    the renderer can iterate recursively. Recognises JMRI's usual
    layout primitives -- ``<column>``, ``<row>``, ``<group>``,
    ``<display>``, ``<label>``, ``<separator>``, ``<cvtable>``,
    ``<speedtable>``, ``<dccaddress>``.

    Nodes carry a ``type`` key and either a ``children`` list
    (containers) or type-specific attributes (leaves):

    * ``{"type": "column", "children": [...]}``
    * ``{"type": "display", "item": "Vstart"}``
    * ``{"type": "label", "text": "Speed control"}``
    * ``{"type": "separator"}``

    Any element the renderer doesn't recognise (rare -- hand-authored
    decoder files sometimes have quirks) is silently skipped, keeping
    the rest of the tree renderable.

    When *pane* has no ``raw_element`` (e.g. constructed in a test
    from a bare ``DecoderPane(name="X", items=[...])``), the returned
    tree is a flat column of the pane's items -- matches what the
    legacy flat-table renderer showed and keeps tests uncoupled from
    XML shape.
    """
    if pane.raw_element is None:
        return _dedupe_display_nodes({
            "type": "column",
            "children": [
                {"type": "display", "item": item}
                for item in pane.items
            ],
        })
    root_children = list(_layout_children(pane.raw_element))
    # If the pane has no explicit container at the top level, wrap in
    # a column so the renderer always starts from a container node.
    if len(root_children) == 1 and root_children[0]["type"] in _LAYOUT_CONTAINER_TAGS:
        return _dedupe_display_nodes(root_children[0])
    return _dedupe_display_nodes(
        {"type": "column", "children": root_children},
    )


def _dedupe_display_nodes(tree: dict) -> dict:
    """Walk *tree* and drop repeat ``<display item="X">`` leaves.

    JMRI's pane XML sometimes references the same variable twice --
    typically for two widget presentations (numeric input + slider),
    or because two ``<column>`` blocks each pull it in. Our renderer
    only knows how to draw one widget per variable, so a naive walk
    produces duplicate rows side-by-side in the same column, or the
    same variable in two adjacent columns. Track seen items across
    the WHOLE tree (not just per container) and drop later
    occurrences.

    Mutates the tree in place and returns it for chaining.
    """
    seen: set[str] = set()

    def prune(node: dict) -> dict | None:
        if node.get("type") == "display":
            item = node.get("item", "")
            if not item or item in seen:
                return None
            seen.add(item)
            return node
        if "children" in node:
            pruned: list[dict] = []
            for child in node["children"]:
                kept = prune(child)
                if kept is not None:
                    pruned.append(kept)
            node["children"] = pruned
        return node

    prune(tree)
    return tree


def _layout_children(elem: "etree._Element") -> Iterator[dict]:
    """Iterate the layout-relevant children of *elem* as tree nodes."""
    for child in elem:
        if not isinstance(child.tag, str):
            # Comments, processing instructions -- skip.
            continue
        node = _layout_node(child)
        if node is not None:
            yield node


def _layout_griditem(elem: "etree._Element") -> dict | None:
    """Turn a ``<griditem>`` into a placement + children node.

    JMRI grid items use Swing's ``GridBagLayout`` attribute names.
    We preserve the ones the renderer needs and drop the rest:

    * ``gridx`` / ``gridy`` -- 0-based cell coordinates.
    * ``gridwidth`` / ``gridheight`` -- spans. JMRI also uses the
      string ``REMAINDER`` for "span to the end of the row/column".
    * ``anchor`` -- Swing anchor constant (``LINE_END``, ``CENTER``,
      etc.); we map the common ones to CSS text-alignment.
    """
    if not isinstance(elem.tag, str) or elem.tag != _LAYOUT_GRIDITEM_TAG:
        return None
    def _int_attr(name: str) -> int | None:
        raw = elem.get(name)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    return {
        "type": "griditem",
        "gridx": _int_attr("gridx"),
        "gridy": _int_attr("gridy"),
        "gridwidth": elem.get("gridwidth") or "",
        "gridheight": elem.get("gridheight") or "",
        "anchor": elem.get("anchor") or "",
        "children": list(_layout_children(elem)),
    }


def _layout_node(elem: "etree._Element") -> dict | None:
    """Turn one XML element into a layout tree node, or None to skip.

    Ordering matters for the renderer, so we preserve document order
    and only drop elements the renderer wouldn't know what to do with
    (rare metadata like ``<name>``, ``<qualifier>`` -- those are
    consumed at parse time or belong elsewhere).
    """
    tag = elem.tag
    if not isinstance(tag, str):
        return None
    if tag in _LAYOUT_CONTAINER_TAGS:
        return {
            "type": tag,
            "children": list(_layout_children(elem)),
        }
    if tag == _LAYOUT_GRID_TAG:
        return {
            "type": "grid",
            "children": [
                node for node in (_layout_griditem(c) for c in elem)
                if node is not None
            ],
        }
    if tag == "display":
        item = elem.get("item", "")
        if not item:
            return None
        return {
            "type": "display",
            "item": item,
            # ``format`` decides how the widget renders:
            # "checkbox" collapses an int variable to a bool checkbox,
            # "" / absent means the variable's natural widget (enum
            # dropdown, numeric input, ...). Preserving the raw attribute
            # here lets the template pick without another lookup.
            "format": elem.get("format", ""),
            # ``label=""`` on a display suppresses the widget's own
            # label -- used inside grid cells where the label is in a
            # neighbouring cell instead.
            "label_override": elem.get("label"),
        }
    if tag == "label":
        # ``<label>text</label>`` or ``<label><text>text</text></label>``.
        # Localised variants match the same rule as variable labels.
        text = _pick_localized_text(elem, "text") or (elem.text or "").strip()
        return {"type": "label", "text": text}
    if tag == "separator":
        return {"type": "separator"}
    if tag == "cvtable":
        return {"type": "cvtable"}
    if tag == "dccaddress":
        return {"type": "dccaddress"}
    if tag == "fnmapping":
        return {"type": "fnmapping"}
    # ``<speedtable/>`` and any case/tag variant that starts with
    # "speedtable" (older JMRI files shipped ``<speedTable>``,
    # ``<speedTableVarSlider>``, etc.).
    if tag.lower().startswith("speedtable"):
        return {"type": "speedtable"}
    return None


def _parse_family(elem: etree._Element) -> DecoderFamily:
    models = [_parse_model(m) for m in elem.findall("model")]
    outputs = [
        DecoderOutput(name=o.get("name", ""), label=o.get("label", ""))
        for o in elem.findall("output")
    ]
    return DecoderFamily(
        name=elem.get("name", ""),
        mfg=elem.get("mfg", ""),
        models=models,
        outputs=outputs,
    )


def _parse_model(elem: etree._Element) -> DecoderModel:
    outputs = [
        DecoderOutput(name=o.get("name", ""), label=o.get("label", ""))
        for o in elem.findall("output")
    ]
    protocols = [
        (p.text or "").strip()
        for p in elem.findall("protocols/protocol")
        if (p.text or "").strip()
    ]
    size = elem.find("size")
    return DecoderModel(
        model=elem.get("model", ""),
        connector=elem.get("connector", ""),
        show_empty_panes=elem.get("showEmptyPanes"),
        max_input_volts=elem.get("maxInputVolts"),
        max_motor_current=elem.get("maxMotorCurrent"),
        outputs=outputs,
        protocols=protocols,
        size_length=size.get("length") if size is not None else None,
        size_width=size.get("width") if size is not None else None,
        size_height=size.get("height") if size is not None else None,
        size_units=size.get("units") if size is not None else None,
        product_id=elem.get("productID", ""),
    )


def _parse_default_int(raw: str | None) -> int | None:
    """Coerce a ``<variable default="...">`` string to an int.

    Real JMRI decoder files use plain decimal ints for the ``default``
    attribute. A handful of hand-authored files use ``0x...`` hex -- accept
    that too via ``base=0`` (auto-detects). Anything else (blank,
    missing, malformed) returns ``None`` so downstream can use its
    "no known default" fallback path.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw, 0)
    except (TypeError, ValueError):
        return None


def _pick_localized_text(
    elem: etree._Element, tag: str, preferred_lang: str = "en",
) -> str:
    """Pick the best localized text out of ``<tag>`` children.

    JMRI decoder XMLs sometimes carry the same descriptive text in
    multiple languages via ``<label xml:lang="...">...</label>`` -- the
    same variable can have an English, Italian, French, German, and
    Dutch label. The user's UI is English; we shouldn't silently render
    the Italian one just because ``elem.find(tag)`` happened to hit it
    first.

    Priority order:

    1. The ``<tag>`` element with ``xml:lang="<preferred_lang>"``.
    2. The ``<tag>`` element with NO ``xml:lang`` attribute (JMRI's
       convention: the unqualified element is the default English
       text -- same rule the ``<pane><name>`` parser uses).
    3. Any other ``<tag>`` element (last-resort fallback).
    4. Empty string.

    Preserves inline markup like ``<br/>`` and ``<b>``: JMRI tooltips
    routinely contain such tags for line breaks and emphasis. We serialize
    the element's content back to a string, stripping only the wrapping
    ``<tag>...</tag>`` -- the caller can render it as HTML.
    """
    children = elem.findall(tag)
    if not children:
        return ""

    def _inner_html(node: etree._Element) -> str:
        out = node.text or ""
        for child in node:
            out += etree.tostring(child, encoding="unicode")
        return out.strip()

    preferred = None
    unqualified = None
    fallback = None
    for c in children:
        lang = c.get(XML_LANG_ATTR, "")
        if lang == preferred_lang and preferred is None:
            preferred = c
        elif not lang and unqualified is None:
            unqualified = c
        elif fallback is None:
            fallback = c
    # lxml Elements are falsy when they have no children -- use
    # explicit ``is not None`` checks so we don't accidentally skip an
    # empty ``<label></label>``.
    picked = preferred
    if picked is None:
        picked = unqualified
    if picked is None:
        picked = fallback
    if picked is None:
        picked = children[0]
    return _inner_html(picked)


def _iter_variables(elem: etree._Element) -> Iterable[DecoderVariable]:
    """Yield CV variables, skipping XInclude pointers (captured separately).

    Populates the step-2 typed fields (``kind`` + type-specific structure)
    based on which value-type child element the variable declares. If
    none is present (rare -- usually a placeholder), ``kind`` stays
    :data:`VariableKind.UNKNOWN` and the variable acts as an opaque byte.
    """
    for v in elem.findall("variable"):
        label_text = _pick_localized_text(v, "label")
        tooltip_text = _pick_localized_text(v, "tooltip")
        # ``<qualifier>`` (or ``<qualifiers><qualifier>``): runtime
        # visibility gates. Each holds a ``variableref`` + ``relation`` +
        # ``value`` triple. Collect them flat so the caller can AND them.
        qualifier_elems = list(v.findall("qualifier"))
        qualifier_elems.extend(v.findall("qualifiers/qualifier"))
        qualifier_dicts: list[dict[str, str]] = []
        for q in qualifier_elems:
            vref = q.find("variableref")
            rel = q.find("relation")
            val = q.find("value")
            if vref is None or rel is None:
                continue
            qualifier_dicts.append({
                "variableref": (vref.text or "").strip(),
                "relation": (rel.text or "").strip(),
                "value": (val.text or "").strip() if val is not None else "",
            })
        # Mask may be on <variable> directly or inside <splitVal>.
        split_elem = _first_child(v, ("splitVal", "splitHexVal", "splitDateTimeVal"))
        mask = v.get("mask") or (split_elem.get("upperMask") if split_elem is not None else None)
        min_val = v.get("min") or (split_elem.get("min") if split_elem is not None else None)
        max_val = v.get("max") or (split_elem.get("max") if split_elem is not None else None)

        cv_attr = v.get("CV", "")
        kind, kind_extras = _classify_value_type(v)

        # Populate ``split`` for ANY variable whose CV attribute uses
        # dotted notation (``16.1.275`` = indexed CV, ``275:4`` =
        # 4-byte split, ``17.18`` = long-address pair). The value-type
        # child (decVal / hexVal / enumVal / ...) doesn't necessarily
        # signal indexed access -- a plain ``<decVal>`` on
        # ``CV="16.1.275"`` is a decimal variable that happens to sit
        # at bank/index 16/1, base CV 275. Without this fallback, the
        # write path would send the byte to CV 275 under the current
        # (uncontrolled) bank/index -- corrupting unrelated state.
        parsed_split = kind_extras.get("split")
        if parsed_split is None and cv_attr and (
            "." in cv_attr or ":" in cv_attr
        ):
            parsed_split = SplitCv.parse(cv_attr)
        yield DecoderVariable(
            CV=cv_attr,
            item=v.get("item", ""),
            default=_parse_default_int(v.get("default")),
            mask=mask,
            min=min_val,
            max=max_val,
            label=label_text,
            ops_only=(v.get("opsOnly", "no").lower() == "yes"),
            kind=kind,
            bit_mask=CvBitMask.parse(mask),
            enum_choices=kind_extras.get("enum_choices", []),
            composite_choices=kind_extras.get("composite_choices", []),
            split=parsed_split,
            constant_value=kind_extras.get("constant_value"),
            speed_table_entries=kind_extras.get("speed_table_entries", 28),
            dt_base=kind_extras.get("dt_base", ""),
            dt_factor=kind_extras.get("dt_factor", 1),
            dt_unit=kind_extras.get("dt_unit", ""),
            dt_display=kind_extras.get("dt_display", ""),
            read_only=(v.get("readOnly", "no").lower() == "yes"),
            write_only=(v.get("writeOnly", "no").lower() == "yes"),
            tooltip=tooltip_text or v.get("tooltip", ""),
            include_product_ids=[
                p.strip() for p in v.get("include", "").split(",")
                if p.strip()
            ],
            exclude_product_ids=[
                p.strip() for p in v.get("exclude", "").split(",")
                if p.strip()
            ],
            qualifiers=qualifier_dicts,
        )


def _first_child(elem: etree._Element, tags: tuple[str, ...]) -> etree._Element | None:
    for tag in tags:
        child = elem.find(tag)
        if child is not None:
            return child
    return None


# Map value-type child element tag -> VariableKind.
_KIND_BY_TAG = {
    "decVal":            VariableKind.DECIMAL,
    "hexVal":            VariableKind.HEXADECIMAL,
    "enumVal":           VariableKind.ENUM,
    "speedTable":        VariableKind.SPEED_TABLE,
    "shortAddressVal":   VariableKind.SHORT_ADDRESS,
    "longAddressVal":    VariableKind.LONG_ADDRESS,
    "splitVal":          VariableKind.SPLIT,
    "splitHexVal":       VariableKind.SPLIT_HEX,
    "splitDateTimeVal":  VariableKind.SPLIT_DATETIME,
    "compositeVal":      VariableKind.COMPOSITE,
    "constant":          VariableKind.CONSTANT,
}


def _classify_value_type(v: etree._Element) -> tuple[VariableKind, dict]:
    """Look at the value-type children of ``<variable>`` and pick a kind.

    Returns ``(kind, extras)`` where ``extras`` is a dict of type-specific
    parsed structure the caller drops into the :class:`DecoderVariable`
    constructor.
    """
    cv_attr = v.get("CV", "")
    for tag, kind in _KIND_BY_TAG.items():
        child = v.find(tag)
        if child is None:
            continue
        if kind is VariableKind.ENUM:
            return kind, {"enum_choices": list(_iter_enum_choices(child))}
        if kind is VariableKind.COMPOSITE:
            return kind, {"composite_choices": list(_iter_composite_choices(child))}
        if kind in (
            VariableKind.SPLIT, VariableKind.SPLIT_HEX,
        ):
            return kind, {"split": SplitCv.parse(cv_attr)}
        if kind is VariableKind.SPLIT_DATETIME:
            # extra1..extra4 are JMRI's name for the attributes in the
            # XSD; our struct names them dt_base / dt_factor / dt_unit /
            # dt_display for readability. Missing attributes fall back
            # to safe defaults (empty strings, factor=1) so the pane
            # widget degrades gracefully rather than crashing.
            try:
                factor = int(child.get("factor", "1"))
            except ValueError:
                factor = 1
            return kind, {
                "split": SplitCv.parse(cv_attr),
                "dt_base": (child.get("base") or "").strip(),
                "dt_factor": factor,
                "dt_unit": (child.get("unit") or "").strip(),
                "dt_display": (child.get("display") or "").strip(),
            }
        if kind is VariableKind.LONG_ADDRESS:
            # CV17.CV18 pair. If the caller wrote CV="17", we still
            # need to know we're touching CV18 too.
            split_cv = SplitCv.parse(cv_attr or "17.18")
            return kind, {"split": split_cv}
        if kind is VariableKind.CONSTANT:
            raw = child.get("value")
            try:
                cval = int(raw) if raw is not None else None
            except ValueError:
                cval = None
            return kind, {"constant_value": cval}
        if kind is VariableKind.SPEED_TABLE:
            # ``<speedTable entries="N"/>`` declares the number of
            # speed-step slots; NMRA default is 28 (CV67..CV94).
            raw = child.get("entries")
            try:
                entries = int(raw) if raw else 28
            except ValueError:
                entries = 28
            return kind, {"speed_table_entries": max(entries, 1)}
        # decVal / hexVal / shortAddressVal -- no extras.
        return kind, {}
    return VariableKind.UNKNOWN, {}


def _iter_enum_choices(elem: etree._Element) -> Iterator[EnumChoice]:
    """Walk ``<enumVal>`` yielding one :class:`EnumChoice` per
    ``<enumChoice>``. Positional index becomes the wire value when
    the element has no explicit ``value`` attribute.
    """
    for index, ec in enumerate(elem.findall("enumChoice")):
        raw = ec.get("value")
        try:
            value = int(raw) if raw is not None else index
        except ValueError:
            value = index
        # Default choice text is the ``choice`` attribute; localised
        # variants appear as <choice xml:lang="...">...</choice> children.
        default = ec.get("choice", "")
        by_lang: dict[str, str] = {}
        for c in ec.findall("choice"):
            text = (c.text or "").strip()
            lang = c.get(XML_LANG_ATTR, "")
            if lang:
                by_lang[lang] = text
            elif not default and text:
                default = text
        yield EnumChoice(value=value, choice=default, choice_by_lang=by_lang)


def _iter_composite_choices(elem: etree._Element) -> Iterator[CompositeChoice]:
    """Walk ``<compositeVal>`` yielding one :class:`CompositeChoice` per
    ``<compositeChoice>`` plus its ``<compositeSetting>`` rows."""
    for cc in elem.findall("compositeChoice"):
        default = cc.get("choice", "")
        by_lang: dict[str, str] = {}
        for c in cc.findall("choice"):
            text = (c.text or "").strip()
            lang = c.get(XML_LANG_ATTR, "")
            if lang:
                by_lang[lang] = text
            elif not default and text:
                default = text
        settings = []
        for cs in cc.findall("compositeSetting"):
            try:
                value = int(cs.get("value", "0"))
            except ValueError:
                value = 0
            settings.append(
                CompositeSetting(label=cs.get("label", ""), value=value)
            )
        yield CompositeChoice(
            choice=default,
            choice_by_lang=by_lang,
            settings=settings,
        )


# ----- variable encode / decode ------------------------------------------


class UnsupportedVariableKind(ValueError):
    """Raised when a caller tries to encode/decode a variable whose kind
    doesn't have a byte-level round-trip yet (COMPOSITE fans out to
    sub-variables; SPEED_TABLE has custom widget semantics)."""


def variable_primary_cv(var: DecoderVariable) -> int:
    """The primary CV byte this variable reads/writes.

    For split variables the primary CV is the low-address byte of the
    split. For simple variables it's just ``int(var.cv)``. Returns 0
    if the CV attribute isn't a plain integer we can parse -- the
    caller should treat that as "not directly programmable".
    """
    if var.split is not None:
        return var.split.primary_cv
    try:
        return int(var.cv)
    except ValueError:
        return 0


def variable_cv_list(var: DecoderVariable) -> list[int]:
    """Every CV byte this variable touches, low-address first.

    * Single-byte variables -> ``[primary_cv]``
    * Long-address -> ``[17, 18]``
    * Split-CV -- depends on ``byte_count`` on the parsed :class:`SplitCv`.
    """
    if var.kind is VariableKind.LONG_ADDRESS:
        return [17, 18]
    if var.split is not None:
        return var.split.cv_list
    primary = variable_primary_cv(var)
    return [primary] if primary else []


def variable_is_indexed(var: DecoderVariable) -> bool:
    """True when *var* uses ``bank.index.base`` indexed CV addressing.

    Indexed access (Tsunami2 / ESU / newer NCE decoders) requires
    writing ``CV31=bank`` + ``CV32=index`` before every read or write
    to the base CV. The decoder maintains bank/index state internally
    and interprets subsequent CV257-CV511 accesses through that page.
    """
    return var.split is not None and var.split.bank is not None


def variable_store_key(var: DecoderVariable, cv_number: int) -> str:
    """Return the roster ``<CVvalue name="...">`` key for *cv_number* under *var*.

    JMRI's roster file writes each CV under a distinct key:

    * Plain CV: ``"275"``
    * Indexed CV: ``"16.1.275"`` -- bank + index + base

    So `16.1.275` and `275` coexist without colliding -- they mean
    different CVs on the wire (same base but different decoder pages).
    """
    if variable_is_indexed(var):
        return f"{var.split.bank}.{var.split.index or 0}.{cv_number}"
    return str(cv_number)


@dataclass(frozen=True)
class VariableWriteOp:
    """One physical CV write generated by :func:`variable_wire_write_plan`.

    * ``wire_cv`` -- actual CV number sent on the wire (bare integer;
      the DCC packet targets this CV under whatever bank/index the
      decoder currently has).
    * ``byte_value`` -- 0..255 byte to write.
    * ``store_key`` -- key to persist under in the roster's cv_values
      dict, or ``None`` when the op is bank/index setup (CV31/CV32)
      that shouldn't leave a per-write trace.
    """
    wire_cv: int
    byte_value: int
    store_key: str | None


def variable_wire_write_plan(
    var: DecoderVariable,
    human_value: int,
    cv_values: dict[str, int],
) -> list[VariableWriteOp]:
    """Ordered wire writes to set *var* to *human_value*.

    Indexed variables produce three-or-more ops: ``CV31=bank``,
    ``CV32=index``, then one or more base-CV writes. The setup ops
    carry ``store_key=None`` so callers can skip persisting them
    (they'd corrupt the roster's per-variable tracking otherwise).

    Non-indexed variables produce the same wire writes as
    :func:`variable_apply_to_state` but wrapped in :class:`VariableWriteOp`
    with plain string keys (``"275"``) for round-trip storage.

    Read-only / CONSTANT variables return an empty list.
    """
    plain = variable_apply_to_state(var, human_value, _int_keyed(cv_values))
    if not plain:
        return []
    ops: list[VariableWriteOp] = []
    if variable_is_indexed(var):
        assert var.split is not None  # narrow for the type checker
        ops.append(VariableWriteOp(wire_cv=31, byte_value=var.split.bank or 0, store_key=None))
        ops.append(VariableWriteOp(wire_cv=32, byte_value=var.split.index or 0, store_key=None))
    for wire_cv, byte in plain.items():
        ops.append(VariableWriteOp(
            wire_cv=wire_cv, byte_value=byte,
            store_key=variable_store_key(var, wire_cv),
        ))
    return ops


def variable_wire_read_plan(var: DecoderVariable) -> list[VariableWriteOp]:
    """Ordered wire ops to read *var*'s current bytes.

    For plain variables: one entry per CV in :func:`variable_cv_list`.
    For indexed variables: ``CV31=bank`` + ``CV32=index`` setup ops
    (as writes with ``store_key=None``) then one read entry per base
    CV. Callers should send the setup writes and then read the
    ``wire_cv`` of any op whose ``store_key`` is non-None.
    """
    if variable_is_indexed(var):
        assert var.split is not None
        ops: list[VariableWriteOp] = [
            VariableWriteOp(wire_cv=31, byte_value=var.split.bank or 0, store_key=None),
            VariableWriteOp(wire_cv=32, byte_value=var.split.index or 0, store_key=None),
        ]
        for cv in variable_cv_list(var):
            ops.append(VariableWriteOp(
                wire_cv=cv, byte_value=0,   # unused for reads
                store_key=variable_store_key(var, cv),
            ))
        return ops
    # Non-indexed: no setup, just read each CV.
    return [
        VariableWriteOp(wire_cv=cv, byte_value=0, store_key=str(cv))
        for cv in variable_cv_list(var)
    ]


def _int_keyed(cv_values: dict[str, int]) -> dict[int, int]:
    """Convert a string-keyed CV snapshot to int-keyed for legacy helpers.

    Silently drops keys that aren't plain integers -- indexed keys
    like ``"16.1.275"`` don't participate in bit-mask RMW anyway;
    those are handled per-byte by the write plan directly.
    """
    out: dict[int, int] = {}
    for k, v in cv_values.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def variable_extract_from_store(
    var: DecoderVariable, cv_values: dict[str, int],
) -> int:
    """Like :func:`variable_extract_from_state` but keyed by store keys.

    * Plain variables look up ``str(primary_cv)``.
    * Indexed variables look up ``"{bank}.{index}.{cv}"`` -- the same
      shape JMRI Java writes in the roster's ``<CVvalue name="...">``.
    * Multi-byte splits assemble big-endian from consecutive keys.

    Returns 0 when a key is missing (the caller should overlay a
    catalog default separately).
    """
    if var.kind is VariableKind.CONSTANT:
        return var.constant_value or 0
    if var.kind is VariableKind.LONG_ADDRESS:
        high = int(cv_values.get("17", 0)) & 0x3F
        low = int(cv_values.get("18", 0)) & 0xFF
        return (high << 8) | low
    if var.kind in (
        VariableKind.SPLIT, VariableKind.SPLIT_HEX,
        VariableKind.SPLIT_DATETIME,
    ):
        acc = 0
        for cv in variable_cv_list(var):
            key = variable_store_key(var, cv)
            acc = (acc << 8) | (int(cv_values.get(key, 0)) & 0xFF)
        return acc
    if var.kind in (
        VariableKind.DECIMAL, VariableKind.HEXADECIMAL,
        VariableKind.ENUM, VariableKind.UNKNOWN, VariableKind.SHORT_ADDRESS,
    ):
        key = variable_store_key(var, variable_primary_cv(var))
        byte = int(cv_values.get(key, 0))
        return var.bit_mask.extract(byte)
    raise UnsupportedVariableKind(
        f"variable_extract_from_store does not support {var.kind}"
    )


def variable_extract_from_state(
    var: DecoderVariable, cv_bytes: dict[int, int],
) -> int:
    """Decode the variable's *human value* from a snapshot of CV bytes.

    * DECIMAL / HEXADECIMAL -- the masked byte value
    * ENUM -- the wire value (caller can look up the choice label)
    * SHORT_ADDRESS -- CV1 straight through
    * LONG_ADDRESS -- ``(CV17 & 0x3F) << 8 | CV18`` (strip the two long-
      address marker bits set on CV17)
    * SPLIT / SPLIT_HEX / SPLIT_DATETIME -- big-endian assembly
      across ``cv_list``. SPLIT_DATETIME returns the raw stored
      integer; conversion to a timestamp is a formatter concern,
      handled at the pane-widget layer with the per-variable
      ``dt_base`` / ``dt_factor`` / ``dt_unit`` / ``dt_display``
      attributes.
    * CONSTANT -- the pinned constant value
    * COMPOSITE, SPEED_TABLE -- raise
      :class:`UnsupportedVariableKind`; those need step-specific work.
    """
    if var.kind is VariableKind.CONSTANT:
        return var.constant_value or 0
    if var.kind is VariableKind.SHORT_ADDRESS:
        return cv_bytes.get(variable_primary_cv(var), 0)
    if var.kind is VariableKind.LONG_ADDRESS:
        high = cv_bytes.get(17, 0) & 0x3F
        low = cv_bytes.get(18, 0) & 0xFF
        return (high << 8) | low
    if var.kind in (
        VariableKind.SPLIT, VariableKind.SPLIT_HEX,
        VariableKind.SPLIT_DATETIME,
    ):
        acc = 0
        for cv in variable_cv_list(var):
            acc = (acc << 8) | (cv_bytes.get(cv, 0) & 0xFF)
        return acc
    if var.kind in (
        VariableKind.DECIMAL, VariableKind.HEXADECIMAL,
        VariableKind.ENUM, VariableKind.UNKNOWN,
    ):
        byte = cv_bytes.get(variable_primary_cv(var), 0)
        return var.bit_mask.extract(byte)
    raise UnsupportedVariableKind(
        f"variable_extract_from_state does not support {var.kind}"
    )


def variable_apply_to_state(
    var: DecoderVariable, human_value: int, cv_bytes: dict[int, int],
) -> dict[int, int]:
    """Compute the CV-byte writes needed to set the variable to *human_value*.

    Returns ``{cv: new_byte_value}`` for every CV the write touches.
    Masked variables read the current byte out of *cv_bytes* to preserve
    the non-``V`` bits (read-modify-write); *cv_bytes* is the caller's
    running snapshot of committed values.
    """
    if var.kind is VariableKind.CONSTANT:
        return {}     # constants never write
    if var.read_only:
        return {}
    if var.kind is VariableKind.SHORT_ADDRESS:
        primary = variable_primary_cv(var)
        return {primary: human_value & 0x7F}
    if var.kind is VariableKind.LONG_ADDRESS:
        # NMRA: high byte carries the 0b11000000 long-address marker.
        addr = int(human_value) & 0x3FFF
        return {17: 0xC0 | ((addr >> 8) & 0x3F), 18: addr & 0xFF}
    if var.kind in (VariableKind.SPLIT, VariableKind.SPLIT_HEX):
        cvs = variable_cv_list(var)
        n = len(cvs)
        writes: dict[int, int] = {}
        v = int(human_value)
        # High-byte first, matching how ``extract`` reassembles.
        for idx, cv in enumerate(cvs):
            shift = 8 * (n - 1 - idx)
            writes[cv] = (v >> shift) & 0xFF
        return writes
    if var.kind in (
        VariableKind.DECIMAL, VariableKind.HEXADECIMAL,
        VariableKind.ENUM, VariableKind.UNKNOWN,
    ):
        primary = variable_primary_cv(var)
        current = cv_bytes.get(primary, 0)
        return {primary: var.bit_mask.apply(current, int(human_value))}
    raise UnsupportedVariableKind(
        f"variable_apply_to_state does not support {var.kind}"
    )


def resolve_composite_choice(
    var: DecoderVariable, choice_text: str,
) -> list[CompositeSetting]:
    """Return the ordered ``<compositeSetting>`` list for the named choice.

    Composite variables fan out to sub-variable writes: picking a choice
    means writing each of its settings to the referenced sub-variables
    (looked up by ``item`` name). This helper does the choice -> settings
    lookup; the caller (session/queue layer in step 4) is responsible
    for finding the referenced variables and applying their writes.

    Raises ``KeyError`` if the choice text isn't a declared option.
    """
    if var.kind is not VariableKind.COMPOSITE:
        raise UnsupportedVariableKind(
            f"resolve_composite_choice: variable is {var.kind}, not COMPOSITE"
        )
    for cc in var.composite_choices:
        if cc.choice == choice_text:
            return list(cc.settings)
        if choice_text in cc.choice_by_lang.values():
            return list(cc.settings)
    raise KeyError(
        f"no composite choice named {choice_text!r} on variable {var.item!r}"
    )


def find_composite_current_choice(
    var: DecoderVariable,
    variables_by_item: dict[str, DecoderVariable],
    cv_bytes: dict[str, int],
) -> str | None:
    """Find the composite choice whose settings match the current state.

    Composite variables don't have a wire representation of their own --
    they're syntactic sugar for a group of sub-variable writes. So the
    "current choice" is whichever named preset's ``<compositeSetting>``
    values ALL match the corresponding sub-variables' current human
    values. If no choice matches (the sub-variables are in an
    intermediate state), returns ``None`` and the UI shows a blank /
    "custom" selection.

    * ``variables_by_item`` -- the decoder's full variable table keyed
      by item name. Composite settings reference sub-variables by
      item; this map lets us resolve them.
    * ``cv_bytes`` -- string-keyed CV byte snapshot (same shape as
      :func:`variable_extract_from_store` consumes).
    """
    if var.kind is not VariableKind.COMPOSITE:
        return None
    for cc in var.composite_choices:
        matched = True
        for setting in cc.settings:
            target = variables_by_item.get(setting.label)
            if target is None:
                matched = False
                break
            try:
                current = variable_extract_from_store(target, cv_bytes)
            except UnsupportedVariableKind:
                matched = False
                break
            if current != setting.value:
                matched = False
                break
        if matched and cc.settings:
            return cc.choice
    return None
