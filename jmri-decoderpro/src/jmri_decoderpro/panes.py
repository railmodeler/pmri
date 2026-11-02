"""Pane composition -- combine standard programmer panes + decoder panes.

The DecoderPro-style tab bar isn't a flat list from any single source.
Panes come from:

* the active :class:`~jmri_decoderpro.ProgrammerConfig` (the standard
  Basic / Motor / Function Map / ... tabs);
* the decoder file's own ``<pane>`` elements (extras -- SUSI decoders,
  Digitrax OpSw meta-controls, sound-decoder tabs, ...).

The user's spec pins the *display order* of the standard tabs (Comments
and Legacy Function Map dropped). We follow that order, drop tabs whose
pane has no variables the loaded decoder can serve, then append any
decoder-declared panes that aren't already in the standard set -- matching
JMRI Java's ``decoderFilePanes="yes"`` behaviour.

A synthetic "Roster Entry" tab always comes first (it renders the
roster row itself, not decoder CVs).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from jmri_core.persistence.decoder_xml import DecoderPane, DecoderVariable
from jmri_core.persistence.programmer_xml import ProgrammerConfig


# Fixed display order for the DecoderPro standard tabs, per the spec.
# "Comments" and "Legacy Function Map" are intentionally excluded.
_STANDARD_TAB_ORDER: tuple[str, ...] = (
    "Basic",
    "Motor",
    "Basic Speed Control",
    "Speed Table",
    "Function Map",
    "Lights",
    "Analog Control",
    "Consist",
    "Advanced",
    "Sound",
    "Sound Levels",
    "CVs",
    "Alt Sound Levels",
    "Equalizer",
    "DDE",
    "Reverb",
    "ASC",
    "ID",
)

# Panes we always drop even if a programmer/decoder declares them,
# matching the user's spec.
_DROPPED_TAB_NAMES = frozenset({"Comments", "Legacy Function Map"})

# Marker slug for the synthetic Roster Entry tab.
ROSTER_ENTRY_SLUG = "roster-entry"
ROSTER_ENTRY_NAME = "Roster Entry"


@dataclass(frozen=True)
class ComposedPane:
    """One entry in the composed tab list.

    * ``name`` -- display label (English default)
    * ``slug`` -- URL-safe id used in ``/programmer/loco/<id>/pane/<slug>``
    * ``pane`` -- the underlying :class:`DecoderPane` (None for the
      synthetic Roster Entry tab)
    * ``is_synthetic`` -- True for tabs the framework injects rather than
      pulls from XML (currently just Roster Entry)
    * ``source`` -- where the pane came from: ``"roster"`` |
      ``"programmer"`` | ``"decoder"``
    * ``items_available`` -- variable ``item`` names the loaded decoder
      actually provides. A subset of ``pane.items`` filtered against the
      decoder's variable table.
    """

    name: str
    slug: str
    source: str
    pane: DecoderPane | None = None
    is_synthetic: bool = False
    items_available: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """True when the pane has no variables the decoder can serve.

        The Roster Entry tab is never empty (it always has roster fields).
        A pane with ``<cvtable/>`` or ``<dccaddress/>`` is also never
        empty -- those widgets don't depend on named variables.
        """
        if self.is_synthetic:
            return False
        if self.pane is not None and (
            self.pane.has_cvtable
            or self.pane.has_dcc_address
            or self.pane.has_speed_table
            or self.pane.has_fnmapping
        ):
            return False
        return not self.items_available


def compose_panes(
    variables: list[DecoderVariable] | tuple[DecoderVariable, ...],
    programmer: ProgrammerConfig | None,
    decoder_panes: list[DecoderPane] | tuple[DecoderPane, ...] = (),
    *,
    show_empty: bool | None = None,
) -> list[ComposedPane]:
    """Build the ordered tab list for a loco's programmer view.

    Behaviour:

    1. Synthetic ``Roster Entry`` tab always first.
    2. Standard tabs from *programmer*, filtered against
       ``_STANDARD_TAB_ORDER``. Any standard tab the programmer doesn't
       declare is skipped.
    3. Any decoder-declared pane from *decoder_panes* that isn't already
       covered by name lands after the standard set.
    4. Empty panes are removed unless *show_empty* forces them in
       (mirrors JMRI's ``showEmptyPanes`` semantics; default follows the
       programmer's own setting when *show_empty* is None).
    """
    if show_empty is None and programmer is not None:
        show_empty = programmer.show_empty_panes.lower() == "yes"
    if show_empty is None:
        show_empty = False

    decoder_items = {v.item for v in variables if v.item}

    out: list[ComposedPane] = [
        ComposedPane(
            name=ROSTER_ENTRY_NAME,
            slug=ROSTER_ENTRY_SLUG,
            source="roster",
            is_synthetic=True,
        )
    ]

    # Decoder-declared panes take priority over the standard programmer
    # pane with the same name -- SoundTraxx Tsunami2 ships its own
    # "Function Map" pane (Extended Function Map) that replaces
    # Comprehensive.xml's generic ``<fnmapping/>`` matrix, ESU/Zimo do
    # the same for their model-specific tabs, etc. Index the
    # decoder-declared panes by name up front so the standard-pane pass
    # can substitute them in place, preserving the standard tab order.
    decoder_by_name: dict[str, DecoderPane] = {}
    include_decoder_panes = programmer is None or programmer.decoder_file_panes
    if include_decoder_panes:
        for pane in decoder_panes:
            if pane.name and pane.name not in _DROPPED_TAB_NAMES:
                decoder_by_name.setdefault(pane.name, pane)

    # Standard panes -- declared by the programmer, ordered by spec.
    programmer_by_name: dict[str, DecoderPane] = {}
    if programmer is not None:
        for pane in programmer.panes:
            if pane.name and pane.name not in _DROPPED_TAB_NAMES:
                programmer_by_name.setdefault(pane.name, pane)

    seen_slugs: set[str] = {ROSTER_ENTRY_SLUG}
    for standard_name in _STANDARD_TAB_ORDER:
        pane = programmer_by_name.pop(standard_name, None)
        if pane is None:
            continue
        # Prefer the decoder-declared variant when it exists.
        override = decoder_by_name.pop(standard_name, None)
        if override is not None:
            composed = _compose_one(override, "decoder", decoder_items)
        else:
            composed = _compose_one(pane, "programmer", decoder_items)
        if composed.is_empty and not show_empty:
            continue
        seen_slugs.add(composed.slug)
        out.append(composed)

    # Programmer-declared panes that don't match the fixed spec order --
    # append after the standard block so downstream users still see them.
    for pane in programmer_by_name.values():
        override = decoder_by_name.pop(pane.name, None)
        if override is not None:
            composed = _compose_one(override, "decoder", decoder_items)
        else:
            composed = _compose_one(pane, "programmer", decoder_items)
        if composed.is_empty and not show_empty:
            continue
        if composed.slug in seen_slugs:
            continue
        seen_slugs.add(composed.slug)
        out.append(composed)

    # Remaining decoder-declared panes -- ones with no matching standard
    # tab. Append after the standard block.
    seen_names = {c.name for c in out}
    for pane in decoder_by_name.values():
        if pane.name in seen_names:
            continue
        composed = _compose_one(pane, "decoder", decoder_items)
        if composed.is_empty and not show_empty:
            continue
        if composed.slug in seen_slugs:
            continue
        seen_slugs.add(composed.slug)
        seen_names.add(composed.name)
        out.append(composed)

    return out


def _compose_one(
    pane: DecoderPane, source: str, decoder_items: set[str],
) -> ComposedPane:
    """Filter one pane's items against the decoder's actual variable set.

    Deduplicates repeated items while preserving order. JMRI's real
    pane fragments sometimes reference the same variable twice -- a
    numeric input plus a slider for Vstart, for example -- because
    JMRI's Java UI can render two linked widgets sharing decoder
    state. Our HTML output only has one row per variable, so a repeat
    would produce two independent rows with divergent state after a
    write. Take only the first occurrence.
    """
    seen: set[str] = set()
    items_available: list[str] = []
    for item in pane.items:
        if item not in decoder_items or item in seen:
            continue
        seen.add(item)
        items_available.append(item)
    return ComposedPane(
        name=pane.name,
        slug=slugify_pane_name(pane.name),
        source=source,
        pane=pane,
        items_available=tuple(items_available),
    )


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify_pane_name(name: str) -> str:
    """Turn a pane display name into a URL-safe slug."""
    lower = name.strip().lower()
    slug = _SLUG_STRIP.sub("-", lower).strip("-")
    return slug or "pane"


def find_pane_by_slug(
    panes: list[ComposedPane], slug: str,
) -> ComposedPane | None:
    for p in panes:
        if p.slug == slug:
            return p
    return None
